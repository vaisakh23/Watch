"""YouTube Music playlist import and Telegram-backed music storage."""

import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse

from telethon import functions, utils
from telethon.tl import types
from yt_dlp import YoutubeDL

from . import config
from .config import run, _peer
from .database import db, dbx, dblock
from .settings import settings

logger = logging.getLogger(__name__)
MAX_TRACKS = 200
jobs = {}
jobs_lock = threading.RLock()


def _best_thumbnail(info):
    thumbnails = info.get("thumbnails") or []
    sized = [item for item in thumbnails if item.get("url")]
    if sized:
        best = max(sized, key=lambda item: (item.get("width") or 0) * (item.get("height") or 0))
        if best.get("width") and best.get("height"):
            return best["url"]
    return info.get("thumbnail") or (sized[-1]["url"] if sized else None)


def playlist_id_from_url(url):
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in (
        "music.youtube.com", "youtube.com", "www.youtube.com", "m.youtube.com"
    ):
        raise ValueError("Paste a YouTube Music playlist link.")
    playlist_id = urllib.parse.parse_qs(parsed.query).get("list", [None])[0]
    if parsed.path.rstrip("/") != "/playlist" or not playlist_id or not re.fullmatch(r"[A-Za-z0-9_-]+", playlist_id):
        raise ValueError("That link is not a YouTube Music playlist.")
    return playlist_id


def _youtube_playlist(url):
    opts = {"extract_flat": "in_playlist", "skip_download": True, "quiet": True,
            "no_warnings": True, "playlistend": MAX_TRACKS}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    tracks = []
    for entry in entries[:MAX_TRACKS]:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        artists = entry.get("artists") or []
        artist_names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in artists]
        artist = entry.get("artist") or ", ".join(name for name in artist_names if name) or entry.get("uploader") or ""
        source_url = entry.get("webpage_url") or entry.get("url")
        if not source_url or not source_url.startswith(("https://", "http://")):
            source_url = f"https://music.youtube.com/watch?v={urllib.parse.quote(video_id)}"
        tracks.append({
            "id": video_id, "title": entry.get("title") or "Unknown title",
            "artists": artist, "album": entry.get("album") or "",
            "duration_ms": int(float(entry.get("duration") or 0) * 1000),
            "url": source_url, "thumbnail_url": _best_thumbnail(entry),
        })
    total = int(info.get("playlist_count") or len(entries))
    playlist_thumbnail = _best_thumbnail(info)
    return info.get("title") or "YouTube Music playlist", tracks, total, playlist_thumbnail


async def _music_topic(chat):
    peer = await _peer(chat)
    entity = await config.client.get_entity(peer)
    offset_topic = 0
    while True:
        result = await config.client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=0, offset_id=0, offset_topic=offset_topic, limit=100
        ))
        for topic in result.topics:
            if (getattr(topic, "title", "") or "").strip().casefold() == "music":
                dbx("INSERT OR REPLACE INTO topics(chat,topic_id,title) VALUES(?,?,?)",
                    (chat, topic.id, "Music"))
                dbx("DELETE FROM items WHERE chat=? AND topic=?", (chat, topic.id))
                return entity, topic.id
        next_offset = getattr(result, "next_offset_topic", 0) or 0
        if not result.topics or not next_offset or next_offset == offset_topic:
            break
        offset_topic = next_offset
    created = await config.client(functions.channels.CreateForumTopicRequest(
        channel=entity, title="Music", random_id=utils.generate_random_long()
    ))
    for update in getattr(created, "updates", []):
        message = getattr(update, "message", None)
        action = getattr(message, "action", None)
        if message and (getattr(action, "title", "") or "").casefold() == "music":
            dbx("INSERT OR REPLACE INTO topics(chat,topic_id,title) VALUES(?,?,?)",
                (chat, message.id, "Music"))
            dbx("DELETE FROM items WHERE chat=? AND topic=?", (chat, message.id))
            return entity, message.id
    raise RuntimeError("Created the Music topic but could not resolve its topic ID; retry the import.")


