"""Tests for R-tree spatial indexing in RoutingGrid (Issue #1249).

Validates that the R-tree-accelerated segment clearance checks produce
identical results to the brute-force path, that the index is maintained
correctly across mark_route/unmark_route, and that graceful degradation
works when rtree is unavailable.
"""

import random
import timeit

import pytest

from kicad_tools.router.grid import (
    RTREE_AVAILABLE,
    RTREE_SEGMENT_THRESHOLD,
    RoutingGrid,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rules():
    """Standard design rules for testing."""
    return DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.127,
    )


@pytest.fixture
def grid(rules):
    """Create a 50x50 mm routing grid."""
    return RoutingGrid(width=50.0, height=50.0, rules=rules)


def _make_segment(x1, y1, x2, y2, net=1, layer=Layer.F_CU, width=0.2):
    """Helper to create a Segment with defaults."""
    return Segment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=width,
        layer=layer,
        net=net,
        net_name=f"NET{net}",
    )


def _make_route(segments, net=1):
    """Helper to create a Route from segments."""
    return Route(net=net, net_name=f"NET{net}", segments=segments, vias=[])


# ---------------------------------------------------------------------------
# Basic R-tree management tests
# ---------------------------------------------------------------------------


class TestRtreeIndexManagement:
    """Tests for R-tree index insertion and removal."""

    def test_rtree_available_flag(self, grid):
        """Verify _rtree_available reflects actual import state."""
        assert grid._rtree_available == RTREE_AVAILABLE

    def test_rtree_count_increments_on_mark(self, grid):
        """Segments are indexed when a route is marked."""
        seg = _make_segment(1.0, 1.0, 5.0, 1.0, net=1)
        route = _make_route([seg], net=1)
        grid.mark_route(route)

        if RTREE_AVAILABLE:
            assert grid._seg_rtree_count == 1
        else:
            assert grid._seg_rtree_count == 0

    def test_rtree_count_decrements_on_unmark(self, grid):
        """Segments are removed from the index when a route is unmarked."""
        seg = _make_segment(1.0, 1.0, 5.0, 1.0, net=1)
        route = _make_route([seg], net=1)
        grid.mark_route(route)
        grid.unmark_route(route)

        assert grid._seg_rtree_count == 0

    def test_rtree_multi_segment_route(self, grid):
        """Multiple segments per route are all indexed."""
        segs = [
            _make_segment(1.0, 1.0, 5.0, 1.0, net=1),
            _make_segment(5.0, 1.0, 5.0, 5.0, net=1),
            _make_segment(5.0, 5.0, 10.0, 5.0, net=1),
        ]
        route = _make_route(segs, net=1)
        grid.mark_route(route)

        if RTREE_AVAILABLE:
            assert grid._seg_rtree_count == 3

    def test_rtree_per_layer_isolation(self, grid):
        """Segments on different layers go into separate R-tree indices."""
        seg_f = _make_segment(1.0, 1.0, 5.0, 1.0, net=1, layer=Layer.F_CU)
        seg_b = _make_segment(1.0, 2.0, 5.0, 2.0, net=2, layer=Layer.B_CU)
        grid.mark_route(_make_route([seg_f], net=1))
        grid.mark_route(_make_route([seg_b], net=2))

        if RTREE_AVAILABLE:
            assert grid._seg_rtree_count == 2
            f_idx = grid.layer_to_index(Layer.F_CU.value)
            b_idx = grid.layer_to_index(Layer.B_CU.value)
            assert len(grid._seg_rtree_items.get(f_idx, {})) == 1
            assert len(grid._seg_rtree_items.get(b_idx, {})) == 1


# ---------------------------------------------------------------------------
# Functional parity: R-tree vs brute-force
# ---------------------------------------------------------------------------


