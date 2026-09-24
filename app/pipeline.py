"""Processing pipeline, run as persistent jobs.

    ingest.scan_inbox            -> recording ids
    'clip'    job (recording_id) -> clipper.find_clips
    'content' job (clip_id)      -> content.generate
    'render'  job (clip_id)      -> render.render
    'qc'      job (render_id)    -> qc.check  (render -> 'queued' | 'qc_failed')

Every stage first looks in the DB for its output and skips work that already
exists, so a pass can be interrupted and re-run at any time.
Recording status: new -> processing -> done | review | no_clips | failed.
"""
from __future__ import annotations

import importlib
import sqlite3
import traceback
from datetime import datetime

from . import db, jobs
from .log import get

log = get("pipeline")

STAGES = ("clip", "content", "render", "qc")
MAX_JOBS_PER_PASS = 500  # safety valve against runaway loops


# --------------------------------------------------------------------------- safety
def kill_reason(settings, conn: sqlite3.Connection) -> str | None:
    """Why everything must stop, or None. Checked before any processing/publishing."""
    if (settings.root / "KILL").exists():
        return f"KILL file present at {settings.root / 'KILL'}"
    if db.get_state(conn, "kill_switch") == "1":
        return "kill switch is ON (python -m app unkill to clear)"
    if settings.section("safety").get("kill_switch"):
        return "kill_switch = true in config/settings.toml [safety]"
    return None


def _mod(name: str):
    """Late import of teammate modules (lets tests monkeypatch sys.modules)."""
    return importlib.import_module(f"app.{name}")


# --------------------------------------------------------------------------- stages
def _stage_clip(settings, conn, recording_id: int) -> None:
    conn.execute("UPDATE recordings SET status='processing', updated_at=? WHERE id=? AND status='new'",
                 (db.now(), recording_id))
    conn.commit()
    have = [r["id"] for r in conn.execute("SELECT id FROM clips WHERE recording_id=?", (recording_id,))]
    if not have:
        _mod("clipper").find_clips(settings, conn, recording_id)
        have = [r["id"] for r in conn.execute("SELECT id FROM clips WHERE recording_id=?", (recording_id,))]
    for cid in have:
        jobs.enqueue(conn, "content", cid, skip_if_done=True)


def _stage_content(settings, conn, clip_id: int) -> None:
    row = conn.execute("SELECT id FROM content WHERE clip_id=?", (clip_id,)).fetchone()
    if not row:
        _mod("content").generate(settings, conn, clip_id)
    jobs.enqueue(conn, "render", clip_id, skip_if_done=True)


def _stage_render(settings, conn, clip_id: int) -> None:
    content = conn.execute("SELECT id FROM content WHERE clip_id=?", (clip_id,)).fetchone()
    if not content:
        raise RuntimeError(f"clip {clip_id} has no content yet")
    row = conn.execute("SELECT id FROM renders WHERE clip_id=? ORDER BY id DESC LIMIT 1", (clip_id,)).fetchone()
    render_id = row["id"] if row else _mod("render").render(settings, conn, clip_id, content["id"])
    jobs.enqueue(conn, "qc", render_id, skip_if_done=True)


