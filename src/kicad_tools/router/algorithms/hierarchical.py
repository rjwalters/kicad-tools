"""Hierarchical routing algorithm (Global-to-Detailed via RegionGraph).

Uses a RegionGraph to plan coarse routing corridors for each net
before performing detailed routing. The flow is:
1. Build a RegionGraph partitioning the board into regions
2. Use GlobalRouter to assign each net a corridor
3. Convert corridors to grid-level preferences
4. Run detailed routing with corridor guidance
5. Fallback: nets that fail global routing are routed without corridors
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kicad_tools.cli.progress import flush_print

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

    from ..grid import RoutingGrid
    from ..pathfinder import Router
    from ..primitives import Pad, Route
    from ..rules import DesignRules


class HierarchicalRouter:
    """Hierarchical global-to-detailed routing algorithm.

    Uses a RegionGraph for coarse-grid corridor assignment before
    detailed routing. This provides better resource allocation than
    direct routing because nets are guided into non-overlapping channels.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        router: Router,
        rules: DesignRules,
        net_class_map: dict | None,
        nets: dict[int, list[tuple[str, str]]],
        net_names: dict[int, str],
        pads: dict[tuple[str, str], Pad],
        routes: list[Route],
        routing_failures: list,
        get_net_priority: callable,
        route_net: callable,
        route_net_with_corridor: callable,
        mark_route: callable,
    ):
        self.grid = grid
        self.router = router
        self.rules = rules
        self.net_class_map = net_class_map
        self.nets = nets
        self.net_names = net_names
        self.pads = pads
        self.routes = routes
        self.routing_failures = routing_failures
        self._get_net_priority = get_net_priority
        self._route_net = route_net
        self._route_net_with_corridor = route_net_with_corridor
        self._mark_route = mark_route

    def route_all(
        self,
        num_cols: int = 10,
        num_rows: int = 10,
        corridor_width_factor: float = 2.0,
        use_negotiated: bool = True,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route all nets using hierarchical global-to-detailed flow.

        Args:
            num_cols: Number of region columns for the RegionGraph (default: 10)
            num_rows: Number of region rows for the RegionGraph (default: 10)
            corridor_width_factor: Corridor width as multiple of clearance (default: 2.0)
            use_negotiated: Use negotiated congestion routing in detailed phase
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds

        Returns:
            List of Route objects (may be partial if timeout reached)
        """
        from ..global_router import GlobalRouter
        from ..output import format_failed_nets_summary
        from ..region_graph import RegionGraph

        start_time = time.time()

        flush_print("\n=== Hierarchical Routing (Global + Detailed) ===")

        # Get nets to route in priority order
        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]
        total_nets = len(net_order)

        if total_nets == 0:
            flush_print("  No nets to route")
            return []

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        # =================================================================
        # Phase 1: Build RegionGraph and run GlobalRouter
        # =================================================================
        flush_print("\n--- Phase 1: Global Routing via RegionGraph ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Phase 1: Building region graph", True):
                return list(self.routes)

        corridor_width = corridor_width_factor * self.rules.trace_clearance

        # Build region graph
        region_graph = RegionGraph(
            board_width=self.grid.width,
            board_height=self.grid.height,
            origin_x=self.grid.origin_x,
            origin_y=self.grid.origin_y,
            num_cols=num_cols,
            num_rows=num_rows,
        )

        # Register obstacles (pads reduce region capacity)
        all_pads = list(self.pads.values())
        region_graph.register_obstacles(all_pads)

        rg_stats = region_graph.get_statistics()
        flush_print(
            f"  Region graph: {rg_stats['num_regions']} regions "
            f"({rg_stats['num_rows']}x{rg_stats['num_cols']}), "
            f"{rg_stats['num_edges']} edges, "
            f"{rg_stats['regions_with_obstacles']} regions with obstacles"
        )

        # Run global router
        global_router = GlobalRouter(
            region_graph=region_graph,
            corridor_width=corridor_width,
            default_layer=0,
        )

        if progress_callback is not None:
            if not progress_callback(0.05, "Phase 1: Assigning corridors", True):
                return list(self.routes)

        global_result = global_router.route_all(
            nets=self.nets,
            pad_dict=self.pads,
            net_order=net_order,
        )

        flush_print(
            f"  Global routing: {len(global_result.assignments)}/{total_nets} "
            f"nets assigned corridors ({elapsed_str()})"
        )
        if global_result.failed_nets:
            flush_print(
                f"  Warning: {len(global_result.failed_nets)} nets failed "
                f"global routing (will attempt without corridors)"
            )

        # =================================================================
        # Phase 2: Detailed Routing with Corridor Guidance
        # =================================================================
        flush_print("\n--- Phase 2: Detailed Routing with Corridors ---")
        if progress_callback is not None:
            if not progress_callback(0.2, "Phase 2: Detailed routing", True):
                return list(self.routes)

        # Set corridor preferences on the grid for nets with assignments
        corridor_penalty = 5.0
        for net, assignment in global_result.assignments.items():
            self.grid.set_corridor_preference(
                assignment.corridor, net, corridor_penalty
            )

        # Route all nets (corridor-assigned nets get guidance, others route freely)
        if use_negotiated:
            detailed_routes = self._detailed_negotiated(
                net_order=net_order,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
            )
        else:
            detailed_routes = self._detailed_standard(
                net_order=net_order,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
            )

        # Clear corridor preferences
        self.grid.clear_all_corridor_preferences()

        # Summary
        successful_nets = len({r.net for r in detailed_routes})
        total_elapsed = time.time() - start_time
        flush_print("\n=== Hierarchical Routing Complete ===")
        flush_print(f"  Total nets: {total_nets}")
        flush_print(
            f"  Global routing: {len(global_result.assignments)} corridors assigned"
        )
        flush_print(f"  Detailed routing: {successful_nets} nets routed")
        flush_print(f"  Total time: {total_elapsed:.1f}s")

        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        if progress_callback is not None:
            progress_callback(
                1.0,
                f"Complete: {successful_nets}/{total_nets} nets in {total_elapsed:.1f}s",
                False,
            )

        return detailed_routes

    def _detailed_negotiated(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed phase using negotiated congestion routing."""
        from ..algorithms import NegotiatedRouter

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        total_nets = len(net_order)

        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)
        net_routes: dict[int, list[Route]] = {}
        present_factor = 0.5

        # Initial routing pass
        for i, net in enumerate(net_order):
            if check_timeout():
                flush_print(
                    f"  Timeout during detailed routing at net {i}/{total_nets}"
                )
                break

            if progress_callback is not None:
                progress = 0.2 + 0.6 * (i / total_nets)
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self._route_net_with_corridor(net, present_factor)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        overflow = self.grid.get_total_overflow()
        flush_print(
            f"  Initial pass: {len(net_routes)}/{total_nets} nets, overflow: {overflow}"
        )

        # Rip-up and reroute if overflow remains
        if overflow > 0:
            max_iterations = 10
            history_increment = 1.0
            present_factor_increment = 0.5

            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    flush_print(f"  Timeout at iteration {iteration}")
                    break

                if progress_callback is not None:
                    progress = 0.8 + 0.15 * (iteration / max_iterations)
                    if not progress_callback(
                        progress, f"Iteration {iteration}/{max_iterations}", True
                    ):
                        break

                present_factor += present_factor_increment
                self.grid.update_history_costs(history_increment)

                overused = self.grid.find_overused_cells()
                nets_to_reroute = neg_router.find_nets_through_overused_cells(
                    net_routes, overused
                )
                flush_print(
                    f"  Iteration {iteration}: ripping up {len(nets_to_reroute)} nets"
                )

                neg_router.rip_up_nets(nets_to_reroute, net_routes, self.routes)

                for net in nets_to_reroute:
                    if check_timeout():
                        break
                    routes = self._route_net_with_corridor(net, present_factor)
                    if routes:
                        net_routes[net] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            self.routes.append(route)

                new_overflow = self.grid.get_total_overflow()
                if new_overflow == 0:
                    flush_print(f"  Overflow resolved at iteration {iteration}")
                    break
                overflow = new_overflow

        # Collect all routes
        all_routes: list[Route] = []
        for routes in net_routes.values():
            all_routes.extend(routes)

        return all_routes

    def _detailed_standard(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed phase using standard sequential routing."""

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        total_nets = len(net_order)
        all_routes: list[Route] = []

        for i, net in enumerate(net_order):
            if check_timeout():
                flush_print(f"  Timeout at net {i}/{total_nets}")
                break

            if progress_callback is not None:
                progress = 0.2 + 0.7 * (i / total_nets)
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self._route_net(net)
            all_routes.extend(routes)

        return all_routes
