"""SQLite connection and database helpers."""

import sqlite3
import threading

from .settings import settings

# ----------------------------- database -----------------------------

db = sqlite3.connect(settings.db_path, check_same_thread=False)
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
CREATE TABLE IF NOT EXISTS music_playlists(
  chat TEXT, playlist_id TEXT, title TEXT, source_url TEXT, imported_at INTEGER, thumbnail_url TEXT,
  PRIMARY KEY(chat, playlist_id));
CREATE TABLE IF NOT EXISTS music_tracks(
  chat TEXT, track_id TEXT, title TEXT, artists TEXT, album TEXT, duration_ms INTEGER,
  message_id INTEGER, fname TEXT, size INTEGER, status TEXT DEFAULT 'pending', error TEXT, thumbnail_url TEXT,
  PRIMARY KEY(chat, track_id));
CREATE TABLE IF NOT EXISTS music_playlist_tracks(
  chat TEXT, playlist_id TEXT, track_id TEXT, position INTEGER,
  PRIMARY KEY(chat, playlist_id, position));
CREATE INDEX IF NOT EXISTS music_playlist_order ON music_playlist_tracks(chat, playlist_id, position);
""")
db.commit()

# Migrate the short-lived Spotify importer schema, if this database has it.
for table, old, new in (("music_playlists", "spotify_id", "playlist_id"),
                        ("music_tracks", "spotify_id", "track_id")):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if old in columns and new not in columns:
        db.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
for table in ("music_playlists", "music_tracks"):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if "thumbnail_url" not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN thumbnail_url TEXT")
# Earlier imports were transcoded to MP3; mark them for replacement on next playlist import.
db.execute("UPDATE music_tracks SET status='upgrade' WHERE fname='audio.mp3' AND status='ready'")
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
        db.executemany(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        db.commit()


