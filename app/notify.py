"""User notifications.

Policy: only notify when the user has to *do* something, plus the daily
summary. Every notification is appended to logs/NOTIFICATIONS.md and the log
(the dashboard shows them too). Identical notifications are sent at most once
per local day.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db
from .log import get

log = get("notify")


def _tz(settings) -> ZoneInfo:
    return ZoneInfo(settings.section("schedule").get("timezone", "UTC"))


def notify(settings, conn, title: str, message: str, *, category: str = "action",
           now: datetime | None = None, key: str | None = None) -> bool:
    """Record a notification. Returns False if it was a duplicate today.

    `key` overrides what counts as "identical" (default: category+title+message)."""
    local = (now or datetime.now(_tz(settings))).astimezone(_tz(settings))
    day = local.date().isoformat()
    ident = key if key is not None else f"{category}|{title}|{message}"
    digest = hashlib.sha1(ident.encode()).hexdigest()[:16]
    key = f"notified:{day}:{digest}"
    if db.get_state(conn, key) == "1":
        return False
    db.set_state(conn, key, "1")

    path = settings.path("logs") / "NOTIFICATIONS.md"
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# Notifications\n\nNewest at the bottom. Only things that need you, "
                    "plus the daily summary.\n\n")
        f.write(f"- {local:%Y-%m-%d %H:%M} [{category}] **{title}** — "
                f"{message.replace(chr(10), ' ')}\n")
    log.info("NOTIFY [%s] %s: %s", category, title, message)
    return True


if __name__ == "__main__":  # manual test: python -m app.notify "title" "message"
    from . import config
    s = config.load()
    c = db.connect(s.path("database"))
    print(notify(s, c, sys.argv[1] if len(sys.argv) > 1 else "Test",
                 sys.argv[2] if len(sys.argv) > 2 else "Hello from RobloxAutoPromo",
                 category="test"))
