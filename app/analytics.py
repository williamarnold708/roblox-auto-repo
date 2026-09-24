"""Post performance metrics. Two sources only, never estimated:
  * 'manual' - numbers the user typed in from TikTok Studio / Roblox analytics
  * 'api'    - TikTok Display API /v2/video/query/ (view/like/comment/share
               counts only; watch time, completion rate and profile visits are
               NOT exposed there, so they stay NULL for API rows)
Summaries use the latest snapshot per post (any source)."""
from __future__ import annotations

import json
import sqlite3

from . import db
from . import log as _log
from . import tiktok as tt

_logger = _log.get("analytics")

INT_FIELDS = ("views", "likes", "comments", "shares", "profile_visits", "roblox_visits")
FLOAT_FIELDS = ("avg_watch_s", "completion_rate")
METRIC_FIELDS = INT_FIELDS + FLOAT_FIELDS


def _insert(conn, post_id: int, source: str, captured_at: str | None, values: dict) -> int:
    cols = ["post_id", "captured_at", "source", *values.keys()]
    cur = conn.execute(
        f"INSERT INTO metrics({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        (post_id, captured_at or db.now(), source, *values.values()))
    conn.commit()
    return cur.lastrowid


def add_manual_metrics(conn: sqlite3.Connection, post_id: int, captured_at: str | None = None,
                       **fields) -> int:
    """Record a manual snapshot. Only the fields given are stored; the rest stay NULL."""
    unknown = set(fields) - set(METRIC_FIELDS)
    if unknown:
        raise ValueError(f"unknown metric field(s): {sorted(unknown)}")
    if conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone() is None:
        raise ValueError(f"post {post_id} not found")
    values = {}
    for k, v in fields.items():
        if v is None or v == "":
            continue
        v = int(v) if k in INT_FIELDS else float(v)
        if v < 0:
            raise ValueError(f"{k} cannot be negative")
        if k == "completion_rate" and v > 1:
            if v <= 100:
                v = v / 100.0  # accept a percentage
            else:
                raise ValueError("completion_rate must be 0..1 or a percentage")
        values[k] = v
    if not values:
        raise ValueError("no metric values given")
    return _insert(conn, post_id, "manual", captured_at, values)


def fetch_api_metrics(settings, conn: sqlite3.Connection,
                      client: tt.TikTokClient | None = None) -> int:
    """Pull counts for every post with a TikTok video_id. Returns rows inserted."""
    posts = conn.execute("SELECT id, video_id, share_url FROM posts "
                         "WHERE video_id IS NOT NULL AND video_id != ''").fetchall()
    if not posts:
        return 0
    client = client or tt.TikTokClient(settings)
    videos = {str(v.get("id")): v for v in client.list_videos([p["video_id"] for p in posts])}
    n = 0
    for p in posts:
        v = videos.get(str(p["video_id"]))
        if not v:
            continue
        values = {"views": v.get("view_count"), "likes": v.get("like_count"),
                  "comments": v.get("comment_count"), "shares": v.get("share_count")}
        _insert(conn, p["id"], "api", None, values)
        if v.get("share_url") and not p["share_url"]:
            conn.execute("UPDATE posts SET share_url=? WHERE id=?", (v["share_url"], p["id"]))
            conn.commit()
        n += 1
    _logger.info("fetched API metrics for %d/%d posts", n, len(posts))
    return n


# --------------------------------------------------------------------------- summaries
_LATEST = """
    SELECT m.* FROM metrics m
    JOIN (SELECT post_id, MAX(id) AS mid FROM metrics GROUP BY post_id) l ON l.mid = m.id
"""


def _dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def totals(conn) -> dict:
    r = conn.execute(f"""
        SELECT COUNT(*) AS posts_with_metrics, SUM(views) AS views, SUM(likes) AS likes,
               SUM(comments) AS comments, SUM(shares) AS shares,
               SUM(profile_visits) AS profile_visits, SUM(roblox_visits) AS roblox_visits
        FROM ({_LATEST})""").fetchone()
    out = dict(r)
    out["posts_total"] = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    out["published"] = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE status='published'").fetchone()[0]
    return out


def views_per_video(conn) -> list[dict]:
    return _dicts(conn.execute(f"""
        SELECT p.id AS post_id, p.video_id, p.share_url, p.status, p.mode,
               lm.views, lm.likes, lm.comments, lm.shares, lm.avg_watch_s,
               lm.completion_rate, lm.source, lm.captured_at
        FROM posts p LEFT JOIN ({_LATEST}) lm ON lm.post_id = p.id
        ORDER BY lm.views IS NULL, lm.views DESC, p.id""").fetchall())


def _hook_text(hooks_json, idx) -> str | None:
    try:
        hooks = json.loads(hooks_json) if hooks_json else []
        h = hooks[int(idx or 0)]
    except (ValueError, IndexError, TypeError):
        return None
    if isinstance(h, dict):
        return h.get("text") or h.get("hook") or json.dumps(h)
    return str(h)


def best_hooks(conn, limit: int = 10) -> list[dict]:
    rows = conn.execute(f"""
        SELECT p.id AS post_id, c.hooks, c.chosen_hook, lm.views, lm.likes, lm.shares
        FROM posts p JOIN renders r ON r.id = p.render_id
        JOIN content c ON c.id = r.content_id
        JOIN ({_LATEST}) lm ON lm.post_id = p.id
        WHERE lm.views IS NOT NULL""").fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        hook = _hook_text(r["hooks"], r["chosen_hook"])
        if hook is None:
            continue
        a = agg.setdefault(hook, {"hook": hook, "posts": 0, "views": 0, "likes": 0, "shares": 0})
        a["posts"] += 1
        a["views"] += r["views"] or 0
        a["likes"] += r["likes"] or 0
        a["shares"] += r["shares"] or 0
    out = sorted(agg.values(), key=lambda a: a["views"] / a["posts"], reverse=True)
    for a in out:
        a["avg_views"] = a["views"] / a["posts"]
    return out[:limit]


def best_games(conn) -> list[dict]:
    return _dicts(conn.execute(f"""
        SELECT g.id AS game_id, g.name, COUNT(lm.id) AS posts,
               SUM(lm.views) AS views, AVG(lm.views) AS avg_views,
               SUM(lm.likes) AS likes, SUM(lm.roblox_visits) AS roblox_visits
        FROM posts p JOIN renders r ON r.id = p.render_id
        JOIN clips cl ON cl.id = r.clip_id
        JOIN recordings rec ON rec.id = cl.recording_id
        JOIN games g ON g.id = rec.game_id
        JOIN ({_LATEST}) lm ON lm.post_id = p.id
        GROUP BY g.id ORDER BY avg_views IS NULL, avg_views DESC""").fetchall())


def time_series(conn, post_id: int | None = None) -> list[dict]:
    """Every snapshot (for charts). Per post if post_id given, else daily
    totals of each post's latest snapshot that day."""
    if post_id is not None:
        return _dicts(conn.execute(
            "SELECT captured_at, views, likes, comments, shares, source FROM metrics "
            "WHERE post_id=? ORDER BY captured_at, id", (post_id,)).fetchall())
    return _dicts(conn.execute("""
        SELECT day, SUM(views) AS views, SUM(likes) AS likes,
               SUM(comments) AS comments, SUM(shares) AS shares
        FROM (SELECT substr(m.captured_at, 1, 10) AS day, m.post_id, m.views, m.likes,
                     m.comments, m.shares
              FROM metrics m JOIN (SELECT post_id, substr(captured_at,1,10) AS d, MAX(id) AS mid
                                   FROM metrics GROUP BY post_id, d) x ON x.mid = m.id)
        GROUP BY day ORDER BY day""").fetchall())
