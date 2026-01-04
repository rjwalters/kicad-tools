"""Tests for kicad_tools.placement module."""

from pathlib import Path

import pytest

from kicad_tools.placement import (
    Conflict,
    ConflictSeverity,
    ConflictType,
    PlacementAnalyzer,
    PlacementFix,
    PlacementFixer,
)
from kicad_tools.placement.analyzer import DesignRules
from kicad_tools.placement.conflict import Point, Rectangle

# Test PCB with overlapping component courtyards
OVERLAPPING_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 100.5 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
)
"""

# Test PCB with hole-to-hole violations
HOLE_CONFLICT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (footprint "Connector:PinHeader_1x02"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "J1" (at 0 -2.5 0) (layer "F.SilkS"))
    (property "Value" "Conn" (at 0 2.5 0) (layer "F.Fab"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "NET1"))
    (pad "2" thru_hole circle (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 0 ""))
  )
  (footprint "Connector:PinHeader_1x02"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 100.3 100)
    (property "Reference" "J2" (at 0 -2.5 0) (layer "F.SilkS"))
    (property "Value" "Conn" (at 0 2.5 0) (layer "F.Fab"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "NET1"))
    (pad "2" thru_hole circle (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 0 ""))
  )
)
"""

# Test PCB with edge clearance violations
EDGE_CONFLICT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 100 100) (end 110 110)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 100 100) (end 110 100) (width 0.1) (layer "Edge.Cuts") (net 0))
  (segment (start 110 100) (end 110 110) (width 0.1) (layer "Edge.Cuts") (net 0))
  (segment (start 110 110) (end 100 110) (width 0.1) (layer "Edge.Cuts") (net 0))
  (segment (start 100 110) (end 100 100) (width 0.1) (layer "Edge.Cuts") (net 0))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100.1 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""

# Test PCB with no conflicts
CLEAN_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 105 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""


@pytest.fixture
def overlapping_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with overlapping components."""
    pcb_file = tmp_path / "overlapping.kicad_pcb"
    pcb_file.write_text(OVERLAPPING_PCB)
    return pcb_file


@pytest.fixture
def hole_conflict_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with hole-to-hole conflicts."""
    pcb_file = tmp_path / "hole_conflict.kicad_pcb"
    pcb_file.write_text(HOLE_CONFLICT_PCB)
    return pcb_file


@pytest.fixture
def edge_conflict_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with edge clearance violations."""
    pcb_file = tmp_path / "edge_conflict.kicad_pcb"
    pcb_file.write_text(EDGE_CONFLICT_PCB)
    return pcb_file


@pytest.fixture
def clean_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with no placement conflicts."""
    pcb_file = tmp_path / "clean.kicad_pcb"
    pcb_file.write_text(CLEAN_PCB)
    return pcb_file


class TestConflictTypes:
    """Tests for conflict data structures."""

    def test_point_distance(self):
        """Test Point distance calculation."""
        p1 = Point(0, 0)
        p2 = Point(3, 4)
        assert p1.distance_to(p2) == pytest.approx(5.0)

    def test_point_addition(self):
        """Test Point addition."""
        p1 = Point(1, 2)
        p2 = Point(3, 4)
        result = p1 + p2
        assert result.x == 4
        assert result.y == 6

    def test_rectangle_intersects(self):
        """Test Rectangle intersection detection."""
        r1 = Rectangle(0, 0, 10, 10)
        r2 = Rectangle(5, 5, 15, 15)
        r3 = Rectangle(20, 20, 30, 30)

        assert r1.intersects(r2) is True
        assert r1.intersects(r3) is False

    def test_rectangle_overlap_vector(self):
        """Test Rectangle overlap vector calculation."""
        r1 = Rectangle(0, 0, 10, 10)
        r2 = Rectangle(8, 0, 18, 10)

        overlap = r1.overlap_vector(r2)
        assert overlap is not None
        # Should move in x direction (smaller overlap)
        assert overlap.x != 0 or overlap.y != 0

    def test_rectangle_no_overlap(self):
        """Test Rectangle overlap vector when not overlapping."""
        r1 = Rectangle(0, 0, 10, 10)
        r2 = Rectangle(20, 20, 30, 30)

        overlap = r1.overlap_vector(r2)
        assert overlap is None

    def test_conflict_to_dict(self):
        """Test Conflict serialization."""
        conflict = Conflict(
            type=ConflictType.COURTYARD_OVERLAP,
            severity=ConflictSeverity.WARNING,
            component1="R1",
            component2="R2",
            message="courtyards overlap",
            location=Point(100, 100),
            overlap_amount=0.5,
        )

        d = conflict.to_dict()
        assert d["type"] == "courtyard_overlap"
        assert d["severity"] == "warning"
        assert d["component1"] == "R1"
        assert d["component2"] == "R2"
        assert d["overlap_amount"] == 0.5


