"""
Telegram presence monitor.

Tracks whether monitored accounts are online and reports transitions to the
chat that started monitoring. Contacts and logs persist to a mounted volume so
they survive redeploys.

Environment variables (set in Railway -> Variables):
    API_ID, API_HASH, BOT_TOKEN, SESSION_STRING  (required, read via creds.py)
    DATA_DIR    optional, defaults to /data when present, else current directory
    ADMIN_IDS   optional, comma-separated chat IDs allowed to use the bot.
                If unset, ANY chat that finds the bot can control it.
"""

import asyncio
import json
import os
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import UserStatusOnline, UserStatusOffline

from creds import Credentials

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'

API_ID = Credentials.API_ID
API_HASH = Credentials.API_HASH
BOT_TOKEN = Credentials.BOT_TOKEN
SESSION_STRING = Credentials.SESSION_STRING

DATA_DIR = os.environ.get('DATA_DIR') or ('/data' if os.path.isdir('/data') else '.')
LOG_FILE = os.path.join(DATA_DIR, 'spy_log.txt')
CONTACTS_FILE = os.path.join(DATA_DIR, 'contacts.json')

MAX_LOG_BYTES = 5_000_000
LOG_KEEP_LINES = 10_000
TELEGRAM_CHUNK = 3_900

MIN_DELAY = 30
DEFAULT_DELAY = 60

_admin_raw = os.environ.get('ADMIN_IDS', '').replace(',', ' ').split()
ADMIN_IDS = {int(x) for x in _admin_raw if x.lstrip('-').isdigit()}

HELP_MESSAGE = '\n'.join([
    '/add @username [Label]      add someone to the watch list',
    '/add +14145551234 [Label]   same, by phone (must already be a contact)',
    '/remove <n>                 remove entry n (see /list)',
    '/list                       show the watch list',
    '/clear                      empty the watch list',
    '/start                      begin monitoring',
    '/stop                       stop monitoring',
    '/setdelay <seconds>         poll interval, minimum ' + str(MIN_DELAY),
    '/status                     current state of every tracked account',
    '/logs                       tail the command log',
    '/clearlogs                  wipe the command log',
    '/cleardata                  reset this chat completely',
    '/help                       this message',
])

# --------------------------------------------------------------------------
# Storage helpers
# --------------------------------------------------------------------------

os.makedirs(DATA_DIR, exist_ok=True)

data = {}


def log_line(text):
    """Print to stdout (captured by Railway) and append to the volume."""
    print(text, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(text + '\n')
        if os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
            with open(LOG_FILE) as f:
                tail = f.readlines()[-LOG_KEEP_LINES:]
            with open(LOG_FILE, 'w') as f:
                f.writelines(tail)
    except OSError as exc:
        print(f'log write failed: {exc}', flush=True)


def save_contacts():
    """Persist the watch list. Runtime status is deliberately not saved."""
    payload = {
        str(chat_id): [c.to_dict() for c in state.get('contacts', [])]
        for chat_id, state in data.items()
    }
    try:
        tmp = CONTACTS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, CONTACTS_FILE)
    except OSError as exc:
        log_line(f'contact save failed: {exc}')


def load_contacts():
    if not os.path.exists(CONTACTS_FILE):
        return
    try:
        with open(CONTACTS_FILE) as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        log_line(f'contact load failed: {exc}')
        return
    for chat_id, items in payload.items():
        data[int(chat_id)] = {'contacts': [Contact.from_dict(i) for i in items]}
    log_line(f'loaded contacts for {len(payload)} chat(s)')


def fmt_duration(delta):
    return str(delta).split('.', 1)[0]


def get_state(chat_id):
    state = data.setdefault(chat_id, {})
    state.setdefault('contacts', [])
    return state


# --------------------------------------------------------------------------
# Contact
# --------------------------------------------------------------------------

class Contact:
    def __init__(self, id, name, identifier=None, kind=None):
        self.id = id
        self.name = name
        self.identifier = identifier
        self.kind = kind
        self.online = None
        self.went_online_at = None
        self.went_offline_at = None

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'identifier': self.identifier,
            'kind': self.kind,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d['id'], d['name'], d.get('identifier'), d.get('kind'))

    def state_label(self):
        if self.online is None:
            return 'unknown'
        return 'online' if self.online else 'offline'

    def __str__(self):
        return f'{self.name} ({self.identifier or self.id})'


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
client.start()

bot = TelegramClient(StringSession(), API_ID, API_HASH).start(bot_token=BOT_TOKEN)

load_contacts()

if not ADMIN_IDS:
    log_line('WARNING: ADMIN_IDS is not set. Any chat can control this bot.')


