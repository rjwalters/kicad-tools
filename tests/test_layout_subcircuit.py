"""Tests for kicad_tools.layout subcircuit module."""

from pathlib import Path

import pytest

from kicad_tools.layout import (
    ComponentOffset,
    SubcircuitExtractor,
    SubcircuitLayout,
    apply_subcircuit,
    rotate_point,
)

# Test PCB with multiple components simulating a subcircuit (LDO regulator)
SUBCIRCUIT_PCB = """(kicad_pcb
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
  (net 1 "VIN")
  (net 2 "VOUT")
  (net 3 "GND")
  (footprint "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100 0)
    (property "Reference" "U1" (at 0 -3.5 0) (layer "F.SilkS"))
    (property "Value" "LM1117" (at 0 3.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at -2.3 0) (size 1.5 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "GND"))
    (pad "2" smd rect (at 0 0) (size 1.5 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "VOUT"))
    (pad "3" smd rect (at 2.3 0) (size 1.5 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VIN"))
  )
  (footprint "Capacitor_SMD:C_0805_2012Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 97 103 90)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10uF" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.95 0) (size 1.0 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VIN"))
    (pad "2" smd roundrect (at 0.95 0) (size 1.0 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "GND"))
  )
  (footprint "Capacitor_SMD:C_0805_2012Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000003")
    (at 103 103 90)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "22uF" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.95 0) (size 1.0 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "VOUT"))
    (pad "2" smd roundrect (at 0.95 0) (size 1.0 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000004")
    (at 105 98 0)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "VOUT"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "GND"))
  )
)
"""