class TestRtreeBruteForceEquivalence:
    """Verify R-tree path produces identical results to brute-force path."""

    def _populate_grid(self, grid, n_routes, n_segs_per_route, seed=42):
        """Add many routes to the grid for testing.

        Creates routes with segments spread across the grid area,
        using different nets to ensure cross-net clearance checking is exercised.
        """
        rng = random.Random(seed)
        routes = []
        for i in range(n_routes):
            net = i + 1
            segs = []
            # Start each route at a random position
            x, y = rng.uniform(2.0, 45.0), rng.uniform(2.0, 45.0)
            for _ in range(n_segs_per_route):
                # Random walk producing small segments
                dx = rng.uniform(-3.0, 3.0)
                dy = rng.uniform(-3.0, 3.0)
                x2 = max(1.0, min(49.0, x + dx))
                y2 = max(1.0, min(49.0, y + dy))
                segs.append(_make_segment(x, y, x2, y2, net=net))
                x, y = x2, y2
            route = _make_route(segs, net=net)
            grid.mark_route(route)
            routes.append(route)
        return routes

    def _query_both_paths(self, grid, query_seg, exclude_net, min_clearance):
        """Run the query with R-tree enabled and with brute-force, return both results."""
        # R-tree path (or brute-force if rtree not available)
        result_normal = grid.validate_segment_clearance(
            query_seg, exclude_net=exclude_net, min_clearance=min_clearance
        )

        # Force brute-force by temporarily setting count below threshold
        saved_count = grid._seg_rtree_count
        grid._seg_rtree_count = 0
        result_brute = grid.validate_segment_clearance(
            query_seg, exclude_net=exclude_net, min_clearance=min_clearance
        )
        grid._seg_rtree_count = saved_count

        return result_normal, result_brute

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_parity_above_threshold(self, grid):
        """With 200+ segments the R-tree path matches brute-force exactly."""
        self._populate_grid(grid, n_routes=40, n_segs_per_route=6)
        assert grid._seg_rtree_count >= RTREE_SEGMENT_THRESHOLD

        rng = random.Random(99)
        for _ in range(20):
            x1 = rng.uniform(2.0, 48.0)
            y1 = rng.uniform(2.0, 48.0)
            x2 = x1 + rng.uniform(-5.0, 5.0)
            y2 = y1 + rng.uniform(-5.0, 5.0)
            exclude_net = rng.randint(1, 40)
            query = _make_segment(x1, y1, x2, y2, net=exclude_net)

            result_rt, result_bf = self._query_both_paths(
                grid, query, exclude_net, min_clearance=0.127
            )
            # is_valid must match
            assert result_rt[0] == result_bf[0], (
                f"is_valid mismatch for query ({x1},{y1})->({x2},{y2}) net={exclude_net}: "
                f"rtree={result_rt[0]}, brute={result_bf[0]}"
            )
            # min clearance must match within floating-point tolerance
            assert abs(result_rt[1] - result_bf[1]) < 1e-9, (
                f"clearance mismatch: rtree={result_rt[1]}, brute={result_bf[1]}"
            )

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_parity_with_violations(self, grid):
        """Verify violation detection is identical between R-tree and brute-force."""
        # Place a dense cluster of routes
        self._populate_grid(grid, n_routes=50, n_segs_per_route=5)

        # Query a segment that runs through the middle -- high chance of violations
        query = _make_segment(10.0, 25.0, 40.0, 25.0, net=999)
        result_rt, result_bf = self._query_both_paths(
            grid, query, exclude_net=999, min_clearance=0.127
        )

        assert result_rt[0] == result_bf[0]
        assert abs(result_rt[1] - result_bf[1]) < 1e-9

    def test_below_threshold_uses_brute_force(self, grid):
        """Below RTREE_SEGMENT_THRESHOLD, brute-force is used regardless."""
        # Add fewer segments than threshold
        for i in range(min(5, RTREE_SEGMENT_THRESHOLD - 1)):
            seg = _make_segment(1.0 + i * 2.0, 1.0, 1.0 + i * 2.0, 5.0, net=i + 1)
            grid.mark_route(_make_route([seg], net=i + 1))

        assert grid._seg_rtree_count < RTREE_SEGMENT_THRESHOLD

        query = _make_segment(2.0, 3.0, 4.0, 3.0, net=999)
        # This should work correctly using brute-force path
        is_valid, clearance, loc = grid.validate_segment_clearance(query, exclude_net=999)
        # Just verify it returns a valid tuple (no crash)
        assert isinstance(is_valid, bool)
        assert isinstance(clearance, float)


# ---------------------------------------------------------------------------
# Issue #3522: global-minimum clearance parity (pin tests)
# ---------------------------------------------------------------------------


