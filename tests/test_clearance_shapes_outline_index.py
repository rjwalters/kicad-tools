"""``min_gap_to_outline`` pruning equivalence and index behaviour (Issue #9751).

Certified arc tessellation (#9689) multiplies outline segment counts by
100-400x, so :func:`kicad_tools.validate.clearance_shapes.min_gap_to_outline`
prunes the per-copper-item scan with a bounding-box lower bound instead of
calling ``copper_gap`` once per outline edge.  These tests pin the contract
that motivated that change:

* **bit-identical results** -- the pruned scan (both the pure-Python loop for
  small outlines and the vectorized pass for dense ones, indexed and plain
  tuple inputs alike) returns exactly the brute-force minimum, including the
  ``NO_INTERACTION`` and negative-gap regimes;
* **the pruning really prunes** -- the exact kernel call count on a
  ~1000-segment tessellated outline is a small fraction of the edge count
  (a call-count ratio, not a wall-clock ratio, so it cannot flake in CI);
* **``outline_shapes`` stays a plain tuple** to every caller.

The timing acceptance (>10x per-item cost drop on a >=1000-segment outline,
no regression on a straight 4-sided one) is measured and reported in the PR,
not asserted here.
"""

from __future__ import annotations

import math
import random
from unittest.mock import patch

import pytest