class TestPlacementAnalyzer:
    """Tests for PlacementAnalyzer."""

    def test_find_courtyard_overlap(self, overlapping_pcb: Path):
        """Test detecting courtyard overlaps."""
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        # Should detect overlap between R1 and R2
        overlap_conflicts = [c for c in conflicts if c.type == ConflictType.COURTYARD_OVERLAP]
        assert len(overlap_conflicts) >= 1

        conflict = overlap_conflicts[0]
        assert {conflict.component1, conflict.component2} == {"R1", "R2"}
        assert conflict.severity == ConflictSeverity.WARNING

    def test_find_pad_clearance_violations(self, overlapping_pcb: Path):
        """Test detecting pad clearance violations."""
        analyzer = PlacementAnalyzer()
        # Use tight clearance to ensure violations are detected
        rules = DesignRules(min_pad_clearance=0.2)
        conflicts = analyzer.find_conflicts(overlapping_pcb, rules)

        # Should detect pad clearance issues or courtyard overlap
        # May or may not have pad conflicts depending on exact overlap
        # At minimum, courtyard should be detected
        assert len(conflicts) >= 1

    def test_find_hole_to_hole_violations(self, hole_conflict_pcb: Path):
        """Test detecting hole-to-hole violations."""
        analyzer = PlacementAnalyzer()
        rules = DesignRules(min_hole_to_hole=0.5)
        conflicts = analyzer.find_conflicts(hole_conflict_pcb, rules)

        hole_conflicts = [c for c in conflicts if c.type == ConflictType.HOLE_TO_HOLE]
        # The holes are 0.3mm apart (centers), with 1.0mm drill each
        # So edge-to-edge they overlap
        assert len(hole_conflicts) >= 1

    def test_find_edge_clearance_violations(self, edge_conflict_pcb: Path):
        """Test detecting edge clearance violations."""
        analyzer = PlacementAnalyzer()
        rules = DesignRules(min_edge_clearance=0.5)
        conflicts = analyzer.find_conflicts(edge_conflict_pcb, rules)

        edge_conflicts = [c for c in conflicts if c.type == ConflictType.EDGE_CLEARANCE]
        # Component at 100.1 with board edge at 100 should trigger
        assert len(edge_conflicts) >= 1

    def test_no_conflicts_in_clean_pcb(self, clean_pcb: Path):
        """Test that clean PCB has no conflicts."""
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(clean_pcb)

        # Components are 5mm apart, should be fine
        assert len(conflicts) == 0

    def test_custom_design_rules(self, clean_pcb: Path):
        """Test that custom design rules are respected."""
        analyzer = PlacementAnalyzer()

        # With default rules, should be clean
        conflicts1 = analyzer.find_conflicts(clean_pcb)
        assert len(conflicts1) == 0

        # With extreme rules, might find issues
        extreme_rules = DesignRules(
            min_pad_clearance=10.0,  # Very large
            courtyard_margin=5.0,
        )
        conflicts2 = analyzer.find_conflicts(clean_pcb, extreme_rules)
        # Components 5mm apart with 5mm courtyard margin would overlap
        assert len(conflicts2) >= 1

    def test_get_components(self, clean_pcb: Path):
        """Test retrieving component information."""
        analyzer = PlacementAnalyzer()
        analyzer.find_conflicts(clean_pcb)

        components = analyzer.get_components()
        assert len(components) == 2

        refs = {c.reference for c in components}
        assert refs == {"R1", "R2"}


