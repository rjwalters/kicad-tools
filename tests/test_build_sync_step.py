"""Tests for the build pipeline SYNC step (issue #2773).

Before this fix, ``kct build`` ran ``kct validate --sync`` inside the
VERIFY step and discarded the result into a cosmetic message suffix.
The build continued through EXPORT, producing a BOM/manufacturing
package even when the schematic and PCB had drifted (board 05 BLDC).

These tests pin the new behaviour: ``BuildStep.SYNC`` runs the
in-process :class:`Reconciler` right after PCB write, *before* OUTLINE
and all downstream work, and HALTS the build when schematic orphans are
detected.  ``--force`` is the only escape hatch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from kicad_tools.cli.build_cmd import (
    BuildContext,
    BuildStep,
    _run_step_sync,
    main,
)
from kicad_tools.sync.reconciler import SyncAnalysis

# Stubs used to exercise the runner without constructing valid KiCad
# files.  The Reconciler is monkey-patched in nearly every test, so the
# file contents do not need to parse.
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


def _make_ctx(
    schematic: Path | None,
    pcb: Path | None,
    *,
    output_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    quiet: bool = True,
) -> BuildContext:
    return BuildContext(
        project_dir=Path("/tmp"),
        spec_file=None,
        schematic_file=schematic,
        pcb_file=pcb,
        output_dir=output_dir,
        dry_run=dry_run,
        quiet=quiet,
        force=force,
    )


# ---------------------------------------------------------------------------
# Enum + pipeline wiring
# ---------------------------------------------------------------------------


class TestBuildStepEnum:
    """The SYNC enum entry and CLI choice are part of the public surface."""

    def test_sync_is_in_buildstep_enum(self) -> None:
        """A new BuildStep.SYNC value exists and round-trips through str()."""
        assert BuildStep.SYNC.value == "sync"
        assert BuildStep("sync") is BuildStep.SYNC

    def test_sync_falls_between_pcb_and_outline_in_enum(self) -> None:
        """SYNC sits between PCB and OUTLINE in the enum definition."""
        members = list(BuildStep.__members__.keys())
        assert members.index("PCB") < members.index("SYNC")
        assert members.index("SYNC") < members.index("OUTLINE")

    def test_sync_is_a_cli_step_choice(self) -> None:
        """``--step sync`` must be accepted by the argument parser.

        argparse rejects unknown choices with SystemExit(2); the rest of
        the build pipeline returns an int exit code instead.
        """
        # Unknown step value must be rejected.
        with pytest.raises(SystemExit) as exc_info:
            main(["--step", "this-step-does-not-exist", "/tmp"])
        assert exc_info.value.code == 2

        # Valid step value must be accepted (argparse does not raise).
        # Failure later in the pipeline returns a non-zero int.
        rc = main(["--step", "sync", "/nonexistent-project-path-for-test"])
        assert isinstance(rc, int)
        # Bogus path -> error int, but not argparse rejection.
        assert rc != 2


# ---------------------------------------------------------------------------
# _run_step_sync behaviour
# ---------------------------------------------------------------------------


class TestSyncStepSkipBehaviour:
    """Skip / dry-run behaviour of _run_step_sync."""

    def test_skipped_when_no_schematic(self, pcb_file: Path) -> None:
        """No schematic available -> skip with informative message."""
        ctx = _make_ctx(schematic=None, pcb=pcb_file)
        result = _run_step_sync(ctx, Console(quiet=True))
        assert result.success is True
        assert "no schematic" in result.message.lower()

    def test_skipped_when_schematic_path_missing(self, pcb_file: Path, tmp_path: Path) -> None:
        """Schematic path set but file does not exist -> skip."""
        missing = tmp_path / "missing.kicad_sch"
        ctx = _make_ctx(schematic=missing, pcb=pcb_file)
        result = _run_step_sync(ctx, Console(quiet=True))
        assert result.success is True
        assert "no schematic" in result.message.lower()

    def test_fail_when_no_pcb(self, sch_file: Path) -> None:
        """Schematic present but no PCB to reconcile against -> failure."""
        ctx = _make_ctx(schematic=sch_file, pcb=None)
        result = _run_step_sync(ctx, Console(quiet=True))
        assert result.success is False
        assert "pcb" in result.message.lower()

    def test_dry_run_skips_reconciler(self, pcb_file: Path, sch_file: Path) -> None:
        """--dry-run never instantiates a Reconciler."""
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file, dry_run=True)

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_recon:
            result = _run_step_sync(ctx, Console(quiet=True))

        mock_recon.assert_not_called()
        assert result.success is True
        assert result.message.startswith("[dry-run]")


class TestSyncStepBehaviour:
    """Analyze behaviour of _run_step_sync."""

    def test_in_sync_passes(self, pcb_file: Path, sch_file: Path) -> None:
        """An in-sync analysis returns success without warnings."""
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file)

        analysis = SyncAnalysis()  # empty analysis is in sync
        assert analysis.is_in_sync

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, Console(quiet=True))

        assert result.success is True
        assert "in sync" in result.message

    def test_orphans_block_without_force(self, pcb_file: Path, sch_file: Path) -> None:
        """Schematic orphans halt the build unless --force is set.

        This is the gate that prevents board 05's unbuildable
        manufacturing package from being shipped: schematic_orphans means
        the BOM lists components that are not on the PCB.
        """
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file)

        analysis = SyncAnalysis(schematic_orphans=["C18", "C19"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, Console(quiet=True))

        assert result.success is False
        assert "schematic-only" in result.message
        # Actionable error: tell the user how to bypass or fix the gate
        assert "--force" in result.message

    def test_force_continues_with_warning(self, pcb_file: Path, sch_file: Path) -> None:
        """--force lets the build proceed past orphan drift as a warning."""
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file, force=True)

        analysis = SyncAnalysis(schematic_orphans=["C18"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, Console(quiet=True))

        assert result.success is True
        assert "continuing" in result.message.lower()

    def test_value_mismatch_only_is_warning(self, pcb_file: Path, sch_file: Path) -> None:
        """Value mismatches alone are non-blocking warnings (success=True)."""
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file)

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
            result = _run_step_sync(ctx, Console(quiet=True))

        assert result.success is True
        assert "value mismatch" in result.message.lower()

    def test_footprint_mismatch_only_is_warning(self, pcb_file: Path, sch_file: Path) -> None:
        """Footprint mismatches alone are non-blocking warnings."""
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file)

        analysis = SyncAnalysis(
            footprint_mismatches=[
                {
                    "reference": "U1",
                    "schematic_footprint": "SOIC-8",
                    "pcb_footprint": "DIP-8",
                }
            ]
        )

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, Console(quiet=True))

        assert result.success is True
        assert "footprint mismatch" in result.message.lower()

    def test_pcb_orphans_only_are_warning(self, pcb_file: Path, sch_file: Path) -> None:
        """PCB-only refs (mounting holes, fiducials) alone are warnings."""
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file)

        analysis = SyncAnalysis(pcb_orphans=["MH1", "MH2", "MH3", "MH4"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            result = _run_step_sync(ctx, Console(quiet=True))

        assert result.success is True
        assert "pcb-only" in result.message.lower()

    def test_analyze_failure_returns_failure(self, pcb_file: Path, sch_file: Path) -> None:
        """If Reconciler construction raises, the step reports failure."""
        ctx = _make_ctx(schematic=sch_file, pcb=pcb_file)

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.side_effect = RuntimeError("cannot load")
            result = _run_step_sync(ctx, Console(quiet=True))

        assert result.success is False
        assert "failed to analyze" in result.message


class TestSyncStepNoSubprocess:
    """The sync step must use the in-process Reconciler API."""

    def test_no_subprocess_in_runner_source(self) -> None:
        """Static check: _run_step_sync source does not shell out via subprocess."""
        import inspect

        src = inspect.getsource(_run_step_sync)
        # The runner must not call subprocess.run() to avoid the bug
        # the original VERIFY-step sync subprocess exhibited.
        assert "subprocess.run" not in src
        assert "subprocess.Popen" not in src


# ---------------------------------------------------------------------------
# Integration: SYNC halts build before EXPORT
# ---------------------------------------------------------------------------


class TestSyncBlocksManufacturing:
    """End-to-end-ish test: drift detection prevents BOM/CPL writes."""

    def test_drift_in_isolated_sync_step_exits_nonzero(
        self, pcb_file: Path, sch_file: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """`kct build --step sync` exits non-zero when drift is present.

        This is the smallest end-to-end assertion: the integration
        between the dispatch ladder and `_run_step_sync` is intact, and
        a Reconciler reporting schematic_orphans propagates into a
        non-zero process exit -- which is what `kct build`'s caller
        (e.g. CI, kct pipeline) relies on to short-circuit before any
        manufacturing artefacts land on disk.
        """
        # Set up project dir with both files at the expected locations
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text(STUB_PCB)
        (project_dir / "board.kicad_sch").write_text(STUB_SCH)

        analysis = SyncAnalysis(schematic_orphans=["C18", "C19"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            rc = main(["--step", "sync", str(project_dir)])

        assert rc != 0, "SYNC step must exit non-zero when drift is detected"

    def test_drift_with_force_exits_zero(
        self, pcb_file: Path, sch_file: Path, tmp_path: Path
    ) -> None:
        """`kct build --step sync --force` exits zero even with drift."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text(STUB_PCB)
        (project_dir / "board.kicad_sch").write_text(STUB_SCH)

        analysis = SyncAnalysis(schematic_orphans=["C18"])

        with patch("kicad_tools.sync.reconciler.Reconciler") as mock_cls:
            mock_cls.return_value.analyze.return_value = analysis
            rc = main(["--step", "sync", "--force", str(project_dir)])

        assert rc == 0, "--force must allow the build to continue past drift"


