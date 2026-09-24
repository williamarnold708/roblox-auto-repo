"""Tests for app.secrets / app.tiktok / app.publisher / app.analytics.
All HTTP is mocked; nothing touches the network or the OS keyring."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import analytics, config, db, publisher  # noqa: E402
from app import secrets as store  # noqa: E402
from app import tiktok as tt  # noqa: E402
from app.log import RedactFilter  # noqa: E402

SECRET_ACCESS = "act.SUPERSECRETaccess123"
SECRET_REFRESH = "rft.SUPERSECRETrefresh456"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPROMO_NO_KEYRING", "1")
    monkeypatch.setenv("AUTOPROMO_TOKEN_FILE", str(tmp_path / "analytics" / ".tokens.json"))
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck_test")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs_test_secret")
    settings = config.load(tmp_path)
    conn = db.connect(settings.path("database"))
    return settings, conn, tmp_path


def make_post(conn, tmp_path, mode="local", size=1024, duration=20.0, post_id=None) -> int:
    video = tmp_path / f"render_{mode}_{size}.mp4"
    video.write_bytes(os.urandom(size))
    now = db.now()
    g = conn.execute("INSERT INTO games(slug,name,created_at) VALUES(?,?,?)",
                     (f"g{time.time_ns()}", "Obby Rush", now)).lastrowid
    rec = conn.execute("INSERT INTO recordings(game_id,path,sha256,created_at) VALUES(?,?,?,?)",
                       (g, "x.mp4", str(time.time_ns()), now)).lastrowid
    clip = conn.execute("INSERT INTO clips(recording_id,start,end,created_at) VALUES(?,?,?,?)",
                        (rec, 0, 20, now)).lastrowid
    content = conn.execute(
        "INSERT INTO content(clip_id,hooks,caption,hashtags,chosen_hook,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (clip, json.dumps(["Nobody beats this jump", "Wait for it"]), "Can you beat it?",
         json.dumps(["roblox", "#obby"]), 0, now)).lastrowid
    render = conn.execute(
        "INSERT INTO renders(clip_id,content_id,path,duration,status,created_at) "
        "VALUES(?,?,?,?,?,?)", (clip, content, str(video), duration, "queued", now)).lastrowid
    pid = conn.execute("INSERT INTO posts(render_id,mode,status,created_at) VALUES(?,?,?,?)",
                       (render, mode, "scheduled", now)).lastrowid
    conn.commit()
    return pid


def store_token(expires_in=86400, scope="user.info.basic,video.upload,video.list"):
    store.set_json(tt.TOKEN_KEY, {
        "access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH, "open_id": "oid",
        "scope": scope, "expires_at": time.time() + expires_in,
        "refresh_expires_at": time.time() + 3600 * 24 * 300})


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def ok(data):
    return Resp(200, {"data": data, "error": {"code": "ok", "message": "", "log_id": "L"}})


def err(status, code, msg="nope"):
    return Resp(status, {"data": {}, "error": {"code": code, "message": msg, "log_id": "L"}})


class FakeHTTP:
    """Routes POST by URL path; records PUTs."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.puts = []

    def post(self, url, json=None, data=None, params=None, headers=None, timeout=None):
        assert timeout is not None, "every request must have a timeout"
        self.calls.append({"url": url, "json": json, "data": data, "headers": headers})
        for key, resp in self.routes.items():
            if url.endswith(key):
                r = resp(json or data) if callable(resp) else resp
                return r.pop(0) if isinstance(r, list) else r
        raise AssertionError(f"unexpected POST {url}")

    def put(self, url, data=None, headers=None, timeout=None):
        assert timeout is not None
        self.puts.append({"url": url, "len": len(data), "headers": headers})
        return Resp(201 if len(self.puts) else 206)


def client_with(settings, http):
    return tt.TikTokClient(settings, session=http)


# --------------------------------------------------------------------------- unit: chunking / pkce
def test_chunk_plan_rules():
    assert tt.chunk_plan(1000) == (1000, 1)                     # < 5MB: single chunk
    size = 23 * tt.MB + 17
    chunk, total = tt.chunk_plan(size, 10 * tt.MB)
    assert (chunk, total) == (10 * tt.MB, 2)                   # floor; last absorbs rest
    ranges = list(tt.chunk_ranges(size, chunk, total))
    assert ranges[0] == (0, chunk - 1) and ranges[-1][1] == size - 1
    assert ranges[-1][1] - ranges[-1][0] + 1 <= tt.MAX_FINAL_CHUNK
    assert tt.chunk_plan(6 * tt.MB, 1)[0] == tt.MIN_CHUNK       # clamps to >= 5MB


