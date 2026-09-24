import json
import re
from unittest import mock

import pytest
import requests

from app import config, content, db, events, providers


@pytest.fixture
def env(tmp_path):
    settings = config.load(tmp_path)
    settings.raw["content"]["provider"] = "template"
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO games (id, slug, name, description, genre, audience, cta, avoid_words, "
                 "hashtags) VALUES (1,'lava-obby','Lava Obby','Jump over lava to reach the top',"
                 "'obby','kids 9-13','Play Lava Obby free - link in bio',?,?)",
                 (json.dumps(["noob", "Easy"]), json.dumps(["#lavaobby", "#fyp", "obbygame"])))
    conn.execute("INSERT INTO recordings (id, game_id, path, sha256) VALUES (1,1,'r.mkv','abc')")
    conn.commit()
    return settings, conn


def add_clip(conn, reasons, start=10.0, end=30.0, evs=()):
    cur = conn.execute("INSERT INTO clips (recording_id, start, end, score, reasons) VALUES (1,?,?,?,?)",
                       (start, end, 0.8, json.dumps(reasons)))
    for t, kind, detail in evs:
        conn.execute("INSERT INTO events (recording_id, t, kind, detail) VALUES (1,?,?,?)", (t, kind, detail))
    conn.commit()
    return cur.lastrowid


def row(conn, cid):
    return conn.execute("SELECT * FROM content WHERE id=?", (cid,)).fetchone()


def all_text(r):
    return " ".join(json.loads(r["hooks"]) + [r["on_screen_text"], r["caption"], r["voiceover"]])


def test_template_output_schema(env):
    settings, conn = env
    clip = add_clip(conn, ["motion_peak", "loud_audio"], evs=[(15.0, "death", "fell in lava")])
    cid = content.generate(settings, conn, clip)
    r = row(conn, cid)
    hooks = json.loads(r["hooks"])
    assert 3 <= len(hooks) <= 5 and len(hooks) == settings.section("content")["hooks_per_clip"]
    assert all(isinstance(h, str) and 0 < len(h) <= content.MAX_HOOK for h in hooks)
    assert 0 < len(r["on_screen_text"]) <= 40
    assert len(r["caption"]) <= 150 and "Lava Obby" in r["caption"] and "link in bio" in r["caption"]
    assert r["voiceover"] and r["cta"] and r["rationale"]
    assert "death" in r["rationale"]
    assert r["provider"] == "template"
    assert 0 <= r["chosen_hook"] < len(hooks)
    tags = json.loads(r["hashtags"])
    assert "#roblox" in tags and "#lavaobby" in tags
    # recovery file
    saved = json.loads((settings.path("clips") / f"{clip}.content.json").read_text())
    assert saved["hooks"] == hooks and saved["content_id"] == cid


def test_deterministic_and_upsert(env):
    settings, conn = env
    clip = add_clip(conn, ["scene_changes"])
    a = content.generate(settings, conn, clip)
    first = dict(row(conn, a))
    b = content.generate(settings, conn, clip)
    assert a == b  # upsert, same row
    second = dict(row(conn, b))
    assert first["hooks"] == second["hooks"] and first["caption"] == second["caption"]
    assert conn.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 1


def test_no_invented_events(env):
    settings, conn = env
    clip = add_clip(conn, ["motion_peak"])  # no events at all
    r = row(conn, content.generate(settings, conn, clip))
    text = all_text(r)
    for kind, rx in content.CLAIMS.items():
        assert not rx.search(text), (kind, text)
    # A dishonest LLM draft gets its claims removed.
    ctx = content.build_context(settings, conn, clip)
    draft = {"hooks": ["Boss defeated in one hit!", "New world record!", "Fast parkour"],
             "on_screen_text": "BOSS DOWN", "caption": "I won the game and got a legendary drop",
             "voiceover": "We killed the boss.", "hashtags": [], "provider": "fake"}
    out = content.apply_guardrails(draft, ctx)
    joined = " ".join(out["hooks"] + [out["on_screen_text"], out["caption"], out["voiceover"]])
    assert "Fast parkour" in out["hooks"]
    assert not re.search(r"boss|won|legendary|record|killed", joined, re.I), joined