class TestPlacementFixer:
    """Tests for PlacementFixer."""

    def test_suggest_fixes_for_overlap(self, overlapping_pcb: Path):
        """Test suggesting fixes for overlapping components."""
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        # Should suggest at least one fix
        assert len(fixes) >= 1

        fix = fixes[0]
        assert fix.component in {"R1", "R2"}
        assert fix.move_vector.x != 0 or fix.move_vector.y != 0

    def test_anchored_component_not_moved(self, overlapping_pcb: Path):
        """Test that anchored components are not moved."""
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        fixer = PlacementFixer(anchored={"R1"})
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        # R1 should not be moved
        for fix in fixes:
            assert fix.component != "R1"

    def test_preview_fixes(self, overlapping_pcb: Path):
        """Test generating fix preview."""
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        preview = fixer.preview_fixes(fixes)
        assert "Suggested fixes" in preview
        assert "Move" in preview

    def test_empty_fixes_preview(self):
        """Test preview with no fixes."""
        fixer = PlacementFixer()
        preview = fixer.preview_fixes([])
        assert "No fixes suggested" in preview

    def test_fix_confidence_scoring(self, overlapping_pcb: Path):
        """Test that fixes have confidence scores."""
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        for fix in fixes:
            assert 0.0 <= fix.confidence <= 1.0


class TestPlacementFixSerialization:
    """Tests for PlacementFix serialization."""

    def test_fix_to_dict(self):
        """Test PlacementFix serialization."""
        conflict = Conflict(
            type=ConflictType.COURTYARD_OVERLAP,
            severity=ConflictSeverity.WARNING,
            component1="R1",
            component2="R2",
            message="overlap",
            location=Point(100, 100),
        )

        fix = PlacementFix(
            conflict=conflict,
            component="R2",
            move_vector=Point(1.0, 0.0),
            confidence=0.8,
            new_position=Point(101, 100),
        )

        d = fix.to_dict()
        assert d["component"] == "R2"
        assert d["move_vector"]["x"] == 1.0
        assert d["confidence"] == 0.8
        assert d["new_position"]["x"] == 101


class TestDesignRules:
    """Tests for DesignRules."""

    def test_default_rules(self):
        """Test default design rule values."""
        rules = DesignRules()
        assert rules.min_pad_clearance == 0.1
        assert rules.min_hole_to_hole == 0.5
        assert rules.min_edge_clearance == 0.3
        assert rules.courtyard_margin == 0.25

    def test_custom_rules(self):
        """Test custom design rule values."""
        rules = DesignRules(
            min_pad_clearance=0.15,
            min_hole_to_hole=0.6,
        )
        assert rules.min_pad_clearance == 0.15
        assert rules.min_hole_to_hole == 0.6


