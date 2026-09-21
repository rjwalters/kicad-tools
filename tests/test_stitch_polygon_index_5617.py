"""Equivalence tests for the filled-polygon edge index (Issue #5617).

The pad-aware stitch pass (phase 10 of the board recipes) spends nearly all
of its time in four predicates that walk every edge of every ``filled_polygon``
for every candidate via position.  Issue #5617 bins those edges into a uniform
grid (:class:`~kicad_tools.cli.stitch_cmd._PolygonIndex`) so a query only
examines edges that could possibly satisfy the predicate.

The optimisation is only admissible if it is **exactly** output-preserving --
the issue's constraints forbid "a faster result obtained by dropping work".
Every test here therefore pins the indexed answer to the linear answer on the
same geometry, with the linear path forced by raising the index's minimum
vertex count above the polygon size.

Geometry deliberately includes the shapes a real KiCad zone fill produces:
a concave outline, a keyhole void (even-odd "inside the hole" == outside),
collinear runs, and a board-spanning edge that overflows the index's per-edge
cell-span cap.
"""

from __future__ import annotations

import math
import random

import pytest

from kicad_tools.cli import stitch_cmd
from kicad_tools.cli.stitch_cmd import (
    FilledPolygon,
    _check_point_filled_polygon_clearance,
    _check_segment_filled_polygon_clearance,
    _point_has_edge_margin,
    _polygon_index,
    _PolygonIndex,
    point_in_polygon,
    point_to_segment_distance,
    segment_to_segment_distance,
)

# --- geometry fixtures --------------------------------------------------------


def _ring(cx: float, cy: float, r: float, n: int, phase: float = 0.0) -> list:
    """A closed convex ring of ``n`` vertices."""
    return [
        (
            cx + r * math.cos(phase + 2 * math.pi * i / n),
            cy + r * math.sin(phase + 2 * math.pi * i / n),
        )
        for i in range(n)
    ]


def _keyholed_pour() -> list[tuple[float, float]]:
    """A fill-like ring: rectangular outline with a keyholed circular void.

    KiCad emits a void as a "keyhole" -- the outline walks in along a seam,
    around the hole, and back out -- which is exactly the shape the even-odd
    ray cast has to treat as *outside* inside the hole.
    """
    outline = [
        (0.0, 0.0),
        (40.0, 0.0),
        (40.0, 30.0),
        (25.0, 30.0),
        (25.0, 18.0),
        (18.0, 18.0),
        (18.0, 30.0),
        (0.0, 30.0),
    ]
    # Densify the outline so the polygon is large enough to be indexed.
    dense: list[tuple[float, float]] = []
    for i, (x1, y1) in enumerate(outline):
        x2, y2 = outline[(i + 1) % len(outline)]
        steps = 12
        for s in range(steps):
            t = s / steps
            dense.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    # Keyhole seam out to a circular void and back.
    hole = _ring(10.0, 10.0, 4.0, 48)
    dense.append((10.0, 30.0))
    dense.append((hole[0][0], hole[0][1]))
    dense.extend(hole[1:])
    dense.append((hole[0][0], hole[0][1]))
    dense.append((10.0, 30.0))
    return dense


@pytest.fixture
def pour_points() -> list[tuple[float, float]]:
    return _keyholed_pour()


@pytest.fixture
def force_linear(monkeypatch: pytest.MonkeyPatch):
    """Raise the index threshold so the predicates take the linear path."""

    def _apply() -> None:
        monkeypatch.setattr(stitch_cmd, "_POLYGON_INDEX_MIN_POINTS", 10**9)

    return _apply


# --- reference implementations (the pre-#5617 linear predicates) --------------


def _linear_edge_within_point(points, x, y, threshold) -> bool:
    n = len(points)
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[(i + 1) % n]
        if point_to_segment_distance(x, y, xi, yi, xj, yj) < threshold:
            return True
    return False