class TestRtreeGlobalMinParity:
    """Pin tests for issue #3522.

    The R-tree path used a single bounded range query (margin =
    ``seg_half_width + min_clearance``), so when the true nearest foreign-net
    segment fell outside that envelope the reported ``actual_clearance`` was
    either ``inf`` (no candidate at all) or a finite but non-minimal value
    (a far candidate whose bbox happened to overlap, e.g. a long diagonal).
    The brute-force reference reports the unbounded global minimum.
    """

    def _query_both_paths(self, grid, query_seg, exclude_net, min_clearance=0.127):
        """Run the same query via the R-tree path and the brute-force path."""
        result_rtree = grid.validate_segment_clearance(
            query_seg, exclude_net=exclude_net, min_clearance=min_clearance
        )
        saved_count = grid._seg_rtree_count
        grid._seg_rtree_count = 0
        result_brute = grid.validate_segment_clearance(
            query_seg, exclude_net=exclude_net, min_clearance=min_clearance
        )
        grid._seg_rtree_count = saved_count
        return result_rtree, result_brute

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_far_neighbor_clearance_not_infinite(self, grid):
        """A neighbor beyond the violation envelope must still be reported.

        33 vertical foreign-net segments sit 4.5mm+ away from the query --
        far outside the pass-1 envelope (~0.227mm margin) -- so the old code
        found zero candidates and returned clearance=inf while brute force
        found 4.3mm.
        """
        for i in range(33):
            s = _make_segment(10.0 + i * 1.0, 20.0, 10.0 + i * 1.0, 30.0, net=i + 1)
            grid.mark_route(_make_route([s], net=i + 1))
        assert grid._seg_rtree_count >= RTREE_SEGMENT_THRESHOLD

        query = _make_segment(5.0, 25.0, 5.5, 25.0, net=999)
        result_rt, result_bf = self._query_both_paths(grid, query, exclude_net=999)

        # Nearest stored segment is at x=10.0: centerline distance 4.5mm,
        # minus both half-widths (0.1 + 0.1) = 4.3mm edge-to-edge.
        assert result_bf[1] == pytest.approx(4.3, abs=1e-12)
        assert result_rt[1] != float("inf"), (
            "R-tree path dropped the nearest neighbor (issue #3522 regression)"
        )
        assert result_rt[0] == result_bf[0]
        assert abs(result_rt[1] - result_bf[1]) < 1e-9

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_bbox_overlap_does_not_mask_true_minimum(self, grid):
        """A far diagonal candidate must not mask a nearer off-envelope segment.

        The long diagonal's bounding box overlaps the query envelope, so the
        old single-pass code returned its (large) clearance even though a
        vertical segment outside the pass-1 envelope was geometrically much
        closer.  This is the finite-but-non-minimal variant of #3522.
        """
        # Long diagonal: bbox covers the query region, but the actual
        # centerline is ~11mm away from the query segment.
        diag = _make_segment(0.0, 0.0, 20.0, 20.0, net=1)
        grid.mark_route(_make_route([diag], net=1))

        # True nearest neighbor: vertical segment 3.5mm right of the query,
        # well outside the pass-1 envelope (~0.227mm margin).
        near = _make_segment(6.0, 10.0, 6.0, 26.0, net=2)
        grid.mark_route(_make_route([near], net=2))

        # Filler segments far away so the R-tree path engages.
        for i in range(32):
            s = _make_segment(35.0 + i * 0.4, 35.0, 35.0 + i * 0.4, 45.0, net=100 + i)
            grid.mark_route(_make_route([s], net=100 + i))
        assert grid._seg_rtree_count >= RTREE_SEGMENT_THRESHOLD

        query = _make_segment(2.0, 18.0, 2.5, 18.0, net=999)
        result_rt, result_bf = self._query_both_paths(grid, query, exclude_net=999)

        # Nearest is the vertical segment at x=6.0: centerline distance 3.5mm
        # minus both half-widths = 3.3mm edge-to-edge.
        assert result_bf[1] == pytest.approx(3.3, abs=1e-12)
        assert result_rt[0] == result_bf[0]
        assert abs(result_rt[1] - result_bf[1]) < 1e-9

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_issue_3522_exact_geometry(self, grid):
        """Pin the exact failing query from the issue #3522 report.

        Reproduces the deterministic geometry from
        ``test_parity_above_threshold`` (populate seed 42, query seed 99,
        second query) where the R-tree path returned clearance=inf and brute
        force found 3.325339152448819mm.
        """
        TestRtreeBruteForceEquivalence()._populate_grid(grid, n_routes=40, n_segs_per_route=6)
        assert grid._seg_rtree_count >= RTREE_SEGMENT_THRESHOLD

        rng = random.Random(99)
        query = None
        exclude_net = None
        for _ in range(2):  # second draw is the historically failing query q1
            x1 = rng.uniform(2.0, 48.0)
            y1 = rng.uniform(2.0, 48.0)
            x2 = x1 + rng.uniform(-5.0, 5.0)
            y2 = y1 + rng.uniform(-5.0, 5.0)
            exclude_net = rng.randint(1, 40)
            query = _make_segment(x1, y1, x2, y2, net=exclude_net)

        result_rt, result_bf = self._query_both_paths(grid, query, exclude_net)

        assert result_bf[1] == pytest.approx(3.325339152448819, abs=1e-9)
        assert result_rt[1] != float("inf"), (
            "R-tree path returned clearance=inf for the issue #3522 geometry"
        )
        assert result_rt[0] == result_bf[0]
        assert abs(result_rt[1] - result_bf[1]) < 1e-9


