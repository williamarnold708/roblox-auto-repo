import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import clipper, db, ingest, qc, render
from app.probe import FFMPEG, FFPROBE, ProbeError, probe


def _stream_info(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def _add_content(conn, clip_id, hooks=None, on_screen="Can you beat it?", chosen=0):
    hooks = hooks or ["This boss fight went WAY too fast", "Wait for the ending...", "No way this worked"]
    cur = conn.execute(
        "INSERT INTO content(clip_id,hooks,on_screen_text,caption,hashtags,chosen_hook,provider,created_at) "
        "VALUES(?,?,?,?,?,?, 'test', ?)",
        (clip_id, json.dumps(hooks), on_screen, "Beat the Lava Titan in 8 seconds",
         json.dumps(["#roblox"]), chosen, db.now()))
    conn.commit()
    return cur.lastrowid


def _ingest_and_clip(settings, conn):
    (rid,) = ingest.scan_inbox(settings, conn)
    return rid, clipper.find_clips(settings, conn, rid)


# ------------------------------------------------------------------ probe
def test_probe(sample_video, tmp_path):
    info = probe(sample_video)
    assert (info["width"], info["height"]) == (320, 180)
    assert info["has_audio"] is True and info["video_codec"] == "h264"
    assert abs(info["duration"] - 45) < 0.5 and info["fps"] == 15
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00not a video" * 500)
    with pytest.raises(ProbeError):
        probe(bad)
    with pytest.raises(ProbeError):
        probe(tmp_path / "missing.mp4")


# ------------------------------------------------------------------ ingest
def test_ingest_dedupe_and_reject(settings, conn, sample_inbox):
    (sample_inbox / "corrupt.mp4").write_bytes(b"garbage" * 1000)
    ids = ingest.scan_inbox(settings, conn)
    assert len(ids) == 1
    rec = conn.execute("SELECT * FROM recordings WHERE id=?", ids).fetchone()
    assert rec["status"] == "new" and rec["has_audio"] == 1 and rec["width"] == 320
    assert Path(rec["path"]).exists() and "processing" in rec["path"]
    assert Path(rec["thumbnail"]).exists()
    assert not (sample_inbox / "demo_session.mp4").exists()
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE recording_id=? ORDER BY t", ids)]
    assert kinds == ["boss_defeat", "death", "rare_item"]
    game = conn.execute("SELECT * FROM games WHERE slug='demo-obby'").fetchone()
    assert game["name"] == "Demo Obby (synthetic)" and "roblox" in json.loads(game["hashtags"])
    # corrupt file -> failed row + moved to _rejected
    bad = conn.execute("SELECT * FROM recordings WHERE status='failed'").fetchone()
    assert bad is not None and bad["error"]
    assert (settings.path("inbox") / "_rejected" / "demo-obby" / "corrupt.mp4").exists()
    # re-adding the same file (different name) is never processed again
    shutil.copy(rec["path"], sample_inbox / "copy_again.mp4")
    assert ingest.scan_inbox(settings, conn) == []
    assert conn.execute("SELECT COUNT(*) c FROM recordings").fetchone()["c"] == 2
    assert (settings.path("inbox") / "_duplicates" / "demo-obby" / "copy_again.mp4").exists()


def test_ingest_missing_game_json_and_growing_file(settings, conn, sample_video):
    folder = settings.path("inbox") / "Cool Tycoon"
    folder.mkdir()
    shutil.copy(sample_video, folder / "a.mp4")
    growing = folder / "b.mp4"
    growing.write_bytes(b"\x00" * 1000)
    stop = threading.Event()

    def writer():
        with open(growing, "ab") as f:
            while not stop.is_set():
                f.write(b"\x00" * 1000)
                f.flush()
                time.sleep(0.02)

    t = threading.Thread(target=writer)
    t.start()
    try:
        ids = ingest.scan_inbox(settings, conn, settle_seconds=0.3)
    finally:
        stop.set()
        t.join()
    assert len(ids) == 1
    assert growing.exists()  # still being written -> left alone
    game = conn.execute("SELECT g.* FROM games g JOIN recordings r ON r.game_id=g.id WHERE r.id=?",
                        ids).fetchone()
    assert game["name"] == "Cool Tycoon" and game["slug"] == "cool-tycoon"


# ------------------------------------------------------------------ clipper
def test_clipper_finds_clips_and_avoids_black(settings, conn, sample_inbox, sample_assets):
    rid, clip_ids = _ingest_and_clip(settings, conn)
    assert 1 <= len(clip_ids) <= 3
    cfg = settings.section("clipping")
    b0, b1 = sample_assets["black"]
    clips = [dict(r) for r in conn.execute("SELECT * FROM clips WHERE recording_id=? ORDER BY start", (rid,))]
    for c in clips:
        length = c["end"] - c["start"]
        assert cfg["min_seconds"] - 0.01 <= length <= cfg["max_seconds"] + 0.01
        overlap = max(0.0, min(c["end"], b1) - max(c["start"], b0))
        assert overlap <= cfg["black_threshold"] * length
        reasons = json.loads(c["reasons"])
        assert reasons["triggers"] and "metrics" in reasons
        assert 0.25 <= c["score"] <= 1.0
        assert Path(c["path"]).exists()
        assert abs(probe(c["path"])["duration"] - length) < 0.3
    for a, b in zip(clips, clips[1:]):
        assert a["end"] <= b["start"]  # non-overlapping
    all_kinds = {e["kind"] for c in clips for e in json.loads(c["reasons"])["events"]}
    assert "boss_defeat" in all_kinds
    assert clipper.find_clips(settings, conn, rid) == clip_ids  # idempotent


def test_clipper_boring_recording_goes_to_review(settings, conn, tmp_path):
    folder = settings.path("inbox") / "boring"
    folder.mkdir()
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:size=160x90:rate=10",
                    "-t", "14", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(folder / "idle.mp4")], check=True)
    (rid,) = ingest.scan_inbox(settings, conn)
    assert clipper.find_clips(settings, conn, rid) == []
    assert conn.execute("SELECT status FROM recordings WHERE id=?", (rid,)).fetchone()["status"] == "review"


