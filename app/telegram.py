import asyncio
import os
import time

from telethon import events, functions

from . import config
from .config import loop, run, _peer
from .settings import settings
from .database import db, dbx, dblock, save_items
from .media import VIDEO_EXT, parse, make_key, apply_alias

# ----------------------- peers / messages / sync -----------------------


def is_media(f):
    mime = (f.mime_type or "").lower()
    if mime.startswith(("video/", "audio/")):
        return True
    return (f.name or "").lower().endswith(VIDEO_EXT)


def topic_of(m):
    rt = m.reply_to
    if rt and getattr(rt, "forum_topic", False):
        return rt.reply_to_top_id or rt.reply_to_msg_id or 1
    return 1


def _aliases(chat):
    return {
        a["old_key"]: (a["new_key"], a["new_title"])
        for a in dbx("SELECT * FROM alias WHERE chat=?", (chat,))
    }


def _item_row(chat, m, alias=None):
    """Build the database row for a supported media message, or return None."""
    f = m.file
    if not f or not is_media(f):
        return None
    name = f.name or (m.text or "").split("\n")[0].strip() or f"file_{m.id}"
    if not os.path.splitext(name)[1]:
        name += f.ext or ""
    info = parse(name, m.text or "")
    aliases = alias if alias is not None else _aliases(chat)
    key, title = apply_alias(aliases, make_key(info), info["title"])
    return (
        chat,
        m.id,
        topic_of(m),
        name,
        f.size or 0,
        int(m.date.timestamp()),
        title,
        key,
        info["season"],
        info["episode"],
        info["year"],
    )


_handlers_registered = False


async def _event_chat():
    chat = config._configured_chat_id
    if chat is None:
        chat = await config._resolve_configured_group()
        config._configured_chat_id = chat
    return chat


def register_message_handlers():
    """Listen for library changes. Call after connecting the Telethon client."""
    global _handlers_registered
    if _handlers_registered:
        return

    async def on_message(event):
        try:
            chat = await _event_chat()
            if event.chat_id != int(chat):
                return
            m = event.message
            row = _item_row(chat, m)
            if row:
                save_items([row])
            else:
                dbx("DELETE FROM items WHERE chat=? AND mid=?", (chat, m.id))
            msg_cache.pop((chat, m.id), None)
        except Exception as e:  # noqa: BLE001
            print(f"Telegram message event failed: {e}")

    async def on_deleted(event):
        if event.chat_id is None:
            return
        try:
            chat = await _event_chat()
            if event.chat_id != int(chat):
                return
            for mid in event.deleted_ids:
                dbx("DELETE FROM items WHERE chat=? AND mid=?", (chat, mid))
                msg_cache.pop((chat, mid), None)
        except Exception as e:  # noqa: BLE001
            print(f"Telegram deletion event failed: {e}")

    config.client.add_event_handler(on_message, events.NewMessage())
    config.client.add_event_handler(on_message, events.MessageEdited())
    config.client.add_event_handler(on_deleted, events.MessageDeleted())
    _handlers_registered = True


sync_state = {}  # chat -> dict(running, scanned, found, error, done_at)


def start_sync(chat, changes=False):
    st = sync_state.setdefault(chat, {})
    if st.get("running"):
        return
    st.update(
        running=True, scanned=0, found=0, total=0, error=None, phase="starting"
    )
    asyncio.run_coroutine_threadsafe(_sync(chat, changes), loop)


def maybe_sync(chat):
    st = sync_state.get(chat, {})
    if st.get("running"):
        return
    r = dbx("SELECT synced_at FROM meta WHERE chat=?", (chat,))
    last = r[0]["synced_at"] if r else 0
    now = time.time()
    if (
        now - last > settings.sync_every
        and now - st.get("done_at", 0) > settings.sync_every
    ):
        start_sync(chat)


async def _sync(chat, changes=False):
    st = sync_state.setdefault(chat, {})
    st.update(
        running=True,
        scanned=0,
        found=0,
        total=0,
        error=None,
        phase="checking existing messages" if changes else "scanning new messages",
    )
    try:
        peer = await _peer(chat)
        ent = await config.client.get_entity(peer)

        try:  # forum topics (names of your Movies / Tv shows / Anime sections)
            res = await config.client(
                functions.messages.GetForumTopicsRequest(
                    peer=ent, offset_date=0, offset_id=0, offset_topic=0, limit=100
                )
            )
            rows = [(chat, t.id, t.title) for t in res.topics if hasattr(t, "title")]
            with dblock:
                db.executemany("INSERT OR REPLACE INTO topics VALUES (?,?,?)", rows)
                db.commit()
        except Exception:  # noqa: BLE001  (not a forum group)
            pass

        r = dbx("SELECT last_id FROM meta WHERE chat=?", (chat,))
        last = r[0]["last_id"] if r else 0
        alias = _aliases(chat)

        if changes:
            # Re-fetch indexed messages in chunks to repair missed edits/deletions.
            r = dbx("SELECT COUNT(*) AS total FROM items WHERE chat=?", (chat,))
            st["total"] = r[0]["total"] if r else 0
            last_mid = 0
            while True:
                rows = dbx(
                    "SELECT mid FROM items WHERE chat=? AND mid>? ORDER BY mid LIMIT 100",
                    (chat, last_mid),
                )
                batch_ids = [r["mid"] for r in rows]
                if not batch_ids:
                    break
                last_mid = batch_ids[-1]
                messages = await config.client.get_messages(ent, ids=batch_ids)
                by_id = {m.id: m for m in messages if m}
                for mid in batch_ids:
                    m = by_id.get(mid)
                    if m:
                        row = _item_row(chat, m, alias)
                        if row:
                            save_items([row])
                            st["found"] += 1
                        else:
                            dbx("DELETE FROM items WHERE chat=? AND mid=?", (chat, mid))
                    else:
                        dbx("DELETE FROM items WHERE chat=? AND mid=?", (chat, mid))
                    msg_cache.pop((chat, mid), None)
                    st["scanned"] += 1

        st["phase"] = "scanning new messages"
        newmax, batch = last, []
        async for m in config.client.iter_messages(
            ent, min_id=last
        ):  # only messages newer than last sync
            st["scanned"] += 1
            newmax = max(newmax, m.id)
            row = _item_row(chat, m, alias)
            if not row:
                continue
            batch.append(row)
            st["found"] += 1
            if len(batch) >= 200:
                save_items(batch)
                batch = []
        save_items(batch)
        dbx(
            "INSERT OR REPLACE INTO meta VALUES (?,?,?)",
            (chat, newmax, int(time.time())),
        )
    except Exception as e:  # noqa: BLE001
        st["error"] = str(e)
    finally:
        st["running"] = False
        st["phase"] = "done"
        st["done_at"] = time.time()


msg_cache = {}  # (chat, mid) -> message (so seeking doesn't refetch every time)


async def _get_msg(chat, mid, fresh=False):
    key = (chat, mid)
    if fresh or key not in msg_cache:
        m = await config.client.get_messages(await _peer(chat), ids=mid)
        if not m:
            return None
        msg_cache[key] = m
        if len(msg_cache) > 50:
            msg_cache.pop(next(iter(msg_cache)))
    return msg_cache[key]


async def _next(it):
    return await it.__anext__()
