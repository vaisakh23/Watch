import json
import mimetypes
import queue
import re
import threading
import time
from difflib import SequenceMatcher
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from flask import Flask, Response, abort, jsonify, redirect, render_template, request
from telethon.errors import SessionPasswordNeededError

from .settings import (CHUNK, PORT, PREFETCH, TMDB_API_KEY, TMDB_LANGUAGE, TMDB_TOKEN, TG_GROUP)
from . import config
from .config import run, configured_chat
from .database import db, dbx, dblock
from .telegram import _get_msg, _next, maybe_sync, start_sync, sync_state

app = Flask(__name__)


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024


app.add_template_filter(human, "human")


def page(**kw):
    kw.setdefault("err", request.args.get("err"))
    kw.setdefault("chat", "")
    kw.setdefault("port", PORT)
    template = {
        "login": "login.html",
        "group_error": "group_error.html",
        "lib": "library.html",
        "show": "show.html",
    }.get(kw.pop("view"))
    if template is None:
        raise ValueError("Unknown page view")
    return render_template(template, **kw)


state = {"stage": "phone", "phone": None}


def authed():
    return run(config.client.is_user_authorized())


def err_redirect(path, e):
    sep = "&" if "?" in path else "?"
    return redirect(f"{path}{sep}err={quote(str(e))}")


@app.route("/")
def index():
    if not authed():
        return page(view="login", stage=state["stage"] if state["stage"] != "main" else "phone")
    state["stage"] = "main"
    return redirect("/lib")


# ----- login -----

@app.route("/login", methods=["POST"])
def login():
    try:
        state["phone"] = request.form["phone"].strip()
        run(config.client.send_code_request(state["phone"]))
        state["stage"] = "code"
    except Exception as e:  # noqa: BLE001
        return err_redirect("/", e)
    return redirect("/")


@app.route("/code", methods=["POST"])
def code():
    try:
        run(config.client.sign_in(state["phone"], request.form["code"].strip()))
        state["stage"] = "main"
    except SessionPasswordNeededError:
        state["stage"] = "password"
    except Exception as e:  # noqa: BLE001
        return err_redirect("/", e)
    return redirect("/")


@app.route("/password", methods=["POST"])
def password():
    try:
        run(config.client.sign_in(password=request.form["password"]))
        state["stage"] = "main"
    except Exception as e:  # noqa: BLE001
        return err_redirect("/", e)
    return redirect("/")


# ----- library -----

def default_chat():
    return configured_chat()


def required_chat():
    try:
        return configured_chat()
    except Exception as e:  # noqa: BLE001 - turn invalid group settings into a route-level failure
        abort(503, description=f"Configured Telegram group could not be resolved: {e}")


def topic_list(chat):
    names = {r["topic_id"]: r["title"] for r in dbx("SELECT topic_id, title FROM topics WHERE chat=?", (chat,))}
    counts = {r["topic"]: r["c"] for r in dbx(
        "SELECT topic, COUNT(DISTINCT key) AS c FROM items WHERE chat=? GROUP BY topic", (chat,))}
    out = []
    for tid in sorted(set(names) | set(counts)):
        if tid == 1:
            title = names.get(1) or ("General" if names else "All files")
        else:
            title = names.get(tid) or f"Topic {tid}"
        out.append({"id": tid, "title": title, "n": counts.get(tid, 0)})
    return [x for x in out if x["n"]]


def tmdb_request(path, params):
    params = dict(params, language=TMDB_LANGUAGE)
    headers = {"Accept": "application/json", "User-Agent": "TG-UI/0.1"}
    if TMDB_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_TOKEN}"
    else:
        params["api_key"] = TMDB_API_KEY
    req = Request(f"https://api.themoviedb.org/3/{path}?{urlencode(params)}", headers=headers)
    with urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def tmdb_lookup(title, year=None):
    params = {"query": title, "include_adult": "false"}
    if year:
        params["year"] = year
    results = tmdb_request("search/movie", params).get("results", [])
    target = re.sub(r"[^a-z0-9]+", "", title.lower())
    def score(item):
        name = item.get("title") or item.get("original_title") or ""
        candidate = re.sub(r"[^a-z0-9]+", "", name.lower())
        ratio = SequenceMatcher(None, target, candidate).ratio()
        released = (item.get("release_date") or "")[:4]
        if year and released and released != str(year):
            ratio -= .25
        return ratio
    match = max(results, key=score, default=None)
    if not match or score(match) < .58:
        return None
    return match


tmdb_state = {}  # chat -> metadata enrichment status
tmdb_lock = threading.Lock()


