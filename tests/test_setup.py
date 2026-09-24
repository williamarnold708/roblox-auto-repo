import json
import shutil
from pathlib import Path

from app import config, setup_win


def _settings(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(Path(__file__).resolve().parent.parent / "config", root / "config")
    return config.load(root)


def test_write_game_json(tmp_path):
    p = setup_win.write_game_json(tmp_path / "my-obby", {"name": "My Obby", "genre": "Obby",
                                                           "avoid": "free robux, scam"})
    g = json.loads(p.read_text())
    assert g["hashtags"] == ["roblox", "obby"]
    assert g["avoid_words"] == ["free robux", "scam"]
    assert "My Obby" in g["cta"]


def test_set_inbox_absolute_path(tmp_path):
    s = _settings(tmp_path)
    synced = tmp_path / "Google Drive" / "AutoPromo"
    setup_win._set_inbox(s, synced)
    assert config.load(s.root).path("inbox") == synced


def test_autostart_on_off(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    s = _settings(tmp_path)
    launcher = setup_win.autostart(s, True)
    text = launcher.read_text()
    assert launcher.suffix == ".pyw" and "'service'" in text and str(s.root) in text
    compile(text, str(launcher), "exec")
    setup_win.autostart(s, False)
    assert not launcher.exists()
