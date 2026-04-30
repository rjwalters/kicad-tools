"""Tests for Shapely-based BoardGeometry engine.

Tests the core geometry operations: polygon construction, containment,
buffer, intersection, difference, and backward-compatibility helpers.
"""

from __future__ import annotations

import math

import pytest

# Guard: skip entire module if Shapely is not installed
shapely = pytest.importorskip("shapely", reason="shapely not installed")

from kicad_tools.pcb.board_geometry import (
    BoardGeometry,
    _chain_segments,
    _linearize_arc,
    _linearize_bezier,
    has_shapely,
)


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _rect_geometry(
    min_x: float = 0.0,
    min_y: float = 0.0,
    max_x: float = 100.0,
    max_y: float = 80.0,
) -> BoardGeometry:
    """Create a simple rectangular board geometry for testing."""
    return BoardGeometry.from_bounds(min_x, min_y, max_x, max_y)


def _l_shape_geometry() -> BoardGeometry:
    """Create an L-shaped board geometry for testing non-rectangular boards."""
    # L-shape: main 100x80 body with a 40x40 notch cut from top-right
    points = [
        (0, 0),
        (100, 0),
        (100, 40),
        (60, 40),
        (60, 80),
        (0, 80),
    ]
    return BoardGeometry.from_outline_points(points)


# -----------------------------------------------------------------------
# has_shapely()
# -----------------------------------------------------------------------


def test_has_shapely():
    """Shapely should be available since we imported it above."""
    assert has_shapely() is True


# -----------------------------------------------------------------------
# from_bounds / from_outline_points
# -----------------------------------------------------------------------


class TestConstruction:
    def test_from_bounds_basic(self):
        geom = _rect_geometry()
        assert geom.bounds == pytest.approx((0.0, 0.0, 100.0, 80.0))
        assert geom.area == pytest.approx(100.0 * 80.0)

    def test_from_outline_points_rectangle(self):
        geom = BoardGeometry.from_outline_points(
            [(0, 0), (50, 0), (50, 30), (0, 30)]
        )
        assert geom.area == pytest.approx(50.0 * 30.0)

    def test_from_outline_points_l_shape(self):
        geom = _l_shape_geometry()
        # Full 100x80 = 8000, minus 40x40 = 1600 notch = 6400
        assert geom.area == pytest.approx(6400.0)

    def test_from_outline_points_too_few_raises(self):
        with pytest.raises(ValueError, match="at least 3"):
            BoardGeometry.from_outline_points([(0, 0), (1, 1)])

    def test_exterior_coords(self):
        geom = _rect_geometry(0, 0, 10, 10)
        coords = geom.exterior_coords
        # Shapely closes the ring, so first == last
        assert len(coords) == 5
        assert coords[0] == coords[-1]


# -----------------------------------------------------------------------
# contains_point
# -----------------------------------------------------------------------


class TestContainsPoint:
    def test_inside_rectangle(self):
        geom = _rect_geometry()
        assert geom.contains_point(50, 40) is True

    def test_outside_rectangle(self):
        geom = _rect_geometry()
        assert geom.contains_point(150, 40) is False
        assert geom.contains_point(-10, 40) is False

    def test_l_shape_inside_body(self):
        geom = _l_shape_geometry()
        assert geom.contains_point(30, 60) is True

    def test_l_shape_inside_notch(self):
        """Point in the notch region should be outside the L-shape."""
        geom = _l_shape_geometry()
        assert geom.contains_point(80, 60) is False

    def test_l_shape_in_arm(self):
        """Point in the remaining arm of the L should be inside."""
        geom = _l_shape_geometry()
        assert geom.contains_point(80, 20) is True


# -----------------------------------------------------------------------
# distance_to_edge
# -----------------------------------------------------------------------


class TestDistanceToEdge:
    def test_inside_distance_positive(self):
        geom = _rect_geometry(0, 0, 100, 80)
        dist = geom.distance_to_edge(10, 40)
        # Closest edge is left at x=0, distance = 10
        assert dist == pytest.approx(10.0)

    def test_outside_distance_negative(self):
        geom = _rect_geometry(0, 0, 100, 80)
        dist = geom.distance_to_edge(-5, 40)
        assert dist == pytest.approx(-5.0)

    def test_on_edge_is_zero(self):
        geom = _rect_geometry(0, 0, 100, 80)
        dist = geom.distance_to_edge(0, 40)
        assert abs(dist) < 0.001


# -----------------------------------------------------------------------
# buffer
# -----------------------------------------------------------------------


class TestBuffer:
    def test_inset_shrinks(self):
        geom = _rect_geometry(0, 0, 100, 80)
        inset = geom.buffer(-5)
        # 90 * 70 = 6300
        assert inset.area == pytest.approx(6300.0)

    def test_outset_expands(self):
        geom = _rect_geometry(0, 0, 100, 80)
        outset = geom.buffer(5)
        # Expanded area > original area (corners become rounded)
        assert outset.area > 100 * 80

    def test_large_inset_collapses(self):
        geom = _rect_geometry(0, 0, 10, 10)
        inset = geom.buffer(-10)
        # Should collapse to empty or near-empty
        assert inset.area < 1.0


# -----------------------------------------------------------------------
# intersection / difference / union
# -----------------------------------------------------------------------


class TestBooleanOps:
    def test_intersection(self):
        a = _rect_geometry(0, 0, 100, 80)
        b = _rect_geometry(50, 40, 150, 120)
        result = a.intersection(b)
        # Overlap region: x=[50,100], y=[40,80] -> 50*40 = 2000
        assert result.area == pytest.approx(2000.0)

    def test_difference(self):
        a = _rect_geometry(0, 0, 100, 80)
        b = _rect_geometry(50, 40, 150, 120)
        result = a.difference(b)
        # Original 8000 minus 2000 overlap
        assert result.area == pytest.approx(6000.0)

    def test_union(self):
        a = _rect_geometry(0, 0, 100, 80)
        b = _rect_geometry(50, 40, 150, 120)
        result = a.union(b)
        # 8000 + 8000 - 2000 = 14000
        assert result.area == pytest.approx(14000.0)