def enrich_tmdb(chat):
    if not (TMDB_TOKEN or TMDB_API_KEY):
        return
    with tmdb_lock:
        if tmdb_state.get(chat, {}).get("running") or tmdb_state.get(chat, {}).get("error"):
            return
        tmdb_state[chat] = {"running": True, "done": 0, "error": None}

    def worker():
        try:
            pending = dbx("""SELECT i.key, MIN(i.title) AS title, MAX(i.year) AS year
                FROM items i LEFT JOIN tmdb_meta m ON m.chat=i.chat AND m.key=i.key
                WHERE i.chat=? AND i.episode IS NULL AND (m.key IS NULL OR m.fetched_at < ?)
                GROUP BY i.key ORDER BY MAX(i.date) DESC LIMIT 60""", (chat, int(time.time()) - 30 * 86400))
            for item in pending:
                try:
                    result = tmdb_lookup(item["title"], item["year"])
                    with dblock:
                        db.execute("""INSERT OR REPLACE INTO tmdb_meta
                            (chat,key,tmdb_id,media_type,poster_path,backdrop_path,overview,vote_average,release_date,fetched_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""", (chat, item["key"], result.get("id") if result else None,
                            "movie", result.get("poster_path") if result else None,
                            result.get("backdrop_path") if result else None, result.get("overview", "") if result else "",
                            result.get("vote_average") if result else None, result.get("release_date", "") if result else "",
                            int(time.time())))
                        db.commit()
                except Exception:  # noqa: BLE001 - metadata must never block library use
                    tmdb_state[chat]["error"] = "TMDB request failed"
                    break
                tmdb_state[chat]["done"] += 1
                time.sleep(.25)
        finally:
            tmdb_state[chat]["running"] = False

    threading.Thread(target=worker, daemon=True, name="tmdb-enrichment").start()


def metadata_for(chat, keys):
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    return {r["key"]: dict(r) for r in dbx(
        f"SELECT * FROM tmdb_meta WHERE chat=? AND key IN ({marks}) AND tmdb_id IS NOT NULL", (chat, *keys))}


