from .settings import settings
from .config import run, _init
from .web import app


def main():
    settings.validate()
    run(_init())
    app.run(host=settings.host, port=settings.port, threaded=True)


if __name__ == "__main__":
    main()
