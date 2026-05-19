"""Tests for Issue #3077: negotiated rip-up re-validation against
escape-phase vias (post-PR-#3070 lateral via halos).

PR #3006 (Issue #3002) and PR #3019 (Issue #3020) wired the
post-iteration re-validation hooks
:meth:`NegotiatedRouter.find_nets_with_segment_via_violations` and
:meth:`NegotiatedRouter.find_nets_with_via_segment_violations` into
the negotiated and two-phase rip-up loops.  Both hooks consume
``net_routes`` only.

Issue #3077: escape-phase routes (lateral / in-pad rescue helpers
from PR #3070) live in ``self.routes`` but are NEVER folded into
``net_routes`` because ``_escape_pad_overrides`` makes them
non-rippable infrastructure.  As a result, the lateral helper's
off-pad via (board-04 OSC_OUT at PCB ``(125.7875, 121.75)``) was
invisible to the post-iteration hook -- subsequent main-router
segments for BOOT0 / SWDIO / SWCLK / SWO committed on top of the
via halo and produced segment-via clearance violations the
post-route DRC report flagged but the live re-validation hook
never surfaced.

The fix adds an ``extra_routes`` kwarg to both hooks.  Callers
(``Autorouter.route_all_negotiated`` and
``TwoPhaseRouter._detailed_negotiated``) supply the delta between
``self.routes`` and ``net_routes`` (computed via the new
``_collect_extra_routes_for_revalidation`` helper) so the foreign-
via / foreign-segment universe seen by the hooks matches the
universe seen by the pre-commit gate in
``Router._validate_route_clearance``.

These tests pin:

1. The hook detects a segment-vs-via violation where the OFFENDING
   via lives in ``extra_routes`` (not ``net_routes``).
2. The symmetric via-vs-segment hook detects a violation where the
   OFFENDING segment lives in ``extra_routes``.
3. ``extra_routes`` does NOT add segments / vias whose nets are
   eligible to be surfaced as violators -- only the segment / via
   owner from ``net_routes`` can be surfaced.
4. Backward compatibility: omitting ``extra_routes`` (default
   ``None``) yields the pre-#3077 behaviour.
"""

from __future__ import annotations

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP, DesignRules


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


def _make_neg_router(grid: RoutingGrid, rules: DesignRules) -> NegotiatedRouter:
    router = _make_router(grid, rules)
    return NegotiatedRouter(grid, router, rules, DEFAULT_NET_CLASS_MAP)


# ---------------------------------------------------------------------------
# find_nets_with_segment_via_violations + extra_routes
# ---------------------------------------------------------------------------


