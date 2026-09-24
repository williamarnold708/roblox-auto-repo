"""Persistent job queue on the `jobs` table.

* enqueue() is idempotent: at most one pending/running job per (kind, ref_id).
* claim() atomically moves the next due job to 'running' and counts the attempt.
* fail() retries with exponential backoff (run_after) until MAX_ATTEMPTS, then
  marks the job 'failed' and notifies the user.
* recover_stale() resets 'running' jobs left behind by a crash.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .log import get

log = get("jobs")

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 60  # 1 min, 2 min, 4 min ...


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def backoff_seconds(attempts: int, base: int = BACKOFF_BASE_S) -> int:
    """Delay before retry number `attempts` (1-based)."""
    return base * (2 ** max(0, attempts - 1))


def enqueue(conn: sqlite3.Connection, kind: str, ref_id: int | None, *,
            skip_if_done: bool = False, run_after: datetime | None = None) -> int:
    """Add a job unless an identical pending/running one exists. Returns job id."""
    row = conn.execute(
        "SELECT id, status FROM jobs WHERE kind=? AND ref_id IS ? AND status IN ('pending','running') "
        "ORDER BY id LIMIT 1", (kind, ref_id)).fetchone()
    if row:
        return row["id"]
    if skip_if_done:
        row = conn.execute("SELECT id FROM jobs WHERE kind=? AND ref_id IS ? AND status IN ('done','failed') "
                           "ORDER BY id DESC LIMIT 1", (kind, ref_id)).fetchone()
        if row:
            return row["id"]
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO jobs(kind, ref_id, status, attempts, run_after, created_at, updated_at) "
        "VALUES(?,?,'pending',0,?,?,?)",
        (kind, ref_id, _iso(run_after) if run_after else None, ts, ts))
    conn.commit()
    return cur.lastrowid


def claim(conn: sqlite3.Connection, kinds: list[str] | None = None,
          now: datetime | None = None) -> sqlite3.Row | None:
    """Atomically take the oldest due pending job (optionally of given kinds)."""
    now_s = _iso(_now(now))
    where, args = "status='pending' AND (run_after IS NULL OR run_after <= ?)", [now_s]
    if kinds:
        where += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY COALESCE(run_after, created_at), id LIMIT 1",
                           args).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                     (db.now(), row["id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def complete(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status='done', last_error=NULL, updated_at=? WHERE id=?", (db.now(), job_id))
    conn.commit()


def fail(conn: sqlite3.Connection, job_id: int, error: str, *, settings=None,
         now: datetime | None = None, max_attempts: int = MAX_ATTEMPTS) -> str:
    """Record a failure. Returns the new status ('pending' for retry, or 'failed')."""
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return "missing"
    attempts = row["attempts"] or 0
    error = (error or "")[:2000]
    if attempts >= max_attempts:
        conn.execute("UPDATE jobs SET status='failed', last_error=?, updated_at=? WHERE id=?",
                     (error, db.now(), job_id))
        conn.commit()
        log.error("job %s %s(%s) failed permanently after %s attempts: %s",
                  job_id, row["kind"], row["ref_id"], attempts, error)
        if settings is not None:
            try:
                from . import notify
                notify.notify(settings, conn, f"Job failed: {row['kind']} #{row['ref_id']}",
                              f"Gave up after {attempts} attempts: {error[:200]}. "
                              f"See logs/autopromo.log; retry with `python -m app retry {job_id}`.")
            except Exception as e:
                log.warning("could not notify: %s", e)
        return "failed"
    delay = backoff_seconds(attempts)
    run_after = _now(now) + timedelta(seconds=delay)
    conn.execute("UPDATE jobs SET status='pending', last_error=?, run_after=?, updated_at=? WHERE id=?",
                 (error, _iso(run_after), db.now(), job_id))
    conn.commit()
    log.warning("job %s %s(%s) attempt %s failed, retry in %ss: %s",
                job_id, row["kind"], row["ref_id"], attempts, delay, error)
    return "pending"


def recover_stale(conn: sqlite3.Connection, older_than_s: int | None = None,
                  now: datetime | None = None) -> int:
    """Reset 'running' jobs (left by a crash) to 'pending'. Returns how many.

    older_than_s=None resets every running job (use at process startup, when
    nothing can legitimately be running)."""
    if older_than_s is None:
        cur = conn.execute("UPDATE jobs SET status='pending', run_after=NULL, updated_at=? "
                           "WHERE status='running'", (db.now(),))
    else:
        cutoff = _iso(_now(now) - timedelta(seconds=older_than_s))
        cur = conn.execute("UPDATE jobs SET status='pending', run_after=NULL, updated_at=? "
                           "WHERE status='running' AND updated_at < ?", (db.now(), cutoff))
    conn.commit()
    if cur.rowcount:
        log.warning("recovered %s stale running job(s)", cur.rowcount)
    return cur.rowcount


def retry(conn: sqlite3.Connection, job_id: int) -> bool:
    """Manually re-queue a failed job with a fresh attempt budget."""
    cur = conn.execute("UPDATE jobs SET status='pending', attempts=0, run_after=NULL, updated_at=? "
                       "WHERE id=? AND status='failed'", (db.now(), job_id))
    conn.commit()
    return cur.rowcount > 0


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in
            conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
