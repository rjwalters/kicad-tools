"""Tests for the fix-vias command."""

from pathlib import Path

import pytest

from kicad_tools.cli.fix_vias_cmd import (
    _closest_point_on_segment,
    find_all_vias,
    find_nearby_items,
    fix_vias,
    get_design_rules,
    main,
)
from kicad_tools.sexp.parser import parse_file

# PCB with undersized vias (0.2mm drill, 0.45mm diameter - below JLCPCB minimums)
PCB_WITH_UNDERSIZED_VIAS = """(kicad_pcb
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
  (via (at 110 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 120 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-2"))
  (via (at 130 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-3"))
  (segment (start 110 110) (end 120 110) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
)
"""


@pytest.fixture
def pcb_with_undersized_vias(tmp_path: Path) -> Path:
    """Create a PCB with undersized vias for testing."""
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(PCB_WITH_UNDERSIZED_VIAS)
    return pcb_file


class TestGetDesignRules:
    """Tests for get_design_rules function."""

    def test_explicit_values(self):
        """Explicit values override manufacturer rules."""
        drill, diameter, annular, clearance = get_design_rules(None, 2, 1.0, 0.4, 0.8)
        assert drill == 0.4
        assert diameter == 0.8
        assert annular == 0.0  # No annular ring check for explicit values
        assert clearance == 0.2  # Default when no manufacturer

    def test_partial_override(self):
        """Can override just drill or just diameter."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 2, 1.0, 0.4, None)
        assert drill == 0.4
        # With 0.4mm drill and 0.15mm annular ring: 0.4 + 2*0.15 = 0.7mm
        assert diameter == 0.7
        assert annular == 0.15
        assert clearance == 0.127  # JLCPCB 2-layer min_clearance_mm

    def test_jlcpcb_defaults(self):
        """JLCPCB 2-layer rules are loaded correctly."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 2, 1.0, None, None)
        assert drill == 0.3
        assert diameter == 0.6  # 0.3 + 2*0.15 = 0.6, same as min_via_diameter
        assert annular == 0.15
        assert clearance == 0.127  # JLCPCB 2-layer min_clearance_mm

    def test_jlcpcb_4layer_annular_ring_crosscheck(self):
        """JLCPCB 4-layer: annular ring requires larger diameter than min_via_diameter."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 4, 1.0, None, None)
        assert drill == 0.2
        # min_via_diameter is 0.45, but annular ring requires 0.2 + 2*0.15 = 0.50
        assert diameter == 0.5
        assert annular == 0.15
        assert clearance == 0.1016  # JLCPCB 4-layer min_clearance_mm

    def test_annular_ring_returns_zero_for_no_mfr(self):
        """No manufacturer specified returns zero annular ring."""
        drill, diameter, annular, clearance = get_design_rules(None, 2, 1.0, None, None)
        assert annular == 0.0
        assert clearance == 0.2  # Default fallback

    def test_jlcpcb_2layer_2oz_clearance(self):
        """JLCPCB 2-layer 2oz uses 8mil (0.2032mm) clearance."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 2, 2.0, None, None)
        assert clearance == 0.2032


class TestFindAllVias:
    """Tests for find_all_vias function."""

    def test_finds_all_vias(self, pcb_with_undersized_vias: Path):
        """Should find all vias in the PCB."""
        doc = parse_file(pcb_with_undersized_vias)
        vias = find_all_vias(doc)

        assert len(vias) == 3

        # Check first via properties
        node, x, y, drill, diameter, net, uuid = vias[0]
        assert x == 110
        assert y == 110
        assert drill == 0.2
        assert diameter == 0.45
        assert net == 1
        assert uuid == "via-1"


