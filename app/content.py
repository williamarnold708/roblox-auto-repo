"""Generate hooks / captions / hashtags for a clip, with guardrails.

Entry point: ``generate(settings, conn, clip_id) -> content_id``.

Output is upserted into the ``content`` table (clip_id UNIQUE) and mirrored
to ``clips/<clip_id>.content.json`` for recovery. ``content.hooks`` is a JSON
list of strings, ``chosen_hook`` an index into it, ``hashtags`` a JSON list of
"#tag" strings, ``caption`` excludes hashtags (the publisher appends them).
"""
from __future__ import annotations

import difflib
import json
import random
import re
import sqlite3
import unicodedata
from pathlib import Path

from app import db, log
from app.providers import (DEFAULT_CTA, TemplateProvider, fill, get_provider,
                           grounded_rationale, load_templates)

_log = log.get("content")

MAX_HOOK = 60
MAX_ON_SCREEN = 40
MAX_CAPTION = 150
MAX_VOICEOVER = 220
DEDUPE_RATIO = 0.85
EPSILON = 0.15

EVENT_KINDS = {"death", "victory", "checkpoint", "rare_item", "boss_defeat", "high_score",
               "unexpected"}
SIGNAL_ALIASES = {
    "motion": "motion_peak", "motion_peak": "motion_peak", "high_motion": "motion_peak",
    "loud": "loud_audio", "loud_audio": "loud_audio", "audio_peak": "loud_audio",
    "audio": "loud_audio", "scene_change": "scene_changes", "scene_changes": "scene_changes",
    "scenes": "scene_changes", "win": "victory", "boss": "boss_defeat", "rare": "rare_item",
    "highscore": "high_score", "died": "death",
}

# Phrases that assert a specific event happened -> the event kind required.
CLAIMS = {
    "boss_defeat": re.compile(r"\bboss", re.I),
    "victory": re.compile(r"\b(won|win|wins|winning|winner|victory|victorious|finish\w*|"
                          r"made it to the end|beat the (game|level|obby|map))\b", re.I),
    "rare_item": re.compile(r"\b(rare|legendary|mythic(al)?|drop|lucky (pull|find))\b", re.I),
    "high_score": re.compile(r"\b(high ?score|new record|personal best|score)\b", re.I),
    "death": re.compile(r"\b(die|died|dies|dying|death|dead|killed|eliminated|respawn\w*)\b", re.I),
    "checkpoint": re.compile(r"\bcheck ?points?\b", re.I),
}
# Unverifiable superlatives - never allowed.
UNVERIFIABLE = re.compile(r"(world record|first ever|#1\b|number one|\bimpossible\b|"
                          r"\b100 ?%|nobody has ever|best game ever)", re.I)
# Engagement bait - never allowed (TikTok down-ranks it and it is spammy).
BAIT = [
    r"like if", r"like for", r"like and (follow|share|subscribe)", r"follow for (part|more)",
    r"part \d+ (coming|if)", r"comment (if|below|for)", r"share (if|this with)", r"tag a friend",
    r"smash (that|the)", r"don'?t scroll", r"stop scrolling", r"wait (till|until|for) the end",
    r"watch (till|until) the end", r"subscribe", r"double tap", r"\bgiveaway\b", r"free robux",
    r"hit (the )?follow", r"if you'?re reading this",
]
BAIT_RE = re.compile("|".join(BAIT), re.I)
BANNED_TAGS = {"fyp", "foryou", "foryoupage", "fy", "fypシ", "viral", "viralvideo", "trending",
               "trend", "xyzbca", "explore", "explorepage", "blowthisup", "goviral", "tiktok",
               "4u", "parati", "fypage", "foryourpage", "viraltiktok", "freerobux", "robuxgiveaway"}


# ---------------------------------------------------------------- context
def _jl(text, default):
    if text is None or text == "":
        return default
    try:
        return json.loads(text) if isinstance(text, str) else text
    except (TypeError, ValueError):
        return default