# ------------------------------------------------------------------ render + qc
def test_render_and_qc_with_duplicate(settings, conn, sample_inbox):
    _, clip_ids = _ingest_and_clip(settings, conn)
    clip_id = clip_ids[0]
    content_id = _add_content(conn, clip_id)
    rid = render.render(settings, conn, clip_id, content_id)
    row = conn.execute("SELECT * FROM renders WHERE id=?", (rid,)).fetchone()
    streams = _stream_info(row["path"])
    v, a = streams["video"], streams["audio"]
    assert (v["width"], v["height"]) == (1080, 1920)
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p" and a["codec_name"] == "aac"
    rep = qc.check(settings, conn, rid)
    assert rep["passed"], rep["failures"]
    row = conn.execute("SELECT * FROM renders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "queued" and row["qc_passed"] == 1 and len(row["phash"]) == 48
    # the same footage clipped again (e.g. overlapping recording) with a different hook -> near-duplicate
    conn.execute("INSERT INTO clips(recording_id,start,end,score,reasons,path) "
                  "SELECT recording_id,start,end,score,reasons,path FROM clips WHERE id=?", (clip_id,))
    clip2 = conn.execute("SELECT MAX(id) m FROM clips").fetchone()["m"]
    content2 = _add_content(conn, clip2, hooks=["Totally different hook text"], on_screen=None)
    rid2 = render.render(settings, conn, clip2, content2)
    rep2 = qc.check(settings, conn, rid2)
    assert not rep2["passed"] and "near_duplicate" in rep2["failures"]
    assert rid in rep2["checks"]["near_duplicate"]["duplicates_of"]
    assert conn.execute("SELECT status FROM renders WHERE id=?", (rid2,)).fetchone()["status"] == "qc_failed"


def test_render_center_crop_without_audio_adds_silent_track(settings, conn, tmp_path):
    src = tmp_path / "silent.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15",
                    "-t", "7", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    conn.execute("INSERT INTO clips(recording_id,start,end,score,reasons,path) VALUES(NULL,0,7,0.5,'{}',?)",
                 (str(src),))
    clip_id = conn.execute("SELECT MAX(id) m FROM clips").fetchone()["m"]
    settings.raw["render"]["layout"] = "center_crop"
    long_hook = "This is an extremely long hook that absolutely must wrap across several lines " * 2
    cid = _add_content(conn, clip_id, hooks=[long_hook])
    rid = render.render(settings, conn, clip_id, cid)
    path = conn.execute("SELECT path FROM renders WHERE id=?", (rid,)).fetchone()["path"]
    info = probe(path)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    rep = qc.check(settings, conn, rid)
    assert rep["checks"]["text_safe_area"]["ok"] and rep["checks"]["audio"]["ok"]


def test_plan_text_respects_safe_area():
    W, H = 1080, 1920
    sa = render.safe_area(W, H)
    for hook in ["Hi", "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW", "a %{pts} 'quote' : colon \\ back" * 5,
                 "emoji \U0001F525 stripped"]:
        lines = render.plan_text(W, H, hook, "Follow for part 2 of this ridiculous obby run", 10)
        assert lines
        for ln in lines:
            assert sa["x0"] <= ln["x0"] and ln["x1"] <= sa["x1"]
            assert sa["y0"] <= ln["y0"] and ln["y1"] <= sa["y1"]
            assert "\U0001F525" not in ln["text"]
        assert sum(ln["role"] == "hook" for ln in lines) <= 3
