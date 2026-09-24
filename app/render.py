"""Render a clip into a vertical 1080x1920 TikTok-ready MP4.

Layouts ([render] layout):
  * blur_fill   - blurred, darkened, cover-scaled copy of the gameplay as the
                  background, with the full uncropped gameplay centred on top
                  (aspect preserved, never stretched).
  * center_crop - gameplay cover-scaled and centre-cropped to 9:16.

The chosen hook is burned in for the first HOOK_SECONDS at the top of the
frame; optional on_screen_text is shown afterwards in the lower-middle band.
All text stays inside the safe area computed by plan_text() (8% side
margins, nothing in the bottom 20% where TikTok's UI sits) - qc.py re-uses
the same function to verify it. Text is passed to drawtext via textfile=
with expansion disabled, so no user text is ever interpreted by ffmpeg.

Audio is loudness-normalised to ~-14 LUFS (single-pass loudnorm); a silent
stereo track is added when the source has none. Video and audio fade out
over the last FADE_SECONDS. No watermarks, logos or music are added.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import db, log
from .probe import FFMPEG, probe

_log = log.get("render")

HOOK_SECONDS = 3.0
FADE_SECONDS = 0.5
SIDE_MARGIN = 0.08       # fraction of width on each side
TOP_MARGIN = 0.05        # fraction of height kept clear at the top
BOTTOM_UI = 0.20         # bottom fraction reserved for TikTok UI
BOX_BORDER = 18
HOOK_TOP = 0.10          # hook block starts at 10% of height
OST_TOP = 0.66           # on-screen text block starts at 66% of height


class RenderError(Exception):
    pass


# ---------------------------------------------------------------- text layout
def _char_w(c: str) -> float:
    """Conservative advance width (in em) for DejaVu Sans Bold."""
    if c == " ":
        return 0.36
    if c in "MW":
        return 1.02
    if c in "mw":
        return 1.06
    if c.isupper():
        return 0.80
    if c.isdigit():
        return 0.72
    if c.islower():
        return 0.68
    if ord(c) > 127:
        return 0.90
    return 0.55


def text_width(text: str, size: int) -> float:
    return sum(_char_w(c) for c in text) * size


def clean_text(text) -> str:
    """Drop control chars and glyphs DejaVu can't draw (emoji etc.); collapse whitespace."""
    if text is None:
        return ""
    s = "".join(c for c in str(text) if (c.isprintable() or c == " ") and ord(c) < 0x2190)
    return " ".join(s.split())


def wrap(text: str, size: int, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        while text_width(word, size) > max_w:        # hard-split very long words
            cut = len(word)
            while cut > 1 and text_width(word[:cut], size) > max_w:
                cut -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:cut])
            word = word[cut:]
        trial = f"{cur} {word}".strip()
        if text_width(trial, size) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _block(text: str, W: int, H: int, top: float, bottom: float, sizes, max_lines: int,
           start: float, end: float | None, role: str) -> list[dict]:
    text = clean_text(text)
    if not text:
        return []
    max_w = W * (1 - 2 * SIDE_MARGIN) - 2 * BOX_BORDER
    for size in sizes:
        lines = wrap(text, size, max_w)
        pitch = int(size * 1.25 + 2 * BOX_BORDER)
        if len(lines) <= max_lines and top + pitch * len(lines) <= bottom:
            break
    else:  # still too long at the smallest size: truncate with an ellipsis
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,") + "..."
        while text_width(lines[-1], size) > max_w and len(lines[-1]) > 4:
            lines[-1] = lines[-1][:-4].rstrip() + "..."
    out = []
    for i, ln in enumerate(lines):
        tw = text_width(ln, size)
        y = int(top + i * pitch)
        out.append({"role": role, "text": ln, "fontsize": size, "y": y,
                    "x0": round((W - tw) / 2 - BOX_BORDER, 1), "x1": round((W + tw) / 2 + BOX_BORDER, 1),
                    "y0": y - BOX_BORDER, "y1": int(y + size * 1.2 + BOX_BORDER),
                    "start": start, "end": end})
    return out


def plan_text(W: int, H: int, hook: str | None, on_screen: str | None, duration: float) -> list[dict]:
    """Deterministic text layout (one dict per drawn line, with its box in px)."""
    lines = _block(hook or "", W, H, H * HOOK_TOP, H * 0.38, [int(W * r) for r in
                   (0.067, 0.061, 0.055, 0.050, 0.044, 0.040)], 3, 0.0, min(HOOK_SECONDS, duration), "hook")
    ost_start = min(HOOK_SECONDS, duration) if lines else 0.0
    lines += _block(on_screen or "", W, H, H * OST_TOP, H * (1 - BOTTOM_UI),
                    [int(W * r) for r in (0.048, 0.044, 0.040, 0.036)], 2, ost_start, duration, "on_screen")
    return lines


def safe_area(W: int, H: int) -> dict:
    return {"x0": W * SIDE_MARGIN, "x1": W * (1 - SIDE_MARGIN), "y0": H * TOP_MARGIN, "y1": H * (1 - BOTTOM_UI)}


# ---------------------------------------------------------------- helpers
def pick_hook(content_row) -> str:
    try:
        hooks = json.loads(content_row["hooks"] or "[]")
    except (TypeError, ValueError):
        hooks = [content_row["hooks"]] if content_row["hooks"] else []
    if isinstance(hooks, str):
        hooks = [hooks]
    if not hooks:
        return ""
    idx = content_row["chosen_hook"] or 0
    idx = idx if 0 <= idx < len(hooks) else 0
    h = hooks[idx]
    if isinstance(h, dict):
        h = h.get("text") or h.get("hook") or ""
    return str(h)