def restricted(handler):
    """Ignore messages from chats that are not on the allow list."""
    async def wrapper(event):
        if ADMIN_IDS and event.chat_id not in ADMIN_IDS:
            log_line(f'ignored command from unauthorised chat {event.chat_id}')
            return
        try:
            return await handler(event)
        except Exception as exc:  # keep one bad command from killing the bot
            log_line(f'{handler.__name__} failed: {type(exc).__name__}: {exc}')
            await event.respond(f'Command failed: {type(exc).__name__}: {exc}')
    wrapper.__name__ = handler.__name__
    return wrapper


# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------

async def report_transition(contact, account, event):
    """Compare the fetched status against what we last saw and report changes."""
    now = datetime.now(timezone.utc)
    status = account.status

    if isinstance(status, UserStatusOnline):
        if contact.online is not True:
            was_offline = (
                fmt_duration(now - contact.went_offline_at)
                if contact.went_offline_at else 'unknown'
            )
            contact.online = True
            contact.went_online_at = now
            await event.respond(
                f'**{contact.name}** is online (offline for {was_offline})'
            )

    elif isinstance(status, UserStatusOffline):
        if contact.online is not False:
            offline_at = status.was_online or now
            was_online = (
                fmt_duration(offline_at - contact.went_online_at)
                if contact.went_online_at else 'unknown'
            )
            contact.online = False
            contact.went_offline_at = offline_at
            await event.respond(
                f'**{contact.name}** went offline (online for {was_online})'
            )

    else:
        # UserStatusRecently / LastWeek / LastMonth / Empty all land here.
        # This usually means the account restricts status visibility rather
        # than that the person is away.
        if contact.online is not False:
            was_online = (
                fmt_duration(now - contact.went_online_at)
                if contact.went_online_at else 'unknown'
            )
            contact.online = False
            contact.went_offline_at = now
            await event.respond(
                f'**{contact.name}** offline or status hidden '
                f'(online for {was_online})'
            )


async def monitor(chat_id, event):
    counter = 0
    while True:
        state = data.get(chat_id)
        if not state or not state.get('is_running'):
            break

        contacts = state.get('contacts', [])
        if not contacts:
            await event.respond('Watch list is empty, stopping.')
            break

        counter += 1
        log_line(f'{datetime.now().strftime(DATETIME_FORMAT)}: poll {chat_id} #{counter}')

        try:
            accounts = await client(GetUsersRequest([c.id for c in contacts]))
        except FloodWaitError as exc:
            log_line(f'flood wait {exc.seconds}s')
            await asyncio.sleep(exc.seconds + 1)
            continue

        by_id = {a.id: a for a in accounts}
        for contact in contacts:
            account = by_id.get(contact.id)
            if account is None:
                continue
            await report_transition(contact, account, event)

        await asyncio.sleep(state.get('delay', DEFAULT_DELAY))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@bot.on(events.NewMessage(pattern=r'^/start$'))
@restricted
async def start(event):
    chat_id = event.chat_id
    state = get_state(chat_id)

    if state.get('is_running'):
        await event.respond('Already running.')
        return

    if not state['contacts']:
        await event.respond('No one on the watch list. Use /add first.')
        return

    state['is_running'] = True
    delay = state.get('delay', DEFAULT_DELAY)
    await event.respond(f'Monitoring started, polling every {delay}s.')

    try:
        await monitor(chat_id, event)
    except Exception as exc:
        log_line(f'monitor crashed: {type(exc).__name__}: {exc}')
        await event.respond(f'Monitoring error: {type(exc).__name__}: {exc}')
    finally:
        if chat_id in data:
            data[chat_id]['is_running'] = False
        await event.respond('Monitoring stopped.')


@bot.on(events.NewMessage(pattern=r'^/stop$'))
@restricted
async def stop(event):
    state = get_state(event.chat_id)
    if not state.get('is_running'):
        await event.respond('Not currently running.')
        return
    state['is_running'] = False
    await event.respond('Stopping after the current poll finishes.')


@bot.on(events.NewMessage(pattern=r'^/add(\s|$)'))
@restricted
async def add(event):
    parts = event.message.message.split(maxsplit=2)

    if len(parts) < 2:
        await event.respond(
            'Usage:\n/add @username [Label]\n/add +14145551234 [Label]'
        )
        return

    identifier = parts[1]
    label = parts[2] if len(parts) > 2 else identifier

    stripped = identifier.replace('-', '').replace(' ', '')
    if identifier.startswith('+') or stripped.isdigit():
        kind = 'phone'
        identifier = '+' + stripped.lstrip('+')
    else:
        kind = 'username'
        identifier = identifier.lstrip('@')

    try:
        entity = await client.get_entity(identifier)
    except (ValueError, TypeError):
        await event.respond(
            f'Could not resolve {identifier}.\n'
            'Usernames must be public. Phone numbers must already be saved '
            'in the monitoring account\'s contacts.'
        )
        return

    state = get_state(event.chat_id)
    if any(c.id == entity.id for c in state['contacts']):
        await event.respond(f'{label} is already on the list.')
        return

    state['contacts'].append(Contact(entity.id, label, identifier, kind))
    save_contacts()
    await event.respond(f'Added {label} ({kind}: {identifier})')


