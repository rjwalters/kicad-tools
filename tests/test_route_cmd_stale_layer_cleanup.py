"""Tests for stale ``_<N>layer.kicad_pcb`` artifact cleanup (issue #2674).

The ``kct route --auto-layers`` escalation paths rename the canonical
output to include the layer count when escalation actually happens
(e.g. ``board_routed_4layer.kicad_pcb``).  A previous failed-2L run
leaves that ``_4layer`` file on disk; the next run that succeeds on 2
layers writes the canonical ``board_routed.kicad_pcb`` but never
touches the stale 4L file.  The result is a confusing pair of routed
PCBs where only the canonical one reflects the current run.

The fix adds a ``_cleanup_stale_layer_artifacts`` helper that both
escalation entry points call before routing begins, deterministically
deleting prior-run ``_<N>layer.{kicad_pcb,kicad_prl}`` siblings.

This module covers:

1. The ``_cleanup_stale_layer_artifacts`` helper directly:
   - removes stale ``_4layer`` and ``_6layer`` siblings,
   - removes companion ``.kicad_prl`` files,
   - does nothing when no siblings exist,
   - leaves unrelated files (the canonical output, other boards) alone,
   - returns the list of removed paths for verification.
2. Acceptance criterion: after a simulated successful 2L run that
   re-uses an output directory containing a stale ``_4layer`` artifact
   from a prior failed run, only the canonical ``_routed.kicad_pcb``
   remains -- no ``_4layer`` siblings.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


class TestCleanupStaleLayerArtifacts:
    """Direct tests of the ``_cleanup_stale_layer_artifacts`` helper."""

    def test_removes_stale_4layer_pcb(self, tmp_path: Path):
        """A leftover ``<stem>_4layer.kicad_pcb`` is removed."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        canonical = tmp_path / "board_routed.kicad_pcb"
        stale_4l = tmp_path / "board_routed_4layer.kicad_pcb"
        stale_4l.write_text("stale 4L content")
        assert stale_4l.exists()

        removed = _cleanup_stale_layer_artifacts(canonical, quiet=True)

        assert not stale_4l.exists(), "Stale 4L artifact must be deleted"
        assert stale_4l in removed

    def test_removes_stale_6layer_pcb(self, tmp_path: Path):
        """A leftover ``<stem>_6layer.kicad_pcb`` is removed."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        canonical = tmp_path / "board_routed.kicad_pcb"
        stale_6l = tmp_path / "board_routed_6layer.kicad_pcb"
        stale_6l.write_text("stale 6L content")
        assert stale_6l.exists()

        removed = _cleanup_stale_layer_artifacts(canonical, quiet=True)

        assert not stale_6l.exists()
        assert stale_6l in removed

    def test_removes_stale_kicad_prl_siblings(self, tmp_path: Path):
        """Companion ``.kicad_prl`` files for stale layers are also removed."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        canonical = tmp_path / "board_routed.kicad_pcb"
        stale_4l_pcb = tmp_path / "board_routed_4layer.kicad_pcb"
        stale_4l_prl = tmp_path / "board_routed_4layer.kicad_prl"
        stale_6l_prl = tmp_path / "board_routed_6layer.kicad_prl"
        stale_4l_pcb.write_text("x")
        stale_4l_prl.write_text("x")
        stale_6l_prl.write_text("x")

        removed = _cleanup_stale_layer_artifacts(canonical, quiet=True)

        # All three should be gone, even though the .prl pair has no .pcb.
        assert not stale_4l_pcb.exists()
        assert not stale_4l_prl.exists()
        assert not stale_6l_prl.exists()
        assert set(removed) == {stale_4l_pcb, stale_4l_prl, stale_6l_prl}

    def test_no_op_when_no_stale_artifacts(self, tmp_path: Path):
        """When there are no stale siblings, helper is a no-op."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        canonical = tmp_path / "board_routed.kicad_pcb"
        canonical.write_text("canonical content")

        removed = _cleanup_stale_layer_artifacts(canonical, quiet=True)

        assert removed == []
        # Canonical file must NOT be deleted.
        assert canonical.exists()
        assert canonical.read_text() == "canonical content"

    def test_does_not_remove_canonical_output(self, tmp_path: Path):
        """The canonical (un-suffixed) output is never deleted, even when stale siblings exist."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        canonical = tmp_path / "board_routed.kicad_pcb"
        canonical.write_text("canonical content")
        stale = tmp_path / "board_routed_4layer.kicad_pcb"
        stale.write_text("stale")

        _cleanup_stale_layer_artifacts(canonical, quiet=True)

        assert canonical.exists()
        assert canonical.read_text() == "canonical content"
        assert not stale.exists()

    def test_does_not_touch_unrelated_files(self, tmp_path: Path):
        """Files for a different board (different stem) are untouched."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        canonical = tmp_path / "board_a_routed.kicad_pcb"
        other_board = tmp_path / "board_b_routed_4layer.kicad_pcb"
        other_board.write_text("board B's legitimate 4L output")

        removed = _cleanup_stale_layer_artifacts(canonical, quiet=True)

        # Different stem -- must NOT be cleaned up.
        assert other_board.exists()
        assert removed == []

    def test_does_not_remove_2layer_or_other_suffixes(self, tmp_path: Path):
        """Only the documented ``_4layer`` and ``_6layer`` suffixes are touched."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        canonical = tmp_path / "board_routed.kicad_pcb"
        # The escalation path never produces ``_2layer``, ``_8layer``, etc.
        # The helper must be conservative and only touch the suffixes the
        # escalation code is known to write.
        not_a_target_1 = tmp_path / "board_routed_2layer.kicad_pcb"
        not_a_target_2 = tmp_path / "board_routed_8layer.kicad_pcb"
        not_a_target_3 = tmp_path / "board_routed.backup.kicad_pcb"
        for p in (not_a_target_1, not_a_target_2, not_a_target_3):
            p.write_text("unrelated")

        _cleanup_stale_layer_artifacts(canonical, quiet=True)

        for p in (not_a_target_1, not_a_target_2, not_a_target_3):
            assert p.exists(), f"{p.name} should not have been touched"

    def test_handles_missing_output_directory_gracefully(self, tmp_path: Path):
        """No crash when the parent directory doesn't contain anything."""
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        # tmp_path is empty; canonical_path doesn't exist either.
        canonical = tmp_path / "nonexistent_routed.kicad_pcb"

        removed = _cleanup_stale_layer_artifacts(canonical, quiet=True)

        assert removed == []


