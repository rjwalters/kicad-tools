"""Tests for footprint rotation handling in router and validation.

Verifies that pad positions are correctly transformed when footprints
are rotated (issue #727).
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
        from kicad_tools.validate.rules.clearance import _transform_pad_position
        from dataclasses import dataclass

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
