"""Two-phase routing algorithm (Global + Detailed).

Phase 1 uses SparseRouter to find coarse paths and reserve corridors.
Phase 2 uses grid-based routing with corridor guidance.
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

    Phase 1 (Global): Use SparseRouter to find coarse paths and reserve
    corridors for each net. This establishes routing channels that prevent
    nets from blocking each other.

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
        use_negotiated: bool = True,
        corridor_width_factor: float = 2.0,
        corridor_penalty: float = 5.0,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> list[Route]:
        """Route all nets using two-phase global+detailed routing.

        Args:
            use_negotiated: Use negotiated congestion routing in detailed phase
            corridor_width_factor: Corridor width as multiple of clearance (default: 2.0)
            corridor_penalty: Cost penalty for routing outside corridor (default: 5.0)
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds

        Returns:
            List of routes (may be partial if timeout reached or some nets fail)
        """
        from ..output import format_failed_nets_summary
        from ..sparse import Corridor, SparseRouter

        start_time = time.time()

        print("\n=== Two-Phase Routing (Global + Detailed) ===")

        # Get nets to route in priority order
        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]
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
        # Phase 1: Global Routing with SparseRouter
        # =====================================================================
        print("\n--- Phase 1: Global Routing ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Phase 1: Global routing", True):
                return list(self.routes)

        # Create sparse router for global routing
        sparse_router = SparseRouter(
            width=self.grid.width,
            height=self.grid.height,
            rules=self.rules,
            origin_x=self.grid.origin_x,
            origin_y=self.grid.origin_y,
            num_layers=self.grid.num_layers,
        )

        # Add all pads to sparse router
        for pad in self.pads.values():
            sparse_router.add_pad(pad)

        # Build the sparse graph
        sparse_router.build_graph()
        stats = sparse_router.get_statistics()
        print(f"  Sparse graph: {stats['total_waypoints']} waypoints, {stats['total_edges']} edges")

        # Find global paths and reserve corridors
        corridors: dict[int, Corridor] = {}
        global_failures: list[int] = []
        corridor_width = corridor_width_factor * self.rules.trace_clearance

        for i, net in enumerate(net_order):
            if check_timeout():
                print(
                    f"  ⚠ Timeout during global routing at net {i}/{total_nets} ({elapsed_str()})"
                )
                break

            pads = self.nets[net]
            if len(pads) < 2:
                continue

            # For multi-pad nets, find path between first two pads
            # (MST routing will handle the rest in detailed phase)
            pad1 = self.pads[pads[0]]
            pad2 = self.pads[pads[1]]

            waypoints = sparse_router.find_global_path(pad1, pad2)

            if waypoints:
                corridor = sparse_router.reserve_corridor(net, waypoints, corridor_width)
                corridors[net] = corridor
            else:
                global_failures.append(net)
                net_name = self.net_names.get(net, f"Net {net}")
                print(f"  ⚠ Global routing failed for {net_name}")

        print(
            f"  Global routing: {len(corridors)}/{total_nets} nets have corridors ({elapsed_str()})"
        )
        if global_failures:
            print(f"  ⚠ {len(global_failures)} nets failed global routing (will attempt anyway)")

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
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed routing phase using negotiated congestion routing."""
        from ..algorithms import NegotiatedRouter

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
                print(
                    f"  ⚠ Timeout during detailed routing at net {i}/{total_nets} ({elapsed_str()})"
                )
                break

            net_name = self.net_names.get(net, f"Net {net}")
            pct = (i / total_nets * 100) if total_nets > 0 else 0
            print(f"  [{pct:5.1f}%] Routing {net_name}... ({elapsed_str()})")

            routes = self._route_net_with_corridor(net, present_factor)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        overflow = self.grid.get_total_overflow()
        print(f"  Initial pass: {len(net_routes)}/{total_nets} nets, overflow: {overflow}")

        # Rip-up and reroute iterations if needed
        if overflow > 0:
            max_iterations = 10
            history_increment = 1.0
            present_factor_increment = 0.5

            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    print(f"  ⚠ Timeout at iteration {iteration} ({elapsed_str()})")
                    break

                if progress_callback is not None:
                    progress = 0.3 + 0.6 * (iteration / max_iterations)
                    if not progress_callback(
                        progress, f"Iteration {iteration}/{max_iterations}", True
                    ):
                        break

                present_factor += present_factor_increment
                self.grid.update_history_costs(history_increment)

                overused = self.grid.find_overused_cells()
                nets_to_reroute = neg_router.find_nets_through_overused_cells(net_routes, overused)
                print(
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
                print(f"  Iteration {iteration} complete: overflow={overflow}")

                if overflow == 0:
                    print(f"  Converged at iteration {iteration}!")
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
