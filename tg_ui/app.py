"""
Telegram library + stream UI (Flask + Telethon) for Termux on Android.
Open http://localhost:5000 in your phone browser.

Tested against: Telethon 1.45.0, Flask 3.1.x

API keys are read from (first found):
  1. environment variables TG_API_ID / TG_API_HASH
  2. the project .env file

Library data is cached in ~/tg_library.db, thumbnails in ~/.tg_thumbs.
After the first scan, only NEW messages are fetched on each sync.
"""
import asyncio
from difflib import SequenceMatcher
import mimetypes
import os
import queue
import re
import sqlite3
import threading
import time
import json
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.errors import SessionPasswordNeededError


# ----------------------------- config -----------------------------

load_dotenv()


def load_keys():
    i, h = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    return int(i or 0), (h or "")


API_ID, API_HASH = load_keys()
PORT = 5000
HOST = os.environ.get("TG_UI_HOST", "0.0.0.0")
DOWNLOAD_DIR = os.path.expanduser("~/storage/downloads/Telegram")
SESSION = os.path.expanduser("~/tg_ui_session")     # keep this file private!
DB_PATH = os.path.expanduser("~/tg_library.db")
THUMB_DIR = os.path.expanduser("~/.tg_thumbs")
TMDB_TOKEN = os.environ.get("TMDB_ACCESS_TOKEN", "").strip()
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TMDB_LANGUAGE = os.environ.get("TMDB_LANGUAGE", "en-US").strip()
CHUNK = 512 * 1024      # multiple of 4096 that divides 1 MB evenly
PREFETCH = 8            # chunks fetched ahead while streaming
SYNC_EVERY = 60         # seconds; opening the library re-syncs if older than this

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

# ---------------- Telethon runs in its own event-loop thread ----------------
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


client = None


async def _init():
    global client
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()


# ----------------------------- database -----------------------------

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
dblock = threading.RLock()
db.executescript("""
CREATE TABLE IF NOT EXISTS items(
  chat TEXT, mid INTEGER, topic INTEGER, fname TEXT, size INTEGER, date INTEGER,
  title TEXT, key TEXT, season INTEGER, episode INTEGER, year INTEGER,
  PRIMARY KEY(chat, mid));
CREATE INDEX IF NOT EXISTS items_key ON items(chat, key);
CREATE TABLE IF NOT EXISTS topics(chat TEXT, topic_id INTEGER, title TEXT, PRIMARY KEY(chat, topic_id));
CREATE TABLE IF NOT EXISTS meta(chat TEXT PRIMARY KEY, last_id INTEGER, synced_at INTEGER);
CREATE TABLE IF NOT EXISTS alias(chat TEXT, old_key TEXT, new_key TEXT, new_title TEXT, PRIMARY KEY(chat, old_key));
CREATE TABLE IF NOT EXISTS tmdb_meta(
  chat TEXT, key TEXT, tmdb_id INTEGER, media_type TEXT, poster_path TEXT, backdrop_path TEXT,
  overview TEXT, vote_average REAL, release_date TEXT, fetched_at INTEGER,
  PRIMARY KEY(chat, key));
""")
db.commit()


def dbx(sql, args=()):
    with dblock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.fetchall()


def save_items(rows):
    if not rows:
        return
    with dblock:
        db.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()


# ------------------------ filename parsing ------------------------

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg",
             ".mpeg", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav")

QUALITY = re.compile(
    r"(?i)\b(480p|576p|720p|1080p|1440p|2160p|4k|uhd|blu[ -]?ray|brrip|bdrip|web[ -]?dl|web[ -]?rip|"
    r"hdrip|hdtv|dvdrip|hdcam|x26[45]|h[ .]?26[45]|hevc|10[ -]?bit|remux|proper|repack|"
    r"extended|unrated|pahe|yify|yts|rarbg)\b")


