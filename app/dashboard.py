"""Streamlit dashboard: `python -m app dashboard` (or `streamlit run app/dashboard.py`).

Reads SQLite directly. Performance numbers only come from the metrics table and
are labelled with their source (api/manual); missing values show "unavailable",
never zero.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # executed by `streamlit run app/dashboard.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from app import config, db

UNAVAILABLE = "unavailable"


def _root() -> Path | None:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--root")
    ns, _ = ap.parse_known_args(sys.argv[1:])
    return Path(ns.root) if ns.root else None


def _conn(settings):
    return db.connect(settings.path("database"))


def _df(conn, sql, args=()) -> pd.DataFrame:
    rows = conn.execute(sql, args).fetchall()
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


def _fmt(v, fmt="{:,.0f}"):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return UNAVAILABLE
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


LATEST_METRICS = """
    SELECT m.* FROM metrics m
    JOIN (SELECT post_id, MAX(id) mid FROM metrics GROUP BY post_id) l ON l.mid = m.id
"""

POSTS_FULL = f"""
    SELECT p.id AS post, p.status, p.mode, p.scheduled_for, p.share_url,
           g.name AS game, r.path AS video, rec.thumbnail AS thumbnail,
           co.hooks, co.chosen_hook, co.caption,
           lm.views, lm.likes, lm.comments, lm.shares, lm.avg_watch_s, lm.completion_rate,
           lm.profile_visits, lm.roblox_visits, lm.source, lm.captured_at
    FROM posts p
    JOIN renders r ON r.id = p.render_id
    JOIN clips c ON c.id = r.clip_id
    JOIN recordings rec ON rec.id = c.recording_id
    LEFT JOIN games g ON g.id = rec.game_id
    LEFT JOIN content co ON co.id = r.content_id
    LEFT JOIN ({LATEST_METRICS}) lm ON lm.post_id = p.id
