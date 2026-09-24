"""Daily summary (markdown) and the 'Action needed' list.

Numbers about performance only ever come from the metrics table (source api or
manual). If there are none, the summary says "no data yet" - nothing is
estimated.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .log import get

log = get("summary")


def _tz(settings) -> ZoneInfo:
    return ZoneInfo(settings.section("schedule").get("timezone", "UTC"))


def _bounds(settings, day: date) -> tuple[str, str]:
    tz = _tz(settings)
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _one(conn, sql, args=()) -> int:
    r = conn.execute(sql, args).fetchone()
    return (r[0] or 0) if r else 0


def action_items(settings, conn: sqlite3.Connection) -> list[str]:
    """Things only the user can do. Empty list = nothing to do."""
    items: list[str] = []
    root = settings.root
    if (root / "KILL").exists() or db.get_state(conn, "kill_switch") == "1":
        items.append("Kill switch is ON - nothing is processed or published. "
                     "Clear with `python -m app unkill` (and delete the KILL file if present).")
    if db.get_state(conn, "publishing_paused") == "1":
        why = db.get_state(conn, "publishing_paused_reason") or "paused"
        hint = ("re-authorise with `python -m app auth`, then `python -m app resume`"
                if any(k in why.lower() for k in ("auth", "token", "expired", "401"))
                else "check logs, then `python -m app resume`")
        items.append(f"Publishing is paused ({why}) - {hint}.")
    review = conn.execute("SELECT r.id, r.path, r.error FROM recordings r WHERE r.status='review' "
                          "ORDER BY r.id").fetchall()
    for r in review:
        items.append(f"Recording #{r['id']} needs review ({Path(r['path']).name}): "
                     f"{r['error'] or 'no good clips found'} - record a livelier session or trim it.")
    failed_rec = conn.execute("SELECT id, path, error FROM recordings WHERE status='failed' "
                              "ORDER BY id").fetchall()
    for r in failed_rec:
        items.append(f"Recording #{r['id']} failed ({Path(r['path']).name}): {(r['error'] or '')[:160]}")
    ready = conn.execute("""
        SELECT p.id, r.path FROM posts p JOIN renders r ON r.id=p.render_id
        WHERE p.status='ready_manual' ORDER BY p.scheduled_for, p.id""").fetchall()
    if ready:
        lst = ", ".join(f"#{r['id']}" for r in ready[:10])
        items.append(f"{len(ready)} video(s) ready to post manually from `queue/ready/` (MP4 + caption .txt): {lst}"
                     + (" ..." if len(ready) > 10 else "")
                     + ". After posting each one, run `python -m app posted N --url <tiktok link>`, "
                     "later add views with `python -m app metrics add --post N --views ...`.")
    awaiting = conn.execute("SELECT id FROM posts WHERE status='awaiting_user' ORDER BY id").fetchall()
    if awaiting:
        items.append(f"{len(awaiting)} draft(s) waiting in your TikTok inbox - open TikTok, "
                     f"finish and post them (posts {', '.join('#' + str(r['id']) for r in awaiting[:10])}).")
    direct_waiting = [r["id"] for r in conn.execute(
        "SELECT id FROM posts WHERE status='scheduled' AND mode='direct' ORDER BY id").fetchall()
        if (db.get_state(conn, f"approved:{r['id']}") or "0") in ("0", "false", "")]
    if direct_waiting:
        items.append("Direct-post videos need approval before they go out: "
                     + ", ".join(f"`python -m app approve {i}`" for i in direct_waiting[:10]))
    failed_posts = conn.execute("SELECT id, error FROM posts WHERE status='failed' ORDER BY id").fetchall()
    for r in failed_posts[:10]:
        items.append(f"Post #{r['id']} failed: {(r['error'] or '')[:160]}")
    failed_jobs = _one(conn, "SELECT COUNT(*) FROM jobs WHERE status='failed'")
    if failed_jobs:
        items.append(f"{failed_jobs} processing job(s) gave up after retries - see `python -m app status` "
                     f"and logs/autopromo.log.")
    retrying = conn.execute("SELECT kind, ref_id, attempts, last_error FROM jobs "
                            "WHERE status='pending' AND attempts > 0 ORDER BY id LIMIT 5").fetchall()
    for j in retrying:
        items.append(f"{j['kind']} job for #{j['ref_id']} failed {j['attempts']}x and will retry: "
                     f"{(j['last_error'] or '')[:200]}")
    return items


def _best(conn) -> tuple[list[str], bool]:
    rows = conn.execute("""
        SELECT p.id pid, m.views, m.likes, m.source, g.name game, g.id gid,
               co.hooks, co.chosen_hook, co.caption
        FROM metrics m
        JOIN (SELECT post_id, MAX(id) mid FROM metrics WHERE views IS NOT NULL GROUP BY post_id) l ON l.mid=m.id
        JOIN posts p ON p.id=m.post_id
        JOIN renders r ON r.id=p.render_id
        JOIN clips c ON c.id=r.clip_id
        JOIN recordings rec ON rec.id=c.recording_id
        LEFT JOIN games g ON g.id=rec.game_id
        LEFT JOIN content co ON co.id=r.content_id
        ORDER BY m.views DESC
    """).fetchall()
    if not rows:
        return ["- Best game: no data yet (add views with `python -m app metrics add` or `metrics fetch`)",
                "- Best clip: no data yet"], False
    games: dict[str, list[int]] = {}
    for r in rows:
        games.setdefault(r["game"] or "unknown", []).append(r["views"])
    best_game = max(games.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
    top = rows[0]
    hook = ""
    try:
        import json
        hooks = json.loads(top["hooks"] or "[]")
        if hooks:
            hook = hooks[min(int(top["chosen_hook"] or 0), len(hooks) - 1)]
            hook = hook if isinstance(hook, str) else str(hook.get("text", hook))
    except Exception:
        pass
    return [f"- Best game: **{best_game[0]}** - {sum(best_game[1]) / len(best_game[1]):,.0f} views/post "
            f"over {len(best_game[1])} post(s)",
            f"- Best clip: post #{top['pid']} ({top['game'] or 'unknown'}) - {top['views']:,} views "
            f"[{top['source']}]" + (f' - hook: "{hook}"' if hook else "")], True


def daily_summary(settings, conn: sqlite3.Connection, day: date | None = None, *, write: bool = True) -> str:
    day = day or datetime.now(_tz(settings)).date()
    a, b = _bounds(settings, day)
    created = _one(conn, "SELECT COUNT(*) FROM renders WHERE created_at >= ? AND created_at < ?", (a, b))
    queued = _one(conn, "SELECT COUNT(*) FROM renders WHERE status='queued' "
                        "AND id NOT IN (SELECT render_id FROM posts WHERE render_id IS NOT NULL)")
    scheduled = _one(conn, "SELECT COUNT(*) FROM posts WHERE status='scheduled'")
    published_today = _one(conn, "SELECT COUNT(*) FROM posts WHERE status='published' "
                                 "AND updated_at >= ? AND updated_at < ?", (a, b))
    published_all = _one(conn, "SELECT COUNT(*) FROM posts WHERE status='published'")
    awaiting = _one(conn, "SELECT COUNT(*) FROM posts WHERE status='awaiting_user'")
    ready_manual = _one(conn, "SELECT COUNT(*) FROM posts WHERE status='ready_manual'")
    qc_failed = _one(conn, "SELECT COUNT(*) FROM renders WHERE status='qc_failed' "
                           "AND created_at >= ? AND created_at < ?", (a, b))
    job_failed = _one(conn, "SELECT COUNT(*) FROM jobs WHERE status='failed' "
                            "AND updated_at >= ? AND updated_at < ?", (a, b))
    post_failed = _one(conn, "SELECT COUNT(*) FROM posts WHERE status='failed' "
                             "AND updated_at >= ? AND updated_at < ?", (a, b))
    rec_new = _one(conn, "SELECT COUNT(*) FROM recordings WHERE created_at >= ? AND created_at < ?", (a, b))
    best, _ = _best(conn)
    actions = action_items(settings, conn)

    lines = [
        f"# RobloxAutoPromo - daily summary {day.isoformat()}",
        "",
        "## Today",
        f"- Recordings ingested: {rec_new}",
        f"- Videos created: {created}",
        f"- Waiting for a slot (QC passed): {queued}",
        f"- Scheduled: {scheduled}",
        f"- Published today: {published_today} (all time: {published_all})",
        f"- Awaiting you in TikTok inbox: {awaiting}",
        f"- Ready to post manually: {ready_manual}",
        f"- Failures today: {qc_failed} QC, {job_failed} processing, {post_failed} publishing",
        "",
        "## Performance (real metrics only)",
        *best,
        "",
        "## Action needed",
        *([f"- [ ] {x}" for x in actions] or ["- Nothing - you're all set."]),
        "",
    ]
    text = "\n".join(lines)
    if write:
        out = settings.path("logs") / f"summary-{day.isoformat()}.md"
        out.write_text(text, encoding="utf-8")
        log.info("wrote %s", out)
    return text