@bot.on(events.NewMessage(pattern=r'^/remove(\s|$)'))
@restricted
async def remove(event):
    parts = event.message.message.split()
    state = get_state(event.chat_id)
    contacts = state['contacts']

    if len(parts) < 2:
        await event.respond('Usage: /remove <number from /list>')
        return

    try:
        index = int(parts[1])
    except ValueError:
        await event.respond(f'"{parts[1]}" is not a number.')
        return

    if not 1 <= index <= len(contacts):
        await event.respond(f'Pick a number between 1 and {len(contacts)}.')
        return

    removed = contacts.pop(index - 1)
    save_contacts()
    await event.respond(f'Removed {removed.name}')


@bot.on(events.NewMessage(pattern=r'^/list$'))
@restricted
async def show_list(event):
    contacts = get_state(event.chat_id)['contacts']
    if not contacts:
        await event.respond('Watch list is empty.')
        return
    lines = [f'{i}. {c}' for i, c in enumerate(contacts, start=1)]
    await event.respond('Watch list:\n' + '\n'.join(lines))


@bot.on(events.NewMessage(pattern=r'^/status$'))
@restricted
async def status(event):
    state = get_state(event.chat_id)
    contacts = state['contacts']
    if not contacts:
        await event.respond('Watch list is empty.')
        return

    running = 'running' if state.get('is_running') else 'stopped'
    delay = state.get('delay', DEFAULT_DELAY)
    lines = [f'Monitoring is {running} (every {delay}s)', '']
    for i, c in enumerate(contacts, start=1):
        lines.append(f'{i}. {c.name}: {c.state_label()}')
    await event.respond('\n'.join(lines))


@bot.on(events.NewMessage(pattern=r'^/clear$'))
@restricted
async def clear(event):
    state = get_state(event.chat_id)
    state['contacts'] = []
    state['is_running'] = False
    save_contacts()
    await event.respond('Watch list cleared, monitoring stopped.')


@bot.on(events.NewMessage(pattern=r'^/cleardata$'))
@restricted
async def clear_data(event):
    chat_id = event.chat_id
    if chat_id in data:
        data[chat_id]['is_running'] = False
        data.pop(chat_id, None)
    save_contacts()
    await event.respond('All data for this chat has been reset.')


@bot.on(events.NewMessage(pattern=r'^/setdelay(\s|$)'))
@restricted
async def set_delay(event):
    parts = event.message.message.split()
    if len(parts) < 2:
        await event.respond(f'Usage: /setdelay <seconds>, minimum {MIN_DELAY}')
        return

    try:
        seconds = int(parts[1])
    except ValueError:
        await event.respond(f'"{parts[1]}" is not a number.')
        return

    if seconds < MIN_DELAY:
        await event.respond(
            f'Minimum is {MIN_DELAY}s. Polling faster than that risks '
            'a Telegram rate limit on the monitoring account.'
        )
        return

    get_state(event.chat_id)['delay'] = seconds
    await event.respond(f'Delay set to {seconds}s (applies from the next poll).')


@bot.on(events.NewMessage(pattern=r'^/logs$'))
@restricted
async def logs(event):
    try:
        with open(LOG_FILE) as f:
            content = f.read()
    except OSError:
        content = ''
    await event.respond(content[-TELEGRAM_CHUNK:] or 'No logs yet.')


@bot.on(events.NewMessage(pattern=r'^/clearlogs$'))
@restricted
async def clear_logs(event):
    try:
        open(LOG_FILE, 'w').close()
        await event.respond('Log file cleared.')
    except OSError as exc:
        await event.respond(f'Could not clear logs: {exc}')


@bot.on(events.NewMessage(pattern=r'^/help$'))
@restricted
async def show_help(event):
    await event.respond(HELP_MESSAGE)


@bot.on(events.NewMessage(pattern=r'^/disconnect$'))
@restricted
async def disconnect(event):
    await event.respond('Shutting down.')
    for chat_state in data.values():
        chat_state['is_running'] = False
    await bot.disconnect()
    await client.disconnect()


@bot.on(events.NewMessage())
@restricted
async def audit_log(event):
    """Record every command issued to the bot."""
    stamp = datetime.now().strftime(DATETIME_FORMAT)
    log_line(f'{stamp}: [{event.chat_id}]: {event.message.message}')


# --------------------------------------------------------------------------

def main():
    log_line(f'bot online, data dir: {DATA_DIR}')
    bot.run_until_disconnected()


if __name__ == '__main__':
    main()