"""


def _hook(hooks_json, idx):
    import json
    try:
        hooks = json.loads(hooks_json or "[]")
        h = hooks[min(int(idx or 0), len(hooks) - 1)] if hooks else None
        return h if isinstance(h, str) or h is None else str(h.get("text", h))
    except Exception:
        return None


def main() -> None:
    settings = config.load(_root())
    st.set_page_config(page_title="RobloxAutoPromo", layout="wide")
    conn = _conn(settings)
    one = lambda sql, a=(): conn.execute(sql, a).fetchone()[0]  # noqa: E731

    st.title("RobloxAutoPromo")
    kill = (settings.root / "KILL").exists() or db.get_state(conn, "kill_switch") == "1"
    paused = db.get_state(conn, "publishing_paused") == "1"
    if kill:
        st.error("KILL SWITCH ON - nothing is processed or published.")
    elif paused:
        st.warning(f"Publishing paused: {db.get_state(conn, 'publishing_paused_reason') or ''}")

    # ---- controls
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Pause publishing", disabled=paused):
        db.set_state(conn, "publishing_paused", "1")
        db.set_state(conn, "publishing_paused_reason", "paused from dashboard")
        st.rerun()
    if c2.button("Resume publishing", disabled=not paused):
        db.set_state(conn, "publishing_paused", "0")
        db.set_state(conn, "publishing_paused_reason", "")
        st.rerun()
    if c3.button("KILL (stop everything)", type="primary", disabled=kill):
        db.set_state(conn, "kill_switch", "1")
        st.rerun()
    if c4.button("Clear kill switch", disabled=not kill):
        db.set_state(conn, "kill_switch", "0")
        kf = settings.root / "KILL"
        if kf.exists():
            kf.unlink()
        st.rerun()

    # ---- headline counts
    created = one("SELECT COUNT(*) FROM renders")
    queued = one("SELECT COUNT(*) FROM renders WHERE status='queued' "
                 "AND id NOT IN (SELECT render_id FROM posts WHERE render_id IS NOT NULL)")
    published = one("SELECT COUNT(*) FROM posts WHERE status='published'")
    manual = one("SELECT COUNT(*) FROM posts WHERE status IN ('ready_manual','awaiting_user')")
    failures = (one("SELECT COUNT(*) FROM renders WHERE status='qc_failed'")
                + one("SELECT COUNT(*) FROM posts WHERE status='failed'")
                + one("SELECT COUNT(*) FROM jobs WHERE status='failed'"))
    lm = _df(conn, LATEST_METRICS)
    total_views = None if lm.empty or lm["views"].dropna().empty else int(lm["views"].dropna().sum())
    k = st.columns(6)
    k[0].metric("Videos created", created)
    k[1].metric("Queued (QC passed)", queued)
    k[2].metric("Published", published)
    k[3].metric("Waiting for you", manual)
    k[4].metric("Failures", failures)
    k[5].metric("Total views (real)", _fmt(total_views))
    if not lm.empty:
        src = ", ".join(f"{s}: {n}" for s, n in lm["source"].value_counts().items())
        st.caption(f"Metric sources (latest per post) - {src}. Nothing is estimated.")
    else:
        st.caption("No metrics yet - add them below or run `python -m app metrics fetch`.")

    posts = _df(conn, POSTS_FULL)
    tab_perf, tab_queue, tab_metrics = st.tabs(["Performance", "Queue", "Enter metrics"])

    with tab_perf:
        if posts.empty or posts["views"].dropna().empty:
            st.info("Performance: no data yet.")
        else:
            withm = posts[posts["views"].notna()].copy()
            withm["hook"] = [_hook(h, i) for h, i in zip(withm["hooks"], withm["chosen_hook"])]
            st.subheader("Views per video")
            show = withm[["post", "game", "hook", "views", "likes", "comments", "shares",
                          "avg_watch_s", "completion_rate", "source", "captured_at"]]
            st.dataframe(show.sort_values("views", ascending=False).astype(object).where(show.notna(), UNAVAILABLE),
                         hide_index=True)
            g1, g2 = st.columns(2)
            with g1:
                st.subheader("Best games (views per post)")
                games = withm.groupby("game", dropna=False)["views"].agg(["mean", "count"]).reset_index()
                games.columns = ["game", "views_per_post", "posts"]
                st.dataframe(games.sort_values("views_per_post", ascending=False), hide_index=True)
            with g2:
                st.subheader("Best hooks")
                hooks = withm.groupby("hook", dropna=False)["views"].agg(["mean", "count"]).reset_index()
                hooks.columns = ["hook", "avg_views", "posts"]
                st.dataframe(hooks.sort_values("avg_views", ascending=False).head(15), hide_index=True)
            st.subheader("Performance over time")
            hist = _df(conn, "SELECT substr(captured_at,1,10) AS day, SUM(views) AS views FROM metrics m "
                             "JOIN (SELECT post_id, substr(captured_at,1,10) d, MAX(id) mid FROM metrics "
                             "WHERE views IS NOT NULL GROUP BY post_id, d) l ON l.mid=m.id GROUP BY day ORDER BY day")
            if not hist.empty:
                st.line_chart(hist.set_index("day")["views"])
                st.caption("Sum of each post's latest view count captured that day.")

    with tab_queue:
        if posts.empty:
            st.info("No posts yet.")
        else:
            st.dataframe(posts[["post", "status", "mode", "scheduled_for", "game", "caption", "share_url"]]
                         .sort_values("scheduled_for", ascending=False), hide_index=True)
        st.subheader("Preview")
        waiting = _df(conn, """
            SELECT r.id AS render, r.status, r.path, rec.thumbnail, g.name AS game, p.id AS post, p.status AS post_status
            FROM renders r JOIN clips c ON c.id=r.clip_id JOIN recordings rec ON rec.id=c.recording_id
            LEFT JOIN games g ON g.id=rec.game_id LEFT JOIN posts p ON p.render_id=r.id
            WHERE r.status IN ('queued','rendered','qc_failed') OR p.status IN ('scheduled','ready_manual','awaiting_user')
            ORDER BY r.id DESC LIMIT 12""")
        if waiting.empty:
            st.caption("Nothing in the queue.")
        for _, w in waiting.iterrows():
            with st.expander(f"Render #{w['render']} - {w['game'] or 'unknown game'} - {w['status']}"
                             + (f" / post #{int(w['post'])} {w['post_status']}" if pd.notna(w['post']) else "")):
                vp = Path(w["path"]) if w["path"] else None
                if vp is not None and not vp.is_absolute():
                    vp = settings.root / vp
                tp = Path(w["thumbnail"]) if w["thumbnail"] else None
                if tp is not None and not tp.is_absolute():
                    tp = settings.root / tp
                if vp and vp.exists():
                    st.video(str(vp))
                elif tp and tp.exists():
                    st.image(str(tp), width=240)
                else:
                    st.caption("preview unavailable")

    with tab_metrics:
        st.write("Record numbers you read in the TikTok app. Stored with source = manual.")
        opts = _df(conn, "SELECT id FROM posts ORDER BY id DESC")
        if opts.empty:
            st.info("No posts yet.")
        else:
            with st.form("manual_metrics"):
                pid = st.selectbox("Post", opts["id"].tolist())
                cols = st.columns(4)
                vals = {
                    "views": cols[0].number_input("Views", min_value=0, value=None, step=1),
                    "likes": cols[1].number_input("Likes", min_value=0, value=None, step=1),
                    "comments": cols[2].number_input("Comments", min_value=0, value=None, step=1),
                    "shares": cols[3].number_input("Shares", min_value=0, value=None, step=1),
                    "avg_watch_s": cols[0].number_input("Avg watch (s)", min_value=0.0, value=None),
                    "completion_rate": cols[1].number_input("Completion rate (0-1)", min_value=0.0,
                                                            max_value=1.0, value=None),
                    "profile_visits": cols[2].number_input("Profile visits", min_value=0, value=None, step=1),
                    "roblox_visits": cols[3].number_input("Roblox visits", min_value=0, value=None, step=1),
                }
                if st.form_submit_button("Save"):
                    fields = {k: v for k, v in vals.items() if v is not None}
                    if not fields:
                        st.error("Enter at least one number.")
                    else:
                        _save_manual(conn, int(pid), fields)
                        st.success(f"Saved for post #{pid}: {fields}")


def _save_manual(conn, post_id: int, fields: dict) -> None:
    try:
        from app import analytics
        analytics.add_manual_metrics(conn, post_id, **fields)
    except (ImportError, AttributeError):
        cols = ", ".join(fields)
        conn.execute(f"INSERT INTO metrics(post_id, captured_at, {cols}, source) VALUES(?,?,"
                     f"{','.join('?' * len(fields))},'manual')", (post_id, db.now(), *fields.values()))
        conn.commit()


main()
