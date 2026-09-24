"""Command line: python -m app <command>

  run                 one pass: ingest > clip > content > render > qc > plan > publish due > summary
  service             run forever (polls the inbox every N seconds; Ctrl+C to stop)
  demo                generate synthetic footage and run the whole pipeline (no TikTok needed)
  status              counts, flags and queue
  summary             print/write today's summary (--day YYYY-MM-DD, --notify)
  pause | resume      stop / restart publishing (processing continues)
  kill | unkill       global kill switch: stops ALL processing and publishing
  approve POST_ID     approve a post for direct publishing
  retry JOB_ID        re-queue a job that gave up
  auth                connect your TikTok account
  metrics add|fetch   record real metrics (manual) or pull them from the API
  dashboard           open the Streamlit dashboard
A file named KILL in the project root also halts everything.
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, db, jobs, log as applog, notify, pipeline, scheduler, summary

log = applog.get("cli")


def _tz(settings) -> ZoneInfo:
    return ZoneInfo(settings.section("schedule").get("timezone", "UTC"))


def _ops(settings) -> dict:
    return settings.section("ops")


def open_db(settings):
    return db.connect(settings.path("database"))


# --------------------------------------------------------------------------- one pass
def maybe_daily_summary(settings, conn, now: datetime | None = None, force: bool = False) -> str | None:
    """Write + notify the daily summary once per local day, after [ops] summary_time (default 21:00)."""
    local = (now or datetime.now(timezone.utc)).astimezone(_tz(settings))
    hh, mm = str(_ops(settings).get("summary_time", "21:00")).split(":")
    if not force:
        if (local.hour, local.minute) < (int(hh), int(mm)):
            return None
        if db.get_state(conn, "last_summary_day") == local.date().isoformat():
            return None
    text = summary.daily_summary(settings, conn, local.date())
    db.set_state(conn, "last_summary_day", local.date().isoformat())
    n_actions = sum(1 for line in text.splitlines() if line.startswith("- [ ]"))
    notify.notify(settings, conn, "RobloxAutoPromo daily summary",
                  f"{n_actions} action(s) needed. See logs/summary-{local.date().isoformat()}.md",
                  category="summary", now=now)
    return text


def notify_actions(settings, conn, now: datetime | None = None) -> int:
    sent = 0
    for item in summary.action_items(settings, conn):
        key = "action|" + re.sub(r"\d+", "#", item)[:80]  # "3 videos ready" == "4 videos ready"
        sent += notify.notify(settings, conn, "RobloxAutoPromo: action needed", item, now=now, key=key)
    return sent


def run_pass(settings, conn, now: datetime | None = None) -> dict:
    reason = pipeline.kill_reason(settings, conn)
    if reason:
        log.warning("halted: %s", reason)
        notify.notify(settings, conn, "RobloxAutoPromo halted", reason, now=now)
        return {"halted": reason}
    out: dict = {"pipeline": pipeline.run_once(settings, conn, now=now)}
    try:
        out["planned"] = scheduler.plan_posts(settings, conn, now)
    except Exception as e:  # noqa: BLE001
        log.error("planning failed: %s", e)
    try:
        out["published"] = scheduler.publish_due(settings, conn, now)
    except ModuleNotFoundError as e:
        log.error("publisher unavailable: %s", e)
    except Exception as e:  # noqa: BLE001
        log.error("publishing failed: %s", e)
    notify_actions(settings, conn, now)
    maybe_daily_summary(settings, conn, now)
    return out


# --------------------------------------------------------------------------- commands
def cmd_run(settings, args) -> int:
    conn = open_db(settings)
    jobs.recover_stale(conn, older_than_s=3600)
    out = run_pass(settings, conn)
    print(_fmt_pass(out))
    return 1 if "halted" in out else 0


def _fmt_pass(out: dict) -> str:
    if "halted" in out:
        return f"HALTED: {out['halted']}"
    p = out.get("pipeline", {})
    return (f"ingested {p.get('ingested', 0)}, jobs done {p.get('done', 0)}, retrying {p.get('retry', 0)}, "
            f"failed {p.get('failed', 0)}; planned {len(out.get('planned') or [])}; "
            f"published {len(out.get('published') or [])}")


class _Stop:
    flag = False


def _heartbeat_ok(settings, conn, interval: int) -> bool:
    """Refuse to start a second service if another one is alive."""
    hb = db.get_state(conn, "service_heartbeat")
    pid = db.get_state(conn, "service_pid")
    if hb and pid and pid != str(os.getpid()):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds()
        except ValueError:
            age = 1e9
        if age < max(3 * interval, 180):
            return False
    return True


def cmd_service(settings, args) -> int:
    interval = int(args.interval or _ops(settings).get("poll_seconds", 60))
    conn = open_db(settings)
    if not args.force and not _heartbeat_ok(settings, conn, interval):
        print("Another service instance looks alive (heartbeat < 3 intervals old). Use --force to override.")
        return 2
    db.set_state(conn, "service_pid", str(os.getpid()))
    jobs.recover_stale(conn)  # nothing can be running at startup: crash recovery

    def _handler(signum, frame):
        _Stop.flag = True
        log.info("signal %s received; stopping after current step", signum)

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handler)
    log.info("service started (pid %s, every %ss); drop recordings in %s", os.getpid(), interval,
             settings.path("inbox"))
    halted_logged = False
    passes = 0
    while not _Stop.flag:
        db.set_state(conn, "service_heartbeat", db.now())
        reason = pipeline.kill_reason(settings, conn)
        if reason:
            if not halted_logged:
                log.warning("halted: %s (service idles until cleared)", reason)
                notify.notify(settings, conn, "RobloxAutoPromo halted", reason)
                halted_logged = True
        else:
            halted_logged = False
            try:
                out = run_pass(settings, conn)
                log.info("pass: %s", _fmt_pass(out))
            except Exception as e:  # noqa: BLE001 - the service must survive anything
                log.exception("pass crashed: %s", e)
        passes += 1
        if args.max_passes and passes >= args.max_passes:
            break
        deadline = _time.monotonic() + interval
        while not _Stop.flag and _time.monotonic() < deadline:
            _time.sleep(min(1.0, max(0.0, deadline - _time.monotonic())))
    db.set_state(conn, "service_heartbeat", "")
    log.info("service stopped")
    return 0


def cmd_demo(settings, args) -> int:
    if args.here:
        root = settings.root
    else:
        root = Path(args.root_dir or tempfile.mkdtemp(prefix="autopromo-demo-"))
    demo_settings = config.load(root)
    demo_settings.raw.setdefault("publish", {})["mode"] = "local"  # never touch TikTok in demo
    print(f"Demo root: {root}")
    script = config.ROOT / "scripts" / "make_sample.py"
    r = subprocess.run([sys.executable, str(script), "--out-root", str(root)], cwd=str(config.ROOT))
    if r.returncode != 0:
        print("make_sample.py failed - is ffmpeg installed and on PATH?")
        return r.returncode
    conn = open_db(demo_settings)
    now = datetime.now(timezone.utc)
    stats = pipeline.run_once(demo_settings, conn, now=now)
    print(f"pipeline: {stats}")
    planned = scheduler.plan_posts(demo_settings, conn, now)
    print(f"scheduled {len(planned)} post(s)")
    # pretend the clock has moved on so the demo shows the local "publish" step too
    later = now + timedelta(days=scheduler.HORIZON_DAYS)
    if not pipeline.kill_reason(demo_settings, conn) and db.get_state(conn, "publishing_paused") != "1":
        try:
            print(f"local publish: {scheduler.publish_due(demo_settings, conn, later)}")
        except ModuleNotFoundError as e:
            print(f"(publisher not available: {e})")
    print()
    print(summary.daily_summary(demo_settings, conn))
    print(f"Outputs: {root / 'rendered'}, {root / 'queue' / 'ready'}; database {demo_settings.path('database')}")
    print(f"Dashboard: python -m app --root \"{root}\" dashboard")
    return 0


def cmd_flag(settings, args) -> int:
    conn = open_db(settings)
    c = args.cmd
    if c == "pause":
        db.set_state(conn, "publishing_paused", "1")
        db.set_state(conn, "publishing_paused_reason", "paused by user")
        print("Publishing paused. Processing continues. `python -m app resume` to restart.")
    elif c == "resume":
        db.set_state(conn, "publishing_paused", "0")
        db.set_state(conn, "publishing_paused_reason", "")
        db.set_state(conn, "paused", "0")
        print("Publishing resumed.")
    elif c == "kill":
        db.set_state(conn, "kill_switch", "1")
        print("KILL SWITCH ON: all processing and publishing stopped. `python -m app unkill` to clear.")
    elif c == "unkill":
        db.set_state(conn, "kill_switch", "0")
        kf = settings.root / "KILL"
        if kf.exists():
            kf.unlink()
            print(f"Removed {kf}.")
        print("Kill switch cleared.")
    return 0


def cmd_status(settings, args) -> int:
    conn = open_db(settings)
    q = lambda sql, a=(): conn.execute(sql, a).fetchall()  # noqa: E731
    print(f"Project: {settings.root}")
    print(f"Kill switch: {pipeline.kill_reason(settings, conn) or 'off'}")
    print(f"Publishing: {scheduler.publishing_block_reason(settings, conn) or 'active'} "
          f"(mode {settings.section('publish').get('mode', 'local')})")
    for table in ("recordings", "renders", "posts", "jobs"):
        rows = q(f"SELECT status, COUNT(*) n FROM {table} GROUP BY status ORDER BY status")
        print(f"{table:>10}: " + (", ".join(f"{r['status']}={r['n']}" for r in rows) or "none"))
    up = q("SELECT id, render_id, status, scheduled_for, mode FROM posts WHERE status IN "
           "('scheduled','uploading') ORDER BY scheduled_for LIMIT 10")
    if up:
        print("Upcoming posts:")
        for r in up:
            print(f"  #{r['id']} render {r['render_id']} {r['status']} at {r['scheduled_for']} ({r['mode']})")
    bad = q("SELECT id, kind, ref_id, attempts, last_error FROM jobs WHERE status='failed' ORDER BY id DESC LIMIT 10")
    if bad:
        print("Failed jobs (python -m app retry ID):")
        for r in bad:
            print(f"  job {r['id']} {r['kind']}({r['ref_id']}) x{r['attempts']}: {(r['last_error'] or '')[:100]}")
    hb = db.get_state(conn, "service_heartbeat")
    print(f"Service heartbeat: {hb or 'not running'}")
    acts = summary.action_items(settings, conn)
    print("Action needed:" if acts else "Action needed: nothing")
    for a in acts:
        print(f"  - {a}")
    return 0


def cmd_summary(settings, args) -> int:
    conn = open_db(settings)
    day = datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else None
    text = summary.daily_summary(settings, conn, day)
    print(text)
    if args.notify:
        d = (day or datetime.now(_tz(settings)).date()).isoformat()
        n = sum(1 for line in text.splitlines() if line.startswith("- [ ]"))
        notify.notify(settings, conn, "RobloxAutoPromo daily summary",
                      f"{n} action(s) needed. See logs/summary-{d}.md", category="summary")
        db.set_state(conn, "last_summary_day", d)
    return 0


def cmd_approve(settings, args) -> int:
    conn = open_db(settings)
    row = conn.execute("SELECT id, status, mode FROM posts WHERE id=?", (args.post_id,)).fetchone()
    if not row:
        print(f"No post #{args.post_id}")
        return 1
    value = db.dumps({"privacy_level": args.privacy}) if args.privacy else "1"
    db.set_state(conn, f"approved:{args.post_id}", value)
    print(f"Approved post #{args.post_id} ({row['mode']}, {row['status']})"
          + (f" with privacy {args.privacy}." if args.privacy else
             " (privacy from [publish] privacy_level)."))
    return 0


def cmd_retry(settings, args) -> int:
    conn = open_db(settings)
    job = conn.execute("SELECT kind, ref_id FROM jobs WHERE id=?", (args.job_id,)).fetchone()
    if not job or not jobs.retry(conn, args.job_id):
        print(f"Job {args.job_id} is not in 'failed' state.")
        return 1
    rec = pipeline._recording_for_job(conn, job["kind"], job["ref_id"])
    if rec:
        conn.execute("UPDATE recordings SET status='processing', updated_at=? WHERE id=? AND status='failed'",
                     (db.now(), rec))
        conn.commit()
    print(f"Job {args.job_id} re-queued.")
    return 0


def cmd_auth(settings, args) -> int:
    importlib.import_module("app.tiktok").authorize_interactive(settings)
    conn = open_db(settings)
    if db.get_state(conn, "publishing_paused") == "1":
        print("Tip: publishing is paused - run `python -m app resume` when ready.")
    return 0


METRIC_FIELDS = ("views", "likes", "comments", "shares", "avg_watch_s", "completion_rate",
                 "profile_visits", "roblox_visits")


def cmd_metrics(settings, args) -> int:
    conn = open_db(settings)
    analytics = importlib.import_module("app.analytics")
    if args.action == "fetch":
        print(analytics.fetch_api_metrics(settings, conn))
        return 0
    if args.post is None:
        print("--post is required")
        return 1
    fields = {k: getattr(args, k) for k in METRIC_FIELDS if getattr(args, k) is not None}
    if not fields:
        print("Give at least one metric, e.g. --views 1234")
        return 1
    analytics.add_manual_metrics(conn, args.post, **fields)
    print(f"Recorded manual metrics for post #{args.post}: {fields}")
    return 0


def cmd_dashboard(settings, args) -> int:
    dash = Path(__file__).with_name("dashboard.py")
    env = dict(os.environ, AUTOPROMO_ROOT=str(settings.root), PYTHONPATH=str(config.ROOT))
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(dash),
                            "--server.headless", "false", "--", "--root", str(settings.root)],
                           env=env, cwd=str(config.ROOT))


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="project root (default: this project or $AUTOPROMO_ROOT)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="single pass")
    s = sub.add_parser("service", help="run forever")
    s.add_argument("--interval", type=int, help="seconds between passes (default [ops] poll_seconds or 60)")
    s.add_argument("--max-passes", type=int, default=0, help=argparse.SUPPRESS)
    s.add_argument("--force", action="store_true", help="start even if another instance seems alive")
    d = sub.add_parser("demo", help="synthetic end-to-end demo")
    d.add_argument("--here", action="store_true", help="use this project's inbox instead of a temp folder")
    d.add_argument("--root-dir", help="demo into this folder")
    for c in ("pause", "resume", "kill", "unkill", "status"):
        sub.add_parser(c)
    sm = sub.add_parser("summary")
    sm.add_argument("--day", help="YYYY-MM-DD (default today)")
    sm.add_argument("--notify", action="store_true", help="also send a notification")
    a = sub.add_parser("approve")
    a.add_argument("post_id", type=int)
    a.add_argument("--privacy", help="TikTok privacy level for this post, e.g. SELF_ONLY, PUBLIC_TO_EVERYONE")
    r = sub.add_parser("retry")
    r.add_argument("job_id", type=int)
    sub.add_parser("auth")
    m = sub.add_parser("metrics")
    m.add_argument("action", choices=["add", "fetch"])
    m.add_argument("--post", type=int)
    for f in METRIC_FIELDS:
        m.add_argument("--" + f.replace("_", "-"), dest=f,
                       type=float if f in ("avg_watch_s", "completion_rate") else int)
    sub.add_parser("dashboard")
    return p


COMMANDS = {"run": cmd_run, "service": cmd_service, "demo": cmd_demo, "pause": cmd_flag, "resume": cmd_flag,
            "kill": cmd_flag, "unkill": cmd_flag, "status": cmd_status, "summary": cmd_summary,
            "approve": cmd_approve, "retry": cmd_retry, "auth": cmd_auth, "metrics": cmd_metrics,
            "dashboard": cmd_dashboard}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = config.load(Path(args.root) if args.root else None)
    applog.setup(settings.path("logs"))
    return COMMANDS[args.cmd](settings, args) or 0