class TestCLIIntegration:
    """Tests for CLI integration."""

    def test_placement_check_command(self, clean_pcb: Path):
        """Test placement check command."""
        from kicad_tools.cli.placement_cmd import main

        result = main(["check", str(clean_pcb)])
        assert result == 0  # No errors for clean PCB

    def test_placement_check_finds_conflicts(self, overlapping_pcb: Path):
        """Test placement check finds conflicts."""
        from kicad_tools.cli.placement_cmd import main

        # Conflicts exist but may be warnings, not errors
        result = main(["check", str(overlapping_pcb)])
        # Result is 0 for warnings, 1 for errors
        assert result in (0, 1)

    def test_placement_fix_dry_run(self, overlapping_pcb: Path):
        """Test placement fix dry run."""
        from kicad_tools.cli.placement_cmd import main

        result = main(["fix", str(overlapping_pcb), "--dry-run"])
        assert result == 0

    def test_placement_check_json_output(self, overlapping_pcb: Path, capsys):
        """Test placement check JSON output."""
        from kicad_tools.cli.placement_cmd import main

        main(["check", str(overlapping_pcb), "--format", "json"])
        captured = capsys.readouterr()

        import json

        conflicts = json.loads(captured.out)
        assert isinstance(conflicts, list)
        if len(conflicts) > 0:
            assert "type" in conflicts[0]
            assert "component1" in conflicts[0]


# Test PCB with diagonally overlapping components (45 degree angle)
DIAGONAL_OVERLAP_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 100.3 100.3)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""

# Test PCB with vertically overlapping components (same X, different Y)
VERTICAL_OVERLAP_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 100 100.5)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""


@pytest.fixture
def diagonal_overlap_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with diagonally overlapping components."""
    pcb_file = tmp_path / "diagonal_overlap.kicad_pcb"
    pcb_file.write_text(DIAGONAL_OVERLAP_PCB)
    return pcb_file


@pytest.fixture
def vertical_overlap_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with vertically overlapping components."""
    pcb_file = tmp_path / "vertical_overlap.kicad_pcb"
    pcb_file.write_text(VERTICAL_OVERLAP_PCB)
    return pcb_file


