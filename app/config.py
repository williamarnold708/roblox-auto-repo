"""Load settings.toml and resolve project paths."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("AUTOPROMO_ROOT", Path(__file__).resolve().parent.parent))


@dataclass
class Settings:
    root: Path
    raw: dict = field(default_factory=dict)

    def section(self, name: str) -> dict:
        return self.raw.get(name, {})

    def path(self, key: str) -> Path:
        p = self.root / self.raw["paths"][key]
        if key != "database":
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p


def load(root: Path | None = None) -> Settings:
    root = Path(root or ROOT)
    cfg = root / "config" / "settings.toml"
    if not cfg.exists():  # fall back to the packaged defaults (e.g. temp roots in tests)
        cfg = ROOT / "config" / "settings.toml"
    with open(cfg, "rb") as f:
        return Settings(root=root, raw=tomllib.load(f))
