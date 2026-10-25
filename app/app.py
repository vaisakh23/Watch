from .settings import API_ID, API_HASH, HOST, PORT, TG_GROUP
from .config import run, _init
from .web import app


def main():
    if not API_ID or not API_HASH:
        raise SystemExit("API keys not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env.")
    if not TG_GROUP:
        raise SystemExit("Telegram group not configured. Set TG_GROUP to the group's @username or numeric ID in .env.")
    run(_init())
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
