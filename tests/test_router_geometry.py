"""Unit tests for consolidated vector geometry primitives.

Tests cover:
- point_to_segment_distance: various geometric configurations
- segments_intersect: crossing, parallel, collinear, endpoint-touching
- segment_to_segment_distance: including intersection case returning 0
- segment_clearance: edge-to-edge clearance accounting for trace widths
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.geometry import (
    point_to_segment_distance,
    segment_clearance,
    segment_to_segment_distance,
    segments_intersect,
)


# ---------------------------------------------------------------------------
# point_to_segment_distance
# ---------------------------------------------------------------------------


class TestPointToSegmentDistance:
    """Tests for point_to_segment_distance."""

    def test_point_on_segment(self):
        """Point lying exactly on the segment should have distance 0."""
        assert point_to_segment_distance(1.0, 0.0, 0.0, 0.0, 2.0, 0.0) == pytest.approx(0.0)

    def test_point_perpendicular(self):
        """Point directly above a horizontal segment."""
        dist = point_to_segment_distance(1.0, 3.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(3.0)

    def test_point_at_start(self):
        """Point coincides with segment start."""
        assert point_to_segment_distance(0.0, 0.0, 0.0, 0.0, 5.0, 0.0) == pytest.approx(0.0)

    def test_point_at_end(self):
        """Point coincides with segment end."""
        assert point_to_segment_distance(5.0, 0.0, 0.0, 0.0, 5.0, 0.0) == pytest.approx(0.0)

    def test_point_past_start(self):
        """Point beyond the start of the segment (projects before t=0)."""
        dist = point_to_segment_distance(-3.0, 0.0, 0.0, 0.0, 5.0, 0.0)
        assert dist == pytest.approx(3.0)

    def test_point_past_end(self):
        """Point beyond the end of the segment (projects after t=1)."""
        dist = point_to_segment_distance(8.0, 0.0, 0.0, 0.0, 5.0, 0.0)
        assert dist == pytest.approx(3.0)

    def test_degenerate_zero_length_segment(self):
        """Zero-length segment reduces to point-to-point distance."""
        dist = point_to_segment_distance(3.0, 4.0, 0.0, 0.0, 0.0, 0.0)
        assert dist == pytest.approx(5.0)

    def test_diagonal_segment(self):
        """Point perpendicular to a 45-degree segment."""
        # Segment from (0,0) to (2,2).  Point at (0,2) is 45-deg perpendicular.
        dist = point_to_segment_distance(0.0, 2.0, 0.0, 0.0, 2.0, 2.0)
        assert dist == pytest.approx(math.sqrt(2.0))

    def test_vertical_segment(self):
        """Point to the right of a vertical segment."""
        dist = point_to_segment_distance(4.0, 1.0, 0.0, 0.0, 0.0, 5.0)
        assert dist == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# segments_intersect
# ---------------------------------------------------------------------------


class TestSegmentsIntersect:
    """Tests for segments_intersect."""

    def test_crossing_segments(self):
        """Two segments that cross in an X pattern."""
        assert segments_intersect(0, 0, 2, 2, 0, 2, 2, 0) is True

    def test_parallel_segments(self):
        """Parallel segments never intersect."""
        assert segments_intersect(0, 0, 5, 0, 0, 1, 5, 1) is False

    def test_collinear_overlapping(self):
        """Collinear overlapping segments are NOT counted as intersecting."""
        assert segments_intersect(0, 0, 4, 0, 2, 0, 6, 0) is False

    def test_shared_endpoint(self):
        """Segments sharing exactly one endpoint are NOT intersecting."""
        assert segments_intersect(0, 0, 2, 2, 2, 2, 4, 0) is False

    def test_t_intersection(self):
        """One segment endpoint touches the other's interior -- not proper intersection."""
        # (0,0)-(4,0) and (2,-1)-(2,0): endpoint (2,0) lies on first segment
        assert segments_intersect(0, 0, 4, 0, 2, -1, 2, 0) is False

    def test_proper_cross(self):
        """Standard + cross."""
        assert segments_intersect(1, 0, 1, 4, 0, 2, 4, 2) is True

    def test_disjoint_segments(self):
        """Completely separated segments."""
        assert segments_intersect(0, 0, 1, 1, 5, 5, 6, 6) is False

    def test_perpendicular_not_reaching(self):
        """Perpendicular segments that don't extend far enough to cross."""
        assert segments_intersect(0, 0, 2, 0, 3, -1, 3, 1) is False

    def test_float_crossing(self):
        """Crossing with float coordinates."""
        assert segments_intersect(0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 0.0) is True


