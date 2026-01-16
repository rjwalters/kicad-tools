"""Negotiated congestion routing algorithm (PathFinder-style).

This module implements iterative rip-up and reroute with increasing
congestion penalties to resolve routing conflicts.

Adaptive parameter tuning (Issue #633) improves convergence by:
- Dynamically adjusting history increment based on progress
- Detecting oscillation patterns to escape local minima
- Adapting present cost factor based on congestion
- Early termination when no progress is being made
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..grid import RoutingGrid
    from ..pathfinder import Router
    from ..primitives import Pad, Route
    from ..rules import DesignRules, NetClassRouting


# =========================================================================
# Adaptive Parameter Functions (Issue #633)
# =========================================================================


def calculate_history_increment(
    iteration: int,
    overflow_history: list[int],
    base_increment: float = 0.5,
) -> float:
    """Calculate adaptive history increment based on convergence progress.

    Args:
        iteration: Current iteration number (0-indexed)
        overflow_history: List of overflow values from previous iterations
        base_increment: Base history increment value (default: 0.5)

    Returns:
        Adjusted history increment value

    The increment is increased when:
    - Overflow is increasing (need more penalty to discourage congested areas)
    - Overflow is stagnant (stuck at local minimum, need stronger push)

    The increment is decreased when:
    - Close to convergence (gentle adjustments to avoid overshooting)
    """
    if len(overflow_history) < 2:
        return base_increment

    current = overflow_history[-1]
    previous = overflow_history[-2]

    # If overflow is increasing, be more aggressive with penalties
    if current > previous:
        return base_increment * 1.5

    # If overflow is stagnant (same value), try stronger penalty
    if current == previous:
        # Check for repeated stagnation
        stagnant_count = 1
        for i in range(len(overflow_history) - 2, max(0, len(overflow_history) - 5) - 1, -1):
            if overflow_history[i] == current:
                stagnant_count += 1
            else:
                break
        # Increase increment based on how long we've been stuck
        return base_increment * (1.0 + 0.5 * stagnant_count)

    # If overflow is decreasing and close to zero, be gentler
    if current < 5:
        return base_increment * 0.5

    # Normal progress - use base increment
    return base_increment


def detect_oscillation(overflow_history: list[int], window: int = 4) -> bool:
    """Detect if the router is oscillating between states.

    Args:
        overflow_history: List of overflow values from previous iterations
        window: Number of recent iterations to check (default: 4)

    Returns:
        True if oscillation is detected, False otherwise

    Detects:
    - A-B-A-B patterns (alternating between two values)
    - Complete stagnation (all same value for window iterations)
    """
    if len(overflow_history) < window:
        return False

    recent = overflow_history[-window:]

    # Check for exact A-B-A-B repetition pattern
    if window >= 4 and recent[0] == recent[2] and recent[1] == recent[3]:
        return True

    # Check for complete stagnation (all same value)
    if len(set(recent)) == 1:
        return True

    # Check for bounded oscillation (values stay within small range)
    if window >= 4:
        unique_vals = set(recent)
        if len(unique_vals) <= 2 and min(recent) > 0:
            return True

    return False


def should_terminate_early(
    overflow_history: list[int],
    iteration: int,
    min_iterations: int = 5,
) -> bool:
    """Decide if further iterations are futile and should terminate early.

    Args:
        overflow_history: List of overflow values from previous iterations
        iteration: Current iteration number
        min_iterations: Minimum iterations before considering early termination

    Returns:
        True if should terminate early, False otherwise

    Terminates when:
    - No improvement in last 5 iterations
    - Oscillation detected
    - Overflow is getting worse over time
    """
    if iteration < min_iterations:
        return False

    if len(overflow_history) < 5:
        return False

    recent = overflow_history[-5:]

    # No improvement in last 5 iterations
    earlier = overflow_history[:-5] if len(overflow_history) > 5 else [float("inf")]
    if min(recent) >= min(earlier):
        return True

    # Oscillating with no progress
    if detect_oscillation(overflow_history, window=4):
        # Only terminate if we've tried enough and aren't improving
        if iteration >= min_iterations and min(recent) == min(overflow_history):
            return True

    # Overflow trending upward over time
    if len(overflow_history) >= 6:
        first_half = overflow_history[: len(overflow_history) // 2]
        second_half = overflow_history[len(overflow_history) // 2 :]
        if min(second_half) > min(first_half) * 1.2:
            # Getting worse, give up
            return True

    return False


def calculate_present_cost(
    iteration: int,
    total_iterations: int,
    overflow_ratio: float,
    base_cost: float = 0.5,
) -> float:
    """Calculate adaptive present cost factor based on iteration and congestion.

    Args:
        iteration: Current iteration number (0-indexed)
        total_iterations: Maximum number of iterations
        overflow_ratio: Current overflow / total cells (congestion metric)
        base_cost: Base present cost value (default: 0.5)

    Returns:
        Adjusted present cost factor

    The cost increases:
    - As iterations progress (more pressure over time)
    - When congestion is high (need to discourage contested resources)
    """
    # Increase pressure as iterations progress (gradual ramp)
    progress_factor = 1.0 + (iteration / max(total_iterations, 1))

    # Higher cost when more congested
    congestion_factor = 1.0 + min(overflow_ratio * 2, 2.0)  # Cap at 3x

    return base_cost * progress_factor * congestion_factor


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

    def targeted_ripup(
        self,
        failed_net: int,
        blocking_nets: set[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        present_cost_factor: float,
        mark_route_callback: callable,
        ripup_history: dict[int, int] | None = None,
        max_ripups_per_net: int = 3,
    ) -> bool:
        """Perform targeted rip-up of blocking nets and re-route.

        Instead of ripping up all conflicting nets, this method only rips up
        the specific nets that are blocking the failed net's path, then
        re-routes the failed net first (giving it priority) followed by
        the displaced nets.

        Args:
            failed_net: Net ID that failed to route
            blocking_nets: Set of net IDs blocking the failed net's path
            net_routes: Dictionary of net_id -> list of routes
            routes_list: Master list of all routes
            pads_by_net: Dictionary of net_id -> list of pads for that net
            present_cost_factor: Current congestion cost factor
            mark_route_callback: Callback to mark routes on the grid
            ripup_history: Optional dict tracking ripup count per net
            max_ripups_per_net: Maximum times a net can be ripped up (prevents loops)

        Returns:
            True if re-routing succeeded for all affected nets, False otherwise
        """
        if ripup_history is None:
            ripup_history = {}

        # Filter out nets that have been ripped up too many times
        # This prevents infinite loops where nets keep displacing each other
        nets_to_ripup: set[int] = set()
        for net in blocking_nets:
            if ripup_history.get(net, 0) < max_ripups_per_net:
                nets_to_ripup.add(net)
                ripup_history[net] = ripup_history.get(net, 0) + 1

        if not nets_to_ripup:
            # All blocking nets have reached their ripup limit
            # Fall back to normal routing with high congestion cost
            return False

        # Rip up only the blocking nets
        self.rip_up_nets(list(nets_to_ripup), net_routes, routes_list)

        # Re-route the failed net first (it now has priority with cleared path)
        failed_pads = pads_by_net.get(failed_net, [])
        failed_net_success = False  # Issue #858: Track if failed net was routed
        if failed_pads and len(failed_pads) >= 2:
            routes = self.route_net_negotiated(
                failed_pads, present_cost_factor, mark_route_callback
            )
            if routes:
                net_routes[failed_net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    routes_list.append(route)
                failed_net_success = True  # Failed net was successfully routed

        # Re-route the displaced nets
        success = failed_net_success  # Issue #858: Start with failed net success
        for net in nets_to_ripup:
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                routes = self.route_net_negotiated(
                    net_pads, present_cost_factor, mark_route_callback
                )
                if routes:
                    net_routes[net] = routes
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)
                else:
                    # Displaced net failed to re-route
                    success = False

        return success

    def find_blocking_nets_for_connection(
        self,
        source_pad: Pad,
        target_pad: Pad,
    ) -> set[int]:
        """Find nets blocking a specific connection.

        Uses the pathfinder's find_blocking_nets method to identify
        which nets are blocking the direct path between two pads.

        Args:
            source_pad: Starting pad
            target_pad: Ending pad

        Returns:
            Set of net IDs blocking the path
        """
        return self.router.find_blocking_nets(source_pad, target_pad)

    def escape_local_minimum(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
        strategy_index: int = 0,
    ) -> tuple[bool, int]:
        """Try escape strategies when router is stuck oscillating.

        When the negotiated router is stuck cycling between states without
        making progress (detected via detect_oscillation), this method
        tries various strategies to escape the local minimum.

        Args:
            overflow_history: List of overflow values from previous iterations
            net_routes: Dictionary of net_id -> list of routes
            routes_list: Master list of all routes
            pads_by_net: Dictionary of net_id -> list of pads
            net_order: List of net IDs in routing priority order
            present_cost_factor: Current congestion cost factor
            mark_route_callback: Callback to mark routes on the grid
            strategy_index: Index of strategy to try (cycles through available)

        Returns:
            Tuple of (success, overflow) where success indicates if overflow improved
        """
        strategies = [
            self._escape_shuffle_order,
            self._escape_reverse_order,
            self._escape_random_subset,
        ]

        strategy = strategies[strategy_index % len(strategies)]
        return strategy(
            overflow_history=overflow_history,
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net=pads_by_net,
            net_order=net_order,
            present_cost_factor=present_cost_factor,
            mark_route_callback=mark_route_callback,
        )

    def _escape_shuffle_order(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
    ) -> tuple[bool, int]:
        """Escape strategy: shuffle the net order randomly.

        Sometimes a different routing order can escape local minima by
        giving different nets priority.
        """
        # Get the conflicting nets
        overused = self.grid.find_overused_cells()
        nets_to_reroute = self.find_nets_through_overused_cells(net_routes, overused)

        if not nets_to_reroute:
            return False, overflow_history[-1] if overflow_history else 0

        # Shuffle the order of conflicting nets
        shuffled = nets_to_reroute.copy()
        random.shuffle(shuffled)

        # Rip up all conflicting nets
        self.rip_up_nets(shuffled, net_routes, routes_list)

        # Re-route in shuffled order with higher cost
        # Track successful re-routes to detect if any net was lost
        boosted_cost = present_cost_factor * 1.5
        rerouted_count = 0
        expected_count = 0
        for net in shuffled:
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                expected_count += 1
                routes = self.route_net_negotiated(net_pads, boosted_cost, mark_route_callback)
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Only consider escape successful if ALL nets were re-routed AND overflow improved
        # (Issue #762: escape was incorrectly reporting success when nets failed to re-route)
        all_rerouted = rerouted_count == expected_count
        return all_rerouted and new_overflow < best_overflow, new_overflow

    def _escape_reverse_order(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
    ) -> tuple[bool, int]:
        """Escape strategy: reverse the net order.

        Routing in reverse order gives previously low-priority nets
        first access to routing resources.
        """
        overused = self.grid.find_overused_cells()
        nets_to_reroute = self.find_nets_through_overused_cells(net_routes, overused)

        if not nets_to_reroute:
            return False, overflow_history[-1] if overflow_history else 0

        # Reverse the order
        reversed_order = list(reversed(nets_to_reroute))

        # Rip up all conflicting nets
        self.rip_up_nets(reversed_order, net_routes, routes_list)

        # Re-route in reversed order
        # Track successful re-routes to detect if any net was lost
        boosted_cost = present_cost_factor * 1.5
        rerouted_count = 0
        expected_count = 0
        for net in reversed_order:
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                expected_count += 1
                routes = self.route_net_negotiated(net_pads, boosted_cost, mark_route_callback)
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Only consider escape successful if ALL nets were re-routed AND overflow improved
        # (Issue #762: escape was incorrectly reporting success when nets failed to re-route)
        all_rerouted = rerouted_count == expected_count
        return all_rerouted and new_overflow < best_overflow, new_overflow

    def _escape_random_subset(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
    ) -> tuple[bool, int]:
        """Escape strategy: rip up and reroute a random subset of nets.

        By ripping up only a subset of conflicting nets, we may find
        a different combination that works.
        """
        overused = self.grid.find_overused_cells()
        nets_to_reroute = self.find_nets_through_overused_cells(net_routes, overused)

        if not nets_to_reroute:
            return False, overflow_history[-1] if overflow_history else 0

        # Select random subset (50-75% of conflicting nets)
        subset_size = max(1, len(nets_to_reroute) * 2 // 3)
        subset = random.sample(nets_to_reroute, min(subset_size, len(nets_to_reroute)))

        # Rip up subset
        self.rip_up_nets(subset, net_routes, routes_list)

        # Re-route with much higher cost to force different paths
        # Track successful re-routes to detect if any net was lost
        boosted_cost = present_cost_factor * 2.0
        rerouted_count = 0
        expected_count = 0
        for net in subset:
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                expected_count += 1
                routes = self.route_net_negotiated(net_pads, boosted_cost, mark_route_callback)
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Only consider escape successful if ALL nets were re-routed AND overflow improved
        # (Issue #762: escape was incorrectly reporting success when nets failed to re-route)
        all_rerouted = rerouted_count == expected_count
        return all_rerouted and new_overflow < best_overflow, new_overflow