# ---------------------------------------------------------------------------
# Incremental update tests
# ---------------------------------------------------------------------------


class TestRtreeIncrementalUpdates:
    """Verify the R-tree reflects mark/unmark correctly."""

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_unmark_removes_from_index(self, grid):
        """After unmarking a route, its segments are no longer found by queries."""
        # Create a route that would cause a violation with a query
        seg1 = _make_segment(5.0, 0.0, 5.0, 10.0, net=1)
        route1 = _make_route([seg1], net=1)
        grid.mark_route(route1)

        # Fill above threshold with other routes so R-tree is consulted
        for i in range(RTREE_SEGMENT_THRESHOLD):
            s = _make_segment(20.0 + i * 0.5, 0.0, 20.0 + i * 0.5, 10.0, net=100 + i)
            grid.mark_route(_make_route([s], net=100 + i))

        # Query near route1 -- should find a violation
        query = _make_segment(5.15, 0.0, 5.15, 10.0, net=2)
        is_valid_before, _, _ = grid.validate_segment_clearance(query, exclude_net=2)
        assert is_valid_before is False

        # Unmark route1
        grid.unmark_route(route1)

        # Now the same query should find no violation from route1
        is_valid_after, _, _ = grid.validate_segment_clearance(query, exclude_net=2)
        # The query should now be valid (route1 was the only nearby obstacle)
        assert is_valid_after is True

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_mark_unmark_mark_cycle(self, grid):
        """Route can be marked, unmarked, and re-marked without index corruption."""
        seg = _make_segment(10.0, 10.0, 15.0, 10.0, net=1)
        route = _make_route([seg], net=1)

        grid.mark_route(route)
        assert grid._seg_rtree_count == 1

        grid.unmark_route(route)
        assert grid._seg_rtree_count == 0

        grid.mark_route(route)
        assert grid._seg_rtree_count == 1


# ---------------------------------------------------------------------------
# Graceful degradation when rtree is unavailable
# ---------------------------------------------------------------------------


class TestRtreeFallback:
    """Verify graceful degradation when rtree import fails."""

    def test_fallback_when_rtree_unavailable(self, rules):
        """Grid works correctly when _rtree_available is False."""
        grid = RoutingGrid(width=50.0, height=50.0, rules=rules)
        grid._rtree_available = False

        # Add routes and validate -- should use brute-force
        seg1 = _make_segment(5.0, 0.0, 5.0, 10.0, net=1)
        route1 = _make_route([seg1], net=1)
        grid.mark_route(route1)

        # R-tree count stays 0 because _rtree_available was False before mark
        # (the insert is a no-op when rtree unavailable)
        # Re-create grid with forced unavailability from start
        grid2 = RoutingGrid(width=50.0, height=50.0, rules=rules)
        grid2._rtree_available = False

        seg1b = _make_segment(5.0, 0.0, 5.0, 10.0, net=1)
        route1b = _make_route([seg1b], net=1)
        grid2.mark_route(route1b)

        query = _make_segment(5.15, 0.0, 5.15, 10.0, net=2)
        is_valid, clearance, loc = grid2.validate_segment_clearance(query, exclude_net=2)
        assert is_valid is False
        assert clearance < 0.127

    def test_import_failure_pattern(self, rules):
        """Mock rtree import failure and verify brute-force still works."""
        grid = RoutingGrid(width=50.0, height=50.0, rules=rules)
        # Simulate rtree not being available
        grid._rtree_available = False
        grid._seg_rtree = {}
        grid._seg_rtree_items = {}
        grid._seg_rtree_count = 0

        seg1 = _make_segment(10.0, 5.0, 20.0, 5.0, net=1)
        route1 = _make_route([seg1], net=1)
        grid.mark_route(route1)

        # Clearance query should succeed via brute-force
        query = _make_segment(15.0, 5.1, 18.0, 5.1, net=2)
        is_valid, clearance, loc = grid.validate_segment_clearance(query, exclude_net=2)
        assert isinstance(is_valid, bool)
        assert isinstance(clearance, float)


