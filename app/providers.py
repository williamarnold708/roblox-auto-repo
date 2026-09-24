"""Text generation providers for clip content.

Every provider implements ``generate(context: dict) -> dict`` returning::

    {"hooks": [str, ...], "hook_templates": [str, ...], "on_screen_text": str,
     "caption": str, "hashtags": [str, ...], "voiceover": str, "cta": str,
     "rationale": str, "provider": str}

Providers only *draft* text. app/content.py always runs the guardrails
(no invented events, avoid-words, bait, hashtag spam, lengths) afterwards,
whatever the provider.

* TemplateProvider - default, free, deterministic (seeded), offline.
* OllamaProvider   - free local LLM (https://ollama.com) if the user runs it;
                     falls back to TemplateProvider on any failure.
* AnthropicProvider / OpenAIProvider - OPTIONAL, PAID. Only used when the
  settings ``[content].provider`` explicitly names them AND the API key env
  var is set. Never selected by "auto". Also fall back to templates.
"""
from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path

import requests

from app import log

_log = log.get("providers")

TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "templates" / "hooks.json"
DEFAULT_CTA = "Play it on Roblox - link in bio"
REQUIRED_KEYS = ("hooks", "on_screen_text", "caption", "voiceover")

_templates_cache: dict | None = None


def load_templates(path: Path | None = None) -> dict:
    global _templates_cache
    if path is None and _templates_cache is not None:
        return _templates_cache
    with open(path or TEMPLATES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if path is None:
        _templates_cache = data
    return data


class _SafeDict(dict):
    def __missing__(self, key):  # unknown placeholder -> empty, never KeyError
        return ""


def fill(template: str, context: dict) -> str:
    g = context.get("game", {})
    values = _SafeDict(
        game=g.get("name") or "this game",
        genre=g.get("genre") or "Roblox",
        cta=context.get("cta") or g.get("cta") or DEFAULT_CTA,
        detail=(context.get("events") or [{}])[0].get("detail") or "",
    )
    out = template.format_map(values)
    return re.sub(r"\s{2,}", " ", out).strip()


def ranked_triggers(context: dict, templates: dict | None = None) -> list[str]:
    """Context triggers ordered by the template priority list, 'generic' last."""
    t = templates or load_templates()
    prio = t.get("priority", [])
    trig = [x for x in context.get("triggers", []) if x in t["hooks"]]
    trig = sorted(set(trig), key=lambda k: prio.index(k) if k in prio else len(prio))
    return trig + ["generic"] if "generic" not in trig else trig


def grounded_rationale(context: dict, templates: dict | None = None) -> str:
    """Explain why the clip might hold attention using only real signals."""
    t = templates or load_templates()
    parts = []
    for ev in context.get("events", [])[:4]:
        why = t["rationale"].get(ev["kind"], "event logged by the Roblox logger")
        parts.append(f"{ev['kind']} at {ev['t']:.1f}s ({why})")
    for trig in ranked_triggers(context, t):
        if trig in {e["kind"] for e in context.get("events", [])} or trig == "generic":
            continue
        parts.append(t["rationale"].get(trig, trig))
    if not parts:
        parts.append(t["rationale"]["generic"])
    dur = context.get("clip", {}).get("duration")
    if dur:
        parts.append(f"{dur:.0f}s long, short enough to rewatch")
    return "; ".join(parts)[:400]


class Provider:
    name = "base"

    def generate(self, context: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class TemplateProvider(Provider):
    """Deterministic: same context + seed + variant -> same output."""
    name = "template"

    def __init__(self, templates: dict | None = None):
        self.t = templates or load_templates()

    def generate(self, context: dict) -> dict:
        seed = f"{context.get('seed', 0)}:{context.get('variant', 0)}"
        rng = random.Random(seed)
        n = int(context.get("hooks_per_clip", 4))
        triggers = ranked_triggers(context, self.t)
        genre = (context.get("game", {}).get("genre") or "").strip().lower()

        # Round-robin over triggers (strongest first) so the hook set is varied.
        pools = []
        for trig in triggers:
            items = [(f"{trig}:{i}", s) for i, s in enumerate(self.t["hooks"][trig])]
            rng.shuffle(items)
            pools.append(items)
        if genre in self.t.get("genre_hooks", {}):
            items = [(f"genre.{genre}:{i}", s) for i, s in enumerate(self.t["genre_hooks"][genre])]
            rng.shuffle(items)
            pools.insert(min(1, len(pools)), items)
        hooks, ids, seen = [], [], set()
        while len(hooks) < n and any(pools):
            for pool in pools:
                if pool and len(hooks) < n:
                    tid, tpl = pool.pop(0)
                    text = fill(tpl, context)
                    if text.lower() not in seen:
                        seen.add(text.lower())
                        hooks.append(text)
                        ids.append(tid)

        top = triggers[0]
        pick = lambda key: rng.choice(self.t[key].get(top) or self.t[key]["generic"])  # noqa: E731
        return {
            "hooks": hooks, "hook_templates": ids,
            "on_screen_text": fill(pick("on_screen"), context),
            "caption": fill(pick("captions"), context),
            "hashtags": [],  # built by content.build_hashtags from game data
            "voiceover": fill(pick("voiceover"), context),
            "cta": context.get("cta") or DEFAULT_CTA,
            "rationale": grounded_rationale(context, self.t),
            "provider": self.name,
        }


def validate_output(data) -> dict:
    """Validate/normalise an LLM JSON response. Raises ValueError if unusable."""
    if not isinstance(data, dict):
        raise ValueError("not an object")
    for k in REQUIRED_KEYS:
        if k not in data:
            raise ValueError(f"missing {k}")
    hooks = data["hooks"]
    if isinstance(hooks, str):
        hooks = [hooks]
    if not isinstance(hooks, list):
        raise ValueError("hooks must be a list")
    hooks = [h.strip() for h in hooks if isinstance(h, str) and h.strip()]
    if not hooks:
        raise ValueError("no hooks")
    for k in ("on_screen_text", "caption", "voiceover"):
        if not isinstance(data[k], str) or not data[k].strip():
            raise ValueError(f"{k} must be a non-empty string")
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    return {
        "hooks": hooks, "hook_templates": [f"llm:{i}" for i in range(len(hooks))],
        "on_screen_text": data["on_screen_text"].strip(),
        "caption": data["caption"].strip(),
        "hashtags": [str(x) for x in tags if isinstance(x, (str, int))],
        "voiceover": data["voiceover"].strip(),
        "cta": str(data.get("cta") or "").strip(),
        "rationale": str(data.get("rationale") or "").strip(),
    }


def build_prompt(context: dict) -> str:
    g = context.get("game", {})
    facts = {
        "game": g.get("name"), "description": g.get("description"), "genre": g.get("genre"),
        "audience": g.get("audience"), "cta": context.get("cta"),
        "clip_seconds": context.get("clip", {}).get("duration"),
        "logged_events_in_clip": [{"t": e["t"], "kind": e["kind"], "detail": e.get("detail")}
                                  for e in context.get("events", [])],
        "detector_signals": context.get("triggers", []),
    }
    n = int(context.get("hooks_per_clip", 4))
    return (
        "You write short TikTok text for a Roblox gameplay clip made by the game's own developer.\n"
        f"FACTS (the only things you may state as happening):\n{json.dumps(facts, ensure_ascii=False)}\n"
        "RULES: Only describe events listed in logged_events_in_clip or detector_signals. "
        "Never claim a boss fight, win, death, rare item, record or score that is not listed. "
        "No engagement bait (no 'like if', 'follow for part 2', 'comment', 'wait till the end'). "
        "No emoji in hooks or on_screen_text. No #fyp/#viral/trending hashtags. "
        f"Avoid these words: {json.dumps(g.get('avoid_words', []))}.\n"
        f"Return JSON with keys: hooks (list of {n} strings, each <= 60 chars), "
        "on_screen_text (<= 40 chars), caption (<= 150 chars, include the game name and the cta), "
        "hashtags (list, max 5, relevant to the game only), voiceover (1-2 sentences), "
        "cta (string), rationale (one sentence on why the clip may hold attention, citing the facts)."
    )


class _LLMProvider(Provider):
    """Shared fallback behaviour for network LLM providers."""

    def __init__(self, fallback: Provider | None = None, timeout: float = 60):
        self.fallback = fallback or TemplateProvider()
        self.timeout = timeout

    def _call(self, prompt: str, context: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def generate(self, context: dict) -> dict:
        try:
            out = validate_output(self._call(build_prompt(context), context))
            out["provider"] = self.name
            return out
        except Exception as e:  # any failure -> deterministic templates
            _log.warning("%s provider failed (%s); falling back to templates", self.name, e)
            out = self.fallback.generate(context)
            out["provider"] = f"{self.fallback.name}(fallback:{self.name})"
            return out


class OllamaProvider(_LLMProvider):
    name = "ollama"

    def __init__(self, url: str = "http://localhost:11434", model: str = "llama3.2:3b",
                 timeout: float = 60, fallback: Provider | None = None):
        super().__init__(fallback, timeout)
        self.url = url.rstrip("/")
        self.model = model

    def _call(self, prompt: str, context: dict) -> dict:
        r = requests.post(f"{self.url}/api/generate", timeout=self.timeout, json={
            "model": self.model, "prompt": prompt, "format": "json", "stream": False,
            "options": {"temperature": 0.7, "seed": int(context.get("seed", 0)) % (2**31)},
        })
        r.raise_for_status()
        return json.loads(r.json()["response"])


def ollama_reachable(url: str, timeout: float = 1.0) -> bool:
    try:
        r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


class AnthropicProvider(_LLMProvider):
    """OPTIONAL PAID provider (per-token billing). Opt-in only."""
    name = "anthropic"

    def __init__(self, model: str, timeout: float = 60, fallback: Provider | None = None):
        super().__init__(fallback, timeout)
        self.model = model

    def _call(self, prompt: str, context: dict) -> dict:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=self.timeout, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json={
            "model": self.model, "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt + "\nRespond with only the JSON object."}]})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        return json.loads(text[text.find("{"): text.rfind("}") + 1])


class OpenAIProvider(_LLMProvider):
    """OPTIONAL PAID provider (per-token billing). Opt-in only."""
    name = "openai"

    def __init__(self, model: str, timeout: float = 60, fallback: Provider | None = None):
        super().__init__(fallback, timeout)
        self.model = model

    def _call(self, prompt: str, context: dict) -> dict:
        r = requests.post("https://api.openai.com/v1/chat/completions", timeout=self.timeout,
                          headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, json={
            "model": self.model, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])


def get_provider(settings) -> Provider:
    """Resolve [content].provider: auto | template | ollama | anthropic | openai."""
    cfg = settings.section("content") if hasattr(settings, "section") else dict(settings)
    choice = str(cfg.get("provider", "auto")).lower()
    url = cfg.get("ollama_url", "http://localhost:11434")
    timeout = float(cfg.get("llm_timeout", 60))
    template = TemplateProvider()
    if choice == "template":
        return template
    if choice == "ollama":
        return OllamaProvider(url, cfg.get("ollama_model", "llama3.2:3b"), timeout, template)
    if choice == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return AnthropicProvider(cfg.get("anthropic_model", "claude-haiku-4-5"), timeout, template)
        _log.warning("provider=anthropic but ANTHROPIC_API_KEY unset; using templates")
        return template
    if choice == "openai":
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAIProvider(cfg.get("openai_model", "gpt-4o-mini"), timeout, template)
        _log.warning("provider=openai but OPENAI_API_KEY unset; using templates")
        return template
    # auto: free options only
    if ollama_reachable(url):
        return OllamaProvider(url, cfg.get("ollama_model", "llama3.2:3b"), timeout, template)
    return template
