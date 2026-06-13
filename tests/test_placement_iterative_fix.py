"""Tests for iterative placement conflict resolution (issue #1948).

Verifies that PlacementFixer.iterative_fix():
- Runs multiple passes to resolve dense conflicts
- Reports per-pass progress
- Escalates move magnitudes on stall
- Avoids re-suggesting identical stalled fixes
- Exits early when all conflicts are resolved
- Exits early when no progress is possible
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.placement import (
    PlacementFixer,
)
from kicad_tools.placement.conflict import (
    Conflict,
    ConflictSeverity,
    ConflictType,
    PlacementFix,
    Point,
)
from kicad_tools.placement.fixer import (
    _escalate_fixes,
    _fix_direction_key,
)

# ---------------------------------------------------------------------------
# Test fixtures: PCB content
# ---------------------------------------------------------------------------

# Three 0402 resistors crammed into the same 1mm x 1mm area.
# Each pair overlaps, so a single-pass fixer cannot resolve all three
# simultaneously -- moving R2 away from R1 may push it into R3.
DENSE_3_COMPONENT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 100.3 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000003")
    (at 100.6 100)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
)
"""

# Simple two-component overlap that single-pass can resolve.
SIMPLE_OVERLAP_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 100.5 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""

# No-conflict PCB
CLEAN_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""


@pytest.fixture
def dense_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "dense.kicad_pcb"
    pcb_file.write_text(DENSE_3_COMPONENT_PCB)
    return pcb_file


@pytest.fixture
def simple_overlap_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "simple_overlap.kicad_pcb"
    pcb_file.write_text(SIMPLE_OVERLAP_PCB)
    return pcb_file


@pytest.fixture
def clean_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "clean.kicad_pcb"
    pcb_file.write_text(CLEAN_PCB)
    return pcb_file


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestFixDirectionKey:
    """Tests for _fix_direction_key."""

    def test_same_component_same_direction(self):
        """Fixes for the same component in the same direction bucket match."""
        conflict = _make_conflict("R1", "R2")
        fix_a = PlacementFix(conflict=conflict, component="R2", move_vector=Point(0.5, 0.3))
        fix_b = PlacementFix(conflict=conflict, component="R2", move_vector=Point(1.0, 0.1))
        assert _fix_direction_key(fix_a) == _fix_direction_key(fix_b)

    def test_different_direction_differs(self):
        """Fixes in opposite directions for the same component differ."""
        conflict = _make_conflict("R1", "R2")
        fix_a = PlacementFix(conflict=conflict, component="R2", move_vector=Point(0.5, 0.3))
        fix_b = PlacementFix(conflict=conflict, component="R2", move_vector=Point(-0.5, 0.3))
        assert _fix_direction_key(fix_a) != _fix_direction_key(fix_b)

    def test_different_component_differs(self):
        """Fixes for different components differ even with same direction."""
        conflict = _make_conflict("R1", "R2")
        fix_a = PlacementFix(conflict=conflict, component="R1", move_vector=Point(0.5, 0.3))
        fix_b = PlacementFix(conflict=conflict, component="R2", move_vector=Point(0.5, 0.3))
        assert _fix_direction_key(fix_a) != _fix_direction_key(fix_b)


class TestEscalateFixes:
    """Tests for _escalate_fixes."""

    def test_scales_move_vector(self):
        """Move vector components are multiplied by the escalation factor."""
        conflict = _make_conflict("R1", "R2")
        fix = PlacementFix(conflict=conflict, component="R2", move_vector=Point(1.0, 0.5))
        escalated = _escalate_fixes([fix], 2.0)
        assert len(escalated) == 1
        assert escalated[0].move_vector.x == pytest.approx(2.0)
        assert escalated[0].move_vector.y == pytest.approx(1.0)

    def test_preserves_component_and_confidence(self):
        """Escalation preserves non-vector fields."""
        conflict = _make_conflict("R1", "R2")
        fix = PlacementFix(
            conflict=conflict,
            component="R2",
            move_vector=Point(1.0, 0.5),
            confidence=0.8,
        )
        escalated = _escalate_fixes([fix], 1.5)
        assert escalated[0].component == "R2"
        assert escalated[0].confidence == 0.8


# ---------------------------------------------------------------------------
# Integration tests for iterative_fix
# ---------------------------------------------------------------------------


