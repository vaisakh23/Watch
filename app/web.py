from flask import Flask
import time

from .helpers.presentation import human
from .routes import register_routes

app = Flask(__name__)
app.add_template_filter(human, "human")
app.add_template_filter(lambda ts: time.strftime("%d %b %Y", time.localtime(ts)), "date")
register_routes(app)
