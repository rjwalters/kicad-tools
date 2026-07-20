"""Tests for the fix-vias command."""

from pathlib import Path

import pytest

from kicad_tools.cli.fix_vias_cmd import (
    _closest_point_on_segment,
    find_all_vias,
    find_nearby_items,
    fix_same_layer_vias,
    fix_vias,
    get_board_outer_layers,
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
        """JLCPCB 4-layer: annular ring (0.10mm) does not enlarge min_via_diameter (0.45mm).

        With advanced PCB capability annular ring of 0.10mm:
        annular_ring_min_diameter = 0.2 + 2*0.10 = 0.40mm < 0.45mm min_via_diameter
        So effective_min_diameter = max(0.45, 0.40) = 0.45mm.
        """
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 4, 1.0, None, None)
        assert drill == 0.2
        # min_via_diameter is 0.45, annular ring requires 0.2 + 2*0.10 = 0.40
        # effective = max(0.45, 0.40) = 0.45
        assert diameter == 0.45
        assert annular == 0.10
        assert clearance == 0.1016  # JLCPCB 4-layer min_clearance_mm

    def test_annular_ring_returns_zero_for_no_mfr(self):
        """No manufacturer specified returns zero annular ring."""
        drill, diameter, annular, clearance = get_design_rules(None, 2, 1.0, None, None)
        assert annular == 0.0
        assert clearance == 0.2  # Default fallback

    def test_jlcpcb_2layer_2oz_clearance(self):
        """JLCPCB 2-layer 2oz uses 6mil (0.1524mm) clearance."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 2, 2.0, None, None)
        assert clearance == 0.1524

    def test_jlcpcb_2layer_annular_ring_unchanged(self):
        """2-layer profiles still use 0.15mm annular ring (unchanged)."""
        _, _, annular_1oz, _ = get_design_rules("jlcpcb", 2, 1.0, None, None)
        _, _, annular_2oz, _ = get_design_rules("jlcpcb", 2, 2.0, None, None)
        assert annular_1oz == 0.15
        assert annular_2oz == 0.15

    def test_jlcpcb_4layer_2oz_annular_ring(self):
        """JLCPCB 4-layer 2oz also uses 0.10mm annular ring."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 4, 2.0, None, None)
        assert drill == 0.2
        assert annular == 0.10
        # 0.2 + 2*0.10 = 0.40 < 0.45, so effective_min_diameter = 0.45
        assert diameter == 0.45


class TestFindAllVias:
    """Tests for find_all_vias function."""

    def test_finds_all_vias(self, pcb_with_undersized_vias: Path):
        """Should find all vias in the PCB."""
        doc = parse_file(pcb_with_undersized_vias)
        vias = find_all_vias(doc)

        assert len(vias) == 3

        # Check first via properties
        node, x, y, drill, diameter, net, uuid, start_layer, end_layer, via_type = vias[0]
        assert x == 110
        assert y == 110
        assert drill == 0.2
        assert diameter == 0.45
        assert net == 1
        assert uuid == "via-1"
        assert start_layer == "F.Cu"
        assert end_layer == "B.Cu"
        assert via_type == ""