class TestFixVias:
    """Tests for fix_vias function."""

    def test_fixes_undersized_vias(self, pcb_with_undersized_vias: Path):
        """Should fix only undersized vias."""
        doc = parse_file(pcb_with_undersized_vias)

        fixes, warnings = fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=True)

        # Should fix 2 vias (via-1 and via-2), not via-3 which is already compliant
        assert len(fixes) == 2

        # Check fix details
        assert fixes[0].old_drill == 0.2
        assert fixes[0].new_drill == 0.3
        assert fixes[0].old_diameter == 0.45
        assert fixes[0].new_diameter == 0.6

    def test_dry_run_does_not_modify(self, pcb_with_undersized_vias: Path):
        """Dry run should not modify the document."""
        doc = parse_file(pcb_with_undersized_vias)

        # Get original values
        vias_before = find_all_vias(doc)
        _, _, _, drill_before, diameter_before, _, _ = vias_before[0]

        # Run fix with dry_run=True
        fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=True)

        # Values should be unchanged
        vias_after = find_all_vias(doc)
        _, _, _, drill_after, diameter_after, _, _ = vias_after[0]

        assert drill_before == drill_after
        assert diameter_before == diameter_after

    def test_applies_fixes_when_not_dry_run(self, pcb_with_undersized_vias: Path):
        """Should modify document when not dry run."""
        doc = parse_file(pcb_with_undersized_vias)

        fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=False)

        # Values should be updated
        vias = find_all_vias(doc)
        _, _, _, drill, diameter, _, _ = vias[0]

        assert drill == 0.3
        assert diameter == 0.6

    def test_no_fixes_needed(self, tmp_path: Path):
        """Should return empty list when no fixes needed."""
        # Create PCB with compliant vias
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "compliant.kicad_pcb"
        pcb_file.write_text(pcb_content)

        doc = parse_file(pcb_file)
        fixes, warnings = fix_vias(doc, target_drill=0.3, target_diameter=0.6)

        assert len(fixes) == 0

    def test_annular_ring_violation_detected(self, tmp_path: Path):
        """Vias meeting min diameter but violating annular ring should be fixed.

        This is the exact scenario from issue #1107:
        - Via: 0.45mm diameter, 0.20mm drill
        - Annular ring: (0.45 - 0.20) / 2 = 0.125mm
        - Required: 0.15mm
        - Required diameter: 0.20 + 2*0.15 = 0.50mm
        """
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "annular_ring.kicad_pcb"
        pcb_file.write_text(pcb_content)

        doc = parse_file(pcb_file)
        # target_diameter=0.45 (JLCPCB 4-layer min), but with annular ring check
        fixes, warnings = fix_vias(
            doc,
            target_drill=0.2,
            target_diameter=0.45,
            dry_run=True,
            min_annular_ring=0.15,
        )

        # Should detect the annular ring violation
        assert len(fixes) == 1
        assert fixes[0].old_diameter == 0.45
        assert fixes[0].new_diameter == 0.5  # 0.2 + 2*0.15

    def test_annular_ring_no_false_positive(self, tmp_path: Path):
        """Vias with sufficient annular ring should not be flagged."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.5) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "good_annular.kicad_pcb"
        pcb_file.write_text(pcb_content)

        doc = parse_file(pcb_file)
        fixes, warnings = fix_vias(
            doc,
            target_drill=0.2,
            target_diameter=0.45,
            dry_run=True,
            min_annular_ring=0.15,
        )

        assert len(fixes) == 0

    def test_annular_ring_with_drill_resize(self, tmp_path: Path):
        """When drill is resized, annular ring should use the new drill size."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.45) (drill 0.15) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "drill_resize.kicad_pcb"
        pcb_file.write_text(pcb_content)

        doc = parse_file(pcb_file)
        # Drill will be resized from 0.15 to 0.2, so annular ring needs
        # 0.2 + 2*0.15 = 0.50mm diameter
        fixes, warnings = fix_vias(
            doc,
            target_drill=0.2,
            target_diameter=0.45,
            dry_run=True,
            min_annular_ring=0.15,
        )

        assert len(fixes) == 1
        assert fixes[0].new_drill == 0.2
        assert fixes[0].new_diameter == 0.5  # Based on new drill, not original


