"""Inbox scanner.

Layout::

    inbox/<game-slug>/game.json            (optional but recommended)
    inbox/<game-slug>/<name>.mp4|.mov|.mkv
    inbox/<game-slug>/<name>.events.json   (optional, from the Roblox logger)

Each new, fully-written video is hashed (sha256, dedupe), probed, moved to
processing/<slug>/, thumbnailed, and recorded in the database together with
its logger events. Corrupt files are recorded as 'failed' and moved to
inbox/_rejected/<slug>/. Re-added copies of an already-known file are moved
to inbox/_duplicates/<slug>/ and never processed again.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import db, log
from .probe import FFMPEG, ProbeError, probe

VIDEO_EXTS = {".mp4", ".mov", ".mkv"}
EVENT_KINDS = {"death", "victory", "rare_item", "boss_defeat", "high_score", "unexpected", "custom"}
SPECIAL_DIRS = {"_rejected", "_duplicates"}
_log = log.get("ingest")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "game"


def _load_game_json(folder: Path) -> dict:
    gj = folder / "game.json"
    if not gj.exists():
        _log.warning("no game.json in %s; using folder name as game name", folder)
        return {"name": folder.name}
    try:
        data = json.loads(gj.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("game.json must be an object")
        return data
    except (ValueError, OSError) as e:
        _log.warning("bad game.json in %s (%s); using folder name", folder, e)
        return {"name": folder.name}


def upsert_game(conn: sqlite3.Connection, slug: str, meta: dict) -> int:
    def lst(key):
        v = meta.get(key) or []
        return db.dumps([str(x) for x in v] if isinstance(v, list) else [str(v)])

    vals = (slug, str(meta.get("name") or slug), meta.get("url"), meta.get("description"),
            meta.get("genre"), meta.get("audience"), meta.get("cta"),
            lst("avoid_words"), lst("hashtags"), db.now())
    conn.execute(
        """INSERT INTO games(slug,name,url,description,genre,audience,cta,avoid_words,hashtags,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(slug) DO UPDATE SET name=excluded.name, url=excluded.url,
             description=excluded.description, genre=excluded.genre, audience=excluded.audience,
             cta=excluded.cta, avoid_words=excluded.avoid_words, hashtags=excluded.hashtags""", vals)
    conn.commit()
    return conn.execute("SELECT id FROM games WHERE slug=?", (slug,)).fetchone()["id"]


def load_events(path: Path, duration: float | None = None) -> list[dict]:
    """Parse a logger .events.json; silently drops malformed entries."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as e:
        _log.warning("unreadable events file %s: %s", path, e)
        return []
    items = data.get("events", []) if isinstance(data, dict) else data
    out = []
    for ev in items if isinstance(items, list) else []:
        try:
            t = float(ev["t"])
        except (KeyError, TypeError, ValueError):
            continue
        kind = str(ev.get("kind", "custom"))
        if kind not in EVENT_KINDS:
            kind = "custom"
        if t < 0 or (duration is not None and t > duration):
            continue
        out.append({"t": t, "kind": kind, "detail": str(ev.get("detail") or "")[:500]})
    return out


def _unique_dest(folder: Path, name: str, tag: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    if dest.exists():
        dest = folder / f"{Path(name).stem}_{tag}{Path(name).suffix}"
    return dest


def make_thumbnail(video: Path, out: Path, duration: float) -> Path | None:
    t = max(0.0, min(duration * 0.3, duration - 0.1))
    cmd = [FFMPEG, "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
           "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0 or not out.exists():
        _log.warning("thumbnail failed for %s: %s", video.name, res.stderr.strip()[:200])
        return None
    return out


def _settled(files: list[Path], settle: float) -> list[Path]:
    """Keep only files whose size is unchanged across `settle` seconds."""
    sizes = {}
    for f in files:
        try:
            sizes[f] = f.stat().st_size
        except OSError:
            pass
    if not sizes:
        return []
    now = time.time()
    young = any(now - f.stat().st_mtime < settle for f in sizes if f.exists())
    if young and settle > 0:
        time.sleep(settle)
    ready = []
    for f, size in sizes.items():
        try:
            if f.stat().st_size == size and size > 0:
                ready.append(f)
            else:
                _log.info("skipping %s: still being written", f.name)
        except OSError:
            pass
    return ready


def scan_inbox(settings, conn: sqlite3.Connection, settle_seconds: float | None = None) -> list[int]:
    """Ingest every finished video in the inbox; returns new recording ids."""
    inbox = settings.path("inbox")
    processing = settings.path("processing")
    if settle_seconds is None:
        settle_seconds = float(settings.section("ingest").get("settle_seconds", 2.0))
    candidates = []
    for folder in sorted(p for p in inbox.iterdir() if p.is_dir() and p.name not in SPECIAL_DIRS
                         and not p.name.startswith(".")):
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS and not f.name.startswith("."):
                candidates.append(f)
    new_ids: list[int] = []
    game_cache: dict[str, int] = {}
    for f in _settled(candidates, settle_seconds):
        folder = f.parent
        slug = slugify(folder.name)
        try:
            digest = sha256_file(f)
        except OSError as e:
            _log.warning("cannot read %s: %s", f, e)
            continue
        events_file = folder / f"{f.stem}.events.json"
        existing = conn.execute("SELECT id FROM recordings WHERE sha256=?", (digest,)).fetchone()
        if existing:
            dest = _unique_dest(inbox / "_duplicates" / slug, f.name, digest[:8])
            shutil.move(str(f), dest)
            if events_file.exists():
                shutil.move(str(events_file), dest.with_name(dest.stem + ".events.json"))
            _log.info("duplicate of recording %s: %s -> %s", existing["id"], f.name, dest)
            continue
        if slug not in game_cache:
            game_cache[slug] = upsert_game(conn, slug, _load_game_json(folder))
        game_id = game_cache[slug]
        try:
            info = probe(f)
        except ProbeError as e:
            dest = _unique_dest(inbox / "_rejected" / slug, f.name, digest[:8])
            shutil.move(str(f), dest)
            if events_file.exists():
                shutil.move(str(events_file), dest.with_name(dest.stem + ".events.json"))
            conn.execute("""INSERT INTO recordings(game_id,path,sha256,status,error,created_at,updated_at)
                            VALUES(?,?,?,'failed',?,?,?)""",
                         (game_id, str(dest.resolve()), digest, str(e), db.now(), db.now()))
            conn.commit()
            _log.error("rejected corrupt video %s: %s", f.name, e)
            continue
        dest = _unique_dest(processing / slug, f.name, digest[:8])
        shutil.move(str(f), dest)
        events = []
        if events_file.exists():
            events = load_events(events_file, info["duration"])
            shutil.move(str(events_file), dest.with_name(dest.stem + ".events.json"))
        thumb = make_thumbnail(dest, dest.with_suffix(".jpg"), info["duration"])
        cur = conn.execute(
            """INSERT INTO recordings(game_id,path,sha256,duration,width,height,has_audio,thumbnail,
                                      status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,'new',?,?)""",
            (game_id, str(dest.resolve()), digest, info["duration"], info["width"], info["height"],
             int(info["has_audio"]), str(thumb.resolve()) if thumb else None, db.now(), db.now()))
        rid = cur.lastrowid
        conn.executemany("INSERT INTO events(recording_id,t,kind,detail) VALUES(?,?,?,?)",
                         [(rid, e["t"], e["kind"], e["detail"]) for e in events])
        conn.commit()
        _log.info("ingested recording %s (%s, %.1fs, %d events)", rid, dest.name,
                  info["duration"], len(events))
        new_ids.append(rid)
    return new_ids
