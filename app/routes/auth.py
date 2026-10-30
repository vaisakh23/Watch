from flask import Blueprint, redirect, request
from telethon.errors import SessionPasswordNeededError

from .. import config
from ..config import run
from ..helpers.auth import authed, err_redirect, state
from ..helpers.presentation import page

auth_blueprint = Blueprint("auth", __name__)


@auth_blueprint.route("/")
def index():
    if not authed():
        return page(
            view="login", stage=state["stage"] if state["stage"] != "main" else "phone"
        )
    state["stage"] = "main"
    return redirect("/lib")


@auth_blueprint.route("/login", methods=["POST"])
def login():
    try:
        state["phone"] = request.form["phone"].strip()
        run(config.client.send_code_request(state["phone"]))
        state["stage"] = "code"
    except Exception as e: 
        return err_redirect("/", e)
    return redirect("/")


@auth_blueprint.route("/code", methods=["POST"])
def code():
    try:
        run(config.client.sign_in(state["phone"], request.form["code"].strip()))
        state["stage"] = "main"
    except SessionPasswordNeededError:
        state["stage"] = "password"
    except Exception as e:
        return err_redirect("/", e)
    return redirect("/")


@auth_blueprint.route("/password", methods=["POST"])
def password():
    try:
        run(config.client.sign_in(password=request.form["password"]))
        state["stage"] = "main"
    except Exception as e:
        return err_redirect("/", e)
    return redirect("/")
