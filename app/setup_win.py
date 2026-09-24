"""One-time setup, written in Python so the download contains no .ps1/.bat
files (antivirus heuristics flag script launchers that install things).

    python -m app setup            # check tools, install packages, demo, game.json, inbox, auto-start
    python -m app autostart on|off # start the service at logon (no admin needed)

Auto-start puts a small .pyw launcher in the per-user Startup folder; Windows
runs .pyw files with pythonw, so no console window appears.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

LAUNCHER_NAME = "RobloxAutoPromo.pyw"


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "Microsoft/Windows/Start Menu/Programs/Startup"


def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def autostart(settings, enable: bool) -> Path:
    target = _startup_dir() / LAUNCHER_NAME
    if not enable:
        target.unlink(missing_ok=True)
        print(f"Auto-start removed ({target})")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    # The launcher re-execs with this project's interpreter so it works even if
    # .pyw opens with a different Python.
    target.write_text(
        "import subprocess\n"
        f"subprocess.Popen([{_pythonw()!r}, '-m', 'app', 'service'], cwd={str(settings.root)!r},\n"
        "                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n",
        encoding="utf-8")
    print(f"Auto-start on: {target}\nThe service starts each time you log in.")
    return target


def _stop_running_service(settings) -> None:
    """Stop a service started earlier (e.g. by a previous setup) so the new one uses current code."""
    from datetime import datetime, timedelta, timezone
    from . import db
    conn = db.connect(settings.path("database"))
    pid, beat = db.get_state(conn, "service_pid"), db.get_state(conn, "service_heartbeat")
    conn.close()
    if not pid or not beat:
        return
    try:  # only if its heartbeat is recent, so a recycled pid is never touched
        if datetime.now(timezone.utc) - datetime.fromisoformat(beat) > timedelta(minutes=10):
            return
        os.kill(int(pid), 15)
        print(f"Stopped the previous service (pid {pid}).")
    except (OSError, ValueError):
        pass


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "my-game"


def _ask(prompt: str, default: str = "") -> str:
    ans = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip().strip('"')
    return ans or default


def _ask_cta(name: str) -> str:
    default = f"Play {name} on Roblox - link in bio"
    while True:
        cta = _ask("Call to action", default)
        if "<" in cta or ">" in cta:
            print("  That still contains a <placeholder>; type the real text or press Enter.")
            continue
        return cta


def _set_inbox(settings, inbox: Path) -> None:
    cfg = settings.root / "config" / "settings.toml"
    text = cfg.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^inbox\s*=.*$', f'inbox = "{inbox.as_posix()}"', text, count=1)
    cfg.write_text(text, encoding="utf-8")
    settings.raw["paths"]["inbox"] = inbox.as_posix()


def write_game_json(game_dir: Path, answers: dict) -> Path:
    genre = re.sub(r"[^a-z0-9]", "", answers.get("genre", "").lower())
    game = {
        "name": answers["name"],
        "url": answers.get("url", ""),
        "description": answers.get("description", ""),
        "genre": answers.get("genre", ""),
        "audience": answers.get("audience", ""),
        "cta": answers.get("cta") or f"Play {answers['name']} on Roblox - link in bio",
        "hashtags": ["roblox"] + ([genre] if genre else []),
        "avoid_words": [w.strip() for w in answers.get("avoid", "").split(",") if w.strip()],
    }
    game_dir.mkdir(parents=True, exist_ok=True)
    path = game_dir / "game.json"
    path.write_text(json.dumps(game, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def run(settings, args) -> int:
    print("=== 1/5 Checking FFmpeg")
    if not shutil.which("ffmpeg"):
        print("FFmpeg not found. Install it, then open a NEW terminal and re-run setup:\n"
              "    winget install Gyan.FFmpeg")
        return 1
    print("FFmpeg:", shutil.which("ffmpeg"))

    print("=== 2/5 Installing Python packages")
    req = settings.root / "requirements.txt"
    if subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", "-r", str(req)]) != 0:
        print("pip install failed.")
        return 1

    if not args.skip_demo:
        print("=== 3/5 Demo (synthetic footage, 1-2 minutes)")
        if subprocess.call([sys.executable, "-m", "app", "demo"], cwd=settings.root) != 0:
            print("Demo failed - see logs/autopromo.log")
            return 1

    print("=== 4/5 Where will recordings arrive?")
    print("Phone recordings: upload to a Google Drive / OneDrive / Dropbox folder that syncs to this PC.")
    while True:
        synced = _ask("Synced folder path, e.g. C:\\Users\\you\\Google Drive\\AutoPromo "
                      "(Enter = use the project's inbox folder)")
        if not synced:
            _set_inbox(settings, Path("inbox"))
            break
        inbox = Path(synced).expanduser()
        if not inbox.is_absolute():
            print("  Please paste the full folder path (starting with a drive letter like C:\\), "
                  "or press Enter.")
            continue
        inbox.mkdir(parents=True, exist_ok=True)
        _set_inbox(settings, inbox)
        break
    inbox = settings.path("inbox")
    print("Inbox:", inbox)

    print("=== Describe your game (used for captions and hashtags)")
    name = _ask("Game name (Enter to skip)")
    if name:
        game_dir = inbox / _slug(name)
        if (game_dir / "game.json").exists() and _ask("game.json exists - overwrite? y/N", "n").lower() != "y":
            print("Kept existing game.json")
        else:
            answers = {"name": name,
                       "url": _ask("Roblox game link"),
                       "description": _ask("One-sentence description"),
                       "genre": _ask("Genre (obby, tycoon, simulator, horror...)"),
                       "audience": _ask("Target audience", "8-14"),
                       "cta": _ask_cta(name),
                       "avoid": _ask("Words to never use, comma-separated")}
            print("Wrote", write_game_json(game_dir, answers))
        print(f"Put recordings for this game in: {game_dir}")

    print("=== 5/5 Auto-start")
    if os.name == "nt" and not args.skip_autostart:
        autostart(settings, True)
        _stop_running_service(settings)
        subprocess.Popen([_pythonw(), "-m", "app", "service", "--force"], cwd=settings.root,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("Service (re)started in the background.")
    else:
        print("Skipped (run `python -m app autostart on` on Windows).")

    print("\nDone. Useful commands:\n"
          "  python -m app status      what's happening\n"
          "  python -m app dashboard   charts + queue\n"
          "  Finished videos + captions: queue/ready/  -> after posting: python -m app posted N --url <link>")
    return 0
