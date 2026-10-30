from .settings import settings
from .config import run, _init
from .telegram import register_message_handlers
from .web import app


def main():
    settings.validate()
    run(_init())
    register_message_handlers()
    app.run(host=settings.host, port=settings.port, threaded=True)


if __name__ == "__main__":
    main()
