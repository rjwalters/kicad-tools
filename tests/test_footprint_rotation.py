"""Tests for footprint rotation handling in router and validation.

Verifies that pad positions are correctly transformed when footprints
are rotated (issue #727), and that pad dimensions are swapped to PCB
space when the total rotation is 90 or 270 degrees (issue #2400).
"""

import math

import pytest


class TestPadPositionRotation:
    """Tests for pad position rotation transformation."""

    def test_router_io_pad_rotation_90_degrees(self):
        """Test that router correctly transforms pad position with 90° rotation."""
        # Simulate the transformation from router/io.py
        fp_x, fp_y = 112.5, 110.0
        fp_rot = 90  # degrees
        pad_x, pad_y = -1.0, 0  # local pad position

        # Apply rotation (fixed: no negation)
        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # Expected: pad at (-1, 0) rotated 90° CCW becomes (0, -1)
        # So absolute position should be (112.5, 109.0)
        assert abs_x == pytest.approx(112.5, abs=0.001)
        assert abs_y == pytest.approx(109.0, abs=0.001)

    def test_router_io_pad_rotation_180_degrees(self):
        """Test pad position with 180° rotation."""
        fp_x, fp_y = 100.0, 100.0
        fp_rot = 180
        pad_x, pad_y = 1.0, 0.5

        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # Pad at (1, 0.5) rotated 180° becomes (-1, -0.5)
        assert abs_x == pytest.approx(99.0, abs=0.001)
        assert abs_y == pytest.approx(99.5, abs=0.001)

    def test_router_io_pad_rotation_270_degrees(self):
        """Test pad position with 270° rotation."""
        fp_x, fp_y = 100.0, 100.0
        fp_rot = 270
        pad_x, pad_y = 1.0, 0

        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # Pad at (1, 0) rotated 270° CCW (or 90° CW) becomes (0, 1)
        assert abs_x == pytest.approx(100.0, abs=0.001)
        assert abs_y == pytest.approx(99.0, abs=0.001)

    def test_router_io_pad_rotation_0_degrees(self):
        """Test pad position with no rotation."""
        fp_x, fp_y = 100.0, 100.0
        fp_rot = 0
        pad_x, pad_y = 2.0, 1.0

        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # No rotation - pad position is simply offset
        assert abs_x == pytest.approx(102.0, abs=0.001)
        assert abs_y == pytest.approx(101.0, abs=0.001)

    def test_router_io_pad_rotation_45_degrees(self):
        """Test pad position with 45° rotation."""
        fp_x, fp_y = 100.0, 100.0
        fp_rot = 45
        pad_x, pad_y = 1.0, 0

        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # Pad at (1, 0) rotated 45° becomes (cos45, sin45) ≈ (0.707, 0.707)
        sqrt2_2 = math.sqrt(2) / 2
        assert abs_x == pytest.approx(100.0 + sqrt2_2, abs=0.001)
        assert abs_y == pytest.approx(100.0 + sqrt2_2, abs=0.001)


class TestConnectivityValidationRotation:
    """Tests for connectivity validation rotation handling."""

    def test_connectivity_pad_rotation(self):
        """Test that connectivity validation uses correct rotation."""
        from kicad_tools.validate.connectivity import ConnectivityValidator

        validator = ConnectivityValidator.__new__(ConnectivityValidator)

        # Test the _transform_pad_position method
        pad_local = (-1.0, 0)
        fp_x, fp_y = 112.5, 110.0
        rotation = 90

        board_x, board_y = validator._transform_pad_position(pad_local, fp_x, fp_y, rotation)

        # Expected: (112.5, 109.0)
        assert board_x == pytest.approx(112.5, abs=0.001)
        assert board_y == pytest.approx(109.0, abs=0.001)


class TestClearanceValidationRotation:
    """Tests for clearance validation rotation handling."""

    def test_clearance_pad_position_transform(self):
        """Test that clearance validation transforms pad positions correctly."""
        from dataclasses import dataclass

        from kicad_tools.validate.rules.clearance import _transform_pad_position

        @dataclass
        class MockPad:
            position: tuple[float, float]

        @dataclass
        class MockFootprint:
            position: tuple[float, float]
            rotation: float

        pad = MockPad(position=(-1.0, 0))
        footprint = MockFootprint(position=(112.5, 110.0), rotation=90)

        abs_x, abs_y = _transform_pad_position(pad, footprint)

        # Expected: (112.5, 109.0)
        assert abs_x == pytest.approx(112.5, abs=0.001)
        assert abs_y == pytest.approx(109.0, abs=0.001)


def _make_pcb_text(fp_rotation: float, pad_rotation: float | None = None) -> str:
    """Build a minimal KiCad PCB S-expression for testing pad dimension rotation.

    The pad has local-frame size 1.475 x 0.400 (asymmetric so we can detect swaps).

    Args:
        fp_rotation: Footprint rotation in degrees.
        pad_rotation: Optional pad-local rotation in degrees.  When None the
            pad ``(at ...)`` omits the rotation field.
    """
    fp_at = f"(at 100 100 {fp_rotation})"
    if pad_rotation is not None:
        pad_at = f"(at -1.0 0 {pad_rotation})"
    else:
        pad_at = "(at -1.0 0)"
    return (
        "(kicad_pcb (version 20221018)\n"
        f'  (footprint "TestPkg" {fp_at}\n'
        '    (fp_text reference "U1" (at 0 0))\n'
        f'    (pad "1" smd rect {pad_at} (size 1.475 0.400)\n'
        '      (layers "F.Cu") (net 1 "VCC"))\n'
        "  )\n"
        ")"
    )


class TestPadDimensionRotation:
    """Tests that pad width/height are swapped to PCB space at 90/270 degrees.

    The fix (commit a3122259) swaps width and height when the total rotation
    (footprint + pad) is approximately 90 or 270 degrees.  At 0 and 180 degrees
    the dimensions remain unchanged.
    """

    def test_0_degree_rotation_no_swap(self):
        """At 0-degree rotation, pad dimensions stay as defined (1.475 x 0.400)."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(0))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(1.475, abs=0.001)
        assert pads[0].height == pytest.approx(0.400, abs=0.001)

    def test_90_degree_rotation_swaps_dimensions(self):
        """At 90-degree rotation, width and height should be swapped."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(90))
        assert len(pads) == 1
        # Swapped: original 1.475 x 0.400 becomes 0.400 x 1.475
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_180_degree_rotation_no_swap(self):
        """At 180-degree rotation, dimensions stay unchanged."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(180))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(1.475, abs=0.001)
        assert pads[0].height == pytest.approx(0.400, abs=0.001)

    def test_270_degree_rotation_swaps_dimensions(self):
        """At 270-degree rotation, width and height should be swapped."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(270))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_negative_90_degree_rotation_swaps_dimensions(self):
        """At -90-degree rotation (equivalent to 270), dimensions should swap."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(-90))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_combined_fp_and_pad_rotation_totaling_90(self):
        """When footprint (45) + pad (45) = 90 total, dimensions should swap."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(45, pad_rotation=45))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_combined_fp_and_pad_rotation_totaling_270(self):
        """When footprint (180) + pad (90) = 270 total, dimensions should swap."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(180, pad_rotation=90))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_combined_fp_and_pad_rotation_totaling_180_no_swap(self):
        """When footprint (90) + pad (90) = 180 total, dimensions stay unchanged."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(90, pad_rotation=90))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(1.475, abs=0.001)
        assert pads[0].height == pytest.approx(0.400, abs=0.001)
