"""Publish a scheduled `posts` row in one of three honest modes.

  local  - copy the MP4 + caption .txt to queue/ready/; the user posts manually.
           Status -> 'ready_manual'.
  inbox  - upload to the creator's TikTok inbox (video.upload scope). The user
           finishes the post in the TikTok app. Status -> 'awaiting_user'.
  direct - Direct Post (video.publish). Only when the app has passed TikTok's
           audit AND the user approved this specific post (state key
           f"approved:{post_id}") with an explicit privacy level.

A post is only ever marked 'published' after TikTok's status endpoint
returns PUBLISH_COMPLETE. A post that already has a publish_id is never
uploaded again; its status is refreshed instead.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db
from . import log as _log
from . import tiktok as tt

_logger = _log.get("publisher")
MAX_ATTEMPTS = 3
TERMINAL = {"published", "ready_manual", "failed"}


class PublishRefused(Exception):
    """A precondition (audit, approval, privacy choice, duration) is not met."""


# --------------------------------------------------------------------------- helpers
def _load(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT p.*, r.path AS render_path, r.duration AS render_duration,
                  r.id AS rid, c.caption AS caption, c.hashtags AS hashtags
           FROM posts p
           JOIN renders r ON r.id = p.render_id
           LEFT JOIN content c ON c.id = r.content_id
           WHERE p.id = ?""", (post_id,)).fetchone()
    if row is None:
        raise ValueError(f"post {post_id} not found (or its render is missing)")
    return row


def _update(conn: sqlite3.Connection, post_id: int, **fields) -> None:
    fields["updated_at"] = db.now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))
    conn.commit()


def _hashtags(raw) -> list[str]:
    if not raw:
        return []
    try:
        tags = json.loads(raw) if isinstance(raw, str) else list(raw)
    except ValueError:
        tags = str(raw).split()
    out = []
    for t in tags:
        t = str(t).strip()
        if t:
            out.append(t if t.startswith("#") else "#" + t)
    return out


def caption_text(row) -> str:
    caption = (row["caption"] or "").strip()
    tags = [t for t in _hashtags(row["hashtags"]) if t.lower() not in caption.lower()]
    return (caption + (" " if caption and tags else "") + " ".join(tags)).strip()


def _flag(settings, conn, key: str) -> bool:
    val = settings.section("publish").get(key) if settings is not None else None
    if isinstance(val, bool) and val:
        return True
    return db.get_state(conn, f"tiktok_{key}") == "1" or str(val).lower() in ("1", "true", "yes")


def _pause(conn, reason: str) -> None:
    db.set_state(conn, "publishing_paused", "1")
    db.set_state(conn, "publishing_paused_reason", reason)
    _logger.warning("publishing paused: %s", reason)


def _fail_attempt(conn, row, err: Exception, pause: str | None = None,
                  retryable: bool = True) -> str:
    attempts = (row["attempts"] or 0) + 1
    # Keep the current status: a post with a publish_id stays where it is (it is
    # refreshed, never re-uploaded); a not-yet-started post stays 'scheduled'.
    status = row["status"]
    if not retryable or (attempts >= MAX_ATTEMPTS and not pause):
        status = "failed"
    _update(conn, row["id"], attempts=attempts, error=str(err)[:500], status=status)
    if pause:
        _pause(conn, pause)
    return status


# --------------------------------------------------------------------------- modes
def _publish_local(settings, conn, row) -> str:
    src = Path(row["render_path"] or "")
    if not src.is_file():
        raise FileNotFoundError(f"render file missing: {src}")
    ready = settings.path("queue") / "ready"
    ready.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now().strftime('%Y-%m-%d')}_{row['id']}"
    dst = ready / f"{stem}.mp4"
    shutil.copy2(src, dst)
    (ready / f"{stem}.txt").write_text(caption_text(row) + "\n", encoding="utf-8")
    _update(conn, row["id"], status="ready_manual", error=None)
    _logger.info("post %s ready for manual upload: %s", row["id"], dst)
    return "ready_manual"


def _upload(client: tt.TikTokClient, conn, row, init: dict) -> None:
    publish_id = init.get("publish_id")
    if not publish_id:
        raise tt.ApiError("no_publish_id", "init response lacked publish_id")
    # Record publish_id BEFORE sending bytes so a crash can never cause a re-upload.
    _update(conn, row["id"], publish_id=publish_id, status="uploading", error=None)
    src = init["source_info"]
    client.upload_chunks(init["upload_url"], row["render_path"], src["chunk_size"],
                         src["total_chunk_count"])


def _publish_inbox(settings, conn, row, client) -> str:
    if "video.upload" not in client.granted_scopes():
        raise tt.AuthExpired("video.upload scope not granted; re-run `auth`")
    init = client.init_inbox_upload(row["render_path"])
    _upload(client, conn, row, init)
    new = refresh_status(settings, conn, row["id"], client=client)
    return new


def _approval(conn, post_id: int) -> dict | None:
    raw = db.get_state(conn, f"approved:{post_id}")
    if not raw or raw in ("0", "false"):
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except ValueError:
        return {}


