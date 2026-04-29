"""Two-phase routing algorithm (Global + Detailed).

Phase 1 uses tile-based GlobalRouter with geometry-based edge capacity
and negotiated iteration to assign corridors for each net.
Phase 2 uses grid-based routing with corridor guidance.

Issue #2276: Replaced SparseRouter global phase with tile-based
GlobalRouter supporting per-layer capacity and negotiated congestion.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kicad_tools.cli.progress import flush_print

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

    from ..grid import RoutingGrid
    from ..output import format_failed_nets_summary
    from ..pathfinder import Router
    from ..primitives import Pad, Route
    from ..rules import DesignRules
    from ..sparse import Corridor


class TwoPhaseRouter:
    """Two-phase global+detailed routing algorithm.

    Phase 1 (Global): Use tile-based GlobalRouter with geometry-based
    edge capacity estimation and negotiated congestion to assign
    corridors for each net.

    Phase 2 (Detailed): Use grid-based routing with corridor guidance.
    Routes prefer to stay within their assigned corridors but can exit
    with a cost penalty.
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
        pour_nets_without_zones: set[str] | None = None,
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
        self._pour_nets_without_zones = pour_nets_without_zones or set()

    def route_all(
        self,
        use_negotiated: bool = True,
        corridor_width_factor: float = 2.0,
        corridor_penalty: float | None = None,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route all nets using two-phase global+detailed routing.

        Args:
            use_negotiated: Use negotiated congestion routing in detailed phase
            corridor_width_factor: Corridor width as multiple of clearance (default: 2.0)
            corridor_penalty: Cost penalty for routing outside corridor.
                Defaults to ``self.rules.cost_corridor_deviation`` when *None*.
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds

        Returns:
            List of routes (may be partial if timeout reached or some nets fail)
        """
        from ..global_router import GlobalRouter
        from ..output import format_failed_nets_summary
        from ..region_graph import RegionGraph
        from ..sparse import Corridor

        if corridor_penalty is None:
            corridor_penalty = self.rules.cost_corridor_deviation

        start_time = time.time()

        print("\n=== Two-Phase Routing (Global + Detailed) ===")

        # Get nets to route in priority order
        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]

        # Issue #1295: Filter out pour nets — they are connected via zone fills.
        # Issue #1841: Exclude pour nets without zones (they route as signals).
        pour_nets = []
        signal_nets = []
        for n in net_order:
            net_name = self.net_names.get(n, "")
            if net_name in self._pour_nets_without_zones:
                signal_nets.append(n)
                continue
            net_class = (self.net_class_map or {}).get(net_name)
            if net_class and net_class.is_pour_net:
                pour_nets.append(n)
            else:
                signal_nets.append(n)
        if pour_nets:
            pour_names = [self.net_names.get(n, f"Net {n}") for n in pour_nets]
            flush_print(
                f"  Skipping {len(pour_nets)} pour net(s) "
                f"(use zone fill instead): {pour_names}"
            )
        net_order = signal_nets

        total_nets = len(net_order)

        if total_nets == 0:
            print("  No nets to route")
            return []

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        # =====================================================================
        # Phase 1: Tile-based Global Routing (Issue #2276)
        # =====================================================================
        print("\n--- Phase 1: Global Routing (tile-based) ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Phase 1: Global routing", True):
                return list(self.routes)

        # Compute routing pitch from design rules
        trace_pitch = self.rules.trace_width + self.rules.trace_clearance
        corridor_width = corridor_width_factor * self.rules.trace_clearance

        # Determine tile grid size: ~10x trace pitch per tile, minimum 3x3
        tile_size = max(trace_pitch * 10.0, 1.0)
        num_cols = max(3, int(self.grid.width / tile_size))
        num_rows = max(3, int(self.grid.height / tile_size))

        # Build tile-based region graph with geometry-based capacity
        region_graph = RegionGraph(
            board_width=self.grid.width,
            board_height=self.grid.height,
            origin_x=self.grid.origin_x,
            origin_y=self.grid.origin_y,
            num_cols=num_cols,
            num_rows=num_rows,
            trace_pitch=trace_pitch,
            num_layers=self.grid.num_layers,
        )

        # Register pads as obstacles for blockage-aware capacity
        pad_list = list(self.pads.values())
        region_graph.register_obstacles(pad_list)

        stats = region_graph.get_statistics()
        flush_print(
            f"  Tile grid: {num_cols}x{num_rows} "
            f"({stats['num_regions']} regions, {stats['num_edges']} edges, "
            f"pitch={trace_pitch:.3f}mm, layers={self.grid.num_layers})"
        )

        # Run global routing with negotiated iteration
        global_router = GlobalRouter(
            region_graph=region_graph,
            corridor_width=corridor_width,
            default_layer=0,
            negotiated=True,
            max_iterations=15,
            history_increment=1.0,
        )

        global_result = global_router.route_all(
            nets=self.nets,
            pad_dict=self.pads,
            net_order=net_order,
        )

        # Extract corridors from global routing result
        corridors: dict[int, Corridor] = {}
        for net_id, assign in global_result.assignments.items():
            corridors[net_id] = assign.corridor

        flush_print(
            f"  Global routing: {len(corridors)}/{total_nets} nets have corridors "
            f"({global_result.iterations} iterations, "
            f"overflow={global_result.final_overflow}, "
            f"{elapsed_str()})"
        )
        if global_result.failed_nets:
            flush_print(
                f"  {len(global_result.failed_nets)} nets failed global routing "
                f"(will attempt anyway)"
            )

        # =====================================================================
        # Phase 2: Detailed Routing with Corridor Guidance
        # =====================================================================
        print("\n--- Phase 2: Detailed Routing ---")
        if progress_callback is not None:
            if not progress_callback(0.3, "Phase 2: Detailed routing", True):
                return list(self.routes)

        # Set corridor preferences on the grid
        for net, corridor in corridors.items():
            self.grid.set_corridor_preference(corridor, net, corridor_penalty)

        # Route using negotiated or standard routing
        if use_negotiated:
            detailed_routes = self._detailed_negotiated(
                net_order=net_order,
                corridor_penalty=corridor_penalty,
                corridors=corridors,
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

        # Clear corridor preferences (not needed after routing)
        self.grid.clear_all_corridor_preferences()

        # Summary
        successful_nets = len({r.net for r in detailed_routes})
        total_elapsed = time.time() - start_time
        print("\n=== Two-Phase Routing Complete ===")
        print(f"  Total nets: {total_nets}")
        print(f"  Global routing: {len(corridors)} corridors assigned")
        print(f"  Detailed routing: {successful_nets} nets routed")
        print(f"  Total time: {total_elapsed:.1f}s")

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        if progress_callback is not None:
            progress_callback(
                1.0,
                f"Complete: {successful_nets}/{total_nets} nets routed in {total_elapsed:.1f}s",
                False,
            )

        return detailed_routes

    def _detailed_negotiated(
        self,
        net_order: list[int],
        corridor_penalty: float | None = None,
        corridors: dict | None = None,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
        start_time: float = 0.0,
    ) -> list[Route]:
        """Detailed routing phase using negotiated congestion routing."""
        from ..algorithms import NegotiatedRouter

        if corridor_penalty is None:
            corridor_penalty = self.rules.cost_corridor_deviation

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        total_nets = len(net_order)

        # Use negotiated routing with corridor guidance
        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)
        net_routes: dict[int, list[Route]] = {}
        present_factor = 0.5

        # Initial routing pass
        for i, net in enumerate(net_order):
            if check_timeout():
                flush_print(
                    f"  ⚠ Timeout during detailed routing at net {i}/{total_nets} ({elapsed_str()})"
                )
                break

            net_name = self.net_names.get(net, f"Net {net}")
            pct = (i / total_nets * 100) if total_nets > 0 else 0
            flush_print(f"  [{pct:5.1f}%] Routing {net_name}... ({elapsed_str()})")

            routes = self._route_net_with_corridor(net, present_factor)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        overflow = self.grid.get_total_overflow()
        flush_print(f"  Initial pass: {len(net_routes)}/{total_nets} nets, overflow: {overflow}")

        # Rip-up and reroute iterations if needed
        if overflow > 0:
            max_iterations = 10
            history_increment = 1.0
            present_factor_increment = 0.5

            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    flush_print(f"  ⚠ Timeout at iteration {iteration} ({elapsed_str()})")
                    break

                if progress_callback is not None:
                    progress = 0.3 + 0.6 * (iteration / max_iterations)
                    if not progress_callback(
                        progress, f"Iteration {iteration}/{max_iterations}", True
                    ):
                        break

                present_factor += present_factor_increment
                self.grid.update_history_costs(history_increment)

                # Issue #2288: Relax corridor constraint as iterations progress
                # so the detailed router can escape suboptimal global corridors.
                # Floor of 0.2 keeps a mild preference even in late iterations.
                if corridors:
                    effective_penalty = corridor_penalty * max(
                        0.2, 1.0 - 0.1 * iteration
                    )
                    for net, corridor in corridors.items():
                        self.grid.set_corridor_preference(
                            corridor, net, effective_penalty
                        )

                overused = self.grid.find_overused_cells()
                nets_to_reroute = neg_router.find_nets_through_overused_cells(net_routes, overused)
                flush_print(
                    f"  Iteration {iteration}: ripping up {len(nets_to_reroute)} nets ({elapsed_str()})"
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

                overflow = self.grid.get_total_overflow()
                flush_print(f"  Iteration {iteration} complete: overflow={overflow}")

                if overflow == 0:
                    flush_print(f"  Converged at iteration {iteration}!")
                    break

        return list(self.routes)

    def _detailed_standard(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed routing phase using standard routing (no negotiation)."""

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        total_nets = len(net_order)
        all_routes: list[Route] = []

        for i, net in enumerate(net_order):
            if check_timeout():
                print(f"  ⚠ Timeout at net {i}/{total_nets} ({time.time() - start_time:.1f}s)")
                break

            if progress_callback is not None:
                progress = 0.3 + 0.7 * (i / total_nets)
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self._route_net(net)
            all_routes.extend(routes)

        return all_routes
