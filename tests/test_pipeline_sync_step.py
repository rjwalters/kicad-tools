"""Tests for the pipeline sync step (kct pipeline --step sync).

Covers the in-process Reconciler integration added by issue #2730.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from kicad_tools.cli.pipeline_cmd import (
    ALL_STEPS,
    STEP_RUNNERS,
    PipelineContext,
    PipelineStep,
    _run_step_sync,
)
from kicad_tools.cli.pipeline_cmd import (
    main as pipeline_main,
)
from kicad_tools.sync.reconciler import SyncAnalysis, SyncChange

# Minimal PCB and schematic stubs used to exercise the runner without
# actually constructing valid KiCad files.  The Reconciler is monkey-patched
# in nearly every test, so the file contents don't need to parse.
STUB_PCB = "(kicad_pcb)\n"
STUB_SCH = "(kicad_sch)\n"


@pytest.fixture
def pcb_file(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_pcb"
    p.write_text(STUB_PCB)
    return p


@pytest.fixture
def sch_file(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_sch"
    p.write_text(STUB_SCH)
    return p


def _ctx(pcb: Path, sch: Path | None = None, **overrides) -> PipelineContext:
    """Build a minimal PipelineContext for tests."""
    return PipelineContext(
        pcb_file=pcb,
        schematic_file=sch,
        mfr="jlcpcb",
        layers="2",
        **overrides,
    )


class TestSyncStepOrdering:
    """Ordering and registration of the SYNC step."""

    def test_sync_step_in_all_steps(self) -> None:
        """SYNC is present in ALL_STEPS."""
        assert PipelineStep.SYNC in ALL_STEPS

    def test_sync_step_in_step_runners(self) -> None:
        """SYNC has a runner registered."""
        assert PipelineStep.SYNC in STEP_RUNNERS
        assert STEP_RUNNERS[PipelineStep.SYNC] is _run_step_sync

    def test_sync_step_falls_between_fix_erc_and_fix_silkscreen(self) -> None:
        """SYNC sits after FIX_ERC and before FIX_SILKSCREEN."""
        order = ALL_STEPS
        assert order.index(PipelineStep.FIX_ERC) < order.index(PipelineStep.SYNC)
        assert order.index(PipelineStep.SYNC) < order.index(PipelineStep.FIX_SILKSCREEN)

    def test_sync_step_before_route(self) -> None:
        """SYNC must run before ROUTE so routing sees a complete footprint set."""
        order = ALL_STEPS
        assert order.index(PipelineStep.SYNC) < order.index(PipelineStep.ROUTE)

    def test_sync_choice_advertised_in_help(self, pcb_file: Path) -> None:
        """The --step argparse choice list includes 'sync'."""
        # Trying an invalid value would raise SystemExit; 'sync' is accepted.
        with patch(
            "kicad_tools.cli.pipeline_cmd.run_pipeline",
            return_value=[],
        ):
            rc = pipeline_main(["--step", "sync", str(pcb_file)])
        assert rc in (0, 1)  # accepted by argparse (not 2)


class TestSyncStepSkipBehavior:
    """Skip / dry-run behavior of _run_step_sync."""

    def test_skipped_when_no_schematic(self, pcb_file: Path) -> None:
        """No schematic available -> skip with informative message."""
        ctx = _ctx(pcb_file, sch=None)
        console = Console(quiet=True)

        result = _run_step_sync(ctx, console)

        assert result.skipped is True
        assert result.success is True
        assert PipelineStep.SYNC in (result.step, PipelineStep.SYNC)
        assert "no .kicad_sch" in result.message

    def test_dry_run_skips_reconciler(self, pcb_file: Path, sch_file: Path) -> None:
        """--dry-run never instantiates a Reconciler."""
        ctx = _ctx(pcb_file, sch=sch_file, dry_run=True)
        console = Console(quiet=True)

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_recon:
            result = _run_step_sync(ctx, console)

        mock_recon.assert_not_called()
        assert result.success is True
        assert result.message.startswith("[dry-run]")


class TestSyncStepBehavior:
    """Analyze / apply behavior of _run_step_sync."""

    def test_in_sync_passes(self, pcb_file: Path, sch_file: Path) -> None:
        """An in-sync analysis returns success without warnings."""
        ctx = _ctx(pcb_file, sch=sch_file)
        console = Console(quiet=True)

        analysis = SyncAnalysis()  # empty analysis is in sync
        assert analysis.is_in_sync

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, console)

        assert result.success is True
        assert result.warning is False
        assert result.skipped is False
        assert "in sync" in result.message

    def test_orphans_block_without_force(self, pcb_file: Path, sch_file: Path) -> None:
        """Schematic orphans halt the pipeline unless --force or --apply-sync."""
        ctx = _ctx(pcb_file, sch=sch_file)
        console = Console(quiet=True)

        analysis = SyncAnalysis(schematic_orphans=["U99", "R42"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, console)

        assert result.success is False
        assert "schematic-only" in result.message
        assert "--apply-sync" in result.message
        assert "--force" in result.message

    def test_force_continues_with_warning(self, pcb_file: Path, sch_file: Path) -> None:
        """--force lets the pipeline proceed past orphan drift as a warning."""
        ctx = _ctx(pcb_file, sch=sch_file, force=True)
        console = Console(quiet=True)

        analysis = SyncAnalysis(schematic_orphans=["U99"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, console)

        assert result.success is True
        assert result.warning is True
        assert "continuing" in result.message.lower()

    def test_value_mismatch_only_is_warning(self, pcb_file: Path, sch_file: Path) -> None:
        """Value mismatches alone are non-blocking warnings."""
        ctx = _ctx(pcb_file, sch=sch_file)
        console = Console(quiet=True)

        analysis = SyncAnalysis(
            value_mismatches=[
                {
                    "reference": "R1",
                    "schematic_value": "10k",
                    "pcb_value": "1k",
                }
            ]
        )

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, console)

        assert result.success is True
        assert result.warning is True
        assert "value mismatch" in result.message.lower()

    def test_apply_sync_invokes_apply(self, pcb_file: Path, sch_file: Path) -> None:
        """--apply-sync calls Reconciler.apply with dry_run=False, min_confidence=high."""
        ctx = _ctx(pcb_file, sch=sch_file, apply_sync=True)
        console = Console(quiet=True)

        analysis = SyncAnalysis(
            schematic_orphans=["U99"],
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "U99",
                    "footprint": "Package_SO:SOIC-8",
                    "value": "AMP",
                }
            ],
        )
        post_analysis = SyncAnalysis()  # in-sync after apply
        applied_change = SyncChange(
            reference="U99",
            change_type="add_footprint",
            old_value="",
            new_value="Package_SO:SOIC-8",
            applied=True,
        )

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            inst = mock_cls.return_value
            inst.analyze.side_effect = [analysis, post_analysis]
            inst.apply.return_value = [applied_change]

            result = _run_step_sync(ctx, console)

        # apply() must be invoked with the expected contract
        inst.apply.assert_called_once()
        kwargs = inst.apply.call_args.kwargs
        assert kwargs.get("dry_run") is False
        assert kwargs.get("min_confidence") == "high"
        assert kwargs.get("remove_orphans") is False

        assert result.success is True
        assert "applied" in result.message
        # post-apply analysis was in sync -> no warning
        assert result.warning is False

    def test_apply_sync_residual_drift_marks_warning(self, pcb_file: Path, sch_file: Path) -> None:
        """When apply leaves residual drift, the step is success+warning."""
        ctx = _ctx(pcb_file, sch=sch_file, apply_sync=True)
        console = Console(quiet=True)

        analysis = SyncAnalysis(schematic_orphans=["U99", "U100"])
        post_analysis = SyncAnalysis(schematic_orphans=["U100"])  # partial
        applied_change = SyncChange(
            reference="U99",
            change_type="add_footprint",
            old_value="",
            new_value="x",
            applied=True,
        )

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            inst = mock_cls.return_value
            inst.analyze.side_effect = [analysis, post_analysis]
            inst.apply.return_value = [applied_change]

            result = _run_step_sync(ctx, console)

        assert result.success is True
        assert result.warning is True

    def test_apply_sync_failure_returns_failure(self, pcb_file: Path, sch_file: Path) -> None:
        """If Reconciler.apply raises, the step reports failure."""
        ctx = _ctx(pcb_file, sch=sch_file, apply_sync=True)
        console = Console(quiet=True)

        analysis = SyncAnalysis(schematic_orphans=["U99"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            inst = mock_cls.return_value
            inst.analyze.return_value = analysis
            inst.apply.side_effect = RuntimeError("boom")
            result = _run_step_sync(ctx, console)

        assert result.success is False
        assert "apply failed" in result.message

    def test_analyze_failure_returns_failure(self, pcb_file: Path, sch_file: Path) -> None:
        """If analyze() raises, the step reports failure without halting elsewhere."""
        ctx = _ctx(pcb_file, sch=sch_file)
        console = Console(quiet=True)

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.side_effect = RuntimeError("cannot load")
            result = _run_step_sync(ctx, console)

        assert result.success is False
        assert "failed to analyze" in result.message


class TestSyncStepNoSubprocess:
    """The sync step must use the in-process Reconciler API."""

    def test_no_subprocess_in_runner_source(self) -> None:
        """Static check: _run_step_sync source does not shell out via subprocess."""
        import inspect

        src = inspect.getsource(_run_step_sync)
        # The runner must not call subprocess.run() or _run_subprocess_step()
        assert "subprocess.run" not in src
        assert "_run_subprocess_step" not in src


class TestApplySyncFlag:
    """Argparse plumbing for the --apply-sync flag."""

    def test_apply_sync_threaded_into_context(self, pcb_file: Path) -> None:
        """--apply-sync flag is threaded into PipelineContext.apply_sync."""
        captured: dict = {}

        def _capture(ctx, steps=None):
            captured["apply_sync"] = ctx.apply_sync
            return []

        with patch("kicad_tools.cli.pipeline_cmd.run_pipeline", side_effect=_capture):
            pipeline_main(["--step", "sync", "--apply-sync", str(pcb_file)])

        assert captured.get("apply_sync") is True

    def test_apply_sync_default_false(self, pcb_file: Path) -> None:
        """Without --apply-sync, ctx.apply_sync defaults to False."""
        captured: dict = {}

        def _capture(ctx, steps=None):
            captured["apply_sync"] = ctx.apply_sync
            return []

        with patch("kicad_tools.cli.pipeline_cmd.run_pipeline", side_effect=_capture):
            pipeline_main(["--step", "sync", str(pcb_file)])

        assert captured.get("apply_sync") is False