def clean(s):
    s = re.sub(r"(?i)\.(mkv|mp4|avi|mov|webm|m4v|ts|flv|wmv|mpg|mpeg|mp3|m4a|flac|ogg|opus|wav)$", "", s.strip())
    for _ in range(3):  # leading [Group] or (tag)
        s2 = re.sub(r"^\s*(\[[^\]]*\]|\([^)]*\))\s*", "", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"(?i)^(www\.)?[\w-]+\.(com|net|org|in|to|io|cc|xyz|me|tv)\s*[-\u2013_ ]+", "", s)
    s = re.sub(r"[._]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def finish_title(t):
    t = re.sub(r"[\s\-\u2013:(\[]+$", "", t.strip())
    t = re.sub(r"^[\s\-\u2013:)\]]+", "", t)
    if t and (t.islower() or t.isupper()):
        t = t.title()
    return t or "Untitled"


def parse_one(raw):
    """Return (info, matched). info = title/season/episode/year."""
    s = clean(raw)
    # TV: S01E10 or 1x10
    m = re.search(r"(?i)^(.*?)[\s(\[]*\b(?:s(\d{1,2})\s?e(\d{1,3})|(\d{1,2})x(\d{2,3}))\b", s)
    if m:
        season = int(m.group(2) or m.group(4))
        ep = int(m.group(3) or m.group(5))
        return {"title": finish_title(m.group(1)), "season": season, "episode": ep, "year": None}, True
    # Anime style: "Show Name - 05"
    m = re.search(r"^(.+?)\s+-\s+(\d{1,3})(?:v\d)?(?=\s|$|\[|\()", s)
    if m and not re.search(r"(?:19|20)\d{2}$", m.group(1).strip()):
        return {"title": finish_title(m.group(1)), "season": 1, "episode": int(m.group(2)), "year": None}, True
    # Movie: title + year
    mq = QUALITY.search(s)
    cut = (s[:mq.start()] if mq else s).strip()
    m = (re.search(r"^(.+?)[\s(\[]+((?:19|20)\d{2})[\s)\]]*$", cut)
         or re.search(r"^(.+?)[\s(\[]+((?:19|20)\d{2})\b", cut))
    if m:
        return {"title": finish_title(m.group(1)), "season": None, "episode": None, "year": int(m.group(2))}, True
    return {"title": finish_title(cut), "season": None, "episode": None, "year": None}, False


def parse(name, caption=""):
    info, ok = parse_one(name)
    if not ok and caption:
        info2, ok2 = parse_one(caption.split("\n")[0])
        if ok2:
            return info2
    return info


def make_key(info):
    k = re.sub(r"[^a-z0-9]+", "", info["title"].lower()) or "untitled"
    if info["season"] is None and info.get("year"):
        k += str(info["year"])
    return k


def apply_alias(alias, key, title):
    for _ in range(5):
        a = alias.get(key)
        if not a:
            break
        key, title = a
    return key, title


# ----------------------- peers / messages / sync -----------------------

async def _peer(chat):
    x = int(chat) if re.fullmatch(r"-?\d+", chat) else chat
    try:
        await client.get_input_entity(x)
    except ValueError:
        async for _ in client.iter_dialogs():   # refresh entity cache once
            pass
        await client.get_input_entity(x)
    return x


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


sync_state = {}   # chat -> dict(running, scanned, found, error, done_at)


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
    if now - last > SYNC_EVERY and now - st.get("done_at", 0) > SYNC_EVERY:
        start_sync(chat)


async def _sync(chat, full=False):
    st = sync_state.setdefault(chat, {})
    st.update(running=True, scanned=0, found=0, error=None)
    try:
        peer = await _peer(chat)
        ent = await client.get_entity(peer)

        try:   # forum topics (names of your Movies / Tv shows / Anime sections)
            res = await client(functions.messages.GetForumTopicsRequest(
                peer=ent, offset_date=0, offset_id=0, offset_topic=0, limit=100))
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
        alias = {a["old_key"]: (a["new_key"], a["new_title"])
                 for a in dbx("SELECT * FROM alias WHERE chat=?", (chat,))}

        newmax, batch = last, []
        async for m in client.iter_messages(ent, min_id=last):   # only messages newer than last sync
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
            batch.append((chat, m.id, topic_of(m), name, f.size or 0, int(m.date.timestamp()),
                          title, key, info["season"], info["episode"], info["year"]))
            st["found"] += 1
            if len(batch) >= 200:
                save_items(batch)
                batch = []
        save_items(batch)
        dbx("INSERT OR REPLACE INTO meta VALUES (?,?,?)", (chat, newmax, int(time.time())))
    except Exception as e:  # noqa: BLE001
        st["error"] = str(e)
    finally:
        st["running"] = False
        st["done_at"] = time.time()


async def _messages(chat, limit):
    peer = await _peer(chat)
    out = []
    async for m in client.iter_messages(peer, limit=limit):
        btns = []
        if m.buttons:
            for i, row in enumerate(m.buttons):
                for j, b in enumerate(row):
                    btns.append({"i": i, "j": j, "text": b.text, "url": b.url})
        f = m.file
        item = {"id": m.id, "text": (m.text or "")[:400], "mine": m.out, "btns": btns,
                "fname": None, "size": 0, "surl": None}
        if f:
            fname = f.name or f"file_{m.id}{f.ext or ''}"
            item.update(fname=fname, size=f.size or 0)
            if is_media(f):
                item["surl"] = f"/stream/{m.id}/{quote(fname)}?chat={quote(chat)}"
        out.append(item)
    return out


async def _send(chat, text):
    await client.send_message(await _peer(chat), text)


async def _click(chat, mid, i, j):
    m = await _get_msg(chat, mid, fresh=True)
    await m.click(i, j)


async def _dialogs():
    out = []
    async for d in client.iter_dialogs(limit=300):
        e = d.entity
        out.append({"id": d.id, "name": d.name or str(d.id),
                    "forum": bool(getattr(e, "forum", False)), "bot": bool(getattr(e, "bot", False))})
    out.sort(key=lambda c: (not c["forum"], c["name"].lower()))
    return out


msg_cache = {}   # (chat, mid) -> message (so seeking doesn't refetch every time)


async def _get_msg(chat, mid, fresh=False):
    key = (chat, mid)
    if fresh or key not in msg_cache:
        m = await client.get_messages(await _peer(chat), ids=mid)
        if not m:
            return None
        msg_cache[key] = m
        if len(msg_cache) > 50:
            msg_cache.pop(next(iter(msg_cache)))
    return msg_cache[key]


async def _thumb(chat, mid):
    m = await _get_msg(chat, mid)
    if not m:
        return None
    return await client.download_media(m, file=bytes, thumb=-1)


jobs = {}   # key -> dict(name, done, total, status, path, error)


async def _download(chat, mid, key):
    job = jobs[key]
    try:
        m = await _get_msg(chat, mid, fresh=True)
        if not m:
            raise RuntimeError("message no longer exists")

        def cb(cur, total):
            job["done"], job["total"] = cur, total

        path = await m.download_media(file=DOWNLOAD_DIR + "/", progress_callback=cb)
        job.update(status="done", path=str(path))
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e))