def test_authorize_url_has_pkce_and_state(env):
    settings, _, _ = env
    url, state, verifier = tt.TikTokClient(settings).build_authorize_url()
    assert url.startswith(tt.AUTH_URL)
    assert f"state={state}" in url and "code_challenge_method=S256" in url
    assert tt.code_challenge(verifier) in url and verifier not in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A" in url
    assert 43 <= len(verifier) <= 128


# --------------------------------------------------------------------------- local mode
def test_local_mode_writes_mp4_and_caption(env):
    settings, conn, tmp = env
    pid = make_post(conn, tmp, "local")
    assert publisher.publish(settings, conn, pid) == "ready_manual"
    ready = settings.path("queue") / "ready"
    mp4s = list(ready.glob(f"*_{pid}.mp4"))
    txts = list(ready.glob(f"*_{pid}.txt"))
    assert len(mp4s) == 1 and len(txts) == 1
    text = txts[0].read_text()
    assert "Can you beat it?" in text and "#roblox" in text and "#obby" in text
    row = conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "ready_manual"  # never 'published' for manual mode


# --------------------------------------------------------------------------- inbox mode
def test_inbox_happy_path_chunked_upload_awaits_user(env):
    settings, conn, tmp = env
    store_token()
    size = 21 * tt.MB + 5  # -> 2 chunks of 10MB, last absorbs the tail
    pid = make_post(conn, tmp, "inbox", size=size)
    init_body = {}

    def init(body):
        init_body.update(body)
        return ok({"publish_id": "v_inbox_1", "upload_url": "https://upload.example/u"})

    http = FakeHTTP({tt.INBOX_INIT: init,
                     tt.STATUS_FETCH: ok({"status": "SEND_TO_USER_INBOX"})})
    status = publisher.publish(settings, conn, pid, client=client_with(settings, http))
    assert status == "awaiting_user"
    src = init_body["source_info"]
    assert src["source"] == "FILE_UPLOAD" and src["video_size"] == size
    assert src["total_chunk_count"] == len(http.puts) == size // src["chunk_size"] == 2
    assert http.puts[0]["headers"]["Content-Range"] == f"bytes 0-{src['chunk_size'] - 1}/{size}"
    assert http.puts[-1]["headers"]["Content-Range"].endswith(f"-{size - 1}/{size}")
    assert sum(p["len"] for p in http.puts) == size
    assert all(p["headers"]["Content-Type"] == "video/mp4" for p in http.puts)
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET_ACCESS}"
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    assert row["publish_id"] == "v_inbox_1" and row["status"] == "awaiting_user"


def test_not_published_without_confirmation_and_no_reupload(env):
    settings, conn, tmp = env
    store_token()
    pid = make_post(conn, tmp, "inbox")
    statuses = [ok({"status": "PROCESSING_UPLOAD"}), ok({"status": "SEND_TO_USER_INBOX"}),
                ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7312345]})]
    http = FakeHTTP({tt.INBOX_INIT: ok({"publish_id": "p1", "upload_url": "https://u"}),
                     tt.STATUS_FETCH: statuses})
    c = client_with(settings, http)
    assert publisher.publish(settings, conn, pid, client=c) == "awaiting_user"
    # Second call must NOT init/upload again; only status checks.
    assert publisher.publish(settings, conn, pid, client=c) == "awaiting_user"
    inits = [x for x in http.calls if x["url"].endswith(tt.INBOX_INIT)]
    assert len(inits) == 1 and len(http.puts) == 1
    assert conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0] != "published"
    # Only an explicit PUBLISH_COMPLETE publishes.
    assert publisher.refresh_status(settings, conn, pid, client=c) == "published"
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    assert row["video_id"] == "7312345"
    assert conn.execute("SELECT status FROM renders WHERE id=?",
                        (row["render_id"],)).fetchone()[0] == "posted"


def test_failed_status_marks_failed(env):
    settings, conn, tmp = env
    store_token()
    pid = make_post(conn, tmp, "inbox")
    http = FakeHTTP({tt.INBOX_INIT: ok({"publish_id": "p2", "upload_url": "https://u"}),
                     tt.STATUS_FETCH: ok({"status": "FAILED", "fail_reason": "file_format_check_failed"})})
    assert publisher.publish(settings, conn, pid, client=client_with(settings, http)) == "failed"


# --------------------------------------------------------------------------- errors
def test_auth_expiry_pauses_publishing(env):
    settings, conn, tmp = env
    store_token(expires_in=-10)  # access token expired -> refresh attempted
    pid = make_post(conn, tmp, "inbox")
    http = FakeHTTP({tt.TOKEN_URL: Resp(400, {"error": "invalid_grant",
                                              "error_description": "refresh token revoked"})})
    status = publisher.publish(settings, conn, pid, client=client_with(settings, http))
    assert status == "scheduled"
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    assert row["attempts"] == 1 and "invalid_grant" in row["error"]
    assert row["publish_id"] is None
    assert db.get_state(conn, "publishing_paused") == "1"
    assert "auth" in db.get_state(conn, "publishing_paused_reason").lower()


