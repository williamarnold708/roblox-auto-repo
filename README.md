# RobloxAutoPromo

Turns your own Roblox gameplay recordings into vertical TikTok-ready videos,
schedules them, and (optionally) sends them to TikTok. It runs on your Windows
PC, free, with no cloud services required.

```
inbox/<game>/*.mp4 ─► ingest ─► find best moments ─► hooks + captions ─► 1080x1920 render ─► quality check
                                                                                              │
         daily summary + notifications ◄─ metrics ◄─ publish (local / inbox / direct) ◄─ schedule
```

You record. It does the editing, writing, queueing and bookkeeping. You spend
0-10 minutes a day on what only a person can do.

## Easiest setup (Windows)

1. On GitHub: **Code -> Download ZIP**, and extract it (e.g. to `Documents\roblox-auto-repo`).
2. Open the extracted folder, click the address bar, type `powershell` and press Enter. Then run:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once; allows local scripts
   Get-ChildItem -Recurse *.ps1 | Unblock-File           # trust the scripts you downloaded
   .\scripts\setup.ps1
   ```
   It installs Python and FFmpeg if missing, installs the packages, runs the
   demo, asks for your game details and (optionally) your Google Drive /
   OneDrive folder, and turns on auto-start.
3. Record gameplay (phone screen recording works) and upload it to the game's
   folder in that synced folder. Finished videos + captions appear in `queue\ready\`.

Phone recordings: Roblox's event logger output cannot leave a live game on a
phone, so clips are picked from motion, audio and scene changes instead. That
works without any event file.

## What is automated and what is not

| Step | Automated? | Notes |
|---|---|---|
| Recording gameplay | **No - you** | OBS, your own play sessions. Nothing plays the game for you. |
| Picking up new recordings | Yes | Drop files in `inbox/<game-slug>/`; the service polls every 60 s. |
| Finding the best 10-30 s moments | Yes | Motion/audio/scene analysis + optional Roblox event log. Low-quality recordings go to *review* instead. |
| Hooks, on-screen text, caption, hashtags | Yes | Local templates, or a local Ollama model if installed. Only mentions things that actually happened. |
| Vertical 1080x1920 render with text | Yes | ffmpeg. |
| Quality check (black frames, duplicates, length, text) | Yes | Failures are never posted. |
| Scheduling (slots, max per day, rotate games) | Yes | Favours games with better **real** results, keeps exploring. |
| Posting to TikTok, mode A `local` (default) | **Partly** | Video + caption are put in `queue/ready/`; you upload in the TikTok app. |
| Posting, mode B `inbox` | **Partly** | Uploaded to your TikTok inbox as a draft; you tap Post in the app. |
| Posting, mode C `direct` | Yes, after approval | Needs a TikTok-audited app **and** your approval per post. |
| Reading views/likes | Mode B/C: Yes (API) · Mode A: **you type them in** | Never estimated. Missing data is shown as "unavailable". |
| Daily summary + "Action needed" list | Yes | `logs/summary-YYYY-MM-DD.md` + a Windows notification. |

## Quickstart (Windows)

1. Install Python 3.11+ and ffmpeg (PowerShell):
   ```powershell
   winget install Python.Python.3.11
   winget install Gyan.FFmpeg
   ```
   Open a **new** terminal afterwards so `ffmpeg` is on PATH (`ffmpeg -version` should work).
2. Install the Python packages from the project folder:
   ```powershell
   cd RobloxAutoPromo
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Optional, better captions with a free local AI model:
   ```powershell
   winget install Ollama.Ollama
   ollama pull llama3.2:3b
   ```
   Without Ollama, built-in templates are used (`[content] provider = "auto"` picks automatically).
4. Try the demo (below), then add your own recordings and start the service:
   ```powershell
   python -m app run          # one pass, prints what it did
   python -m app status
   .\scripts\install_windows_task.ps1 -StartNow
   ```

## Demo mode

```powershell
python -m app demo
```

Generates a short **synthetic** gameplay video (`scripts/make_sample.py`) into a
temporary folder, runs the full pipeline (ingest → clips → captions → render →
QC → schedule → local "publish"), and prints the summary. No TikTok account or
credentials are needed and nothing is uploaded. Use `--here` to run it in this
project's own `inbox/`, and open the result with
`python -m app --root "<printed folder>" dashboard`.

## Adding recordings: inbox layout

```
inbox/
  my-obby/                     <- one folder per game (the folder name is the game slug)
    game.json                  <- recommended, once per game
    2026-09-24 18-02-11.mp4    <- your recordings (.mp4 / .mov / .mkv)
    2026-09-24 18-02-11.events.json   <- optional, from the Roblox EventLogger (same file stem)
```