def parse_reasons(raw) -> list[str]:
    """Normalise clips.reasons (list of str / list of dict / dict) to trigger kinds."""
    data = _jl(raw, [])
    items: list[str] = []

    def add(x):
        if isinstance(x, str):
            items.append(x)
        elif isinstance(x, dict):
            for k in ("kind", "type", "trigger", "reason", "name"):
                if isinstance(x.get(k), str):
                    items.append(x[k])
                    break
        elif isinstance(x, list):
            for y in x:
                add(y)

    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("events", "event_kinds", "triggers", "reasons"):
                add(v)
            elif v:  # e.g. {"motion_peak": 0.8}
                items.append(k)
    else:
        add(data)
    out = []
    for s in items:
        s = s.strip().lower().replace("-", "_").replace(" ", "_")
        s = s.split(":", 1)[1] if s.startswith(("event:", "event_", "evt:")) and ":" in s else s
        s = s[len("event_"):] if s.startswith("event_") else s
        s = SIGNAL_ALIASES.get(s, s)
        if s and s not in out:
            out.append(s)
    return out


def build_context(settings, conn: sqlite3.Connection, clip_id: int) -> dict:
    clip = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if clip is None:
        raise ValueError(f"clip {clip_id} not found")
    rec = conn.execute("SELECT * FROM recordings WHERE id=?", (clip["recording_id"],)).fetchone()
    game = None
    if rec is not None and rec["game_id"] is not None:
        game = conn.execute("SELECT * FROM games WHERE id=?", (rec["game_id"],)).fetchone()
    g = dict(game) if game else {}
    game_ctx = {
        "name": (g.get("name") or "this Roblox game").strip(),
        "slug": g.get("slug"), "url": g.get("url"),
        "description": g.get("description") or "", "genre": (g.get("genre") or "").strip(),
        "audience": g.get("audience") or "", "cta": (g.get("cta") or "").strip(),
        "avoid_words": [w for w in _jl(g.get("avoid_words"), []) if isinstance(w, str) and w.strip()],
        "hashtags": [h for h in _jl(g.get("hashtags"), []) if isinstance(h, str)],
    }
    start, end = float(clip["start"]), float(clip["end"])
    events = [{"t": round(float(r["t"]) - start, 2), "kind": r["kind"], "detail": r["detail"] or ""}
              for r in conn.execute("SELECT t, kind, detail FROM events WHERE recording_id=? "
                                    "AND t>=? AND t<=? ORDER BY t", (clip["recording_id"], start, end))]
    triggers = parse_reasons(clip["reasons"])
    for e in events:
        if e["kind"] not in triggers:
            triggers.insert(0, e["kind"])
    cfg = settings.section("content")
    cta = game_ctx["cta"] or DEFAULT_CTA
    return {
        "game": game_ctx,
        "clip": {"id": clip_id, "start": start, "end": end, "duration": round(end - start, 2),
                 "score": clip["score"]},
        "events": events, "triggers": triggers, "cta": cta,
        "hooks_per_clip": int(cfg.get("hooks_per_clip", 4)),
        "max_hashtags": int(cfg.get("max_hashtags", 5)),
        "seed": int(cfg.get("seed", 0)) * 100003 + clip_id,
        "variant": 0,
    }


def evidence(context: dict) -> set[str]:
    """Event kinds the context proves happened (logged events + event-kind reasons)."""
    ev = {e["kind"] for e in context.get("events", [])}
    ev |= {t for t in context.get("triggers", []) if t in EVENT_KINDS}
    # A game's own name can contain a claim word ("Boss Rush Obby") - that's not a claim.
    return ev


# ---------------------------------------------------------------- guardrails
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")
_ASCII_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", "…": "..."})


def strip_emoji(text: str) -> str:
    """Remove emoji / pictographs for drawtext (DejaVu lacks colour emoji)."""
    text = _EMOJI_RE.sub("", (text or "").translate(_ASCII_MAP))
    text = "".join(c for c in text if unicodedata.category(c) not in ("So", "Cs", "Co", "Cn"))
    return re.sub(r"\s{2,}", " ", text).strip()


