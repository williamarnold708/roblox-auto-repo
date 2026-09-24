"""Deterministic highlight finder.

The recording is decoded once at low resolution / low frame rate (default
4 fps, 160 px wide, greyscale) and once as 8 kHz mono audio. Per 0.25 s bin we
measure motion energy (mean absolute frame difference), scene changes
(grey-histogram jumps), mean luma (black / loading screens) and audio RMS
loudness. Candidate windows are anchored on Roblox-logger events (weighted
highest) and on peaks of the combined excitement curve, placed so the moment
lands ~60-70% into the clip (setup -> moment -> payoff), penalised for black
and static dead time, then greedily selected without overlap.

Every reason stored in clips.reasons is a measured value or a logged event;
nothing is invented.
"""
from __future__ import annotations

import json
import math
import sqlite3
import subprocess
from pathlib import Path

import numpy as np

from . import db, log
from .probe import FFMPEG, ProbeError, probe

_log = log.get("clipper")

EVENT_WEIGHTS = {"victory": 1.0, "boss_defeat": 1.0, "rare_item": 0.9, "high_score": 0.85,
                 "unexpected": 0.8, "death": 0.7, "custom": 0.6}
BLACK_LUMA = 40         # grey level (0-255) at/below which a pixel counts as dark
BLACK_PIXELS = 0.98     # frame is black/loading when >= this fraction of pixels are dark (like blackdetect pic_th)
STATIC_MOTION = 0.8    # mean abs diff below which consecutive frames are "static"


class ClipError(Exception):
    pass