# ---------------------------------------------------------------------------
# Segment envelope calculation
# ---------------------------------------------------------------------------


class TestSegmentEnvelope:
    """Tests for the static _segment_envelope method."""

    def test_horizontal_segment(self):
        """Horizontal segment envelope is correct."""
        seg = _make_segment(1.0, 5.0, 10.0, 5.0, width=0.4)
        env = RoutingGrid._segment_envelope(seg)
        assert env == pytest.approx((0.8, 4.8, 10.2, 5.2))

    def test_vertical_segment(self):
        """Vertical segment envelope is correct."""
        seg = _make_segment(5.0, 1.0, 5.0, 10.0, width=0.2)
        env = RoutingGrid._segment_envelope(seg)
        assert env == pytest.approx((4.9, 0.9, 5.1, 10.1))

    def test_diagonal_segment(self):
        """Diagonal segment envelope includes half-width expansion."""
        seg = _make_segment(0.0, 0.0, 10.0, 10.0, width=0.6)
        env = RoutingGrid._segment_envelope(seg)
        assert env == pytest.approx((-0.3, -0.3, 10.3, 10.3))

    def test_zero_length_segment(self):
        """Zero-length segment still has envelope from width."""
        seg = _make_segment(5.0, 5.0, 5.0, 5.0, width=1.0)
        env = RoutingGrid._segment_envelope(seg)
        assert env == pytest.approx((4.5, 4.5, 5.5, 5.5))


# ---------------------------------------------------------------------------
# Threshold constant tests
# ---------------------------------------------------------------------------


class TestRtreeThreshold:
    """Tests for the RTREE_SEGMENT_THRESHOLD constant."""

    def test_threshold_is_positive_integer(self):
        """Threshold is a reasonable positive integer."""
        assert isinstance(RTREE_SEGMENT_THRESHOLD, int)
        assert RTREE_SEGMENT_THRESHOLD > 0

    def test_threshold_value(self):
        """Threshold matches the documented value."""
        assert RTREE_SEGMENT_THRESHOLD == 32


# ---------------------------------------------------------------------------
# Performance benchmark
# ---------------------------------------------------------------------------


