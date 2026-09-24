"""User notifications.

Policy: only notify when the user has to *do* something, plus the daily
summary. Every notification is appended to logs/NOTIFICATIONS.md; on Windows a
toast is shown too (BurntToast module if installed, otherwise the built-in
Windows.UI.Notifications API via PowerShell). Identical notifications are sent
at most once per local day.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db
from .log import get

log = get("notify")


def _tz(settings) -> ZoneInfo:
    return ZoneInfo(settings.section("schedule").get("timezone", "UTC"))


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _toast_script(title: str, message: str) -> str:
    t, m = _ps_quote(title[:120]), _ps_quote(message[:400])
    return f"""
$ErrorActionPreference = 'SilentlyContinue'
if (Get-Module -ListAvailable -Name BurntToast) {{
  Import-Module BurntToast
  New-BurntToastNotification -Text {t}, {m}
  exit 0
}}
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $tpl.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($tpl.CreateTextNode({t})) | Out-Null
$nodes.Item(1).AppendChild($tpl.CreateTextNode({m})) | Out-Null
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($tpl))
"""


def _toast(title: str, message: str) -> bool:
    if os.name != "nt":
        return False
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", _toast_script(title, message)],
            timeout=20, capture_output=True, creationflags=flags, check=False)
        return True
    except Exception as e:  # never let a toast failure break the pipeline
        log.warning("toast failed: %s", e)
        return False


def notify(settings, conn, title: str, message: str, *, category: str = "action",
           now: datetime | None = None, key: str | None = None) -> bool:
    """Record (and toast) a notification. Returns False if it was a duplicate today.

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
    shown = _toast(title, message)
    log.info("NOTIFY [%s] %s: %s%s", category, title, message, "" if shown else " (log only)")
    return True


if __name__ == "__main__":  # manual test: python -m app.notify "title" "message"
    from . import config
    s = config.load()
    c = db.connect(s.path("database"))
    print(notify(s, c, sys.argv[1] if len(sys.argv) > 1 else "Test",
                 sys.argv[2] if len(sys.argv) > 2 else "Hello from RobloxAutoPromo",
                 category="test"))
