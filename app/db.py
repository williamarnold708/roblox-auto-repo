"""SQLite persistence. Every pipeline stage records its state here so the
system can resume after a restart and never processes a file twice."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    url TEXT, description TEXT, genre TEXT, audience TEXT, cta TEXT,
    avoid_words TEXT DEFAULT '[]', hashtags TEXT DEFAULT '[]', created_at TEXT
);
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY, game_id INTEGER REFERENCES games(id),
    path TEXT NOT NULL, sha256 TEXT UNIQUE NOT NULL,
    duration REAL, width INTEGER, height INTEGER, has_audio INTEGER,
    thumbnail TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new|processing|done|no_clips|failed|review
    error TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (          -- from the Roblox Lua logger
    id INTEGER PRIMARY KEY, recording_id INTEGER REFERENCES recordings(id),
    t REAL NOT NULL, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY, recording_id INTEGER REFERENCES recordings(id),
    start REAL NOT NULL, end REAL NOT NULL, score REAL, reasons TEXT,
    path TEXT, status TEXT DEFAULT 'candidate', created_at TEXT
);
CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY, clip_id INTEGER UNIQUE REFERENCES clips(id),
    hooks TEXT, on_screen_text TEXT, caption TEXT, hashtags TEXT,
    voiceover TEXT, cta TEXT, rationale TEXT, provider TEXT,
    chosen_hook INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS renders (
    id INTEGER PRIMARY KEY, clip_id INTEGER REFERENCES clips(id),
    content_id INTEGER REFERENCES content(id), path TEXT, width INTEGER,
    height INTEGER, duration REAL, phash TEXT, qc_passed INTEGER,
    qc_report TEXT,
    status TEXT DEFAULT 'rendered', -- rendered|qc_failed|queued|posted|rejected
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY, render_id INTEGER UNIQUE REFERENCES renders(id),
    mode TEXT NOT NULL,  -- local|inbox|direct
    scheduled_for TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled|uploading|awaiting_user|published|ready_manual|failed
    publish_id TEXT, video_id TEXT, share_url TEXT, error TEXT,
    attempts INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY, post_id INTEGER REFERENCES posts(id),
    captured_at TEXT, views INTEGER, likes INTEGER, comments INTEGER,
    shares INTEGER, avg_watch_s REAL, completion_rate REAL,
    profile_visits INTEGER, roblox_visits INTEGER,
    source TEXT NOT NULL  -- api|manual  (never estimated)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed
    attempts INTEGER DEFAULT 0, last_error TEXT, run_after TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO state(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