def test_real_event_can_be_mentioned(env):
    settings, conn = env
    clip = add_clip(conn, [], evs=[(20.0, "boss_defeat", "Lava King")])
    r = row(conn, content.generate(settings, conn, clip))
    assert re.search(r"boss", all_text(r), re.I)


def test_avoid_words_bait_and_emoji(env):
    settings, conn = env
    clip = add_clip(conn, ["motion_peak"])
    ctx = content.build_context(settings, conn, clip)
    draft = {"hooks": ["Like if you are not a noob \U0001F525", "Easy jumps \U0001F525 fast",
                       "Follow for part 2", "Wait till the end"],
             "on_screen_text": "SO FAST \U0001F525\U0001F525", "caption": "Easy mode \U0001F525 Lava Obby",
             "voiceover": "Too easy. Follow for more.", "hashtags": ["#fyp", "#viral", "#lava"],
             "provider": "fake"}
    out = content.apply_guardrails(draft, ctx)
    joined = " ".join(out["hooks"] + [out["on_screen_text"], out["caption"], out["voiceover"]])
    assert not re.search(r"\bnoob\b|\beasy\b", joined, re.I)
    assert not content.BAIT_RE.search(joined)
    assert all(ord(c) < 0x2000 for c in "".join(out["hooks"]) + out["on_screen_text"])
    assert "fast" in " ".join(out["hooks"]).lower()
    assert "#fyp" not in out["hashtags"] and "#viral" not in out["hashtags"]
    tags = content.build_hashtags({**ctx, "max_hashtags": 10}, draft["hashtags"] + ["#minecraft"])
    assert "#lava" in tags and "#minecraft" not in tags  # only game-relevant suggestions


def test_hashtag_cap(env):
    settings, conn = env
    conn.execute("UPDATE games SET hashtags=?", (json.dumps([f"#tag{i}" for i in range(12)]),))
    settings.raw["content"]["max_hashtags"] = 3
    clip = add_clip(conn, ["motion_peak"])
    tags = json.loads(row(conn, content.generate(settings, conn, clip))["hashtags"])
    assert len(tags) == 3 and all(t.startswith("#") for t in tags)


def test_caption_dedupe(env):
    settings, conn = env
    captions = set()
    for i in range(4):
        clip = add_clip(conn, ["motion_peak"], start=100 + i * 40, end=120 + i * 40)
        captions.add(row(conn, content.generate(settings, conn, clip))["caption"])
    assert len(captions) == 4
    assert all(len(c) <= 150 for c in captions)


def test_ollama_fallback_when_unreachable(env):
    settings, conn = env
    settings.raw["content"]["provider"] = "auto"
    with mock.patch("app.providers.requests.get", side_effect=requests.ConnectionError):
        assert isinstance(providers.get_provider(settings), providers.TemplateProvider)
    settings.raw["content"]["provider"] = "ollama"
    clip = add_clip(conn, ["loud_audio"])
    with mock.patch("app.providers.requests.post", side_effect=requests.ConnectionError("down")):
        r = row(conn, content.generate(settings, conn, clip))
    assert r["provider"].startswith("template(fallback:ollama")
    assert len(json.loads(r["hooks"])) == 4


def test_ollama_valid_and_invalid_json(env):
    settings, conn = env
    ctx = content.build_context(settings, conn, add_clip(conn, ["motion_peak"]))
    good = {"hooks": ["Speedrunning the lava", "Can you keep up?", "Parkour at full pace"],
            "on_screen_text": "FULL SPEED", "caption": "Lava Obby at full speed", "hashtags": ["#obby"],
            "voiceover": "This is Lava Obby.", "cta": "Link in bio", "rationale": "fast motion"}
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"response": json.dumps(good)}
    p = providers.OllamaProvider()
    with mock.patch("app.providers.requests.post", return_value=resp):
        assert p.generate(ctx)["provider"] == "ollama"
    resp.json.return_value = {"response": json.dumps({"hooks": []})}
    with mock.patch("app.providers.requests.post", return_value=resp):
        assert p.generate(ctx)["provider"].startswith("template")


