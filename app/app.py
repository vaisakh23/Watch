import logging

from .settings import settings
from .config import run, _init
from .telegram import register_message_handlers
from .web import app
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


def main():
    configure_logging()
    settings.validate()
    try:
        run(_init())
    except Exception:  # noqa: BLE001
        logger.exception("Telegram connection failed")
        raise
    logger.info("Telegram client connected; starting media library for %s", settings.tg_group)
    register_message_handlers()
    if not (settings.tmdb_token or settings.tmdb_api_key):
        logger.warning("TMDB metadata lookup is disabled; set TMDB_ACCESS_TOKEN or TMDB_API_KEY to enable it")
    logger.info("Web interface listening on http://%s:%s", settings.host, settings.port)
    app.run(host=settings.host, port=settings.port, threaded=True)


if __name__ == "__main__":
    main()
