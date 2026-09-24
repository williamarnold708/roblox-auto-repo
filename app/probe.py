"""Thin ffprobe wrapper used by every pipeline stage."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


class ProbeError(Exception):
    """File is missing, corrupt, or has no decodable video stream."""


def _fps(rate: str | None) -> float:
    try:
        num, den = (rate or "0/1").split("/")
        return round(float(num) / float(den), 3) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path) -> dict:
    """Return {duration, width, height, fps, has_audio, video_codec}.

    Raises ProbeError for missing/corrupt/unsupported files (no video stream,
    zero size, or non-positive duration)."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ProbeError(f"missing or empty file: {path}")
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as e:
        raise ProbeError(f"ffprobe timed out on {path}") from e
    if res.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path.name}: {res.stderr.strip()[:300]}")
    try:
        info = json.loads(res.stdout or "{}")
    except json.JSONDecodeError as e:
        raise ProbeError(f"unreadable ffprobe output for {path.name}") from e
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise ProbeError(f"no video stream in {path.name}")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = 0.0
    for cand in ((info.get("format") or {}).get("duration"), video.get("duration")):
        try:
            duration = float(cand)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if duration <= 0 or width <= 0 or height <= 0:
        raise ProbeError(f"invalid duration/dimensions in {path.name}")
    return {
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": _fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate")),
        "has_audio": has_audio,
        "video_codec": video.get("codec_name", "unknown"),
    }