def check_direct_ready(settings, conn, row, client) -> dict:
    """Validate every Direct Post precondition; return init_direct_post kwargs."""
    if not _flag(settings, conn, "app_audited"):
        raise PublishRefused(
            "Direct Post refused: this TikTok app is not marked as audited. Unaudited "
            "clients can only post SELF_ONLY (private) content for a handful of users. "
            "Use publish mode 'inbox' or 'local', or set [publish].app_audited = true "
            "once TikTok has approved your audit.")
    approval = _approval(conn, row["id"])
    if approval is None:
        raise PublishRefused(f"Direct Post needs your approval for post {row['id']} "
                             f"(state key approved:{row['id']}) after previewing it.")
    info = client.creator_info()
    options = list(info.get("privacy_level_options") or [])
    privacy = approval.get("privacy_level") or settings.section("publish").get("privacy_level")
    if not privacy:
        raise PublishRefused("No privacy level chosen. TikTok requires the user to pick one "
                             f"(options: {', '.join(options) or 'none returned'}); there is no default.")
    if privacy not in options:
        raise PublishRefused(f"privacy level {privacy!r} not allowed for this creator "
                             f"(options: {options})")
    max_dur = info.get("max_video_post_duration_sec")
    dur = row["render_duration"]
    if max_dur and dur and float(dur) > float(max_dur):
        raise PublishRefused(f"video is {dur:.0f}s; creator max is {max_dur}s")
    pub = settings.section("publish")

    def pick(key: str, disabled_by_creator: bool = False) -> bool:
        return bool(disabled_by_creator or approval.get(key, pub.get(key, False)))

    return {
        "caption": caption_text(row),
        "privacy_level": privacy,
        "disable_comment": pick("disable_comment", bool(info.get("comment_disabled"))),
        "disable_duet": pick("disable_duet", bool(info.get("duet_disabled"))),
        "disable_stitch": pick("disable_stitch", bool(info.get("stitch_disabled"))),
        "is_aigc": pick("is_aigc"),
        "brand_content_toggle": pick("brand_content_toggle"),
        "brand_organic_toggle": pick("brand_organic_toggle"),
    }


def _publish_direct(settings, conn, row, client) -> str:
    try:
        kwargs = check_direct_ready(settings, conn, row, client)
    except PublishRefused as e:
        audited = "not marked as audited" not in str(e)
        # Missing approval / choice: wait (stay scheduled). Unaudited: fail clearly.
        status = row["status"] if audited else "failed"
        _update(conn, row["id"], status=status, error=str(e))
        _logger.warning("post %s: %s", row["id"], e)
        return status
    init = client.init_direct_post(row["render_path"], **kwargs)
    _upload(client, conn, row, init)
    return refresh_status(settings, conn, row["id"], client=client)


# --------------------------------------------------------------------------- public API
def refresh_status(settings, conn, post_id: int, client: tt.TikTokClient | None = None) -> str:
    """Poll TikTok for a post that already has a publish_id. Only
    PUBLISH_COMPLETE marks it 'published'."""
    row = _load(conn, post_id)
    if not row["publish_id"]:
        return row["status"]
    if row["status"] == "published":
        return "published"
    client = client or tt.TikTokClient(settings)
    data = client.fetch_status(row["publish_id"])
    st = data.get("status")
    if st == "PUBLISH_COMPLETE":
        ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
        video_id = str(ids[0]) if ids else row["video_id"]
        _update(conn, post_id, status="published", video_id=video_id, error=None)
        conn.execute("UPDATE renders SET status='posted' WHERE id=?", (row["rid"],))
        conn.commit()
        return "published"
    if st == "FAILED":
        _update(conn, post_id, status="failed",
                error=f"TikTok reported FAILED: {data.get('fail_reason', 'unknown')}")
        return "failed"
    if st == "SEND_TO_USER_INBOX" or row["mode"] == "inbox":
        new = "awaiting_user"
    else:
        new = "uploading"  # PROCESSING_UPLOAD / PROCESSING_DOWNLOAD (direct)
    if new != row["status"]:
        _update(conn, post_id, status=new)
    return new


def publish(settings, conn: sqlite3.Connection, post_id: int,
            client: tt.TikTokClient | None = None) -> str:
    """Publish one posts row. Returns the new posts.status."""
    row = _load(conn, post_id)
    if row["status"] in TERMINAL:
        return row["status"]
    if row["publish_id"] and row["mode"] != "local":
        try:
            return refresh_status(settings, conn, post_id, client=client)
        except tt.TikTokError as e:
            return _handle_error(conn, row, e)
    mode = row["mode"]
    try:
        if mode == "local":
            return _publish_local(settings, conn, row)
        client = client or tt.TikTokClient(settings)
        if mode == "inbox":
            return _publish_inbox(settings, conn, row, client)
        if mode == "direct":
            return _publish_direct(settings, conn, row, client)
        raise ValueError(f"unknown publish mode {mode!r}")
    except (tt.TikTokError, OSError, ValueError) as e:
        return _handle_error(conn, _load(conn, post_id), e)


def _handle_error(conn, row, e: Exception) -> str:
    if isinstance(e, tt.AuthExpired):
        return _fail_attempt(conn, row, e, pause=f"TikTok auth: {e}. Run `auth` to reconnect.")
    if isinstance(e, tt.RateLimited):
        return _fail_attempt(conn, row, e, pause=f"TikTok rate/spam limit ({e.code}); "
                                                 "resume later.")
    if isinstance(e, tt.ApiError):
        _logger.error("post %s: %s", row["id"], e)
        return _fail_attempt(conn, row, e, retryable=e.retryable)
    _logger.error("post %s failed: %s", row["id"], e)
    return _fail_attempt(conn, row, e, retryable=False)
