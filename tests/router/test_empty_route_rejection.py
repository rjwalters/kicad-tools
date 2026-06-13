"""Tests for Issue #2934: empty-Route rejection in negotiated routing.

Background
----------

Issue #2306's incremental-Steiner early-termination
(`pathfinder.py: extra_goal_cells goal-check`) terminates the A* search
the moment ``current`` hits any cell in ``extra_goal_cells`` (the
previously-routed net tree).  When the start pad of a later RSMT edge
shares a grid cell with the previously routed tree, ``current`` IS the
start node on the very first pop, the parent walk yields a 1-element
path, ``_convert_path_to_route`` silently no-ops on ``len(path) < 2``,
and ``_reconstruct_route`` returns a ``Route(segments=[], vias=[])``.

The caller in ``route_net_negotiated`` (``algorithms/negotiated.py``)
used ``if route:`` -- a truthy check on a dataclass that is always
truthy regardless of segment count -- and appended the empty Route
without firing ``failure_callback`` or updating ``routing_failures``.
Downstream connectivity validation then reported the pad as un-routed
("VOUT: 2/3 pads connected" on board 01).

The fix has two layers (defense in depth):

1. ``_reconstruct_route`` returns ``None`` when the resulting Route has
   no segments and no vias (primary fix at the source).
2. ``route_net_negotiated`` guards ``if route and (route.segments or
   route.vias):`` (safety net for any future code-path that might still
   produce an empty Route, e.g., a C++ backend).

These tests verify both layers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import AStarNode, Router
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# Layer 1: ``_reconstruct_route`` rejects empty Routes
# ---------------------------------------------------------------------------


class TestReconstructRouteRejectsEmpty:
    """Issue #2934: ``_reconstruct_route`` returns ``None`` when the A*
    path collapses to a single node (start cell == goal cell).
    """

    @staticmethod
    def _build_pathfinder() -> tuple[Router, Pad, Pad, int]:
        rules = DesignRules(grid_resolution=0.5)
        stack = LayerStack.two_layer()
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=stack,
        )
        pathfinder = Router(grid, rules)
        layer_enum = stack.layers[0].layer_enum
        layer_idx = grid.layer_to_index(layer_enum.value)
        start = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET",
            layer=layer_enum,
        )
        end = Pad(
            x=15.0,
            y=15.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET",
            layer=layer_enum,
        )
        return pathfinder, start, end, layer_idx

    def test_single_node_path_returns_none(self):
        """When ``end_node`` has no parent (single-node path), the
        resulting Route would have ``segments=[]``; ``_reconstruct_route``
        must return ``None`` rather than that empty Route.

        This is the exact condition triggered by Issue #2306's
        early-termination: ``current`` IS the start node on the very
        first A* pop because ``(start.x, start.y, start.layer)`` is in
        ``extra_goal_cells``.  The parent walk yields one element and
        ``_convert_path_to_route`` no-ops.
        """
        pathfinder, start, end, layer_idx = self._build_pathfinder()
        sgx, sgy = pathfinder.grid.world_to_grid(start.x, start.y)
        # Single-node A* result (no parent chain) -- exactly the shape
        # produced by Issue #2306's early-termination when ``current``
        # is the start node.
        end_node = AStarNode(
            f_score=0.0,
            g_score=0.0,
            x=sgx,
            y=sgy,
            layer=layer_idx,
            parent=None,
        )
        result = pathfinder._reconstruct_route(end_node, start, end)
        assert result is None, (
            "Expected _reconstruct_route to return None for a single-node "
            f"path; got Route(segments={result.segments if result else 'N/A'})"
        )

    def test_early_termination_at_start_cell_returns_none(self):
        """End-to-end: route() with ``extra_goal_cells`` containing the
        start cell triggers the early-termination path on the very first
        A* pop.  Before the fix this returned ``Route(segments=[])``;
        after the fix it returns ``None``.
        """
        pathfinder, start, end, layer_idx = self._build_pathfinder()
        sgx, sgy = pathfinder.grid.world_to_grid(start.x, start.y)
        # Seed ``extra_goal_cells`` with the start cell itself: the very
        # first popped node satisfies the early-termination check at
        # `pathfinder.py: if extra_goal_cells and (current.x, current.y,
        # current.layer) in extra_goal_cells:`.
        extra = {(sgx, sgy, layer_idx)}

        route = pathfinder.route(
            start,
            end,
            negotiated_mode=True,
            present_cost_factor=0.0,
            extra_goal_cells=extra,
        )

        # Result must be ``None`` (rejected at source) -- NOT a Route
        # with empty segments.  If a route is returned, it must carry
        # actual geometry (segments or vias).
        if route is not None:
            assert route.segments or route.vias, (
                "Returned Route has no segments and no vias -- this is "
                "the Issue #2934 bug: empty Route silently emitted."
            )


# ---------------------------------------------------------------------------
# Layer 2: ``route_net_negotiated`` rejects empty Routes (defensive)
# ---------------------------------------------------------------------------


def _make_negotiated_router_with_3pad_net() -> tuple[Autorouter, NegotiatedRouter, list[Pad]]:
    """Construct a minimal NegotiatedRouter with one 3-pad net.

    Three pads on the same net forces ``route_net_negotiated`` into the
    RSMT branch (2 edges), matching the board-01 VOUT topology.
    """
    router = Autorouter(width=40.0, height=40.0)
    pads_a = [
        {
            "number": "1",
            "x": 5.0,
            "y": 5.0,
            "width": 0.5,
            "height": 0.5,
            "net": 1,
            "net_name": "VOUT",
        },
        {
            "number": "2",
            "x": 20.0,
            "y": 5.0,
            "width": 0.5,
            "height": 0.5,
            "net": 1,
            "net_name": "VOUT",
        },
        {
            "number": "3",
            "x": 35.0,
            "y": 5.0,
            "width": 0.5,
            "height": 0.5,
            "net": 1,
            "net_name": "VOUT",
        },
    ]
    router.add_component("U_A", pads_a)
    neg_router = NegotiatedRouter(
        grid=router.grid,
        router=router.router,
        rules=router.rules,
        net_class_map={},
    )
    pad_objs = [router.pads[p] for p in router.nets[1]]
    return router, neg_router, pad_objs


class TestNegotiatedRouterEmptyRouteGuard:
    """Issue #2934: even if a Route with empty segments somehow reaches
    the negotiated caller (e.g., from a future C++ backend), it must not
    be appended to ``routes`` and must surface ``failure_callback``.
    """

    def test_empty_route_triggers_failure_callback(self):
        """Patch ``router.route`` to return ``Route(segments=[], vias=[])``
        and verify ``route_net_negotiated`` records the failure rather
        than appending the empty Route.
        """
        _router, neg_router, pad_objs = _make_negotiated_router_with_3pad_net()

        failures: list[tuple[Pad, Pad]] = []

        def failure_cb(src: Pad, dst: Pad) -> None:
            failures.append((src, dst))

        empty_route = Route(net=1, net_name="VOUT", segments=[], vias=[])

        with patch.object(neg_router.router, "route", return_value=empty_route):
            routes = neg_router.route_net_negotiated(
                pad_objs,
                present_cost_factor=0.0,
                mark_route_callback=lambda r: None,
                failure_callback=failure_cb,
            )

        # No empty Routes in the returned list.
        for r in routes:
            assert r.segments or r.vias, (
                "Empty Route appended to routes list -- guard missing in route_net_negotiated."
            )
        # Failure callback fired for the failed edge(s).
        assert len(failures) > 0, (
            "Empty Route was treated as success: failure_callback never "
            "fired and routing_failures will stay empty."
        )

    def test_route_with_only_vias_is_accepted(self):
        """A Route with no segments but at least one via is geometrically
        non-empty (cross-layer connection at a single point).  The
        empty-Route guard must NOT reject it.

        Defensive: guards against an over-aggressive ``not route.segments``
        check that would also reject legitimate single-via routes.
        Uses a 2-pin net path (no RSMT-edge dedup) to isolate the guard
        from the sibling-via dedup logic.
        """
        from kicad_tools.router.primitives import Via

        # 2-pin net: avoids RSMT decomposition and the sibling via-dedup
        # pass, so we can verify the guard in isolation.
        router = Autorouter(width=20.0, height=20.0)
        router.add_component(
            "U_A",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 5.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "SIG",
                },
                {
                    "number": "2",
                    "x": 15.0,
                    "y": 5.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "SIG",
                },
            ],
        )
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pad_objs = [router.pads[p] for p in router.nets[1]]

        via_only_route = Route(
            net=1,
            net_name="SIG",
            segments=[],
            vias=[
                Via(
                    x=5.0,
                    y=5.0,
                    drill=0.35,
                    diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU),
                    net=1,
                    net_name="SIG",
                )
            ],
        )

        failures: list[tuple[Pad, Pad]] = []

        def failure_cb(src: Pad, dst: Pad) -> None:
            failures.append((src, dst))

        with patch.object(neg_router.router, "route", return_value=via_only_route):
            routes = neg_router.route_net_negotiated(
                pad_objs,
                present_cost_factor=0.0,
                mark_route_callback=lambda r: None,
                failure_callback=failure_cb,
            )

        # via-only route should be accepted (not treated as empty).
        assert len(routes) == 1, (
            f"Expected 1 route (via-only), got {len(routes)}.  Guard "
            "incorrectly rejected a Route with vias but no segments."
        )
        assert not failures, (
            "Failure callback fired for a via-only Route: the empty-Route guard is too aggressive."
        )


# ---------------------------------------------------------------------------
# Layer 3: Negative invariant on a real board (board 01)
# ---------------------------------------------------------------------------


def _route_all_negotiated_for_board(pcb_path: str) -> list[Route]:
    """Run a single ``route_all_negotiated`` pass and return the result.

    Lightweight integration helper -- DOES NOT call the full
    ``kct route`` pipeline (manufacturer DRC, escalation, saving).
    """
    from kicad_tools.router.io import load_pcb_for_routing

    router, _net_map = load_pcb_for_routing(
        pcb_path,
        validate_drc=False,
        use_pcb_rules=True,
    )
    return router.route_all_negotiated(max_iterations=3, per_net_timeout=10.0)


class TestNoEmptyRoutesAcrossBoards:
    """Issue #2934 AC #4: assert no Route in ``route_all_negotiated()``
    output has ``len(segments) == 0`` AND ``len(vias) == 0``.

    Belt-and-braces invariant guarding against any future regression
    where an empty Route slips into the result list.
    """

    @pytest.mark.parametrize(
        "board_id",
        [
            "01-voltage-divider",
            # Boards 02-07 take significantly longer to route and the
            # invariant is identical -- the board-01 case is the smallest
            # regression guard sufficient to catch the bug.  Full-fleet
            # parity is verified via ``kct fleet status`` separately
            # (see PR description).
        ],
    )
    def test_no_empty_routes_in_output(self, board_id: str):
        """No Route returned by ``route_all_negotiated`` should be
        geometrically empty (segments=[] AND vias=[]).
        """
        import os

        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        pcb = os.path.join(
            repo_root,
            "boards",
            board_id,
            "output",
            f"{board_id.split('-', 1)[1].replace('-', '_')}.kicad_pcb",
        )
        if not os.path.exists(pcb):
            pytest.skip(f"Board PCB not present: {pcb}")

        routes = _route_all_negotiated_for_board(pcb)
        empties = [r for r in routes if not r.segments and not r.vias]
        assert not empties, (
            f"Found {len(empties)} empty Route(s) in route_all_negotiated() "
            f"output for board {board_id}: this is the Issue #2934 regression."
        )
