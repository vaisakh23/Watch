import asyncio
import os
import time

from telethon import functions

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


sync_state = {}  # chat -> dict(running, scanned, found, error, done_at)


def start_sync(chat, full=False):
    st = sync_state.setdefault(chat, {})
    if st.get("running"):
        return
    st.update(running=True, scanned=0, found=0, error=None)
    asyncio.run_coroutine_threadsafe(_sync(chat, full), loop)


def maybe_sync(chat):
    st = sync_state.get(chat, {})
    if st.get("running"):
        return
    r = dbx("SELECT synced_at FROM meta WHERE chat=?", (chat,))
    last = r[0]["synced_at"] if r else 0
    now = time.time()
    if now - last > settings.sync_every and now - st.get("done_at", 0) > settings.sync_every:
        start_sync(chat)


async def _sync(chat, full=False):
    st = sync_state.setdefault(chat, {})
    st.update(running=True, scanned=0, found=0, error=None)
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

        if full:
            dbx("DELETE FROM items WHERE chat=?", (chat,))
            dbx("DELETE FROM meta WHERE chat=?", (chat,))

        r = dbx("SELECT last_id FROM meta WHERE chat=?", (chat,))
        last = r[0]["last_id"] if r else 0
        alias = {
            a["old_key"]: (a["new_key"], a["new_title"])
            for a in dbx("SELECT * FROM alias WHERE chat=?", (chat,))
        }

        newmax, batch = last, []
        async for m in config.client.iter_messages(
            ent, min_id=last
        ):  # only messages newer than last sync
            st["scanned"] += 1
            newmax = max(newmax, m.id)
            f = m.file
            if not f or not is_media(f):
                continue
            name = f.name or (m.text or "").split("\n")[0].strip() or f"file_{m.id}"
            if not os.path.splitext(name)[1]:
                name += f.ext or ""
            info = parse(name, m.text or "")
            key, title = apply_alias(alias, make_key(info), info["title"])
            batch.append(
                (
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
            )
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