# ---------------------------------------------------------------- analysis
def _video_stats(path: Path, width: int, height: int, fps: float, sample_w: int):
    sw = sample_w
    sh = max(2, int(round(sw * height / max(width, 1) / 2)) * 2)
    cmd = [FFMPEG, "-v", "error", "-i", str(path), "-an", "-sn",
           "-vf", f"fps={fps},scale={sw}:{sh}:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    fsize = sw * sh
    luma, dark, motion, scene = [], [], [], []
    prev = prev_hist = None
    try:
        while True:
            buf = proc.stdout.read(fsize)
            if len(buf) < fsize:
                break
            fr = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
            luma.append(float(fr.mean()))
            dark.append(float((fr <= BLACK_LUMA).mean()))
            hist = np.bincount((fr // 16).astype(np.int64), minlength=16) / fsize
            if prev is None:
                motion.append(0.0)
                scene.append(0.0)
            else:
                motion.append(float(np.abs(fr - prev).mean()))
                scene.append(1.0 if 0.5 * np.abs(hist - prev_hist).sum() > 0.35 else 0.0)
            prev, prev_hist = fr, hist
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0 and not luma:
        raise ClipError(f"video analysis failed: {err.strip()[:300]}")
    return np.array(luma), np.array(dark), np.array(motion), np.array(scene)


def _audio_db(path: Path, bin_s: float, rate: int = 8000) -> np.ndarray:
    cmd = [FFMPEG, "-v", "error", "-i", str(path), "-vn", "-sn", "-ac", "1", "-ar", str(rate),
           "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    per_bin = int(rate * bin_s)
    chunk = per_bin * 2 * 256
    out, rest = [], b""
    try:
        while True:
            buf = proc.stdout.read(chunk)
            if not buf:
                break
            buf = rest + buf
            usable = (len(buf) // (per_bin * 2)) * per_bin * 2
            rest = buf[usable:]
            if usable:
                a = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32) / 32768.0
                rms = np.sqrt((a.reshape(-1, per_bin) ** 2).mean(axis=1))
                out.append(20 * np.log10(np.maximum(rms, 1e-5)))
    finally:
        proc.stdout.close()
        proc.wait()
    return np.concatenate(out) if out else np.array([])


def _fit(a: np.ndarray, n: int, fill: float) -> np.ndarray:
    if len(a) >= n:
        return a[:n]
    return np.concatenate([a, np.full(n - len(a), fill)])


def _smooth(a: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(a) == 0:
        return a.copy()
    return np.convolve(a, np.ones(k) / k, mode="same")


def analyze(path, info: dict | None = None, fps: float = 4.0, sample_w: int = 160) -> dict:
    """Per-bin (1/fps s) signals for a recording. Cheap even for hour-long files."""
    path = Path(path)
    info = info or probe(path)
    luma, dark, motion, scene = _video_stats(path, info["width"], info["height"], fps, sample_w)
    n = max(len(luma), int(math.ceil(info["duration"] * fps)))
    luma, motion, scene = _fit(luma, n, 0.0), _fit(motion, n, 0.0), _fit(scene, n, 0.0)
    dark = _fit(dark, n, 1.0)
    audio = _audio_db(path, 1.0 / fps) if info.get("has_audio") else np.array([])
    audio = _fit(audio, n, -100.0)

    black = dark >= BLACK_PIXELS
    motion_n = np.clip(motion / max(float(np.percentile(motion, 90)) if n else 0.0, 4.0), 0, 1)
    audible = audio > -45
    if audible.any():
        med = max(float(np.median(audio[audible])), -60.0)
        top = float(np.percentile(audio[audible], 98))
        loud_n = np.clip((audio - med) / max(top - med, 6.0), 0, 1) * audible
    else:
        loud_n = np.zeros(n)
    k2 = int(2 * fps)
    scene_s = np.clip(_smooth(scene, k2) * k2 / 2.0, 0, 1)   # cuts per 2 s, saturating at 2
    excite = 0.45 * motion_n + 0.35 * loud_n + 0.20 * scene_s
    excite[black] = 0.0
    static = (motion < STATIC_MOTION) & ~black & (loud_n < 0.2)
    return {"fps": fps, "n": n, "duration": info["duration"], "luma": luma, "motion": motion,
            "motion_n": motion_n, "scene": scene, "audio_db": audio, "loud_n": loud_n,
            "black": black, "static": static, "excite": excite,
            "excite_s": _smooth(excite, int(fps))}


# ---------------------------------------------------------------- scoring
def _score_window(sig: dict, start: float, end: float, events: list[dict], black_max: float):
    fps = sig["fps"]
    a, b = int(start * fps), max(int(start * fps) + 1, int(round(end * fps)))
    b = min(b, sig["n"])
    if b <= a:
        return None
    black_frac = float(sig["black"][a:b].mean())
    if black_frac > black_max:
        return None
    static_frac = float(sig["static"][a:b].mean())
    mean_e = float(sig["excite"][a:b].mean())
    peak_e = float(sig["excite_s"][a:b].max())
    base = 0.5 * mean_e + 0.5 * peak_e
    inside = [e for e in events if start <= e["t"] <= end]
    if inside:
        ev = min(1.0, max(EVENT_WEIGHTS.get(e["kind"], 0.6) for e in inside) + 0.1 * (len(inside) - 1))
        raw = 0.4 * base + 0.6 * ev
    else:
        ev = 0.0
        raw = 0.75 * base
    score = raw - 1.0 * black_frac - 0.4 * static_frac
    return {"score": round(max(0.0, min(1.0, score)), 4), "base": round(base, 4), "event": round(ev, 4),
            "black_fraction": round(black_frac, 4), "static_fraction": round(static_frac, 4),
            "events": inside, "a": a, "b": b}


def _peaks(sig: dict, sep_s: float, limit: int) -> list[float]:
    es, fps = sig["excite_s"], sig["fps"]
    order = np.argsort(-es, kind="stable")
    chosen: list[int] = []
    sep = max(1, int(sep_s * fps))
    for i in order:
        if es[i] < 0.2 or len(chosen) >= limit:
            break
        if all(abs(int(i) - c) >= sep for c in chosen):
            chosen.append(int(i))
    return [c / fps for c in chosen]


def _reasons(sig: dict, anchor: dict, w: dict, start: float, end: float) -> dict:
    fps, a, b = sig["fps"], w["a"], w["b"]
    triggers = []
    for e in w["events"]:
        triggers.append(f"logger event '{e['kind']}' at {e['t']:.1f}s"
                        + (f" ({e['detail']})" if e.get("detail") else ""))
    loud_i = a + int(np.argmax(sig["loud_n"][a:b]))
    if sig["loud_n"][loud_i] >= 0.6:
        triggers.append(f"audio loudness peak {sig['audio_db'][loud_i]:.1f} dBFS at {loud_i / fps:.1f}s")
    motion_mean = float(sig["motion_n"][a:b].mean())
    if motion_mean >= 0.5:
        triggers.append(f"high on-screen motion (normalised {motion_mean:.2f})")
    cuts = int(sig["scene"][a:b].sum())
    if cuts >= 2:
        triggers.append(f"{cuts} scene changes")
    if not triggers:
        triggers.append(f"excitement peak at {anchor['t']:.1f}s (motion/audio/scene composite)")
    return {
        "anchor": anchor,
        "anchor_position": round((anchor["t"] - start) / max(end - start, 1e-6), 3),
        "triggers": triggers,
        "events": [{"t": e["t"], "kind": e["kind"], "detail": e.get("detail", "")} for e in w["events"]],
        "metrics": {
            "motion_mean_norm": round(motion_mean, 3),
            "loudness_peak_dbfs": round(float(sig["audio_db"][a:b].max()), 1),
            "scene_changes": cuts,
            "black_fraction": w["black_fraction"],
            "static_fraction": w["static_fraction"],
        },
        "score_parts": {"base": w["base"], "event": w["event"]},
    }


def select_windows(sig: dict, events: list[dict], clip_cfg: dict) -> list[dict]:
    """Pure function: signals + events -> chosen windows (sorted by start)."""
    dur = sig["duration"]
    mn, mx = float(clip_cfg.get("min_seconds", 10)), float(clip_cfg.get("max_seconds", 30))
    tgt = float(clip_cfg.get("target_seconds", (mn + mx) / 2))
    max_clips = int(clip_cfg.get("max_clips_per_recording", 5))
    min_score = float(clip_cfg.get("min_score", 0.25))
    black_max = float(clip_cfg.get("black_threshold", 0.10))
    if dur < mn:
        return []
    lengths = sorted({min(x, dur) for x in (mn, tgt, mx) if min(x, dur) >= mn})
    anchors = [{"type": "event", "t": e["t"], "kind": e["kind"]} for e in events if 0 <= e["t"] <= dur]
    anchors += [{"type": "peak", "t": round(t, 2)} for t in _peaks(sig, mn / 2, max_clips * 4)]
    fracs = (0.65, 0.6, 0.7, 0.55, 0.75, 0.5, 0.8)
    cands = []
    for anc in anchors:
        best = None
        for L in lengths:
            for fr in fracs:
                start = min(max(0.0, anc["t"] - fr * L), dur - L)
                start = round(start, 2)
                w = _score_window(sig, start, start + L, events, black_max)
                if w is None:
                    continue
                # prefer the 65% placement and the target length when scores are close
                key = w["score"] - 0.02 * abs(fr - 0.65) - 0.08 * abs(L - tgt) / max(tgt, 1)
                if best is None or key > best[0]:
                    best = (key, start, start + L, w)
        if best and best[3]["score"] >= min_score:
            cands.append({"key": best[0], "start": best[1], "end": best[2], "w": best[3], "anchor": anc})
    cands.sort(key=lambda c: (-c["key"], c["start"]))
    chosen = []
    for c in cands:
        if len(chosen) >= max_clips:
            break
        if all(c["end"] <= o["start"] or c["start"] >= o["end"] for o in chosen):
            chosen.append(c)
    out = []
    for c in sorted(chosen, key=lambda c: c["start"]):
        out.append({"start": c["start"], "end": round(c["end"], 2), "score": c["w"]["score"],
                    "reasons": _reasons(sig, c["anchor"], c["w"], c["start"], c["end"])})
    return out


# ---------------------------------------------------------------- cutting
def cut_clip(src: Path, start: float, end: float, out: Path, has_audio: bool) -> Path:
    cmd = [FFMPEG, "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
           "-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out.exists():
        raise ClipError(f"cut failed: {res.stderr.strip()[:300]}")
    return out


def _set_status(conn, rid, status, error=None):
    conn.execute("UPDATE recordings SET status=?, error=?, updated_at=? WHERE id=?",
                 (status, error, db.now(), rid))
    conn.commit()


def find_clips(settings, conn: sqlite3.Connection, recording_id: int) -> list[int]:
    """Analyse a recording, cut the best windows into clips/, return clip ids.

    Idempotent: if clips already exist for the recording their ids are returned.
    No qualifying window -> recording status 'review' and [] returned."""
    rec = conn.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
    if rec is None:
        raise ClipError(f"no recording {recording_id}")
    existing = [r["id"] for r in conn.execute(
        "SELECT id FROM clips WHERE recording_id=? ORDER BY start", (recording_id,))]
    if existing:
        return existing
    src = Path(rec["path"])
    cfg = settings.section("clipping")
    try:
        info = probe(src)
    except ProbeError as e:
        _set_status(conn, recording_id, "failed", str(e))
        raise
    _set_status(conn, recording_id, "processing")
    events = [dict(r) for r in conn.execute(
        "SELECT t, kind, detail FROM events WHERE recording_id=? ORDER BY t", (recording_id,))]
    try:
        sig = analyze(src, info, fps=float(cfg.get("analysis_fps", 4)),
                      sample_w=int(cfg.get("analysis_width", 160)))
        windows = select_windows(sig, events, cfg)
    except Exception as e:  # noqa: BLE001 - record and re-raise
        _set_status(conn, recording_id, "failed", f"analysis: {e}")
        raise
    if not windows:
        _set_status(conn, recording_id, "review", "no window reached min_score")
        _log.info("recording %s: no clip qualified -> review", recording_id)
        return []
    out_dir = settings.path("clips")
    ids = []
    for i, w in enumerate(windows):
        out = out_dir / f"rec{recording_id}_{i + 1:02d}_{int(w['start'] * 1000):08d}.mp4"
        try:
            cut_clip(src, w["start"], w["end"], out, bool(info["has_audio"]))
        except ClipError as e:
            _log.error("recording %s clip %s: %s", recording_id, i, e)
            continue
        cur = conn.execute(
            "INSERT INTO clips(recording_id,start,end,score,reasons,path,status,created_at) "
            "VALUES(?,?,?,?,?,?, 'candidate', ?)",
            (recording_id, w["start"], w["end"], w["score"], json.dumps(w["reasons"]),
             str(out.resolve()), db.now()))
        ids.append(cur.lastrowid)
    conn.commit()
    if not ids:
        _set_status(conn, recording_id, "failed", "all clip cuts failed")
    _log.info("recording %s: %d clips %s", recording_id, len(ids),
              [(w["start"], w["end"], w["score"]) for w in windows])
    return ids
