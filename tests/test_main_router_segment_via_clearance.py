"""Tests for Issue #3002: main-router segment-vs-foreign-via clearance gate
and the negotiated post-iteration re-validation hook.

Issue #3002 is the main-router follow-up to PR #2999 (escape-time
gate, Issue #2998).  PR #2999 added
``EscapeRouter._segment_clears_foreign_via`` as a static-method
predicate inside the escape phase only.  The MAIN router commits
segments without that predicate, so cross-net ordering bugs in the
negotiated rip-up loop -- net A's segment commits BEFORE net B's via
lands in the same iteration -- slip past the pre-commit clearance
gate (which walks ``grid.routes`` and only sees vias already
committed at validation time).

Concrete failure these tests reproduce:
    PCB (143.8, 119.7) B.Cu on board-04 -- SWDIO segment clips the
    BOOT0 in-pad via by -0.075 mm.  The C++ post-route validator at
    ``cpp/src/grid.cpp:510-536`` (block 1c) catches it post-hoc but
    only after the bad geometry is on the board.

The fix has two parts:

1. ``Router.set_segment_foreign_context()`` lets the autorouter push
   foreign-net vias into the router so ``_validate_route_clearance``
   can use the shared :func:`segment_clears_foreign_via` predicate
   (STANDARD threshold) BEFORE committing the candidate segment.

2. ``NegotiatedRouter.find_nets_with_segment_via_violations()``
   walks every committed segment against every foreign-net via at
   the end of each negotiated iteration and feeds violators back
   into ``nets_to_reroute`` -- converting the post-commit validator
   from advisory to live.
"""

from __future__ import annotations

import pytest

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
    """Construct a bare ``Router`` for unit-testing the foreign-context
    setter and the validator hook -- we only need it instantiable, the
    A* search itself is exercised by other test suites.
    """
    return Router(grid, rules)


# ---------------------------------------------------------------------------
# Router.set_segment_foreign_context()
# ---------------------------------------------------------------------------


class TestSetSegmentForeignContext:
    """Unit tests for the new setter on ``pathfinder.Router``."""

    def test_default_state_is_empty(self):
        """Before the setter is called, the context list is empty so
        behavior matches pre-#3002 (no extra rejections)."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = _make_router(grid, rules)
        assert router._foreign_vias == []

    def test_populates_via_list(self):
        """A list of vias is stored verbatim for downstream consumption."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = _make_router(grid, rules)
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=42,
        )
        router.set_segment_foreign_context(foreign_vias=[via])
        assert len(router._foreign_vias) == 1
        assert router._foreign_vias[0] is via

    def test_none_clears_context(self):
        """``None`` clears the previously-set foreign-via list."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = _make_router(grid, rules)
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=42,
        )
        router.set_segment_foreign_context(foreign_vias=[via])
        router.set_segment_foreign_context(foreign_vias=None)
        assert router._foreign_vias == []

    def test_setter_invalidates_via_cache(self):
        """Mirrors the cache-invariant rule of
        ``set_via_foreign_context`` -- foreign geometry changes can
        affect downstream cached results, so the via cache is cleared."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = _make_router(grid, rules)
        # Seed a positive cache entry.
        router._via_cache[(0, 0, 1, 3)] = True
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=42,
        )
        router.set_segment_foreign_context(foreign_vias=[via])
        assert router._via_cache == {}


# ---------------------------------------------------------------------------
# _validate_route_clearance consumes the new context
# ---------------------------------------------------------------------------


