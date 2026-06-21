"""Unit tests for true-geometry pad polygons and pad-pad clearance (Issue #3826).

``src/kicad_tools/validate/rules/clearance.py`` previously modeled every
pad as an axis-aligned bounding box, over-approximating the rounded
corners of ``roundrect``/``oval`` pads.  When such a corner sat next to a
foreign-net zone fill (or another pad) at the design minimum clearance,
the phantom corner triangle produced a sub-10-micron "overlap" that
KiCad's true rounded geometry never sees -- a false-positive
``clearance_pad_zone`` / ``clearance_pad_pad`` short.

These tests cover the shared ``_pad_polygon`` helper (exact outline per
shape + rotation) and the pad-pad clearance path that now consumes it.
"""

from __future__ import annotations

import math

from kicad_tools.schema.pcb import Footprint, Pad
from kicad_tools.validate.rules.clearance import (
    CopperElement,
    _calculate_clearance,
    _pad_polygon,
)


def _fp(position=(0.0, 0.0), rotation=0.0) -> Footprint:
    return Footprint(
        name="fp",
        reference="U1",
        value="",
        position=position,
        rotation=rotation,
        layer="F.Cu",
    )


def _pad(shape, size, position=(0.0, 0.0), rotation=0.0, rratio=0.25) -> Pad:
    return Pad(
        number="1",
        type="smd",
        shape=shape,
        position=position,
        size=size,
        layers=["F.Cu"],
        net_number=1,
        net_name="SIG",
        rotation=rotation,
        roundrect_rratio=rratio,
    )


class TestPadPolygonShapes:
    def test_rect_pad_equals_exact_box(self):
        poly = _pad_polygon(_pad("rect", (1.0, 2.0)), _fp())
        assert abs(poly.area - 2.0) < 1e-9

    def test_roundrect_area_strictly_below_aabb(self):
        poly = _pad_polygon(_pad("roundrect", (1.0, 2.0)), _fp())
        aabb = 1.0 * 2.0
        # Corner triangles are removed -> area must be less than the AABB.
        assert poly.area < aabb
        # ...but not radically smaller (only the four rounded corners).
        assert poly.area > aabb * 0.9

    def test_circle_pad_matches_disc(self):
        poly = _pad_polygon(_pad("circle", (2.0, 2.0)), _fp())
        assert abs(poly.area - math.pi * 1.0**2) < 0.01

    def test_oval_pad_is_stadium_between_circle_and_box(self):
        poly = _pad_polygon(_pad("oval", (3.0, 1.0)), _fp())
        # Stadium = core rect (w-2r) x h + circle of radius r ; r = 0.5
        expected = (3.0 - 1.0) * 1.0 + math.pi * 0.5**2
        assert abs(poly.area - expected) < 0.01
        # Bounded by inscribed circle (area) and bounding rectangle.
        assert math.pi * 0.5**2 < poly.area < 3.0 * 1.0

    def test_oval_with_equal_axes_degenerates_to_circle(self):
        poly = _pad_polygon(_pad("oval", (2.0, 2.0)), _fp())
        assert abs(poly.area - math.pi) < 0.01

    def test_default_rratio_used_when_unset(self):
        # A bare roundrect Pad (rratio default 0.25) rounds its corners.
        pad = Pad(
            number="1",
            type="smd",
            shape="roundrect",
            position=(0.0, 0.0),
            size=(2.0, 2.0),
            layers=["F.Cu"],
        )
        assert pad.roundrect_rratio == 0.25
        assert _pad_polygon(pad, _fp()).area < 4.0

    def test_zero_size_returns_none(self):
        assert _pad_polygon(_pad("rect", (0.0, 1.0)), _fp()) is None


class TestPadPolygonRotation:
    def test_footprint_rotation_90_swaps_extents(self):
        poly = _pad_polygon(_pad("rect", (4.0, 1.0)), _fp(rotation=90.0))
        minx, miny, maxx, maxy = poly.bounds
        assert abs((maxy - miny) - 4.0) < 1e-6
        assert abs((maxx - minx) - 1.0) < 1e-6

    def test_per_pad_rotation_adds_to_footprint_rotation(self):
        # footprint 45deg + pad 45deg = 90deg total -> extents swap.
        poly = _pad_polygon(_pad("rect", (4.0, 1.0), rotation=45.0), _fp(rotation=45.0))
        minx, miny, maxx, maxy = poly.bounds
        assert abs((maxy - miny) - 4.0) < 1e-5
        assert abs((maxx - minx) - 1.0) < 1e-5

    def test_translation_to_board_position(self):
        poly = _pad_polygon(_pad("rect", (1.0, 1.0)), _fp(position=(10.0, 20.0)))
        assert abs(poly.centroid.x - 10.0) < 1e-9
        assert abs(poly.centroid.y - 20.0) < 1e-9


class TestPadPadClearance:
    """Pad-vs-pad clearance now uses true polygons (Issue #3826)."""

    def _elem(self, shape, size, position):
        fp = _fp(position=position)
        return CopperElement.from_pad(_pad(shape, size), fp)

    def test_roundrect_corner_clears_no_false_overlap(self):
        # Two 1x1 roundrect pads whose AABB corners just touch diagonally.
        # Sharp-corner AABBs would touch (clearance ~0); the rounded
        # corners leave a real positive gap, so clearance must be > 0.
        a = self._elem("roundrect", (1.0, 1.0), (0.0, 0.0))
        b = self._elem("roundrect", (1.0, 1.0), (1.0, 1.0))
        clearance, _, _ = _calculate_clearance(a, b)
        assert clearance > 0.0, "rounded corners must leave a positive gap"

    def test_rect_corner_touch_is_zero_clearance(self):
        # Control: sharp rect corners diagonally adjacent -> ~0 clearance.
        a = self._elem("rect", (1.0, 1.0), (0.0, 0.0))
        b = self._elem("rect", (1.0, 1.0), (1.0, 1.0))
        clearance, _, _ = _calculate_clearance(a, b)
        assert abs(clearance) < 1e-6

    def test_real_body_overlap_still_negative(self):
        # Two pads that genuinely overlap in the body must report a
        # negative (overlapping) clearance.
        a = self._elem("roundrect", (2.0, 2.0), (0.0, 0.0))
        b = self._elem("roundrect", (2.0, 2.0), (1.0, 0.0))
        clearance, _, _ = _calculate_clearance(a, b)
        assert clearance < 0.0, "real body overlap must remain a short"

    def test_separated_pads_positive_clearance(self):
        a = self._elem("roundrect", (1.0, 1.0), (0.0, 0.0))
        b = self._elem("roundrect", (1.0, 1.0), (5.0, 0.0))
        clearance, _, _ = _calculate_clearance(a, b)
        # Edge-to-edge: centers 5mm apart, half-widths 0.5 each -> ~4.0mm.
        assert abs(clearance - 4.0) < 1e-3
