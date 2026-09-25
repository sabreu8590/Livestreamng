"""SQLite library for the compilation dashboard.

One file, no ORM. WAL mode so the background build worker can write progress
while the web request thread reads it.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

_local = threading.local()
_DB_PATH = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    handle        TEXT,
    title         TEXT,
    kind          TEXT NOT NULL DEFAULT 'youtube',
    added_at      REAL NOT NULL,
    last_synced   REAL,
    sync_state    TEXT NOT NULL DEFAULT 'idle',
    sync_message  TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    duration      REAL NOT NULL DEFAULT 0,
    view_count    INTEGER,
    upload_date   TEXT,
    is_short      INTEGER NOT NULL DEFAULT 0,
    url           TEXT,
    thumb         TEXT,
    local_path    TEXT,
    width         INTEGER,
    height        INTEGER,
    used_count    INTEGER NOT NULL DEFAULT 0,
    last_used_at  REAL,
    added_at      REAL NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_upload  ON videos(upload_date);
CREATE INDEX IF NOT EXISTS idx_videos_used    ON videos(used_count);

CREATE TABLE IF NOT EXISTS builds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    stage         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    total_clips   INTEGER NOT NULL DEFAULT 0,
    done_clips    INTEGER NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT '',
    error         TEXT,
    config_json   TEXT NOT NULL DEFAULT '{}',
    output_path   TEXT,
    output_bytes  INTEGER,
    duration_s    REAL,
    cancel        INTEGER NOT NULL DEFAULT 0,
    recipe_id     INTEGER,
    channel_id    TEXT
);

CREATE TABLE IF NOT EXISTS build_items (
    build_id      INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    video_id      TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    note          TEXT,
    trim_start    REAL NOT NULL DEFAULT 0,
    trim_end      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (build_id, position)
);

CREATE TABLE IF NOT EXISTS recipes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    channels_json TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    rules_json    TEXT NOT NULL DEFAULT '{}',
    label_json    TEXT NOT NULL DEFAULT '{}',
    video_json    TEXT NOT NULL DEFAULT '{}',
    output_json   TEXT NOT NULL DEFAULT '{}',
    schedule      TEXT NOT NULL DEFAULT 'off',
    schedule_day  INTEGER NOT NULL DEFAULT 1,
    schedule_hour INTEGER NOT NULL DEFAULT 4,
    next_run      REAL,
    last_run      REAL,
    last_build_id INTEGER,
    last_status   TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipes_next ON recipes(next_run);

CREATE TABLE IF NOT EXISTS settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add a column to a table that already exists, so they are applied by hand.
MIGRATIONS = [
    ("builds", "recipe_id", "INTEGER"),
    ("recipes", "channels_json", "TEXT"),
    ("recipes", "video_json", "TEXT"),
    ("builds", "channel_id", "TEXT"),
    ("build_items", "trim_start", "REAL NOT NULL DEFAULT 0"),
    ("build_items", "trim_end", "REAL NOT NULL DEFAULT 0"),
]


def init(db_path):
    global _DB_PATH
    _DB_PATH = str(db_path)
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn):
    for table, column, decl in MIGRATIONS:
        try:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if existing and column not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                conn.commit()
            except sqlite3.Error:
                pass


def connect():
    if getattr(_local, "conn", None) is None:
        conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def query(sql, params=()):
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def one(sql, params=()):
    row = connect().execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql, params=()):
    conn = connect()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def executemany(sql, rows):
    conn = connect()
    cur = conn.executemany(sql, rows)
    conn.commit()
    return cur


# ---------------------------------------------------------------- settings

def get_setting(key, default=None):
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_setting(key, value):
    execute("INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)))


# ---------------------------------------------------------------- videos

def upsert_videos(channel_id, entries):
    """Insert new videos, refresh volatile fields on existing ones.

    used_count and local_path are deliberately preserved: a re-sync must never
    forget that a clip has already been in three compilations.
    """
    now = time.time()
    rows = [(
        e["id"], channel_id, e.get("title") or "", float(e.get("duration") or 0),
        e.get("view_count"), e.get("upload_date"), 1 if e.get("is_short") else 0,
        e.get("url"), e.get("thumb"), e.get("width"), e.get("height"), now,
    ) for e in entries]
    executemany(
        """INSERT INTO videos
             (id, channel_id, title, duration, view_count, upload_date,
              is_short, url, thumb, width, height, added_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             title       = excluded.title,
             duration    = excluded.duration,
             view_count  = excluded.view_count,
             upload_date = excluded.upload_date,
             is_short    = excluded.is_short,
             url         = excluded.url,
             thumb       = COALESCE(videos.thumb, excluded.thumb)""",
        rows)
    return len(rows)


def mark_used(video_ids):
    now = time.time()
    executemany("UPDATE videos SET used_count = used_count + 1, last_used_at = ? "
                "WHERE id = ?", [(now, vid) for vid in video_ids])
