"""Tests for Issue #3020: main-router via-vs-foreign-segment clearance.

Issue #3020 is the empirical-AC closer for the four-quadrant
segment/via clearance matrix:

* PR #2999 / Issue #2998 -- escape-phase segment-vs-foreign-via gate.
* PR #3006 / Issue #3002 -- main-router segment-vs-foreign-via gate +
  negotiated post-iteration re-validation hook.
* PR #3019 / Issue #3013 -- escape-phase two-pass commit.
* PR (this) / Issue #3020 -- main-router via-vs-foreign-segment
  post-iteration re-validation hook.

The board-04 SWDIO/BOOT0 violation at PCB (143.8, 119.7) on B.Cu
survives PRs #3006 and #3019 because both PRs gate on SEGMENT commits.
The violation here is a VIA committed by the main router clipping an
already-committed foreign-net escape segment.  Escape segments are
permanent infrastructure (``_escape_pad_overrides``), so the fix MUST
be on the VIA side -- the via's net is the one that re-routes, and A*
finds a different layer-transition point.

Test plan:

* ``TestViaClearsForeignSegment``: predicate correctness (clean pass,
  hard intersection, marginal sub-clearance, layer-span mismatch).
* ``TestFindNetsWithViaSegmentViolations``: hook integration (clean
  pass, board-04 SWDIO/BOOT0 detection, same-net filtering, escape-
  segment-as-foreign filtering).
* ``TestFindNetsWithViaSegmentViolationsPerformance``: cache key
  invalidation, layer-bucket, bbox prefilter.
* ``TestIterationMetricsCombinedViolators``: combined seg-via +
  via-seg participate in the lex tuple comparator.
"""

from __future__ import annotations

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP, DesignRules
from kicad_tools.router.via_clearance import (
    segment_clears_foreign_via,
    via_clears_foreign_segment,
)


