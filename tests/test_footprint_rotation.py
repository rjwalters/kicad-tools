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

        # KiCad applies the footprint orientation as a NEGATED angle (#3739).
        rot_rad = math.radians(-fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # KiCad: pad at (-1, 0) under a +90° footprint lands at local->world
        # offset (0, +1), so absolute position is (112.5, 111.0).
        assert abs_x == pytest.approx(112.5, abs=0.001)
        assert abs_y == pytest.approx(111.0, abs=0.001)

    def test_router_io_pad_rotation_180_degrees(self):
        """Test pad position with 180° rotation."""
        fp_x, fp_y = 100.0, 100.0
        fp_rot = 180
        pad_x, pad_y = 1.0, 0.5

        rot_rad = math.radians(-fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # Pad at (1, 0.5) rotated 180° becomes (-1, -0.5) under both
        # conventions (180° is sign-blind).
        assert abs_x == pytest.approx(99.0, abs=0.001)
        assert abs_y == pytest.approx(99.5, abs=0.001)

    def test_router_io_pad_rotation_270_degrees(self):
        """Test pad position with 270° rotation."""
        fp_x, fp_y = 100.0, 100.0
        fp_rot = 270
        pad_x, pad_y = 1.0, 0

        rot_rad = math.radians(-fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # KiCad: pad at (1, 0) under a +270° footprint lands at offset (0, +1),
        # so absolute position is (100.0, 101.0).
        assert abs_x == pytest.approx(100.0, abs=0.001)
        assert abs_y == pytest.approx(101.0, abs=0.001)

    def test_router_io_pad_rotation_0_degrees(self):
        """Test pad position with no rotation."""
        fp_x, fp_y = 100.0, 100.0
        fp_rot = 0
        pad_x, pad_y = 2.0, 1.0

        rot_rad = math.radians(-fp_rot)
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

        rot_rad = math.radians(-fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
        abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

        # KiCad (negated angle): pad at (1, 0) under +45° lands at
        # (cos(-45), sin(-45)) ≈ (0.707, -0.707).
        sqrt2_2 = math.sqrt(2) / 2
        assert abs_x == pytest.approx(100.0 + sqrt2_2, abs=0.001)
        assert abs_y == pytest.approx(100.0 - sqrt2_2, abs=0.001)


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

        # KiCad negated-angle convention (#3739): (112.5, 111.0)
        assert board_x == pytest.approx(112.5, abs=0.001)
        assert board_y == pytest.approx(111.0, abs=0.001)


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

        # KiCad negated-angle convention (#3739): (112.5, 111.0)
        assert abs_x == pytest.approx(112.5, abs=0.001)
        assert abs_y == pytest.approx(111.0, abs=0.001)


def _make_pcb_text(fp_rotation: float, pad_rotation: float | None = None) -> str:
    """Build a minimal KiCad PCB S-expression for testing pad dimension rotation.

    The pad has local-frame size 1.475 x 0.400 (asymmetric so we can detect swaps).

    Args:
        fp_rotation: Footprint rotation in degrees.
        pad_rotation: Optional pad ABSOLUTE angle in degrees.  KiCad stores each
            pad's ``(at x y ANGLE)`` in the ABSOLUTE board frame -- the angle
            already includes the footprint rotation (issue #3902).  When None the
            pad ``(at ...)`` omits the rotation field, which KiCad reads as an
            absolute angle of 0 (i.e. an UNROTATED pad, regardless of the
            footprint's own rotation).
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

    The dimension-swap is driven by the pad's ABSOLUTE angle (the third token of
    the pad ``(at ...)``), which KiCad stores already including the footprint
    rotation (issue #3902).  The consumer must therefore read the stored angle
    directly and must NOT add the footprint rotation on top of it.  At an
    absolute 90/270 degrees the axes swap; at 0/180 they stay unchanged.

    Historical note: these tests previously encoded the buggy LOCAL convention
    (``total = fp_rotation + pad_rotation``).  They were updated for #3902 so a
    footprint placed at 90 degrees emits an absolute pad angle of 90.
    """

    def test_0_degree_rotation_no_swap(self):
        """Absolute pad angle 0, dimensions stay as defined (1.475 x 0.400)."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(0, pad_rotation=0))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(1.475, abs=0.001)
        assert pads[0].height == pytest.approx(0.400, abs=0.001)

    def test_90_degree_rotation_swaps_dimensions(self):
        """Absolute pad angle 90 (fp placed at 90), width/height swapped."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(90, pad_rotation=90))
        assert len(pads) == 1
        # Swapped: original 1.475 x 0.400 becomes 0.400 x 1.475
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_180_degree_rotation_no_swap(self):
        """Absolute pad angle 180, dimensions stay unchanged."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(180, pad_rotation=180))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(1.475, abs=0.001)
        assert pads[0].height == pytest.approx(0.400, abs=0.001)

    def test_270_degree_rotation_swaps_dimensions(self):
        """Absolute pad angle 270, width/height swapped."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(270, pad_rotation=270))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_absent_pad_angle_under_rotated_footprint_no_swap(self):
        """A missing pad angle reads as absolute 0 -> UNROTATED pad, no swap.

        Even though the footprint itself is placed at 90 degrees, an absent pad
        angle means the pad shape is unrotated in the board frame (absolute 0).
        This is the exact bug #3902 guards against: the pad dimensions must NOT
        be swapped just because the footprint is rotated.
        """
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(90))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(1.475, abs=0.001)
        assert pads[0].height == pytest.approx(0.400, abs=0.001)

    def test_negative_90_degree_rotation_swaps_dimensions(self):
        """Absolute pad angle -90 (equivalent to 270), dimensions swap."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(-90, pad_rotation=-90))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_absolute_pad_angle_90_swaps(self):
        """Absolute pad angle of 90 swaps dimensions regardless of fp rotation.

        The footprint here is placed at 45 but the pad carries an absolute angle
        of 90; the swap is decided by the pad angle alone (not fp + pad).
        """
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(45, pad_rotation=90))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_absolute_pad_angle_270_swaps(self):
        """Absolute pad angle of 270 swaps dimensions (fp rotation irrelevant)."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(180, pad_rotation=270))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(0.400, abs=0.001)
        assert pads[0].height == pytest.approx(1.475, abs=0.001)

    def test_absolute_pad_angle_180_no_swap(self):
        """Absolute pad angle of 180 keeps dimensions unchanged."""
        from kicad_tools.router.io import load_pads_for_analysis

        pads = load_pads_for_analysis(_make_pcb_text(90, pad_rotation=180))
        assert len(pads) == 1
        assert pads[0].width == pytest.approx(1.475, abs=0.001)
        assert pads[0].height == pytest.approx(0.400, abs=0.001)
