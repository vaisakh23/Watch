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
        db.executemany(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        db.commit()