async def _next(it):
    return await it.__anext__()


# ----------------------------- web app -----------------------------

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
    kw.setdefault("dl_dir", DOWNLOAD_DIR)
    kw.setdefault("port", PORT)
    return render_template("page.html", **kw)


state = {"stage": "phone", "phone": None}


def authed():
    return run(client.is_user_authorized())


def err_redirect(path, e):
    sep = "&" if "?" in path else "?"
    return redirect(f"{path}{sep}err={quote(str(e))}")


# ----- chat browser (bots, channels, any chat) -----

@app.route("/")
def index():
    if not authed():
        return page(view="login", stage=state["stage"] if state["stage"] != "main" else "phone")
    state["stage"] = "main"
    chat = request.args.get("chat", "").strip()
    msgs, err = [], request.args.get("err")
    if chat:
        try:
            msgs = run(_messages(chat, 25))
        except Exception as e:  # noqa: BLE001
            err = str(e)
    return page(view="browser", chat=chat, msgs=msgs, err=err)


@app.route("/chats")
def chats():
    if not authed():
        return redirect("/")
    try:
        lst = run(_dialogs())
    except Exception as e:  # noqa: BLE001
        return page(view="chats", chats=[], err=str(e))
    return page(view="chats", chats=lst)


# ----- login -----

@app.route("/login", methods=["POST"])
def login():
    try:
        state["phone"] = request.form["phone"].strip()
        run(client.send_code_request(state["phone"]))
        state["stage"] = "code"
    except Exception as e:  # noqa: BLE001
        return err_redirect("/", e)
    return redirect("/")


@app.route("/code", methods=["POST"])
def code():
    try:
        run(client.sign_in(state["phone"], request.form["code"].strip()))
        state["stage"] = "main"
    except SessionPasswordNeededError:
        state["stage"] = "password"
    except Exception as e:  # noqa: BLE001
        return err_redirect("/", e)
    return redirect("/")


@app.route("/password", methods=["POST"])
def password():
    try:
        run(client.sign_in(password=request.form["password"]))
        state["stage"] = "main"
    except Exception as e:  # noqa: BLE001
        return err_redirect("/", e)
    return redirect("/")


# ----- bot interaction -----

@app.route("/send", methods=["POST"])
def send():
    chat = request.form["chat"]
    try:
        run(_send(chat, request.form["text"]))
    except Exception as e:  # noqa: BLE001
        return err_redirect(f"/?chat={quote(chat)}", e)
    return redirect(f"/?chat={quote(chat)}")


@app.route("/click", methods=["POST"])
def click():
    chat = request.form["chat"]
    try:
        run(_click(chat, int(request.form["mid"]), int(request.form["i"]), int(request.form["j"])))
    except Exception as e:  # noqa: BLE001
        return err_redirect(f"/?chat={quote(chat)}", e)
    return redirect(f"/?chat={quote(chat)}")


@app.route("/dl", methods=["POST"])
def dl():
    chat, mid = request.form["chat"], int(request.form["mid"])
    key = f"{chat}:{mid}"
    if jobs.get(key, {}).get("status") != "running":
        jobs[key] = {"name": request.form["name"], "done": 0, "total": 0, "status": "running"}
        asyncio.run_coroutine_threadsafe(_download(chat, mid, key), loop)
    return redirect(request.referrer or f"/?chat={quote(chat)}")


