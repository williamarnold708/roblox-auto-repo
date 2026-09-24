"""Shared fixtures: a temp project root with settings copied, a DB connection,
and a short synthetic sample video (generated once per test session)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db  # noqa: E402

SAMPLE_DURATION = 45.0   # scaled version of the 90 s demo timeline


@pytest.fixture(scope="session")
def sample_assets(tmp_path_factory):
    """Generate the synthetic sample once (320x180, 15 fps, ~1-2 s)."""
    import make_sample
    out = tmp_path_factory.mktemp("sample")
    info = make_sample.generate(out, duration=SAMPLE_DURATION, width=320, height=180, fps=15,
                                preset="ultrafast")
    return info


@pytest.fixture
def sample_video(sample_assets):
    return sample_assets["video"]


@pytest.fixture
def project_root(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
    return tmp_path


@pytest.fixture
def settings(project_root):
    s = config.load(project_root)
    # fast, test-friendly overrides
    s.raw.setdefault("ingest", {})["settle_seconds"] = 0.2
    s.raw["clipping"].update(min_seconds=6, max_seconds=12, target_seconds=8, max_clips_per_recording=3)
    s.raw["render"]["preset"] = "ultrafast"
    return s


@pytest.fixture
def conn(settings):
    c = db.connect(settings.path("database"))
    yield c
    c.close()


@pytest.fixture
def sample_inbox(settings, sample_assets):
    """Copy the sample video, events and game.json into <root>/inbox/demo-obby/."""
    dest = settings.path("inbox") / "demo-obby"
    dest.mkdir(parents=True, exist_ok=True)
    for key in ("video", "events_file", "game_json"):
        shutil.copy(sample_assets[key], dest / Path(sample_assets[key]).name)
    return dest
