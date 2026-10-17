"""Tests for ``kct screenshot --baseline-save/--baseline-compare`` (issue #6912)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from kicad_tools.cli import screenshot_baseline as sb
from kicad_tools.cli import screenshot_cmd

IDENTITY = {"kicad_cli": "8.0.1", "cairosvg": "2.7.0"}


def _png(path: Path, size=(20, 10), color=(255, 255, 255), dot=None) -> None:
    img = Image.new("RGB", size, color)
    if dot:
        img.putpixel(dot, (0, 0, 0))
    img.save(path)


@pytest.fixture
def board(tmp_path):
    pcb = tmp_path / "demo.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return pcb


@pytest.fixture
def fake_render(monkeypatch):
    """Stub the real renderer; ``state`` controls what gets 'rendered'."""
    state = {"size": (20, 10), "dot": None, "identity": dict(IDENTITY)}

    def render(pcb_path, layers, max_size_px, output_path, black_and_white, theme):
        _png(Path(output_path), size=state["size"], dot=state["dot"])
        w, h = state["size"]
        return {
            "success": True,
            "output_path": output_path,
            "width_px": w,
            "height_px": h,
            "layers_rendered": ["F.Cu"],
        }

    monkeypatch.setattr("kicad_tools.mcp.tools.screenshot.screenshot_board", render)
    monkeypatch.setattr(sb, "renderer_identity", lambda: dict(state["identity"]))
    return state


def _run(capsys, *argv):
    code = screenshot_cmd.main([*argv, "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def test_pixel_drift_identical_and_changed(tmp_path):
    a, b, c = tmp_path / "a.png", tmp_path / "b.png", tmp_path / "c.png"
    _png(a)
    _png(b)
    _png(c, dot=(3, 3))
    same = sb.pixel_drift(a, b)
    assert same["status"] == "identical" and same["drift_fraction"] == 0
    diff = sb.pixel_drift(a, c)
    assert diff["status"] == "changed"
    assert diff["changed_pixels"] == 1 and diff["total_pixels"] == 200
    assert diff["drift_fraction"] == pytest.approx(1 / 200)


def test_save_then_compare_identical(tmp_path, board, fake_render, capsys):
    base = tmp_path / "base"
    code, doc = _run(
        capsys, str(board), "-o", str(tmp_path / "out.png"), "--baseline-save", str(base)
    )
    assert code == 0 and doc["baseline"]["renderer"] == IDENTITY
    sidecar = json.loads((base / "demo.baseline.json").read_text())
    assert sidecar["options"]["max_size"] == 1568 and (base / "demo.png").is_file()

    code, doc = _run(capsys, str(board), "--baseline-compare", str(base))
    assert code == 0 and doc["status"] == "identical" and doc["drift_fraction"] == 0
    assert not (tmp_path / "demo.png").exists()  # compare leaves no stray render


def test_compare_changed(tmp_path, board, fake_render, capsys):
    base = tmp_path / "base"
    _run(capsys, str(board), "-o", str(tmp_path / "o.png"), "--baseline-save", str(base))
    fake_render["dot"] = (1, 1)
    code, doc = _run(capsys, str(board), "--baseline-compare", str(base))
    assert code == 0 and doc["status"] == "changed" and doc["changed_pixels"] == 1


def test_renderer_mismatch(tmp_path, board, fake_render, capsys):
    base = tmp_path / "base"
    _run(capsys, str(board), "-o", str(tmp_path / "o.png"), "--baseline-save", str(base))
    fake_render["identity"]["kicad_cli"] = "9.0.0"
    code, doc = _run(capsys, str(board), "--baseline-compare", str(base))
    assert code != 0 and doc["status"] == "renderer_mismatch" and "drift_fraction" not in doc
    assert doc["differences"] == [
        {"field": "renderer.kicad_cli", "baseline": "8.0.1", "current": "9.0.0"}
    ]


def test_options_and_dimension_mismatch(tmp_path, board, fake_render, capsys):
    base = tmp_path / "base"
    _run(capsys, str(board), "-o", str(tmp_path / "o.png"), "--baseline-save", str(base))
    fake_render["size"] = (30, 10)
    code, doc = _run(capsys, str(board), "--baseline-compare", str(base), "--bw")
    fields = {d["field"] for d in doc["differences"]}
    assert code != 0 and fields == {"options.black_and_white", "width_px"}


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_baseline(tmp_path, board, fake_render, capsys, corrupt):
    base = tmp_path / "base"
    if corrupt:
        base.mkdir()
        _png(base / "demo.png")
        (base / "demo.baseline.json").write_text("{not json")
    code, doc = _run(capsys, str(board), "--baseline-compare", str(base))
    assert code == 1 and doc["success"] is False and "baseline" in doc["error"].lower()


def test_save_and_compare_are_exclusive(board):
    with pytest.raises(SystemExit):
        screenshot_cmd.main([str(board), "--baseline-save", "a", "--baseline-compare", "b"])
