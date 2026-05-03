"""Regression tests for via-vs-via failure-reason propagation (Issue #2476).

These tests verify that the C++ pathfinder surfaces a structured
``FAILURE_VIA_VIA_BLOCKED`` reason (along with the offending stored-via
net id) when its A* expansion is unable to place a via because of the
geometric clearance check added in Issue #2466.  The negotiated strategy
uses this diagnostic to dispatch a targeted rip-up at the specific
blocker rather than blanket retry.

Background
==========

PR #2472 (Issue #2466) made ``Pathfinder::is_via_blocked`` honor the
post-route validator's via-vs-via clearance rule.  The check is correct
(refuses placements the validator would flag), but board 02
(charlieplex_3x3) regressed from 7/8 (DRC-failing) to 6/8 (DRC-clean):
the negotiated strategy could not react to the new rejections because
``RouteResult`` only carried a boolean ``success``.

Fix surface area:

* C++ ``RouteResult`` gains ``failure_reason``, ``blocking_via_net``,
  ``failure_x``, ``failure_y`` (mirroring the
  ``ValidationResult::violation_type`` numbering, value 5 = via-via).
* C++ ``Pathfinder::is_via_blocked_diag`` overload returns the offending
  stored via id along with the boolean rejection.
* The two A* loops (``route()`` and ``run_astar_loop()``) accumulate
  via-block events and write them into the result on failure.
* Python ``CppPathfinder`` exposes the diagnostic via
  ``get_last_failure_info()``.
* The negotiated strategy records ``(failed_net, blocking_net)`` pairs
  whenever a sub-route fails with the via-vs-via reason and exposes them
  via ``get_and_clear_via_blocking_nets()``.
* New ``via_blocked_ripup`` method on ``NegotiatedRouter`` performs a
  targeted rip-up driven by those pairs.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)


def _make_grid_and_rules(
    width: float = 10.0,
    height: float = 10.0,
    resolution: float = 0.1,
    trace_width: float = 0.25,
    trace_clearance: float = 0.2,
    via_diameter: float = 0.6,
    via_clearance: float = 0.2,
) -> tuple[RoutingGrid, DesignRules]:
    rules = DesignRules(
        trace_width=trace_width,
        trace_clearance=trace_clearance,
        via_diameter=via_diameter,
        via_clearance=via_clearance,
        grid_resolution=resolution,
    )
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    return grid, rules


@requires_cpp
class TestRouteResultFailureReason:
    """RouteResult must carry a structured failure reason on failure (Issue #2476)."""

    def test_failure_reason_constants_exposed(self):
        """The C++ extension exposes FAILURE_* constants for Python dispatch."""
        from kicad_tools.router import router_cpp

        assert hasattr(router_cpp, "FAILURE_NONE")
        assert hasattr(router_cpp, "FAILURE_NO_PATH")
        assert hasattr(router_cpp, "FAILURE_VIA_VIA_BLOCKED")
        # Mirror ValidationResult::violation_type vocabulary -- via-via = 5.
        assert router_cpp.FAILURE_VIA_VIA_BLOCKED == 5
        assert router_cpp.FAILURE_NONE == 0

    def test_route_result_default_failure_reason_is_none(self):
        """A default-constructed RouteResult has failure_reason == FAILURE_NONE."""
        from kicad_tools.router import router_cpp

        r = router_cpp.RouteResult()
        assert r.failure_reason == router_cpp.FAILURE_NONE
        assert r.blocking_via_net == 0
        assert r.failure_x == pytest.approx(0.0)
        assert r.failure_y == pytest.approx(0.0)

    def test_via_blocked_failure_reason_propagates_to_python(self):
        """End-to-end: when the cpp search rejects a via slot due to
        stored-via geometry, ``get_last_failure_info()`` returns a dict
        with ``failure_reason == FAILURE_VIA_VIA_BLOCKED`` and
        ``blocking_via_net`` set to the offending stored-via's net.

        Fixture isolates the geometric branch by:
        - Carpeting the grid with net-2 stored vias on a tight pitch
          (closer than ``via_diameter + via_clearance``) so any via
          candidate is geometrically blocked by at least one of them.
        - NOT marking any grid cells, so the grid-cell blocking check
          inside ``is_via_blocked`` always passes -- the only rejection
          mechanism is the geometric stored-via clearance check that
          mirrors the post-route validator (Issue #2466).

        Note: ``CppPathfinder.route()`` falls back to the pure-Python
        pathfinder when the cpp search fails, and the fallback may still
        find a route.  This test asserts the cpp-side diagnostic via
        ``get_last_failure_info()`` regardless of whether the fallback
        succeeded -- the diagnostic must be captured before any fallback
        is attempted.
        """
        from kicad_tools.router import router_cpp

        # Compact 2mm x 2mm grid so the carpet of stored vias densely
        # covers every reachable via candidate.
        rules = DesignRules(
            trace_width=0.25,
            trace_clearance=0.2,
            via_diameter=0.6,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(
            width=2.0, height=2.0, rules=rules,
            layer_stack=LayerStack.four_layer_all_signal(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Carpet of stored vias on 0.4mm pitch -- well inside the
        # 0.8mm geometric keepout.  We deliberately do NOT mark grid
        # cells, so any cell-based check passes.
        net2 = 2
        for i in range(6):
            for j in range(6):
                x = i * 0.4
                y = j * 0.4
                cpp_grid._impl.add_stored_via(
                    x, y, 0.3, rules.via_diameter, net2,
                )

        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        # Force the search to attempt a via: start on F_CU, end on B_CU.
        start = Pad(
            x=0.2, y=0.2, width=0.2, height=0.2,
            layer=Layer.F_CU, net=1, net_name="N1",
        )
        end = Pad(
            x=1.8, y=1.8, width=0.2, height=0.2,
            layer=Layer.B_CU, net=1, net_name="N1",
        )

        # The Python fallback may still find a path -- we don't assert on
        # the route() return.  We DO assert that the cpp-side diagnostic
        # was captured via _capture_failure_info before the fallback ran.
        pathfinder.route(start, end, negotiated_mode=False)

        info = pathfinder.get_last_failure_info()
        assert info is not None, (
            "cpp search failed but get_last_failure_info() returned None -- "
            "_capture_failure_info should have recorded the diagnostic"
        )
        assert info["failure_reason"] == router_cpp.FAILURE_VIA_VIA_BLOCKED, (
            f"Expected FAILURE_VIA_VIA_BLOCKED ({router_cpp.FAILURE_VIA_VIA_BLOCKED}), "
            f"got {info['failure_reason']}"
        )
        assert info["blocking_via_net"] == net2, (
            f"Expected blocking_via_net == {net2}, "
            f"got {info['blocking_via_net']}"
        )
        # Failure coordinates should be non-zero (a real candidate was
        # rejected somewhere on the board).
        assert info["failure_x"] != 0.0 or info["failure_y"] != 0.0, (
            f"failure_x/_y must record where the candidate was rejected, "
            f"got ({info['failure_x']}, {info['failure_y']})"
        )

    def test_failure_info_cleared_on_success(self):
        """A successful route() clears any previous failure diagnostic."""
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        # Trivial 2-pad route that should succeed without vias.
        start = Pad(
            x=1.0, y=1.0, width=0.4, height=0.4,
            layer=Layer.F_CU, net=1, net_name="N1",
        )
        end = Pad(
            x=3.0, y=1.0, width=0.4, height=0.4,
            layer=Layer.F_CU, net=1, net_name="N1",
        )

        route = pathfinder.route(start, end)
        assert route is not None
        # On success, failure-info is left at its previous value (None
        # initially, since route() resets it at the start of every call).
        # This guarantees stale diagnostics do not leak into the next
        # failed route().
        assert pathfinder.get_last_failure_info() is None

    def test_failure_info_python_router_returns_none(self):
        """The Python pathfinder's get_last_failure_info() returns None.

        This is the API parity contract -- the negotiated strategy treats
        ``None`` as "no actionable diagnostic" and falls back to its
        existing rip-up logic, so the Python-only path is unaffected.
        """
        from kicad_tools.router.pathfinder import Router

        grid, rules = _make_grid_and_rules()
        py_router = Router(grid, rules)
        assert py_router.get_last_failure_info() is None


@requires_cpp
class TestNegotiatedRouterViaBlockedRetry:
    """The negotiated strategy must consume the via-blocked diagnostic
    and dispatch a targeted rip-up of the specific blocker (Issue #2476)."""

    def test_record_via_blocked_failure_captures_pair(self):
        """``_record_via_blocked_failure`` stores (failed_net, blocking_net)
        when the underlying router reports FAILURE_VIA_VIA_BLOCKED.
        """
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        grid, rules = _make_grid_and_rules()

        # Mock router that reports a via-vs-via failure.
        class FakeRouter:
            def get_last_failure_info(self):
                return {
                    "failure_reason": NegotiatedRouter._FAILURE_VIA_VIA_BLOCKED,
                    "blocking_via_net": 7,
                    "failure_x": 1.0,
                    "failure_y": 2.0,
                }

        neg = NegotiatedRouter(grid, FakeRouter(), rules, net_class_map={})
        neg._record_via_blocked_failure(failed_net=3)

        pairs = neg.get_and_clear_via_blocking_nets()
        assert pairs == {(3, 7)}

        # Drain semantics: a second drain returns empty.
        assert neg.get_and_clear_via_blocking_nets() == set()

    def test_record_via_blocked_failure_ignores_non_via_failures(self):
        """Failures with other reasons are NOT recorded as via-blocking.

        This avoids polluting the targeted-ripup queue with rejections
        that have nothing to do with stored-via geometry (e.g. plain
        no-path failures, grid-cell rejections, or Python-fallback
        failures with no diagnostic).
        """
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        grid, rules = _make_grid_and_rules()

        class FakeRouter:
            info = None

            def get_last_failure_info(self):
                return self.info

        fr = FakeRouter()
        neg = NegotiatedRouter(grid, fr, rules, net_class_map={})

        # Case 1: None
        fr.info = None
        neg._record_via_blocked_failure(failed_net=1)
        assert neg.get_and_clear_via_blocking_nets() == set()

        # Case 2: NO_PATH (different reason)
        fr.info = {
            "failure_reason": 1,  # FAILURE_NO_PATH
            "blocking_via_net": 0,
            "failure_x": 0.0,
            "failure_y": 0.0,
        }
        neg._record_via_blocked_failure(failed_net=1)
        assert neg.get_and_clear_via_blocking_nets() == set()

        # Case 3: VIA_VIA_BLOCKED but blocking_net == 0
        fr.info = {
            "failure_reason": NegotiatedRouter._FAILURE_VIA_VIA_BLOCKED,
            "blocking_via_net": 0,
            "failure_x": 0.0,
            "failure_y": 0.0,
        }
        neg._record_via_blocked_failure(failed_net=1)
        assert neg.get_and_clear_via_blocking_nets() == set()

        # Case 4: blocking_net == failed_net (same-net spacing, irrelevant)
        fr.info = {
            "failure_reason": NegotiatedRouter._FAILURE_VIA_VIA_BLOCKED,
            "blocking_via_net": 1,
            "failure_x": 0.0,
            "failure_y": 0.0,
        }
        neg._record_via_blocked_failure(failed_net=1)
        assert neg.get_and_clear_via_blocking_nets() == set()

    def test_via_blocked_ripup_targets_recorded_pairs(self):
        """``via_blocked_ripup`` rips up exactly the recorded blocking nets
        and re-routes the failed net first.

        Fixture: register two via-blocked failure pairs; verify that the
        method drains them, calls ``rip_up_nets`` with the blockers, and
        attempts to reroute the failed nets afterwards.
        """
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
        from kicad_tools.router.primitives import Route

        grid, rules = _make_grid_and_rules()

        # Track which nets were ripped up and re-routed.
        ripped: list[list[int]] = []
        rerouted: list[int] = []

        class FakeRouter:
            def get_last_failure_info(self):
                return None

            def route(self, *args, **kwargs):
                return None

        # Build a real NegotiatedRouter but override the heavy methods.
        neg = NegotiatedRouter(grid, FakeRouter(), rules, net_class_map={})

        def fake_rip_up_nets(nets, net_routes, routes_list):
            ripped.append(list(nets))
            for n in nets:
                net_routes.pop(n, None)

        def fake_route_net_negotiated(pad_objs, *args, **kwargs):
            net_id = pad_objs[0].net
            rerouted.append(net_id)
            # Net 5 (the failed net) reroutes successfully; others fail
            # so we can verify the resolved-vs-attempted return value.
            if net_id == 5:
                return [Route(net=net_id, net_name=f"N{net_id}")]
            return []

        neg.rip_up_nets = fake_rip_up_nets  # type: ignore
        neg.route_net_negotiated = fake_route_net_negotiated  # type: ignore

        # Seed the via-blocked pairs.
        neg._last_via_blocking_nets = {(5, 9)}

        # Set up minimal pads_by_net so the method has something to
        # invoke route_net_negotiated with.
        pads_by_net: dict[int, list[Pad]] = {
            5: [
                Pad(x=0.5, y=0.5, width=0.4, height=0.4, layer=Layer.F_CU, net=5, net_name="N5"),
                Pad(x=2.0, y=2.0, width=0.4, height=0.4, layer=Layer.F_CU, net=5, net_name="N5"),
            ],
            9: [
                Pad(x=1.0, y=1.0, width=0.4, height=0.4, layer=Layer.F_CU, net=9, net_name="N9"),
                Pad(x=3.0, y=3.0, width=0.4, height=0.4, layer=Layer.F_CU, net=9, net_name="N9"),
            ],
        }

        net_routes: dict[int, list[Route]] = {9: [Route(net=9, net_name="N9")]}
        routes_list: list[Route] = list(net_routes[9])

        resolved, attempted = neg.via_blocked_ripup(
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net=pads_by_net,
            present_cost_factor=1.0,
            mark_route_callback=lambda r: None,
        )

        assert attempted == 1  # one distinct failed net
        assert resolved == 1  # net 5 rerouted successfully

        # The blocker was net 9 -- it must have been ripped up.
        assert ripped == [[9]]

        # Re-route was attempted for the failed net first, then the
        # displaced blocker.
        assert rerouted[0] == 5
        assert 9 in rerouted

        # The internal pairs set is drained (idempotent retry guard).
        assert neg.get_and_clear_via_blocking_nets() == set()

    def test_via_blocked_ripup_respects_ripup_budget(self):
        """``via_blocked_ripup`` honors ``max_ripups_per_net`` so a blocker
        already at its rip-up budget is skipped (prevents infinite churn).
        """
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        grid, rules = _make_grid_and_rules()

        ripped: list[list[int]] = []

        class FakeRouter:
            def get_last_failure_info(self):
                return None

            def route(self, *args, **kwargs):
                return None

        neg = NegotiatedRouter(grid, FakeRouter(), rules, net_class_map={})

        def fake_rip_up_nets(nets, net_routes, routes_list):
            ripped.append(list(nets))

        neg.rip_up_nets = fake_rip_up_nets  # type: ignore
        neg.route_net_negotiated = lambda *a, **k: []  # type: ignore

        neg._last_via_blocking_nets = {(5, 9)}

        # Pre-load ripup_history so net 9 has already hit its budget.
        ripup_history = {9: 3}

        resolved, attempted = neg.via_blocked_ripup(
            net_routes={},
            routes_list=[],
            pads_by_net={},
            present_cost_factor=1.0,
            mark_route_callback=lambda r: None,
            ripup_history=ripup_history,
            max_ripups_per_net=3,
        )

        # The pair was attempted but no rip-up occurred (budget exhausted).
        assert attempted == 1
        assert resolved == 0
        assert ripped == [], (
            "When all blockers are over-budget no rip-up should occur"
        )

    def test_via_blocked_ripup_no_pairs_returns_zero(self):
        """When no via-blocked pairs have been recorded, the method is a no-op."""
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        grid, rules = _make_grid_and_rules()

        class FakeRouter:
            def get_last_failure_info(self):
                return None

        neg = NegotiatedRouter(grid, FakeRouter(), rules, net_class_map={})

        resolved, attempted = neg.via_blocked_ripup(
            net_routes={},
            routes_list=[],
            pads_by_net={},
            present_cost_factor=1.0,
            mark_route_callback=lambda r: None,
        )
        assert resolved == 0
        assert attempted == 0