@pytest.fixture
def subcircuit_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with subcircuit components."""
    pcb_file = tmp_path / "subcircuit.kicad_pcb"
    pcb_file.write_text(SUBCIRCUIT_PCB)
    return pcb_file


class TestComponentOffset:
    """Tests for ComponentOffset data class."""

    def test_basic_creation(self):
        """Test creating a ComponentOffset."""
        offset = ComponentOffset(ref="C1", dx=2.0, dy=-3.0, rotation_delta=90.0)
        assert offset.ref == "C1"
        assert offset.dx == 2.0
        assert offset.dy == -3.0
        assert offset.rotation_delta == 90.0

    def test_default_rotation(self):
        """Test default rotation delta is zero."""
        offset = ComponentOffset(ref="C1", dx=1.0, dy=1.0)
        assert offset.rotation_delta == 0.0

    def test_rotated_zero_degrees(self):
        """Test rotation by zero degrees returns original offset."""
        offset = ComponentOffset(ref="C1", dx=3.0, dy=4.0)
        rx, ry = offset.rotated(0.0)
        assert rx == pytest.approx(3.0)
        assert ry == pytest.approx(4.0)

    def test_rotated_90_degrees(self):
        """Test rotation by 90 degrees."""
        offset = ComponentOffset(ref="C1", dx=3.0, dy=0.0)
        rx, ry = offset.rotated(90.0)
        assert rx == pytest.approx(0.0, abs=0.001)
        assert ry == pytest.approx(3.0, abs=0.001)

    def test_rotated_180_degrees(self):
        """Test rotation by 180 degrees."""
        offset = ComponentOffset(ref="C1", dx=3.0, dy=4.0)
        rx, ry = offset.rotated(180.0)
        assert rx == pytest.approx(-3.0, abs=0.001)
        assert ry == pytest.approx(-4.0, abs=0.001)

    def test_rotated_270_degrees(self):
        """Test rotation by 270 degrees."""
        offset = ComponentOffset(ref="C1", dx=3.0, dy=0.0)
        rx, ry = offset.rotated(270.0)
        assert rx == pytest.approx(0.0, abs=0.001)
        assert ry == pytest.approx(-3.0, abs=0.001)


class TestSubcircuitLayout:
    """Tests for SubcircuitLayout data class."""

    def test_basic_creation(self):
        """Test creating a SubcircuitLayout."""
        layout = SubcircuitLayout(
            path="power.ldo",
            anchor_ref="U1",
            anchor_position=(100.0, 100.0, 0.0),
        )
        assert layout.path == "power.ldo"
        assert layout.anchor_ref == "U1"
        assert layout.anchor_position == (100.0, 100.0, 0.0)
        assert layout.offsets == {}

    def test_component_count(self):
        """Test component_count property."""
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(0.0, 0.0, 0.0),
            offsets={
                "C1": ComponentOffset("C1", 1.0, 2.0),
                "C2": ComponentOffset("C2", -1.0, 2.0),
            },
        )
        assert layout.component_count == 3  # anchor + 2 offsets

    def test_component_refs(self):
        """Test component_refs property."""
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(0.0, 0.0, 0.0),
            offsets={
                "C1": ComponentOffset("C1", 1.0, 2.0),
                "R1": ComponentOffset("R1", -1.0, 2.0),
            },
        )
        refs = layout.component_refs
        assert "U1" in refs
        assert "C1" in refs
        assert "R1" in refs
        assert len(refs) == 3

    def test_get_position_anchor(self):
        """Test get_position for anchor component."""
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(50.0, 60.0, 45.0),
        )
        pos = layout.get_position("U1")
        assert pos == (50.0, 60.0, 45.0)

    def test_get_position_offset_no_rotation(self):
        """Test get_position for offset component with no anchor rotation."""
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(100.0, 100.0, 0.0),
            offsets={
                "C1": ComponentOffset("C1", 5.0, 3.0, 90.0),
            },
        )
        pos = layout.get_position("C1")
        assert pos is not None
        x, y, rot = pos
        assert x == pytest.approx(105.0)
        assert y == pytest.approx(103.0)
        assert rot == pytest.approx(90.0)

    def test_get_position_offset_with_rotation(self):
        """Test get_position for offset component with anchor rotation."""
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(100.0, 100.0, 90.0),
            offsets={
                "C1": ComponentOffset("C1", 5.0, 0.0, 0.0),
            },
        )
        pos = layout.get_position("C1")
        assert pos is not None
        x, y, rot = pos
        # Original offset (5, 0) rotated 90 degrees becomes (0, 5)
        assert x == pytest.approx(100.0, abs=0.001)
        assert y == pytest.approx(105.0, abs=0.001)
        assert rot == pytest.approx(90.0)

    def test_get_position_not_found(self):
        """Test get_position for non-existent component."""
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(0.0, 0.0, 0.0),
        )
        pos = layout.get_position("NONEXISTENT")
        assert pos is None

    def test_with_anchor_position(self):
        """Test creating layout with new anchor position."""
        original = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(100.0, 100.0, 0.0),
            offsets={
                "C1": ComponentOffset("C1", 5.0, 3.0),
            },
        )
        new_layout = original.with_anchor_position((200.0, 150.0, 90.0))

        assert new_layout.anchor_position == (200.0, 150.0, 90.0)
        assert new_layout.path == original.path
        assert new_layout.anchor_ref == original.anchor_ref
        assert "C1" in new_layout.offsets

        # Original should be unchanged
        assert original.anchor_position == (100.0, 100.0, 0.0)

    def test_get_all_positions(self):
        """Test get_all_positions method."""
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(100.0, 100.0, 0.0),
            offsets={
                "C1": ComponentOffset("C1", 5.0, 3.0, 90.0),
                "C2": ComponentOffset("C2", -5.0, 3.0, 90.0),
            },
        )
        positions = layout.get_all_positions()

        assert len(positions) == 3
        assert "U1" in positions
        assert "C1" in positions
        assert "C2" in positions

        assert positions["U1"] == (100.0, 100.0, 0.0)
        assert positions["C1"][0] == pytest.approx(105.0)
        assert positions["C2"][0] == pytest.approx(95.0)


class TestSubcircuitExtractor:
    """Tests for SubcircuitExtractor class."""

    def test_extract_basic(self, subcircuit_pcb: Path):
        """Test basic extraction of subcircuit layout."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1", "C2", "R1"],
            subcircuit_path="power.ldo",
        )

        assert layout.path == "power.ldo"
        assert layout.anchor_ref == "U1"  # IC should be selected as anchor
        assert layout.component_count == 4
        assert "C1" in layout.offsets
        assert "C2" in layout.offsets
        assert "R1" in layout.offsets

    def test_extract_anchor_is_ic(self, subcircuit_pcb: Path):
        """Test that IC is selected as anchor over passives."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["C1", "U1", "R1"],  # U1 not first in list
            subcircuit_path="test",
        )

        assert layout.anchor_ref == "U1"

    def test_extract_correct_offsets(self, subcircuit_pcb: Path):
        """Test that offsets are calculated correctly."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1"],
            subcircuit_path="test",
        )

        # U1 is at (100, 100), C1 is at (97, 103)
        c1_offset = layout.offsets["C1"]
        assert c1_offset.dx == pytest.approx(-3.0)  # 97 - 100
        assert c1_offset.dy == pytest.approx(3.0)  # 103 - 100
        assert c1_offset.rotation_delta == pytest.approx(90.0)  # C1 has 90 deg rotation

    def test_extract_empty_refs_error(self, subcircuit_pcb: Path):
        """Test that empty refs list raises error."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        with pytest.raises(ValueError, match="cannot be empty"):
            extractor.extract(pcb, component_refs=[], subcircuit_path="test")

    def test_extract_no_matching_refs_error(self, subcircuit_pcb: Path):
        """Test that no matching refs raises error."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        with pytest.raises(ValueError, match="No components found"):
            extractor.extract(
                pcb,
                component_refs=["NONEXISTENT1", "NONEXISTENT2"],
                subcircuit_path="test",
            )

    def test_extract_by_pattern(self, subcircuit_pcb: Path):
        """Test extraction by regex pattern."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        # Match all capacitors
        layout = extractor.extract_by_pattern(
            pcb,
            pattern=r"C\d+",
            subcircuit_path="caps",
        )

        assert layout.component_count == 2
        assert layout.anchor_ref in ["C1", "C2"]

    def test_extract_single_component(self, subcircuit_pcb: Path):
        """Test extraction with single component."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["U1"],
            subcircuit_path="single",
        )

        assert layout.anchor_ref == "U1"
        assert layout.component_count == 1
        assert len(layout.offsets) == 0

    def test_custom_anchor_selector(self, subcircuit_pcb: Path):
        """Test using a custom anchor selector."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))

        # Custom selector that always picks the first component
        def custom_selector(components):
            return components[0]

        extractor = SubcircuitExtractor(anchor_selector=custom_selector)

        layout = extractor.extract(
            pcb,
            component_refs=["R1", "U1", "C1"],  # R1 is first
            subcircuit_path="test",
        )

        assert layout.anchor_ref == "R1"


class TestApplySubcircuit:
    """Tests for apply_subcircuit function."""

    def test_apply_no_rotation(self, subcircuit_pcb: Path):
        """Test applying layout without rotation."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        # Extract layout
        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1"],
            subcircuit_path="test",
        )

        # Apply to new position (offset by 50mm in X)
        new_positions = apply_subcircuit(
            pcb,
            layout,
            new_anchor_position=(150.0, 100.0, 0.0),
        )

        # U1 should be at new position
        assert new_positions["U1"] == (150.0, 100.0, 0.0)

        # C1 should maintain relative offset
        # Original: U1 at (100, 100), C1 at (97, 103) -> offset (-3, 3)
        # New: U1 at (150, 100), C1 should be at (147, 103)
        c1_x, c1_y, c1_rot = new_positions["C1"]
        assert c1_x == pytest.approx(147.0)
        assert c1_y == pytest.approx(103.0)

    def test_apply_with_90_rotation(self, subcircuit_pcb: Path):
        """Test applying layout with 90 degree rotation."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        # Extract layout
        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1"],
            subcircuit_path="test",
        )

        # Apply to new position with 90 degree rotation
        new_positions = apply_subcircuit(
            pcb,
            layout,
            new_anchor_position=(100.0, 100.0, 90.0),
        )

        # U1 should be at new position with rotation
        assert new_positions["U1"] == (100.0, 100.0, 90.0)

        # C1 offset (-3, 3) rotated 90 degrees becomes (-3, -3)
        c1_x, c1_y, c1_rot = new_positions["C1"]
        assert c1_x == pytest.approx(97.0, abs=0.01)  # 100 - 3
        assert c1_y == pytest.approx(97.0, abs=0.01)  # 100 - 3

    def test_apply_with_ref_mapping(self, subcircuit_pcb: Path, tmp_path: Path):
        """Test applying layout with reference remapping."""
        from kicad_tools.schema import PCB

        # Create a PCB with components that match remapped refs
        pcb_content = SUBCIRCUIT_PCB.replace('"U1"', '"U10"').replace('"C1"', '"C10"')
        pcb_file = tmp_path / "remapped.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))

        # Create a layout with original refs
        layout = SubcircuitLayout(
            path="test",
            anchor_ref="U1",
            anchor_position=(100.0, 100.0, 0.0),
            offsets={
                "C1": ComponentOffset("C1", -3.0, 3.0, 90.0),
            },
        )

        # Apply with remapping
        new_positions = apply_subcircuit(
            pcb,
            layout,
            new_anchor_position=(150.0, 150.0, 0.0),
            ref_mapping={"U1": "U10", "C1": "C10"},
        )

        # Should have remapped refs in output
        assert "U10" in new_positions
        assert "C10" in new_positions
        assert "U1" not in new_positions
        assert "C1" not in new_positions

    def test_apply_updates_pcb(self, subcircuit_pcb: Path, tmp_path: Path):
        """Test that apply_subcircuit actually updates PCB positions."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1"],
            subcircuit_path="test",
        )

        # Apply to new position
        apply_subcircuit(
            pcb,
            layout,
            new_anchor_position=(200.0, 200.0, 0.0),
        )

        # Save and reload to verify
        output_file = tmp_path / "updated.kicad_pcb"
        pcb.save(str(output_file))

        reloaded = PCB.load(str(output_file))
        u1 = reloaded.get_footprint("U1")
        assert u1 is not None
        assert u1.position[0] == pytest.approx(200.0)
        assert u1.position[1] == pytest.approx(200.0)


