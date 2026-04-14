"""Tests for figure generation wiring in report_cmd.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.report_cmd import (
    _entries_to_pcb_figures,
    _entries_to_schematic_sheets,
)
from kicad_tools.cli.report_cmd import (
    main as report_main,
)
from kicad_tools.report.figures import FigureEntry

# ---------------------------------------------------------------------------
# Helper functions for FigureEntry conversion
# ---------------------------------------------------------------------------


class TestEntriesToPcbFigures:
    """Tests for _entries_to_pcb_figures helper."""

    def test_all_pcb_types(self):
        entries = [
            FigureEntry("pcb_front.png", "PCB Front", "pcb_front"),
            FigureEntry("pcb_back.png", "PCB Back", "pcb_back"),
            FigureEntry("pcb_copper.png", "PCB Copper Layers", "pcb_copper"),
            FigureEntry("assembly.png", "Assembly View", "assembly"),
        ]
        result = _entries_to_pcb_figures(entries)
        assert result == {
            "front": "figures/pcb_front.png",
            "back": "figures/pcb_back.png",
            "copper": "figures/pcb_copper.png",
            "assembly": "figures/assembly.png",
        }

    def test_partial_pcb_types(self):
        entries = [
            FigureEntry("pcb_front.png", "PCB Front", "pcb_front"),
            FigureEntry("pcb_back.png", "PCB Back", "pcb_back"),
        ]
        result = _entries_to_pcb_figures(entries)
        assert result == {
            "front": "figures/pcb_front.png",
            "back": "figures/pcb_back.png",
        }

    def test_no_pcb_entries_returns_none(self):
        entries = [
            FigureEntry("schematic_main.png", "Schematic: main", "schematic"),
        ]
        result = _entries_to_pcb_figures(entries)
        assert result is None

    def test_empty_list_returns_none(self):
        assert _entries_to_pcb_figures([]) is None

    def test_ignores_schematic_entries(self):
        entries = [
            FigureEntry("pcb_front.png", "PCB Front", "pcb_front"),
            FigureEntry("schematic_main.png", "Schematic: main", "schematic"),
        ]
        result = _entries_to_pcb_figures(entries)
        assert result == {"front": "figures/pcb_front.png"}


class TestEntriesToSchematicSheets:
    """Tests for _entries_to_schematic_sheets helper."""

    def test_schematic_entries(self):
        entries = [
            FigureEntry("schematic_main.png", "Schematic: main", "schematic"),
            FigureEntry("schematic_power.png", "Schematic: power", "schematic"),
        ]
        result = _entries_to_schematic_sheets(entries)
        assert result == [
            {"name": "Schematic: main", "figure_path": "figures/schematic_main.png"},
            {"name": "Schematic: power", "figure_path": "figures/schematic_power.png"},
        ]

    def test_no_schematic_entries_returns_none(self):
        entries = [
            FigureEntry("pcb_front.png", "PCB Front", "pcb_front"),
        ]
        result = _entries_to_schematic_sheets(entries)
        assert result is None

    def test_empty_list_returns_none(self):
        assert _entries_to_schematic_sheets([]) is None

    def test_ignores_pcb_entries(self):
        entries = [
            FigureEntry("pcb_front.png", "PCB Front", "pcb_front"),
            FigureEntry("schematic_main.png", "Schematic: main", "schematic"),
        ]
        result = _entries_to_schematic_sheets(entries)
        assert result == [
            {"name": "Schematic: main", "figure_path": "figures/schematic_main.png"},
        ]


# ---------------------------------------------------------------------------
# CLI integration tests for figure generation wiring
# ---------------------------------------------------------------------------


def _mock_figure_entries() -> list[FigureEntry]:
    """Return a synthetic list of FigureEntry objects for testing."""
    return [
        FigureEntry("pcb_front.png", "PCB Front", "pcb_front"),
        FigureEntry("pcb_back.png", "PCB Back", "pcb_back"),
        FigureEntry("pcb_copper.png", "PCB Copper Layers", "pcb_copper"),
        FigureEntry("assembly.png", "Assembly View", "assembly"),
        FigureEntry("schematic_main.png", "Schematic: main", "schematic"),
    ]


class TestFigureGenerationWiring:
    """Tests for automatic figure generation in the report CLI."""

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_figures_generated_for_kicad_pcb(self, mock_gen_cls, tmp_path):
        """Calling generate with a .kicad_pcb file triggers figure generation."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.return_value = _mock_figure_entries()

        output_dir = tmp_path / "reports"
        result = report_main(
            ["generate", "board.kicad_pcb", "--mfr", "jlcpcb", "-o", str(output_dir)]
        )
        assert result == 0

        # Verify generate_all was called
        mock_instance.generate_all.assert_called_once()
        call_args = mock_instance.generate_all.call_args
        assert call_args[0][0] == Path("board.kicad_pcb")
        # sch_path should be inferred from pcb path
        assert call_args[0][1] == Path("board.kicad_sch")
        # figures_dir should be under version dir
        figures_dir = call_args[0][2]
        assert figures_dir.name == "figures"
        assert "v1" in str(figures_dir)

        # Verify the report contains figure references
        report_path = output_dir / "v1" / "report.md"
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "![PCB Front](figures/pcb_front.png)" in content
        assert "![PCB Back](figures/pcb_back.png)" in content
        assert "![PCB Copper](figures/pcb_copper.png)" in content
        assert "![Assembly](figures/assembly.png)" in content
        assert "![Schematic: main](figures/schematic_main.png)" in content

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_sch_flag_overrides_inferred_path(self, mock_gen_cls, tmp_path):
        """--sch explicitly sets the schematic path."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.return_value = _mock_figure_entries()

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
                "--sch",
                "custom/root.kicad_sch",
            ]
        )
        assert result == 0

        call_args = mock_instance.generate_all.call_args
        assert call_args[0][1] == Path("custom/root.kicad_sch")

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_no_figures_flag_skips_generation(self, mock_gen_cls, tmp_path):
        """--no-figures prevents figure generation entirely."""
        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
                "--no-figures",
            ]
        )
        assert result == 0

        # ReportFigureGenerator should never be instantiated
        mock_gen_cls.assert_not_called()

        # Report should exist but without figure sections
        report_path = output_dir / "v1" / "report.md"
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "## PCB Layout" not in content
        assert "## Schematic Overview" not in content

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_no_figures_for_kicad_pro_input(self, mock_gen_cls, tmp_path):
        """Figure generation is skipped for .kicad_pro files."""
        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "project.kicad_pro",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
            ]
        )
        assert result == 0
        mock_gen_cls.assert_not_called()

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_data_dir_skips_figure_generation(self, mock_gen_cls, tmp_path):
        """When --data-dir is provided, figure generation is skipped."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "board_stats.json").write_text(
            json.dumps({"layer_count": 2, "component_count": 10})
        )

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
                "--data-dir",
                str(data_dir),
            ]
        )
        assert result == 0
        mock_gen_cls.assert_not_called()