def strip_words(text: str, words) -> str:
    for w in words:
        text = re.sub(rf"(?i)(?<!\w)#?{re.escape(w.strip())}(?!\w)", "", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -,")


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,.-:;")
    return cut if cut else text[:limit]


def unsupported_claims(text: str, context: dict) -> list[str]:
    """Event kinds that ``text`` asserts but the context doesn't support."""
    ev = evidence(context)
    name = context.get("game", {}).get("name", "")
    probe = re.sub(re.escape(name), " ", text, flags=re.I) if name else text
    bad = [k for k, rx in CLAIMS.items() if k not in ev and rx.search(probe)]
    if UNVERIFIABLE.search(probe):
        bad.append("unverifiable")
    return bad


def is_clean(text: str, context: dict) -> bool:
    return bool(text) and not BAIT_RE.search(text) and not unsupported_claims(text, context)


def normalize_tag(tag: str) -> str:
    t = re.sub(r"[^\w]", "", str(tag).lstrip("#"), flags=re.UNICODE)
    return f"#{t.lower()}" if t else ""


def build_hashtags(context: dict, suggested=()) -> list[str]:
    """Game tags first, then #roblox, genre, game name; LLM suggestions only if
    they are made of words from the game's own name/genre/description."""
    g = context["game"]
    avoid = {w.lower().strip().lstrip("#") for w in g.get("avoid_words", [])}
    genre = re.sub(r"\W", "", g.get("genre", "").lower())
    cands = list(g.get("hashtags", []))
    name_tag = re.sub(r"\W", "", g.get("name", "")) if g.get("slug") or g.get("url") else ""
    name_tag = name_tag if len(name_tag) <= 29 else ""
    cands += ["#roblox", f"#{name_tag}" if name_tag else ""]
    if genre:
        cands += [f"#roblox{genre}", f"#{genre}"]
    vocab = set(re.findall(r"\w+", f"{g.get('name','')} {g.get('genre','')} {g.get('description','')}"
                           .lower())) | {"roblox", "gaming", "robloxgame", "robloxgames"}
    for s in suggested or []:
        t = normalize_tag(s)[1:]
        if t and (t in vocab or t.replace("roblox", "") in vocab):
            cands.append(t)
    out: list[str] = []
    for c in cands:
        t = normalize_tag(c)
        if (not t or t[1:] in BANNED_TAGS or t[1:] in avoid or t in out or len(t) > 30
                or any(a and a in t[1:] for a in avoid)):
            continue
        out.append(t)
    return out[: context.get("max_hashtags", 5)]


def apply_guardrails(draft: dict, context: dict) -> dict:
    """Make any provider's draft safe. Falls back to template text per field."""
    g = context["game"]
    avoid = g.get("avoid_words", [])
    tmpl = TemplateProvider().generate({**context, "variant": context.get("variant", 0)})

    def clean(text: str, limit: int, emoji_ok: bool) -> str:
        text = strip_words(str(text or ""), avoid)
        if not emoji_ok:
            text = strip_emoji(text)
        text = re.sub(r"#\w+", "", text) if not emoji_ok else text
        return truncate(re.sub(r"\s{2,}", " ", text).strip(), limit)

    hooks, ids = [], []
    src_ids = draft.get("hook_templates") or [f"llm:{i}" for i in range(len(draft.get("hooks", [])))]
    pairs = list(zip(draft.get("hooks", []), src_ids)) + list(zip(tmpl["hooks"], tmpl["hook_templates"]))
    for h, tid in pairs:
        h = clean(h, MAX_HOOK, emoji_ok=False)
        if is_clean(h, context) and h.lower() not in {x.lower() for x in hooks} and len(h) >= 3:
            hooks.append(h)
            ids.append(tid)
        if len(hooks) >= context.get("hooks_per_clip", 4):
            break
    if not hooks:  # last resort - always safe
        hooks, ids = [truncate(strip_words(f"This is {g['name']}", avoid) or "Roblox gameplay",
                               MAX_HOOK)], ["safe:0"]

    def field(key, limit, emoji_ok):
        for cand in (draft.get(key), tmpl[key]):
            c = clean(cand, limit, emoji_ok)
            if is_clean(c, context):
                return c
        return ""

    on_screen = field("on_screen_text", MAX_ON_SCREEN, False) or truncate(strip_emoji(hooks[0]), MAX_ON_SCREEN)
    cta = strip_words(str(draft.get("cta") or context.get("cta") or DEFAULT_CTA), avoid)
    cta = cta if is_clean(cta, context) else DEFAULT_CTA
    cta = truncate(cta, 60)
    caption = field("caption", MAX_CAPTION, True)
    caption = re.sub(r"\s#\w+", "", caption).strip()  # hashtags live in their own column
    if g["name"].lower() not in caption.lower():
        caption = f"{g['name']}: {caption}"
    if cta.lower() not in caption.lower() and "link in bio" not in caption.lower():
        caption = f"{caption.rstrip()} {cta}"
    if len(caption) > MAX_CAPTION:  # keep game name + CTA, shorten the middle
        body = caption.replace(cta, "").strip()
        caption = f"{truncate(body, MAX_CAPTION - len(cta) - 1)} {cta}"
    voiceover = field("voiceover", MAX_VOICEOVER, False)
    sentences = re.split(r"(?<=[.!?])\s+", voiceover)
    voiceover = " ".join(sentences[:2]).strip()

    return {
        "hooks": hooks, "hook_templates": ids, "on_screen_text": on_screen,
        "caption": caption, "hashtags": build_hashtags(context, draft.get("hashtags")),
        "voiceover": voiceover, "cta": cta, "rationale": grounded_rationale(context),
        "provider": draft.get("provider", "unknown"),
    }


def recent_captions(conn: sqlite3.Connection, clip_id: int, n: int = 50) -> list[str]:
    return [r["caption"] for r in conn.execute(
        "SELECT caption FROM content WHERE clip_id<>? AND caption IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (clip_id, n))]


def too_similar(caption: str, previous: list[str], ratio: float = DEDUPE_RATIO) -> bool:
    c = caption.lower()
    return any(difflib.SequenceMatcher(None, c, p.lower()).ratio() >= ratio for p in previous)


# ---------------------------------------------------------------- hook selection
def _template_regexes(game_name: str) -> list[tuple[str, re.Pattern]]:
    t = load_templates()
    ctx = {"game": {"name": "\x00", "genre": "\x01"}, "cta": "\x02"}
    out = []
    groups = [("", t["hooks"])] + [("genre.", {k: v}) for k, v in t.get("genre_hooks", {}).items()]
    for prefix, table in groups:
        for key, tpls in table.items():
            for i, tpl in enumerate(tpls):
                s = re.escape(fill(tpl, ctx)).replace("\x00", ".+?").replace("\x01", ".*?")
                out.append((f"{prefix}{key}:{i}", re.compile(f"^{s}$", re.I)))
    return out


def template_id_for(hook: str, regexes=None) -> str | None:
    for tid, rx in regexes or _template_regexes(""):
        if rx.match(hook.strip()):
            return tid
    return None


def hook_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """Average performance per hook template of the hooks actually used.

    Joins metrics -> posts -> renders -> content, takes the latest metrics row
    per post and scores it by completion_rate (or engagement rate if missing).
    Returns {} when there is no real data (then chosen_hook stays 0)."""
    rows = conn.execute("""
        SELECT c.hooks, c.chosen_hook, m.views, m.likes, m.comments, m.shares,
               m.completion_rate
        FROM metrics m JOIN posts p ON p.id = m.post_id
        JOIN renders r ON r.id = p.render_id JOIN content c ON c.id = r.content_id
        WHERE m.id = (SELECT m2.id FROM metrics m2 WHERE m2.post_id = m.post_id
                      ORDER BY m2.captured_at DESC, m2.id DESC LIMIT 1)""").fetchall()
    if not rows:
        return {}
    regexes = _template_regexes("")
    acc: dict[str, list[float]] = {}
    for r in rows:
        hooks = _jl(r["hooks"], [])
        idx = r["chosen_hook"] or 0
        if not hooks or idx >= len(hooks):
            continue
        tid = template_id_for(hooks[idx], regexes)
        if tid is None:
            continue
        if r["completion_rate"] is not None:
            score = float(r["completion_rate"])
        elif r["views"]:
            score = ((r["likes"] or 0) + (r["comments"] or 0) + (r["shares"] or 0)) / r["views"]
        else:
            continue
        acc.setdefault(tid, []).append(score)
    return {k: sum(v) / len(v) for k, v in acc.items()}


def choose_hook(hook_ids: list[str], weights: dict[str, float], seed, epsilon: float = EPSILON) -> int:
    """Epsilon-greedy over hook templates. No data -> 0. Deterministic per seed."""
    if not weights or not hook_ids:
        return 0
    rng = random.Random(f"choose:{seed}")
    if rng.random() < epsilon:
        return rng.randrange(len(hook_ids))
    prior = sum(weights.values()) / len(weights)  # unseen templates compete at the mean
    scores = [weights.get(h, prior) for h in hook_ids]
    return max(range(len(scores)), key=lambda i: (scores[i], -i))


# ---------------------------------------------------------------- main
def generate(settings, conn: sqlite3.Connection, clip_id: int, provider=None) -> int:
    context = build_context(settings, conn, clip_id)
    provider = provider or get_provider(settings)
    previous = recent_captions(conn, clip_id)
    result = None
    for variant in range(8):
        context["variant"] = variant
        # Only the first attempt uses the (possibly slow) LLM; retries vary templates.
        draft = (provider if variant == 0 else TemplateProvider()).generate(context)
        result = apply_guardrails(draft, context)
        if not too_similar(result["caption"], previous):
            break
    else:
        # Still near-identical: add a grounded, clip-specific detail.
        tail = f" ({context['clip']['duration']:.0f}s clip #{clip_id})"
        result["caption"] = truncate(result["caption"], MAX_CAPTION - len(tail)) + tail
    chosen = choose_hook(result["hook_templates"], hook_weights(conn), context["seed"])
    payload = {**result, "clip_id": clip_id, "chosen_hook": chosen,
               "context": {k: context[k] for k in ("clip", "events", "triggers")}}
    now = db.now()
    conn.execute("""
        INSERT INTO content (clip_id, hooks, on_screen_text, caption, hashtags, voiceover, cta,
                             rationale, provider, chosen_hook, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(clip_id) DO UPDATE SET hooks=excluded.hooks,
            on_screen_text=excluded.on_screen_text, caption=excluded.caption,
            hashtags=excluded.hashtags, voiceover=excluded.voiceover, cta=excluded.cta,
            rationale=excluded.rationale, provider=excluded.provider,
            chosen_hook=excluded.chosen_hook, created_at=excluded.created_at""",
        (clip_id, db.dumps(result["hooks"]), result["on_screen_text"], result["caption"],
         db.dumps(result["hashtags"]), result["voiceover"], result["cta"], result["rationale"],
         result["provider"], chosen, now))
    conn.commit()
    content_id = conn.execute("SELECT id FROM content WHERE clip_id=?", (clip_id,)).fetchone()["id"]
    try:
        out = Path(settings.path("clips")) / f"{clip_id}.content.json"
        out.write_text(json.dumps({**payload, "content_id": content_id, "created_at": now},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        _log.warning("could not write content json for clip %s: %s", clip_id, e)
    _log.info("content %s for clip %s via %s (hook %d)", content_id, clip_id, result["provider"], chosen)
    return content_id
