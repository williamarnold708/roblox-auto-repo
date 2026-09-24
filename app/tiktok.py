"""Thin client for TikTok's *official* v2 Open API (Login Kit + Content Posting
API + Display API video.query). See docs/TIKTOK_API.md for the verified facts
behind every constant here.

No browser automation, no cookies, no unofficial endpoints: the user signs in
on tiktok.com in their own browser, TikTok redirects back to a one-shot local
listener, and tokens are stored via app.secrets (never logged).
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import math
import os
import secrets as pysecrets  # stdlib (absolute import; app/secrets.py is app.secrets)
import string
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from . import log as _log
from . import secrets as store

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_BASE = "https://open.tiktokapis.com"
TOKEN_URL = API_BASE + "/v2/oauth/token/"
REVOKE_URL = API_BASE + "/v2/oauth/revoke/"
CREATOR_INFO = "/v2/post/publish/creator_info/query/"
DIRECT_INIT = "/v2/post/publish/video/init/"
INBOX_INIT = "/v2/post/publish/inbox/video/init/"
STATUS_FETCH = "/v2/post/publish/status/fetch/"
VIDEO_QUERY = "/v2/video/query/"

DEFAULT_SCOPES = ("user.info.basic", "video.upload", "video.list")
DIRECT_SCOPES = ("user.info.basic", "video.upload", "video.publish", "video.list")
DEFAULT_PORT = 8765
TIMEOUT = (10, 60)            # (connect, read) seconds for API calls
UPLOAD_TIMEOUT = (10, 300)    # chunk PUTs can be slow on home uplinks

MB = 1024 * 1024
MIN_CHUNK = 5 * MB            # every chunk except the last >= 5 MB
MAX_CHUNK = 64 * MB           # every chunk except the last <= 64 MB
MAX_FINAL_CHUNK = 128 * MB    # final chunk may absorb trailing bytes up to 128 MB
MAX_CHUNKS = 1000
DEFAULT_CHUNK = 10 * MB

TOKEN_KEY = "tiktok_token"
VIDEO_METRIC_FIELDS = ("id", "create_time", "share_url", "duration", "title",
                       "view_count", "like_count", "comment_count", "share_count")

_logger = _log.get("tiktok")

_AUTH_ERRORS = {"access_token_invalid", "scope_not_authorized", "invalid_grant",
                "token_expired", "invalid_token", "scope_permission_missed"}
_RATE_ERRORS = {"rate_limit_exceeded", "spam_risk_too_many_posts",
                "spam_risk_too_many_pending_share", "spam_risk_user_banned_from_posting",
                "spam_risk"}


# --------------------------------------------------------------------------- errors
class TikTokError(Exception):
    """Base class for all TikTok client errors."""


class AuthExpired(TikTokError):
    """Tokens missing/expired/revoked, or a required scope was not granted.
    The user must run `autopromo auth` again."""


class RateLimited(TikTokError):
    """HTTP 429 / rate_limit_exceeded / spam_risk_* (e.g. too many pending
    inbox shares or daily post cap). Back off; do not hammer the API."""

    def __init__(self, message: str, code: str = "rate_limit_exceeded"):
        super().__init__(message)
        self.code = code


class ApiError(TikTokError):
    def __init__(self, code: str, message: str = "", http_status: int | None = None,
                 log_id: str | None = None):
        super().__init__(f"TikTok API error {code} (HTTP {http_status}): {message}"
                         + (f" [log_id={log_id}]" if log_id else ""))
        self.code = code
        self.http_status = http_status
        self.log_id = log_id

    @property
    def retryable(self) -> bool:
        return self.http_status is None or self.http_status >= 500


# --------------------------------------------------------------------------- PKCE
_VERIFIER_ALPHABET = string.ascii_letters + string.digits + "-._~"


def make_code_verifier(length: int = 64) -> str:
    if not 43 <= length <= 128:
        raise ValueError("PKCE code_verifier must be 43..128 chars")
    return "".join(pysecrets.choice(_VERIFIER_ALPHABET) for _ in range(length))


def code_challenge(verifier: str, encoding: str = "hex") -> str:
    """S256 challenge. TikTok's *desktop* Login Kit docs specify the SHA-256
    digest hex-encoded (not RFC 7636 base64url); set
    [publish].pkce_encoding = "base64url" if TikTok ever changes this."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    if encoding == "base64url":
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return digest.hex()