`game.json`:

```json
{
  "name": "My Obby",
  "url": "https://www.roblox.com/games/1234567890/My-Obby",
  "description": "A 50-stage obby with lava and moving platforms",
  "genre": "obby",
  "audience": "8-14, likes hard obbies",
  "cta": "Play My Obby on Roblox - link in bio",
  "hashtags": ["roblox", "obby"],
  "avoid_words": ["free robux"]
}
```

Files are only picked up once they have stopped growing, so it is safe to let
OBS write straight into the folder. Processed files move to `processing/`;
duplicates go to `inbox/_duplicates/`, unreadable files to `inbox/_rejected/`.

### Recording with OBS

- Output: **1920x1080 at 60 fps** (best) or **1920x1080 at 30 fps** (fine).
  Settings → Video: Base and Output resolution 1920x1080.
- Recording format **MP4** - or record as MKV (safer if OBS crashes) and use
  *File → Remux Recordings* to MP4. `.mkv` is accepted too.
- Encoder: hardware (NVENC/AMF/QuickSync) if available, quality "High Quality, Medium File Size".
- Record game audio. Turn off webcam/overlays for recordings you want promoted.
- Set Settings → Output → Recording Path to `...\RobloxAutoPromo\inbox\<game-slug>`.
- 5-20 minute sessions with real action (wins, fails, close calls) work best.
- Optional: the Roblox EventLogger marks real moments so clips and captions are
  better. See **[roblox/README.md](roblox/README.md)**.

## Your daily routine (0-10 minutes)

1. Play and record as usual (the service picks it up on its own).
2. Read the Windows notification or `logs/summary-<today>.md` → **Action needed**.
3. Do what it lists, typically:
   - Mode A: upload the videos in `queue/ready/` (each has a `.txt` with the
     caption), then `python -m app posted <post id> --url <link>`.
   - Mode B: open TikTok, finish the drafts in your inbox, post.
   - Mode C: preview upcoming posts and `python -m app approve <post id>`.
   - Once a day or so, type in views for mode A posts:
     `python -m app metrics add --post 12 --views 3400 --likes 210`
     (or use the dashboard form). Modes B/C: `python -m app metrics fetch`.
4. Glance at the dashboard: `python -m app dashboard`.

If nothing is listed, there is nothing to do.

## Commands

| Command | What it does |
|---|---|
| `python -m app run` | One pass: ingest → clip → content → render → QC → plan → publish due → summary (after 21:00, once a day) |
| `python -m app service [--interval 60]` | Runs forever, polling the inbox. Ctrl+C stops cleanly. |
| `python -m app demo [--here]` | Synthetic end-to-end demo, no TikTok needed |
| `python -m app status` | Counts, flags, upcoming posts, failed jobs, action list |
| `python -m app summary [--day YYYY-MM-DD] [--notify]` | Write/print the daily summary |
| `python -m app pause` / `resume` | Stop / restart **publishing** (processing continues) |
| `python -m app kill` / `unkill` | **Global kill switch**: stops all processing and publishing |
| `python -m app approve <post> [--privacy SELF_ONLY]` | Approve one post for Direct Post (mode C) |
| `python -m app posted <post> [--url ...]` | Confirm you posted a mode A/B video yourself |
| `python -m app retry <job>` | Re-run a processing job that gave up after 3 attempts |
| `python -m app auth` | Connect your TikTok account (modes B/C) |
| `python -m app metrics add --post N --views ...` | Record real numbers you read in the TikTok app |
| `python -m app metrics fetch` | Pull metrics from the TikTok API (modes B/C) |
| `python -m app dashboard` | Streamlit dashboard in your browser |

Double-clicking `scripts\run_service.bat` runs the service in a console window.

### Running automatically (Task Scheduler)

```powershell
.\scripts\install_windows_task.ps1 [-SummaryTime 21:00] [-StartNow]
.\scripts\uninstall_windows_task.ps1
```

Registers, for your user only (no admin): **RobloxAutoPromo Service** (at logon,
`pythonw -m app service`, no window, restarted by a 15-minute watchdog trigger
if it stops, never two copies) and **RobloxAutoPromo Daily Summary**.

## Settings

`config/settings.toml`. The ones you are most likely to change:

```toml
[schedule]
max_posts_per_day = 2
slots = ["12:30", "18:30", "20:30"]   # local time
timezone = "Europe/London"
# exploration_share = 0.3            # min share of slots spread evenly over all games

[publish]
mode = "local"                       # local (A) | inbox (B) | direct (C)

# [ops]
# poll_seconds = 60
# summary_time = "21:00"

# [safety]
# kill_switch = true                 # same as `python -m app kill`
```

