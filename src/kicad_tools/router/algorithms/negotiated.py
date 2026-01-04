"""Negotiated congestion routing algorithm (PathFinder-style).

This module implements iterative rip-up and reroute with increasing
congestion penalties to resolve routing conflicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..grid import RoutingGrid
    from ..pathfinder import Router
    from ..primitives import Pad, Route
    from ..rules import DesignRules, NetClassRouting


class NegotiatedRouter:
    """PathFinder-style negotiated congestion router.

    Routes all nets with temporary resource sharing allowed,
    then iteratively rips up and reroutes conflicting nets
    with increasing congestion penalties until convergence.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        router: Router,
        rules: DesignRules,
        net_class_map: dict[str, NetClassRouting],
    ):
        """Initialize the negotiated router.

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

    def route_net_negotiated(
        self,
        pad_objs: list[Pad],
        present_cost_factor: float,
        mark_route_callback: callable,
    ) -> list[Route]:
        """Route a single net in negotiated mode.

        Args:
            pad_objs: List of Pad objects to connect
            present_cost_factor: Multiplier for present sharing cost
            mark_route_callback: Callback to mark a route on the grid

        Returns:
            List of routes created
        """
        if len(pad_objs) < 2:
            return []

        routes: list[Route] = []

        if len(pad_objs) > 2:
            # MST-based routing with negotiated mode
            n = len(pad_objs)

            # Build MST using Prim's algorithm
            connected: set[int] = {0}
            unconnected = set(range(1, n))
            mst_edges: list[tuple[int, int]] = []

            while unconnected:
                best_dist = float("inf")
                best_edge: tuple[int, int] | None = None

                for i in connected:
                    for j in unconnected:
                        dist = abs(pad_objs[i].x - pad_objs[j].x) + abs(
                            pad_objs[i].y - pad_objs[j].y
                        )
                        if dist < best_dist:
                            best_dist = dist
                            best_edge = (i, j)

                if best_edge:
                    i, j = best_edge
                    mst_edges.append((i, j))
                    connected.add(j)
                    unconnected.remove(j)

            # Sort edges by length
            mst_edges.sort(
                key=lambda e: abs(pad_objs[e[0]].x - pad_objs[e[1]].x)
                + abs(pad_objs[e[0]].y - pad_objs[e[1]].y)
            )

            for i, j in mst_edges:
                source_pad = pad_objs[i]
                target_pad = pad_objs[j]
                route = self.router.route(
                    source_pad,
                    target_pad,
                    negotiated_mode=True,
                    present_cost_factor=present_cost_factor,
                )
                if route:
                    mark_route_callback(route)
                    routes.append(route)
        else:
            # 2-pin net
            route = self.router.route(
                pad_objs[0],
                pad_objs[1],
                negotiated_mode=True,
                present_cost_factor=present_cost_factor,
            )
            if route:
                mark_route_callback(route)
                routes.append(route)

        return routes

    def find_nets_through_overused_cells(
        self,
        net_routes: dict[int, list[Route]],
        overused_cells: list[tuple[int, int, int, int]],
    ) -> list[int]:
        """Find nets with routes passing through overused cells.

        Args:
            net_routes: Dictionary of net_id -> list of routes
            overused_cells: List of (gx, gy, layer, overflow) tuples

        Returns:
            List of net IDs that need rerouting
        """
        overused_set = {(x, y, layer) for x, y, layer, _ in overused_cells}
        nets_to_reroute: list[int] = []

        for net, routes in net_routes.items():
            needs_reroute = False
            for route in routes:
                for seg in route.segments:
                    # Check if segment passes through overused cell
                    gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
                    gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
                    layer = seg.layer.value

                    # Sample points along segment
                    steps = max(abs(gx2 - gx1), abs(gy2 - gy1), 1)
                    for i in range(steps + 1):
                        t = i / steps
                        gx = int(gx1 + t * (gx2 - gx1))
                        gy = int(gy1 + t * (gy2 - gy1))
                        if (gx, gy, layer) in overused_set:
                            needs_reroute = True
                            break
                    if needs_reroute:
                        break
                if needs_reroute:
                    break

            if needs_reroute:
                nets_to_reroute.append(net)

        return nets_to_reroute

    def rip_up_nets(
        self,
        nets: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
    ) -> None:
        """Rip up routes for specified nets.

        Args:
            nets: Net IDs to rip up
            net_routes: Dictionary of net_id -> list of routes
            routes_list: Master list of all routes
        """
        for net in nets:
            for route in net_routes.get(net, []):
                self.grid.unmark_route_usage(route)
                self.grid.unmark_route(route)
                if route in routes_list:
                    routes_list.remove(route)
            net_routes[net] = []
