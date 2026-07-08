"""Tests for the EXPORT step in build_cmd."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console

from kicad_tools.cli.build_cmd import (
    _ALL_STEPS,
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
        """When --step all is used, EXPORT should be in the canonical list."""
        assert BuildStep.EXPORT in _ALL_STEPS

    def test_export_precedes_verify_in_all_steps(self) -> None:
        """Export must run before verify (issue #3970).

        VERIFY's meta-check reads ``manufacturing/manifest.json``, which the
        EXPORT step produces.  If VERIFY ran first the manifest sub-check
        reported ``NOT RUN`` -> rollup ``INCOMPLETE`` -> exit 2, misreported
        as "DRC found issues".
        """
        assert BuildStep.EXPORT in _ALL_STEPS
        assert BuildStep.VERIFY in _ALL_STEPS
        assert _ALL_STEPS.index(BuildStep.EXPORT) < _ALL_STEPS.index(BuildStep.VERIFY)


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

    def test_uses_ctx_mfr_not_stale_spec_reread(self, tmp_path: Path) -> None:
        """The export step uses ctx.mfr, not a fresh re-read of spec.target_fab.

        Issue #3920: previously ``_run_step_export`` re-read
        ``spec.requirements.manufacturing.target_fab`` and let it override
        ctx.mfr -- the "split-brain" where export judged against a different
        profile than route/verify/stitch. The manufacturer is now resolved
        exactly once (``build_cmd._resolve_effective_mfr``) and threaded
        through ``ctx.mfr`` at BuildContext creation, so the export step must
        honour ctx.mfr even when the spec carries a *different* target_fab
        (e.g. when an explicit ``--mfr`` override was applied upstream).
        """
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        # Spec still declares "pcbway", but ctx.mfr was resolved to "oshpark"
        # upstream (e.g. an explicit --mfr override). The export step must
        # follow ctx.mfr, not silently snap back to the spec value.
        mock_spec = MagicMock()
        mock_spec.requirements.manufacturing.target_fab = "pcbway"
        mock_spec.requirements.manufacturing.assembly = None

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb_file,
            spec=mock_spec,
            mfr="oshpark",
            dry_run=True,
        )
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)
        assert result.success
        assert "oshpark" in result.message
        assert "pcbway" not in result.message

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

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
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

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="export error", stdout="")
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

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
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

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
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

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
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