## TikTok integration

Three honest modes (set `[publish] mode`):

- **A - `local` (default).** No TikTok account connection. Finished videos and
  captions go to `queue/ready/`; you post them in the app. Metrics are typed in
  by you.
- **B - `inbox`.** Uses TikTok's Content Posting API upload scope. The video
  lands in your TikTok inbox as a draft and you finish the post in the app.
  Needs a (free) TikTok developer app and `python -m app auth`.
- **C - `direct`.** Direct Post. Only works once TikTok has **audited** your
  developer app (until then TikTok only allows private posts for a few
  users), you set `app_audited = true`, choose a privacy level, and approve
  each post (`python -m app approve <id>`). Nothing is ever posted publicly
  without that approval.

Credentials (`TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET`) are stored in the
Windows Credential Manager when `keyring` is installed, otherwise read from
environment variables; tokens are never logged. If TikTok rejects the token or
rate-limits us, publishing pauses itself and the summary tells you to run
`python -m app auth` and `python -m app resume`.

Full setup, scopes, audit rules and API limits: **[docs/TIKTOK_API.md](docs/TIKTOK_API.md)**.

## Roblox event logger

An optional Lua module for **your own** game that writes timestamps of real
moments (wins, deaths, rare items...) while you play-test and record. The
pipeline uses it to pick better clips and to keep captions truthful. It never
plays or automates anything. See **[roblox/README.md](roblox/README.md)**.

## Costs

**£0.** Python, ffmpeg, Ollama, Streamlit, SQLite and the TikTok developer
APIs are free. Optional paid text-generation APIs are supported but **off by
default**: they are only used if you name the provider in `[content] provider`
and set its API key yourself.

## Safety controls

- **Kill switch:** `python -m app kill`, the dashboard button, `[safety]
  kill_switch = true`, or simply create an empty file named `KILL` in the
  project folder. All processing and publishing stop (the service idles until
  cleared with `python -m app unkill`, which also deletes the file).
- **Pause publishing:** `python -m app pause` (also set automatically on auth
  expiry or TikTok rate limits). Processing continues; nothing is posted.
- **Per-post approval** for Direct Post; unaudited apps are refused.
- **Never twice:** each render can have only one post (enforced by the database);
  files are hashed so the same recording is never processed twice.
- **Caps:** `max_posts_per_day`, fixed time slots.
- **Retries with backoff:** a failing step is retried 3 times (1, 2, 4 min),
  then marked failed and you are notified. Crashed runs resume where they
  stopped.
- **Honest numbers:** metrics are only from the TikTok API or your own manual
  entry, labelled by source. No estimates; "no data yet" / "unavailable" otherwise.
- Notifications only when you need to act, plus one daily summary
  (`logs/NOTIFICATIONS.md` keeps a copy; identical ones are sent once per day).

## Troubleshooting

| Problem | Fix |
|---|---|
| `ffmpeg not found` | `winget install Gyan.FFmpeg`, then open a new terminal. `ffmpeg -version` must work. |
| Nothing happens to a new recording | `python -m app status`. Is the kill switch on or a `KILL` file present? Is the file still being written (OBS)? Is it in `inbox/<game>/`, not `inbox/` itself? |
| Recording shows **review** | No good 10-30 s moment was found (idle, menus, black screen). Record livelier footage or add EventLogger marks. |
| Recording shows **failed** | See `logs/autopromo.log`; fix the cause, then `python -m app retry <job id>` (ids in `status`). |
| Publishing paused | Read the reason in `status`. Auth: `python -m app auth` then `resume`. Rate limit: wait, then `resume`. |
| No Windows notifications | Check Focus Assist / notification settings. Optional nicer toasts: `Install-Module BurntToast -Scope CurrentUser`. Everything is also in `logs/NOTIFICATIONS.md`. |
| Service not running | `Get-ScheduledTask "RobloxAutoPromo*"`; re-run the install script; or run `scripts\run_service.bat` to see errors. |
| "Another service instance looks alive" | One is already running. If you're sure it isn't, wait 3 minutes or use `service --force`. |
| Dashboard won't open | `pip install -r requirements.txt`, then `python -m app dashboard` (opens http://localhost:8501). |
| Captions are generic | Install Ollama (see Quickstart) and fill in `game.json`. |

Logs: `logs/autopromo.log` (rotating, secrets redacted). Database:
`analytics/autopromo.db` (SQLite).