@app.route("/jobs")
def jobs_json():
    return jsonify(jobs)


# ----- library -----

def default_chat():
    r = dbx("SELECT chat FROM meta ORDER BY synced_at DESC LIMIT 1")
    return r[0]["chat"] if r else ""


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
    sql = ("SELECT key, MIN(title) AS title, MAX(date) AS latest, COUNT(*) AS n, MIN(mid) AS thumb_mid, "
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
    chat = request.args.get("chat", "").strip() or default_chat()
    if not chat:
        return redirect("/chats")
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
    chat = request.args.get("chat", "")
    st = sync_state.get(chat, {})
    r = dbx("SELECT synced_at FROM meta WHERE chat=?", (chat,))
    metadata = tmdb_state.get(chat, {})
    return jsonify(running=bool(st.get("running")), scanned=st.get("scanned", 0), found=st.get("found", 0),
                   error=st.get("error"), last=r[0]["synced_at"] if r else None,
                   metadata_running=bool(metadata.get("running")), metadata_done=metadata.get("done", 0),
                   metadata_enabled=bool(TMDB_TOKEN or TMDB_API_KEY), metadata_error=metadata.get("error"))


@app.route("/lib/sync", methods=["POST"])
def lib_sync():
    chat = request.form["chat"]
    start_sync(chat, full=request.form.get("full") == "1")
    return redirect(f"/lib?chat={quote(chat)}")


@app.route("/lib/show")
def lib_show():
    if not authed():
        return redirect("/")
    chat, key = request.args.get("chat", ""), request.args.get("key", "")
    rows = dbx("SELECT * FROM items WHERE chat=? AND key=? ORDER BY season, episode, date", (chat, key))
    if not rows:
        return redirect(f"/lib?chat={quote(chat)}")
    seasons, order = {}, []
    for r in rows:
        sk = r["season"]
        if sk not in seasons:
            seasons[sk] = []
            order.append(sk)
        tag = f'S{r["season"]:02d}E{r["episode"]:02d}' if r["season"] is not None and r["episode"] is not None else ""
        seasons[sk].append({"mid": r["mid"], "fname": r["fname"], "size": r["size"], "tag": tag,
                            "surl": f"/stream/{r['mid']}/{quote(r['fname'])}?chat={quote(chat)}"})
    secs = [{"label": f"Season {sk}" if sk is not None else "Files", "files": seasons[sk]} for sk in order]
    tmdb = metadata_for(chat, [key]).get(key)
    if tmdb:
        tmdb["poster_url"] = "https://image.tmdb.org/t/p/w500" + tmdb["poster_path"] if tmdb["poster_path"] else None
        tmdb["backdrop_url"] = "https://image.tmdb.org/t/p/w1280" + tmdb["backdrop_path"] if tmdb["backdrop_path"] else None
    return page(view="show", chat=chat, key=key, name=rows[0]["title"], seasons=secs, tmdb=tmdb)


@app.route("/lib/rename", methods=["POST"])
def lib_rename():
    chat, key = request.form["chat"], request.form["key"]
    name = request.form["name"].strip()
    newkey = re.sub(r"[^a-z0-9]+", "", name.lower())
    if name and newkey:
        with dblock:
            db.execute("UPDATE items SET key=?, title=? WHERE chat=? AND key=?", (newkey, name, chat, key))
            db.execute("UPDATE items SET title=? WHERE chat=? AND key=?", (name, chat, newkey))
            db.execute("INSERT OR REPLACE INTO alias VALUES (?,?,?,?)", (chat, key, newkey, name))
            db.commit()
        key = newkey
    return redirect(f"/lib/show?chat={quote(chat)}&key={key}")


@app.route("/thumb")
def thumb():
    chat, mid = request.args.get("chat", ""), int(request.args.get("mid", "0"))
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", chat)
    path = os.path.join(THUMB_DIR, f"{safe}_{mid}.jpg")
    if os.path.exists(path + ".none"):
        abort(404)
    if not os.path.exists(path):
        try:
            data = run(_thumb(chat, mid))
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            open(path + ".none", "w").close()
            abort(404)
        with open(path, "wb") as fh:
            fh.write(data)
    return send_file(path, mimetype="image/jpeg", max_age=86400 * 30)


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
    chat = request.args.get("chat", "")
    if not chat or not authed():
        abort(403)
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


def main():
    if not API_ID or not API_HASH:
        raise SystemExit("API keys not found. Set TG_API_ID and TG_API_HASH in .env.")
    run(_init())
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
