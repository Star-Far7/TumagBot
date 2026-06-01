import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/umag_bot.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE: str = os.getenv("LOG_FILE", "logs/bot.log")

_raw_users = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: list[int] = [int(x.strip()) for x in _raw_users.split(",") if x.strip()]

GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")

AUTO_MATCH_THRESHOLD: float = float(os.getenv("AUTO_MATCH_THRESHOLD", "0.82"))
SUGGEST_THRESHOLD: float = float(os.getenv("SUGGEST_THRESHOLD", "0.40"))
MAX_SUGGESTIONS: int = int(os.getenv("MAX_SUGGESTIONS", "5"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env файле")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY не задан в .env файле")