class TestRotatePoint:
    """Tests for rotate_point utility function."""

    def test_zero_rotation(self):
        """Test zero rotation returns same point."""
        x, y = rotate_point(5.0, 3.0, 0.0)
        assert x == pytest.approx(5.0)
        assert y == pytest.approx(3.0)

    def test_90_rotation(self):
        """Test 90 degree rotation."""
        x, y = rotate_point(5.0, 0.0, 90.0)
        assert x == pytest.approx(0.0, abs=0.001)
        assert y == pytest.approx(5.0, abs=0.001)

    def test_180_rotation(self):
        """Test 180 degree rotation."""
        x, y = rotate_point(5.0, 3.0, 180.0)
        assert x == pytest.approx(-5.0, abs=0.001)
        assert y == pytest.approx(-3.0, abs=0.001)

    def test_rotation_with_origin(self):
        """Test rotation around custom origin."""
        # Point (15, 10) rotated 90 degrees around (10, 10)
        x, y = rotate_point(15.0, 10.0, 90.0, origin_x=10.0, origin_y=10.0)
        assert x == pytest.approx(10.0, abs=0.001)
        assert y == pytest.approx(15.0, abs=0.001)


class TestSubcircuitRotations:
    """Tests for subcircuit rotation at different angles."""

    def test_subcircuit_180_rotation(self, subcircuit_pcb: Path):
        """Test subcircuit rotation by 180 degrees."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1"],
            subcircuit_path="test",
        )

        # Apply with 180 degree rotation
        new_positions = apply_subcircuit(
            pcb,
            layout,
            new_anchor_position=(100.0, 100.0, 180.0),
        )

        # C1 offset (-3, 3) rotated 180 degrees becomes (3, -3)
        c1_x, c1_y, _ = new_positions["C1"]
        assert c1_x == pytest.approx(103.0, abs=0.01)  # 100 + 3
        assert c1_y == pytest.approx(97.0, abs=0.01)  # 100 - 3

    def test_subcircuit_270_rotation(self, subcircuit_pcb: Path):
        """Test subcircuit rotation by 270 degrees."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1"],
            subcircuit_path="test",
        )

        # Apply with 270 degree rotation
        new_positions = apply_subcircuit(
            pcb,
            layout,
            new_anchor_position=(100.0, 100.0, 270.0),
        )

        # C1 offset (-3, 3) rotated 270 degrees becomes (3, 3)
        c1_x, c1_y, _ = new_positions["C1"]
        assert c1_x == pytest.approx(103.0, abs=0.01)  # 100 + 3
        assert c1_y == pytest.approx(103.0, abs=0.01)  # 100 + 3


