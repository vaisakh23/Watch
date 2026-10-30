import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    host: str
    port: int
    tg_group: str
    session: str
    db_path: str
    tmdb_token: str
    tmdb_api_key: str
    tmdb_language: str
    chunk: int
    prefetch: int
    sync_every: int

    @classmethod
    def from_env(cls):
        return cls(
            telegram_api_id=int(os.environ.get("TELEGRAM_API_ID") or 0),
            telegram_api_hash=os.environ.get("TELEGRAM_API_HASH") or "",
            host=os.environ.get("TG_UI_HOST", "127.0.0.1"),
            port=5000,
            tg_group=os.environ.get("TG_GROUP", "").strip(),
            session=os.path.expanduser("~/tg_ui_session"),
            db_path=os.path.expanduser("~/tg_library.db"),
            tmdb_token=os.environ.get("TMDB_ACCESS_TOKEN", "").strip(),
            tmdb_api_key=os.environ.get("TMDB_API_KEY", "").strip(),
            tmdb_language=os.environ.get("TMDB_LANGUAGE", "en-US").strip(),
            chunk=512 * 1024,
            prefetch=8,
            sync_every=60,
        )

    def validate(self):
        if not self.telegram_api_id or not self.telegram_api_hash:
            raise SystemExit(
                "API keys not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env."
            )
        if not self.tg_group:
            raise SystemExit(
                "Telegram group not configured. Set TG_GROUP to the group's @username or numeric ID in .env."
            )


settings = Settings.from_env()