class TestCLI:
    """Tests for the CLI interface."""

    def test_dry_run(self, pcb_with_undersized_vias: Path, capsys):
        """Dry run should show changes but not modify file."""
        original_content = pcb_with_undersized_vias.read_text()

        result = main([str(pcb_with_undersized_vias), "--dry-run"])

        # Should succeed
        assert result == 0

        # File should be unchanged
        assert pcb_with_undersized_vias.read_text() == original_content

        # Should show output
        captured = capsys.readouterr()
        assert "via" in captured.out.lower()

    def test_output_to_different_file(self, pcb_with_undersized_vias: Path, tmp_path: Path):
        """Should write to output file when specified."""
        original_content = pcb_with_undersized_vias.read_text()
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main([str(pcb_with_undersized_vias), "-o", str(output_file)])

        assert result == 0

        # Original should be unchanged
        assert pcb_with_undersized_vias.read_text() == original_content

        # Output file should exist and contain fixes
        assert output_file.exists()
        output_doc = parse_file(output_file)
        vias = find_all_vias(output_doc)
        _, _, _, drill, diameter, _, _ = vias[0]
        assert drill == 0.3
        assert diameter == 0.6

    def test_json_output(self, pcb_with_undersized_vias: Path, capsys):
        """JSON output should be valid JSON."""
        import json

        result = main([str(pcb_with_undersized_vias), "--dry-run", "--format", "json"])

        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "fixes" in data
        assert len(data["fixes"]) == 2

    def test_summary_output(self, pcb_with_undersized_vias: Path, capsys):
        """Summary output should show counts."""
        result = main([str(pcb_with_undersized_vias), "--dry-run", "--format", "summary"])

        assert result == 0

        captured = capsys.readouterr()
        assert "2" in captured.out  # 2 vias fixed
        assert "via" in captured.out.lower()

    def test_explicit_sizes(self, pcb_with_undersized_vias: Path, tmp_path: Path):
        """Should use explicit sizes when specified."""
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main(
            [
                str(pcb_with_undersized_vias),
                "--drill",
                "0.35",
                "--diameter",
                "0.7",
                "-o",
                str(output_file),
            ]
        )

        assert result == 0

        output_doc = parse_file(output_file)
        vias = find_all_vias(output_doc)
        _, _, _, drill, diameter, _, _ = vias[0]

        # Should use specified values
        assert drill == 0.35
        assert diameter == 0.7

    def test_invalid_file(self, tmp_path: Path):
        """Should fail gracefully for non-existent file."""
        result = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert result == 1

    def test_quiet_mode(self, pcb_with_undersized_vias: Path, tmp_path: Path, capsys):
        """Quiet mode should suppress output."""
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main([str(pcb_with_undersized_vias), "-o", str(output_file), "--quiet"])

        assert result == 0

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_annular_ring_violation_4layer_jlcpcb(self, tmp_path: Path, capsys):
        """Reproduce issue #1107: 4-layer JLCPCB vias with insufficient annular ring.

        Via: 0.45mm diameter, 0.20mm drill
        Annular ring: (0.45 - 0.20) / 2 = 0.125mm < 0.15mm required
        Expected: resize to 0.50mm diameter
        Before fix: "No vias needed resizing"
        """
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (1 "In1.Cu" signal)
            (2 "In2.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 133.75 95.0) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "4layer.kicad_pcb"
        pcb_file.write_text(pcb_content)

        output_file = tmp_path / "fixed.kicad_pcb"
        result = main(
            [
                str(pcb_file),
                "--mfr",
                "jlcpcb",
                "--layers",
                "4",
                "-o",
                str(output_file),
            ]
        )

        assert result == 0

        # Verify the via was resized
        output_doc = parse_file(output_file)
        vias = find_all_vias(output_doc)
        _, _, _, drill, diameter, _, _ = vias[0]

        assert drill == 0.2  # Drill meets min (0.2mm for 4-layer)
        assert diameter == 0.5  # 0.2 + 2*0.15 = 0.50mm

        # Verify output doesn't say "No vias needed resizing"
        captured = capsys.readouterr()
        assert "No vias needed resizing" not in captured.out


class TestClosestPointOnSegment:
    """Tests for the _closest_point_on_segment helper."""

    def test_point_projects_onto_segment(self):
        """Point perpendicular to segment midpoint should return midpoint."""
        cx, cy, dist = _closest_point_on_segment(0, 0, 10, 0, 5, 3)
        assert abs(cx - 5.0) < 1e-6
        assert abs(cy - 0.0) < 1e-6
        assert abs(dist - 3.0) < 1e-6

    def test_point_closest_to_start(self):
        """Point beyond segment start should clamp to start."""
        cx, cy, dist = _closest_point_on_segment(0, 0, 10, 0, -5, 0)
        assert abs(cx - 0.0) < 1e-6
        assert abs(cy - 0.0) < 1e-6
        assert abs(dist - 5.0) < 1e-6

    def test_point_closest_to_end(self):
        """Point beyond segment end should clamp to end."""
        cx, cy, dist = _closest_point_on_segment(0, 0, 10, 0, 15, 0)
        assert abs(cx - 10.0) < 1e-6
        assert abs(cy - 0.0) < 1e-6
        assert abs(dist - 5.0) < 1e-6

    def test_degenerate_segment(self):
        """Zero-length segment should return the segment point."""
        cx, cy, dist = _closest_point_on_segment(5, 5, 5, 5, 8, 9)
        assert abs(cx - 5.0) < 1e-6
        assert abs(cy - 5.0) < 1e-6
        assert abs(dist - 5.0) < 1e-6

    def test_diagonal_segment(self):
        """Point near a diagonal segment."""
        # Segment from (0,0) to (10,10), point at (0,10)
        # Closest point should be (5,5)
        cx, cy, dist = _closest_point_on_segment(0, 0, 10, 10, 0, 10)
        assert abs(cx - 5.0) < 1e-6
        assert abs(cy - 5.0) < 1e-6


class TestFindNearbyItemsGeometry:
    """Tests for the closest-point-on-segment fix in find_nearby_items."""

    def test_segment_near_via_detected_with_closest_point(self, tmp_path: Path):
        """A segment passing close to a via should be detected even when its
        midpoint is far away.

        This is the core bug: the old midpoint-based check would miss segments
        whose midpoint is outside the search radius but whose closest point is
        inside it.
        """
        # Segment from (100, 100) to (100, 130): midpoint at (100, 115)
        # Via at (100.5, 110): closest point on segment is (100, 110), dist ~ 0.5
        # Midpoint distance: sqrt((100-100.5)^2 + (115-110)^2) ~ 5.02
        # With a radius of 2.0, midpoint method misses it but closest-point finds it
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (segment (start 100 100) (end 100 130) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "closest_point.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        items = find_nearby_items(doc, 100.5, 110, 2.0)
        assert len(items) == 1
        item_type, ix, iy, width = items[0]
        assert item_type == "track"
        assert abs(ix - 100.0) < 1e-6  # Closest point x
        assert abs(iy - 110.0) < 1e-6  # Closest point y
        assert abs(width - 0.25) < 1e-6  # Track width returned

    def test_segment_midpoint_far_but_endpoint_close(self, tmp_path: Path):
        """Segment with midpoint far from via but one endpoint close."""
        # Segment from (100, 100) to (200, 100): midpoint at (150, 100)
        # Via at (102, 101): closest point is (102, 100), dist ~ 1.0
        # Midpoint distance: sqrt((150-102)^2 + (100-101)^2) ~ 48.0
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (segment (start 100 100) (end 200 100) (width 0.15) (layer "F.Cu") (net 1) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "endpoint_close.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        items = find_nearby_items(doc, 102, 101, 2.0)
        assert len(items) == 1
        assert items[0][0] == "track"

    def test_nearby_items_returns_track_width(self, tmp_path: Path):
        """find_nearby_items should return the trace width for tracks."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (segment (start 100 100) (end 110 100) (width 0.3) (layer "F.Cu") (net 1) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "track_width.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        items = find_nearby_items(doc, 105, 100.5, 2.0)
        assert len(items) == 1
        _, _, _, width = items[0]
        assert abs(width - 0.3) < 1e-6

    def test_nearby_items_returns_via_diameter(self, tmp_path: Path):
        """find_nearby_items should return the via diameter for nearby vias."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (via (at 101.5 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-2"))
        )
        """
        pcb_file = tmp_path / "via_diameter.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        # Search near via-1, should find via-2
        items = find_nearby_items(doc, 100, 100, 3.0)
        assert len(items) == 1
        item_type, _, _, via_diam = items[0]
        assert item_type == "via"
        assert abs(via_diam - 0.8) < 1e-6


class TestClearanceWithTraceWidth:
    """Tests for clearance gap accounting for trace width."""

    def test_wide_trace_triggers_warning(self, tmp_path: Path):
        """A wide trace near a resized via should trigger a clearance warning
        because the gap subtracts trace_width/2."""
        # Via at (100, 100) will be resized to 0.6mm diameter (radius 0.3)
        # Trace at y=100.5, width=0.25 => trace edge at 100.5 - 0.125 = 100.375
        # Via edge at 100.3 => gap = 100.375 - 100.3 = 0.075mm < 0.127mm
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (segment (start 95 100.5) (end 105 100.5) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "wide_trace.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_vias(
            doc, target_drill=0.3, target_diameter=0.6, min_clearance=0.127, dry_run=True
        )

        assert len(fixes) == 1
        assert len(warnings) == 1
        # Clearance should account for trace width
        # dist from (100,100) to closest point on segment = 0.5
        # clearance = 0.5 - 0.6/2 - 0.25/2 = 0.5 - 0.3 - 0.125 = 0.075
        assert warnings[0].clearance_mm == pytest.approx(0.075, abs=0.01)

    def test_narrow_trace_no_warning(self, tmp_path: Path):
        """A narrow trace far enough from the via should not trigger a warning."""
        # Via at (100, 100) resized to 0.6mm (radius 0.3)
        # Trace at y=101, width=0.1 => trace edge at 101 - 0.05 = 100.95
        # Via edge at 100.3 => gap = 100.95 - 100.3 = 0.65 > 0.127
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (segment (start 95 101) (end 105 101) (width 0.1) (layer "F.Cu") (net 2) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "narrow_trace.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_vias(
            doc, target_drill=0.3, target_diameter=0.6, min_clearance=0.127, dry_run=True
        )

        assert len(fixes) == 1
        assert len(warnings) == 0


class TestManufacturerClearance:
    """Tests that min_clearance is sourced from manufacturer design rules."""

    def test_jlcpcb_clearance_used_in_warnings(self, tmp_path: Path):
        """When --mfr jlcpcb is supplied, min_clearance should be 0.127mm (5 mil)
        not the old hardcoded 0.2mm."""
        # Via at (100, 100) resized to 0.6mm (radius 0.3)
        # Trace at y=100.55, width=0.25 => trace edge at 0.55 - 0.125 = 0.425
        # Via edge at 0.3 => gap = 0.425 - 0.3 = 0.125
        # With old hardcoded 0.2: 0.125 < 0.2 => warning
        # With JLCPCB 0.127: 0.125 < 0.127 => warning (barely)
        # But at y=100.6: gap = 0.6 - 0.3 - 0.125 = 0.175
        # With old hardcoded 0.2: 0.175 < 0.2 => warning (false positive!)
        # With JLCPCB 0.127: 0.175 > 0.127 => no warning (correct)
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (segment (start 95 100.6) (end 105 100.6) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "mfr_clearance.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        # With JLCPCB clearance (0.127mm), the trace at 0.6mm distance should NOT warn
        # gap = 0.6 - 0.3 - 0.125 = 0.175 > 0.127 => no warning
        fixes, warnings = fix_vias(
            doc, target_drill=0.3, target_diameter=0.6, min_clearance=0.127, dry_run=True
        )
        assert len(fixes) == 1
        assert len(warnings) == 0

        # With old hardcoded clearance (0.2mm), the same trace would warn
        # gap = 0.175 < 0.2 => warning
        doc2 = parse_file(pcb_file)
        fixes2, warnings2 = fix_vias(
            doc2, target_drill=0.3, target_diameter=0.6, min_clearance=0.2, dry_run=True
        )
        assert len(fixes2) == 1
        assert len(warnings2) == 1

    def test_cli_passes_mfr_clearance(self, tmp_path: Path, capsys):
        """CLI with --mfr jlcpcb should pass manufacturer clearance, not 0.2mm."""
        # This trace is positioned so it triggers a warning with 0.2mm clearance
        # but NOT with JLCPCB's 0.127mm clearance
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (segment (start 95 100.6) (end 105 100.6) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "cli_clearance.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main(
            [
                str(pcb_file),
                "--mfr",
                "jlcpcb",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        import json

        data = json.loads(captured.out)
        # With JLCPCB 0.127mm clearance, no warnings expected
        assert len(data["warnings"]) == 0
        assert result == 0


class TestExitCodeWarnings:
    """Tests for exit code semantics: 0=success, 1=error, 2=success-with-warnings."""

    def test_exit_code_0_no_fixes_needed(self, tmp_path: Path):
        """Exit code 0 when all vias are already compliant (no fixes, no warnings)."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "compliant.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main([str(pcb_file), "--mfr", "jlcpcb", "--dry-run"])
        assert result == 0

    def test_exit_code_0_fixes_no_warnings(self, tmp_path: Path):
        """Exit code 0 when vias are resized but no clearance warnings are detected."""
        # Via far from any other items -- no clearance issue after resize
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "isolated.kicad_pcb"
        pcb_file.write_text(pcb_content)
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main([str(pcb_file), "--mfr", "jlcpcb", "-o", str(output_file)])
        assert result == 0

    def test_exit_code_2_fixes_with_warnings(self, tmp_path: Path):
        """Exit code 2 when vias are resized and clearance warnings are detected."""
        # Via very close to a trace on a different net -- clearance warning expected
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (segment (start 95 100.5) (end 105 100.5) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "crowded.kicad_pcb"
        pcb_file.write_text(pcb_content)
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main([str(pcb_file), "--mfr", "jlcpcb", "-o", str(output_file)])
        assert result == 2

    def test_exit_code_1_file_not_found(self, tmp_path: Path):
        """Exit code 1 for actual errors (file not found)."""
        result = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert result == 1

    def test_exit_code_1_bad_extension(self, tmp_path: Path):
        """Exit code 1 for unsupported file extension."""
        bad_file = tmp_path / "board.txt"
        bad_file.write_text("not a pcb")
        result = main([str(bad_file)])
        assert result == 1

    def test_dry_run_exit_code_0_no_warnings(self, tmp_path: Path):
        """Dry-run exit code is 0 when there are fixes but no clearance warnings."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "isolated_dry.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main([str(pcb_file), "--mfr", "jlcpcb", "--dry-run"])
        assert result == 0

    def test_dry_run_exit_code_2_with_warnings(self, tmp_path: Path):
        """Dry-run still returns exit code 2 when clearance warnings exist."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (segment (start 95 100.5) (end 105 100.5) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "crowded_dry.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main([str(pcb_file), "--mfr", "jlcpcb", "--dry-run"])
        assert result == 2
