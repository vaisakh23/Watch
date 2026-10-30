from .auth import auth_blueprint
from .library import library_blueprint
from .streaming import streaming_blueprint


def register_routes(app):
    app.register_blueprint(auth_blueprint)
    app.register_blueprint(library_blueprint)
    app.register_blueprint(streaming_blueprint)
