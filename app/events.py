"""Parse Roblox EventLogger output and align it with an OBS recording.

Two input formats are accepted:

1. Canonical JSON file::

       {"session_start_utc": "2026-09-24T18:30:00Z",
        "events": [{"t": 12.3, "kind": "death", "detail": "fell"}, ...]}

2. Raw logger lines copied from the Roblox Studio Output window (or received
   over HttpService), one per event, anywhere on the line::

       18:30:12.345  [AutoPromoEvent] {"v":1,"t":12.345,"kind":"death","detail":"fell"}  -  Server - EventLogger:88

   The first line of a session is kind ``session_start`` with ``utc``.

``t`` is seconds since the logger session started. A ``recording_start``
event (``/rec`` chat command) marks when OBS recording began, so video time
= t - t(recording_start). Sidecar files next to a video are discovered by
``find_sidecar``: ``<video>.events.json`` / ``.events.txt`` / ``.events.log``.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "[AutoPromoEvent]"
KNOWN_KINDS = {"session_start", "recording_start", "recording_stop", "death", "victory",
               "checkpoint", "rare_item", "boss_defeat", "high_score", "unexpected", "custom"}
SYNC_KINDS = {"session_start", "recording_start", "recording_stop"}
MAX_DETAIL = 200
_LINE = re.compile(re.escape(PREFIX) + r"\s*(\{.*)")
_KIND = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class EventParseError(ValueError):
    pass


def normalize_kind(kind) -> str:
    k = re.sub(r"[^a-z0-9_]", "_", str(kind or "").strip().lower()).strip("_")
    k = re.sub(r"_+", "_", k)
    return k if _KIND.match(k or "") else ""


def validate_event(e) -> dict | None:
    """Return a clean {t, kind, detail} or None if invalid."""
    if not isinstance(e, dict):
        return None
    try:
        t = float(e.get("t"))
    except (TypeError, ValueError):
        return None
    if t != t or t < 0 or t > 7 * 24 * 3600:  # NaN / negative / absurd
        return None
    kind = normalize_kind(e.get("kind"))
    if not kind:
        return None
    detail = e.get("detail")
    detail = "" if detail is None else (detail if isinstance(detail, str) else json.dumps(detail))
    return {"t": round(t, 3), "kind": kind, "detail": detail[:MAX_DETAIL]}


def parse_utc(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_json_doc(doc) -> dict:
    if isinstance(doc, list):
        doc = {"events": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
        raise EventParseError("expected an object with an 'events' list")
    events = [v for v in (validate_event(e) for e in doc["events"]) if v]
    start = doc.get("session_start_utc")
    if not start:  # HTTP batches carry it on the session_start event
        start = next((e.get("utc") for e in doc["events"]
                      if isinstance(e, dict) and e.get("kind") == "session_start"), None)
    return _finish(start, events, len(doc["events"]) - len(events))


def parse_lines(text: str) -> dict:
    """Parse raw logger lines (Output window copy/paste or HTTP body)."""
    events, bad, start = [], 0, None
    for line in text.splitlines():
        m = _LINE.search(line)
        if not m:
            continue
        raw = m.group(1)
        try:
            obj = json.loads(raw[: raw.rfind("}") + 1])
        except ValueError:
            bad += 1
            continue
        if obj.get("kind") == "session_start":
            if start is not None and events:
                # A new Play session began: keep only the latest session.
                events, bad = [], 0
            start = obj.get("utc") or obj.get("session_start_utc")
        ev = validate_event(obj)
        if ev:
            events.append(ev)
        else:
            bad += 1
    return _finish(start, events, bad)


def _finish(start, events, skipped) -> dict:
    events.sort(key=lambda e: e["t"])
    return {"session_start_utc": start, "events": events, "skipped": skipped}


def parse(source) -> dict:
    """Parse a path, JSON string, raw log text, dict or list."""
    if isinstance(source, (dict, list)):
        return parse_json_doc(source)
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source
                                     and len(source) < 4096 and Path(source).exists()):
        source = Path(source).read_text(encoding="utf-8", errors="replace")
    text = str(source).strip().lstrip("﻿")
    if text.startswith(("{", "[")):
        try:
            return parse_json_doc(json.loads(text))
        except ValueError:
            pass
    if PREFIX in text:
        return parse_lines(text)
    raise EventParseError("no events found (expected JSON or [AutoPromoEvent] lines)")


def to_canonical(parsed: dict) -> dict:
    return {"session_start_utc": parsed.get("session_start_utc"), "events": parsed["events"]}


# ------------------------------------------------------------------ alignment
def sync_offset(parsed: dict, recording_start_utc: datetime | None = None) -> float:
    """Logger-time (seconds) at which the video starts.

    Priority: first ``recording_start`` marker; else recording wall-clock start
    minus session_start_utc; else 0 (assume recording started with the session).
    """
    for e in parsed["events"]:
        if e["kind"] == "recording_start":
            return e["t"]
    start = parse_utc(parsed.get("session_start_utc"))
    if start and recording_start_utc:
        rs = recording_start_utc if recording_start_utc.tzinfo else \
            recording_start_utc.replace(tzinfo=timezone.utc)
        return (rs - start).total_seconds()
    return 0.0


def align(events: list[dict], offset: float, duration: float | None = None,
          keep_sync: bool = False) -> list[dict]:
    """Shift events to video time (t - offset) and drop ones outside the video."""
    out = []
    for e in events:
        if e["kind"] in SYNC_KINDS and not keep_sync:
            continue
        t = round(e["t"] - offset, 3)
        if t < 0 or (duration is not None and t > duration):
            continue
        out.append({**e, "t": t})
    return out


_OBS_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})-(\d{2})-(\d{2})")


def obs_filename_time(path, tz=None) -> datetime | None:
    """OBS default names look like '2026-09-24 18-30-00.mkv' (local time)."""
    m = _OBS_NAME.search(Path(path).name)
    if not m:
        return None
    dt = datetime(*map(int, m.groups()))
    return dt.replace(tzinfo=tz).astimezone(timezone.utc) if tz else dt.astimezone(timezone.utc)


def find_sidecar(video_path) -> Path | None:
    p = Path(video_path)
    for suffix in (".events.json", ".events.txt", ".events.log"):
        for cand in (p.with_suffix(suffix), p.parent / (p.name + suffix)):
            if cand.exists() and cand != p:
                return cand
    return None


def store_events(conn: sqlite3.Connection, recording_id: int, events: list[dict]) -> int:
    """Replace a recording's events with already-aligned events. Returns count."""
    conn.execute("DELETE FROM events WHERE recording_id=?", (recording_id,))
    conn.executemany("INSERT INTO events (recording_id, t, kind, detail) VALUES (?,?,?,?)",
                     [(recording_id, e["t"], e["kind"], e.get("detail") or "") for e in events])
    conn.commit()
    return len(events)


def load_for_recording(conn: sqlite3.Connection, recording_id: int, video_path,
                       duration: float | None = None, tz=None) -> int:
    """Find, parse, align and store the sidecar events for a recording (0 if none)."""
    side = find_sidecar(video_path)
    if side is None:
        return 0
    parsed = parse(side)
    offset = sync_offset(parsed, obs_filename_time(video_path, tz))
    return store_events(conn, recording_id, align(parsed["events"], offset, duration))
