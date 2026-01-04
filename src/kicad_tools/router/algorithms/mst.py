"""Minimum Spanning Tree based routing algorithm.

This module provides MST-based net routing that minimizes total wirelength
by connecting pads in order of shortest Manhattan distance.
"""

from __future__ import annotations

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
    ) -> list[Route]:
        """Route a net using MST ordering.

        Args:
            pad_objs: List of Pad objects to connect
            mark_route_callback: Callback to mark a route on the grid

        Returns:
            List of successfully created routes
        """
        if len(pad_objs) < 2:
            return []

        routes: list[Route] = []

        if len(pad_objs) > 2:
            # Build and sort MST edges by length
            mst_edges = self.build_mst(pad_objs)
            mst_edges.sort(
                key=lambda e: abs(pad_objs[e[0]].x - pad_objs[e[1]].x)
                + abs(pad_objs[e[0]].y - pad_objs[e[1]].y)
            )

            for i, j in mst_edges:
                source_pad = pad_objs[i]
                target_pad = pad_objs[j]
                route = self.router.route(source_pad, target_pad)

                if route:
                    mark_route_callback(route)
                    routes.append(route)
        else:
            # Simple 2-pin net
            route = self.router.route(pad_objs[0], pad_objs[1])
            if route:
                mark_route_callback(route)
                routes.append(route)

        return routes

    def route_net_star(
        self,
        pad_objs: list[Pad],
        mark_route_callback: callable,
    ) -> list[Route]:
        """Route a net using star topology from the first pad.

        Args:
            pad_objs: List of Pad objects to connect
            mark_route_callback: Callback to mark a route on the grid

        Returns:
            List of successfully created routes
        """
        if len(pad_objs) < 2:
            return []

        routes: list[Route] = []
        first_pad = pad_objs[0]

        for i in range(1, len(pad_objs)):
            target_pad = pad_objs[i]
            route = self.router.route(first_pad, target_pad)

            if route:
                mark_route_callback(route)
                routes.append(route)

        return routes
