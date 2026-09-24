"""Posting schedule.

plan_posts(): turn QC-passed renders (renders.status='queued', not yet in
posts) into `posts` rows on free slots from [schedule] slots, capped by
max_posts_per_day, in the configured timezone.

Game allocation: games with better *real* views-per-post (metrics table, api or
manual - never estimated) get a larger share of slots, but every game keeps an
exploration share. With no metrics at all it is plain round-robin. Recent
history (last 7 days of posts) counts toward each game's share so rotation
continues across passes. The same game is not placed in back-to-back slots when
another game has something ready and an equal claim.

publish_due(): publish posts whose slot has arrived, honouring the kill switch
and the pause flags. posts.render_id is UNIQUE, so a render can never be posted
twice; only 'scheduled' posts are ever handed to the publisher.
"""
from __future__ import annotations

import importlib
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .log import get
from .pipeline import kill_reason

log = get("scheduler")

HISTORY_DAYS = 7
HORIZON_DAYS = 2           # plan today + tomorrow
DEFAULT_EXPLORATION = 0.3  # min share of slots spread evenly across all games


def _tz(settings) -> ZoneInfo:
    return ZoneInfo(settings.section("schedule").get("timezone", "UTC"))


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return _utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def publishing_block_reason(settings, conn) -> str | None:
    reason = kill_reason(settings, conn)
    if reason:
        return reason
    if db.get_state(conn, "publishing_paused") == "1":
        why = db.get_state(conn, "publishing_paused_reason")
        return "publishing paused" + (f" ({why})" if why else "") + " - python -m app resume"
    if db.get_state(conn, "paused") == "1":
        return "paused - python -m app resume"
    return None


# --------------------------------------------------------------------------- slots
def _slot_times(settings) -> list[time]:
    out = []
    for s in settings.section("schedule").get("slots", []):
        h, m = str(s).split(":")[:2]
        out.append(time(int(h), int(m)))
    return sorted(set(out))


def free_slots(settings, conn, now: datetime, days: int = HORIZON_DAYS) -> list[datetime]:
    """Future slot datetimes (aware, UTC) with room under max_posts_per_day."""
    tz = _tz(settings)
    local_now = _utc(now).astimezone(tz)
    max_per_day = int(settings.section("schedule").get("max_posts_per_day", 2))
    taken: dict[date, int] = {}
    taken_exact: set[str] = set()
    for r in conn.execute("SELECT scheduled_for FROM posts WHERE scheduled_for IS NOT NULL "
                          "AND status != 'failed'"):
        dt = _parse(r["scheduled_for"])
        if dt:
            taken_exact.add(_iso(dt))
            d = dt.astimezone(tz).date()
            taken[d] = taken.get(d, 0) + 1
    out = []
    for i in range(days):
        d = local_now.date() + timedelta(days=i)
        room = max_per_day - taken.get(d, 0)
        for t in _slot_times(settings):
            if room <= 0:
                break
            slot = datetime.combine(d, t, tzinfo=tz)
            if slot <= local_now or _iso(slot) in taken_exact:
                continue
            out.append(_utc(slot))
            room -= 1
    return sorted(out)


# --------------------------------------------------------------------------- allocation
def game_views_per_post(conn) -> dict[int, float]:
    """Mean of the latest real view count per post, by game. Only games with data."""
    rows = conn.execute("""
        SELECT rec.game_id AS game_id, m.views AS views
        FROM metrics m
        JOIN (SELECT post_id, MAX(id) mid FROM metrics WHERE views IS NOT NULL GROUP BY post_id) last
             ON last.mid = m.id
        JOIN posts p ON p.id = m.post_id
        JOIN renders r ON r.id = p.render_id
        JOIN clips c ON c.id = r.clip_id
        JOIN recordings rec ON rec.id = c.recording_id
    """).fetchall()
    agg: dict[int, list[int]] = {}
    for r in rows:
        agg.setdefault(r["game_id"], []).append(r["views"])
    return {g: sum(v) / len(v) for g, v in agg.items()}


def game_weights(game_ids: list[int], vpp: dict[int, float], exploration: float) -> dict[int, float]:
    """Target share per game (sums to 1)."""
    if not game_ids:
        return {}
    n = len(game_ids)
    known = {g: vpp[g] for g in game_ids if g in vpp}
    total = sum(known.values())
    if not known or total <= 0:
        return {g: 1 / n for g in game_ids}
    # games without data are treated as average performers so they still get tried
    avg = total / len(known)
    perf = {g: known.get(g, avg) for g in game_ids}
    ptot = sum(perf.values())
    exploration = min(max(exploration, 0.0), 1.0)
    return {g: exploration / n + (1 - exploration) * perf[g] / ptot for g in game_ids}