class TestFixVias:
    """Tests for fix_vias function."""

    def test_fixes_undersized_vias(self, pcb_with_undersized_vias: Path):
        """Should fix only undersized vias."""
        doc = parse_file(pcb_with_undersized_vias)

        fixes, warnings, _skips = fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=True)

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
        _, _, _, drill_before, diameter_before, _, _, _, _, _ = vias_before[0]

        # Run fix with dry_run=True
        fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=True)

        # Values should be unchanged
        vias_after = find_all_vias(doc)
        _, _, _, drill_after, diameter_after, _, _, _, _, _ = vias_after[0]

        assert drill_before == drill_after
        assert diameter_before == diameter_after

    def test_applies_fixes_when_not_dry_run(self, pcb_with_undersized_vias: Path):
        """Should modify document when not dry run."""
        doc = parse_file(pcb_with_undersized_vias)

        fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=False)

        # Values should be updated
        vias = find_all_vias(doc)
        _, _, _, drill, diameter, _, _, _, _, _ = vias[0]

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
        fixes, warnings, _skips = fix_vias(doc, target_drill=0.3, target_diameter=0.6)

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
        fixes, warnings, _skips = fix_vias(
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
        fixes, warnings, _skips = fix_vias(
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
        fixes, warnings, _skips = fix_vias(
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
        _, _, _, drill, diameter, _, _, _, _, _ = vias[0]
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
        _, _, _, drill, diameter, _, _, _, _, _ = vias[0]

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

    def test_4layer_jlcpcb_compliant_via_not_enlarged(self, tmp_path: Path, capsys):
        """4-layer JLCPCB: 0.45mm via with 0.20mm drill is already compliant.

        With the corrected 0.10mm annular ring for 4-layer advanced PCB:
        - Annular ring: (0.45 - 0.20) / 2 = 0.125mm >= 0.10mm required
        - min_via_diameter: 0.45mm
        - The via is already at 0.45mm, so no enlargement should occur.
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

        result = main(
            [
                str(pcb_file),
                "--mfr",
                "jlcpcb",
                "--layers",
                "4",
                "--dry-run",
            ]
        )

        assert result == 0

        # Verify output says "No vias needed resizing" — the via is compliant
        captured = capsys.readouterr()
        assert "No vias needed resizing" in captured.out

    def test_4layer_jlcpcb_undersized_via_enlarged(self, tmp_path: Path, capsys):
        """4-layer JLCPCB: 0.44mm via with 0.20mm drill should be enlarged to 0.45mm.

        The via is below min_via_diameter (0.45mm) so it must be enlarged.
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
          (via (at 133.75 95.0) (size 0.44) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "4layer_under.kicad_pcb"
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

        # Verify the via was resized to minimum
        output_doc = parse_file(output_file)
        vias = find_all_vias(output_doc)
        _, _, _, drill, diameter, _, _, _, _, _ = vias[0]

        assert drill == 0.2  # Drill meets min (0.2mm for 4-layer)
        assert diameter == 0.45  # Enlarged to min_via_diameter


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

        fixes, warnings, _skips = fix_vias(
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

        fixes, warnings, _skips = fix_vias(
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
        fixes, warnings, _skips = fix_vias(
            doc, target_drill=0.3, target_diameter=0.6, min_clearance=0.127, dry_run=True
        )
        assert len(fixes) == 1
        assert len(warnings) == 0

        # With old hardcoded clearance (0.2mm), the same trace would warn
        # gap = 0.175 < 0.2 => warning
        doc2 = parse_file(pcb_file)
        fixes2, warnings2, _skips2 = fix_vias(
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


class TestSelectiveViaSkip:
    """Tests for --skip-if-clearance-violation feature (Option B)."""

    def test_skip_via_with_clearance_violation(self, tmp_path: Path):
        """Via near a trace should be skipped when skip_on_clearance is True."""
        # Via at (100, 100) would be resized from 0.45mm to 0.6mm.
        # Trace at y=100.5, width=0.25 => clearance after resize:
        # dist=0.5, gap = 0.5 - 0.3 - 0.125 = 0.075 < 0.127 => violation
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
        pcb_file = tmp_path / "skip_via.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings, skips = fix_vias(
            doc,
            target_drill=0.3,
            target_diameter=0.6,
            min_clearance=0.127,
            dry_run=True,
            skip_on_clearance=True,
        )

        assert len(fixes) == 0
        assert len(warnings) == 0
        assert len(skips) == 1
        assert skips[0].uuid == "via-1"
        assert skips[0].current_diameter == 0.45
        assert skips[0].would_be_diameter == 0.6
        assert "track" in skips[0].reason

    def test_skip_does_not_affect_uncrowded_vias(self, tmp_path: Path):
        """Isolated via should still be resized even with skip_on_clearance=True."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "isolated_skip.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings, skips = fix_vias(
            doc,
            target_drill=0.3,
            target_diameter=0.6,
            min_clearance=0.127,
            dry_run=True,
            skip_on_clearance=True,
        )

        assert len(fixes) == 1
        assert len(skips) == 0
        assert fixes[0].new_diameter == 0.6

    def test_mixed_skip_and_resize(self, tmp_path: Path):
        """Board with two vias: one crowded (skip), one isolated (resize)."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-crowded"))
          (via (at 200 200) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-isolated"))
          (segment (start 95 100.5) (end 105 100.5) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-1"))
        )
        """
        pcb_file = tmp_path / "mixed.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings, skips = fix_vias(
            doc,
            target_drill=0.3,
            target_diameter=0.6,
            min_clearance=0.127,
            dry_run=True,
            skip_on_clearance=True,
        )

        assert len(fixes) == 1
        assert len(skips) == 1
        assert fixes[0].uuid == "via-isolated"
        assert skips[0].uuid == "via-crowded"

    def test_skip_keeps_original_size_on_disk(self, tmp_path: Path):
        """When skip_on_clearance skips a via, the file should retain original size."""
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
        pcb_file = tmp_path / "skip_disk.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings, skips = fix_vias(
            doc,
            target_drill=0.3,
            target_diameter=0.6,
            min_clearance=0.127,
            dry_run=False,
            skip_on_clearance=True,
        )

        # No fixes applied (the only via was skipped)
        assert len(fixes) == 0
        assert len(skips) == 1

        # Verify the via retains its original size in the document
        vias = find_all_vias(doc)
        _, _, _, drill, diameter, _, _, _, _, _ = vias[0]
        assert drill == 0.2
        assert diameter == 0.45

    def test_skip_without_flag_still_warns(self, tmp_path: Path):
        """Without skip_on_clearance, crowded vias produce warnings, not skips."""
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
        pcb_file = tmp_path / "no_skip.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings, skips = fix_vias(
            doc,
            target_drill=0.3,
            target_diameter=0.6,
            min_clearance=0.127,
            dry_run=True,
            skip_on_clearance=False,
        )

        assert len(fixes) == 1
        assert len(warnings) == 1
        assert len(skips) == 0

    def test_cli_skip_flag(self, tmp_path: Path, capsys):
        """CLI --skip-if-clearance-violation flag works correctly."""
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
        pcb_file = tmp_path / "cli_skip.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main(
            [
                str(pcb_file),
                "--mfr",
                "jlcpcb",
                "--dry-run",
                "--format",
                "json",
                "--skip-if-clearance-violation",
            ]
        )

        assert result == 0  # No warnings (via was skipped, not warned)

        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data["fixes"]) == 0
        assert len(data["warnings"]) == 0
        assert len(data["skipped"]) == 1
        assert data["skipped"][0]["uuid"] == "via-1"

    def test_cli_skip_json_output_includes_skipped_key(self, tmp_path: Path, capsys):
        """JSON output includes the 'skipped' key even when empty."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "json_skipped.kicad_pcb"
        pcb_file.write_text(pcb_content)

        main([str(pcb_file), "--dry-run", "--format", "json"])

        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "skipped" in data
        assert len(data["skipped"]) == 0


class TestLayerAutoDetection:
    """Tests for auto-detecting layer count from PCB file."""

    def test_4layer_pcb_auto_detects(self, tmp_path: Path, capsys):
        """A 4-layer PCB file should auto-detect 4 layers and use 4-layer rules."""
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
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "4layer_auto.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Without --layers, should auto-detect 4 layers from PCB.
        # For 4-layer JLCPCB: min_via_drill=0.2, min_via_diameter=0.45,
        # annular ring 0.10 (corrected in #1414) requires only
        # 0.2 + 2*0.10 = 0.40, so the 0.45mm via is already compliant
        # and no fixes are emitted -- but the 4-layer targets prove the
        # auto-detection picked the right rule set.
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

        assert result == 0
        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data["fixes"]) == 0
        # 4-layer rules: mfr min diameter 0.45 dominates 0.40 annular floor
        assert data["target_diameter_mm"] == 0.45
        assert data["target_drill_mm"] == 0.2

    def test_2layer_pcb_auto_detects(self, tmp_path: Path, capsys):
        """A 2-layer PCB file should auto-detect 2 layers and use 2-layer rules."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "2layer_auto.kicad_pcb"
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

        assert result == 0
        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # 2-layer rules: min_via_drill=0.3, min_via_diameter=0.6
        assert data["target_diameter_mm"] == 0.6
        assert data["target_drill_mm"] == 0.3

    def test_explicit_layers_overrides_auto(self, tmp_path: Path, capsys):
        """Explicit --layers flag should override auto-detection."""
        # 4-layer PCB file, but we force --layers 2
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
          (via (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "override.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main(
            [
                str(pcb_file),
                "--mfr",
                "jlcpcb",
                "--layers",
                "2",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        assert result == 0
        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # 2-layer rules forced despite 4-layer PCB
        assert data["target_diameter_mm"] == 0.6
        assert data["target_drill_mm"] == 0.3

    def test_layers_4_selects_4layer_1oz_rules(self):
        """--layers 4 should select 4layer_1oz rules from JLCPCB."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 4, 1.0, None, None)
        assert drill == 0.2
        # JLCPCB 4-layer annular ring is 0.10mm (corrected in #1414):
        # annular floor 0.2 + 2*0.10 = 0.40 < mfr min diameter 0.45.
        assert diameter == 0.45
        assert annular == 0.10
        assert clearance == 0.1016

    def test_layers_2_still_enlarges_to_0_6mm(self):
        """--layers 2 should still require 0.6mm diameter for 2-layer JLCPCB."""
        drill, diameter, annular, clearance = get_design_rules("jlcpcb", 2, 1.0, None, None)
        assert drill == 0.3
        assert diameter == 0.6
        assert clearance == 0.127


class TestSameLayerViaDetection:
    """Tests for same-layer via detection and repair."""

    def test_detect_same_layer_vias_dry_run(self, tmp_path: Path):
        """Same-layer vias should be detected in dry-run mode."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-1"))
          (via (at 120 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-2"))
        )
        """
        pcb_file = tmp_path / "same_layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_same_layer_vias(doc, dry_run=True)

        assert len(fixes) == 1
        assert fixes[0].uuid == "via-1"
        assert fixes[0].old_start_layer == "F.Cu"
        assert fixes[0].old_end_layer == "F.Cu"
        assert fixes[0].new_start_layer == "F.Cu"
        assert fixes[0].new_end_layer == "B.Cu"
        assert len(warnings) == 0

    def test_repair_same_layer_through_hole_vias(self, tmp_path: Path):
        """Through-hole same-layer vias should be repaired to span outer layers."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "repair_same_layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_same_layer_vias(doc, dry_run=False)

        assert len(fixes) == 1

        # Verify the layers node was actually updated
        vias = find_all_vias(doc)
        _, _, _, _, _, _, _, start_layer, end_layer, _ = vias[0]
        assert start_layer == "F.Cu"
        assert end_layer == "B.Cu"

    def test_same_layer_blind_via_flagged_not_repaired(self, tmp_path: Path):
        """Blind same-layer vias should produce a warning without auto-repair."""
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
          (via (at 110 110) (size 0.45) (drill 0.2) (type blind) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-blind"))
        )
        """
        pcb_file = tmp_path / "blind_same_layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_same_layer_vias(doc, dry_run=False)

        assert len(fixes) == 0
        assert len(warnings) == 1
        assert warnings[0].uuid == "via-blind"
        assert warnings[0].via_type == "blind"

        # Verify the layers node was NOT changed
        vias = find_all_vias(doc)
        _, _, _, _, _, _, _, start_layer, end_layer, _ = vias[0]
        assert start_layer == "F.Cu"
        assert end_layer == "F.Cu"

    def test_same_layer_micro_via_flagged_not_repaired(self, tmp_path: Path):
        """Micro same-layer vias should produce a warning without auto-repair."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (1 "In1.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.3) (drill 0.15) (type micro) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-micro"))
        )
        """
        pcb_file = tmp_path / "micro_same_layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_same_layer_vias(doc, dry_run=False)

        assert len(fixes) == 0
        assert len(warnings) == 1
        assert warnings[0].via_type == "micro"

    def test_via_missing_layers_node_skipped(self, tmp_path: Path):
        """Via with missing layers node should be skipped gracefully."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (net 1) (uuid "via-nolayers"))
        )
        """
        pcb_file = tmp_path / "no_layers.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_same_layer_vias(doc, dry_run=False)

        assert len(fixes) == 0
        assert len(warnings) == 0

    def test_correct_vias_no_false_positives(self, tmp_path: Path):
        """Vias with distinct layers should not be flagged."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
          (via (at 120 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-2"))
        )
        """
        pcb_file = tmp_path / "correct_vias.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, warnings = fix_same_layer_vias(doc, dry_run=False)

        assert len(fixes) == 0
        assert len(warnings) == 0

    def test_mixed_same_layer_and_undersized(self, tmp_path: Path):
        """Both same-layer and undersized fixes should be applied in one file."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-same"))
          (via (at 120 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-small"))
        )
        """
        pcb_file = tmp_path / "mixed.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        # Fix same-layer first
        sl_fixes, sl_warnings = fix_same_layer_vias(doc, dry_run=False)
        assert len(sl_fixes) == 1
        assert sl_fixes[0].uuid == "via-same"

        # Fix undersized second
        size_fixes, _, _ = fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=False)
        assert len(size_fixes) == 1
        assert size_fixes[0].uuid == "via-small"

    def test_4layer_board_uses_outer_layers(self, tmp_path: Path):
        """On a 4-layer board, through-hole same-layer via should span F.Cu to B.Cu."""
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
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "In1.Cu" "In1.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "4layer_same.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, _ = fix_same_layer_vias(doc, dry_run=False)

        assert len(fixes) == 1
        assert fixes[0].new_start_layer == "F.Cu"
        assert fixes[0].new_end_layer == "B.Cu"

    def test_dry_run_does_not_modify_same_layer(self, tmp_path: Path):
        """Dry run should not modify same-layer vias."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "dry_same.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        fixes, _ = fix_same_layer_vias(doc, dry_run=True)
        assert len(fixes) == 1

        # Layers should be unchanged
        vias = find_all_vias(doc)
        _, _, _, _, _, _, _, start_layer, end_layer, _ = vias[0]
        assert start_layer == "F.Cu"
        assert end_layer == "F.Cu"


class TestSameLayerViaCLI:
    """CLI integration tests for same-layer via detection."""

    def test_cli_dry_run_reports_same_layer(self, tmp_path: Path, capsys):
        """CLI --dry-run should report same-layer vias."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "cli_same.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main([str(pcb_file), "--mfr", "jlcpcb", "--dry-run"])
        assert result == 0

        captured = capsys.readouterr()
        assert "same-layer" in captured.out.lower()

    def test_cli_json_includes_same_layer_keys(self, tmp_path: Path, capsys):
        """JSON output should include same_layer_fixes and same_layer_warnings."""
        import json as json_mod

        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "cli_json_same.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main([str(pcb_file), "--dry-run", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json_mod.loads(captured.out)

        assert "same_layer_fixes" in data
        assert len(data["same_layer_fixes"]) == 1
        assert data["same_layer_fixes"][0]["uuid"] == "via-1"
        assert data["same_layer_fixes"][0]["old_start_layer"] == "F.Cu"
        assert data["same_layer_fixes"][0]["new_end_layer"] == "B.Cu"
        assert "same_layer_warnings" in data
        assert len(data["same_layer_warnings"]) == 0

    def test_cli_repairs_same_layer_and_saves(self, tmp_path: Path):
        """CLI without --dry-run should repair same-layer vias and save."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "repair_cli.kicad_pcb"
        pcb_file.write_text(pcb_content)
        output_file = tmp_path / "repaired.kicad_pcb"

        result = main([str(pcb_file), "-o", str(output_file)])
        assert result == 0

        # Verify output file has corrected layers
        output_doc = parse_file(output_file)
        vias = find_all_vias(output_doc)
        _, _, _, _, _, _, _, start_layer, end_layer, _ = vias[0]
        assert start_layer == "F.Cu"
        assert end_layer == "B.Cu"

    def test_cli_exit_code_2_for_blind_same_layer_warning(self, tmp_path: Path):
        """CLI should return exit code 2 when blind same-layer vias produce warnings."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (1 "In1.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.45) (drill 0.2) (type blind) (layers "F.Cu" "F.Cu") (net 1) (uuid "via-blind"))
        )
        """
        pcb_file = tmp_path / "blind_cli.kicad_pcb"
        pcb_file.write_text(pcb_content)

        result = main([str(pcb_file), "--dry-run"])
        assert result == 2


class TestGetBoardOuterLayers:
    """Tests for get_board_outer_layers helper."""

    def test_2layer_board(self, tmp_path: Path):
        """2-layer board should return F.Cu and B.Cu."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup (pad_to_mask_clearance 0))
        )
        """
        pcb_file = tmp_path / "2layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        start, end = get_board_outer_layers(doc)
        assert start == "F.Cu"
        assert end == "B.Cu"

    def test_4layer_board(self, tmp_path: Path):
        """4-layer board should return F.Cu and B.Cu (outermost copper)."""
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
        )
        """
        pcb_file = tmp_path / "4layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        doc = parse_file(pcb_file)

        start, end = get_board_outer_layers(doc)
        assert start == "F.Cu"
        assert end == "B.Cu"