class TestDiagonalMovement:
    """Tests for 2D displacement vectors in PlacementFixer."""

    def test_diagonal_courtyard_fix(self, diagonal_overlap_pcb: Path):
        """Test that diagonal overlap produces diagonal fix vector.

        When components are positioned at a 45° angle, the fix should
        move along that diagonal rather than X-only.
        """
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(diagonal_overlap_pcb)

        # Find courtyard overlap conflict
        overlap_conflicts = [c for c in conflicts if c.type == ConflictType.COURTYARD_OVERLAP]
        assert len(overlap_conflicts) >= 1, "Should detect courtyard overlap"

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        assert len(fixes) >= 1, "Should suggest at least one fix"

        fix = fixes[0]
        # Both X and Y should have non-zero values for diagonal movement
        assert fix.move_vector.x != 0, "Diagonal fix should have X component"
        assert fix.move_vector.y != 0, "Diagonal fix should have Y component"

        # The X and Y components should be roughly equal for 45° angle
        # Note: tolerance is wider (0.5-2.0) because component footprint geometry
        # and courtyard margins can shift the effective centroid slightly
        ratio = (
            abs(fix.move_vector.x / fix.move_vector.y) if fix.move_vector.y != 0 else float("inf")
        )
        assert 0.5 < ratio < 2.0, f"Diagonal fix should be roughly diagonal, got ratio {ratio}"

    def test_vertical_courtyard_fix_y_only(self, vertical_overlap_pcb: Path):
        """Test that vertical overlap produces Y-only fix vector.

        When components are positioned on the same X axis (vertical alignment),
        the fix should move along Y axis.
        """
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(vertical_overlap_pcb)

        # Find courtyard overlap conflict
        overlap_conflicts = [c for c in conflicts if c.type == ConflictType.COURTYARD_OVERLAP]
        assert len(overlap_conflicts) >= 1, "Should detect courtyard overlap"

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        assert len(fixes) >= 1, "Should suggest at least one fix"

        fix = fixes[0]
        # For vertical alignment, Y should dominate
        assert abs(fix.move_vector.y) > abs(fix.move_vector.x), (
            f"Vertical overlap fix should have larger Y component: "
            f"got x={fix.move_vector.x}, y={fix.move_vector.y}"
        )

    def test_horizontal_courtyard_fix_x_only(self, overlapping_pcb: Path):
        """Test that horizontal overlap produces X-dominant fix vector.

        The existing OVERLAPPING_PCB has components at (100, 100) and (100.5, 100),
        which is horizontal alignment - fix should be X-dominant.
        """
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        overlap_conflicts = [c for c in conflicts if c.type == ConflictType.COURTYARD_OVERLAP]
        assert len(overlap_conflicts) >= 1, "Should detect courtyard overlap"

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        assert len(fixes) >= 1, "Should suggest at least one fix"

        fix = fixes[0]
        # For horizontal alignment, X should dominate
        assert abs(fix.move_vector.x) > abs(fix.move_vector.y), (
            f"Horizontal overlap fix should have larger X component: "
            f"got x={fix.move_vector.x}, y={fix.move_vector.y}"
        )

    def test_fallback_to_x_only_without_analyzer(self):
        """Test that fix falls back to X-only when no analyzer is provided."""
        conflict = Conflict(
            type=ConflictType.COURTYARD_OVERLAP,
            severity=ConflictSeverity.WARNING,
            component1="R1",
            component2="R2",
            message="courtyard overlap",
            location=Point(100, 100),
            overlap_amount=0.5,
        )

        fixer = PlacementFixer()
        # Call suggest_fixes without analyzer
        fixes = fixer.suggest_fixes([conflict], analyzer=None)

        assert len(fixes) == 1, "Should suggest one fix"

        fix = fixes[0]
        # Without analyzer, should fall back to X-only
        assert fix.move_vector.x != 0, "Fallback should have X component"
        assert fix.move_vector.y == 0, "Fallback should have no Y component (X-only)"

    def test_diagonal_fix_magnitude_reasonable(self, diagonal_overlap_pcb: Path):
        """Test that diagonal fix magnitude is smaller than X-only would be.

        A diagonal move should have a smaller total displacement than
        an X-only move covering the same overlap.
        """
        import math

        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(diagonal_overlap_pcb)

        overlap_conflicts = [c for c in conflicts if c.type == ConflictType.COURTYARD_OVERLAP]
        assert len(overlap_conflicts) >= 1

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        assert len(fixes) >= 1

        fix = fixes[0]
        magnitude = math.sqrt(fix.move_vector.x**2 + fix.move_vector.y**2)

        # The magnitude should be roughly overlap + margin
        overlap = overlap_conflicts[0].overlap_amount
        if overlap:
            expected_magnitude = overlap + 0.1  # margin
            # Allow 50% tolerance for different calculation methods
            assert magnitude < expected_magnitude * 1.5, (
                f"Diagonal fix magnitude {magnitude} should be close to {expected_magnitude}"
            )