class TestSegmentVsExtraEscapeVia:
    """Pins the board-04 OSC_OUT scenario from Issue #3077."""

    def test_escape_via_clipped_by_main_router_segment_surfaces_main_net(self):
        """The lateral escape via (in ``extra_routes``) is clipped by a
        main-router segment in ``net_routes`` -- the SEGMENT's net
        surfaces as a violator.

        Mirrors the OSC_OUT escape via at ``(125.7875, 121.75)`` whose
        halo clips BOOT0 / SWDIO / SWCLK / SWO main-router segments.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)

        # Net A (main router): segment running through (5.0, 5.0) on B.Cu.
        seg_a = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1, net_name="BOOT0",
        )
        net_routes = {
            1: [Route(net=1, net_name="BOOT0", segments=[seg_a], vias=[])],
        }

        # Net B (escape phase): a lateral via centered on net A's segment.
        # This via lives in self.routes but NOT in net_routes -- emulating
        # the lateral-helper output from PR #3070.
        via_b = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="OSC_OUT",
        )
        extra_routes = [
            Route(net=2, net_name="OSC_OUT", segments=[], vias=[via_b]),
        ]

        # Without extra_routes, the hook can't see the escape via and
        # reports no violation.
        violators_no_extra = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15
        )
        assert 1 not in violators_no_extra, (
            "Baseline: extra_routes=None must NOT see the escape via "
            "(this is the bug the issue describes)."
        )

        # With extra_routes, the hook surfaces net A (the segment's net)
        # for re-routing.
        violators = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, extra_routes=extra_routes,
        )
        assert 1 in violators, (
            "Fix: extra_routes=[escape] surfaces the main-router net "
            "whose segment clips the escape via."
        )
        # The escape via's net is NOT surfaced -- it's non-rippable.
        assert 2 not in violators

    def test_same_net_filtering_with_extra_routes(self):
        """A segment in ``net_routes`` and a via in ``extra_routes`` that
        share a net are NOT a violation -- the same-net convention
        applies to the extra-routes universe too.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1,  # SAME net as seg.
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg], vias=[])],
        }
        extra_routes = [
            Route(net=1, net_name="A", segments=[], vias=[via]),
        ]
        violators = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, extra_routes=extra_routes,
        )
        assert violators == []

    def test_extra_routes_empty_list_is_noop(self):
        """``extra_routes=[]`` is identical to ``extra_routes=None`` --
        the foreign universe is unchanged.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)
        seg_a = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via_b = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        stub_b = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg_a], vias=[])],
            2: [Route(net=2, net_name="B", segments=[stub_b], vias=[via_b])],
        }
        baseline = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15,
        )
        with_empty = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, extra_routes=[],
        )
        assert set(baseline) == set(with_empty)

    def test_cache_invalidates_when_extra_routes_added(self):
        """Regression: a prior call WITHOUT ``extra_routes`` must NOT
        leak its result to a subsequent call WITH ``extra_routes`` under
        the same nominal ``cache_key``.

        This is the bug surfaced by board-04's two-phase end-of-iteration
        capture: the initial call (``cache_key=("two_phase_init",)``)
        ran before extra_routes was wired, was cached with the same key,
        then the mid-iter call (also ``("two_phase_init",)`` on
        iteration 1) hit the cache and returned a stale empty list even
        though escape vias were now in the universe.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)

        seg_a = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1, net_name="BOOT0",
        )
        net_routes = {
            1: [Route(net=1, net_name="BOOT0", segments=[seg_a], vias=[])],
        }
        via_b = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="OSC_OUT",
        )
        extra_routes = [
            Route(net=2, net_name="OSC_OUT", segments=[], vias=[via_b]),
        ]

        # First call: no extra_routes -- no violations seen, cache stores [].
        empty = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, cache_key=("k",),
        )
        assert empty == []

        # Second call: SAME cache_key but extra_routes provided -- the
        # cache MUST be invalidated by the extra_routes discriminator so
        # the violator surfaces.
        violators = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, cache_key=("k",),
            extra_routes=extra_routes,
        )
        assert 1 in violators, (
            "Cache must not leak the no-extra-routes empty result into "
            "a with-extra-routes call under the same nominal key."
        )

        # Third call: same extra_routes -- cache should hit and return the
        # same answer.
        violators2 = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, cache_key=("k",),
            extra_routes=extra_routes,
        )
        assert set(violators) == set(violators2)

    def test_cache_invalidates_via_seg_too(self):
        """Mirror of :meth:`test_cache_invalidates_when_extra_routes_added`
        for the symmetric :meth:`find_nets_with_via_segment_violations`
        hook.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)

        via_a = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1, net_name="BOOT0",
        )
        stub_a = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=1, net_name="BOOT0",
        )
        net_routes = {
            1: [Route(net=1, net_name="BOOT0", segments=[stub_a], vias=[via_a])],
        }
        seg_b = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=2, net_name="SWDIO",
        )
        extra_routes = [
            Route(net=2, net_name="SWDIO", segments=[seg_b], vias=[]),
        ]
        empty = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("k",),
        )
        assert empty == []
        violators = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("k",),
            extra_routes=extra_routes,
        )
        assert 1 in violators

    def test_extra_route_vias_do_not_self_violate(self):
        """A via in ``extra_routes`` whose net only appears in
        ``extra_routes`` (not in ``net_routes``) is NEVER surfaced as a
        violator -- its segments aren't walked.  This is the "escape
        infrastructure is non-rippable" invariant.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)

        # net_routes is empty -- no main-router progress yet.
        net_routes: dict[int, list[Route]] = {}

        # Two escape routes with vias that would clip each other (net 1
        # via at (5,5), net 2 via at (5.1, 5)) -- but neither is
        # main-routed, so neither can be surfaced.
        via1 = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        via2 = Via(
            x=5.1, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2,
        )
        seg1 = Segment(
            x1=5.0, y1=5.0, x2=6.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        seg2 = Segment(
            x1=5.1, y1=5.0, x2=6.1, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=2,
        )
        extra_routes = [
            Route(net=1, net_name="A", segments=[seg1], vias=[via1]),
            Route(net=2, net_name="B", segments=[seg2], vias=[via2]),
        ]
        violators = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15, extra_routes=extra_routes,
        )
        # Nothing in net_routes -> nothing to walk for segments -> empty.
        assert violators == []


# ---------------------------------------------------------------------------
# find_nets_with_via_segment_violations + extra_routes
# ---------------------------------------------------------------------------


