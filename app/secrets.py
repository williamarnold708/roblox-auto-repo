"""Secret storage for TikTok credentials and OAuth tokens.

Order of preference:
  1. OS keyring (Windows Credential Manager / macOS Keychain / Secret Service)
     when the optional `keyring` package is importable.
  2. Environment variables (read-only; e.g. TIKTOK_CLIENT_KEY).
  3. A JSON token file at analytics/.tokens.json with 0600 permissions
     (a warning is logged once, since this is plaintext on disk).

Secret *values* are never logged -- only key names.
"""
from __future__ import annotations

import json
import os
import stat
import warnings
from pathlib import Path

from . import log as _log

SERVICE = "RobloxAutoPromo"
TOKEN_FILE_NAME = ".tokens.json"
_logger = _log.get("secrets")
_warned = False

try:  # optional dependency
    import keyring as _keyring  # type: ignore
except Exception:  # pragma: no cover - depends on environment
    _keyring = None


def _use_keyring() -> bool:
    return _keyring is not None and os.environ.get("AUTOPROMO_NO_KEYRING") != "1"


def token_file(root: Path | None = None) -> Path:
    if os.environ.get("AUTOPROMO_TOKEN_FILE"):
        return Path(os.environ["AUTOPROMO_TOKEN_FILE"])
    from .config import ROOT
    return Path(root or ROOT) / "analytics" / TOKEN_FILE_NAME


def _warn_plaintext(path: Path) -> None:
    global _warned
    if not _warned:
        _warned = True
        msg = (f"'keyring' not available: storing secrets in plaintext file {path} "
               "(permissions 0600). Install 'keyring' for OS credential storage.")
        _logger.warning(msg)
        warnings.warn(msg, RuntimeWarning, stacklevel=3)


def _read_file(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _logger.error("token file %s unreadable; ignoring", path)
        return {}


def _write_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - e.g. some Windows filesystems
        pass
    os.replace(tmp, path)


def get_secret(name: str, root: Path | None = None) -> str | None:
    """Return a secret by name, or None. Env var NAME (upper-case) wins."""
    env = os.environ.get(name.upper())
    if env:
        return env
    if _use_keyring():
        try:
            val = _keyring.get_password(SERVICE, name)
            if val is not None:
                return val
        except Exception as e:  # keyring backend failure -> fall back
            _logger.warning("keyring read failed for %s: %s", name, type(e).__name__)
    return _read_file(token_file(root)).get(name)


def set_secret(name: str, value: str | None, root: Path | None = None) -> None:
    """Store (or delete, when value is None) a secret."""
    if _use_keyring():
        try:
            if value is None:
                try:
                    _keyring.delete_password(SERVICE, name)
                except Exception:
                    pass
            else:
                _keyring.set_password(SERVICE, name, value)
            _logger.info("secret %s stored in OS keyring", name)
            return
        except Exception as e:
            _logger.warning("keyring write failed for %s (%s); using token file",
                            name, type(e).__name__)
    path = token_file(root)
    _warn_plaintext(path)
    data = _read_file(path)
    if value is None:
        data.pop(name, None)
    else:
        data[name] = value
    _write_file(path, data)
    _logger.info("secret %s stored in token file", name)


def get_json(name: str, root: Path | None = None) -> dict | None:
    raw = get_secret(name, root)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def set_json(name: str, value: dict | None, root: Path | None = None) -> None:
    set_secret(name, None if value is None else json.dumps(value), root)


def client_credentials() -> tuple[str | None, str | None]:
    """TikTok app credentials from env (TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET),
    falling back to the secret store."""
    return (os.environ.get("TIKTOK_CLIENT_KEY") or get_secret("tiktok_client_key"),
            os.environ.get("TIKTOK_CLIENT_SECRET") or get_secret("tiktok_client_secret"))


def mask(value: str | None) -> str:
    """Safe representation of a secret for UI/logs."""
    if not value:
        return "<unset>"
    return f"***({len(value)} chars)"
