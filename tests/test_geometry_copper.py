"""Tests for the trace-copper polygon helper (Issue #4176)."""

from __future__ import annotations

import pytest

from kicad_tools.geometry.copper import (
    point_segment_distance,
    segment_centerline_distance,
    segment_copper_polygon,
    segments_copper_touch,
)

pytest.importorskip("shapely")


def test_segment_polygon_has_expected_area():
    # A 10mm-long, 0.25mm-wide trace: rectangle 10 x 0.25 = 2.5 plus two
    # semicircular end caps of radius 0.125 (area pi * 0.125**2 ~= 0.049).
    poly = segment_copper_polygon((0.0, 0.0), (10.0, 0.0), 0.25)
    assert poly is not None
    expected = 10.0 * 0.25 + 3.141592653589793 * 0.125**2
    assert poly.area == pytest.approx(expected, rel=1e-3)


def test_two_touching_segments_intersect():
    a = segment_copper_polygon((0.0, 0.0), (5.0, 0.0), 0.25)
    b = segment_copper_polygon((5.0, 0.0), (5.0, 5.0), 0.25)
    assert a.intersects(b)


def test_two_apart_segments_do_not_intersect():
    # Endpoints 0.009mm apart but width 0.001 (buffer 0.0005 each side): the
    # copper does not bridge the gap.
    a = segment_copper_polygon((0.0, 0.0), (5.0, 0.0), 0.001)
    b = segment_copper_polygon((5.0, 0.009), (5.0, 5.0), 0.001)
    assert not a.intersects(b)


def test_zero_width_returns_line_geometry():
    poly = segment_copper_polygon((0.0, 0.0), (10.0, 0.0), 0.0)
    assert poly is not None
    assert poly.area == 0.0
    # A zero-length + zero-width segment reduces to a point.
    pt = segment_copper_polygon((1.0, 1.0), (1.0, 1.0), 0.0)
    assert pt is not None
    assert pt.area == 0.0


def test_zero_length_positive_width_is_disk():
    disk = segment_copper_polygon((1.0, 1.0), (1.0, 1.0), 0.5)
    assert disk is not None
    # Round end cap disk of radius 0.25.
    assert disk.area == pytest.approx(3.141592653589793 * 0.25**2, rel=1e-2)


# ---------------------------------------------------------------------------
# Full-copper contact primitives (Issue #5060)
# ---------------------------------------------------------------------------


def test_point_segment_distance_clamps_to_the_segment():
    # Perpendicular foot inside the segment.
    assert point_segment_distance((5.0, 2.0), (0.0, 0.0), (10.0, 0.0)) == pytest.approx(2.0)
    # Perpendicular foot beyond the end: distance to the endpoint.
    assert point_segment_distance((13.0, 4.0), (0.0, 0.0), (10.0, 0.0)) == pytest.approx(5.0)
    # Degenerate zero-length segment behaves as a point.
    assert point_segment_distance((3.0, 4.0), (0.0, 0.0), (0.0, 0.0)) == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # Proper crossing: the minimum is interior to BOTH segments, so a
        # naive endpoint-only minimum would report a positive distance.
        ((((-5.0, 0.0), (5.0, 0.0))), (((0.0, -5.0), (0.0, 5.0))), 0.0),
        # T-junction against an interior point.
        ((((0.0, 0.0), (10.0, 0.0))), (((5.0, 0.0), (5.0, 5.0))), 0.0),
        # Collinear overlap.
        ((((0.0, 0.0), (10.0, 0.0))), (((4.0, 0.0), (6.0, 0.0))), 0.0),
        # Collinear but disjoint.
        ((((0.0, 0.0), (4.0, 0.0))), (((6.0, 0.0), (10.0, 0.0))), 2.0),
        # Parallel offset.
        ((((0.0, 0.0), (10.0, 0.0))), (((0.0, 3.0), (10.0, 3.0))), 3.0),
        # T-junction stopping short of the trunk.
        ((((0.0, 0.0), (10.0, 0.0))), (((5.0, 1.5), (5.0, 5.0))), 1.5),
    ],
)
def test_segment_centerline_distance_matches_shapely(a, b, expected):
    assert segment_centerline_distance(a[0], a[1], b[0], b[1]) == pytest.approx(expected)
    # The analytic result must agree with shapely's LineString distance.
    from shapely.geometry import LineString

    assert segment_centerline_distance(a[0], a[1], b[0], b[1]) == pytest.approx(
        LineString([a[0], a[1]]).distance(LineString([b[0], b[1]]))
    )


def test_segments_copper_touch_uses_full_width():
    trunk = ((0.0, 0.0), (10.0, 0.0), 0.6)
    # Branch stopping exactly on the trunk's copper edge: capsules overlap.
    assert segments_copper_touch(*trunk, (5.0, 0.3), (5.0, 5.0), 0.4)
    # Branch stopping 1 um past the combined reach (0.3 + 0.2): open.
    assert not segments_copper_touch(*trunk, (5.0, 0.501), (5.0, 5.0), 0.4)
    # Exactly tangent copper counts as touching (a ZERO gap, not a positive one).
    assert segments_copper_touch(*trunk, (5.0, 0.5), (5.0, 5.0), 0.4)


def test_segments_copper_touch_agrees_with_buffered_polygons():
    """The analytic predicate matches the buffered-polygon ground truth."""
    cases = [
        ((0.0, 0.0), (10.0, 0.0), 0.6, (5.0, 0.0), (5.0, 5.0), 0.4),
        ((0.0, 0.0), (10.0, 0.0), 0.6, (3.0, 0.5), (7.0, 0.5), 0.6),
        ((0.0, 0.0), (10.0, 0.0), 0.6, (5.0, 0.8), (9.0, 0.8), 0.6),
        ((0.0, 0.0), (10.0, 0.0), 0.6, (5.0, 5.0), (9.0, 5.0), 0.6),
        ((0.0, 0.0), (0.0, 0.0), 0.4, (0.1, 0.0), (5.0, 0.0), 0.2),
        ((0.0, 0.0), (0.0, 0.0), 0.4, (0.4, 0.0), (5.0, 0.0), 0.2),
    ]
    for a_start, a_end, a_w, b_start, b_end, b_w in cases:
        poly_a = segment_copper_polygon(a_start, a_end, a_w)
        poly_b = segment_copper_polygon(b_start, b_end, b_w)
        # buffer() facets the round caps inwards, so shapely can only ever
        # UNDER-report contact; a mismatch is allowed solely in that direction.
        if poly_a.intersects(poly_b):
            assert segments_copper_touch(a_start, a_end, a_w, b_start, b_end, b_w)


def test_segments_copper_touch_is_invariant_under_collinear_splits():
    """Splitting a segment cannot create or destroy contact (#5060)."""
    trunk_start, trunk_end, trunk_w = (0.0, 0.0), (10.0, 0.0), 0.6
    for branch, expected in (
        (((5.0, 0.0), (5.0, 5.0)), True),  # T on the interior
        (((5.0, 0.601), (5.0, 5.0)), False),  # 1 um positive gap
    ):
        whole = segments_copper_touch(trunk_start, trunk_end, trunk_w, *branch, 0.6)
        assert whole is expected
        for parts in (2, 3, 7):
            touched = any(
                segments_copper_touch(
                    (10.0 * i / parts, 0.0),
                    (10.0 * (i + 1) / parts, 0.0),
                    trunk_w,
                    *branch,
                    0.6,
                )
                for i in range(parts)
            )
            assert touched is expected, f"{parts}-way split changed the verdict"