from kicad_tools.core.outline_tessellation import tessellate_arc
from kicad_tools.router.clearance_kernel import (
    ALL_LAYERS,
    NO_INTERACTION,
    KEdge,
    KSegment,
    KVia,
    KZonePoly,
    copper_gap,
    make_pad,
)
from kicad_tools.validate import clearance_shapes
from kicad_tools.validate.clearance_shapes import (
    _VECTOR_MIN_EDGES,
    min_gap_to_outline,
    outline_shapes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _brute_force(shape, outline) -> float:
    """The pre-#9751 scan, verbatim: one copper_gap call per outline edge."""
    best = NO_INTERACTION
    for edge in outline:
        gap = copper_gap(shape, edge)
        if gap < best:
            best = gap
    return best


def _rounded_rect_segments(
    w: float = 50.0, h: float = 30.0, r: float = 2.0
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A rounded rectangle whose corners are certified-tessellated arcs.

    Four r=2 mm 90-degree corners at ``max_error=1e-5`` (#9689's bound)
    produce ~1000 outline segments -- the density class the issue measured.
    """
    corners = [
        ((w - r, h - r), 0.0),
        ((r, h - r), 90.0),
        ((r, r), 180.0),
        ((w - r, r), 270.0),
    ]
    pts: list[tuple[float, float]] = []
    for (cx, cy), a0 in corners:
        start = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
        mid = (
            cx + r * math.cos(math.radians(a0 + 45.0)),
            cy + r * math.sin(math.radians(a0 + 45.0)),
        )
        end = (
            cx + r * math.cos(math.radians(a0 + 90.0)),
            cy + r * math.sin(math.radians(a0 + 90.0)),
        )
        pts.extend(tessellate_arc(start, mid, end, max_error=1e-5))
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _rect_segments(w: float = 50.0, h: float = 30.0):
    return [
        ((0.0, 0.0), (w, 0.0)),
        ((w, 0.0), (w, h)),
        ((w, h), (0.0, h)),
        ((0.0, h), (0.0, 0.0)),
    ]


def _random_segment(rng: random.Random) -> KSegment:
    x1 = rng.uniform(-5.0, 55.0)
    y1 = rng.uniform(-5.0, 35.0)
    x2 = x1 + rng.uniform(-30.0, 30.0)
    y2 = y1 + rng.uniform(-30.0, 30.0)
    return KSegment(x1=x1, y1=y1, x2=x2, y2=y2, width=rng.choice([0.0, 0.15, 0.2, 1.0]))


def _random_via(rng: random.Random) -> KVia:
    return KVia(
        x=rng.uniform(-5.0, 55.0),
        y=rng.uniform(-5.0, 35.0),
        diameter=rng.choice([0.3, 0.6, 1.2]),
        drill=rng.choice([0.0, 0.3]),
    )


def _random_pad(rng: random.Random):
    return make_pad(
        rng.choice(["circle", "rect", "oval", "roundrect"]),
        rng.uniform(0.3, 2.5),
        rng.uniform(0.3, 2.5),
        rratio=rng.choice([0.25, 0.5]),
        rotation_deg=rng.uniform(0.0, 360.0),
        cx=rng.uniform(-2.0, 52.0),
        cy=rng.uniform(-2.0, 32.0),
        drill=rng.choice([0.0, 0.8]),
    )


def _random_zone(rng: random.Random) -> KZonePoly:
    cx, cy = rng.uniform(-5.0, 55.0), rng.uniform(-5.0, 35.0)
    a = rng.uniform(0.5, 6.0)
    b = rng.uniform(0.5, 6.0)
    ring = ((cx, cy), (cx + a, cy), (cx + a, cy + b), (cx, cy + b), (cx, cy))
    return KZonePoly(rings=(ring,), layer=ALL_LAYERS)


def _random_degenerate_pad(rng: random.Random):
    return make_pad("rect", 0.0, 0.0, cx=rng.uniform(0.0, 50.0), cy=rng.uniform(0.0, 30.0))


def _random_shape(rng: random.Random):
    return rng.choice(
        [
            _random_segment,
            _random_via,
            _random_pad,
            _random_zone,
            _random_degenerate_pad,
        ]
    )(rng)


# ---------------------------------------------------------------------------
# Equivalence: pruned scan == brute-force scan, bit for bit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
class TestPrunedScanMatchesBruteForce:
    """Randomized shapes against four outline/input-form combinations.

    The >=1000-segment rounded rectangle exercises the vectorized pass and the
    4-sided rectangle the pure-Python loop; ``outline_shapes()`` exercises the
    cached index and a plain ``tuple`` of ``KEdge`` the per-call bbox build.
    """

    def _assert_bit_identical(self, shape, outline) -> None:
        expected = _brute_force(shape, outline)
        got = min_gap_to_outline(shape, outline)
        assert not math.isnan(got)
        assert got == expected

    def test_dense_indexed_outline(self, seed: int) -> None:
        rng = random.Random(9751 + seed)
        outline = outline_shapes(_rounded_rect_segments())
        assert len(outline) >= _VECTOR_MIN_EDGES
        for _ in range(20):
            self._assert_bit_identical(_random_shape(rng), outline)

    def test_dense_plain_tuple_outline(self, seed: int) -> None:
        rng = random.Random(29751 + seed)
        outline = tuple(KEdge(points=(s, e)) for s, e in _rounded_rect_segments())
        for _ in range(20):
            self._assert_bit_identical(_random_shape(rng), outline)

    def test_small_indexed_outline(self, seed: int) -> None:
        rng = random.Random(39751 + seed)
        outline = outline_shapes(_rect_segments())
        assert len(outline) < _VECTOR_MIN_EDGES
        for _ in range(20):
            self._assert_bit_identical(_random_shape(rng), outline)

    def test_small_plain_tuple_outline(self, seed: int) -> None:
        rng = random.Random(49751 + seed)
        outline = tuple(KEdge(points=(s, e)) for s, e in _rect_segments())
        for _ in range(20):
            self._assert_bit_identical(_random_shape(rng), outline)


class TestDegenerateEdgesAndShapes:
    """The curated edge cases: empty/point/zero-length edges, degenerate items."""

    def test_empty_outline_is_no_interaction(self) -> None:
        shape = KSegment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2)
        assert min_gap_to_outline(shape, outline_shapes([])) == NO_INTERACTION
        assert min_gap_to_outline(shape, ()) == NO_INTERACTION

    def test_single_segment_outline(self) -> None:
        outline = outline_shapes([((0.0, 0.0), (10.0, 0.0))])
        shape = KSegment(x1=2.0, y1=1.0, x2=8.0, y2=1.0, width=0.2)
        assert min_gap_to_outline(shape, outline) == pytest.approx(0.9)
        assert min_gap_to_outline(shape, outline) == _brute_force(shape, outline)

    def test_zero_length_outline_segment(self) -> None:
        outline = outline_shapes([((5.0, 0.0), (5.0, 0.0)), ((0.0, 0.0), (10.0, 0.0))])
        shape = KSegment(x1=5.0, y1=2.0, x2=5.0, y2=2.0, width=0.0)
        assert min_gap_to_outline(shape, outline) == _brute_force(shape, outline)
        assert min_gap_to_outline(shape, outline) == pytest.approx(2.0)

    def test_vertexless_edge_in_a_dense_outline(self) -> None:
        from kicad_tools.validate.clearance_shapes import _IndexedOutline

        edges = [KEdge(points=(s, e)) for s, e in _rounded_rect_segments()]
        edges.append(KEdge(points=()))
        outline = _IndexedOutline(edges)
        shape = KSegment(x1=10.0, y1=15.0, x2=40.0, y2=15.0, width=0.2)
        assert min_gap_to_outline(shape, outline) == _brute_force(shape, outline)

    def test_vertexless_edge_in_a_small_outline(self) -> None:
        outline = outline_shapes(_rect_segments()) + (KEdge(points=()),)
        shape = KSegment(x1=10.0, y1=15.0, x2=40.0, y2=15.0, width=0.2)
        assert min_gap_to_outline(shape, outline) == _brute_force(shape, outline)

    def test_single_vertex_edge(self) -> None:
        outline = (KEdge(points=((0.0, 0.0),)), KEdge(points=((0.0, 0.0), (10.0, 0.0))))
        shape = KVia(x=5.0, y=3.0, diameter=0.6)
        assert min_gap_to_outline(shape, outline) == _brute_force(shape, outline)
        assert min_gap_to_outline(shape, outline) == pytest.approx(2.7)

    def test_item_crossing_the_outline_reports_negative_gap(self) -> None:
        outline = outline_shapes(_rect_segments())
        shape = KSegment(x1=-5.0, y1=15.0, x2=25.0, y2=15.0, width=0.2)
        got = min_gap_to_outline(shape, outline)
        assert got == _brute_force(shape, outline)
        assert got < 0.0

    def test_item_far_outside_the_outline(self) -> None:
        outline = outline_shapes(_rounded_rect_segments())
        shape = KVia(x=500.0, y=500.0, diameter=0.6)
        assert min_gap_to_outline(shape, outline) == _brute_force(shape, outline)

    def test_empty_zone_and_degenerate_pad_match_brute_force(self) -> None:
        outline = outline_shapes(_rounded_rect_segments())
        empty_zone = KZonePoly(rings=((),), layer=ALL_LAYERS)
        assert min_gap_to_outline(empty_zone, outline) == NO_INTERACTION
        degenerate = make_pad("rect", 0.0, 0.0, cx=25.0, cy=15.0)
        assert not degenerate.core
        assert min_gap_to_outline(degenerate, outline) == NO_INTERACTION

    def test_outline_shape_against_outline_is_no_interaction(self) -> None:
        outline = outline_shapes(_rect_segments())
        assert min_gap_to_outline(outline[0], outline) == NO_INTERACTION


# ---------------------------------------------------------------------------
# The pruning actually prunes (call counts, not wall clock)
# ---------------------------------------------------------------------------


class TestPruningCallCounts:
    """``copper_gap`` invocations must shrink with the pruning, not the result."""

    def test_dense_outline_near_a_corner_calls_few_edges(self) -> None:
        outline = outline_shapes(_rounded_rect_segments())
        # A via 0.5 mm from the bottom-left corner arc: the closest edges are
        # a handful of arc chords, everything else must prune away.
        shape = KVia(x=1.5, y=1.5, diameter=0.6)
        with patch.object(clearance_shapes, "copper_gap", wraps=clearance_shapes.copper_gap) as spy:
            got = min_gap_to_outline(shape, outline)
        assert got == _brute_force(shape, outline)
        assert spy.call_count < len(outline) // 8

    def test_dense_outline_far_item_calls_few_edges(self) -> None:
        outline = outline_shapes(_rounded_rect_segments())
        shape = KSegment(x1=20.0, y1=15.0, x2=30.0, y2=15.0, width=0.2)
        with patch.object(clearance_shapes, "copper_gap", wraps=clearance_shapes.copper_gap) as spy:
            got = min_gap_to_outline(shape, outline)
        assert got == _brute_force(shape, outline)
        assert spy.call_count < len(outline) // 8

    def test_small_outline_prunes_to_the_near_edge(self) -> None:
        outline = outline_shapes(_rect_segments())
        shape = KSegment(x1=10.0, y1=0.5, x2=40.0, y2=0.5, width=0.2)
        with patch.object(clearance_shapes, "copper_gap", wraps=clearance_shapes.copper_gap) as spy:
            got = min_gap_to_outline(shape, outline)
        assert got == _brute_force(shape, outline)
        assert spy.call_count <= 2

    def test_result_is_stable_across_repeated_queries(self) -> None:
        outline = outline_shapes(_rounded_rect_segments())
        shape = _random_segment(random.Random(7))
        first = min_gap_to_outline(shape, outline)
        for _ in range(3):
            assert min_gap_to_outline(shape, outline) == first


class TestOutlineShapesTupleCompatibility:
    """``outline_shapes`` returns an indexed tuple that is still a plain tuple."""

    def test_is_a_tuple_equal_and_hashable(self) -> None:
        segments = _rect_segments()
        indexed = outline_shapes(segments)
        plain = tuple(KEdge(points=(s, e)) for s, e in segments)
        assert isinstance(indexed, tuple)
        assert indexed == plain
        assert hash(indexed) == hash(plain)
        assert tuple(indexed) == plain
        assert indexed[0] == plain[0]
        assert list(indexed) == list(plain)

    def test_bbox_cache_is_built_once(self) -> None:
        outline = outline_shapes(_rect_segments())
        first = outline.bboxes()
        min_gap_to_outline(KVia(x=25.0, y=15.0, diameter=0.6), outline)
        assert outline.bboxes() is first
