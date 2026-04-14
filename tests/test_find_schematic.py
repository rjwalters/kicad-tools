"""Tests for find_schematic helper in kicad_tools.report.utils.

Tests cover all four resolution steps:
1. Direct stem match (PCB stem == schematic stem)
2. Project file lookup (derive stem from .kicad_pro meta.filename)
3. Single-glob fallback (exactly one .kicad_sch in the directory)
4. None when absent or ambiguous (multiple .kicad_sch, no match)

Also tests integration with collector.py and report_cmd.py call sites.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.report.utils import find_schematic

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_FIXTURES = FIXTURES / "projects"


# ---------------------------------------------------------------------------
# Step 1: direct stem match
# ---------------------------------------------------------------------------


class TestDirectStemMatch:
    """find_schematic returns <pcb_stem>.kicad_sch when it exists."""

    def test_returns_direct_match(self, tmp_path: Path) -> None:
        """When PCB and schematic share a stem, direct match wins."""
        pcb = tmp_path / "board.kicad_pcb"
        sch = tmp_path / "board.kicad_sch"
        pcb.write_text("")
        sch.write_text("")

        result = find_schematic(pcb)
        assert result == sch

    def test_direct_match_takes_priority_over_project(self, tmp_path: Path) -> None:
        """Direct match is preferred even when a .kicad_pro exists."""
        pcb = tmp_path / "board.kicad_pcb"
        sch_direct = tmp_path / "board.kicad_sch"
        sch_project = tmp_path / "project.kicad_sch"
        pro = tmp_path / "project.kicad_pro"

        pcb.write_text("")
        sch_direct.write_text("")
        sch_project.write_text("")
        pro.write_text(json.dumps({"meta": {"filename": "project.kicad_pro"}}))

        result = find_schematic(pcb)
        assert result == sch_direct


# ---------------------------------------------------------------------------
# Step 2: project file lookup
# ---------------------------------------------------------------------------


class TestProjectFileLookup:
    """find_schematic derives the schematic stem from .kicad_pro."""

    def test_finds_sch_via_project_meta(self, tmp_path: Path) -> None:
        """PCB renamed but .kicad_pro meta.filename leads to the schematic."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        sch = tmp_path / "board.kicad_sch"
        pro = tmp_path / "board.kicad_pro"

        pcb.write_text("")
        sch.write_text("")
        pro.write_text(json.dumps({"meta": {"filename": "board.kicad_pro"}}))

        result = find_schematic(pcb)
        assert result == sch

    def test_finds_sch_via_project_stem_fallback(self, tmp_path: Path) -> None:
        """When .kicad_pro JSON is missing meta.filename, use file stem."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        sch = tmp_path / "myproject.kicad_sch"
        pro = tmp_path / "myproject.kicad_pro"

        pcb.write_text("")
        sch.write_text("")
        pro.write_text("{}")  # no meta.filename

        result = find_schematic(pcb)
        assert result == sch

    def test_finds_sch_via_project_invalid_json(self, tmp_path: Path) -> None:
        """When .kicad_pro is not valid JSON, fall back to file stem."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        sch = tmp_path / "myproject.kicad_sch"
        pro = tmp_path / "myproject.kicad_pro"

        pcb.write_text("")
        sch.write_text("")
        pro.write_text("not json")

        result = find_schematic(pcb)
        assert result == sch

    def test_project_file_sch_does_not_exist(self, tmp_path: Path) -> None:
        """When .kicad_pro points to a stem whose .kicad_sch does not exist."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        pro = tmp_path / "board.kicad_pro"

        pcb.write_text("")
        pro.write_text(json.dumps({"meta": {"filename": "board.kicad_pro"}}))
        # No board.kicad_sch exists

        result = find_schematic(pcb)
        assert result is None


# ---------------------------------------------------------------------------
# Step 3: single-glob fallback
# ---------------------------------------------------------------------------


class TestSingleGlobFallback:
    """find_schematic falls back to the lone .kicad_sch in the directory."""

    def test_single_sch_found(self, tmp_path: Path) -> None:
        """One .kicad_sch in the directory is returned."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        sch = tmp_path / "original.kicad_sch"

        pcb.write_text("")
        sch.write_text("")

        result = find_schematic(pcb)
        assert result == sch

    def test_ambiguous_multiple_sch(self, tmp_path: Path) -> None:
        """Multiple .kicad_sch files with no direct or project match returns None."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        (tmp_path / "a.kicad_sch").write_text("")
        (tmp_path / "b.kicad_sch").write_text("")
        pcb.write_text("")

        result = find_schematic(pcb)
        assert result is None

    def test_ambiguous_emits_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Multiple .kicad_sch files triggers a warning log."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        (tmp_path / "a.kicad_sch").write_text("")
        (tmp_path / "b.kicad_sch").write_text("")
        pcb.write_text("")

        import logging

        with caplog.at_level(logging.WARNING, logger="kicad_tools.report.utils"):
            find_schematic(pcb)

        assert any("Multiple .kicad_sch" in msg for msg in caplog.messages)
        assert any("--sch" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# Step 4: no candidates
# ---------------------------------------------------------------------------


class TestNoCandidates:
    """find_schematic returns None when no .kicad_sch exists."""

    def test_empty_directory(self, tmp_path: Path) -> None:
        """No .kicad_sch at all returns None."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("")

        result = find_schematic(pcb)
        assert result is None

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        """Even with a .kicad_pro that doesn't help, returns None."""
        pcb = tmp_path / "board-fixed.kicad_pcb"
        pro = tmp_path / "project.kicad_pro"

        pcb.write_text("")
        pro.write_text(json.dumps({"meta": {"filename": "project.kicad_pro"}}))
        # project.kicad_sch does not exist

        result = find_schematic(pcb)
        assert result is None