# -----------------------------------------------------------------------
# compute_boundary_violation
# -----------------------------------------------------------------------


class TestBoundaryViolation:
    def test_fully_inside(self):
        geom = _rect_geometry(0, 0, 100, 80)
        violation = geom.compute_boundary_violation(10, 10, 30, 30)
        assert violation == pytest.approx(0.0)

    def test_partially_outside(self):
        geom = _rect_geometry(0, 0, 100, 80)
        # Component from x=90 to x=110, y=0 to y=20
        # Outside area: x=[100,110], y=[0,20] -> 10*20 = 200
        violation = geom.compute_boundary_violation(90, 0, 110, 20)
        assert violation == pytest.approx(200.0)

    def test_fully_outside(self):
        geom = _rect_geometry(0, 0, 100, 80)
        violation = geom.compute_boundary_violation(200, 200, 210, 210)
        assert violation == pytest.approx(100.0)

    def test_l_shape_in_notch(self):
        """Component in the notch area of an L-shape should be out of bounds."""
        geom = _l_shape_geometry()
        # Place component fully in the notch: x=[70,90], y=[50,70]
        violation = geom.compute_boundary_violation(70, 50, 90, 70)
        assert violation == pytest.approx(20.0 * 20.0)


# -----------------------------------------------------------------------
# to_board_outline / to_optim_polygon
# -----------------------------------------------------------------------


class TestCompatibility:
    def test_to_board_outline(self):
        geom = _rect_geometry(10, 20, 110, 100)
        outline = geom.to_board_outline()
        assert outline.min_x == pytest.approx(10.0)
        assert outline.min_y == pytest.approx(20.0)
        assert outline.max_x == pytest.approx(110.0)
        assert outline.max_y == pytest.approx(100.0)

    def test_to_optim_polygon(self):
        geom = _rect_geometry(0, 0, 50, 30)
        poly = geom.to_optim_polygon()
        assert len(poly.vertices) >= 4


# -----------------------------------------------------------------------
# Arc linearisation
# -----------------------------------------------------------------------


class TestLinearizeArc:
    def test_semicircle(self):
        """A semicircle from (10,0) through (0,10) to (-10,0) around origin."""
        pts = _linearize_arc(10, 0, 0, 10, -10, 0, num_segments=16)
        assert len(pts) == 17  # 16 segments + 1
        # First and last should be near start/end
        assert pts[0] == pytest.approx((10, 0), abs=0.01)
        assert pts[-1] == pytest.approx((-10, 0), abs=0.01)
        # All points should be ~10 units from origin
        for x, y in pts:
            r = math.sqrt(x * x + y * y)
            assert r == pytest.approx(10.0, abs=0.1)

    def test_degenerate_collinear(self):
        """Collinear points should degrade to a straight line."""
        pts = _linearize_arc(0, 0, 5, 0, 10, 0)
        assert len(pts) == 2
        assert pts[0] == (0, 0)
        assert pts[1] == (10, 0)


# -----------------------------------------------------------------------
# Bezier linearisation
# -----------------------------------------------------------------------


class TestLinearizeBezier:
    def test_quadratic(self):
        pts = _linearize_bezier([(0, 0), (5, 10), (10, 0)], num_segments=8)
        assert len(pts) == 9
        assert pts[0] == pytest.approx((0, 0))
        assert pts[-1] == pytest.approx((10, 0))

    def test_cubic(self):
        pts = _linearize_bezier([(0, 0), (3, 10), (7, 10), (10, 0)], num_segments=16)
        assert len(pts) == 17
        assert pts[0] == pytest.approx((0, 0))
        assert pts[-1] == pytest.approx((10, 0))

    def test_single_point(self):
        pts = _linearize_bezier([(5, 5)])
        assert pts == [(5, 5)]


# -----------------------------------------------------------------------
# Segment chaining
# -----------------------------------------------------------------------


class TestChainSegments:
    def test_chain_rectangle(self):
        segments = [
            [(0, 0), (10, 0)],
            [(10, 0), (10, 5)],
            [(10, 5), (0, 5)],
            [(0, 5), (0, 0)],
        ]
        result = _chain_segments(segments)
        assert len(result) >= 4

    def test_chain_reversed_segment(self):
        """Segments with reversed direction should still chain."""
        segments = [
            [(0, 0), (10, 0)],
            [(10, 5), (10, 0)],  # reversed
            [(10, 5), (0, 5)],
            [(0, 5), (0, 0)],
        ]
        result = _chain_segments(segments)
        assert len(result) >= 4

    def test_empty(self):
        assert _chain_segments([]) == []


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------


class TestEdgeCases:
    def test_self_intersecting_outline_fixed(self):
        """A self-intersecting polygon should be auto-fixed via buffer(0)."""
        # Bowtie shape
        points = [(0, 0), (10, 10), (10, 0), (0, 10)]
        geom = BoardGeometry.from_outline_points(points)
        # buffer(0) makes it valid
        assert geom.area > 0

    def test_bounds_property(self):
        geom = _rect_geometry(5, 10, 50, 40)
        min_x, min_y, max_x, max_y = geom.bounds
        assert min_x == pytest.approx(5.0)
        assert min_y == pytest.approx(10.0)
        assert max_x == pytest.approx(50.0)
        assert max_y == pytest.approx(40.0)
