import re
from urllib.parse import quote

from flask import Blueprint, abort, jsonify, redirect, request

from ..database import db, dbx, dblock
from ..helpers.auth import authed
from ..helpers.chat import default_chat, required_chat
from ..helpers.library import group_rows, metadata_for, topic_list
from ..helpers.presentation import page
from ..helpers.tmdb import TMDB_API_KEY, TMDB_TOKEN, enrich_tmdb, tmdb_state
from ..settings import TG_GROUP
from ..telegram import maybe_sync, start_sync, sync_state

library_blueprint = Blueprint("library", __name__)


@library_blueprint.route("/lib")
def lib():
    if not authed():
        return redirect("/")
    try:
        chat = default_chat()
    except Exception as e:  # noqa: BLE001 - login is required before resolving Telegram usernames
        return page(
            view="group_error", err=f"Could not resolve TG_GROUP {TG_GROUP!r}: {e}"
        )

    # - q is the search text, e.g. /lib?q=Interstellar.
    # - t is a Telegram topic ID, e.g. /lib?t=123.
    q, t = request.args.get("q", "").strip(), request.args.get("t", "")
    maybe_sync(chat)
    enrich_tmdb(chat)
    topics = topic_list(chat)
    sections, grid = [], None
    if q:
        grid = group_rows(chat, "AND title LIKE ?", (f"%{q}%",), 300)
    elif t.isdigit():
        grid = group_rows(chat, "AND topic=?", (int(t),), 300)
    elif len(topics) <= 1:
        grid = group_rows(chat, limit=300) if topics else None
    else:
        sections.append(
            {
                "title": "Recently added",
                "tid": None,
                "groups": group_rows(chat, limit=20),
            }
        )
        for tp in topics:
            sections.append(
                {
                    "title": tp["title"],
                    "tid": tp["id"],
                    "groups": group_rows(chat, "AND topic=?", (tp["id"],), 20),
                }
            )
    return page(
        view="lib",
        chat=chat,
        q=q,
        t=t,
        topics=topics,
        sections=sections,
        grid=grid,
        metadata_enabled=bool(TMDB_TOKEN or TMDB_API_KEY),
    )


@library_blueprint.route("/lib/status")
def lib_status():
    if not authed():
        abort(403)
    chat = required_chat()
    st = sync_state.get(chat, {})
    r = dbx("SELECT synced_at FROM meta WHERE chat=?", (chat,))
    metadata = tmdb_state.get(chat, {})
    return jsonify(
        running=bool(st.get("running")),
        scanned=st.get("scanned", 0),
        found=st.get("found", 0),
        error=st.get("error"),
        last=r[0]["synced_at"] if r else None,
        metadata_running=bool(metadata.get("running")),
        metadata_done=metadata.get("done", 0),
        metadata_enabled=bool(TMDB_TOKEN or TMDB_API_KEY),
        metadata_error=metadata.get("error"),
    )


@library_blueprint.route("/lib/sync", methods=["POST"])
def lib_sync():
    if not authed():
        abort(403)
    chat = required_chat()
    start_sync(chat, full=request.form.get("full") == "1")
    return redirect("/lib")


@library_blueprint.route("/lib/show")
def lib_show():
    if not authed():
        return redirect("/")
    chat, key = required_chat(), request.args.get("key", "")
    rows = dbx(
        "SELECT * FROM items WHERE chat=? AND key=? ORDER BY season, episode, date",
        (chat, key),
    )
    if not rows:
        return redirect("/lib")
    seasons, order = {}, []
    for r in rows:
        sk = r["season"]
        if sk not in seasons:
            seasons[sk] = []
            order.append(sk)
        tag = (
            f"S{r['season']:02d}E{r['episode']:02d}"
            if r["season"] is not None and r["episode"] is not None
            else ""
        )
        seasons[sk].append(
            {
                "mid": r["mid"],
                "fname": r["fname"],
                "size": r["size"],
                "tag": tag,
                "surl": f"/stream/{r['mid']}/{quote(r['fname'])}",
            }
        )
    secs = [
        {"label": f"Season {sk}" if sk is not None else "Files", "files": seasons[sk]}
        for sk in order
    ]
    tmdb = metadata_for(chat, [key]).get(key)
    if tmdb:
        tmdb["poster_url"] = (
            "https://image.tmdb.org/t/p/w500" + tmdb["poster_path"]
            if tmdb["poster_path"]
            else None
        )
        tmdb["backdrop_url"] = (
            "https://image.tmdb.org/t/p/w1280" + tmdb["backdrop_path"]
            if tmdb["backdrop_path"]
            else None
        )
    return page(
        view="show", chat=chat, key=key, name=rows[0]["title"], seasons=secs, tmdb=tmdb
    )


@library_blueprint.route("/lib/rename", methods=["POST"])
def lib_rename():
    if not authed():
        abort(403)
    chat, key = required_chat(), request.form["key"]
    name = request.form["name"].strip()
    newkey = re.sub(r"[^a-z0-9]+", "", name.lower())
    if name and newkey:
        with dblock:
            db.execute(
                "UPDATE items SET key=?, title=? WHERE chat=? AND key=?",
                (newkey, name, chat, key),
            )
            db.execute(
                "UPDATE items SET title=? WHERE chat=? AND key=?", (name, chat, newkey)
            )
            db.execute(
                "INSERT OR REPLACE INTO alias VALUES (?,?,?,?)",
                (chat, key, newkey, name),
            )
            db.commit()
        key = newkey
    return redirect(f"/lib/show?key={quote(key)}")