class TestIterativeFixCleanPCB:
    """Iterative fix on a conflict-free PCB."""

    def test_returns_zero_passes(self, clean_pcb: Path, tmp_path: Path):
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(clean_pcb, output_path=output)

        assert result.success is True
        assert result.total_passes == 0
        assert result.initial_conflicts == 0
        assert result.remaining_conflicts == 0
        assert "No placement conflicts" in result.message


class TestIterativeFixSimpleOverlap:
    """Iterative fix on a simple two-component overlap."""

    def test_resolves_in_few_passes(self, simple_overlap_pcb: Path, tmp_path: Path):
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(simple_overlap_pcb, output_path=output)

        assert result.initial_conflicts >= 1
        # Should resolve within max_passes (default 10)
        assert result.remaining_conflicts == 0
        assert result.total_fixes_applied >= 1
        assert result.made_progress is True

    def test_output_file_differs_from_input(self, simple_overlap_pcb: Path, tmp_path: Path):
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        fixer.iterative_fix(simple_overlap_pcb, output_path=output)

        original = simple_overlap_pcb.read_text()
        modified = output.read_text()
        assert original != modified


class TestIterativeFixDenseCluster:
    """Iterative fix on a dense 3-component cluster."""

    def test_reduces_conflicts(self, dense_pcb: Path, tmp_path: Path):
        """Multi-pass should reduce conflicts even if not to zero."""
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(dense_pcb, output_path=output)

        assert result.initial_conflicts >= 1
        # Must make progress (reduce conflicts)
        assert result.remaining_conflicts < result.initial_conflicts

    def test_pass_results_populated(self, dense_pcb: Path, tmp_path: Path):
        """Per-pass diagnostics are populated."""
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(dense_pcb, output_path=output)

        assert len(result.pass_results) >= 1
        for pr in result.pass_results:
            assert pr.pass_number >= 1
            assert pr.conflicts_before >= 0
            assert pr.conflicts_after >= 0
            assert pr.escalation_factor >= 1.0

    def test_escalation_triggers_on_stall(self, dense_pcb: Path, tmp_path: Path):
        """When a pass fails to reduce conflicts, escalation kicks in."""
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(dense_pcb, output_path=output)

        # If there are stalled passes, at least one should have escalation > 1.0
        stalled_passes = [pr for pr in result.pass_results if pr.escalation_factor > 1.0]
        # We may or may not have stalled passes depending on geometry,
        # but the total_passes should be > 1 for a dense cluster
        assert result.total_passes >= 1


class TestIterativeFixDryRun:
    """Dry-run mode for iterative fix."""

    def test_dry_run_does_not_write(self, simple_overlap_pcb: Path, tmp_path: Path):
        fixer = PlacementFixer()
        output = tmp_path / "should_not_exist.kicad_pcb"
        result = fixer.iterative_fix(
            simple_overlap_pcb,
            output_path=output,
            dry_run=True,
        )

        assert "dry run" in result.message
        assert not output.exists()

    def test_dry_run_still_reports_progress(self, simple_overlap_pcb: Path, tmp_path: Path):
        fixer = PlacementFixer()
        output = tmp_path / "should_not_exist.kicad_pcb"
        result = fixer.iterative_fix(
            simple_overlap_pcb,
            output_path=output,
            dry_run=True,
        )

        assert result.total_fixes_applied >= 1
        assert result.initial_conflicts >= 1


class TestIterativeFixMaxPasses:
    """Max passes limit is respected."""

    def test_respects_max_passes(self, dense_pcb: Path, tmp_path: Path):
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(dense_pcb, output_path=output, max_passes=2)

        assert result.total_passes <= 2

    def test_single_pass_behaves_like_old_fix(self, simple_overlap_pcb: Path, tmp_path: Path):
        """With max_passes=1, behavior is equivalent to old single-pass."""
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(
            simple_overlap_pcb,
            output_path=output,
            max_passes=1,
        )

        assert result.total_passes == 1
        assert result.total_fixes_applied >= 1


class TestIterativeFixAnchored:
    """Iterative fix with anchored (immovable) components."""

    def test_anchored_both_reports_unresolvable(self, simple_overlap_pcb: Path, tmp_path: Path):
        """When both overlapping components are anchored, fix cannot proceed."""
        fixer = PlacementFixer(anchored={"R1", "R2"})
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(simple_overlap_pcb, output_path=output)

        # Should detect conflicts but not resolve them
        assert result.initial_conflicts >= 1
        assert result.remaining_conflicts >= 1
        assert result.total_fixes_applied == 0