# ---------------------------------------------------------------------------
# segment_to_segment_distance
# ---------------------------------------------------------------------------


class TestSegmentToSegmentDistance:
    """Tests for segment_to_segment_distance."""

    def test_intersecting_returns_zero(self):
        """Intersecting segments should return distance 0."""
        dist = segment_to_segment_distance(0, 0, 2, 2, 0, 2, 2, 0)
        assert dist == pytest.approx(0.0)

    def test_parallel_horizontal(self):
        """Parallel horizontal segments separated by 3 units."""
        dist = segment_to_segment_distance(0, 0, 5, 0, 0, 3, 5, 3)
        assert dist == pytest.approx(3.0)

    def test_perpendicular_non_overlapping(self):
        """L-shaped segments that don't overlap."""
        # Horizontal (0,0)-(3,0) and vertical (5,0)-(5,3)
        dist = segment_to_segment_distance(0, 0, 3, 0, 5, 0, 5, 3)
        assert dist == pytest.approx(2.0)

    def test_touching_at_endpoints(self):
        """Segments that touch at one endpoint pair."""
        dist = segment_to_segment_distance(0, 0, 2, 0, 2, 0, 2, 3)
        assert dist == pytest.approx(0.0)

    def test_zero_length_segments(self):
        """Two degenerate (zero-length) segments -- point-to-point distance."""
        dist = segment_to_segment_distance(0, 0, 0, 0, 3, 4, 3, 4)
        assert dist == pytest.approx(5.0)

    def test_collinear_gap(self):
        """Collinear segments with a gap between them."""
        dist = segment_to_segment_distance(0, 0, 2, 0, 5, 0, 8, 0)
        assert dist == pytest.approx(3.0)

    def test_close_parallel_segments(self):
        """Parallel segments very close together."""
        dist = segment_to_segment_distance(0, 0, 10, 0, 0, 0.1, 10, 0.1)
        assert dist == pytest.approx(0.1, abs=1e-9)


# ---------------------------------------------------------------------------
# segment_clearance
# ---------------------------------------------------------------------------


class TestSegmentClearance:
    """Tests for segment_clearance."""

    def test_well_separated(self):
        """Two traces with plenty of clearance."""
        # Parallel segments 3mm apart, each 0.2mm wide
        clr = segment_clearance(0, 0, 5, 0, 0.2, 0, 3, 5, 3, 0.2)
        # Expected: 3.0 - 0.1 - 0.1 = 2.8
        assert clr == pytest.approx(2.8)

    def test_just_touching(self):
        """Two traces whose edges exactly touch (clearance = 0)."""
        # Distance = 1.0, widths = 1.0 each => clearance = 1.0 - 0.5 - 0.5 = 0
        clr = segment_clearance(0, 0, 5, 0, 1.0, 0, 1, 5, 1, 1.0)
        assert clr == pytest.approx(0.0)

    def test_overlapping(self):
        """Two traces that overlap (negative clearance)."""
        clr = segment_clearance(0, 0, 5, 0, 1.0, 0, 0.5, 5, 0.5, 1.0)
        # Distance = 0.5, clearance = 0.5 - 0.5 - 0.5 = -0.5
        assert clr == pytest.approx(-0.5)

    def test_crossing(self):
        """Crossing segments have negative clearance."""
        clr = segment_clearance(0, 0, 2, 2, 0.25, 0, 2, 2, 0, 0.25)
        # Distance = 0 (intersection), clearance = -0.25
        assert clr == pytest.approx(-0.25)
