"""Rotating file logging with token redaction."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET = re.compile(r"(access_token|refresh_token|client_secret|Authorization)([\"'=:\s]+)(Bearer\s+)?[\w\-\.~+/]+", re.I)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _SECRET.sub(r"\1\2\3***", str(record.msg))
        if record.args:
            record.args = tuple(_SECRET.sub(r"\1\2\3***", str(a)) for a in record.args)
        return True


def setup(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log = logging.getLogger("autopromo")
    if log.handlers:
        return log
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(log_dir / "autopromo.log", maxBytes=2_000_000, backupCount=5)
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        log.addHandler(h)
    return log


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"autopromo.{name}")