class TestViaVsExtraEscapeSegment:
    """Symmetric sibling of TestSegmentVsExtraEscapeVia."""

    def test_main_via_clipping_extra_escape_segment_surfaces_via_net(self):
        """A main-router via clips an escape-phase segment (in
        ``extra_routes``) -- the VIA's net surfaces as a violator.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)

        # Net A (main router): a through-hole via at (5, 5).
        via_a = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1, net_name="BOOT0",
        )
        # And a tiny stub so net_routes has something to iterate.
        stub_a = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=1, net_name="BOOT0",
        )
        net_routes = {
            1: [Route(net=1, net_name="BOOT0", segments=[stub_a], vias=[via_a])],
        }

        # Net B (escape phase): a segment running through (5, 5) on B.Cu.
        seg_b = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=2, net_name="SWDIO",
        )
        extra_routes = [
            Route(net=2, net_name="SWDIO", segments=[seg_b], vias=[]),
        ]

        # Without extra_routes the escape segment is invisible.
        violators_no_extra = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        )
        assert 1 not in violators_no_extra

        # With extra_routes the via's net surfaces.
        violators = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, extra_routes=extra_routes,
        )
        assert 1 in violators
        assert 2 not in violators  # Escape segment owner -- not rippable.

    def test_extra_routes_empty_list_is_noop_via_seg(self):
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = _make_neg_router(grid, rules)
        via_a = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1,
        )
        stub_a = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=1,
        )
        seg_b = Segment(
            x1=0.0, y1=2.0, x2=10.0, y2=2.0,
            width=0.2, layer=Layer.B_CU, net=2,
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[stub_a], vias=[via_a])],
            2: [Route(net=2, net_name="B", segments=[seg_b], vias=[])],
        }
        baseline = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15,
        )
        with_empty = neg.find_nets_with_via_segment_violations(
            net_routes, trace_clearance=0.15, extra_routes=[],
        )
        assert set(baseline) == set(with_empty)


# ---------------------------------------------------------------------------
# Autorouter._collect_extra_routes_for_revalidation
# ---------------------------------------------------------------------------


class TestCollectExtraRoutesHelper:
    """Pins the helper on :class:`Autorouter` and :class:`TwoPhaseRouter`
    that materialises the delta between ``self.routes`` and ``net_routes``.
    """

    def test_returns_routes_not_in_net_routes(self):
        """Routes appended to ``self.routes`` that are not referenced
        by any ``net_routes`` entry are returned.  This is the typical
        shape after escape-phase routes land before the main router
        starts.
        """
        # Import here so test collection doesn't pay the cost when
        # this module is filtered out.
        from kicad_tools.router.core import Autorouter

        # Construct a minimal Autorouter shape: we only need ``self.routes``
        # and the helper, which doesn't touch any other state.
        ar = Autorouter.__new__(Autorouter)
        seg_escape = Segment(
            x1=0.0, y1=0.0, x2=1.0, y2=0.0,
            width=0.2, layer=Layer.F_CU, net=10,
        )
        seg_main = Segment(
            x1=5.0, y1=5.0, x2=6.0, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=20,
        )
        route_escape = Route(net=10, net_name="ESC", segments=[seg_escape], vias=[])
        route_main = Route(net=20, net_name="MAIN", segments=[seg_main], vias=[])
        ar.routes = [route_escape, route_main]
        net_routes = {20: [route_main]}

        extras = ar._collect_extra_routes_for_revalidation(net_routes)
        assert extras == [route_escape]

    def test_returns_empty_when_routes_empty(self):
        from kicad_tools.router.core import Autorouter
        ar = Autorouter.__new__(Autorouter)
        ar.routes = []
        assert ar._collect_extra_routes_for_revalidation({}) == []

    def test_returns_empty_when_all_routes_tracked(self):
        from kicad_tools.router.core import Autorouter
        ar = Autorouter.__new__(Autorouter)
        seg = Segment(
            x1=0.0, y1=0.0, x2=1.0, y2=0.0,
            width=0.2, layer=Layer.F_CU, net=5,
        )
        route = Route(net=5, net_name="A", segments=[seg], vias=[])
        ar.routes = [route]
        net_routes = {5: [route]}
        assert ar._collect_extra_routes_for_revalidation(net_routes) == []

    def test_id_based_membership_not_equality(self):
        """Membership is tested by ``id()`` so two distinct Route
        objects with identical contents are NOT collapsed.  This
        matters when the rip-up flow creates a fresh Route object
        with the same net + segments as an escape stub.
        """
        from kicad_tools.router.core import Autorouter
        ar = Autorouter.__new__(Autorouter)
        seg = Segment(
            x1=0.0, y1=0.0, x2=1.0, y2=0.0,
            width=0.2, layer=Layer.F_CU, net=5,
        )
        route_a = Route(net=5, net_name="A", segments=[seg], vias=[])
        # Build an identical-content but distinct Route.
        route_b = Route(net=5, net_name="A", segments=[seg], vias=[])
        ar.routes = [route_a, route_b]
        # net_routes references route_b only.
        net_routes = {5: [route_b]}
        extras = ar._collect_extra_routes_for_revalidation(net_routes)
        assert extras == [route_a]
        # id() identification: route_a survives because it's not the
        # SAME object as route_b, even though their fields match.

    def test_two_phase_helper_parity(self):
        """``TwoPhaseRouter._collect_extra_routes_for_revalidation``
        is a bit-for-bit mirror of the Autorouter helper.
        """
        from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter

        tp = TwoPhaseRouter.__new__(TwoPhaseRouter)
        seg_escape = Segment(
            x1=0.0, y1=0.0, x2=1.0, y2=0.0,
            width=0.2, layer=Layer.F_CU, net=10,
        )
        seg_main = Segment(
            x1=5.0, y1=5.0, x2=6.0, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=20,
        )
        route_escape = Route(net=10, net_name="ESC", segments=[seg_escape], vias=[])
        route_main = Route(net=20, net_name="MAIN", segments=[seg_main], vias=[])
        tp.routes = [route_escape, route_main]
        net_routes = {20: [route_main]}

        extras = tp._collect_extra_routes_for_revalidation(net_routes)
        assert extras == [route_escape]
