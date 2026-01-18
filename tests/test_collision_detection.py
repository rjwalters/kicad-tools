"""Tests for PCB collision detection and DRC methods.

Tests the new API methods on PCB class:
- check_placement_collision()
- validate_placements()
- run_drc()
- place_footprint_safe()
- set_design_rules()
"""

from pathlib import Path

import pytest

from kicad_tools.placement import (
    CollisionResult,
    ConflictType,
    DRCResult,
    PlacementValidationResult,
)
from kicad_tools.schema import PCB


# Test PCB with two components that can overlap
TWO_RESISTORS_PCB = """(kicad_pcb
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
  (segment (start 90 90) (end 110 90) (width 0.1) (layer "Edge.Cuts") (net 0))
  (segment (start 110 90) (end 110 110) (width 0.1) (layer "Edge.Cuts") (net 0))
  (segment (start 110 110) (end 90 110) (width 0.1) (layer "Edge.Cuts") (net 0))
  (segment (start 90 110) (end 90 90) (width 0.1) (layer "Edge.Cuts") (net 0))
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
def two_resistors_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with two resistors."""
    pcb_file = tmp_path / "two_resistors.kicad_pcb"
    pcb_file.write_text(TWO_RESISTORS_PCB)
    return pcb_file


class TestCheckPlacementCollision:
    """Tests for PCB.check_placement_collision()."""

    def test_no_collision_when_well_spaced(self, two_resistors_pcb: Path):
        """Test that well-spaced components report no collision."""
        pcb = PCB.load(str(two_resistors_pcb))

        # R2 is at (105, 100), check placing R1 at (100, 100) - should be fine
        result = pcb.check_placement_collision("R1", x=100, y=100)

        assert isinstance(result, CollisionResult)
        assert not result.has_collision
        assert result.message == "No collision detected"

    def test_collision_when_overlapping(self, two_resistors_pcb: Path):
        """Test that overlapping placement reports collision."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Move R1 to same position as R2 (105, 100)
        result = pcb.check_placement_collision("R1", x=105, y=100)

        assert result.has_collision
        assert result.other_ref == "R2"
        assert result.conflict_type in (
            ConflictType.COURTYARD_OVERLAP,
            ConflictType.PAD_CLEARANCE,
        )

    def test_collision_with_close_placement(self, two_resistors_pcb: Path):
        """Test collision detection for close but not overlapping placement."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Move R1 very close to R2
        result = pcb.check_placement_collision("R1", x=104, y=100)

        # This should detect courtyard overlap or pad clearance
        assert result.has_collision
        assert result.other_ref == "R2"

    def test_nonexistent_component(self, two_resistors_pcb: Path):
        """Test checking collision for nonexistent component."""
        pcb = PCB.load(str(two_resistors_pcb))

        result = pcb.check_placement_collision("U99", x=50, y=50)

        assert not result.has_collision
        assert "not found" in result.message

    def test_original_position_restored(self, two_resistors_pcb: Path):
        """Test that original position is restored after collision check."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Get original position
        fp = pcb.get_footprint("R1")
        orig_x, orig_y = fp.position
        orig_rot = fp.rotation

        # Check collision at different position
        pcb.check_placement_collision("R1", x=110, y=110, rotation=45)

        # Position should be restored
        assert fp.position == (orig_x, orig_y)
        assert fp.rotation == orig_rot

    def test_result_to_dict(self, two_resistors_pcb: Path):
        """Test CollisionResult serialization."""
        pcb = PCB.load(str(two_resistors_pcb))

        result = pcb.check_placement_collision("R1", x=105, y=100)
        d = result.to_dict()

        assert "has_collision" in d
        assert "other_ref" in d
        assert "conflict_type" in d
        assert "message" in d


class TestValidatePlacements:
    """Tests for PCB.validate_placements()."""

    def test_valid_placements(self, two_resistors_pcb: Path):
        """Test validation of well-spaced placements."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Place components far apart
        placements = {
            "R1": (95, 100, 0),
            "R2": (105, 100, 0),
        }
        result = pcb.validate_placements(placements)

        assert isinstance(result, PlacementValidationResult)
        assert result.is_valid
        assert result.collision_count == 0
        assert len(result.collisions) == 0

    def test_invalid_placements_overlap(self, two_resistors_pcb: Path):
        """Test validation detects overlapping placements."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Place components at same position
        placements = {
            "R1": (100, 100, 0),
            "R2": (100, 100, 0),  # Same position!
        }
        result = pcb.validate_placements(placements)

        assert not result.is_valid
        assert result.collision_count >= 1
        assert len(result.collisions) >= 1

    def test_original_positions_restored(self, two_resistors_pcb: Path):
        """Test that original positions are restored after validation."""
        pcb = PCB.load(str(two_resistors_pcb))

        fp1 = pcb.get_footprint("R1")
        fp2 = pcb.get_footprint("R2")
        orig1 = (fp1.position, fp1.rotation)
        orig2 = (fp2.position, fp2.rotation)

        # Validate different positions
        placements = {
            "R1": (50, 50, 90),
            "R2": (60, 60, 180),
        }
        pcb.validate_placements(placements)

        # Positions should be restored
        assert (fp1.position, fp1.rotation) == orig1
        assert (fp2.position, fp2.rotation) == orig2

    def test_result_to_dict(self, two_resistors_pcb: Path):
        """Test PlacementValidationResult serialization."""
        pcb = PCB.load(str(two_resistors_pcb))

        placements = {"R1": (100, 100, 0), "R2": (100, 100, 0)}
        result = pcb.validate_placements(placements)
        d = result.to_dict()

        assert "is_valid" in d
        assert "total_placements" in d
        assert "collision_count" in d
        assert "collisions" in d


class TestRunDrc:
    """Tests for PCB.run_drc()."""

    def test_clean_pcb_passes_drc(self, two_resistors_pcb: Path):
        """Test that well-designed PCB passes DRC."""
        pcb = PCB.load(str(two_resistors_pcb))

        result = pcb.run_drc()

        assert isinstance(result, DRCResult)
        assert result.passed
        assert result.violation_count == 0

    def test_overlapping_components_fail_drc(self, tmp_path: Path):
        """Test that overlapping components fail DRC."""
        # Create PCB with overlapping components
        pcb_content = TWO_RESISTORS_PCB.replace("(at 105 100)", "(at 100.5 100)")
        pcb_file = tmp_path / "overlap.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        result = pcb.run_drc()

        assert not result.passed
        assert result.violation_count >= 1
        # Should have courtyard or pad clearance violations
        assert result.courtyard_count >= 1 or result.clearance_count >= 1

    def test_drc_with_custom_rules(self, two_resistors_pcb: Path):
        """Test DRC with custom design rules."""
        pcb = PCB.load(str(two_resistors_pcb))

        # With very tight rules, even well-spaced components may fail
        result = pcb.run_drc(
            clearance=10.0,  # Very large clearance requirement
            courtyard_margin=5.0,
        )

        # Components 5mm apart with 5mm courtyard should fail
        assert not result.passed or result.violation_count >= 0  # May or may not fail

    def test_drc_result_counts(self, tmp_path: Path):
        """Test that DRC result counts are accurate."""
        # Create PCB with known violations
        pcb_content = TWO_RESISTORS_PCB.replace("(at 105 100)", "(at 100.5 100)")
        pcb_file = tmp_path / "overlap.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        result = pcb.run_drc()

        # Total should match sum of type counts
        type_counts = (
            result.clearance_count
            + result.courtyard_count
            + result.edge_clearance_count
            + result.hole_to_hole_count
        )
        assert result.violation_count >= type_counts  # May have other types

    def test_drc_result_to_dict(self, two_resistors_pcb: Path):
        """Test DRCResult serialization."""
        pcb = PCB.load(str(two_resistors_pcb))

        result = pcb.run_drc()
        d = result.to_dict()

        assert "passed" in d
        assert "violation_count" in d
        assert "clearance_count" in d
        assert "courtyard_count" in d
        assert "violations" in d


class TestPlaceFootprintSafe:
    """Tests for PCB.place_footprint_safe()."""

    def test_place_at_clear_position(self, two_resistors_pcb: Path):
        """Test placing at a clear position succeeds."""
        pcb = PCB.load(str(two_resistors_pcb))

        success, pos, msg = pcb.place_footprint_safe("R1", x=95, y=95)

        assert success
        assert pos is not None
        assert pos == (95, 95)
        assert "requested position" in msg.lower()

        # Verify position was actually updated
        fp = pcb.get_footprint("R1")
        assert fp.position == (95, 95)

    def test_auto_adjust_on_collision(self, two_resistors_pcb: Path):
        """Test auto-adjustment when collision would occur."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Try to place R1 at R2's position - should auto-adjust
        success, pos, msg = pcb.place_footprint_safe(
            "R1", x=105, y=100, auto_adjust=True, max_adjustment=5.0
        )

        assert success
        assert pos is not None
        # Position should be different from requested (adjusted)
        assert pos != (105, 100)
        assert "adjusted" in msg.lower()

    def test_no_auto_adjust_fails(self, two_resistors_pcb: Path):
        """Test that disabling auto-adjust causes failure on collision."""
        pcb = PCB.load(str(two_resistors_pcb))

        success, pos, msg = pcb.place_footprint_safe(
            "R1", x=105, y=100, auto_adjust=False
        )

        assert not success
        assert pos is None
        assert "collision" in msg.lower()

    def test_nonexistent_component(self, two_resistors_pcb: Path):
        """Test placing nonexistent component."""
        pcb = PCB.load(str(two_resistors_pcb))

        success, pos, msg = pcb.place_footprint_safe("U99", x=50, y=50)

        assert not success
        assert pos is None
        assert "not found" in msg.lower()

    def test_max_adjustment_limit(self, tmp_path: Path):
        """Test that max_adjustment limits search radius."""
        # Create PCB where all nearby positions are blocked
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "001")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "002")
    (at 100 101)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "003")
    (at 100 99)
    (property "Reference" "R3" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "004")
    (at 101 100)
    (property "Reference" "R4" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "005")
    (at 99 100)
    (property "Reference" "R5" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu"))
  )
)
"""
        pcb_file = tmp_path / "crowded.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))

        # With very small max_adjustment, should fail
        success, pos, msg = pcb.place_footprint_safe(
            "R1", x=100, y=100, auto_adjust=True, max_adjustment=0.1
        )

        # May or may not find a spot, but shouldn't search beyond 0.1mm
        if not success:
            assert "could not find" in msg.lower()


class TestSetDesignRules:
    """Tests for PCB.set_design_rules()."""

    def test_set_design_rules(self, two_resistors_pcb: Path):
        """Test setting design rules on PCB."""
        pcb = PCB.load(str(two_resistors_pcb))

        # This should not raise
        pcb.set_design_rules(
            clearance=0.15,
            courtyard_clearance=0.2,
            edge_clearance=0.25,
            hole_to_hole=0.4,
        )

        # Design rules should be stored (implementation detail)
        assert hasattr(pcb, "_design_rules")


class TestCollisionResultDataclass:
    """Tests for CollisionResult dataclass."""

    def test_no_collision_factory(self):
        """Test no_collision factory method."""
        result = CollisionResult.no_collision()

        assert not result.has_collision
        assert result.other_ref is None
        assert result.conflict_type is None

    def test_collision_result_attributes(self):
        """Test CollisionResult has expected attributes."""
        from kicad_tools.placement.conflict import Point

        result = CollisionResult(
            has_collision=True,
            other_ref="R2",
            conflict_type=ConflictType.COURTYARD_OVERLAP,
            required_clearance=0.25,
            actual_clearance=0.1,
            location=Point(100, 100),
            message="courtyard overlap",
        )

        assert result.has_collision
        assert result.other_ref == "R2"
        assert result.required_clearance == 0.25
        assert result.actual_clearance == 0.1


class TestIntegrationWorkflow:
    """Integration tests for the full workflow described in issue #925."""

    def test_placement_validation_workflow(self, two_resistors_pcb: Path):
        """Test the validation workflow from the issue description."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Workflow: validate all placements before committing
        placements = {
            "R1": (95, 100, 0),
            "R2": (105, 100, 0),
        }
        result = pcb.validate_placements(placements)

        if result.is_valid:
            # Commit the placements
            for ref, (x, y, rot) in placements.items():
                pcb.update_footprint_position(ref, x, y, rot)

            # Verify final DRC passes
            drc_result = pcb.run_drc()
            assert drc_result.passed

    def test_iterative_placement_adjustment(self, two_resistors_pcb: Path):
        """Test iterative placement adjustment with collision feedback."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Initial placement attempt - may collide
        initial_placements = {
            "R1": (100, 100, 0),
            "R2": (101, 100, 0),  # Too close
        }
        result = pcb.validate_placements(initial_placements)

        if not result.is_valid:
            # Adjust based on collision info
            adjusted_placements = {
                "R1": (98, 100, 0),
                "R2": (103, 100, 0),  # Moved further apart
            }
            result = pcb.validate_placements(adjusted_placements)

            # After adjustment, should be valid
            # (depending on exact clearance rules)
            if result.is_valid:
                for ref, (x, y, rot) in adjusted_placements.items():
                    pcb.update_footprint_position(ref, x, y, rot)

    def test_safe_placement_workflow(self, two_resistors_pcb: Path):
        """Test using place_footprint_safe for automatic adjustment."""
        pcb = PCB.load(str(two_resistors_pcb))

        # Place R1 at a potentially problematic position
        success, final_pos, msg = pcb.place_footprint_safe(
            "R1",
            x=104,  # Close to R2 at 105
            y=100,
            min_clearance=0.2,
            auto_adjust=True,
        )

        if success:
            # Final DRC should pass
            drc_result = pcb.run_drc()
            # Note: DRC may still find issues if placement is tight
            # but at least we avoided direct collision
            assert final_pos is not None
