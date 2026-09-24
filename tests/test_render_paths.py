"""Windows paths (C:\\..., spaces, commas, apostrophes) must survive filtergraph escaping."""
import shutil
import subprocess

import pytest

from app import render
from app.probe import FFMPEG


@pytest.mark.skipif(shutil.which(FFMPEG) is None and not shutil.os.path.exists(FFMPEG), reason="ffmpeg missing")
def test_drawtext_paths_with_colon_space_comma_apostrophe(tmp_path):
    d = tmp_path / "C:" / "Users" / "O'Brien, W" / "AppData Local"
    d.mkdir(parents=True)
    font = d / "font.ttf"
    shutil.copy(render.resolve_font(None), font)
    txt = d / "line0.txt"
    txt.write_text("hello", encoding="utf-8")
    graph = (f"[0:v]drawtext=fontfile={render._esc(str(font))}:textfile={render._esc(str(txt))}:"
             f"fontsize=20:enable=between(t\\,0\\,1)[v]")
    r = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=0.2",
                        "-filter_complex", graph, "-map", "[v]", "-f", "null", "-"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_backslashes_become_forward_slashes():
    assert render._esc(r"C:\Windows\Fonts\arialbd.ttf") == r"C\\:/Windows/Fonts/arialbd.ttf"


def test_missing_configured_font_falls_back():
    assert render.resolve_font("C:/does/not/exist.ttf")
