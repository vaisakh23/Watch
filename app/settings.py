import os
from dotenv import load_dotenv

load_dotenv()

def load_keys():
    i, h = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    return int(i or 0), (h or "")

API_ID, API_HASH = load_keys()
PORT = 5000
HOST = os.environ.get("TG_UI_HOST", "127.0.0.1")
TG_GROUP = os.environ.get("TG_GROUP", "").strip()
SESSION = os.path.expanduser("~/tg_ui_session")
DB_PATH = os.path.expanduser("~/tg_library.db")
TMDB_TOKEN = os.environ.get("TMDB_ACCESS_TOKEN", "").strip()
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_LANGUAGE = os.environ.get("TMDB_LANGUAGE", "en-US").strip()
CHUNK = 512 * 1024
PREFETCH = 8
SYNC_EVERY = 60