def _measure_segment_rtree_speedup(
    rules, n_trials: int = 5, n_iters: int = 1
) -> tuple[float, float, float]:
    """Measure the R-tree/brute-force segment-clearance speedup robustly.

    Builds a ~200-segment grid once, then runs ``n_trials`` *interleaved*
    (R-tree, brute-force) measurement rounds -- each round times
    ``n_iters`` full passes over the 100 queries -- and takes the
    **minimum** wall-clock for each path.

    Interleaving means a transient CPU-contention spike (xdist sibling
    workers, shared CI-runner neighbors) tends to hit both paths in the
    same round rather than biasing one; taking the min discards
    scheduler-noise-polluted rounds, since the minimum is the best
    estimator of true cost for a deterministic micro-benchmark.  The
    original single ``time.perf_counter()`` pair had no such protection
    and flaked when the R-tree block happened to land in a scheduler
    contention window (issue #3949).

    Returns:
        (speedup, rtree_min, brute_min) where ``speedup = brute_min /
        rtree_min``.
    """
    grid = RoutingGrid(width=100.0, height=100.0, rules=rules)

    # Populate with ~200 segments across 40 nets
    rng = random.Random(42)
    for i in range(40):
        net = i + 1
        segs = []
        x = rng.uniform(5.0, 90.0)
        y = rng.uniform(5.0, 90.0)
        for _ in range(5):
            x2 = max(2.0, min(98.0, x + rng.uniform(-8.0, 8.0)))
            y2 = max(2.0, min(98.0, y + rng.uniform(-8.0, 8.0)))
            segs.append(_make_segment(x, y, x2, y2, net=net))
            x, y = x2, y2
        grid.mark_route(_make_route(segs, net=net))

    assert grid._seg_rtree_count >= 200

    # Prepare queries
    queries = []
    for _ in range(100):
        x1 = rng.uniform(5.0, 95.0)
        y1 = rng.uniform(5.0, 95.0)
        x2 = x1 + rng.uniform(-10.0, 10.0)
        y2 = y1 + rng.uniform(-10.0, 10.0)
        queries.append(_make_segment(x1, y1, x2, y2, net=999))

    saved = grid._seg_rtree_count

    def run_rtree() -> None:
        for q in queries:
            grid.validate_segment_clearance(q, exclude_net=999)

    def run_brute() -> None:
        # Force the count below threshold so the brute-force path runs.
        grid._seg_rtree_count = 0
        try:
            for q in queries:
                grid.validate_segment_clearance(q, exclude_net=999)
        finally:
            grid._seg_rtree_count = saved

    # Warm up -- prime lazy imports / caches for both paths.
    run_rtree()
    run_brute()

    rtree_min = float("inf")
    brute_min = float("inf")
    for _ in range(n_trials):
        rtree_min = min(rtree_min, timeit.timeit(run_rtree, number=n_iters))
        brute_min = min(brute_min, timeit.timeit(run_brute, number=n_iters))

    speedup = brute_min / rtree_min if rtree_min > 0 else float("inf")
    return speedup, rtree_min, brute_min


class TestRtreePerformance:
    """Benchmark: R-tree should be faster than brute-force at scale."""

    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_rtree_faster_at_200_segments(self, rules):
        """Per-PR guard: R-tree path does not regress vs brute-force.

        Uses interleaved min-of-N ``timeit`` trials (see
        ``_measure_segment_rtree_speedup``) so a transient scheduler
        spike cannot bias a single measurement.  At only 200 segments the
        R-tree advantage over O(N) brute-force is shallow, so the per-PR
        bound is a generous, noise-tolerant ``> 0.5`` (runtime at most 2x
        brute-force).  The real speedup grows with N and is asserted
        tightly in the nightly ``slow`` variant below.
        """
        speedup, rtree_min, brute_min = _measure_segment_rtree_speedup(rules)
        # Log for diagnostic visibility even if the assertion passes.
        print(
            f"\nR-tree: {rtree_min:.4f}s, Brute: {brute_min:.4f}s, "
            f"Speedup: {speedup:.2f}x (min of 5 interleaved trials)"
        )
        assert speedup > 0.5, (
            f"R-tree path was unexpectedly slow: {speedup:.2f}x "
            f"(rtree_min={rtree_min:.4f}s, brute_min={brute_min:.4f}s, "
            f"min of 5 interleaved trials).  Expected speedup > 0.5 "
            f"(CI noise budget; see issue #3949)."
        )

    @pytest.mark.slow
    @pytest.mark.skipif(not RTREE_AVAILABLE, reason="rtree not installed")
    def test_rtree_faster_at_200_segments_tight(self, rules):
        """Nightly tight guard: R-tree is meaningfully faster than brute-force.

        Runs only in the nightly ``slow-tests`` lane (and locally via
        ``pytest -m slow``), where interleaved min-of-N sampling makes a
        tighter bound reliable.  Asserts the documented target that the
        R-tree path outpaces brute-force (``speedup > 1.5``).
        """
        speedup, rtree_min, brute_min = _measure_segment_rtree_speedup(rules)
        print(
            f"\nR-tree: {rtree_min:.4f}s, Brute: {brute_min:.4f}s, "
            f"Speedup: {speedup:.2f}x (min of 5 interleaved trials)"
        )
        assert speedup > 1.5, (
            f"R-tree path is not meaningfully faster than brute-force: "
            f"{speedup:.2f}x (rtree_min={rtree_min:.4f}s, "
            f"brute_min={brute_min:.4f}s).  Expected speedup > 1.5."
        )