# ===========================================================================
# Issue #4359 -- fix-vias --relocate-in-pad (Phase 1: signal-via slide-out)
# ===========================================================================

from kicad_tools.cli.fix_vias_cmd import get_mfr_design_rules  # noqa: E402
from kicad_tools.cli.relocate_in_pad_vias import (  # noqa: E402
    _check_clearance,
    _collect_smd_pads_by_net,
    _collect_tht_pads,
    relocate_in_pad_vias,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.via_in_pad import (  # noqa: E402
    ViaInPadRule,
    _pad_absolute_bbox,
    _via_inside_pad,
)

# Signal in-pad via: 1x1mm SMD pad U1-1 at (100,100) on net SIG1, a via drilled
# dead-center inside it, and a B.Cu escape track leaving the pad to (105,100).
_PCB_SIGNAL_IN_PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-inpad"))
  (segment (start 100 100) (end 105 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-escape"))
)
"""

# In-pad via with NO connected routed track (plane-stitch style).
_PCB_PLANE_STITCH_IN_PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "GND"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-stitch"))
)
"""

# In-pad signal via whose only off-pad slide (+X) lands inside a foreign-net pad
# -> relocation would create a clearance violation and must be skipped.
_PCB_BOXED_IN = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (footprint "test:pad" (layer "F.Cu") (at 100.9 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 2 "SIG2"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-boxed"))
  (segment (start 100 100) (end 105 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-boxed"))
)
"""

# Plane-stitch in-pad via WITH a same-net pour whose boundary extends well
# beyond the pad -- Phase 2 can slide the via off-pad into the zone (no stub).
_PCB_PLANE_STITCH_WITH_ZONE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "GND"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-stitch"))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "zone-gnd")
    (polygon (pts (xy 95 95) (xy 108 95) (xy 108 108) (xy 95 108)))
  )
)
"""

# Plane-stitch in-pad via whose same-net pour is only a thin strip to the +X of
# the pad, and a foreign-net pad occupies that strip -> every zone-legal
# candidate collides with foreign copper, so the via is unresolvable (never
# mis-placed outside the pour or into a short).
_PCB_PLANE_STITCH_BOXED_IN = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "test:pad" (layer "F.Cu") (at 102 100)
    (pad "1" smd rect (at 0 0) (size 2.0 1.0) (layers "F.Cu") (net 2 "SIG2"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-stitch"))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "zone-gnd")
    (polygon (pts (xy 100.5 99.7) (xy 104 99.7) (xy 104 100.3) (xy 100.5 100.3)))
  )
)
"""

# Plane-stitch via with a same-net pour, but the via is already clear of the
# pad -> nothing to relocate (clean no-op even though a zone exists).
_PCB_PLANE_STITCH_CLEAR_OF_PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "GND"))
  )
  (via (at 104 104) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-clear"))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "zone-gnd")
    (polygon (pts (xy 95 95) (xy 108 95) (xy 108 108) (xy 95 108)))
  )
)
"""

# Board with a via that is NOT inside any pad (well clear of it).
_PCB_NO_IN_PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-clear"))
  (segment (start 110 110) (end 115 110) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-clear"))
)
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "board.kicad_pcb"
    p.write_text(content)
    return p


def _via_in_pad_count(pcb: PCB, mfr: str = "jlcpcb") -> int:
    rules = get_mfr_design_rules(mfr, 2, 1.0)
    return len(ViaInPadRule().check(pcb, rules).violations)


class TestRelocateInPadVias:
    """Phase-1 signal in-pad via relocation (issue #4359)."""

    def test_signal_via_moved_off_pad(self, tmp_path: Path):
        """In-pad signal via is slid outside the pad with drill-edge clearance."""
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        pcb = PCB.load(p)
        assert _via_in_pad_count(pcb) == 1  # precondition: it IS in-pad

        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert len(result.moved) == 1
        assert not result.skipped
        assert not result.unresolvable

        via = pcb.vias[0]
        fp = pcb.footprints[0]
        pad = fp.pads[0]
        bbox = _pad_absolute_bbox(pad, fp)

        # Via drill is now fully OUTSIDE the pad bbox.
        assert not _via_inside_pad(via, bbox)
        # Drill edge clears the pad edge by at least min_clearance.
        drill_edge = via.position[0] - via.drill / 2.0
        assert drill_edge >= bbox[2] + rules.min_clearance_mm - 1e-6

    def test_stub_and_connectivity_preserved(self, tmp_path: Path):
        """A stub on the pad layer + escape layer keeps the net connected."""
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        moved = result.moved[0]
        # Stub added on both the pad's copper layer and the escape track layer.
        assert "F.Cu" in moved.stub_layers
        assert "B.Cu" in moved.stub_layers

        via = pcb.vias[0]
        new_x, new_y = via.position
        # The via net is unchanged (no net rip).
        assert via.net_number == 1
        # A same-net segment now lands on the relocated via (connectivity intact).
        landing = [
            seg
            for seg in pcb.segments_in_net(1)
            if (abs(seg.start[0] - new_x) < 1e-6 and abs(seg.start[1] - new_y) < 1e-6)
            or (abs(seg.end[0] - new_x) < 1e-6 and abs(seg.end[1] - new_y) < 1e-6)
        ]
        assert landing, "no segment lands on the relocated via"
        # The original in-pad escape segment is untouched (no existing copper mutated).
        escape = [seg for seg in pcb.segments if seg.uuid == "seg-escape"]
        assert len(escape) == 1
        assert escape[0].start == (100.0, 100.0)
        assert escape[0].end == (105.0, 100.0)

    def test_relocate_persists_through_save(self, tmp_path: Path):
        """The move round-trips to disk and clears the via-in-pad DRC count."""
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        relocate_in_pad_vias(pcb, rules, dry_run=False)
        moved_pos = pcb.vias[0].position
        pcb.save(p)

        reloaded = PCB.load(p)
        assert reloaded.vias[0].position == pytest.approx(moved_pos)
        assert _via_in_pad_count(reloaded) == 0

    def test_dry_run_mutates_nothing(self, tmp_path: Path):
        """--dry-run reports the move but writes nothing to the board."""
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        pcb = PCB.load(p)
        n_segments_before = len(pcb.segments)
        via_pos_before = pcb.vias[0].position

        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=True)

        assert len(result.moved) == 1  # it is reported as movable
        # ...but nothing was mutated.
        assert pcb.vias[0].position == via_pos_before
        assert len(pcb.segments) == n_segments_before

    def test_dry_run_via_main_no_file_write(self, tmp_path: Path):
        """The CLI --dry-run path leaves the file byte-identical."""
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        original = p.read_text()
        rc = main([str(p), "--mfr", "jlcpcb", "--relocate-in-pad", "--dry-run", "-q"])
        assert rc in (0, 2)
        assert p.read_text() == original

    def test_no_in_pad_vias_clean_noop(self, tmp_path: Path):
        """A board with no in-pad vias is a clean no-op (no moves, no writes)."""
        p = _write(tmp_path, _PCB_NO_IN_PAD)
        original = p.read_text()
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert not result.skipped
        assert not result.unresolvable
        # main() must not rewrite the file when nothing changed.
        rc = main([str(p), "--mfr", "jlcpcb", "--relocate-in-pad", "-q"])
        assert rc == 0
        assert p.read_text() == original

    def test_supported_mfr_is_noop(self, tmp_path: Path):
        """On jlcpcb-tier1 (via-in-pad supported) nothing is relocated."""
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb-tier1", 2, 1.0)
        assert rules.via_in_pad_supported is True
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert result.supported_noop is True
        assert not result.moved
        assert pcb.vias[0].position == (100.0, 100.0)

    def test_plane_stitch_via_reported_unresolvable(self, tmp_path: Path):
        """An in-pad via with no escape track is surfaced, never left silently."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_IN_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert len(result.unresolvable) == 1
        assert result.unresolvable[0].category == "unresolvable"
        # The via is left untouched (still in-pad, but reported).
        assert pcb.vias[0].position == (100.0, 100.0)

    def test_boxed_in_via_skipped(self, tmp_path: Path):
        """A via that cannot clear a foreign-net pad is skipped, not mis-placed."""
        p = _write(tmp_path, _PCB_BOXED_IN)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert len(result.skipped) == 1
        assert result.skipped[0].category == "skipped"
        # Untouched: never emit a worse board.
        assert pcb.vias[0].position == (100.0, 100.0)

    def test_net_filter_scopes_pass(self, tmp_path: Path):
        """--net scoping skips vias whose net is not in the filter set."""
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, nets={"OTHER_NET"}, dry_run=False)
        assert not result.moved
        assert pcb.vias[0].position == (100.0, 100.0)

        # And the matching net IS processed.
        pcb2 = PCB.load(p)
        result2 = relocate_in_pad_vias(pcb2, rules, nets={"SIG1"}, dry_run=False)
        assert len(result2.moved) == 1

    def test_cli_main_json_report(self, tmp_path: Path, capsys):
        """--format json emits moved/skipped/unresolvable arrays."""
        import json as _json

        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        rc = main([str(p), "--mfr", "jlcpcb", "--relocate-in-pad", "--dry-run", "--format", "json"])
        assert rc in (0, 2)
        out = capsys.readouterr().out
        data = _json.loads(out)
        assert data["dry_run"] is True
        assert len(data["moved"]) == 1
        assert "skipped" in data and "unresolvable" in data


# ===========================================================================
# Issue #4367 -- fix-vias --relocate-in-pad (Phase 2: plane-stitch off-pad)
# ===========================================================================

from kicad_tools.cli.stitch_cmd import point_in_polygon  # noqa: E402


class TestPlaneStitchRelocation:
    """Phase-2 plane-stitch in-pad via relocation (issue #4367)."""

    def test_plane_stitch_via_moved_into_zone(self, tmp_path: Path):
        """A plane-stitch via is slid off-pad into the same-net zone, no stub."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_WITH_ZONE)
        pcb = PCB.load(p)
        assert _via_in_pad_count(pcb) == 1  # precondition: it IS in-pad

        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert len(result.moved) == 1
        assert not result.skipped
        assert not result.unresolvable

        moved = result.moved[0]
        # Plane-stitch move: NO stub (pour overlap carries the net after refill).
        assert moved.stub_layers == []
        assert moved.kind == "plane-stitch"

        via = pcb.vias[0]
        fp = pcb.footprints[0]
        pad = fp.pads[0]
        bbox = _pad_absolute_bbox(pad, fp)

        # Via drill is now fully OUTSIDE the pad bbox.
        assert not _via_inside_pad(via, bbox)
        # Drill edge clears the pad edge by at least min_clearance (any side).
        drill_edge_gap = (
            min(
                abs(via.position[0] - bbox[0]),
                abs(via.position[0] - bbox[2]),
                abs(via.position[1] - bbox[1]),
                abs(via.position[1] - bbox[3]),
            )
            - via.drill / 2.0
        )
        assert drill_edge_gap >= rules.min_clearance_mm - 1e-6

        # The new position lies inside the same-net zone boundary polygon.
        zone = pcb.zones[0]
        assert point_in_polygon(via.position[0], via.position[1], zone.polygon)

        # No stub segment was appended (plane-stitch relies on pour overlap).
        assert len(pcb.segments) == 0

    def test_plane_stitch_move_persists_through_save(self, tmp_path: Path):
        """The plane-stitch move round-trips to disk and clears the DRC count."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_WITH_ZONE)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        relocate_in_pad_vias(pcb, rules, dry_run=False)
        moved_pos = pcb.vias[0].position
        pcb.save(p)

        reloaded = PCB.load(p)
        assert reloaded.vias[0].position == pytest.approx(moved_pos)
        assert _via_in_pad_count(reloaded) == 0

    def test_plane_stitch_dry_run_mutates_nothing(self, tmp_path: Path):
        """--dry-run reports the plane-stitch move but writes nothing."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_WITH_ZONE)
        pcb = PCB.load(p)
        n_segments_before = len(pcb.segments)
        via_pos_before = pcb.vias[0].position

        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=True)

        assert len(result.moved) == 1
        assert result.moved[0].kind == "plane-stitch"
        # ...but nothing was mutated.
        assert pcb.vias[0].position == via_pos_before
        assert len(pcb.segments) == n_segments_before

    def test_plane_stitch_dry_run_via_main_no_file_write(self, tmp_path: Path):
        """The CLI --dry-run path leaves the file byte-identical for a stitch move."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_WITH_ZONE)
        original = p.read_text()
        rc = main([str(p), "--mfr", "jlcpcb", "--relocate-in-pad", "--dry-run", "-q"])
        assert rc in (0, 2)
        assert p.read_text() == original

    def test_plane_stitch_no_zone_reported_unresolvable(self, tmp_path: Path):
        """A plane-stitch via with NO same-net zone stays unresolvable, untouched."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_IN_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert len(result.unresolvable) == 1
        assert result.unresolvable[0].category == "unresolvable"
        assert "no same-net zone" in result.unresolvable[0].reason
        # The via is left untouched (still in-pad, but reported).
        assert pcb.vias[0].position == (100.0, 100.0)

    def test_plane_stitch_boxed_in_unresolvable(self, tmp_path: Path):
        """A plane-stitch via whose only in-zone candidates hit foreign copper
        is unresolvable, never mis-placed outside the pour or into a short."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_BOXED_IN)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert len(result.unresolvable) == 1
        assert result.unresolvable[0].category == "unresolvable"
        # Untouched: never emit a worse board.
        assert pcb.vias[0].position == (100.0, 100.0)

    def test_plane_stitch_clear_of_pad_clean_noop(self, tmp_path: Path):
        """A via already clear of its pad is a clean no-op even with a zone."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_CLEAR_OF_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert not result.skipped
        assert not result.unresolvable
        assert pcb.vias[0].position == (104.0, 104.0)

    def test_plane_stitch_supported_mfr_is_noop(self, tmp_path: Path):
        """On jlcpcb-tier1 (via-in-pad supported) nothing is relocated."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_WITH_ZONE)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb-tier1", 2, 1.0)
        assert rules.via_in_pad_supported is True
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert result.supported_noop is True
        assert not result.moved
        assert pcb.vias[0].position == (100.0, 100.0)

    def test_plane_stitch_net_scoping(self, tmp_path: Path):
        """--net scoping excludes/includes the plane net as expected."""
        p = _write(tmp_path, _PCB_PLANE_STITCH_WITH_ZONE)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)

        # Excluded: no move.
        pcb = PCB.load(p)
        result = relocate_in_pad_vias(pcb, rules, nets={"OTHER_NET"}, dry_run=False)
        assert not result.moved
        assert pcb.vias[0].position == (100.0, 100.0)

        # Included: moved.
        pcb2 = PCB.load(p)
        result2 = relocate_in_pad_vias(pcb2, rules, nets={"GND"}, dry_run=False)
        assert len(result2.moved) == 1
        assert result2.moved[0].kind == "plane-stitch"

    def test_plane_stitch_json_report_includes_kind(self, tmp_path: Path, capsys):
        """--format json marks the plane-stitch move with kind + empty stub_layers."""
        import json as _json

        p = _write(tmp_path, _PCB_PLANE_STITCH_WITH_ZONE)
        rc = main([str(p), "--mfr", "jlcpcb", "--relocate-in-pad", "--dry-run", "--format", "json"])
        assert rc in (0, 2)
        data = _json.loads(capsys.readouterr().out)
        assert len(data["moved"]) == 1
        assert data["moved"][0]["kind"] == "plane-stitch"
        assert data["moved"][0]["stub_layers"] == []


# ---------------------------------------------------------------------------
# Phase 3 (issue #4377): multi-branch fallback + THT hole-to-hole clearance.
# ---------------------------------------------------------------------------

# Signal in-pad via whose +X off-pad slide lands next to a foreign-net THT pad
# drill closer than min_hole_to_hole (0.5mm) -> the slide must be SKIPPED.
# The via slides to x=100.777 (t_exit 0.5 + drill/2 0.15 + clearance 0.127); the
# THT hole at (101.3, 100) leaves a hole-to-hole gap of only ~0.22mm.  The
# --refill-zones DRC cross-gate would NOT catch this (hole-to-hole is not a
# copper interaction), so _check_clearance must.
_PCB_THT_HOLE_TO_HOLE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (footprint "test:tht" (layer "F.Cu") (at 101.3 100)
    (pad "1" thru_hole circle (at 0 0) (size 0.5 0.5) (drill 0.3) (layers "*.Cu") (net 2 "SIG2"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-inpad"))
  (segment (start 100 100) (end 105 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-escape"))
)
"""

# Same slide, but the crowding THT pad is on the SAME net (SIG1) as the via.
# Hole-to-hole is net-agnostic (a drill collision is a drill collision), so the
# slide must still be SKIPPED -- mirroring the existing other-via logic.
_PCB_THT_HOLE_TO_HOLE_SAME_NET = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (footprint "test:tht" (layer "F.Cu") (at 101.3 100)
    (pad "1" thru_hole circle (at 0 0) (size 0.5 0.5) (drill 0.3) (layers "*.Cu") (net 1 "SIG1"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-inpad"))
  (segment (start 100 100) (end 105 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-escape"))
)
"""

# Signal in-pad via whose +X slide keeps the THT DRILL far (hole-to-hole OK) but
# runs the via copper inside min_clearance of a large foreign-net THT pad's
# copper -> SKIPPED on copper clearance (different-net plated pad).
_PCB_THT_COPPER_CLEARANCE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (footprint "test:tht" (layer "F.Cu") (at 102 100)
    (pad "1" thru_hole circle (at 0 0) (size 2.0 2.0) (drill 0.3) (layers "*.Cu") (net 2 "SIG2"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-inpad"))
  (segment (start 100 100) (end 105 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-escape"))
)
"""

# Multi-branch in-pad via: both connected branches terminate INSIDE the pad, so
# no single escape direction exists.  Phase 3 walks the 8-dir ladder and stubs
# on every connected layer (F.Cu from the pad + F.Cu/B.Cu from the branches).
_PCB_MULTIBRANCH = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-mb"))
  (segment (start 100 100) (end 100.3 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-b1"))
  (segment (start 100 100) (end 100 100.3) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-b2"))
)
"""

# Multi-branch via boxed in by a large overlapping foreign-net pad -> every
# ladder candidate collides with foreign copper, so it is UNRESOLVABLE and left
# in place (never mis-placed).
_PCB_MULTIBRANCH_BOXED_IN = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (footprint "test:block" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 8.0 8.0) (layers "F.Cu") (net 2 "SIG2"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-mb"))
  (segment (start 100 100) (end 100.3 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-b1"))
  (segment (start 100 100) (end 100 100.3) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-b2"))
)
"""

# Footprint rotated 45deg with an in-pad signal via and a +X escape.  The
# over-approximated (rotated-rectangle) AABB drives a conservative slide; the
# moved via must still land clear of the true pad and pass _check_clearance.
_PCB_ROTATED_PAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (footprint "test:pad" (layer "F.Cu") (at 100 100 45)
    (pad "1" smd rect (at 0 0) (size 2.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-rot"))
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-rot"))
)
"""

# Dense-pitch cluster: a ring of 8 foreign-net THT pads boxes a multi-branch via
# in -- every ladder candidate is rejected on hole-to-hole (net-agnostic) to a
# ring drill, so the via is UNRESOLVABLE and the board is left unchanged.
_PCB_DENSE_THT_RING = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 1 "SIG1"))
  )
  (footprint "test:r1" (layer "F.Cu") (at 101 100)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (footprint "test:r2" (layer "F.Cu") (at 99 100)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (footprint "test:r3" (layer "F.Cu") (at 100 101)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (footprint "test:r4" (layer "F.Cu") (at 100 99)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (footprint "test:r5" (layer "F.Cu") (at 100.707 100.707)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (footprint "test:r6" (layer "F.Cu") (at 99.293 100.707)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (footprint "test:r7" (layer "F.Cu") (at 99.293 99.293)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (footprint "test:r8" (layer "F.Cu") (at 100.707 99.293)
    (pad "1" thru_hole circle (at 0 0) (size 0.9 0.9) (drill 0.6) (layers "*.Cu") (net 2 "SIG2")))
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-dense"))
  (segment (start 100 100) (end 100.3 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-d1"))
  (segment (start 100 100) (end 100 100.3) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-d2"))
)
"""


class TestRelocatePhase3:
    """Phase-3 edge cases (issue #4377): THT hole-to-hole + multi-branch."""

    def test_tht_hole_to_hole_blocks_slide(self, tmp_path: Path):
        """A slide that crowds a foreign THT drill is skipped, not moved."""
        p = _write(tmp_path, _PCB_THT_HOLE_TO_HOLE)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        via_pos_before = pcb.vias[0].position

        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert len(result.skipped) == 1
        reason = result.skipped[0].reason
        assert "hole-to-hole" in reason
        assert "THT pad" in reason
        # Safety invariant: the via was NOT moved into the violation.
        assert pcb.vias[0].position == via_pos_before

    def test_without_tht_pad_the_slide_succeeds(self, tmp_path: Path):
        """Contrast: identical geometry without the THT pad DOES relocate.

        Proves the THT hole-to-hole check is what blocks the move -- the pre-#4377
        code (which never inspected THT drills) would have mis-placed this via.
        """
        p = _write(tmp_path, _PCB_SIGNAL_IN_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)
        assert len(result.moved) == 1
        assert not result.skipped

    def test_tht_hole_to_hole_is_net_agnostic(self, tmp_path: Path):
        """A same-net THT drill still blocks the slide (hole-to-hole any net)."""
        p = _write(tmp_path, _PCB_THT_HOLE_TO_HOLE_SAME_NET)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)
        assert not result.moved
        assert len(result.skipped) == 1
        assert "hole-to-hole" in result.skipped[0].reason

    def test_tht_copper_clearance_different_net(self, tmp_path: Path):
        """Different-net THT copper (drill far) is rejected on copper clearance."""
        p = _write(tmp_path, _PCB_THT_COPPER_CLEARANCE)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)
        assert not result.moved
        assert len(result.skipped) == 1
        reason = result.skipped[0].reason
        assert "clearance" in reason
        assert "THT pad" in reason

    def test_collect_tht_pads_finds_plated_holes(self, tmp_path: Path):
        """_collect_tht_pads returns plated pads (any net) with a hole center."""
        p = _write(tmp_path, _PCB_THT_HOLE_TO_HOLE)
        pcb = PCB.load(p)
        tht = _collect_tht_pads(pcb)
        assert len(tht) == 1
        _fp, pad, _bbox, hole_center = tht[0]
        assert pad.drill == pytest.approx(0.3)
        assert hole_center == pytest.approx((101.3, 100.0))

    def test_multibranch_relocated_via_ladder(self, tmp_path: Path):
        """A via with no escaping branch relocates via the ladder + stubs."""
        p = _write(tmp_path, _PCB_MULTIBRANCH)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert len(result.moved) == 1
        assert not result.unresolvable
        moved = result.moved[0]
        # Signal-kind move (stubs), not plane-stitch.
        assert moved.kind == "signal"
        # Stubs land on every connected layer (pad F.Cu + both branch layers).
        assert "F.Cu" in moved.stub_layers
        assert "B.Cu" in moved.stub_layers

        via = pcb.vias[0]
        fp = pcb.footprints[0]
        bbox = _pad_absolute_bbox(fp.pads[0], fp)
        # The via now sits OUTSIDE the pad it was drilled into.
        assert not _via_inside_pad(via, bbox)
        # And the relocated via is clearance-clean.
        pads_by_net = _collect_smd_pads_by_net(pcb)
        tht = _collect_tht_pads(pcb)
        assert (
            _check_clearance(
                pcb,
                via,
                via.position[0],
                via.position[1],
                pads_by_net,
                tht,
                rules.min_clearance_mm,
                rules.min_hole_to_hole_mm,
            )
            is None
        )

    def test_multibranch_boxed_in_unresolvable(self, tmp_path: Path):
        """A boxed-in multi-branch via is unresolvable and left in place."""
        p = _write(tmp_path, _PCB_MULTIBRANCH_BOXED_IN)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        n_segments_before = len(pcb.segments)
        via_pos_before = pcb.vias[0].position

        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert len(result.unresolvable) == 1
        assert "multi-branch" in result.unresolvable[0].reason
        # Board unchanged: no stubs added, via not moved.
        assert len(pcb.segments) == n_segments_before
        assert pcb.vias[0].position == via_pos_before

    def test_rotated_pad_relocates_clearance_clean(self, tmp_path: Path):
        """A 45deg footprint relocates its via to a clearance-clean spot."""
        p = _write(tmp_path, _PCB_ROTATED_PAD)
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        # Relocate-or-unresolvable; never skipped-into-violation.
        assert len(result.moved) + len(result.unresolvable) == 1
        if result.moved:
            via = pcb.vias[0]
            fp = pcb.footprints[0]
            bbox = _pad_absolute_bbox(fp.pads[0], fp)
            assert not _via_inside_pad(via, bbox)
            pads_by_net = _collect_smd_pads_by_net(pcb)
            tht = _collect_tht_pads(pcb)
            assert (
                _check_clearance(
                    pcb,
                    via,
                    via.position[0],
                    via.position[1],
                    pads_by_net,
                    tht,
                    rules.min_clearance_mm,
                    rules.min_hole_to_hole_mm,
                )
                is None
            )

    def test_dense_tht_ring_unresolvable_board_unchanged(self, tmp_path: Path):
        """A via boxed in by a dense THT ring is unresolvable, board unchanged."""
        p = _write(tmp_path, _PCB_DENSE_THT_RING)
        original = p.read_text()
        pcb = PCB.load(p)
        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        n_segments_before = len(pcb.segments)

        result = relocate_in_pad_vias(pcb, rules, dry_run=False)

        assert not result.moved
        assert len(result.unresolvable) == 1
        assert len(pcb.segments) == n_segments_before
        # A CLI dry-run over the same board writes nothing.
        rc = main([str(p), "--mfr", "jlcpcb", "--relocate-in-pad", "--dry-run", "-q"])
        assert rc in (0, 2)
        assert p.read_text() == original

    def test_dry_run_with_tht_mutates_nothing(self, tmp_path: Path):
        """--dry-run over a THT-blocked board leaves the file byte-identical."""
        p = _write(tmp_path, _PCB_THT_HOLE_TO_HOLE)
        original = p.read_text()
        pcb = PCB.load(p)
        n_segments_before = len(pcb.segments)
        via_pos_before = pcb.vias[0].position

        rules = get_mfr_design_rules("jlcpcb", 2, 1.0)
        relocate_in_pad_vias(pcb, rules, dry_run=True)

        assert pcb.vias[0].position == via_pos_before
        assert len(pcb.segments) == n_segments_before
        rc = main([str(p), "--mfr", "jlcpcb", "--relocate-in-pad", "--dry-run", "-q"])
        assert rc in (0, 2)
        assert p.read_text() == original