# --------------------------------------------------------------------------- chunking
def chunk_plan(video_size: int, preferred: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) per TikTok's media transfer rules:
    files < 5 MB go as a single chunk (chunk_size == video_size); otherwise
    chunks are 5..64 MB, total = floor(size / chunk_size), and the final chunk
    absorbs the trailing bytes (it may be up to 128 MB)."""
    if video_size <= 0:
        raise ValueError("empty video file")
    if video_size < MIN_CHUNK:
        return video_size, 1
    chunk = max(MIN_CHUNK, min(MAX_CHUNK, preferred))
    if video_size // chunk > MAX_CHUNKS:
        chunk = math.ceil(video_size / MAX_CHUNKS)
        if chunk > MAX_CHUNK:
            raise ValueError("video too large for chunked upload")
    total = max(1, video_size // chunk)
    if video_size - chunk * (total - 1) > MAX_FINAL_CHUNK:  # pragma: no cover - defensive
        raise ValueError("final chunk would exceed 128 MB")
    return chunk, total


def chunk_ranges(video_size: int, chunk_size: int, total: int):
    """Yield (first_byte, last_byte) inclusive for each chunk."""
    for i in range(total):
        start = i * chunk_size
        end = video_size - 1 if i == total - 1 else start + chunk_size - 1
        yield start, end


# --------------------------------------------------------------------------- client
class TikTokClient:
    def __init__(self, settings=None, session: requests.Session | None = None,
                 root: Path | None = None):
        self.settings = settings
        self.cfg = settings.section("publish") if settings is not None else {}
        self.root = root or (settings.root if settings is not None else None)
        self.http = session or requests.Session()
        self.client_key, self.client_secret = store.client_credentials()

    # ----- config helpers
    @property
    def port(self) -> int:
        return int(os.environ.get("TIKTOK_REDIRECT_PORT") or self.cfg.get("redirect_port", DEFAULT_PORT))

    @property
    def redirect_uri(self) -> str:
        return os.environ.get("TIKTOK_REDIRECT_URI") or f"http://localhost:{self.port}/callback"

    def scopes(self) -> list[str]:
        if self.cfg.get("scopes"):
            return list(self.cfg["scopes"])
        mode = self.cfg.get("mode", "local")
        return list(DIRECT_SCOPES if mode == "direct" else DEFAULT_SCOPES)

    def _require_client(self) -> None:
        if not self.client_key or not self.client_secret:
            raise AuthExpired("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET are not set "
                              "(see .env.example)")

    # ----- token storage
    def _load_token(self) -> dict | None:
        return store.get_json(TOKEN_KEY, self.root)

    def _save_token(self, payload: dict) -> dict:
        now = time.time()
        tok = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token"),
            "open_id": payload.get("open_id"),
            "scope": payload.get("scope", ""),
            "expires_at": now + int(payload.get("expires_in", 86400)),
            "refresh_expires_at": now + int(payload.get("refresh_expires_in", 31536000)),
        }
        store.set_json(TOKEN_KEY, tok, self.root)
        _logger.info("TikTok token stored (scopes: %s)", tok["scope"])
        return tok

    def has_token(self) -> bool:
        return bool(self._load_token())

    def granted_scopes(self) -> set[str]:
        tok = self._load_token() or {}
        return {s.strip() for s in str(tok.get("scope", "")).split(",") if s.strip()}

    def logout(self) -> None:
        store.set_json(TOKEN_KEY, None, self.root)

    # ----- OAuth
    def build_authorize_url(self, state: str | None = None, verifier: str | None = None,
                            scopes: list[str] | None = None) -> tuple[str, str, str]:
        """Return (url, state, code_verifier)."""
        self._require_client()
        state = state or pysecrets.token_urlsafe(24)
        verifier = verifier or make_code_verifier()
        params = {
            "client_key": self.client_key,
            "response_type": "code",
            "scope": ",".join(scopes or self.scopes()),
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": code_challenge(verifier, self.cfg.get("pkce_encoding", "hex")),
            "code_challenge_method": "S256",
        }
        return AUTH_URL + "?" + urllib.parse.urlencode(params), state, verifier

    def _token_request(self, data: dict) -> dict:
        self._require_client()
        data = {"client_key": self.client_key, "client_secret": self.client_secret, **data}
        try:
            r = self.http.post(TOKEN_URL, data=data, timeout=TIMEOUT,
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "Cache-Control": "no-cache"})
        except requests.RequestException as e:
            raise ApiError("network_error", type(e).__name__) from None
        if r.status_code == 429:
            raise RateLimited("token endpoint rate limited")
        try:
            body = r.json()
        except ValueError:
            raise ApiError("bad_response", "non-JSON token response", r.status_code) from None
        if "access_token" not in body:
            code = str(body.get("error") or "token_error")
            desc = str(body.get("error_description", ""))
            if r.status_code >= 500:
                raise ApiError(code, desc, r.status_code, body.get("log_id"))
            raise AuthExpired(f"token request rejected: {code} {desc}".strip())
        return body

    def exchange_code(self, code: str, verifier: str) -> dict:
        body = self._token_request({"code": code, "grant_type": "authorization_code",
                                    "redirect_uri": self.redirect_uri,
                                    "code_verifier": verifier})
        return self._save_token(body)

    def refresh(self) -> dict:
        tok = self._load_token()
        if not tok or not tok.get("refresh_token"):
            raise AuthExpired("no TikTok refresh token; run `auth`")
        if tok.get("refresh_expires_at", 0) <= time.time():
            raise AuthExpired("TikTok refresh token expired (365 days); run `auth`")
        body = self._token_request({"grant_type": "refresh_token",
                                    "refresh_token": tok["refresh_token"]})
        return self._save_token(body)

    def access_token(self) -> str:
        tok = self._load_token()
        if not tok:
            raise AuthExpired("not connected to TikTok; run `auth`")
        if tok.get("expires_at", 0) - 120 <= time.time():
            tok = self.refresh()
        return tok["access_token"]

    # ----- core request
    def _api(self, path: str, json_body: dict | None = None, params: dict | None = None,
             _retry_auth: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token()}",
                   "Content-Type": "application/json; charset=UTF-8"}
        try:
            r = self.http.post(API_BASE + path, json=json_body or {}, params=params,
                               headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ApiError("network_error", type(e).__name__) from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        err = body.get("error") or {}
        code = str(err.get("code") or ("ok" if r.status_code < 400 else f"http_{r.status_code}"))
        msg = str(err.get("message", ""))
        log_id = err.get("log_id")
        if code == "ok" and r.status_code < 400:
            return body.get("data") or {}
        if r.status_code == 401 or code in _AUTH_ERRORS:
            if _retry_auth and code != "scope_not_authorized":
                try:
                    self.refresh()
                except AuthExpired:
                    raise
                return self._api(path, json_body, params, _retry_auth=False)
            raise AuthExpired(f"{code}: {msg}")
        if r.status_code == 429 or code in _RATE_ERRORS or code.startswith("spam_risk"):
            raise RateLimited(f"{code}: {msg}", code)
        raise ApiError(code, msg, r.status_code, log_id)

    # ----- Content Posting API
    def creator_info(self) -> dict:
        """privacy_level_options, comment/duet/stitch_disabled,
        max_video_post_duration_sec, creator_username, creator_nickname..."""
        return self._api(CREATOR_INFO)

    @staticmethod
    def _source_info(video_path: Path | str, preferred_chunk: int = DEFAULT_CHUNK) -> dict:
        size = Path(video_path).stat().st_size
        chunk, total = chunk_plan(size, preferred_chunk)
        return {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                "total_chunk_count": total}

    def _chunk_pref(self) -> int:
        return int(float(self.cfg.get("chunk_size_mb", DEFAULT_CHUNK / MB)) * MB)

    def init_inbox_upload(self, video_path: Path | str) -> dict:
        """video.upload scope: sends the video to the creator's TikTok inbox;
        the creator finishes editing/posting in the app. Returns
        {publish_id, upload_url, source_info}."""
        src = self._source_info(video_path, self._chunk_pref())
        data = self._api(INBOX_INIT, {"source_info": src})
        return {**data, "source_info": src}

    def init_direct_post(self, video_path: Path | str, caption: str, privacy_level: str,
                         disable_comment: bool = False, disable_duet: bool = False,
                         disable_stitch: bool = False, is_aigc: bool = False,
                         brand_content_toggle: bool = False,
                         brand_organic_toggle: bool = False,
                         video_cover_timestamp_ms: int | None = None) -> dict:
        """video.publish scope. Caller MUST have queried creator_info and got
        explicit user choices first (see app/publisher.py)."""
        if not privacy_level:
            raise ValueError("privacy_level must be chosen by the user (no default)")
        post_info = {
            "title": caption[:2200],
            "privacy_level": privacy_level,
            "disable_comment": bool(disable_comment),
            "disable_duet": bool(disable_duet),
            "disable_stitch": bool(disable_stitch),
            "brand_content_toggle": bool(brand_content_toggle),
            "brand_organic_toggle": bool(brand_organic_toggle),
            "is_aigc": bool(is_aigc),
        }
        if video_cover_timestamp_ms is not None:
            post_info["video_cover_timestamp_ms"] = int(video_cover_timestamp_ms)
        src = self._source_info(video_path, self._chunk_pref())
        data = self._api(DIRECT_INIT, {"post_info": post_info, "source_info": src})
        return {**data, "source_info": src}

    def upload_chunks(self, upload_url: str, video_path: Path | str, chunk_size: int,
                      total: int, mime: str = "video/mp4") -> None:
        """PUT each chunk with Content-Range: bytes first-last/total."""
        path = Path(video_path)
        size = path.stat().st_size
        with open(path, "rb") as f:
            for i, (start, end) in enumerate(chunk_ranges(size, chunk_size, total), 1):
                f.seek(start)
                blob = f.read(end - start + 1)
                headers = {"Content-Type": mime, "Content-Length": str(len(blob)),
                           "Content-Range": f"bytes {start}-{end}/{size}"}
                try:
                    r = self.http.put(upload_url, data=blob, headers=headers,
                                      timeout=UPLOAD_TIMEOUT)
                except requests.RequestException as e:
                    raise ApiError("upload_network_error", type(e).__name__) from None
                if r.status_code == 429:
                    raise RateLimited("upload rate limited")
                if r.status_code not in (200, 201, 206):
                    raise ApiError("upload_failed", f"chunk {i}/{total}", r.status_code)
                _logger.info("uploaded chunk %d/%d (%d bytes)", i, total, len(blob))

    def fetch_status(self, publish_id: str) -> dict:
        """{status: PROCESSING_UPLOAD|PROCESSING_DOWNLOAD|SEND_TO_USER_INBOX|
        PUBLISH_COMPLETE|FAILED, fail_reason, publicaly_available_post_id[], ...}"""
        return self._api(STATUS_FETCH, {"publish_id": publish_id})

    # ----- Display API
    def list_videos(self, video_ids: list[str],
                    fields: tuple[str, ...] = VIDEO_METRIC_FIELDS) -> list[dict]:
        """video.list scope; /v2/video/query/ accepts up to 20 ids per call and
        only returns videos owned by the authorised user."""
        out: list[dict] = []
        ids = [str(v) for v in video_ids if v]
        for i in range(0, len(ids), 20):
            data = self._api(VIDEO_QUERY, {"filters": {"video_ids": ids[i:i + 20]}},
                             params={"fields": ",".join(fields)})
            out.extend(data.get("videos") or [])
        return out


# --------------------------------------------------------------------------- interactive auth
class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        type(self).result.update(q)
        ok = "code" in q and "error" not in q
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("TikTok connected. You can close this tab and return to RobloxAutoPromo."
               if ok else "TikTok authorisation failed or was cancelled. You can close this tab.")
        self.wfile.write(f"<html><body><p>{msg}</p></body></html>".encode())

    def log_message(self, *args):  # silence: the query string contains the code
        pass


def authorize_interactive(settings, open_browser: bool = True, timeout: float = 300,
                          client: TikTokClient | None = None) -> dict:
    """Run the Login Kit OAuth v2 + PKCE flow against a one-shot local listener
    at http://localhost:<port>/callback (this exact URI must be registered in
    the TikTok developer portal). Returns a safe summary (no tokens)."""
    client = client or TikTokClient(settings)
    url, state, verifier = client.build_authorize_url()
    handler = type("Handler", (_CallbackHandler,), {"result": {}})
    server = http.server.HTTPServer(("127.0.0.1", client.port), handler)
    server.timeout = timeout
    print("Open this URL to connect TikTok (it should open automatically):\n" + url)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        deadline = time.time() + timeout
        while "code" not in handler.result and "error" not in handler.result:
            if time.time() > deadline:
                raise AuthExpired("timed out waiting for TikTok redirect")
            server.handle_request()  # single request per loop; 404s for favicon etc.
    finally:
        server.server_close()
    res = handler.result
    if res.get("state") != state:
        raise AuthExpired("OAuth state mismatch - possible CSRF; aborting")
    if "error" in res:
        raise AuthExpired(f"authorisation denied: {res.get('error')} "
                          f"{res.get('error_description', '')}".strip())
    tok = client.exchange_code(res["code"], verifier)
    summary = {"open_id": tok.get("open_id"), "scope": tok.get("scope"),
               "expires_at": tok.get("expires_at")}
    _logger.info("TikTok authorised; scopes=%s", summary["scope"])
    return summary