def _esc(value: str) -> str:
    """Escape a file path for a filter option inside -filter_complex.

    Two parsing levels apply (graph, then option), so ':' and "'" need a
    doubled backslash. Backslashes become '/' (Windows accepts both), which also
    keeps 'C:\\Users' from turning into escape sequences."""
    return (value.replace("\\", "/").replace(":", "\\\\:").replace(",", "\\,")
            .replace("'", "\\\\\\'").replace(" ", "\\ "))


FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
)


def resolve_font(configured: str | None) -> str:
    """The configured font if it exists, else the first bold font found on this OS."""
    for f in ([configured] if configured else []) + list(FONT_CANDIDATES):
        if f and Path(f).is_file():
            return f
    raise RenderError("no usable font found; set [render] font in config/settings.toml "
                      "to a .ttf file on this computer")


def _video_graph(layout: str, W: int, H: int, fps: int) -> str:
    if layout == "center_crop":
        return (f"[0:v]fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={W}:{H},setsar=1,format=yuv420p[base]")
    bw, bh = W // 4, H // 4
    return (f"[0:v]fps={fps},setsar=1,split=2[bgs][fgs];"
            f"[bgs]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=16:2,scale={W}:{H},eq=brightness=-0.08:saturation=0.9[bg];"
            f"[fgs]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
            f"setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[base]")


def build_command(src: Path, out: Path, info: dict, lines: list[dict], txt_dir: Path, settings) -> list[str]:
    r = settings.section("render")
    W, H, fps = int(r.get("width", 1080)), int(r.get("height", 1920)), int(r.get("fps", 30))
    font = resolve_font(r.get("font"))
    D = info["duration"]
    graph = _video_graph(str(r.get("layout", "blur_fill")), W, H, fps)
    chain = "[base]"
    for i, ln in enumerate(lines):
        tf = txt_dir / f"line{i}.txt"
        tf.write_text(ln["text"], encoding="utf-8")
        end = ln["end"] if ln["end"] is not None else D
        opts = [f"fontfile={_esc(font)}", f"textfile={_esc(str(tf))}", "expansion=none",
                f"fontsize={ln['fontsize']}", "fontcolor=white", "borderw=3", "bordercolor=black@0.9",
                "box=1", "boxcolor=black@0.55", f"boxborderw={BOX_BORDER}",
                "x=(w-text_w)/2", f"y={ln['y']}", f"enable=between(t\\,{ln['start']:.2f}\\,{end:.2f})"]
        chain += f"drawtext={':'.join(opts)},"
    fade_st = max(0.0, D - FADE_SECONDS)
    graph += f";{chain}fade=t=out:st={fade_st:.3f}:d={FADE_SECONDS}[v]"
    cmd = [FFMPEG, "-v", "error", "-y", "-i", str(src)]
    if info["has_audio"]:
        graph += (f";[0:a:0]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                  f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                  f"afade=t=out:st={fade_st:.3f}:d={FADE_SECONDS}[a]")
        amap = "[a]"
    else:
        cmd += ["-f", "lavfi", "-t", f"{D:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a"
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", amap, "-t", f"{D:.3f}",
            "-c:v", "libx264", "-preset", str(r.get("preset", "veryfast")), "-crf", str(r.get("crf", 20)),
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(out)]
    return cmd


# ---------------------------------------------------------------- entry point
def render(settings, conn: sqlite3.Connection, clip_id: int, content_id: int) -> int:
    """Render clip + content into rendered/; insert a renders row and return its id."""
    clip = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    content = conn.execute("SELECT * FROM content WHERE id=?", (content_id,)).fetchone()
    if clip is None or not clip["path"]:
        raise RenderError(f"clip {clip_id} missing or has no file")
    if content is None:
        raise RenderError(f"content {content_id} missing")
    src = Path(clip["path"])
    info = probe(src)
    r = settings.section("render")
    W, H = int(r.get("width", 1080)), int(r.get("height", 1920))
    lines = plan_text(W, H, pick_hook(content), content["on_screen_text"], info["duration"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    out = settings.path("rendered") / f"clip{clip_id}_c{content_id}_{stamp}.mp4"
    txt_dir = Path(tempfile.mkdtemp(prefix="arp_txt_"))
    try:
        cmd = build_command(src, out, info, lines, txt_dir, settings)
        res = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        shutil.rmtree(txt_dir, ignore_errors=True)
    if res.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg render failed: {res.stderr.strip()[-600:]}")
    oinfo = probe(out)
    upscale = W / info["width"] if r.get("layout", "blur_fill") != "center_crop" else H / info["height"]
    _log.info("rendered clip %s -> %s (source %dx%d, fg scale x%.2f, %d text lines)", clip_id, out.name,
              info["width"], info["height"], upscale, len(lines))
    cur = conn.execute(
        "INSERT INTO renders(clip_id,content_id,path,width,height,duration,status,created_at) "
        "VALUES(?,?,?,?,?,?,'rendered',?)",
        (clip_id, content_id, str(out.resolve()), oinfo["width"], oinfo["height"], oinfo["duration"], db.now()))
    conn.commit()
    return cur.lastrowid