def _download_track(track, directory):
    opts = {
        # Select only the best audio-only stream and keep its original codec.
        "format": "bestaudio", "format_sort": ["abr", "asr", "acodec"],
        "noplaylist": True, "quiet": True,
        "no_warnings": True, "outtmpl": os.path.join(directory, "track.%(ext)s"),
        "max_filesize": 100 * 1024 * 1024, "playlist_items": "1",
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([track["url"]])
    downloaded = [os.path.join(directory, name) for name in os.listdir(directory)
                  if not name.endswith((".part", ".ytdl"))]
    if not downloaded:
        raise RuntimeError("No audio match was downloaded.")
    return downloaded[0]


def _set_track(chat, track, **fields):
    allowed = {"message_id", "fname", "size", "status", "error", "title", "artists",
               "album", "duration_ms", "thumbnail_url"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    with dblock:
        existing = db.execute("SELECT 1 FROM music_tracks WHERE chat=? AND track_id=?", (chat, track["id"])).fetchone()
        if existing:
            updates = ",".join(f"{key}=?" for key in fields)
            db.execute(
                f"UPDATE music_tracks SET {updates} WHERE chat=? AND track_id=?" if updates else
                "UPDATE music_tracks SET status='pending',error=NULL WHERE chat=? AND track_id=?",
                (*fields.values(), chat, track["id"]) if updates else (chat, track["id"]),
            )
        else:
            db.execute(
                "INSERT INTO music_tracks(chat,track_id,title,artists,album,duration_ms,status,error,thumbnail_url) VALUES(?,?,?,?,?,?,?,?,?)",
                (chat, track["id"], track["title"], track["artists"], track["album"], track["duration_ms"],
                 fields.get("status", "pending"), fields.get("error"), track.get("thumbnail_url")),
            )
        db.commit()


def _job_update(chat, **values):
    with jobs_lock:
        jobs.setdefault(chat, {}).update(values)


def _run_import(chat, url, playlist_id):
    try:
        title, tracks, total, thumbnail_url = _youtube_playlist(url)
        if not tracks:
            raise RuntimeError("This playlist has no importable tracks.")
        _job_update(chat, playlist=title, total=len(tracks), source_total=total,
                    capped=total > len(tracks), done=0, failed=0, phase="preparing Music topic")
        entity, topic_id = run(_music_topic(chat))
        with dblock:
            db.execute("INSERT OR REPLACE INTO music_playlists(chat,playlist_id,title,source_url,imported_at,thumbnail_url) VALUES(?,?,?,?,?,?)",
                       (chat, playlist_id, title, url, int(time.time()), thumbnail_url))
            db.execute("DELETE FROM music_playlist_tracks WHERE chat=? AND playlist_id=?", (chat, playlist_id))
            db.executemany("INSERT INTO music_playlist_tracks VALUES(?,?,?,?)",
                           [(chat, playlist_id, track["id"], pos) for pos, track in enumerate(tracks)])
            db.commit()
        for index, track in enumerate(tracks, 1):
            _job_update(chat, phase=f"processing {index} of {len(tracks)}", current=track["title"])
            _set_track(chat, track, title=track["title"], artists=track["artists"],
                       album=track["album"], duration_ms=track["duration_ms"],
                       thumbnail_url=track.get("thumbnail_url"))
            existing = dbx("SELECT message_id,status,fname FROM music_tracks WHERE chat=? AND track_id=?", (chat, track["id"]))
            old_message_id = existing[0]["message_id"] if existing else None
            if (existing and old_message_id and existing[0]["status"] == "ready"
                    and existing[0]["fname"] != "audio.mp3"):
                _set_track(chat, track, status="ready", error=None)
                _job_update(chat, done=index)
                continue
            _set_track(chat, track, status="upgrading" if old_message_id else "pending", error=None)
            try:
                with tempfile.TemporaryDirectory(prefix="tg-music-") as directory:
                    path = _download_track(track, directory)
                    # Keep descriptive metadata in SQLite; the Telegram message only carries audio.
                    filename = "audio" + os.path.splitext(path)[1]
                    uploaded = run(config.client.send_file(
                        entity, path, reply_to=topic_id, force_document=False,
                        attributes=[types.DocumentAttributeFilename(filename)],
                    ))
                    message = uploaded[0] if isinstance(uploaded, list) else uploaded
                    _set_track(chat, track, message_id=message.id, fname=filename,
                               size=os.path.getsize(path), status="ready", error=None)
                    if old_message_id:
                        try:
                            run(config.client.delete_messages(entity, old_message_id))
                        except Exception:  # noqa: BLE001 - keep the new copy if cleanup fails
                            logger.exception("Could not remove replaced music message %s", old_message_id)
            except Exception as exc:  # keep the playlist moving after a per-track error
                logger.exception("Music track import failed (playlist=%s, track=%s)", playlist_id, track["id"])
                _set_track(chat, track, status="ready" if old_message_id else "failed", error=str(exc)[:500])
                _job_update(chat, failed=jobs.get(chat, {}).get("failed", 0) + 1)
            _job_update(chat, done=index)
        _job_update(chat, running=False, phase="done", current="")
    except Exception as exc:
        logger.exception("YouTube Music playlist import failed (playlist=%s)", playlist_id)
        _job_update(chat, running=False, phase="error", error=str(exc))


def start_import(chat, url):
    playlist_id = playlist_id_from_url(url)
    with jobs_lock:
        if jobs.get(chat, {}).get("running"):
            raise RuntimeError("A music import is already running.")
        jobs[chat] = {"running": True, "phase": "starting", "error": None, "playlist": "",
                      "total": 0, "done": 0, "failed": 0, "current": ""}
    threading.Thread(target=_run_import, args=(chat, url, playlist_id), daemon=True).start()


def playlist_rows(chat):
    playlists = [dict(row) for row in dbx("SELECT * FROM music_playlists WHERE chat=? ORDER BY imported_at DESC", (chat,))]
    for playlist in playlists:
        playlist["tracks"] = [dict(row) for row in dbx(
            "SELECT t.*,pt.position FROM music_playlist_tracks pt JOIN music_tracks t "
            "ON t.chat=pt.chat AND t.track_id=pt.track_id "
            "WHERE pt.chat=? AND pt.playlist_id=? ORDER BY pt.position",
            (chat, playlist["playlist_id"]),
        )]
    return playlists