def group_rows(chat, where="", args=(), limit=None):
    sql = ("SELECT key, MIN(title) AS title, MAX(date) AS latest, COUNT(*) AS n, "
           "COUNT(DISTINCT season) AS seasons, COUNT(episode) AS eps, MAX(year) AS year, MAX(size) AS size "
           f"FROM items WHERE chat=? {where} GROUP BY key ORDER BY latest DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    rows = dbx(sql, (chat,) + tuple(args))
    metadata = metadata_for(chat, [r["key"] for r in rows])
    for r in rows:
        g = dict(r)
        g["tmdb"] = metadata.get(g["key"])
        if g["tmdb"]:
            g["poster_url"] = ("https://image.tmdb.org/t/p/w500" + g["tmdb"]["poster_path"]
                                if g["tmdb"]["poster_path"] else None)
            g["backdrop_url"] = ("https://image.tmdb.org/t/p/w1280" + g["tmdb"]["backdrop_path"]
                                  if g["tmdb"]["backdrop_path"] else None)
            g["year"] = int((g["tmdb"]["release_date"] or "0000")[:4]) or g["year"]
        if g["eps"]:
            g["sub"] = f'{g["seasons"]} season{"s" if g["seasons"] != 1 else ""} \u00b7 {g["eps"]} ep{"s" if g["eps"] != 1 else ""}'
        elif g["tmdb"]:
            bits = (["★ %.1f" % g["tmdb"]["vote_average"]] if g["tmdb"]["vote_average"] else [])
            if g["year"]:
                bits.append(str(g["year"]))
            g["sub"] = " \u00b7 ".join(bits) or human(g["size"])
        else:
            g["sub"] = " \u00b7 ".join(x for x in (str(g["year"]) if g["year"] else "", human(g["size"])) if x)
        out.append(g)
    return out


@app.route("/lib")
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
        sections.append({"title": "Recently added", "tid": None, "groups": group_rows(chat, limit=20)})
        for tp in topics:
            sections.append({"title": tp["title"], "tid": tp["id"],
                             "groups": group_rows(chat, "AND topic=?", (tp["id"],), 20)})
    return page(view="lib", chat=chat, q=q, t=t, topics=topics, sections=sections, grid=grid,
                metadata_enabled=bool(TMDB_TOKEN or TMDB_API_KEY))


@app.route("/lib/status")
def lib_status():
    if not authed():
        abort(403)
    chat = required_chat()
    st = sync_state.get(chat, {})
    r = dbx("SELECT synced_at FROM meta WHERE chat=?", (chat,))
    metadata = tmdb_state.get(chat, {})
    return jsonify(running=bool(st.get("running")), scanned=st.get("scanned", 0), found=st.get("found", 0),
                   error=st.get("error"), last=r[0]["synced_at"] if r else None,
                   metadata_running=bool(metadata.get("running")), metadata_done=metadata.get("done", 0),
                   metadata_enabled=bool(TMDB_TOKEN or TMDB_API_KEY), metadata_error=metadata.get("error"))


@app.route("/lib/sync", methods=["POST"])
def lib_sync():
    if not authed():
        abort(403)
    chat = required_chat()
    start_sync(chat, full=request.form.get("full") == "1")
    return redirect("/lib")


@app.route("/lib/show")
def lib_show():
    if not authed():
        return redirect("/")
    chat, key = required_chat(), request.args.get("key", "")
    rows = dbx("SELECT * FROM items WHERE chat=? AND key=? ORDER BY season, episode, date", (chat, key))
    if not rows:
        return redirect("/lib")
    seasons, order = {}, []
    for r in rows:
        sk = r["season"]
        if sk not in seasons:
            seasons[sk] = []
            order.append(sk)
        tag = f'S{r["season"]:02d}E{r["episode"]:02d}' if r["season"] is not None and r["episode"] is not None else ""
        seasons[sk].append(
            {
                "mid": r["mid"],
                "fname": r["fname"],
                "size": r["size"],
                "tag": tag,
                "surl": f"/stream/{r['mid']}/{quote(r['fname'])}",
            }
        )
    secs = [{"label": f"Season {sk}" if sk is not None else "Files", "files": seasons[sk]} for sk in order]
    tmdb = metadata_for(chat, [key]).get(key)
    if tmdb:
        tmdb["poster_url"] = "https://image.tmdb.org/t/p/w500" + tmdb["poster_path"] if tmdb["poster_path"] else None
        tmdb["backdrop_url"] = "https://image.tmdb.org/t/p/w1280" + tmdb["backdrop_path"] if tmdb["backdrop_path"] else None
    return page(view="show", chat=chat, key=key, name=rows[0]["title"], seasons=secs, tmdb=tmdb)


@app.route("/lib/rename", methods=["POST"])
def lib_rename():
    if not authed():
        abort(403)
    chat, key = required_chat(), request.form["key"]
    name = request.form["name"].strip()
    newkey = re.sub(r"[^a-z0-9]+", "", name.lower())
    if name and newkey:
        with dblock:
            db.execute("UPDATE items SET key=?, title=? WHERE chat=? AND key=?", (newkey, name, chat, key))
            db.execute("UPDATE items SET title=? WHERE chat=? AND key=?", (name, chat, newkey))
            db.execute("INSERT OR REPLACE INTO alias VALUES (?,?,?,?)", (chat, key, newkey, name))
            db.commit()
        key = newkey
    return redirect(f"/lib/show?key={quote(key)}")


# ----- streaming (HTTP Range) -----

def parse_range(header, size):
    """Return (start, end) inclusive, None if no Range header, or 'bad'."""
    if not header:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
    if not m:
        return None
    s, e = m.groups()
    if s == "" and e == "":
        return None
    if s == "":
        start, end = max(size - int(e), 0), size - 1
    else:
        start = int(s)
        end = min(int(e), size - 1) if e else size - 1
    if start > end or start >= size:
        return "bad"
    return start, end


@app.route("/stream/<int:mid>/<path:fname>")
def stream(mid, fname):
    if not authed():
        abort(403)
    chat = required_chat()
    try:
        msg = run(_get_msg(chat, mid))
    except Exception:  # noqa: BLE001
        abort(404)
    if not msg or not msg.file:
        abort(404)

    size = msg.file.size
    if not size:
        abort(415)
    mime = msg.file.mime_type or mimetypes.guess_type(fname)[0] or "application/octet-stream"

    rng = parse_range(request.headers.get("Range"), size)
    if rng == "bad":
        return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = rng if rng else (0, size - 1)
    length = end - start + 1
    aligned = start - (start % CHUNK)
    skip = start - aligned

    def open_iter(m):
        return m.client.iter_download(m.media, offset=aligned, request_size=CHUNK, file_size=size)

    it = open_iter(msg)
    q = queue.Queue(maxsize=PREFETCH)
    stop = threading.Event()

    def put(item):
        while not stop.is_set():
            try:
                q.put(item, timeout=1)
                return True
            except queue.Full:
                pass
        return False

    def producer():
        nonlocal it
        retried = False
        try:
            while not stop.is_set():
                try:
                    chunk = run(_next(it))
                except StopAsyncIteration:
                    break
                except Exception:  # noqa: BLE001
                    if retried:
                        raise
                    retried = True   # file reference may have expired: refetch the message once
                    fresh = run(_get_msg(chat, mid, fresh=True))
                    it = open_iter(fresh)
                    continue
                if not put(chunk):
                    return
        except Exception as e:  # noqa: BLE001
            put(e)
        finally:
            put(None)
            try:
                run(it.close())
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=producer, daemon=True).start()

    def gen():
        remaining, to_skip = length, skip
        try:
            while remaining > 0:
                chunk = q.get()
                if chunk is None or isinstance(chunk, Exception):
                    break
                chunk = bytes(chunk)
                if to_skip:
                    chunk, to_skip = chunk[to_skip:], 0
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                remaining -= len(chunk)
                yield chunk
        finally:
            stop.set()

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Content-Type": mime,
               "Content-Disposition": f'inline; filename="{quote(fname)}"', "Cache-Control": "no-store"}
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(gen(), status=206, headers=headers, direct_passthrough=True)
    return Response(gen(), status=200, headers=headers, direct_passthrough=True)

