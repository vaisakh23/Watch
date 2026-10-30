from flask import abort

from ..config import configured_chat


def default_chat():
    return configured_chat()


def required_chat():
    try:
        return configured_chat()
    except Exception as e:  # noqa: BLE001 - turn invalid group settings into a route-level failure
        abort(503, description=f"Configured Telegram group could not be resolved: {e}")
