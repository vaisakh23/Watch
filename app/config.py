import asyncio
import threading

from telethon import TelegramClient, utils
from telethon.tl import types

from .settings import settings

# ---------------- Telethon runs in its own event-loop thread ----------------
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


client = None
_configured_chat_id = None


async def _init():
    global client
    client = TelegramClient(
        settings.session, settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()


async def _resolve_configured_group():
    peer = await _peer(settings.tg_group)
    entity = await client.get_entity(peer)
    if isinstance(entity, types.User) or getattr(entity, "broadcast", False):
        raise ValueError(
            "TG_GROUP must identify a Telegram group or supergroup, not a user or broadcast channel."
        )
    return str(utils.get_peer_id(entity))


def configured_chat():
    """Return the configured group's canonical Telegram ID, resolving it once per process."""
    global _configured_chat_id
    if not settings.tg_group:
        raise RuntimeError(
            "TG_GROUP is required. Set it to the group's @username or numeric ID."
        )
    if _configured_chat_id is None:
        _configured_chat_id = run(_resolve_configured_group())
    return _configured_chat_id




async def _peer(chat):
    x = int(chat) if str(chat).lstrip("-").isdigit() else chat
    try:
        await client.get_input_entity(x)
    except ValueError:
        async for _ in client.iter_dialogs():
            pass
        await client.get_input_entity(x)
    return x