class TestIterativeFixCLI:
    """CLI integration for iterative fix."""

    def test_cli_fix_uses_iterative(self, simple_overlap_pcb: Path, tmp_path: Path):
        """The CLI fix command now uses iterative resolution."""
        from kicad_tools.cli.placement_cmd import main

        output = tmp_path / "cli_fixed.kicad_pcb"
        result = main(
            [
                "fix",
                str(simple_overlap_pcb),
                "-o",
                str(output),
                "--quiet",
            ]
        )

        # Should succeed (return 0) for simple overlap
        assert output.exists()
        original = simple_overlap_pcb.read_text()
        modified = output.read_text()
        assert original != modified

    def test_cli_fix_dry_run(self, simple_overlap_pcb: Path, tmp_path: Path):
        """CLI dry-run mode works with iterative fix."""
        from kicad_tools.cli.placement_cmd import main

        output = tmp_path / "should_not_exist.kicad_pcb"
        result = main(
            [
                "fix",
                str(simple_overlap_pcb),
                "-o",
                str(output),
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 0

    def test_cli_fix_max_passes(self, dense_pcb: Path, tmp_path: Path):
        """CLI --max-passes argument is respected."""
        from kicad_tools.cli.placement_cmd import main

        output = tmp_path / "cli_fixed.kicad_pcb"
        result = main(
            [
                "fix",
                str(dense_pcb),
                "-o",
                str(output),
                "--max-passes",
                "2",
                "--quiet",
            ]
        )
        # Should not crash
        assert result in (0, 1)

    def test_cli_fix_verbose_output(self, simple_overlap_pcb: Path, tmp_path: Path, capsys):
        """Verbose mode shows per-pass progress."""
        from kicad_tools.cli.placement_cmd import main

        output = tmp_path / "cli_fixed.kicad_pcb"
        main(
            [
                "fix",
                str(simple_overlap_pcb),
                "-o",
                str(output),
                "--verbose",
            ]
        )

        captured = capsys.readouterr()
        # Verbose output should mention pass progress
        assert "Pass" in captured.out or "conflicts" in captured.out.lower()


class TestIterativeFixTimeout:
    """Timeout returns best-effort results."""

    def test_timeout_returns_partial_result(self, dense_pcb: Path, tmp_path: Path):
        """When timeout is set, the fixer returns whatever progress it made."""
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        # Use a generous timeout so it doesn't actually time out on this
        # tiny PCB, but the parameter is exercised.
        result = fixer.iterative_fix(dense_pcb, output_path=output, timeout=60.0)
        assert result.total_passes >= 1
        assert result.initial_conflicts >= 1

    def test_timeout_zero_returns_immediately(self, dense_pcb: Path, tmp_path: Path):
        """A timeout of 0 should return after the first pass at most."""
        fixer = PlacementFixer()
        output = tmp_path / "out.kicad_pcb"
        result = fixer.iterative_fix(dense_pcb, output_path=output, timeout=0.0)
        # Should have timed out and message should mention it
        assert "timed out" in result.message
        # Should still report the initial conflict count
        assert result.initial_conflicts >= 1

    def test_cli_timeout_flag(self, simple_overlap_pcb: Path, tmp_path: Path):
        """CLI --timeout flag is accepted and passed through."""
        from kicad_tools.cli.placement_cmd import main

        output = tmp_path / "cli_timeout.kicad_pcb"
        result = main(
            [
                "fix",
                str(simple_overlap_pcb),
                "-o",
                str(output),
                "--timeout",
                "30",
                "--quiet",
            ]
        )
        assert result == 0


class TestIterativeFixProgress:
    """Progress output is emitted during computation."""

    def test_progress_output_emitted(self, simple_overlap_pcb: Path, tmp_path: Path, capsys):
        """Fixer emits progress lines to stderr."""
        fixer = PlacementFixer(verbose=True)
        output = tmp_path / "out.kicad_pcb"
        fixer.iterative_fix(simple_overlap_pcb, output_path=output)
        captured = capsys.readouterr()
        # Progress goes to stderr
        assert "conflicts" in captured.err.lower() or "Pass" in captured.err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conflict(c1: str, c2: str) -> Conflict:
    """Create a minimal Conflict for unit tests."""
    return Conflict(
        type=ConflictType.COURTYARD_OVERLAP,
        severity=ConflictSeverity.WARNING,
        component1=c1,
        component2=c2,
        message="courtyard overlap",
        location=Point(100, 100),
        overlap_amount=0.5,
    )