def _linear_edge_within_segment(points, sx, sy, ex, ey, threshold) -> bool:
    n = len(points)
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[(i + 1) % n]
        if segment_to_segment_distance(sx, sy, ex, ey, xi, yi, xj, yj) < threshold:
            return True
    return False


# --- tests --------------------------------------------------------------------


def test_index_is_actually_built_for_a_fill_sized_polygon(pour_points):
    """The fixture is large enough that the real code path indexes it."""
    assert len(pour_points) >= stitch_cmd._POLYGON_INDEX_MIN_POINTS
    assert isinstance(_polygon_index(pour_points), _PolygonIndex)


def test_small_polygons_are_not_indexed():
    """Below the threshold the linear scan is cheaper -- no index is built."""
    assert _polygon_index(_ring(0.0, 0.0, 1.0, 8)) is None


def test_index_is_cached_per_point_list_identity(pour_points):
    first = _polygon_index(pour_points)
    assert _polygon_index(pour_points) is first
    # A distinct list with equal contents is a distinct index (identity-keyed).
    assert _polygon_index(list(pour_points)) is not first


def test_contains_matches_point_in_polygon_on_a_grid(pour_points):
    """Even-odd parity is identical for the indexed ray cast, everywhere."""
    index = _PolygonIndex(pour_points)
    checked = 0
    for gx in range(-2, 43):
        for gy in range(-2, 33):
            x, y = gx + 0.37, gy + 0.61
            assert index.contains(x, y) == point_in_polygon(x, y, pour_points), (x, y)
            checked += 1
    assert checked > 1000


def test_contains_treats_the_keyhole_void_as_outside(pour_points):
    """Sanity anchor: the fixture really does exercise the void case."""
    index = _PolygonIndex(pour_points)
    assert index.contains(30.0, 5.0) is True  # solid copper
    assert index.contains(10.0, 10.0) is False  # inside the keyholed void
    assert point_in_polygon(10.0, 10.0, pour_points) is False


def test_contains_matches_on_random_points(pour_points):
    index = _PolygonIndex(pour_points)
    rng = random.Random(5617)
    for _ in range(3000):
        x = rng.uniform(-5.0, 45.0)
        y = rng.uniform(-5.0, 35.0)
        assert index.contains(x, y) == point_in_polygon(x, y, pour_points), (x, y)


@pytest.mark.parametrize("threshold", [0.05, 0.2, 0.5, 2.0])
def test_point_edge_query_matches_linear_scan(pour_points, threshold):
    index = _PolygonIndex(pour_points)
    rng = random.Random(0xC0FFEE + int(threshold * 100))
    for _ in range(1500):
        x = rng.uniform(-3.0, 43.0)
        y = rng.uniform(-3.0, 33.0)
        assert index.has_edge_within_point(x, y, threshold) == _linear_edge_within_point(
            pour_points, x, y, threshold
        ), (x, y, threshold)


@pytest.mark.parametrize("threshold", [0.05, 0.3, 1.5])
def test_segment_edge_query_matches_linear_scan(pour_points, threshold):
    index = _PolygonIndex(pour_points)
    rng = random.Random(42 + int(threshold * 100))
    for _ in range(800):
        sx = rng.uniform(-3.0, 43.0)
        sy = rng.uniform(-3.0, 33.0)
        ex = sx + rng.uniform(-6.0, 6.0)
        ey = sy + rng.uniform(-6.0, 6.0)
        assert index.has_edge_within_segment(
            sx, sy, ex, ey, threshold
        ) == _linear_edge_within_segment(pour_points, sx, sy, ex, ey, threshold), (
            sx,
            sy,
            ex,
            ey,
            threshold,
        )