class TestGracefulDegradation:
    """Tests for graceful degradation when dependencies are missing."""

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_runtime_error_prints_warning(self, mock_gen_cls, tmp_path, capsys):
        """When ReportFigureGenerator raises RuntimeError, CLI prints warning
        and continues without figures."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.side_effect = RuntimeError("kicad-cli not found")

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
            ]
        )

        assert result == 0

        captured = capsys.readouterr()
        assert "Warning: figure generation skipped" in captured.err
        assert "kicad-cli not found" in captured.err

        # Report should still be generated
        report_path = output_dir / "v1" / "report.md"
        assert report_path.exists()

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_cairosvg_missing_prints_warning(self, mock_gen_cls, tmp_path, capsys):
        """When cairosvg is missing, CLI prints warning and continues."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.side_effect = RuntimeError(
            "cairosvg is required for report figure generation"
        )

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
            ]
        )

        assert result == 0

        captured = capsys.readouterr()
        assert "Warning: figure generation skipped" in captured.err
        assert "cairosvg is required" in captured.err

        # Report should still be generated without figure sections
        report_path = output_dir / "v1" / "report.md"
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "## PCB Layout" not in content
        assert "## Schematic Overview" not in content


class TestFiguresInVersionDir:
    """Tests verifying that figures land in the correct versioned directory."""

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_figures_dir_in_same_version_as_report(self, mock_gen_cls, tmp_path):
        """Figures directory must be in the same vN/ directory as report.md."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.return_value = _mock_figure_entries()

        output_dir = tmp_path / "reports"
        report_main(["generate", "board.kicad_pcb", "--mfr", "jlcpcb", "-o", str(output_dir)])

        # Both report.md and figures/ should be under v1/
        report_path = output_dir / "v1" / "report.md"
        assert report_path.exists()

        # generate_all was called with figures_dir under v1
        call_args = mock_instance.generate_all.call_args
        figures_dir = call_args[0][2]
        assert figures_dir == output_dir / "v1" / "figures"

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_second_report_increments_version(self, mock_gen_cls, tmp_path):
        """Creating a second report puts figures in v2/figures/."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.return_value = _mock_figure_entries()

        output_dir = tmp_path / "reports"

        # First report -> v1
        report_main(["generate", "board.kicad_pcb", "--mfr", "jlcpcb", "-o", str(output_dir)])
        assert (output_dir / "v1" / "report.md").exists()

        # Second report -> v2
        report_main(["generate", "board.kicad_pcb", "--mfr", "jlcpcb", "-o", str(output_dir)])
        assert (output_dir / "v2" / "report.md").exists()

        # Verify the second call used v2/figures/
        second_call = mock_instance.generate_all.call_args_list[1]
        figures_dir = second_call[0][2]
        assert figures_dir == output_dir / "v2" / "figures"


