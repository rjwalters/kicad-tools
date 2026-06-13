"""Minimum Spanning Tree based routing algorithm.

This module provides MST-based net routing that minimizes total wirelength
by connecting pads in order of shortest Manhattan distance.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..grid import RoutingGrid
    from ..pathfinder import Router
    from ..primitives import Pad, Route
    from ..rules import DesignRules, NetClassRouting


class MSTRouter:
    """MST-based router for multi-pin nets.

    Uses Prim's algorithm to build a minimum spanning tree connecting
    all pads of a net, then routes edges in order of increasing length.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        router: Router,
        rules: DesignRules,
        net_class_map: dict[str, NetClassRouting],
    ):
        """Initialize the MST router.

        Args:
            grid: The routing grid
            router: The pathfinding router
            rules: Design rules
            net_class_map: Net class routing rules
        """
        self.grid = grid
        self.router = router
        self.rules = rules
        self.net_class_map = net_class_map

    def build_mst(self, pad_objs: list[Pad]) -> list[tuple[int, int]]:
        """Build MST edges using Prim's algorithm.

        Args:
            pad_objs: List of Pad objects to connect

        Returns:
            List of (source_idx, target_idx) tuples representing MST edges
        """
        n = len(pad_objs)
        if n < 2:
            return []

        # Prim's algorithm
        connected: set[int] = {0}
        unconnected = set(range(1, n))
        mst_edges: list[tuple[int, int]] = []

        while unconnected:
            best_dist = float("inf")
            best_edge: tuple[int, int] | None = None

            for i in connected:
                for j in unconnected:
                    # Manhattan distance
                    dist = abs(pad_objs[i].x - pad_objs[j].x) + abs(pad_objs[i].y - pad_objs[j].y)
                    if dist < best_dist:
                        best_dist = dist
                        best_edge = (i, j)

            if best_edge:
                i, j = best_edge
                mst_edges.append((i, j))
                connected.add(j)
                unconnected.remove(j)

        return mst_edges

    def route_net(
        self,
        pad_objs: list[Pad],
        mark_route_callback: callable,
        failure_callback: callable | None = None,
        use_steiner: bool = True,
        per_net_timeout: float | None = None,
    ) -> list[Route]:
        """Route a net using MST or RSMT ordering.

        Args:
            pad_objs: List of Pad objects to connect
            mark_route_callback: Callback to mark a route on the grid
            failure_callback: Optional callback to record routing failures.
                Called with (source_pad, target_pad) when routing fails.
            use_steiner: If True, use RSMT decomposition (Steiner tree)
                instead of plain MST for multi-pin nets. Default True.
            per_net_timeout: Issue #3485: optional cumulative wall-clock
                budget (seconds) for the WHOLE net.  A single
                ``time.monotonic()`` deadline is computed before the
                edge loop; each edge's ``self.router.route()`` receives
                the REMAINING budget (so the sum of all edge A* searches
                is bounded by ``per_net_timeout``), and edges that arrive
                after the budget is exhausted are short-circuited (their
                ``failure_callback`` still fires so the rip-up layer sees
                them as failures).  Mirrors
                ``NegotiatedRouter.route_net_negotiated``'s #2769
                bracketing.  ``None`` preserves the pre-#3485 unbudgeted
                behaviour.  Without this, a single pathological
                multi-terminal Steiner net (e.g. softstart's VGATE, 8
                pads) could grind for 20-30 min despite an explicit
                ``--per-net-timeout``, because the per-edge A* calls
                here never received the deadline.

        Returns:
            List of successfully created routes
        """
        if len(pad_objs) < 2:
            return []

        routes: list[Route] = []

        if len(pad_objs) > 2:
            if use_steiner:
                from .steiner import (
                    build_rsmt,
                    make_blocked_cell_predicate,
                    relocate_blocked_point,
                )

                # PR #3481 fix: snap synthetic Steiner branch points
                # onto the routing grid (off-grid virtual pads have no
                # sub-grid rescue and fail ``pin_access``).
                #
                # Issue #3471: additionally relocate branch points that
                # land on cells blocked on EVERY routable layer (net-0
                # obstacles / foreign-net copper).  A blocked virtual
                # pad has no rescue path: every incident A* edge
                # exhaustively fails, burning the per-net budget AND
                # classifying the whole net ``blocked_path`` (board
                # 05's ISENSE cluster: 6 nets x ~80 s of guaranteed
                # failure per route).  Same fix as
                # ``NegotiatedRouter.route_net_negotiated``; MSTRouter
                # is the path the auto-layers two-phase flow actually
                # executes, so it needs the relocation too.
                grid = self.grid
                blocked_fn = make_blocked_cell_predicate(
                    grid, self.rules, pad_objs[0].net
                )

                def snap_fn(x: float, y: float) -> tuple[float, float]:
                    gx, gy = grid.world_to_grid(x, y)
                    if blocked_fn is not None:
                        gx, gy = relocate_blocked_point(gx, gy, blocked_fn)
                    return grid.grid_to_world(gx, gy)

                pad_objs, edges = build_rsmt(pad_objs, snap_fn=snap_fn)
            else:
                # Build and sort MST edges by length
                edges = self.build_mst(pad_objs)
                edges.sort(
                    key=lambda e: abs(pad_objs[e[0]].x - pad_objs[e[1]].x)
                    + abs(pad_objs[e[0]].y - pad_objs[e[1]].y)
                )

            # Issue #3485: cumulative per-net deadline.  ``per_net_timeout``
            # brackets the WHOLE net, not each edge -- so compute one
            # deadline before the loop and hand each edge the remaining
            # budget.  This guarantees the sum of all edge A* searches is
            # bounded by ``per_net_timeout`` and a single grindy edge can
            # no longer consume ``per_net_timeout * len(edges)`` seconds.
            net_deadline = (
                time.monotonic() + per_net_timeout
                if per_net_timeout is not None
                else None
            )

            for i, j in edges:
                source_pad = pad_objs[i]
                target_pad = pad_objs[j]

                if net_deadline is not None:
                    remaining = net_deadline - time.monotonic()
                    if remaining <= 0:
                        # Cumulative budget exhausted before this edge could
                        # be attempted.  Record the failure so the rip-up /
                        # retry layer still sees the edge, then skip it.
                        if failure_callback:
                            failure_callback(source_pad, target_pad)
                        continue
                    edge_timeout: float | None = remaining
                else:
                    edge_timeout = None

                route = self.router.route(
                    source_pad, target_pad, per_net_timeout=edge_timeout
                )

                if route:
                    mark_route_callback(route)
                    routes.append(route)
                elif failure_callback:
                    failure_callback(source_pad, target_pad)
        else:
            # Simple 2-pin net: the whole budget bounds the single A* search.
            route = self.router.route(
                pad_objs[0], pad_objs[1], per_net_timeout=per_net_timeout
            )
            if route:
                mark_route_callback(route)
                routes.append(route)
            elif failure_callback:
                failure_callback(pad_objs[0], pad_objs[1])

        return routes

    def route_net_star(
        self,
        pad_objs: list[Pad],
        mark_route_callback: callable,
        failure_callback: callable | None = None,
        per_net_timeout: float | None = None,
    ) -> list[Route]:
        """Route a net using star topology from the first pad.

        Args:
            pad_objs: List of Pad objects to connect
            mark_route_callback: Callback to mark a route on the grid
            failure_callback: Optional callback to record routing failures.
                Called with (source_pad, target_pad) when routing fails.
            per_net_timeout: Issue #3485: optional cumulative wall-clock
                budget (seconds) for the WHOLE net.  See
                :meth:`route_net` for the bracketing contract.  ``None``
                preserves the pre-#3485 unbudgeted behaviour.

        Returns:
            List of successfully created routes
        """
        if len(pad_objs) < 2:
            return []

        routes: list[Route] = []
        first_pad = pad_objs[0]

        # Issue #3485: cumulative per-net deadline (see route_net above).
        net_deadline = (
            time.monotonic() + per_net_timeout
            if per_net_timeout is not None
            else None
        )

        for i in range(1, len(pad_objs)):
            target_pad = pad_objs[i]

            if net_deadline is not None:
                remaining = net_deadline - time.monotonic()
                if remaining <= 0:
                    if failure_callback:
                        failure_callback(first_pad, target_pad)
                    continue
                edge_timeout: float | None = remaining
            else:
                edge_timeout = None

            route = self.router.route(
                first_pad, target_pad, per_net_timeout=edge_timeout
            )

            if route:
                mark_route_callback(route)
                routes.append(route)
            elif failure_callback:
                failure_callback(first_pad, target_pad)

        return routes