def test_access_token_refreshed_transparently(env):
    settings, conn, tmp = env
    store_token(expires_in=-10)
    http = FakeHTTP({tt.TOKEN_URL: Resp(200, {"access_token": "act.NEW", "refresh_token": "rft.NEW",
                                              "expires_in": 86400, "refresh_expires_in": 31536000,
                                              "scope": "video.upload", "open_id": "oid"}),
                     tt.CREATOR_INFO: ok({"privacy_level_options": ["SELF_ONLY"]})})
    c = client_with(settings, http)
    assert c.creator_info()["privacy_level_options"] == ["SELF_ONLY"]
    assert http.calls[-1]["headers"]["Authorization"] == "Bearer act.NEW"
    assert http.calls[0]["data"]["grant_type"] == "refresh_token"


def test_rate_limit_pauses_publishing(env):
    settings, conn, tmp = env
    store_token()
    pid = make_post(conn, tmp, "inbox")
    http = FakeHTTP({tt.INBOX_INIT: err(403, "spam_risk_too_many_pending_share")})
    assert publisher.publish(settings, conn, pid, client=client_with(settings, http)) == "scheduled"
    assert db.get_state(conn, "publishing_paused") == "1"
    assert conn.execute("SELECT attempts FROM posts WHERE id=?", (pid,)).fetchone()[0] == 1


def test_server_error_retries_then_fails(env):
    settings, conn, tmp = env
    store_token()
    pid = make_post(conn, tmp, "inbox")
    http = FakeHTTP({tt.INBOX_INIT: err(503, "internal_error")})
    c = client_with(settings, http)
    assert publisher.publish(settings, conn, pid, client=c) == "scheduled"
    assert db.get_state(conn, "publishing_paused") is None
    publisher.publish(settings, conn, pid, client=c)
    assert publisher.publish(settings, conn, pid, client=c) == "failed"


# --------------------------------------------------------------------------- direct mode
def test_direct_refused_when_unaudited(env):
    settings, conn, tmp = env
    store_token(scope="video.publish,video.upload")
    pid = make_post(conn, tmp, "direct")
    db.set_state(conn, f"approved:{pid}", json.dumps({"privacy_level": "PUBLIC_TO_EVERYONE"}))
    http = FakeHTTP({})  # any HTTP call would raise
    assert publisher.publish(settings, conn, pid, client=client_with(settings, http)) == "failed"
    row = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    assert "not marked as audited" in row["error"] and not http.calls and not http.puts


def test_direct_requires_approval_and_valid_privacy(env):
    settings, conn, tmp = env
    store_token(scope="video.publish,video.upload")
    db.set_state(conn, "tiktok_app_audited", "1")
    pid = make_post(conn, tmp, "direct", duration=20)
    info = ok({"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
               "comment_disabled": True, "duet_disabled": False, "stitch_disabled": False,
               "max_video_post_duration_sec": 600})
    body = {}

    def init(b):
        body.update(b)
        return ok({"publish_id": "d1", "upload_url": "https://u"})

    http = FakeHTTP({tt.CREATOR_INFO: info, tt.DIRECT_INIT: init,
                     tt.STATUS_FETCH: ok({"status": "PROCESSING_UPLOAD"})})
    c = client_with(settings, http)
    # No approval -> stays scheduled, nothing uploaded.
    assert publisher.publish(settings, conn, pid, client=c) == "scheduled"
    assert not http.puts
    # Approved, but without a privacy choice -> refused (no silent default).
    db.set_state(conn, f"approved:{pid}", "1")
    assert publisher.publish(settings, conn, pid, client=c) == "scheduled"
    assert "no default" in conn.execute("SELECT error FROM posts WHERE id=?", (pid,)).fetchone()[0]
    # Privacy not in creator options -> refused.
    db.set_state(conn, f"approved:{pid}", json.dumps({"privacy_level": "FOLLOWER_OF_CREATOR"}))
    assert publisher.publish(settings, conn, pid, client=c) == "scheduled"
    # Valid choice -> uploads; comment forced off because creator disabled it.
    db.set_state(conn, f"approved:{pid}",
                 json.dumps({"privacy_level": "SELF_ONLY", "is_aigc": True}))
    assert publisher.publish(settings, conn, pid, client=c) == "uploading"
    pi = body["post_info"]
    assert pi["privacy_level"] == "SELF_ONLY" and pi["disable_comment"] is True
    assert pi["is_aigc"] is True and pi["brand_content_toggle"] is False
    assert conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0] != "published"


