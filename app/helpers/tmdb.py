import json
import re
import threading
import time
from difflib import SequenceMatcher
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..database import db, dbx, dblock
from ..settings import TMDB_API_KEY, TMDB_LANGUAGE, TMDB_TOKEN

tmdb_state = {}  # chat -> metadata enrichment status
tmdb_lock = threading.Lock()


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


def tmdb_lookup(title, year=None, media_type="movie"):
    params = {"query": title, "include_adult": "false"}
    if year:
        params["first_air_date_year" if media_type == "tv" else "year"] = year
    results = tmdb_request(f"search/{media_type}", params).get("results", [])
    target = re.sub(r"[^a-z0-9]+", "", title.lower())
    def score(item):
        name = (item.get("name") or item.get("original_name") if media_type == "tv"
                else item.get("title") or item.get("original_title")) or ""
        candidate = re.sub(r"[^a-z0-9]+", "", name.lower())
        ratio = SequenceMatcher(None, target, candidate).ratio()
        date_field = "first_air_date" if media_type == "tv" else "release_date"
        released = (item.get(date_field) or "")[:4]
        if year and released and released != str(year):
            ratio -= .25
        return ratio
    match = max(results, key=score, default=None)
    if not match or score(match) < .58:
        return None
    return match


def enrich_tmdb(chat):
    if not (TMDB_TOKEN or TMDB_API_KEY):
        return
    with tmdb_lock:
        if tmdb_state.get(chat, {}).get("running") or tmdb_state.get(chat, {}).get("error"):
            return
        tmdb_state[chat] = {"running": True, "done": 0, "error": None}

    def worker():
        try:
            pending = dbx("""SELECT i.key, MIN(i.title) AS title, MAX(i.year) AS year,
                    MAX(i.episode IS NOT NULL) AS is_tv
                FROM items i LEFT JOIN tmdb_meta m ON m.chat=i.chat AND m.key=i.key
                WHERE i.chat=? AND (m.key IS NULL OR m.fetched_at < ?)
                GROUP BY i.key ORDER BY MAX(i.date) DESC LIMIT 60""", (chat, int(time.time()) - 30 * 86400))
            for item in pending:
                try:
                    media_type = "tv" if item["is_tv"] else "movie"
                    result = tmdb_lookup(item["title"], item["year"], media_type)
                    with dblock:
                        db.execute("""INSERT OR REPLACE INTO tmdb_meta
                            (chat,key,tmdb_id,media_type,poster_path,backdrop_path,overview,vote_average,release_date,fetched_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""", (chat, item["key"], result.get("id") if result else None,
                            media_type, result.get("poster_path") if result else None,
                            result.get("backdrop_path") if result else None, result.get("overview", "") if result else "",
                            result.get("vote_average") if result else None,
                            (result.get("first_air_date" if media_type == "tv" else "release_date", "") if result else ""),
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
