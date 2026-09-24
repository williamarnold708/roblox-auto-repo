"""Ops tests: job queue, pipeline orchestration, scheduler, summary, CLI flags.
Teammate modules (ingest/clipper/content/render/qc/publisher) are faked."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import cli, config, db, jobs, notify, pipeline, scheduler, summary

LON = ZoneInfo("Europe/London")


@pytest.fixture
def env(tmp_path):
    settings = config.load(tmp_path)
    settings.raw = {**settings.raw,
                    "schedule": {"max_posts_per_day": 2, "slots": ["12:30", "18:30", "20:30"],
                                 "timezone": "Europe/London"},
                    "publish": {"mode": "local"}}
    conn = db.connect(settings.path("database"))
    return settings, conn


def _game(conn, slug):
    cur = conn.execute("INSERT INTO games(slug,name,created_at) VALUES(?,?,?)", (slug, slug.title(), db.now()))
    return cur.lastrowid


def _rec(conn, game_id, status="new", sha=None):
    cur = conn.execute("INSERT INTO recordings(game_id,path,sha256,status,created_at) VALUES(?,?,?,?,?)",
                       (game_id, f"inbox/x/{sha or game_id}.mp4", sha or f"sha{game_id}-{status}", status, db.now()))
    return cur.lastrowid


def _queued_render(conn, game_id, score=0.5, status="queued"):
    rec = _rec(conn, game_id, "done", sha=f"s{conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]}")
    clip = conn.execute("INSERT INTO clips(recording_id,start,end,score,created_at) VALUES(?,?,?,?,?)",
                        (rec, 0, 10, score, db.now())).lastrowid
    content = conn.execute("INSERT INTO content(clip_id,hooks,caption,created_at) VALUES(?,?,?,?)",
                           (clip, '["Can you beat this?"]', "cap", db.now())).lastrowid
    rid = conn.execute("INSERT INTO renders(clip_id,content_id,path,status,created_at) VALUES(?,?,?,?,?)",
                       (clip, content, f"rendered/{clip}.mp4", status, db.now())).lastrowid
    conn.commit()
    return rid


def _post(conn, render_id, status="published", when=None, mode="local"):
    pid = conn.execute("INSERT INTO posts(render_id,mode,scheduled_for,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?)", (render_id, mode, when, status, db.now(), db.now())).lastrowid
    conn.commit()
    return pid


# ----------------------------------------------------------------------------- jobs
def test_enqueue_idempotent(env):
    _, conn = env
    a = jobs.enqueue(conn, "clip", 1)
    b = jobs.enqueue(conn, "clip", 1)
    c = jobs.enqueue(conn, "clip", 2)
    assert a == b and a != c
    j = jobs.claim(conn)
    assert j["id"] == a and j["status"] == "running"
    assert jobs.enqueue(conn, "clip", 1) == a  # running also counts
    jobs.complete(conn, a)
    assert jobs.enqueue(conn, "clip", 1, skip_if_done=True) == a
    assert jobs.enqueue(conn, "clip", 1) != a  # explicit re-run allowed


def test_retry_backoff_then_failed(env, monkeypatch):
    settings, conn = env
    sent = []
    monkeypatch.setattr(notify, "notify", lambda *a, **k: sent.append(a) or True)
    t0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    jid = jobs.enqueue(conn, "render", 7)
    j = jobs.claim(conn, now=t0)
    assert jobs.fail(conn, j["id"], "boom", settings=settings, now=t0) == "pending"
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["run_after"] == (t0 + timedelta(seconds=60)).isoformat(timespec="seconds")
    assert jobs.claim(conn, now=t0 + timedelta(seconds=30)) is None  # not due yet
    j = jobs.claim(conn, now=t0 + timedelta(seconds=61))
    assert j["attempts"] == 2
    jobs.fail(conn, jid, "boom2", settings=settings, now=t0 + timedelta(seconds=61))
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["run_after"] == (t0 + timedelta(seconds=61 + 120)).isoformat(timespec="seconds")
    j = jobs.claim(conn, now=t0 + timedelta(hours=1))
    assert jobs.fail(conn, jid, "boom3", settings=settings) == "failed"
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "failed"
    assert len(sent) == 1
    assert jobs.retry(conn, jid)
    assert jobs.claim(conn)["id"] == jid


def test_recover_stale(env):
    _, conn = env
    jid = jobs.enqueue(conn, "qc", 3)
    jobs.claim(conn)
    assert jobs.claim(conn) is None
    assert jobs.recover_stale(conn) == 1
    assert jobs.claim(conn)["id"] == jid


# ----------------------------------------------------------------------------- pipeline
def _fake_modules(monkeypatch, conn_holder, *, clips=2, fail_render_times=0):
    calls = {"clip": 0, "content": 0, "render": 0, "qc": 0, "render_fail": fail_render_times}

    def scan_inbox(settings, conn):
        return []

    def find_clips(settings, conn, rid):
        calls["clip"] += 1
        ids = []
        for i in range(clips):
            ids.append(conn.execute("INSERT INTO clips(recording_id,start,end,score) VALUES(?,?,?,?)",
                                    (rid, i * 10, i * 10 + 10, 0.5)).lastrowid)
        if not ids:
            conn.execute("UPDATE recordings SET status='review' WHERE id=?", (rid,))
        conn.commit()
        return ids

    def generate(settings, conn, clip_id):
        calls["content"] += 1
        cid = conn.execute("INSERT INTO content(clip_id,hooks) VALUES(?,?)", (clip_id, "[]")).lastrowid
        conn.commit()
        return cid

    def render(settings, conn, clip_id, content_id):
        if calls["render_fail"] > 0:
            calls["render_fail"] -= 1
            raise RuntimeError("ffmpeg exploded")
        calls["render"] += 1
        rid = conn.execute("INSERT INTO renders(clip_id,content_id,status) VALUES(?,?, 'rendered')",
                           (clip_id, content_id)).lastrowid
        conn.commit()
        return rid

    def check(settings, conn, render_id):
        calls["qc"] += 1
        conn.execute("UPDATE renders SET status='queued' WHERE id=?", (render_id,))
        conn.commit()
        return {"passed": True}

    for name, attrs in {"ingest": {"scan_inbox": scan_inbox}, "clipper": {"find_clips": find_clips},
                        "content": {"generate": generate}, "render": {"render": render},
                        "qc": {"check": check}}.items():
        m = types.ModuleType(f"app.{name}")
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, f"app.{name}", m)
    return calls


def test_pipeline_full_pass_and_idempotent(env, monkeypatch):
    settings, conn = env
    calls = _fake_modules(monkeypatch, conn)
    rid = _rec(conn, _game(conn, "obby"))
    conn.commit()
    pipeline.run_once(settings, conn)
    assert conn.execute("SELECT status FROM recordings WHERE id=?", (rid,)).fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM renders WHERE status='queued'").fetchone()[0] == 2
    before = dict(calls)
    pipeline.run_once(settings, conn)
    assert calls == before  # nothing re-done


def test_pipeline_no_clips_goes_to_review(env, monkeypatch):
    settings, conn = env
    _fake_modules(monkeypatch, conn, clips=0)
    rid = _rec(conn, _game(conn, "obby"))
    conn.commit()
    pipeline.run_once(settings, conn)
    assert conn.execute("SELECT status FROM recordings WHERE id=?", (rid,)).fetchone()[0] == "review"


def test_pipeline_retries_failed_stage(env, monkeypatch):
    settings, conn = env
    calls = _fake_modules(monkeypatch, conn, clips=1, fail_render_times=1)
    rid = _rec(conn, _game(conn, "obby"))
    conn.commit()
    t0 = datetime.now(timezone.utc)
    pipeline.run_once(settings, conn, now=t0)
    assert conn.execute("SELECT status FROM recordings WHERE id=?", (rid,)).fetchone()[0] == "processing"
    pipeline.run_once(settings, conn, now=t0 + timedelta(minutes=5))
    assert calls["render"] == 1
    assert conn.execute("SELECT status FROM recordings WHERE id=?", (rid,)).fetchone()[0] == "done"


def test_pipeline_respects_kill(env, monkeypatch):
    settings, conn = env
    calls = _fake_modules(monkeypatch, conn)
    _rec(conn, _game(conn, "obby"))
    conn.commit()
    (settings.root / "KILL").write_text("stop")
    assert "halted" in pipeline.run_once(settings, conn)
    assert calls["clip"] == 0


# ----------------------------------------------------------------------------- scheduler
def test_plan_fills_slots_respects_max_and_no_dup(env):
    settings, conn = env
    g1, g2 = _game(conn, "a"), _game(conn, "b")
    for _ in range(5):
        _queued_render(conn, g1)
        _queued_render(conn, g2)
    now = datetime(2026, 3, 10, 9, 0, tzinfo=LON)
    created = scheduler.plan_posts(settings, conn, now)
    assert len(created) == 4  # 2/day for today + tomorrow
    rows = conn.execute("SELECT p.render_id, p.scheduled_for, rec.game_id FROM posts p JOIN renders r "
                        "ON r.id=p.render_id JOIN clips c ON c.id=r.clip_id JOIN recordings rec "
                        "ON rec.id=c.recording_id ORDER BY p.scheduled_for").fetchall()
    per_day: dict = {}
    for r in rows:
        d = datetime.fromisoformat(r["scheduled_for"]).astimezone(LON).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= 2
    assert len({r["render_id"] for r in rows}) == 4
    assert [r["game_id"] for r in rows] == [g1, g2, g1, g2]  # round robin, spread
    local_slot = datetime.fromisoformat(rows[0]["scheduled_for"]).astimezone(LON)
    assert (local_slot.hour, local_slot.minute) == (12, 30)
    assert scheduler.plan_posts(settings, conn, now) == []  # full


def test_plan_skips_past_slots(env):
    settings, conn = env
    g = _game(conn, "a")
    for _ in range(3):
        _queued_render(conn, g)
    now = datetime(2026, 3, 10, 19, 0, tzinfo=LON)  # only 20:30 left today
    scheduler.plan_posts(settings, conn, now)
    times = [datetime.fromisoformat(r[0]).astimezone(LON) for r in
             conn.execute("SELECT scheduled_for FROM posts ORDER BY scheduled_for")]
    assert times[0].hour == 20 and all(t > now for t in times)


def test_metrics_weighting_keeps_exploration(env):
    vpp = {1: 1000.0, 2: 10.0}
    w = scheduler.game_weights([1, 2, 3], vpp, 0.3)
    assert abs(sum(w.values()) - 1) < 1e-9
    assert w[1] > w[3] > w[2] >= 0.1 - 1e-9
    assert scheduler.game_weights([1, 2], {}, 0.3) == {1: 0.5, 2: 0.5}


def test_plan_prefers_better_game(env):
    settings, conn = env
    good, bad = _game(conn, "good"), _game(conn, "bad")
    old = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    for g, views in ((good, 5000), (bad, 50)):
        pid = _post(conn, _queued_render(conn, g, status="posted"), when=old.isoformat())
        conn.execute("INSERT INTO metrics(post_id,captured_at,views,source) VALUES(?,?,?,'manual')",
                     (pid, db.now(), views))
    for _ in range(6):
        _queued_render(conn, good)
        _queued_render(conn, bad)
    conn.commit()
    settings.raw["schedule"]["max_posts_per_day"] = 3
    now = datetime(2026, 3, 10, 9, 0, tzinfo=LON)
    scheduler.plan_posts(settings, conn, now)
    rows = conn.execute("SELECT rec.game_id g FROM posts p JOIN renders r ON r.id=p.render_id JOIN clips c "
                        "ON c.id=r.clip_id JOIN recordings rec ON rec.id=c.recording_id "
                        "WHERE p.status='scheduled'").fetchall()
    gs = [r["g"] for r in rows]
    assert gs.count(good) > gs.count(bad) >= 1


def _fake_publisher(monkeypatch, pause_after=None):
    published = []
    m = types.ModuleType("app.publisher")

    def publish(settings, conn, post_id):
        published.append(post_id)
        conn.execute("UPDATE posts SET status='ready_manual' WHERE id=?", (post_id,))
        conn.commit()
        if pause_after and len(published) >= pause_after:
            db.set_state(conn, "publishing_paused", "1")
        return "ready_manual"

    m.publish = publish
    m.refresh_status = lambda s, c, p: None
    monkeypatch.setitem(sys.modules, "app.publisher", m)
    return published


def test_publish_due_respects_kill_pause_and_no_dup(env, monkeypatch):
    settings, conn = env
    published = _fake_publisher(monkeypatch)
    g = _game(conn, "a")
    past = datetime(2026, 3, 10, 11, tzinfo=timezone.utc)
    p1 = _post(conn, _queued_render(conn, g), "scheduled", past.isoformat())
    p2 = _post(conn, _queued_render(conn, g), "scheduled", (past + timedelta(days=5)).isoformat())
    now = past + timedelta(hours=1)
    db.set_state(conn, "kill_switch", "1")
    assert scheduler.publish_due(settings, conn, now) == [] and published == []
    db.set_state(conn, "kill_switch", "0")
    db.set_state(conn, "publishing_paused", "1")
    assert scheduler.publish_due(settings, conn, now) == []
    db.set_state(conn, "publishing_paused", "0")
    db.set_state(conn, "paused", "1")
    assert scheduler.publish_due(settings, conn, now) == []
    db.set_state(conn, "paused", "0")
    assert scheduler.publish_due(settings, conn, now) == [(p1, "ready_manual")]
    assert scheduler.publish_due(settings, conn, now) == []  # not twice
    assert published == [p1] and p2 not in published
    with pytest.raises(Exception):
        _post(conn, conn.execute("SELECT render_id FROM posts WHERE id=?", (p1,)).fetchone()[0], "scheduled")


def test_publish_stops_when_publisher_pauses(env, monkeypatch):
    settings, conn = env
    published = _fake_publisher(monkeypatch, pause_after=1)
    g = _game(conn, "a")
    past = datetime(2026, 3, 10, 11, tzinfo=timezone.utc)
    for i in range(3):
        _post(conn, _queued_render(conn, g), "scheduled", (past + timedelta(minutes=i)).isoformat())
    scheduler.publish_due(settings, conn, past + timedelta(hours=1))
    assert len(published) == 1


# ----------------------------------------------------------------------------- summary + notify
def test_summary_action_items(env):
    settings, conn = env
    g = _game(conn, "a")
    _rec(conn, g, "review", sha="rv")
    _post(conn, _queued_render(conn, g, status="posted"), "ready_manual")
    _post(conn, _queued_render(conn, g, status="posted"), "awaiting_user")
    db.set_state(conn, "publishing_paused", "1")
    db.set_state(conn, "publishing_paused_reason", "auth expired")
    conn.commit()
    text = summary.daily_summary(settings, conn, datetime.now(LON).date())
    assert "## Action needed" in text
    assert "needs review" in text
    assert "ready to post manually" in text
    assert "TikTok inbox" in text
    assert "python -m app auth" in text
    assert "no data yet" in text
    assert list(settings.path("logs").glob("summary-*.md"))


def test_summary_best_uses_real_metrics(env):
    settings, conn = env
    g = _game(conn, "tower")
    pid = _post(conn, _queued_render(conn, g, status="posted"), "published")
    conn.execute("INSERT INTO metrics(post_id,captured_at,views,source) VALUES(?,?,?,'api')", (pid, db.now(), 4321))
    conn.commit()
    text = summary.daily_summary(settings, conn, write=False)
    assert "Tower" in text and "4,321" in text and "[api]" in text
    assert "- Nothing" in text


def test_notify_dedupes_per_day(env):
    settings, conn = env
    assert notify.notify(settings, conn, "T", "msg") is True
    assert notify.notify(settings, conn, "T", "msg") is False
    assert notify.notify(settings, conn, "T", "other") is True
    tomorrow = datetime.now(LON) + timedelta(days=1)
    assert notify.notify(settings, conn, "T", "msg", now=tomorrow) is True
    assert (settings.path("logs") / "NOTIFICATIONS.md").read_text().count("**T**") == 3


# ----------------------------------------------------------------------------- cli
def test_cli_pause_kill_approve(env, monkeypatch):
    settings, conn = env
    root = str(settings.root)
    assert cli.main(["--root", root, "pause"]) == 0
    assert db.get_state(conn, "publishing_paused") == "1"
    assert cli.main(["--root", root, "resume"]) == 0
    assert db.get_state(conn, "publishing_paused") == "0"
    assert cli.main(["--root", root, "kill"]) == 0
    assert db.get_state(conn, "kill_switch") == "1"
    assert pipeline.kill_reason(settings, conn)
    (settings.root / "KILL").write_text("")
    assert cli.main(["--root", root, "unkill"]) == 0
    assert db.get_state(conn, "kill_switch") == "0"
    assert not (settings.root / "KILL").exists()
    assert pipeline.kill_reason(settings, conn) is None
    pid = _post(conn, _queued_render(conn, _game(conn, "a")), "scheduled", mode="direct")
    assert cli.main(["--root", root, "approve", str(pid)]) == 0
    assert db.get_state(conn, f"approved:{pid}") == "1"
    assert cli.main(["--root", root, "status"]) == 0


def test_cli_run_halts_on_kill_file(env, monkeypatch):
    settings, conn = env
    calls = _fake_modules(monkeypatch, conn)
    _rec(conn, _game(conn, "a"))
    conn.commit()
    (settings.root / "KILL").write_text("")
    assert cli.main(["--root", str(settings.root), "run"]) == 1
    assert calls["clip"] == 0


def test_cli_run_end_to_end(env, monkeypatch):
    settings, conn = env
    _fake_modules(monkeypatch, conn)
    published = _fake_publisher(monkeypatch)
    _rec(conn, _game(conn, "a"))
    conn.commit()
    assert cli.main(["--root", str(settings.root), "run"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] >= 1
    assert published == []  # slots are in the future
    assert cli.main(["--root", str(settings.root), "service", "--interval", "1", "--max-passes", "1",
                     "--force"]) == 0
