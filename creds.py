from decouple import config
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())


class Credentials:
    API_ID: int = config("API_ID", cast=int)
    API_HASH: str = config("API_HASH")
    BOT_TOKEN: str = config("BOT_TOKEN")
    USER_NAME: str = config("USER_NAME")
    SESSION_STRING: str = config("SESSION_STRING")