def _make_rules() -> DesignRules:
    """DesignRules tuned to mirror board-04's clearance regime."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.two_layer(),
    )


def _make_router(grid: RoutingGrid, rules: DesignRules) -> Router:
    return Router(grid, rules)


# ---------------------------------------------------------------------------
# via_clears_foreign_segment predicate
# ---------------------------------------------------------------------------


class TestViaClearsForeignSegment:
    """Unit tests for the new symmetric predicate."""

    def test_clean_geometry_returns_true(self):
        """A via 5mm from a segment cleanly clears all thresholds."""
        seg = Segment(
            x1=0.0, y1=0.0, x2=10.0, y2=0.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        assert via_clears_foreign_segment(via, seg, trace_clearance=0.15) is True

    def test_hard_intersection_returns_false(self):
        """A via dead-center on a segment is a hard intersection."""
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        assert via_clears_foreign_segment(via, seg, trace_clearance=0.15) is False

    def test_marginal_sub_clearance_returns_false(self):
        """A via at the board-04 violation distance (~0.075mm short of
        the trace_clearance threshold) is flagged.

        Distance: via_radius (0.3) + half_seg_w (0.1) + clearance (0.15)
        = 0.55mm.  At 0.475mm centre-to-centre we are 0.075mm short.
        """
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        # via centre 0.475mm above segment centre => 0.075mm short.
        via = Via(
            x=5.0, y=5.475, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        assert via_clears_foreign_segment(via, seg, trace_clearance=0.15) is False

    def test_hard_intersection_only_admits_marginal(self):
        """With ``hard_intersection_only=True``, the 0.075mm-short via
        is admitted (only NEGATIVE edge-to-edge clearance is flagged).
        """
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.475, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        assert via_clears_foreign_segment(
            via, seg, trace_clearance=0.15, hard_intersection_only=True
        ) is True
        # And a dead-centre via still fails the hard threshold.
        via_overlap = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        assert via_clears_foreign_segment(
            via_overlap, seg, trace_clearance=0.15, hard_intersection_only=True
        ) is False

    def test_layer_mismatch_returns_true(self):
        """A B.Cu segment is invisible to an F.Cu-only via (layers
        F.Cu..F.Cu does not span B.Cu).
        """
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        # F.Cu-only "blind" via -- does not reach B.Cu.
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.F_CU), net=2,
        )
        assert via_clears_foreign_segment(via, seg, trace_clearance=0.15) is True

    def test_symmetric_with_existing_predicate(self):
        """The new predicate is the symmetric mirror of
        ``segment_clears_foreign_via`` -- both functions must return
        the same boolean for the same (via, segment, clearance) input.
        """
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        assert (
            via_clears_foreign_segment(via, seg, trace_clearance=0.15)
            == segment_clears_foreign_via(seg, via, trace_clearance=0.15)
        )


# ---------------------------------------------------------------------------
# NegotiatedRouter.find_nets_with_via_segment_violations
# ---------------------------------------------------------------------------


class TestFindNetsWithViaSegmentViolations:
    """Unit tests for the negotiated post-iteration re-validation hook."""

    def _make_neg_router(self, grid, rules):
        router = _make_router(grid, rules)
        return NegotiatedRouter(grid, router, rules, DEFAULT_NET_CLASS_MAP)

    def test_clean_routes_yields_empty(self):
        """No violations -> empty list."""
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(grid, rules)
        seg_a = Segment(
            x1=0.0, y1=0.0, x2=10.0, y2=0.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        seg_b = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=2,
        )
        via_b = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg_a], vias=[])],
            2: [Route(net=2, net_name="B", segments=[seg_b], vias=[via_b])],
        }
        assert neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15
        ) == []

    def test_detects_via_clipping_foreign_segment(self):
        """Board-04 SWDIO/BOOT0 regression: BOOT0's main-router via
        clips SWDIO's escape segment.  The hook surfaces the VIA's
        net (BOOT0), NOT the segment's net (SWDIO).

        Net attribution invariant from PR #3019 judge: escape segments
        are permanent infrastructure (``_escape_pad_overrides`` makes
        them non-rippable), so the rip-up target must be the via's
        net.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(grid, rules)
        # Net 1 = SWDIO: B.Cu escape segment.
        swdio_seg = Segment(
            x1=140.0, y1=119.7, x2=150.0, y2=119.7,
            width=0.2, layer=Layer.B_CU, net=1, net_name="SWDIO",
        )
        # Net 2 = BOOT0: main-router via on B.Cu clipping SWDIO.
        boot0_via = Via(
            x=143.8, y=119.7, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="BOOT0",
        )
        # Net 2 stub so BOOT0 has at least one route entry.
        boot0_stub = Segment(
            x1=143.8, y1=119.7, x2=144.3, y2=119.7,
            width=0.2, layer=Layer.F_CU, net=2, net_name="BOOT0",
        )
        net_routes = {
            1: [Route(net=1, net_name="SWDIO", segments=[swdio_seg], vias=[])],
            2: [Route(net=2, net_name="BOOT0", segments=[boot0_stub], vias=[boot0_via])],
        }
        violators = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        )
        # PR #3019 invariant: VIA's net (BOOT0) surfaces for re-route.
        assert 2 in violators, (
            "Hook must surface BOOT0 (the via's net) for re-route, "
            "not SWDIO (the escape segment is permanent infrastructure)."
        )
        # SWDIO must NOT surface -- its escape segment is permanent.
        assert 1 not in violators, (
            "SWDIO must NOT be surfaced -- its escape segment is "
            "permanent infrastructure protected by _escape_pad_overrides."
        )

    def test_ignores_same_net_via(self):
        """A via on the SAME net as the foreign segment is filtered."""
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(grid, rules)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via_same_net = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1,  # SAME net.
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg], vias=[via_same_net])],
        }
        assert neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15
        ) == []

    def test_escape_segment_as_foreign_segment(self):
        """Escape segments (committed in escape phase) are visible to
        the hook as foreign segments.  This is the load-bearing
        invariant for issue #3020 -- without it the board-04 violation
        is invisible to the hook.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(grid, rules)
        # Net 1 = SWDIO's escape segment on B.Cu (committed in escape
        # phase, lives in net_routes).
        escape_seg = Segment(
            x1=140.0, y1=120.0, x2=150.0, y2=120.0,
            width=0.2, layer=Layer.B_CU, net=1, net_name="SWDIO",
        )
        # Net 2 = some other net's via clipping it.
        clipping_via = Via(
            x=145.0, y=120.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="BOOT0",
        )
        boot0_stub = Segment(
            x1=145.0, y1=120.0, x2=145.5, y2=120.0,
            width=0.2, layer=Layer.F_CU, net=2, net_name="BOOT0",
        )
        net_routes = {
            1: [Route(net=1, net_name="SWDIO", segments=[escape_seg], vias=[])],
            2: [Route(net=2, net_name="BOOT0", segments=[boot0_stub], vias=[clipping_via])],
        }
        violators = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        )
        assert 2 in violators  # VIA's net surfaces.
        assert 1 not in violators  # Escape segment's net does NOT.


# ---------------------------------------------------------------------------
# Performance optimization invariants (cache, bbox, layer-bucket)
# ---------------------------------------------------------------------------


class TestFindNetsWithViaSegmentViolationsPerformance:
    """Verify the three perf layers mirroring PR #3006:

    1. Per-iteration cache (``cache_key``).
    2. Per-call segment bucketing by layer.
    3. Via-bbox prefilter.
    """

    def _make_neg_router(self, rules, grid):
        router = _make_router(grid, rules)
        return NegotiatedRouter(grid, router, rules, DEFAULT_NET_CLASS_MAP)

    def test_cache_key_returns_same_result_on_hit(self):
        """Same ``cache_key`` -> same result list.  Cache hit must not
        re-walk (verified by mutating ``net_routes`` between calls).
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(rules, grid)
        # Geometry that produces a violation (via 2 clips segment 1).
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        stub = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg], vias=[])],
            2: [Route(net=2, net_name="B", segments=[stub], vias=[via])],
        }
        first = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("iter", 5),
        )
        assert 2 in first
        # Mutate routes -> the violation is GONE but the cache should
        # still return the stale snapshot when called with the same key.
        net_routes[2] = []
        second = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("iter", 5),
        )
        assert first == second  # Cache hit.
        # Distinct cache_key bypasses memo -> recomputes.
        third = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("iter", 6),
        )
        assert third == []

    def test_cache_key_none_disables_memo(self):
        """``cache_key=None`` (default) must compute fresh every call."""
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(rules, grid)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        stub = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg], vias=[])],
            2: [Route(net=2, net_name="B", segments=[stub], vias=[via])],
        }
        first = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        )
        assert 2 in first
        # Mutate; cache_key=None -> recompute -> reflects mutation.
        net_routes[2] = []
        second = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        )
        assert second == []

    def test_bbox_prefilter_skips_distant_segments(self):
        """A segment far from any via is bbox-rejected without an
        exact distance computation.  Correctness side: a clearly-
        distant segment must NOT produce a violator.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(rules, grid)
        # Segment is at y=15 (far from via at y=5).
        far_seg = Segment(
            x1=0.0, y1=15.0, x2=10.0, y2=15.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        stub = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[far_seg], vias=[])],
            2: [Route(net=2, net_name="B", segments=[stub], vias=[via])],
        }
        assert neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        ) == []

    def test_layer_bucket_skips_non_overlapping_segment(self):
        """A blind via (F.Cu..F.Cu) cannot violate a B.Cu segment.
        The layer bucket should never present that pair to the inner
        predicate.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(rules, grid)
        # B.Cu segment with a via dead-centre on it BUT via is F.Cu-only.
        b_cu_seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        f_cu_only_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.F_CU), net=2,
        )
        f_cu_stub = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[b_cu_seg], vias=[])],
            2: [Route(net=2, net_name="B", segments=[f_cu_stub], vias=[f_cu_only_via])],
        }
        assert neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        ) == []