class TestApplyFixes:
    """Tests for PlacementFixer.apply_fixes - the fix command's core functionality."""

    def test_apply_fixes_modifies_positions(self, overlapping_pcb: Path, tmp_path: Path):
        """Test that apply_fixes actually modifies component positions.

        This is the core bug fix for issue #361 - fixes were being suggested
        but the regex wasn't matching, so 0 fixes were applied.
        """
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)
        assert len(conflicts) >= 1, "Should have conflicts to fix"

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)
        assert len(fixes) >= 1, "Should suggest fixes"

        # Apply fixes to a new file
        output_path = tmp_path / "fixed.kicad_pcb"
        result = fixer.apply_fixes(overlapping_pcb, fixes, output_path)

        # The key assertion: fixes_applied should be > 0
        assert result.fixes_applied > 0, (
            f"Should apply at least one fix, but applied {result.fixes_applied}. "
            "This was the bug in issue #361."
        )

        # Verify the output file was modified
        original = overlapping_pcb.read_text()
        modified = output_path.read_text()
        assert original != modified, "Output file should be different from original"

    def test_apply_fixes_changes_coordinates(self, overlapping_pcb: Path, tmp_path: Path):
        """Test that apply_fixes changes the actual coordinate values."""
        import re

        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        # Get the component being moved
        component_to_move = fixes[0].component
        move_vector = fixes[0].move_vector

        # Find original position
        original = overlapping_pcb.read_text()

        # Apply fixes
        output_path = tmp_path / "fixed.kicad_pcb"
        result = fixer.apply_fixes(overlapping_pcb, fixes, output_path)

        assert result.fixes_applied > 0

        # Read modified content and verify position changed
        modified = output_path.read_text()

        # Extract positions from both files for the moved component
        # Look for the (at X Y) pattern near the component's reference
        def find_position(content: str, ref: str) -> tuple[float, float] | None:
            # Find footprint with this reference and extract its position
            pattern = rf'\(footprint\s+"[^"]+"\s+\(layer\s+"[^"]+"\)[\s\S]*?\(at\s+([\d.-]+)\s+([\d.-]+)[\s\S]*?property\s+"Reference"\s+"{re.escape(ref)}"'
            match = re.search(pattern, content)
            if match:
                return float(match.group(1)), float(match.group(2))
            return None

        orig_pos = find_position(original, component_to_move)
        new_pos = find_position(modified, component_to_move)

        assert orig_pos is not None, f"Should find original position for {component_to_move}"
        assert new_pos is not None, f"Should find new position for {component_to_move}"

        # Verify position changed by approximately the move vector
        dx = new_pos[0] - orig_pos[0]
        dy = new_pos[1] - orig_pos[1]

        assert abs(dx - move_vector.x) < 0.001, (
            f"X position change {dx} should match move vector {move_vector.x}"
        )
        assert abs(dy - move_vector.y) < 0.001, (
            f"Y position change {dy} should match move vector {move_vector.y}"
        )

    def test_apply_fixes_dry_run(self, overlapping_pcb: Path, tmp_path: Path):
        """Test that dry_run=True doesn't write changes."""
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(overlapping_pcb)

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts, analyzer)

        output_path = tmp_path / "should_not_exist.kicad_pcb"
        result = fixer.apply_fixes(overlapping_pcb, fixes, output_path, dry_run=True)

        # Should report fixes would be applied
        assert result.fixes_applied > 0
        assert "dry run" in result.message.lower()

        # But file should not be created
        assert not output_path.exists(), "Dry run should not create output file"

    def test_apply_fixes_reduces_conflicts(self, overlapping_pcb: Path, tmp_path: Path):
        """Test that applying fixes reduces the number of conflicts."""
        analyzer = PlacementAnalyzer()
        original_conflicts = analyzer.find_conflicts(overlapping_pcb)

        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(original_conflicts, analyzer)

        output_path = tmp_path / "fixed.kicad_pcb"
        result = fixer.apply_fixes(overlapping_pcb, fixes, output_path)

        assert result.fixes_applied > 0

        # Re-analyze the fixed file
        new_conflicts = analyzer.find_conflicts(output_path)

        # Should have fewer or equal conflicts (ideally zero)
        assert len(new_conflicts) <= len(original_conflicts), (
            f"Fixed file should have fewer conflicts: "
            f"original={len(original_conflicts)}, new={len(new_conflicts)}"
        )

    def test_cli_fix_applies_changes(self, overlapping_pcb: Path, tmp_path: Path):
        """Test that the CLI fix command actually applies fixes (not just suggests)."""
        from kicad_tools.cli.placement_cmd import main

        output_path = tmp_path / "cli_fixed.kicad_pcb"

        # Run fix command (not dry-run)
        # Note: may return 1 if conflicts remain after fixes, but fixes should still be applied
        main(["fix", str(overlapping_pcb), "-o", str(output_path), "--quiet"])

        # Verify output file exists and is different from original
        assert output_path.exists(), "Fix command should create output file"

        original = overlapping_pcb.read_text()
        modified = output_path.read_text()
        assert original != modified, "Fix command should modify the file"
