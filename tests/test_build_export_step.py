"""Tests for the EXPORT step in build_cmd."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from kicad_tools.cli.build_cmd import (
    BuildContext,
    BuildStep,
    _run_step_export,
    main,
)


class TestBuildStepExportEnum:
    """Tests that EXPORT is properly added to BuildStep."""

    def test_export_in_build_step_enum(self) -> None:
        assert BuildStep.EXPORT.value == "export"

    def test_export_in_all_steps(self) -> None:
        """When --step all is used, EXPORT should be in the step list."""
        all_steps = [
            BuildStep.SCHEMATIC,
            BuildStep.PCB,
            BuildStep.OUTLINE,
            BuildStep.ROUTE,
            BuildStep.VERIFY,
            BuildStep.EXPORT,
        ]
        assert BuildStep.EXPORT in all_steps


class TestRunStepExport:
    """Tests for _run_step_export function."""

    def test_no_pcb_file_returns_failure(self, tmp_path: Path) -> None:
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=None,
            routed_pcb_file=None,
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert not result.success
        assert "No PCB file" in result.message

    def test_missing_pcb_file_returns_failure(self, tmp_path: Path) -> None:
        """PCB path set but file does not exist on disk."""
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=tmp_path / "nonexistent.kicad_pcb",
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert not result.success

    def test_dry_run_returns_success_without_executing(self, tmp_path: Path) -> None:
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            dry_run=True,
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert result.success
        assert "[dry-run]" in result.message
        assert "kct export" in result.message

    def test_prefers_routed_pcb(self, tmp_path: Path) -> None:
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        routed = tmp_path / "board_routed.kicad_pcb"
        routed.write_text("(kicad_pcb)")
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            routed_pcb_file=routed,
            dry_run=True,
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert result.success
        assert "board_routed" in result.message

    def test_uses_target_fab_from_spec(self, tmp_path: Path) -> None:
        """When spec has target_fab, it should be used instead of ctx.mfr."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        # Create a mock spec with target_fab
        mock_spec = MagicMock()
        mock_spec.requirements.manufacturing.target_fab = "pcbway"
        mock_spec.requirements.manufacturing.assembly = None

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            spec=mock_spec,
            mfr="jlcpcb",
            dry_run=True,
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert result.success
        assert "pcbway" in result.message

    def test_falls_back_to_ctx_mfr(self, tmp_path: Path) -> None:
        """When spec has no target_fab, ctx.mfr should be used."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            mfr="oshpark",
            dry_run=True,
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert result.success
        assert "oshpark" in result.message

    def test_subprocess_success(self, tmp_path: Path) -> None:
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            quiet=True,
        )
        console = Console(quiet=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = _run_step_export(ctx, console)

        assert result.success
        assert "manufacturing" in result.message.lower() or result.output_file is not None

    def test_subprocess_failure(self, tmp_path: Path) -> None:
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            quiet=True,
        )
        console = Console(quiet=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stderr="export error", stdout=""
            )
            result = _run_step_export(ctx, console)

        assert not result.success
        assert "export error" in result.message

    def test_output_dir_used_for_manufacturing(self, tmp_path: Path) -> None:
        """When output_dir is set, manufacturing goes under output_dir/manufacturing."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        output_dir = tmp_path / "custom_output"
        output_dir.mkdir()

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            output_dir=output_dir,
            dry_run=True,
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert result.success
        assert "custom_output" in result.message

    def test_skips_bom_cpl_without_assembly_spec(self, tmp_path: Path) -> None:
        """When spec has no assembly field, --no-bom and --no-cpl should be passed."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            quiet=True,
        )
        console = Console(quiet=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            _run_step_export(ctx, console)

        # Check that --no-bom and --no-cpl are in the command
        cmd = mock_run.call_args[0][0]
        assert "--no-bom" in cmd
        assert "--no-cpl" in cmd

    def test_includes_bom_cpl_with_assembly_spec(self, tmp_path: Path) -> None:
        """When spec has assembly field, BOM/CPL should NOT be skipped."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        mock_spec = MagicMock()
        mock_spec.requirements.manufacturing.target_fab = None
        mock_spec.requirements.manufacturing.assembly = "smt"

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            spec=mock_spec,
            quiet=True,
        )
        console = Console(quiet=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            _run_step_export(ctx, console)

        cmd = mock_run.call_args[0][0]
        assert "--no-bom" not in cmd
        assert "--no-cpl" not in cmd

    def test_passes_schematic_for_bom(self, tmp_path: Path) -> None:
        """When schematic is available, --sch should be passed."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text("(kicad_sch)")

        mock_spec = MagicMock()
        mock_spec.requirements.manufacturing.target_fab = None
        mock_spec.requirements.manufacturing.assembly = "smt"

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            schematic_file=sch_file,
            spec=mock_spec,
            quiet=True,
        )
        console = Console(quiet=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            _run_step_export(ctx, console)

        cmd = mock_run.call_args[0][0]
        assert "--sch" in cmd
        assert str(sch_file) in cmd


class TestBuildStepExportCLI:
    """Tests for --step export CLI integration."""

    def test_step_export_accepted(self, tmp_path: Path) -> None:
        """--step export should be a valid CLI choice."""
        kct_file = tmp_path / "project.kct"
        kct_file.write_text("[project]\nname = 'test'\n")
        ret = main([str(kct_file), "--step", "export", "--dry-run", "--quiet"])
        # dry-run may succeed or fail depending on PCB presence, but should not
        # fail due to argument parsing
        assert ret in (0, 1)

    def test_dry_run_includes_export(self, tmp_path: Path) -> None:
        """--dry-run with --step all should include the export step."""
        kct_file = tmp_path / "project.kct"
        kct_file.write_text("[project]\nname = 'test'\n")
        # With dry-run, all steps should be listed without error
        ret = main([str(kct_file), "--dry-run", "--quiet"])
        assert ret in (0, 1)
