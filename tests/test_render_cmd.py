"""Tests for the kct render CLI command (Epic #3674, Phase 1).

These tests mock out the kicad-cli subprocess calls so they can run in any
environment without a live KiCad installation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli import render_cmd
from kicad_tools.cli.render_cmd import (
    RENDER_OUTPUTS,
    _discover_boards,
    _parse_version,
)
from kicad_tools.cli.render_cmd import (
    main as render_main,
)


def _make_board(root: Path, name: str, pcb_name: str | None = None) -> Path:
    """Create a board directory with an output/ dir and optional PCB file."""
    board = root / name
    output = board / "output"
    output.mkdir(parents=True)
    if pcb_name is not None:
        (output / pcb_name).write_text("(kicad_pcb)")
    return board


@pytest.fixture
def fake_kicad(monkeypatch):
    """Make kicad-cli appear available and stub render helpers to write files.

    The 2D export helper creates the requested SVG and the 3D render helper the
    requested PNG (both non-empty), returning a successful KiCadCLIResult, so the
    command exercises its full path without a live KiCad.
    """
    from kicad_tools.cli.runner import KiCadCLIResult

    fake_cli = Path("/usr/bin/kicad-cli")

    monkeypatch.setattr(render_cmd, "find_kicad_cli", lambda: fake_cli)
    monkeypatch.setattr(render_cmd, "get_kicad_version", lambda *a, **k: "8.0.6")

    calls: dict[str, list] = {"svg": [], "render": []}

    def fake_svg(pcb_path, output_path, layers, **kwargs):
        Path(output_path).write_bytes(b"<svg></svg>")
        calls["svg"].append((Path(pcb_path), Path(output_path), list(layers)))
        return KiCadCLIResult(success=True, output_path=Path(output_path))

    def fake_render(pcb_path, output_path, side="front", **kwargs):
        Path(output_path).write_bytes(b"PNG3D")
        calls["render"].append((Path(pcb_path), Path(output_path), side))
        return KiCadCLIResult(success=True, output_path=Path(output_path))

    monkeypatch.setattr(render_cmd, "run_pcb_export_svg", fake_svg)
    monkeypatch.setattr(render_cmd, "run_pcb_render", fake_render)

    return calls


# --------------------------------------------------------------------------
# Version parsing
# --------------------------------------------------------------------------


class TestParseVersion:
    def test_simple(self):
        assert _parse_version("8.0.6") == (8, 0, 6)

    def test_two_component(self):
        assert _parse_version("9.0") == (9, 0, 0)

    def test_extra_text(self):
        assert _parse_version("KiCad 8.0.4 release build") == (8, 0, 4)

    def test_rc_suffix(self):
        assert _parse_version("8.0.4-rc1") == (8, 0, 4)

    def test_none(self):
        assert _parse_version(None) is None

    def test_unparseable(self):
        assert _parse_version("nightly") is None


# --------------------------------------------------------------------------
# Board discovery
# --------------------------------------------------------------------------


class TestDiscoverBoards:
    def test_single_board_dir(self, tmp_path):
        board = _make_board(tmp_path, "01-foo", "foo.kicad_pcb")
        assert _discover_boards(board) == [board]

    def test_root_with_multiple_boards(self, tmp_path):
        b1 = _make_board(tmp_path, "00-a", "a.kicad_pcb")
        b2 = _make_board(tmp_path, "01-b", "b.kicad_pcb")
        assert _discover_boards(tmp_path) == [b1, b2]

    def test_nested_grouping_dir(self, tmp_path):
        # boards/external/<board>/output  — one level of grouping
        b = _make_board(tmp_path / "external", "softstart", "s.kicad_pcb")
        assert b in _discover_boards(tmp_path)


# --------------------------------------------------------------------------
# kicad-cli-not-found handling
# --------------------------------------------------------------------------


class TestKicadCliMissing:
    def test_not_found_returns_1(self, tmp_path, monkeypatch, capsys):
        _make_board(tmp_path, "00-a", "a.kicad_pcb")
        monkeypatch.setattr(render_cmd, "find_kicad_cli", lambda: None)
        rc = render_main([str(tmp_path)])
        assert rc == 1
        assert "kicad-cli not found" in capsys.readouterr().err

    def test_missing_path_returns_1(self, tmp_path, capsys):
        rc = render_main([str(tmp_path / "does-not-exist")])
        assert rc == 1
        assert "path not found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Output path contract
# --------------------------------------------------------------------------


class TestOutputPathContract:
    def test_writes_exact_filenames(self, tmp_path, fake_kicad):
        board = _make_board(tmp_path, "01-vd", "vd.kicad_pcb")
        rc = render_main([str(board)])
        assert rc == 0

        renders = board / "output" / "renders"
        for fname in RENDER_OUTPUTS.values():
            assert (renders / fname).exists(), f"missing {fname}"
        # Exactly the four contracted files, nothing else.
        assert sorted(p.name for p in renders.iterdir()) == sorted(RENDER_OUTPUTS.values())

    def test_filenames_are_exactly_the_contract(self):
        assert RENDER_OUTPUTS == {
            "pcb-front": "pcb-front.svg",
            "pcb-back": "pcb-back.svg",
            "3d-front": "3d-front.png",
            "3d-back": "3d-back.png",
        }


# --------------------------------------------------------------------------
# Routed PCB preference / fallback
# --------------------------------------------------------------------------


class TestPcbSelection:
    def test_prefers_routed_pcb(self, tmp_path, fake_kicad):
        board = _make_board(tmp_path, "01-vd", "vd.kicad_pcb")
        (board / "output" / "vd_routed.kicad_pcb").write_text("(kicad_pcb)")

        rc = render_main([str(board)])
        assert rc == 0

        # Every helper should have been called with the routed PCB.
        used = {call[0].name for call in fake_kicad["svg"]}
        assert used == {"vd_routed.kicad_pcb"}

    def test_falls_back_to_unrouted(self, tmp_path, fake_kicad):
        board = _make_board(tmp_path, "00-led", "led.kicad_pcb")
        rc = render_main([str(board)])
        assert rc == 0
        used = {call[0].name for call in fake_kicad["svg"]}
        assert used == {"led.kicad_pcb"}


# --------------------------------------------------------------------------
# --no-3d
# --------------------------------------------------------------------------


class TestNo3d:
    def test_skips_3d_renders(self, tmp_path, fake_kicad):
        board = _make_board(tmp_path, "01-vd", "vd.kicad_pcb")
        rc = render_main([str(board), "--no-3d"])
        assert rc == 0

        renders = board / "output" / "renders"
        assert (renders / "pcb-front.svg").exists()
        assert (renders / "pcb-back.svg").exists()
        assert not (renders / "3d-front.png").exists()
        assert not (renders / "3d-back.png").exists()
        # The 3D render helper was never called.
        assert fake_kicad["render"] == []


# --------------------------------------------------------------------------
# --format json
# --------------------------------------------------------------------------


class TestJsonOutput:
    def test_emits_valid_json_with_per_board_status(self, tmp_path, fake_kicad, capsys):
        _make_board(tmp_path, "00-a", "a.kicad_pcb")
        _make_board(tmp_path, "01-b", "b.kicad_pcb")

        rc = render_main([str(tmp_path), "--format", "json"])
        assert rc == 0

        payload = json.loads(capsys.readouterr().out)
        assert "boards" in payload
        names = {entry["board"] for entry in payload["boards"]}
        assert names == {"00-a", "01-b"}
        for entry in payload["boards"]:
            assert entry["status"] == "ok"
            assert set(entry["outputs"]) == set(RENDER_OUTPUTS)


# --------------------------------------------------------------------------
# 3D render command flags (issue #3702): logical front/back -> oblique
# top/bottom views, not kicad-cli's edge-on native front/back.
# --------------------------------------------------------------------------


class TestRenderObliqueFlags:
    _RUNNER = "kicad_tools.cli.runner"

    def _capture_cmd(self, tmp_path: Path, side: str):
        from kicad_tools.cli.runner import run_pcb_render

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        out = tmp_path / f"3d-{side}.png"

        def fake_run(cmd, *args, **kwargs):
            out.write_bytes(b"PNG3D")
            fake_run.cmd = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch(f"{self._RUNNER}.subprocess.run", side_effect=fake_run):
            res = run_pcb_render(
                pcb,
                out,
                side=side,
                kicad_cli=Path("/usr/bin/kicad-cli"),
            )

        assert res.success
        return fake_run.cmd

    def test_front_uses_oblique_top_view(self, tmp_path):
        cmd = self._capture_cmd(tmp_path, "front")
        assert cmd[cmd.index("--side") + 1] == "top"
        assert "--rotate" in cmd
        assert cmd[cmd.index("--rotate") + 1] == "-70,0,0"
        assert "--perspective" in cmd
        # The native edge-on "front" must NOT be emitted.
        assert "front" not in cmd

    def test_back_uses_oblique_bottom_view(self, tmp_path):
        cmd = self._capture_cmd(tmp_path, "back")
        assert cmd[cmd.index("--side") + 1] == "bottom"
        assert "--rotate" in cmd
        assert cmd[cmd.index("--rotate") + 1] == "70,0,0"
        assert "--perspective" in cmd
        assert "back" not in cmd

    def test_native_side_passes_through_without_rotate(self, tmp_path):
        # Callers passing a native kicad-cli side get a straight-on view.
        cmd = self._capture_cmd(tmp_path, "top")
        assert cmd[cmd.index("--side") + 1] == "top"
        assert "--rotate" not in cmd
        assert "--perspective" not in cmd


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


class TestEdgeCases:
    def test_board_without_pcb_is_non_fatal(self, tmp_path, fake_kicad, capsys):
        # output/ exists but contains no .kicad_pcb at all.
        _make_board(tmp_path, "00-empty", pcb_name=None)
        rc = render_main([str(tmp_path)])
        assert rc == 0
        assert "SKIP" in capsys.readouterr().err

    def test_render_failure_reports_error_and_exits_1(self, tmp_path, monkeypatch, fake_kicad):
        from kicad_tools.cli.runner import KiCadCLIResult

        board = _make_board(tmp_path, "01-vd", "vd.kicad_pcb")

        def failing(pcb_path, output_path, *a, **k):
            return KiCadCLIResult(success=False, stderr="boom")

        monkeypatch.setattr(render_cmd, "run_pcb_export_svg", failing)
        monkeypatch.setattr(render_cmd, "run_pcb_render", failing)

        rc = render_main([str(board)])
        assert rc == 1

    def test_old_kicad_version_blocks_3d(self, tmp_path, fake_kicad, monkeypatch, capsys):
        monkeypatch.setattr(render_cmd, "get_kicad_version", lambda *a, **k: "8.0.2")
        board = _make_board(tmp_path, "01-vd", "vd.kicad_pcb")
        rc = render_main([str(board)])
        assert rc == 1
        assert "8.0.4" in capsys.readouterr().err

    def test_old_kicad_version_allows_2d_with_no_3d(self, tmp_path, fake_kicad, monkeypatch):
        monkeypatch.setattr(render_cmd, "get_kicad_version", lambda *a, **k: "8.0.2")
        board = _make_board(tmp_path, "01-vd", "vd.kicad_pcb")
        rc = render_main([str(board), "--no-3d"])
        assert rc == 0