def _ready_renders(conn) -> dict[int, list[int]]:
    rows = conn.execute("""
        SELECT r.id AS rid, rec.game_id AS gid
        FROM renders r
        JOIN clips c ON c.id = r.clip_id
        JOIN recordings rec ON rec.id = c.recording_id
        WHERE r.status = 'queued' AND r.id NOT IN (SELECT render_id FROM posts WHERE render_id IS NOT NULL)
        ORDER BY COALESCE(c.score, 0) DESC, r.id
    """).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["gid"] if r["gid"] is not None else -1, []).append(r["rid"])
    return out


def _recent_counts(conn, now: datetime) -> tuple[dict[int, int], int | None]:
    since = _iso(now - timedelta(days=HISTORY_DAYS))
    rows = conn.execute("""
        SELECT rec.game_id AS gid, p.scheduled_for AS sf
        FROM posts p JOIN renders r ON r.id = p.render_id
        JOIN clips c ON c.id = r.clip_id JOIN recordings rec ON rec.id = c.recording_id
        WHERE p.scheduled_for >= ? ORDER BY p.scheduled_for
    """, (since,)).fetchall()
    counts: dict[int, int] = {}
    last = None
    for r in rows:
        g = r["gid"] if r["gid"] is not None else -1
        counts[g] = counts.get(g, 0) + 1
        last = g
    return counts, last


def plan_posts(settings, conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    """Create 'scheduled' posts for free slots. Returns new post ids."""
    now = _utc(now or datetime.now(timezone.utc))
    if kill_reason(settings, conn):
        return []
    slots = free_slots(settings, conn, now)
    ready = _ready_renders(conn)
    if not slots or not ready:
        return []
    sched = settings.section("schedule")
    exploration = float(sched.get("exploration_share", DEFAULT_EXPLORATION))
    all_games = sorted(set(ready))
    weights = game_weights(all_games, game_views_per_post(conn), exploration)
    counts, last = _recent_counts(conn, now)
    mode = settings.section("publish").get("mode", "local")
    created = []
    for slot in slots:
        avail = [g for g in all_games if ready.get(g)]
        if not avail:
            break
        total = sum(counts.get(g, 0) for g in all_games) + 1

        def deficit(g):  # how far below its target share this game is
            return weights[g] * total - counts.get(g, 0)

        best = max(deficit(g) for g in avail)
        # near-ties: prefer a game different from the previous slot, then fewer posts
        cands = [g for g in avail if deficit(g) >= best - 0.5] or avail
        cands.sort(key=lambda g: (g == last, -deficit(g), counts.get(g, 0), g))
        g = cands[0]
        rid = ready[g].pop(0)
        ts = db.now()
        cur = conn.execute(
            "INSERT OR IGNORE INTO posts(render_id, mode, scheduled_for, status, attempts, created_at, updated_at) "
            "VALUES(?,?,?,'scheduled',0,?,?)", (rid, mode, _iso(slot), ts, ts))
        if cur.rowcount:
            created.append(cur.lastrowid)
            counts[g] = counts.get(g, 0) + 1
            last = g
            log.info("scheduled render %s (game %s) for %s", rid, g, _iso(slot))
    conn.commit()
    return created


# --------------------------------------------------------------------------- publishing
def publish_due(settings, conn: sqlite3.Connection, now: datetime | None = None) -> list[tuple[int, str]]:
    """Publish every due 'scheduled' post. Returns [(post_id, status)]."""
    now = _utc(now or datetime.now(timezone.utc))
    reason = publishing_block_reason(settings, conn)
    if reason:
        log.info("not publishing: %s", reason)
        return []
    publisher = importlib.import_module("app.publisher")
    results = []
    due = [r for r in conn.execute("SELECT id, scheduled_for FROM posts WHERE status='scheduled' "
                                   "ORDER BY scheduled_for, id").fetchall()
           if (_parse(r["scheduled_for"]) or now) <= now]
    for r in due:
        if publishing_block_reason(settings, conn):  # publisher may pause us mid-loop
            break
        # re-check: another process may have picked it up
        cur = conn.execute("SELECT status FROM posts WHERE id=?", (r["id"],)).fetchone()
        if not cur or cur["status"] != "scheduled":
            continue
        try:
            status = publisher.publish(settings, conn, r["id"])
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            log.error("publish of post %s raised: %s", r["id"], e)
            conn.execute("UPDATE posts SET attempts=COALESCE(attempts,0)+1, error=?, updated_at=?, "
                         "status=CASE WHEN COALESCE(attempts,0)+1 >= 3 THEN 'failed' ELSE status END "
                         "WHERE id=?", (f"{type(e).__name__}: {e}"[:1000], db.now(), r["id"]))
            conn.commit()
            status = "error"
        results.append((r["id"], status))
    # follow up on uploads still being processed by TikTok
    for r in conn.execute("SELECT id FROM posts WHERE status='uploading'").fetchall():
        if publishing_block_reason(settings, conn):
            break
        try:
            publisher.refresh_status(settings, conn, r["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("refresh_status post %s: %s", r["id"], e)
    return results
