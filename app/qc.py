"""Quality control for rendered videos.

Checks: resolution/aspect, duration, full decode, audio present, black-frame
fraction, text inside the safe area (same layout maths as render.py),
metadata present (caption + hooks), and near-duplicates via a perceptual
average hash (8x8 aHash of 3 frames, stored in renders.phash).
Pass -> render status 'queued'; fail -> 'qc_failed'.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

import numpy as np

from . import log
from .probe import FFMPEG, ProbeError, probe
from .render import pick_hook, plan_text, safe_area

_log = log.get("qc")
HASH_POINTS = (0.3, 0.5, 0.7)   # fractions of duration (after the hook has gone)
_BLACK_RE = re.compile(r"black_duration:\s*([\d.]+)")


def frame_hash(path: Path, t: float) -> str:
    cmd = [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
           "-vf", "scale=8:8:flags=area,format=gray", "-f", "rawvideo", "-"]
    res = subprocess.run(cmd, capture_output=True)
    px = np.frombuffer(res.stdout[:64], dtype=np.uint8)
    if len(px) < 64:
        return "0" * 16
    bits = (px > px.mean()).astype(np.uint8)
    return f"{int(''.join(map(str, bits)), 2):016x}"


def phash(path: Path, duration: float) -> str:
    """Concatenated 64-bit average hashes of frames at HASH_POINTS (48 hex chars)."""
    return "".join(frame_hash(path, duration * p) for p in HASH_POINTS)


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 10 ** 6
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def decode_scan(path: Path) -> tuple[list[str], float]:
    """Decode everything once; return (error lines, total black seconds)."""
    cmd = [FFMPEG, "-nostats", "-hide_banner", "-loglevel", "level+info", "-i", str(path),
           "-vf", "scale=270:480,blackdetect=d=0.1:pix_th=0.10:pic_th=0.98", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    errors = [ln for ln in res.stderr.splitlines() if "[error]" in ln or "[fatal]" in ln]
    if res.returncode != 0 and not errors:
        errors.append(f"ffmpeg exit {res.returncode}")
    black = sum(float(m.group(1)) for m in _BLACK_RE.finditer(res.stderr))
    return errors, black


def check(settings, conn: sqlite3.Connection, render_id: int) -> dict:
    rnd = conn.execute("SELECT * FROM renders WHERE id=?", (render_id,)).fetchone()
    if rnd is None:
        raise ValueError(f"no render {render_id}")
    r, clip_cfg, qcfg = settings.section("render"), settings.section("clipping"), settings.section("qc")
    W, H = int(r.get("width", 1080)), int(r.get("height", 1920))
    tol = float(qcfg.get("duration_tolerance", 1.0))
    dmin, dmax = float(clip_cfg.get("min_seconds", 10)) - tol, float(clip_cfg.get("max_seconds", 30)) + tol
    black_max = float(clip_cfg.get("black_threshold", 0.10))
    dup_threshold = int(qcfg.get("phash_threshold", 12))
    checks: dict[str, dict] = {}

    def add(name, ok, **detail):
        checks[name] = {"ok": bool(ok), **detail}

    path = Path(rnd["path"] or "")
    try:
        info = probe(path)
    except ProbeError as e:
        info = None
        add("probe", False, error=str(e))
    content = conn.execute("SELECT * FROM content WHERE id=?", (rnd["content_id"],)).fetchone()
    ph = None
    if info:
        add("resolution", info["width"] == W and info["height"] == H
            and abs(info["width"] / info["height"] - 9 / 16) < 0.01,
            value=f"{info['width']}x{info['height']}", expected=f"{W}x{H} (9:16)")
        add("duration", dmin <= info["duration"] <= dmax, value=info["duration"], range=[dmin, dmax])
        add("audio", info["has_audio"], value=info["has_audio"])
        errors, black_s = decode_scan(path)
        add("decode", not errors, errors=errors[:5])
        frac = round(black_s / info["duration"], 4) if info["duration"] else 1.0
        add("black_frames", frac <= black_max, value=frac, max=black_max)
        if content is not None:
            lines = plan_text(W, H, pick_hook(content), content["on_screen_text"], info["duration"])
            sa = safe_area(W, H)
            bad = [ln["text"] for ln in lines if ln["x0"] < sa["x0"] or ln["x1"] > sa["x1"]
                   or ln["y0"] < sa["y0"] or ln["y1"] > sa["y1"]]
            add("text_safe_area", not bad and any(ln["role"] == "hook" for ln in lines),
                lines=len(lines), outside=bad, has_hook=any(ln["role"] == "hook" for ln in lines),
                safe_area={k: round(v, 1) for k, v in sa.items()})
        ph = phash(path, info["duration"])
        others = conn.execute(
            "SELECT id, phash FROM renders WHERE id<>? AND phash IS NOT NULL "
            "AND status NOT IN ('qc_failed','rejected')", (render_id,)).fetchall()
        dists = sorted((hamming(ph, o["phash"]), o["id"]) for o in others)
        dup = [rid for d, rid in dists if d < dup_threshold]
        add("near_duplicate", not dup, duplicates_of=dup, nearest=dists[0][0] if dists else None,
            threshold=dup_threshold)
        clip = conn.execute("SELECT path FROM clips WHERE id=?", (rnd["clip_id"],)).fetchone()
        try:
            src = probe(clip["path"]) if clip and clip["path"] else None
            if src:
                checks["source"] = {"ok": True, "width": src["width"], "height": src["height"],
                                    "upscaled": src["width"] < W}
        except ProbeError:
            pass
    if content is None:
        add("metadata", False, error="no content row")
    else:
        try:
            hooks = json.loads(content["hooks"] or "[]")
        except (TypeError, ValueError):
            hooks = []
        add("metadata", bool((content["caption"] or "").strip()) and bool(hooks),
            caption=bool((content["caption"] or "").strip()), hooks=len(hooks) if isinstance(hooks, list) else 0)
    failures = [k for k, v in checks.items() if not v["ok"]]
    report = {"render_id": render_id, "passed": not failures, "failures": failures, "checks": checks}
    status = "queued" if not failures else "qc_failed"
    conn.execute("UPDATE renders SET qc_report=?, qc_passed=?, status=?, phash=COALESCE(?, phash) WHERE id=?",
                 (json.dumps(report), int(not failures), status, ph, render_id))
    conn.commit()
    _log.info("QC render %s: %s %s", render_id, "PASS" if not failures else "FAIL", failures)
    return report