# ---------------------------------------------------------------------------
# Acceptance test: simulate a 2L-success run reusing a directory containing
# a stale 4L artifact from a prior failed run.
# ---------------------------------------------------------------------------


class TestEscalationLeavesCleanOutputDirectory:
    """Issue #2674 acceptance criterion.

    After ``kct route --auto-layers`` succeeds on a 2L attempt, the
    output directory must contain exactly ONE routed PCB: the canonical
    ``<board>_routed.kicad_pcb``.  Any ``_4layer.kicad_pcb`` sibling
    from a prior failed run must be cleaned up at the start of the new
    run -- before routing begins, so the directory is in a defined
    state even if the new route subsequently crashes.
    """

    def test_stale_4layer_is_cleaned_before_2l_success_writes_canonical(self, tmp_path: Path):
        """Simulates the exact failure mode from issue #2674.

        Setup:
          - prior failed-2L run produced both:
              board_routed.kicad_pcb         (2L attempt, then renamed below)
              board_routed_4layer.kicad_pcb  (4L attempt, didn't succeed either)
            and the user re-routed with a code version where 2L now succeeds.
          - In the new run, the canonical output is written first, then the
            escalation breaks before reaching 4L.
          - Without the fix: the stale ``_4layer`` artifact persists.
          - With the fix: ``_cleanup_stale_layer_artifacts`` runs at startup
            and the directory is left with only the canonical file.

        We exercise the helper directly because spinning up a real route
        through the CLI requires loading a fully-formed PCB through the
        C++ backend, which is out of scope for a unit-level regression
        test.  The acceptance contract is: starting from a directory
        with stale siblings, after calling the cleanup helper and then
        writing the canonical output, only the canonical file remains.
        """
        from kicad_tools.cli.route_cmd import _cleanup_stale_layer_artifacts

        # Simulate prior-run state: both the canonical and a stale 4L exist.
        canonical = tmp_path / "charlieplex_3x3_routed.kicad_pcb"
        canonical.write_text("prior 2L content (will be overwritten by new run)")
        stale_4l = tmp_path / "charlieplex_3x3_routed_4layer.kicad_pcb"
        stale_4l.write_text("prior 4L attempt, never cleaned")
        stale_4l_prl = tmp_path / "charlieplex_3x3_routed_4layer.kicad_prl"
        stale_4l_prl.write_text("prior 4L state file")

        # Sanity: pre-cleanup directory has two routed PCBs.
        routed_pcbs_before = sorted(
            p.name
            for p in tmp_path.iterdir()
            if p.name.endswith("_routed.kicad_pcb")
            or p.name.endswith("_routed_4layer.kicad_pcb")
            or p.name.endswith("_routed_6layer.kicad_pcb")
        )
        assert len(routed_pcbs_before) == 2

        # ---- The fix runs at the top of the escalation function. ----
        _cleanup_stale_layer_artifacts(canonical, quiet=True)

        # ---- The escalation then proceeds and 2L succeeds: ----
        # the canonical file is (re-)written; rename is skipped because
        # final_result.layer_count == 2 so no ``_4layer`` is produced.
        canonical.write_text("new 2L content (success)")

        # Acceptance criterion: exactly ONE routed PCB present, the canonical.
        routed_pcbs_after = sorted(
            p.name for p in tmp_path.iterdir() if p.name.endswith(".kicad_pcb")
        )
        assert routed_pcbs_after == ["charlieplex_3x3_routed.kicad_pcb"], (
            f"Expected only the canonical routed PCB after successful 2L run, "
            f"got: {routed_pcbs_after}"
        )
        # And the companion .kicad_prl from the stale run is also gone.
        prls_after = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".kicad_prl")
        assert prls_after == [], f"Expected no stale .kicad_prl files, got: {prls_after}"
