from urllib.parse import quote

from flask import redirect

from .. import config
from ..config import run

state = {"stage": "phone", "phone": None}


def authed():
    return run(config.client.is_user_authorized())


def err_redirect(path, e):
    sep = "&" if "?" in path else "?"
    return redirect(f"{path}{sep}err={quote(str(e))}")