def test_direct_refuses_too_long(env):
    settings, conn, tmp = env
    store_token(scope="video.publish")
    db.set_state(conn, "tiktok_app_audited", "1")
    pid = make_post(conn, tmp, "direct", duration=90)
    db.set_state(conn, f"approved:{pid}", json.dumps({"privacy_level": "SELF_ONLY"}))
    http = FakeHTTP({tt.CREATOR_INFO: ok({"privacy_level_options": ["SELF_ONLY"],
                                          "max_video_post_duration_sec": 60})})
    publisher.publish(settings, conn, pid, client=client_with(settings, http))
    assert "max is 60" in conn.execute("SELECT error FROM posts WHERE id=?", (pid,)).fetchone()[0]
    assert not http.puts


# --------------------------------------------------------------------------- secrets / logging
def test_token_never_in_logs(env, caplog):
    settings, conn, tmp = env
    store_token(expires_in=-10)
    pid = make_post(conn, tmp, "inbox")
    http = FakeHTTP({
        tt.TOKEN_URL: Resp(200, {"access_token": SECRET_ACCESS + "X", "refresh_token": SECRET_REFRESH,
                                 "expires_in": 86400, "refresh_expires_in": 31536000,
                                 "scope": "video.upload", "open_id": "oid"}),
        tt.INBOX_INIT: ok({"publish_id": "p9", "upload_url": "https://u"}),
        tt.STATUS_FETCH: ok({"status": "SEND_TO_USER_INBOX"})})
    with caplog.at_level(logging.DEBUG):
        publisher.publish(settings, conn, pid, client=client_with(settings, http))
    text = caplog.text + json.dumps([dict(r) for r in conn.execute("SELECT * FROM posts")])
    assert SECRET_ACCESS not in text and SECRET_REFRESH not in text
    assert "cs_test_secret" not in text
    # The shared redaction filter is a second line of defence for inline leaks.
    rec = logging.LogRecord("x", logging.INFO, "", 0, f"access_token={SECRET_ACCESS}", None, None)
    RedactFilter().filter(rec)
    assert SECRET_ACCESS not in rec.getMessage()


def test_token_file_permissions(env):
    store.set_secret("demo", "value")
    path = store.token_file()
    assert path.exists() and store.get_secret("demo") == "value"
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    store.set_secret("demo", None)
    assert store.get_secret("demo") is None


# --------------------------------------------------------------------------- analytics
def test_manual_metrics_and_summaries(env):
    settings, conn, tmp = env
    pid = make_post(conn, tmp, "local")
    analytics.add_manual_metrics(conn, pid, views=1200, likes=80, roblox_visits=15,
                                 completion_rate=42)
    row = conn.execute("SELECT * FROM metrics WHERE post_id=?", (pid,)).fetchone()
    assert row["source"] == "manual" and row["views"] == 1200
    assert row["completion_rate"] == pytest.approx(0.42)
    assert row["shares"] is None and row["avg_watch_s"] is None   # never estimated
    with pytest.raises(ValueError):
        analytics.add_manual_metrics(conn, pid, bogus=1)
    with pytest.raises(ValueError):
        analytics.add_manual_metrics(conn, pid, views=-1)
    t = analytics.totals(conn)
    assert t["views"] == 1200 and t["roblox_visits"] == 15
    assert analytics.best_hooks(conn)[0]["hook"] == "Nobody beats this jump"
    assert analytics.best_games(conn)[0]["name"] == "Obby Rush"
    assert analytics.views_per_video(conn)[0]["views"] == 1200
    assert analytics.time_series(conn)[0]["views"] == 1200
    assert len(analytics.time_series(conn, pid)) == 1


def test_fetch_api_metrics(env):
    settings, conn, tmp = env
    store_token()
    pid = make_post(conn, tmp, "inbox")
    conn.execute("UPDATE posts SET video_id='7312345', status='published' WHERE id=?", (pid,))
    conn.commit()
    http = FakeHTTP({tt.VIDEO_QUERY: ok({"videos": [
        {"id": "7312345", "view_count": 500, "like_count": 20, "comment_count": 3,
         "share_count": 1, "share_url": "https://www.tiktok.com/@x/video/7312345"}]})})
    assert analytics.fetch_api_metrics(settings, conn, client=client_with(settings, http)) == 1
    m = conn.execute("SELECT * FROM metrics WHERE post_id=?", (pid,)).fetchone()
    assert m["source"] == "api" and m["views"] == 500 and m["avg_watch_s"] is None
    assert m["completion_rate"] is None
    assert http.calls[0]["json"] == {"filters": {"video_ids": ["7312345"]}}