def test_board_spanning_edge_overflow_is_still_exact():
    """An edge whose bbox spans more cells than the cap stays always-checked."""
    # A long thin sliver: one edge runs the full width, forcing the overflow
    # list for both the cell grid and the row index.
    points = [(0.0, 0.0), (300.0, 0.0), (300.0, 0.02)]
    points += [(300.0 - i * 0.05, 0.02) for i in range(1, 200)]
    index = _PolygonIndex(points)
    rng = random.Random(7)
    for _ in range(800):
        x = rng.uniform(-1.0, 301.0)
        y = rng.uniform(-1.0, 1.0)
        assert index.contains(x, y) == point_in_polygon(x, y, points), (x, y)
        assert index.has_edge_within_point(x, y, 0.4) == _linear_edge_within_point(
            points, x, y, 0.4
        ), (x, y)


# --- predicate-level equivalence (indexed vs. forced-linear) -----------------


def _fill(points, *, net=3, name="GND", layer="In1.Cu") -> FilledPolygon:
    return FilledPolygon(net_number=net, net_name=name, layer=layer, points=list(points))


def test_point_clearance_predicate_matches_linear(pour_points, monkeypatch):
    fills = [_fill(pour_points)]
    rng = random.Random(1234)
    samples = [
        (rng.uniform(-3.0, 43.0), rng.uniform(-3.0, 33.0), rng.choice([0.1, 0.225, 0.5]))
        for _ in range(1200)
    ]
    indexed = [_check_point_filled_polygon_clearance(x, y, r, fills, 0.2) for x, y, r in samples]
    monkeypatch.setattr(stitch_cmd, "_POLYGON_INDEX_MIN_POINTS", 10**9)
    linear = [_check_point_filled_polygon_clearance(x, y, r, fills, 0.2) for x, y, r in samples]
    assert indexed == linear
    # The sample must exercise both verdicts or the comparison is vacuous.
    assert True in linear and False in linear


def test_segment_clearance_predicate_matches_linear(pour_points, monkeypatch):
    fills = [_fill(pour_points)]
    rng = random.Random(99)
    samples = []
    for _ in range(700):
        sx = rng.uniform(-3.0, 43.0)
        sy = rng.uniform(-3.0, 33.0)
        samples.append((sx, sy, sx + rng.uniform(-5.0, 5.0), sy + rng.uniform(-5.0, 5.0)))
    indexed = [
        _check_segment_filled_polygon_clearance(sx, sy, ex, ey, 0.1, fills, 0.2)
        for sx, sy, ex, ey in samples
    ]
    monkeypatch.setattr(stitch_cmd, "_POLYGON_INDEX_MIN_POINTS", 10**9)
    linear = [
        _check_segment_filled_polygon_clearance(sx, sy, ex, ey, 0.1, fills, 0.2)
        for sx, sy, ex, ey in samples
    ]
    assert indexed == linear
    assert True in linear and False in linear


def test_edge_margin_predicate_matches_linear(pour_points, monkeypatch):
    rng = random.Random(2026)
    samples = [
        (rng.uniform(-3.0, 43.0), rng.uniform(-3.0, 33.0), rng.choice([0.15, 0.4]))
        for _ in range(1200)
    ]
    indexed = [_point_has_edge_margin(x, y, pour_points, m) for x, y, m in samples]
    monkeypatch.setattr(stitch_cmd, "_POLYGON_INDEX_MIN_POINTS", 10**9)
    linear = [_point_has_edge_margin(x, y, pour_points, m) for x, y, m in samples]
    assert indexed == linear
    assert True in linear and False in linear


def test_same_net_fill_predicate_matches_linear(pour_points, monkeypatch):
    fills = [_fill(pour_points)]
    rng = random.Random(555)
    samples = [(rng.uniform(-3.0, 43.0), rng.uniform(-3.0, 33.0)) for _ in range(1200)]
    indexed = [stitch_cmd._point_on_same_net_fill(x, y, 0.225, fills, "In1.Cu") for x, y in samples]
    monkeypatch.setattr(stitch_cmd, "_POLYGON_INDEX_MIN_POINTS", 10**9)
    linear = [stitch_cmd._point_on_same_net_fill(x, y, 0.225, fills, "In1.Cu") for x, y in samples]
    assert indexed == linear
    assert True in linear and False in linear