# ---------------------------------------------------------------------------
# Combined seg-via + via-seg in IterationMetrics
# ---------------------------------------------------------------------------


class TestIterationMetricsCombinedViolators:
    """Issue #3020: the lex tuple comparator sums BOTH directions of
    the clearance matrix so a best-state restore preserves a hook-
    driven re-route that fixes EITHER direction.
    """

    def test_combined_clearance_violations_in_lex_tuple(self):
        """An iteration that resolved a via-vs-segment violation
        without changing overflow must beat one with a live violation.
        """
        from kicad_tools.router.core import IterationMetrics

        # Iteration A: clean board (0 violations total).
        a = IterationMetrics(
            iteration=2, routed_count=30, overflow=10, clearance_violations=0,
        )
        # Iteration B: same overflow but 1 live violation (could be
        # either direction -- the count is combined upstream).
        b = IterationMetrics(
            iteration=1, routed_count=30, overflow=10, clearance_violations=1,
        )
        assert a.is_better_than(b)
        assert not b.is_better_than(a)

    def test_default_state_no_violations(self):
        """``IterationMetrics`` default ``clearance_violations=0``
        preserves back-compat (Issue #3002 invariant carried forward).
        """
        from kicad_tools.router.core import IterationMetrics

        m = IterationMetrics(iteration=0, routed_count=5, overflow=0)
        assert m.clearance_violations == 0


# ---------------------------------------------------------------------------
# Smoke test: confirm both hooks coexist without interference
# ---------------------------------------------------------------------------


class TestBothHooksCoexist:
    """Smoke test that both PR #3006's hook and PR #3020's hook can be
    called on the same ``NegotiatedRouter`` without polluting each
    other's caches.
    """

    def _make_neg_router(self, rules, grid):
        router = _make_router(grid, rules)
        return NegotiatedRouter(grid, router, rules, DEFAULT_NET_CLASS_MAP)

    def test_separate_cache_slots(self):
        """Each hook owns its own single-slot cache field; populating
        one must not affect the other.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(rules, grid)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        stub = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg], vias=[])],
            2: [Route(net=2, net_name="B", segments=[stub], vias=[via])],
        }
        neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, cache_key=("iter", 0),
        )
        neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("iter", 0),
        )
        # Each cache field is populated independently.
        assert neg._seg_via_violations_cache is not None
        assert neg._via_seg_violations_cache is not None
        # Cache slots are distinct objects.
        assert (
            neg._seg_via_violations_cache
            is not neg._via_seg_violations_cache
        )
