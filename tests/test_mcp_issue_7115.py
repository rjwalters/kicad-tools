"""Regression tests for Issue #7115 (Part 1): placement_suggestions,
zipped Gerber copper counting, and board_view at low ``max_size_px``.

Each test makes the reproducing call through ``MCPServer().call_tool`` on a
copy of the ``test_project`` fixture.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.mcp.server import MCPServer

FIXTURES = Path(__file__).parent / "fixtures" / "projects"


@pytest.fixture
def project(tmp_path):
    for ext in ("kicad_pcb", "kicad_sch", "kicad_pro"):
        shutil.copy(FIXTURES / f"test_project.{ext}", tmp_path / f"test_project.{ext}")
    return tmp_path


@pytest.fixture
def pcb(project):
    return str(project / "test_project.kicad_pcb")


@pytest.fixture
def sch(project):
    return str(project / "test_project.kicad_sch")


@pytest.fixture
def server():
    return MCPServer()


def _need_kicad_cli():
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")


class TestPlacementSuggestions:
    def test_all_components(self, server, pcb):
        result = server.call_tool("placement_suggestions", {"pcb_path": pcb})
        assert result["success"] is True
        refs = [s["reference"] for s in result["suggestions"]]
        assert refs and len(refs) == len(set(refs)) == result["count"]
        keys = [(-len(s["constraints_violated"]), s["confidence"]) for s in result["suggestions"]]
        assert keys == sorted(keys)

    def test_single_component(self, server, pcb):
        result = server.call_tool("placement_suggestions", {"pcb_path": pcb, "component": "R1"})
        assert result["success"] is True
        assert [s["reference"] for s in result["suggestions"]] == ["R1"]

    def test_unknown_component(self, server, pcb):
        result = server.call_tool("placement_suggestions", {"pcb_path": pcb, "component": "NOPE"})
        assert result["success"] is False
        assert "NOPE" in result["error"]

    def test_max_suggestions(self, server, pcb):
        result = server.call_tool("placement_suggestions", {"pcb_path": pcb, "max_suggestions": 1})
        assert result["success"] is True
        assert len(result["suggestions"]) == 1

    def test_missing_file(self, server, project):
        result = server.call_tool(
            "placement_suggestions", {"pcb_path": str(project / "missing.kicad_pcb")}
        )
        assert result["success"] is False

    def test_strategy_not_in_schema(self):
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        props = TOOL_REGISTRY["placement_suggestions"].parameters["properties"]
        assert "strategy" not in props


class TestZippedGerberCopperCount:
    def test_default_zip(self, server, pcb, project):
        _need_kicad_cli()
        out = project / "out"
        result = server.call_tool("export_gerbers", {"pcb_path": pcb, "output_dir": str(out)})
        assert result["success"] is True, result
        assert result["layer_count"] == 2
        assert result["zip_file"] and Path(result["zip_file"]).exists()

    def test_loose_files_still_work(self, server, pcb, project):
        _need_kicad_cli()
        out = project / "out"
        result = server.call_tool(
            "export_gerbers", {"pcb_path": pcb, "output_dir": str(out), "zip_output": False}
        )
        assert result["success"] is True
        assert result["layer_count"] == 2

    def test_export_assembly_nested_gerbers(self, server, pcb, sch, project):
        _need_kicad_cli()
        result = server.call_tool(
            "export_assembly",
            {"pcb_path": pcb, "schematic_path": sch, "output_dir": str(project / "asm")},
        )
        assert result["gerbers"]["success"] is True, result["gerbers"]
        assert result["gerbers"]["layer_count"] == 2

    def test_helper_counts_zip_members(self, tmp_path):
        from kicad_tools.mcp.tools.export import _gerber_files_in

        with zipfile.ZipFile(tmp_path / "gerbers.zip", "w") as zf:
            zf.writestr("b-F_Cu.gtl", "x")
            zf.writestr("b-B_Cu.gbl", "x")
        files = _gerber_files_in(tmp_path)
        assert sum(f.file_type == "copper" for f in files) == 2
        assert any(f.file_type == "archive" for f in files)

    def test_zip_without_copper_still_fails(self, server, pcb, project, monkeypatch):
        from kicad_tools.mcp.tools import export as export_mod

        out = project / "out"

        class FakeExporter:
            def __init__(self, *_a, **_k):
                pass

            def export(self, config, output_dir):
                Path(output_dir).mkdir(parents=True, exist_ok=True)
                path = Path(output_dir) / config.zip_name
                with zipfile.ZipFile(path, "w") as zf:
                    zf.writestr("b-F_Silkscreen.gto", "x")
                return path

        monkeypatch.setattr(export_mod, "GerberExporter", FakeExporter)
        result = server.call_tool("export_gerbers", {"pcb_path": pcb, "output_dir": str(out)})
        assert result["success"] is False
        assert "no copper-layer" in result["error"]


class TestBoardViewSmallSizes:
    @pytest.mark.parametrize("px", [320, 400, 450, 500, 640])
    def test_never_caption_only(self, server, pcb, px):
        pytest.importorskip("matplotlib")
        from kicad_tools.boardview.render import MIN_BOARD_PX

        result = server.call_tool("board_view", {"pcb_path": pcb, "max_size_px": px})
        if result["success"]:
            assert result["px_per_mm"] > 0
            assert result["width_px"] >= MIN_BOARD_PX
            assert max(result["width_px"], result["height_px"]) <= px
        else:
            assert "max_size_px" in result["error_message"]

    def test_400_succeeds(self, server, pcb):
        pytest.importorskip("matplotlib")
        result = server.call_tool("board_view", {"pcb_path": pcb, "max_size_px": 400})
        assert result["success"] is True
        assert result["width_px"] >= 300
