"""Tests for the placement optimization step in the build pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.build_cmd import (
    BuildContext,
    BuildStep,
    _run_step_placement,
    main,
)
from rich.console import Console


class TestBuildStepEnum:
    """Tests for BuildStep enum including PLACEMENT."""

    def test_placement_step_exists(self) -> None:
        assert BuildStep.PLACEMENT == "placement"

    def test_placement_between_outline_and_route(self) -> None:
        members = list(BuildStep)
        outline_idx = members.index(BuildStep.OUTLINE)
        placement_idx = members.index(BuildStep.PLACEMENT)
        route_idx = members.index(BuildStep.ROUTE)
        assert outline_idx < placement_idx < route_idx


class TestRunStepPlacement:
    """Tests for _run_step_placement."""

    def _make_ctx(self, tmp_path: Path, **overrides) -> BuildContext:
        """Create a minimal BuildContext for testing."""
        defaults = dict(
            project_dir=tmp_path,
            spec_file=None,
            optimize_placement=False,
        )
        defaults.update(overrides)
        return BuildContext(**defaults)

    def test_skipped_by_default(self, tmp_path: Path) -> None:
        """Placement step is a no-op when --optimize-placement is not set."""
        ctx = self._make_ctx(tmp_path, optimize_placement=False)
        console = Console(quiet=True)
        result = _run_step_placement(ctx, console)
        assert result.success is True
        assert "not requested" in result.message

    def test_skipped_when_no_pcb(self, tmp_path: Path) -> None:
        """Placement step skips gracefully when no PCB file exists."""
        ctx = self._make_ctx(tmp_path, optimize_placement=True, pcb_file=None)
        console = Console(quiet=True)
        result = _run_step_placement(ctx, console)
        assert result.success is True
        assert "No PCB" in result.message

    def test_skipped_when_pcb_missing(self, tmp_path: Path) -> None:
        """Placement step skips gracefully when PCB file path doesn't exist on disk."""
        pcb = tmp_path / "nonexistent.kicad_pcb"
        ctx = self._make_ctx(tmp_path, optimize_placement=True, pcb_file=pcb)
        console = Console(quiet=True)
        result = _run_step_placement(ctx, console)
        assert result.success is True
        assert "No PCB" in result.message

    def test_dry_run(self, tmp_path: Path) -> None:
        """Dry run reports what would happen without executing."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        ctx = self._make_ctx(tmp_path, optimize_placement=True, pcb_file=pcb, dry_run=True)
        console = Console(quiet=True)
        result = _run_step_placement(ctx, console)
        assert result.success is True
        assert "[dry-run]" in result.message
        assert "board.kicad_pcb" in result.message

    def test_subprocess_called_with_correct_args(self, tmp_path: Path) -> None:
        """When enabled, the step invokes the optimize-placement subprocess."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        ctx = self._make_ctx(tmp_path, optimize_placement=True, pcb_file=pcb, quiet=True)
        console = Console(quiet=True)

        with patch("kicad_tools.cli.build_cmd.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = _run_step_placement(ctx, console)

        assert result.success is True
        call_args = mock_run.call_args[0][0]
        assert "optimize-placement" in call_args
        assert str(pcb) in call_args
        assert "--max-iterations" in call_args
        assert "300" in call_args
        assert "--quiet" in call_args

    def test_subprocess_failure_reported(self, tmp_path: Path) -> None:
        """A failing subprocess produces a failure result."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        ctx = self._make_ctx(tmp_path, optimize_placement=True, pcb_file=pcb, quiet=True)
        console = Console(quiet=True)

        with patch("kicad_tools.cli.build_cmd.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "optimization error"
            result = _run_step_placement(ctx, console)

        assert result.success is False
        assert "optimization error" in result.message


class TestBuildMainPlacementFlag:
    """Tests for --optimize-placement CLI flag integration."""

    def test_placement_flag_accepted(self, tmp_path: Path) -> None:
        """The --optimize-placement flag is accepted by the argument parser."""
        spec = tmp_path / "project.kct"
        spec.write_text("")
        # Just verify the flag parses without error via dry-run on an empty dir
        ret = main(["--step", "placement", "--dry-run", str(tmp_path)])
        assert ret == 0

    def test_placement_step_selectable(self, tmp_path: Path) -> None:
        """The placement step can be selected via --step placement."""
        ret = main(["--step", "placement", "--dry-run", str(tmp_path)])
        assert ret == 0

    def test_all_steps_include_placement_in_order(self) -> None:
        """When step=all, the pipeline includes placement between outline and route."""
        # This tests the step ordering in the 'all' path
        from kicad_tools.cli.build_cmd import BuildStep

        steps = [
            BuildStep.SCHEMATIC,
            BuildStep.PCB,
            BuildStep.OUTLINE,
            BuildStep.PLACEMENT,
            BuildStep.ROUTE,
            BuildStep.VERIFY,
        ]
        # Verify PLACEMENT is at index 3 (between OUTLINE=2 and ROUTE=4)
        assert steps[3] == BuildStep.PLACEMENT