class TestValidateRouteClearanceWithForeignContext:
    """The pre-commit validator must reject a candidate route whose
    segment clips a foreign-net via pushed via the new setter."""

    def test_passes_when_foreign_context_empty(self):
        """Default-empty context preserves pre-#3002 behavior."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = _make_router(grid, rules)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=5,
        )
        route = Route(net=5, net_name="SWDIO", segments=[seg], vias=[])
        assert router._validate_route_clearance(route, exclude_net=5) is True

    def test_rejects_when_foreign_via_clips_segment(self):
        """Board-04 SWDIO/BOOT0 case: a foreign via on the segment's
        centerline at less than (via_radius + half_width + clearance)
        triggers rejection."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = _make_router(grid, rules)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=5,
        )
        route = Route(net=5, net_name="SWDIO", segments=[seg], vias=[])
        # Centered on segment centerline -- worst case.
        foreign_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=99,
        )
        router.set_segment_foreign_context(foreign_vias=[foreign_via])
        assert router._validate_route_clearance(route, exclude_net=5) is False

    def test_ignores_same_net_via(self):
        """A via on the SAME net as the segment is filtered out."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = _make_router(grid, rules)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=5,
        )
        route = Route(net=5, net_name="SWDIO", segments=[seg], vias=[])
        same_net_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=5,  # SAME net.
        )
        router.set_segment_foreign_context(foreign_vias=[same_net_via])
        assert router._validate_route_clearance(route, exclude_net=5) is True

    def test_layer_filter_skips_non_overlapping_via(self):
        """A B.Cu segment ignores a foreign via stopping at In1.Cu."""
        rules = _make_rules()
        grid = _make_grid(rules)
        # 4-layer stack so In1.Cu is meaningful.
        grid = RoutingGrid(
            width=20.0, height=20.0, rules=rules,
            origin_x=0.0, origin_y=0.0,
            layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
        )
        router = _make_router(grid, rules)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=5,
        )
        route = Route(net=5, net_name="SIG", segments=[seg], vias=[])
        # Blind via F.Cu -> In1.Cu (does NOT reach B.Cu).
        foreign_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.IN1_CU), net=99,
        )
        router.set_segment_foreign_context(foreign_vias=[foreign_via])
        assert router._validate_route_clearance(route, exclude_net=5) is True


# ---------------------------------------------------------------------------
# NegotiatedRouter.find_nets_with_segment_via_violations()
# ---------------------------------------------------------------------------


class TestFindNetsWithSegmentViaViolations:
    """Unit tests for the negotiated post-iteration re-validation hook."""

    def _make_neg_router(self, grid, rules):
        # NegotiatedRouter only needs grid + router + rules; no actual
        # A* search runs in these tests.
        router = _make_router(grid, rules)
        return NegotiatedRouter(grid, router, rules, DEFAULT_NET_CLASS_MAP)

    def test_clean_routes_yields_empty(self):
        """No violations -> empty list."""
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(grid, rules)
        # Two nets, geometrically separated.
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
        assert neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15
        ) == []

    def test_detects_segment_clipping_foreign_via(self):
        """Regression: SWDIO/BOOT0 -- the segment of net A clips net
        B's via.  The hook surfaces net A (the segment net) for
        re-routing.

        This is the precise scenario the curator named in the issue:
        net A's segment committed BEFORE net B's via placed, with via
        violating clearance -- the negotiated loop should rip up net A.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(grid, rules)
        # Net A: B.Cu segment running through (5.0, 5.0)
        seg_a = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1, net_name="SWDIO",
        )
        # Net B: through-hole via centered on net A's segment
        via_b = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="BOOT0",
        )
        # The via itself is on a tiny net B stub so it appears in
        # net_routes.
        stub_b = Segment(
            x1=5.0, y1=5.0, x2=5.5, y2=5.0,
            width=0.2, layer=Layer.F_CU, net=2, net_name="BOOT0",
        )
        net_routes = {
            1: [Route(net=1, net_name="SWDIO", segments=[seg_a], vias=[])],
            2: [Route(net=2, net_name="BOOT0", segments=[stub_b], vias=[via_b])],
        }
        violators = neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15
        )
        # Net A's segment violates -- net A surfaces.
        assert 1 in violators
        # Net B's stub is far from any foreign via.
        assert 2 not in violators

    def test_ignores_same_net_via(self):
        """A net's own via clipping its own segment is NOT a violation."""
        rules = _make_rules()
        grid = _make_grid(rules)
        neg = self._make_neg_router(grid, rules)
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0,
            width=0.2, layer=Layer.B_CU, net=1,
        )
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1,  # SAME net.
        )
        net_routes = {
            1: [Route(net=1, net_name="A", segments=[seg], vias=[via])],
        }
        assert neg.find_nets_with_segment_via_violations(
            net_routes, trace_clearance=0.15
        ) == []