def test_paid_providers_need_explicit_opt_in(env, monkeypatch):
    settings, _ = env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.raw["content"]["provider"] = "anthropic"
    assert isinstance(providers.get_provider(settings), providers.TemplateProvider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    settings.raw["content"]["provider"] = "auto"
    with mock.patch("app.providers.requests.get", side_effect=requests.ConnectionError):
        assert isinstance(providers.get_provider(settings), providers.TemplateProvider)


def test_hook_weights_and_choice(env):
    settings, conn = env
    assert content.hook_weights(conn) == {}
    assert content.choose_hook(["a", "b"], {}, 1) == 0
    clip = add_clip(conn, ["motion_peak"])
    cid = content.generate(settings, conn, clip)
    hooks = json.loads(row(conn, cid)["hooks"])
    conn.execute("UPDATE content SET chosen_hook=1 WHERE id=?", (cid,))
    conn.execute("INSERT INTO renders (id, clip_id, content_id) VALUES (1,?,?)", (clip, cid))
    conn.execute("INSERT INTO posts (id, render_id, mode) VALUES (1,1,'local')")
    conn.execute("INSERT INTO metrics (post_id, captured_at, views, completion_rate, source) "
                 "VALUES (1,'2026-01-01',100,0.6,'manual')")
    conn.commit()
    w = content.hook_weights(conn)
    tid = content.template_id_for(hooks[1])
    assert w == {tid: 0.6}
    ids = ["x:0", tid, "y:0"]
    picks = [content.choose_hook(ids, {tid: 0.6, "x:0": 0.1, "y:0": 0.2}, s) for s in range(50)]
    assert picks.count(1) > 35  # mostly exploit
    assert picks == [content.choose_hook(ids, {tid: 0.6, "x:0": 0.1, "y:0": 0.2}, s) for s in range(50)]


# ------------------------------------------------------------------ events
def test_parse_canonical_json():
    doc = {"session_start_utc": "2026-09-24T18:30:00Z",
           "events": [{"t": 5, "kind": "Death", "detail": "lava"}, {"t": -1, "kind": "x"},
                      {"t": 2.5, "kind": "recording_start"}, {"kind": "no_t"}]}
    p = events.parse(json.dumps(doc))
    assert [e["kind"] for e in p["events"]] == ["recording_start", "death"]
    assert p["skipped"] == 2
    assert events.sync_offset(p) == 2.5
    assert events.align(p["events"], 2.5) == [{"t": 2.5, "kind": "death", "detail": "lava"}]


def test_parse_raw_logger_lines(tmp_path):
    text = "\n".join([
        "18:29:59.001  Some other output",
        '18:30:00.000  [AutoPromoEvent] {"v":1,"t":0,"kind":"session_start","detail":"studio",'
        '"utc":"2026-09-24T18:30:00.000Z"}  -  Server - EventLogger:60',
        '18:30:03.000  [AutoPromoEvent] {"v":1,"t":3.0,"kind":"recording_start","detail":""}',
        '18:30:15.000  [AutoPromoEvent] {"v":1,"t":15.25,"kind":"boss_defeat","detail":"Lava King"}',
        '[AutoPromoEvent] {broken json',
    ])
    f = tmp_path / "2026-09-24 18-30-03.events.txt"
    f.write_text(text)
    p = events.parse(f)
    assert p["session_start_utc"].startswith("2026-09-24T18:30")
    assert p["skipped"] == 1
    aligned = events.align(p["events"], events.sync_offset(p), duration=60)
    assert aligned == [{"t": 12.25, "kind": "boss_defeat", "detail": "Lava King"}]


def test_offset_from_obs_filename_and_store(tmp_path):
    from datetime import timezone
    video = tmp_path / "2026-09-24 18-30-10.mkv"
    video.write_bytes(b"")
    (tmp_path / "2026-09-24 18-30-10.events.json").write_text(json.dumps(
        {"session_start_utc": "2026-09-24T18:30:00Z", "events": [{"t": 25, "kind": "victory"}]}))
    p = events.parse(events.find_sidecar(video))
    assert events.sync_offset(p, events.obs_filename_time(video, timezone.utc)) == 10.0
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO recordings (id, path, sha256) VALUES (1,'v','s')")
    assert events.load_for_recording(conn, 1, video, duration=60, tz=timezone.utc) == 1
    assert conn.execute("SELECT t, kind FROM events").fetchone()["t"] == 15.0


def test_parse_rejects_garbage():
    with pytest.raises(events.EventParseError):
        events.parse("hello world\nnothing here")