class TestReportContentWithFigures:
    """Tests verifying report content when figures are generated."""

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_report_has_pcb_layout_section(self, mock_gen_cls, tmp_path):
        """Report should have PCB Layout section with all figure links."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.return_value = _mock_figure_entries()

        output_dir = tmp_path / "reports"
        report_main(["generate", "board.kicad_pcb", "--mfr", "jlcpcb", "-o", str(output_dir)])

        content = (output_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert "## PCB Layout" in content
        assert "### Front" in content
        assert "### Back" in content
        assert "### Copper" in content
        assert "### Assembly" in content

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_report_has_schematic_overview(self, mock_gen_cls, tmp_path):
        """Report should have Schematic Overview section."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.return_value = _mock_figure_entries()

        output_dir = tmp_path / "reports"
        report_main(["generate", "board.kicad_pcb", "--mfr", "jlcpcb", "-o", str(output_dir)])

        content = (output_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert "## Schematic Overview" in content
        assert "### Schematic: main" in content

    @patch("kicad_tools.report.ReportFigureGenerator")
    def test_empty_figure_entries_no_sections(self, mock_gen_cls, tmp_path):
        """When generate_all returns empty list, no figure sections appear."""
        mock_instance = mock_gen_cls.return_value
        mock_instance.generate_all.return_value = []

        output_dir = tmp_path / "reports"
        report_main(["generate", "board.kicad_pcb", "--mfr", "jlcpcb", "-o", str(output_dir)])

        content = (output_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert "## PCB Layout" not in content
        assert "## Schematic Overview" not in content


class TestCLIFlags:
    """Tests for the new CLI flags."""

    def test_help_includes_no_figures(self, capsys):
        """--no-figures should be in the help output."""
        with pytest.raises(SystemExit) as exc_info:
            report_main(["generate", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--no-figures" in captured.out

    def test_help_includes_sch(self, capsys):
        """--sch should be in the help output."""
        with pytest.raises(SystemExit) as exc_info:
            report_main(["generate", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--sch" in captured.out

    def test_parser_registered_sch_flag(self):
        """The --sch flag is registered in the main parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["report", "generate", "board.kicad_pcb", "--sch", "root.kicad_sch"]
        )
        assert args.report_sch == "root.kicad_sch"

    def test_parser_registered_no_figures_flag(self):
        """The --no-figures flag is registered in the main parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["report", "generate", "board.kicad_pcb", "--no-figures"])
        assert args.report_no_figures is True

    def test_parser_no_figures_defaults_false(self):
        """The --no-figures flag defaults to False."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["report", "generate", "board.kicad_pcb"])
        assert args.report_no_figures is False
