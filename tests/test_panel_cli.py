"""CLI integration tests for the panel command."""

from __future__ import annotations

from pathlib import Path

import pytest

shapely = pytest.importorskip("shapely", reason="Shapely required for panel CLI tests")

from kicad_tools.cli.parser import create_parser  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "projects"
TEST_PCB = FIXTURES_DIR / "test_project.kicad_pcb"


class TestPanelCLI:
    """Tests for the kct panel CLI command."""

    @pytest.fixture
    def parser(self):
        return create_parser()

    def test_parser_accepts_panel_command(self, parser):
        """Parser recognizes the panel command with required args."""
        args = parser.parse_args(["panel", str(TEST_PCB)])
        assert args.command == "panel"
        assert args.panel_input == str(TEST_PCB)
        assert args.panel_rows == 2
        assert args.panel_cols == 2

    def test_parser_custom_grid(self, parser):
        """Parser accepts custom grid size."""
        args = parser.parse_args([
            "panel", str(TEST_PCB),
            "--rows", "3",
            "--cols", "4",
            "--spacing", "3.5",
        ])
        assert args.panel_rows == 3
        assert args.panel_cols == 4
        assert args.panel_spacing == 3.5

    def test_parser_cut_method(self, parser):
        """Parser accepts cut method selection."""
        args = parser.parse_args([
            "panel", str(TEST_PCB),
            "--cut", "vcut",
        ])
        assert args.panel_cut == "vcut"

    def test_parser_frame_options(self, parser):
        """Parser accepts frame configuration."""
        args = parser.parse_args([
            "panel", str(TEST_PCB),
            "--frame",
            "--frame-width", "6.0",
            "--tooling-holes",
            "--fiducials",
        ])
        assert args.panel_frame is True
        assert args.panel_frame_width == 6.0
        assert args.panel_tooling_holes is True
        assert args.panel_fiducials is True

    def test_panel_command_runs(self, tmp_path):
        """Panel command produces output file."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        from kicad_tools.cli.commands.panel import run_panel_command

        output = tmp_path / "test_panel.kicad_pcb"

        class Args:
            panel_input = str(TEST_PCB)
            panel_output = str(output)
            panel_rows = 2
            panel_cols = 2
            panel_spacing = 2.0
            panel_cut = "mousebite"
            panel_tab_width = 3.0
            panel_tab_count = 3
            panel_mousebite_diameter = 0.5
            panel_mousebite_spacing = 0.8
            panel_frame = False
            panel_frame_width = 5.0
            panel_frame_space = 2.0
            panel_tooling_holes = False
            panel_fiducials = False

        result = run_panel_command(Args())
        assert result == 0
        assert output.exists()

    def test_panel_command_missing_file(self):
        """Panel command returns error for missing input file."""
        from kicad_tools.cli.commands.panel import run_panel_command

        class Args:
            panel_input = "/nonexistent/board.kicad_pcb"
            panel_output = None

        result = run_panel_command(Args())
        assert result == 1
