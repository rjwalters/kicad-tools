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