class TestMultipleInstances:
    """Tests for multiple instances of the same subcircuit."""

    def test_multiple_instances_different_positions(self, subcircuit_pcb: Path):
        """Test applying same layout to multiple positions."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(subcircuit_pcb))
        extractor = SubcircuitExtractor()

        layout = extractor.extract(
            pcb,
            component_refs=["U1", "C1"],
            subcircuit_path="test",
        )

        # First instance at original position
        positions1 = apply_subcircuit(
            pcb,
            layout,
            new_anchor_position=(100.0, 100.0, 0.0),
        )

        # Create a copy of layout for second instance
        # (In real usage, would use different ref_mapping)
        positions2 = layout.with_anchor_position((200.0, 100.0, 0.0)).get_all_positions()

        # Verify relative spacing is preserved
        u1_c1_dx_1 = positions1["C1"][0] - positions1["U1"][0]
        u1_c1_dy_1 = positions1["C1"][1] - positions1["U1"][1]

        u1_c1_dx_2 = positions2["C1"][0] - positions2["U1"][0]
        u1_c1_dy_2 = positions2["C1"][1] - positions2["U1"][1]

        assert u1_c1_dx_1 == pytest.approx(u1_c1_dx_2)
        assert u1_c1_dy_1 == pytest.approx(u1_c1_dy_2)