# ---------------------------------------------------------------------------
# Integration: collector.py uses find_schematic
# ---------------------------------------------------------------------------


class TestCollectorIntegration:
    """Tests that ReportDataCollector.collect_all uses find_schematic."""

    def test_collect_all_finds_sch_by_project_file(self, tmp_path: Path) -> None:
        """BOM collection succeeds when PCB is renamed but .kicad_pro exists."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        # Create a directory with a renamed PCB, original schematic, and project file
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        renamed_pcb = work_dir / "board-fixed.kicad_pcb"
        shutil.copy(pcb_path, renamed_pcb)

        sch_src = PROJECT_FIXTURES / "test_project.kicad_sch"
        if sch_src.exists():
            shutil.copy(sch_src, work_dir / "test_project.kicad_sch")

        pro_src = PROJECT_FIXTURES / "test_project.kicad_pro"
        if pro_src.exists():
            shutil.copy(pro_src, work_dir / "test_project.kicad_pro")

        from kicad_tools.report.collector import ReportDataCollector

        collector = ReportDataCollector(renamed_pcb, skip_erc=True)
        output_dir = tmp_path / "output"
        files = collector.collect_all(output_dir)

        # BOM should be present because find_schematic found it via project file
        assert "bom" in files

    def test_collect_all_finds_sch_by_glob(self, tmp_path: Path) -> None:
        """BOM collection succeeds via single-glob when no project file."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        sch_path = PROJECT_FIXTURES / "test_project.kicad_sch"
        if not pcb_path.exists() or not sch_path.exists():
            pytest.skip("test fixtures not found")

        # Create a directory with renamed PCB and lone schematic (no .kicad_pro)
        work_dir = tmp_path / "project"
        work_dir.mkdir()

        renamed_pcb = work_dir / "board-fixed.kicad_pcb"
        shutil.copy(pcb_path, renamed_pcb)
        shutil.copy(sch_path, work_dir / "test_project.kicad_sch")
        # No .kicad_pro file

        from kicad_tools.report.collector import ReportDataCollector

        collector = ReportDataCollector(renamed_pcb, skip_erc=True)
        output_dir = tmp_path / "output"
        files = collector.collect_all(output_dir)

        # BOM should be present because find_schematic found it via glob
        assert "bom" in files

    def test_collect_all_warns_on_ambiguous_sch(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """BOM is skipped with warning when multiple schematics exist."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        work_dir = tmp_path / "project"
        work_dir.mkdir()

        renamed_pcb = work_dir / "board-fixed.kicad_pcb"
        shutil.copy(pcb_path, renamed_pcb)

        # Create two schematics (ambiguous)
        (work_dir / "a.kicad_sch").write_text("")
        (work_dir / "b.kicad_sch").write_text("")

        import logging

        from kicad_tools.report.collector import ReportDataCollector

        with caplog.at_level(logging.WARNING):
            collector = ReportDataCollector(renamed_pcb, skip_erc=True)
            output_dir = tmp_path / "output"
            files = collector.collect_all(output_dir)

        # BOM should NOT be present
        assert "bom" not in files


# ---------------------------------------------------------------------------
# Integration: report_cmd.py _generate_figures uses find_schematic
# ---------------------------------------------------------------------------


class TestReportCmdIntegration:
    """Tests that _generate_figures in report_cmd.py uses find_schematic."""

    def test_generate_figures_uses_find_schematic(self, tmp_path: Path) -> None:
        """_generate_figures resolves schematic via find_schematic when --sch not given."""
        import argparse
        from unittest.mock import MagicMock

        # Set up: renamed PCB with schematic found via glob
        pcb = tmp_path / "board-fixed.kicad_pcb"
        sch = tmp_path / "original.kicad_sch"
        pcb.write_text("")
        sch.write_text("")

        args = argparse.Namespace(sch=None)
        figures_dir = tmp_path / "figures"
        data = MagicMock()

        # Patch ReportFigureGenerator at its source (lazy-imported from kicad_tools.report)
        mock_fig_gen = MagicMock()
        mock_fig_gen.generate_all.return_value = []

        with patch(
            "kicad_tools.report.ReportFigureGenerator",
            return_value=mock_fig_gen,
        ):
            from kicad_tools.cli.report_cmd import _generate_figures

            _generate_figures(args, pcb, figures_dir, data)

        # Verify generate_all was called with the discovered schematic
        mock_fig_gen.generate_all.assert_called_once()
        call_args = mock_fig_gen.generate_all.call_args
        assert call_args[0][1] == sch

    def test_generate_figures_sch_explicit_takes_precedence(self, tmp_path: Path) -> None:
        """--sch always takes precedence over auto-discovery."""
        import argparse
        from unittest.mock import MagicMock

        pcb = tmp_path / "board-fixed.kicad_pcb"
        sch_auto = tmp_path / "auto.kicad_sch"
        sch_explicit = tmp_path / "explicit.kicad_sch"
        pcb.write_text("")
        sch_auto.write_text("")
        sch_explicit.write_text("")

        args = argparse.Namespace(sch=str(sch_explicit))
        figures_dir = tmp_path / "figures"
        data = MagicMock()

        mock_fig_gen = MagicMock()
        mock_fig_gen.generate_all.return_value = []

        with patch(
            "kicad_tools.report.ReportFigureGenerator",
            return_value=mock_fig_gen,
        ):
            from kicad_tools.cli.report_cmd import _generate_figures

            _generate_figures(args, pcb, figures_dir, data)

        mock_fig_gen.generate_all.assert_called_once()
        call_args = mock_fig_gen.generate_all.call_args
        assert call_args[0][1] == sch_explicit

    def test_generate_figures_skips_when_no_sch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """_generate_figures prints warning and returns when no schematic found."""
        import argparse
        from unittest.mock import MagicMock

        pcb = tmp_path / "board-fixed.kicad_pcb"
        pcb.write_text("")
        # No .kicad_sch files in directory

        args = argparse.Namespace(sch=None)
        figures_dir = tmp_path / "figures"
        data = MagicMock()

        from kicad_tools.cli.report_cmd import _generate_figures

        _generate_figures(args, pcb, figures_dir, data)

        captured = capsys.readouterr()
        assert "no schematic found" in captured.err
        assert "--sch" in captured.err
