#!/usr/bin/env python3
"""Generate SYNTHETIC demo footage so the pipeline can be tried without Roblox.

IMPORTANT: this is not real gameplay. It is ffmpeg test-pattern video
(lavfi sources: SMPTE bars, testsrc2 with a moving box, animated gradients,
Conway's game of life) with a synthetic soundtrack (sine tones and noise
bursts). It exists only to exercise ingest -> clip -> content -> render -> QC
end to end. Clips made from it are not something you should post.

Timeline for the default 90 s (scaled proportionally for other durations):

    0-10  s  static bars, quiet tone         ("lobby", dead time)
    10-16 s  black                            ("loading screen")
    16-40 s  moving test pattern, louder     noise burst 29-32 s, boss_defeat @ 31 s
    40-54 s  slow gradients, near silence     (dead time)
    54-74 s  game of life, louder            bursts 59-60.5 s & 65-67.5 s,
                                              death @ 59.5 s, rare_item @ 66 s
    74-90 s  static flat colour, quiet       ("outro", dead time)

Writes <out-root>/inbox/demo-obby/{demo_session.mp4, demo_session.events.json, game.json}.

Usage:  python scripts/make_sample.py [--out-root PATH] [--duration 90] [--size 1280x720]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
BASE = 90.0
# (start, end, lavfi source template) in base-90 s time
SEGMENTS = [
    (0, 10, "smptebars=size={W}x{H}:rate={R}"),
    (10, 16, "color=c=0x060606:size={W}x{H}:rate={R}"),
    (16, 40, "testsrc2=size={W}x{H}:rate={R},drawbox=x='mod(t*{V},iw)':y=ih/3:w=iw/8:h=ih/5:color=red:t=fill"),
    (40, 54, "gradients=size={W}x{H}:rate={R}:speed=0.002:seed=7"),
    (54, 74, "life=size={W}x{H}:rate={R}:mold=10:ratio=0.12:seed=42:life_color=#33ff66:death_color=#aa2222"),
    (74, 90, "color=c=0x224466:size={W}x{H}:rate={R}"),
]
LOUD = [(16, 40, 0.12), (54, 74, 0.12)]            # louder "action" tone
BURSTS = [(29, 32), (59, 60.5), (65, 67.5)]       # loud noise bursts
EVENTS = [(31, "boss_defeat", "Defeated the Lava Titan"),
          (59.5, "death", "Fell off the spinning bar"),
          (66, "rare_item", "Found the Golden Jetpack")]
BLACK = (10, 16)

GAME_JSON = {
    "name": "Demo Obby (synthetic)",
    "url": "https://www.roblox.com/games/0/demo-obby",
    "description": "Synthetic demo game used to test RobloxAutoPromo. Not a real experience.",
    "genre": "obby",
    "audience": "kids and teens who like parkour challenges",
    "cta": "Try the obby - link in bio!",
    "avoid_words": ["free robux", "hack"],
    "hashtags": ["roblox", "obby", "robloxobby"],
}


def generate(out_root, duration: float = 90.0, width: int = 1280, height: int = 720,
             fps: int = 30, slug: str = "demo-obby", stem: str = "demo_session",
             preset: str = "veryfast") -> dict:
    """Write the synthetic video + events + game.json; return paths and the scaled timeline."""
    k = duration / BASE
    folder = Path(out_root) / "inbox" / slug
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{stem}.mp4"
    cmd = [FFMPEG, "-v", "error", "-y"]
    for s, e, src in SEGMENTS:
        cmd += ["-f", "lavfi", "-t", f"{(e - s) * k:.3f}",
                "-i", src.format(W=width, H=height, R=fps, V=max(40, width // 3))]
    terms = ["0.02*sin(2*PI*220*t)"]
    for s, e, amp in LOUD:
        terms.append(f"{amp}*sin(2*PI*330*t)*between(t,{s * k:.3f},{e * k:.3f})")
    for s, e in BURSTS:
        terms.append(f"0.8*(random(0)*2-1)*between(t,{s * k:.3f},{e * k:.3f})")
    expr = "+".join(terms)
    cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"aevalsrc='{expr}':s=22050"]
    n = len(SEGMENTS)
    chain = "".join(f"[{i}:v]setsar=1[v{i}];" for i in range(n))
    chain += "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    cmd += ["-filter_complex", chain, "-map", "[v]", "-map", f"{n}:a",
            "-c:v", "libx264", "-preset", preset, "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(video)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr[-800:]}")
    events = [{"t": round(t * k, 3), "kind": kind, "detail": detail} for t, kind, detail in EVENTS]
    (folder / f"{stem}.events.json").write_text(json.dumps({"events": events}, indent=2))
    (folder / "game.json").write_text(json.dumps(GAME_JSON, indent=2))
    return {"video": video, "events_file": folder / f"{stem}.events.json",
            "game_json": folder / "game.json", "events": events,
            "black": (BLACK[0] * k, BLACK[1] * k), "duration": duration}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out-root", default=str(Path(__file__).resolve().parent.parent),
                    help="project root (writes into <out-root>/inbox/demo-obby)")
    ap.add_argument("--duration", type=float, default=90.0)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args(argv)
    w, h = (int(x) for x in a.size.lower().split("x"))
    out = generate(a.out_root, a.duration, w, h, a.fps)
    print(f"SYNTHETIC demo footage written: {out['video']}")
    print(f"events: {out['events_file']}  game: {out['game_json']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