def _stage_qc(settings, conn, render_id: int) -> None:
    row = conn.execute("SELECT status FROM renders WHERE id=?", (render_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"render {render_id} not found")
    if row["status"] in (None, "rendered"):
        _mod("qc").check(settings, conn, render_id)


HANDLERS = {"clip": _stage_clip, "content": _stage_content, "render": _stage_render, "qc": _stage_qc}


# --------------------------------------------------------------------------- bookkeeping
def _recording_for_job(conn, kind: str, ref_id: int) -> int | None:
    if kind == "clip":
        return ref_id
    if kind in ("content", "render"):
        r = conn.execute("SELECT recording_id FROM clips WHERE id=?", (ref_id,)).fetchone()
    elif kind == "qc":
        r = conn.execute("SELECT c.recording_id FROM renders r JOIN clips c ON c.id=r.clip_id WHERE r.id=?",
                         (ref_id,)).fetchone()
    else:
        return None
    return r[0] if r else None


def _job_rows_for_recording(conn, rec_id: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT j.* FROM jobs j WHERE
          (j.kind='clip' AND j.ref_id=:r)
          OR (j.kind IN ('content','render') AND j.ref_id IN (SELECT id FROM clips WHERE recording_id=:r))
          OR (j.kind='qc' AND j.ref_id IN (SELECT r.id FROM renders r JOIN clips c ON c.id=r.clip_id
                                             WHERE c.recording_id=:r))
    """, {"r": rec_id}).fetchall()


def finalize_recordings(conn) -> None:
    """Move 'processing' recordings to their final status once no work remains."""
    for rec in conn.execute("SELECT id FROM recordings WHERE status='processing'").fetchall():
        rows = _job_rows_for_recording(conn, rec["id"])
        if any(r["status"] in ("pending", "running") for r in rows):
            continue
        failed = [r for r in rows if r["status"] == "failed"]
        n_clips = conn.execute("SELECT COUNT(*) FROM clips WHERE recording_id=?", (rec["id"],)).fetchone()[0]
        if failed:
            status, err = "failed", "; ".join(f"{r['kind']}#{r['ref_id']}: {(r['last_error'] or '')[:120]}"
                                              for r in failed)[:1000]
        elif not rows:
            continue  # nothing enqueued yet
        elif n_clips == 0:
            status, err = "review", "no usable clips found - please review"
        else:
            status, err = "done", None
        conn.execute("UPDATE recordings SET status=?, error=COALESCE(?, error), updated_at=? WHERE id=?",
                     (status, err, db.now(), rec["id"]))
        log.info("recording %s -> %s", rec["id"], status)
    conn.commit()


def enqueue_pending_recordings(conn) -> int:
    n = 0
    for r in conn.execute("SELECT id FROM recordings WHERE status IN ('new','processing')").fetchall():
        jobs.enqueue(conn, "clip", r["id"], skip_if_done=True)
        n += 1
    return n


def process_jobs(settings, conn, now: datetime | None = None, limit: int = MAX_JOBS_PER_PASS) -> dict:
    stats = {"done": 0, "retry": 0, "failed": 0}
    for _ in range(limit):
        if kill_reason(settings, conn):
            log.warning("kill switch engaged mid-pass; stopping")
            break
        job = jobs.claim(conn, kinds=list(STAGES), now=now)
        if job is None:
            break
        try:
            HANDLERS[job["kind"]](settings, conn, job["ref_id"])
            jobs.complete(conn, job["id"])
            stats["done"] += 1
        except Exception as e:  # noqa: BLE001 - any stage error is retried
            conn.rollback()
            log.debug(traceback.format_exc())
            res = jobs.fail(conn, job["id"], f"{type(e).__name__}: {e}", settings=settings, now=now)
            stats["retry" if res == "pending" else "failed"] += 1
    return stats


def run_once(settings, conn: sqlite3.Connection | None = None, now: datetime | None = None) -> dict:
    """One full processing pass: ingest -> clip -> content -> render -> qc."""
    own = conn is None
    conn = conn or db.connect(settings.path("database"))
    try:
        reason = kill_reason(settings, conn)
        if reason:
            log.warning("pipeline halted: %s", reason)
            return {"halted": reason}
        new_ids: list = []
        try:
            new_ids = _mod("ingest").scan_inbox(settings, conn) or []
        except Exception as e:  # ingest errors should not block already-queued work
            log.error("ingest failed: %s", e)
        enqueue_pending_recordings(conn)
        stats = process_jobs(settings, conn, now=now)
        finalize_recordings(conn)
        stats["ingested"] = len(new_ids)
        log.info("pipeline pass: %s", stats)
        return stats
    finally:
        if own:
            conn.close()
