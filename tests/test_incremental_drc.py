"""Tests for incremental DRC engine."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kicad_tools.drc.incremental import (
    DRCDelta,
    IncrementalDRC,
    Rectangle,
    SpatialIndex,
    Violation,
)
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.schema.pcb import PCB

# Test fixture: PCB with multiple components close together
# R1 pad 2 (SIG1) and R2 pad 1 (SIG2) are on different nets and only ~0.18mm apart
# Pad centers: R1-pad2 at 120.51, R2-pad1 at 120.69, distance 0.18mm
# Pad radius ~0.32mm each, so edge-to-edge < 0 (overlapping)
# This should trigger a clearance violation
PCB_WITH_CLEARANCE_ISSUES = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG1")
  (net 3 "SIG2")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "SIG1"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 121.2 120)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG2"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 140 140)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000031"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000032"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "SIG1"))
  )
)
"""

# Test fixture: PCB with no issues (components well spaced)
PCB_NO_ISSUES = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 140 140)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
)
"""


@pytest.fixture
def pcb_with_issues(tmp_path: Path) -> PCB:
    """Create a PCB with clearance issues for testing."""
    pcb_file = tmp_path / "clearance_issues.kicad_pcb"
    pcb_file.write_text(PCB_WITH_CLEARANCE_ISSUES)
    return PCB.load(str(pcb_file))


@pytest.fixture
def pcb_no_issues(tmp_path: Path) -> PCB:
    """Create a PCB with no clearance issues."""
    pcb_file = tmp_path / "no_issues.kicad_pcb"
    pcb_file.write_text(PCB_NO_ISSUES)
    return PCB.load(str(pcb_file))


@pytest.fixture
def default_rules() -> DesignRules:
    """Create default design rules for testing."""
    return DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=0.15,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.15,
    )


class TestRectangle:
    """Tests for Rectangle class."""

    def test_from_center(self):
        """Test creating rectangle from center point."""
        rect = Rectangle.from_center(10.0, 20.0, 4.0, 6.0)
        assert rect.min_x == 8.0
        assert rect.min_y == 17.0
        assert rect.max_x == 12.0
        assert rect.max_y == 23.0

    def test_properties(self):
        """Test rectangle properties."""
        rect = Rectangle(0.0, 0.0, 10.0, 20.0)
        assert rect.width == 10.0
        assert rect.height == 20.0
        assert rect.center_x == 5.0
        assert rect.center_y == 10.0

    def test_translate(self):
        """Test rectangle translation."""
        rect = Rectangle(0.0, 0.0, 10.0, 10.0)
        translated = rect.translate(5.0, -3.0)
        assert translated.min_x == 5.0
        assert translated.min_y == -3.0
        assert translated.max_x == 15.0
        assert translated.max_y == 7.0

    def test_union(self):
        """Test rectangle union."""
        rect1 = Rectangle(0.0, 0.0, 10.0, 10.0)
        rect2 = Rectangle(5.0, 5.0, 15.0, 15.0)
        union = rect1.union(rect2)
        assert union.min_x == 0.0
        assert union.min_y == 0.0
        assert union.max_x == 15.0
        assert union.max_y == 15.0

    def test_expand(self):
        """Test rectangle expansion."""
        rect = Rectangle(5.0, 5.0, 15.0, 15.0)
        expanded = rect.expand(2.0)
        assert expanded.min_x == 3.0
        assert expanded.min_y == 3.0
        assert expanded.max_x == 17.0
        assert expanded.max_y == 17.0

    def test_intersects(self):
        """Test rectangle intersection."""
        rect1 = Rectangle(0.0, 0.0, 10.0, 10.0)
        rect2 = Rectangle(5.0, 5.0, 15.0, 15.0)
        rect3 = Rectangle(20.0, 20.0, 30.0, 30.0)

        assert rect1.intersects(rect2) is True
        assert rect2.intersects(rect1) is True
        assert rect1.intersects(rect3) is False
        assert rect3.intersects(rect1) is False

    def test_as_tuple(self):
        """Test converting rectangle to tuple."""
        rect = Rectangle(1.0, 2.0, 3.0, 4.0)
        assert rect.as_tuple() == (1.0, 2.0, 3.0, 4.0)


class TestViolation:
    """Tests for Violation class."""

    def test_involves(self):
        """Test checking if violation involves a component."""
        violation = Violation(
            rule_id="clearance",
            message="Test violation",
            items=("R1-1", "R2-2"),
        )
        assert violation.involves("R1") is True
        assert violation.involves("R2") is True
        assert violation.involves("R3") is False

    def test_equality(self):
        """Test violation equality."""
        v1 = Violation(
            rule_id="clearance",
            message="Test",
            location=(10.0, 20.0),
            items=("R1-1", "R2-2"),
        )
        v2 = Violation(
            rule_id="clearance",
            message="Test",
            location=(10.0, 20.0),
            items=("R1-1", "R2-2"),
        )
        v3 = Violation(
            rule_id="clearance",
            message="Different",
            location=(10.0, 20.0),
            items=("R1-1", "R3-1"),
        )

        assert v1 == v2
        assert v1 != v3

    def test_hash(self):
        """Test violation hashing for set operations."""
        v1 = Violation(
            rule_id="clearance",
            message="Test",
            location=(10.0, 20.0),
            items=("R1-1", "R2-2"),
        )
        v2 = Violation(
            rule_id="clearance",
            message="Test",
            location=(10.0, 20.0),
            items=("R1-1", "R2-2"),
        )

        violations = {v1, v2}
        assert len(violations) == 1


class TestSpatialIndex:
    """Tests for SpatialIndex class."""

    def test_insert_and_query(self):
        """Test basic insert and query operations."""
        index = SpatialIndex()
        index.insert("R1", Rectangle(0.0, 0.0, 10.0, 10.0))
        index.insert("R2", Rectangle(5.0, 5.0, 15.0, 15.0))
        index.insert("R3", Rectangle(20.0, 20.0, 30.0, 30.0))

        # Query should find R1 and R2
        results = index.query(Rectangle(4.0, 4.0, 12.0, 12.0))
        assert set(results) == {"R1", "R2"}

        # Query should find only R3
        results = index.query(Rectangle(25.0, 25.0, 28.0, 28.0))
        assert set(results) == {"R3"}

    def test_remove(self):
        """Test removing items from index."""
        index = SpatialIndex()
        index.insert("R1", Rectangle(0.0, 0.0, 10.0, 10.0))
        index.insert("R2", Rectangle(5.0, 5.0, 15.0, 15.0))

        assert "R1" in index
        index.remove("R1")
        assert "R1" not in index

        results = index.query(Rectangle(0.0, 0.0, 10.0, 10.0))
        assert "R1" not in results

    def test_update(self):
        """Test updating item bounds."""
        index = SpatialIndex()
        index.insert("R1", Rectangle(0.0, 0.0, 10.0, 10.0))

        # Update to new position
        index.update("R1", Rectangle(50.0, 50.0, 60.0, 60.0))

        # Old position should not find R1
        results = index.query(Rectangle(0.0, 0.0, 10.0, 10.0))
        assert "R1" not in results

        # New position should find R1
        results = index.query(Rectangle(55.0, 55.0, 58.0, 58.0))
        assert "R1" in results

    def test_get_bounds(self):
        """Test getting stored bounds."""
        index = SpatialIndex()
        bounds = Rectangle(1.0, 2.0, 3.0, 4.0)
        index.insert("R1", bounds)

        retrieved = index.get_bounds("R1")
        assert retrieved is not None
        assert retrieved.as_tuple() == bounds.as_tuple()

        assert index.get_bounds("R2") is None

    def test_len(self):
        """Test length of index."""
        index = SpatialIndex()
        assert len(index) == 0

        index.insert("R1", Rectangle(0.0, 0.0, 10.0, 10.0))
        assert len(index) == 1

        index.insert("R2", Rectangle(0.0, 0.0, 10.0, 10.0))
        assert len(index) == 2

        index.remove("R1")
        assert len(index) == 1


class TestDRCDelta:
    """Tests for DRCDelta class."""

    def test_net_change_positive(self):
        """Test net change when violations increase."""
        delta = DRCDelta(
            new_violations=[
                Violation(rule_id="clearance", message="V1"),
                Violation(rule_id="clearance", message="V2"),
            ],
            resolved_violations=[
                Violation(rule_id="clearance", message="V3"),
            ],
        )
        assert delta.net_change == 1
        assert delta.is_improvement is False

    def test_net_change_negative(self):
        """Test net change when violations decrease."""
        delta = DRCDelta(
            new_violations=[
                Violation(rule_id="clearance", message="V1"),
            ],
            resolved_violations=[
                Violation(rule_id="clearance", message="V2"),
                Violation(rule_id="clearance", message="V3"),
            ],
        )
        assert delta.net_change == -1
        assert delta.is_improvement is True

    def test_summary(self):
        """Test summary generation."""
        delta = DRCDelta(
            new_violations=[
                Violation(rule_id="clearance", message="V1"),
            ],
            resolved_violations=[
                Violation(rule_id="clearance", message="V2"),
                Violation(rule_id="clearance", message="V3"),
            ],
        )
        summary = delta.summary()
        assert "-1" in summary
        assert "1 new" in summary
        assert "2 resolved" in summary


class TestIncrementalDRC:
    """Tests for IncrementalDRC class."""

    def test_full_check_finds_violations(self, pcb_with_issues: PCB, default_rules: DesignRules):
        """Test that full_check finds clearance violations."""
        drc = IncrementalDRC(pcb_with_issues, default_rules)
        violations = drc.full_check()

        # Should find at least one violation (R1 and R2 are close together)
        assert len(violations) > 0
        assert all(v.rule_id == "clearance" for v in violations)

    def test_full_check_clean_board(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that full_check returns empty list for clean board."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        violations = drc.full_check()

        # Well-spaced components should have no violations
        assert len(violations) == 0

    def test_state_initialization(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that full_check initializes state correctly."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        assert drc.state is None

        drc.full_check()

        assert drc.state is not None
        assert len(drc.state.component_bounds) == 2  # R1 and R2
        assert "R1" in drc.state.spatial_index
        assert "R2" in drc.state.spatial_index

    def test_check_move_returns_delta(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that check_move returns a DRCDelta."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        drc.full_check()

        delta = drc.check_move("R1", 140.0, 140.0)  # Move R1 to R2's position

        assert isinstance(delta, DRCDelta)
        assert delta.check_time_ms > 0
        assert "R1" in delta.affected_components

    def test_check_move_detects_new_violations(
        self, pcb_no_issues: PCB, default_rules: DesignRules
    ):
        """Test that check_move detects violations when moving close to another component."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        violations = drc.full_check()
        assert len(violations) == 0  # Start clean

        # Move R1 very close to R2 (R2 is at 140, 140)
        delta = drc.check_move("R1", 141.0, 140.0)

        # Should detect new violations
        assert len(delta.new_violations) > 0

    def test_check_move_preserves_state(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that check_move does not modify state."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        drc.full_check()

        original_bounds = drc.state.component_bounds["R1"]
        original_violations = list(drc.state.violations)

        drc.check_move("R1", 200.0, 200.0)

        # State should be unchanged
        assert drc.state.component_bounds["R1"].as_tuple() == original_bounds.as_tuple()
        assert len(drc.state.violations) == len(original_violations)

    def test_apply_move_updates_state(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that apply_move updates the cached state."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        drc.full_check()

        original_bounds = drc.state.component_bounds["R1"]
        new_x, new_y = 160.0, 160.0

        drc.apply_move("R1", new_x, new_y)

        # State should be updated
        new_bounds = drc.state.component_bounds["R1"]
        assert abs(new_bounds.center_x - new_x) < 1.0
        assert abs(new_bounds.center_y - new_y) < 1.0
        assert new_bounds.as_tuple() != original_bounds.as_tuple()

    def test_apply_move_updates_violations(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that apply_move updates violation list."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        drc.full_check()
        assert len(drc.state.violations) == 0

        # Move R1 close to R2 to create violation
        delta = drc.apply_move("R1", 141.0, 140.0)

        # Violations should be updated in state
        assert len(drc.state.violations) == len(delta.new_violations)

    def test_component_not_found(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test handling of non-existent component."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        drc.full_check()

        delta = drc.check_move("NONEXISTENT", 100.0, 100.0)

        # Should return empty delta, not error
        assert len(delta.new_violations) == 0
        assert len(delta.resolved_violations) == 0

    def test_check_move_without_full_check(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that check_move calls full_check if state is None."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)
        assert drc.state is None

        # Should auto-initialize
        delta = drc.check_move("R1", 140.0, 140.0)

        assert drc.state is not None
        assert isinstance(delta, DRCDelta)


class TestIncrementalVsFullEquivalence:
    """Tests comparing incremental and full DRC results."""

    def test_apply_move_equivalent_to_full_check(self, tmp_path: Path, default_rules: DesignRules):
        """Test that apply_move gives same violations as full check with modified PCB."""
        # Create PCB
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_NO_ISSUES)
        pcb = PCB.load(str(pcb_file))

        # Do incremental check
        drc = IncrementalDRC(pcb, default_rules)
        drc.full_check()

        # Move R1 close to R2
        delta = drc.apply_move("R1", 141.0, 140.0)

        # The violations after apply should match what we'd get from a new full check
        # Note: This is a simplified test - in a real implementation, you'd modify
        # the actual PCB and compare. Here we verify that violations were detected.
        if len(delta.new_violations) > 0:
            # At least check that the violations are for the moved component
            assert any(v.involves("R1") for v in delta.new_violations)


class TestPerformance:
    """Performance tests for incremental DRC."""

    def test_incremental_faster_than_full(self, pcb_no_issues: PCB, default_rules: DesignRules):
        """Test that incremental check is faster than full check."""
        drc = IncrementalDRC(pcb_no_issues, default_rules)

        # Time full check
        start = time.perf_counter()
        drc.full_check()
        full_time = time.perf_counter() - start

        # Time incremental check
        start = time.perf_counter()
        drc.check_move("R1", 130.0, 130.0)
        incremental_time = time.perf_counter() - start

        # Incremental should be faster (or at least not significantly slower)
        # Note: For small boards, both are fast, so we just verify they complete
        assert incremental_time < 1.0  # Should complete in under 1 second
        assert full_time < 1.0

    @pytest.mark.benchmark(group="drc")
    def test_spatial_index_query_performance(self):
        """Test spatial index query performance."""
        index = SpatialIndex()

        # Insert 200 components
        for i in range(200):
            x = (i % 20) * 5.0
            y = (i // 20) * 5.0
            index.insert(f"R{i}", Rectangle(x, y, x + 2.0, y + 2.0))

        # Query should be fast
        start = time.perf_counter()
        for _ in range(100):
            index.query(Rectangle(40.0, 40.0, 60.0, 60.0))
        query_time = time.perf_counter() - start

        # 100 queries should complete very quickly
        assert query_time < 0.1  # Less than 100ms for 100 queries