# ---------------------------------------------------------------------------
# Ordering invariant: SYNC must run before any downstream/manufacturing step
# ---------------------------------------------------------------------------


class TestSyncOrdering:
    """SYNC must run between PCB and OUTLINE in the default chain.

    This is the load-bearing invariant: SYNC must fire before OUTLINE,
    PLACEMENT, ZONES, SILKSCREEN, ROUTE, STITCH, VERIFY, and EXPORT so
    that no expensive downstream work (and crucially, no
    BOM/manufacturing artefacts) is produced when the schematic and PCB
    have drifted.
    """

    def test_default_chain_orders_sync_after_pcb_before_outline(self) -> None:
        """Statically check the default chain ordering.

        We parse the source of `main` to find the BuildStep list rather
        than running it (which would require a full project setup).
        """
        import inspect

        from kicad_tools.cli import build_cmd

        src = inspect.getsource(build_cmd.main)
        # The default chain is a literal list inside main(); just check
        # the relative order of the substrings is correct.  This is a
        # weak but extremely cheap invariant to enforce.
        pcb_idx = src.index("BuildStep.PCB")
        sync_idx = src.index("BuildStep.SYNC")
        outline_idx = src.index("BuildStep.OUTLINE")
        export_idx = src.index("BuildStep.EXPORT")

        assert pcb_idx < sync_idx < outline_idx, (
            "SYNC must appear between PCB and OUTLINE in main()'s default chain"
        )
        assert sync_idx < export_idx, "SYNC must precede EXPORT in the default chain"
