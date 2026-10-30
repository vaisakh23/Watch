from flask import Flask

from .helpers.presentation import human
from .routes import register_routes

app = Flask(__name__)
app.add_template_filter(human, "human")
register_routes(app)
