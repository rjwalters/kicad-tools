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
    from ..congestion_estimator import CongestionEstimator
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
    - Bounded oscillation (only when the window does NOT contain a new
      global minimum, since a new minimum means the router is still
      making progress -- Issue #1823)
    """
    if len(overflow_history) < window:
        return False

    recent = overflow_history[-window:]

    # Issue #2262: If the most recent overflow is zero the router has
    # converged -- never report oscillation in this state.
    if recent[-1] == 0:
        return False

    # Issue #1823: If the recent window contains a new global minimum,
    # the router is still making progress -- do not declare oscillation.
    # For example, [21, 21, 8, 21] has overflow 8 as a new best, which
    # means the router found a better configuration even if it bounced back.
    best_overall = min(overflow_history)
    window_min = min(recent)
    window_has_new_minimum = window_min <= best_overall and window_min < min(
        overflow_history[:-window]
    ) if len(overflow_history) > window else False

    if window_has_new_minimum:
        return False

    # Check for exact A-B-A-B repetition pattern
    if window >= 4 and recent[0] == recent[2] and recent[1] == recent[3]:
        return True

    # Check for complete stagnation (all same value)
    # Zero-overflow stagnation is convergence, not oscillation (#2262)
    if len(set(recent)) == 1 and recent[0] > 0:
        return True

    # Check for bounded oscillation (values stay within small range)
    if window >= 4:
        unique_vals = set(recent)
        if len(unique_vals) <= 2 and min(recent) > 0:
            return True

    return False


def _is_monotonically_diverging(overflow_history: list[int], window: int = 3) -> bool:
    """Detect monotonically increasing overflow above the best-seen value.

    This catches diverging sequences like [90, 96, 88, 130, 148, 155] where
    the last ``window`` values are all strictly increasing and all above the
    overall best (minimum) value.  Standard oscillation detection misses this
    pattern because it looks for A-B-A-B cycles, and the half-split worsening
    check is defeated by an early dip that lands in the second half.

    Args:
        overflow_history: List of overflow values from previous iterations.
        window: Number of trailing values to inspect (default: 3).

    Returns:
        True if the last ``window`` values form a strictly increasing
        sequence that is entirely above the historical minimum.
    """
    if len(overflow_history) < window + 1:
        return False

    recent = overflow_history[-window:]
    best_seen = min(overflow_history)

    # All recent values must be strictly above the best seen
    if not all(v > best_seen for v in recent):
        return False

    # The recent window must be strictly increasing
    return all(recent[i] < recent[i + 1] for i in range(len(recent) - 1))


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
    - Monotonic divergence detected (overflow climbing away from best)
    - Oscillation detected
    - Overflow is getting worse over time
    """
    if iteration < min_iterations:
        return False

    if len(overflow_history) < 5:
        return False

    recent = overflow_history[-5:]

    # Issue #1823: Check if the recent window contains a new global minimum.
    # If so, skip the "no improvement" stagnation check (but still allow
    # other termination checks like monotonic divergence).
    best_overall = min(overflow_history)
    recent_has_new_global_min = False
    if min(recent) == best_overall and len(overflow_history) > 5:
        best_before_recent = min(overflow_history[:-5])
        if min(recent) < best_before_recent:
            recent_has_new_global_min = True

    # No improvement in last 5 iterations.
    # When len(overflow_history) == 5 there is no earlier window; use the
    # first recorded value as baseline instead of float('inf') which would
    # make this check unreachable and mask stale-baseline divergence.
    # Issue #1823: Skip this check when recent window found a new global
    # minimum -- the router is still making progress.
    if not recent_has_new_global_min:
        if len(overflow_history) > 5:
            earlier = overflow_history[:-5]
        else:
            earlier = overflow_history[:1]
        if min(recent) >= min(earlier):
            return True

    # Monotonic divergence: the last N values are strictly increasing and
    # all above the best-seen minimum.  This catches patterns like
    # [90, 96, 88, 130, 148, 155] that slip past the half-split check
    # because a single dip (88) in the second half keeps min(second_half)
    # low.
    if _is_monotonically_diverging(overflow_history, window=3):
        return True

    # Oscillating with no progress
    # Issue #1823: Skip this check when the recent 5-iteration window
    # contains a new global minimum -- the router recently made progress.
    if not recent_has_new_global_min and detect_oscillation(overflow_history, window=4):
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
        congestion_estimator: CongestionEstimator | None = None,
        congestion_weight: float = 0.5,
    ):
        """Initialize the negotiated router.

        Args:
            grid: The routing grid
            router: The pathfinding router
            rules: Design rules
            net_class_map: Net class routing rules
            congestion_estimator: Optional RUDY congestion estimator.  When
                provided, Steiner tree construction blends Manhattan distance
                with tile demand so that multi-terminal nets prefer
                less-congested paths.
            congestion_weight: Multiplier applied to the tile demand when
                computing the congestion-aware edge cost.  The weight is
                scaled internally by tile area so that demand (mm) and
                Manhattan distance (mm) are comparable.  A value of 0
                disables congestion influence.  Default is 0.5.
        """
        self.grid = grid
        self.router = router
        self.rules = rules
        self.net_class_map = net_class_map
        self.congestion_estimator = congestion_estimator
        self.congestion_weight = congestion_weight

    def route_net_negotiated(
        self,
        pad_objs: list[Pad],
        present_cost_factor: float,
        mark_route_callback: callable,
        per_net_timeout: float | None = None,
    ) -> list[Route]:
        """Route a single net in negotiated mode.

        Args:
            pad_objs: List of Pad objects to connect
            present_cost_factor: Multiplier for present sharing cost
            mark_route_callback: Callback to mark a route on the grid
            per_net_timeout: Optional wall-clock timeout in seconds for each
                A* search within this net (Issue #1605)

        Returns:
            List of routes created
        """
        if len(pad_objs) < 2:
            return []

        routes: list[Route] = []

        if len(pad_objs) > 2:
            # RSMT-based routing with negotiated mode
            from .steiner import build_rsmt

            # Build congestion-aware cost function for Steiner tree
            # construction when a RUDY estimator is available.
            congestion_fn = None
            if (
                self.congestion_estimator is not None
                and self.congestion_weight > 0
            ):
                est = self.congestion_estimator
                tile_area = est.grid.tile_w * est.grid.tile_h
                # Scale weight by tile area so demand and Manhattan
                # distance are in comparable units (mm).
                scaled_weight = self.congestion_weight * tile_area

                def congestion_fn(
                    x1: float, y1: float, x2: float, y2: float
                ) -> float:
                    manhattan = abs(x1 - x2) + abs(y1 - y2)
                    mid_x = (x1 + x2) / 2
                    mid_y = (y1 + y2) / 2
                    col, row = est.grid.tile_at(mid_x, mid_y)
                    demand = est.get_tile_demand(row, col)
                    return manhattan + scaled_weight * demand

            pad_objs, rsmt_edges = build_rsmt(
                pad_objs, congestion_fn=congestion_fn
            )

            for i, j in rsmt_edges:
                source_pad = pad_objs[i]
                target_pad = pad_objs[j]
                route = self.router.route(
                    source_pad,
                    target_pad,
                    negotiated_mode=True,
                    present_cost_factor=present_cost_factor,
                    per_net_timeout=per_net_timeout,
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
                per_net_timeout=per_net_timeout,
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
        per_net_timeout: float | None = None,
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
            per_net_timeout: Optional wall-clock timeout in seconds for each
                A* search (Issue #1605)

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
                failed_pads, present_cost_factor, mark_route_callback,
                per_net_timeout=per_net_timeout,
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
                    net_pads, present_cost_factor, mark_route_callback,
                    per_net_timeout=per_net_timeout,
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

    def find_blocking_nets_relaxed(
        self,
        failed_nets: list[int],
        pads_by_net: dict[int, list[Pad]],
        per_net_timeout: float | None = None,
    ) -> dict[int, int]:
        """Identify true blockers using relaxed A* (Issue #2274).

        For each unrouted net, temporarily unblocks all routed-net cells
        and runs A* to find a viable path.  Cells along that relaxed path
        that were originally occupied by routed nets reveal the *true*
        blockers.

        Args:
            failed_nets: List of net IDs that failed to route.
            pads_by_net: Mapping of net ID to its pads.
            per_net_timeout: Optional timeout per relaxed A* search.

        Returns:
            Dict mapping blocking net ID to the number of stuck nets it
            blocks (score).  Higher score = blocks more stuck nets.
        """
        blocker_scores: dict[int, int] = {}

        with self.grid.temporarily_unblock_routed_nets() as unblocker:
            saved_blocked = unblocker._saved_blocked
            saved_net = unblocker._saved_net

            for net_id in failed_nets:
                pads = pads_by_net.get(net_id, [])
                if len(pads) < 2:
                    continue

                # Try finding a relaxed path for the first pair of pads
                net_blockers: set[int] = set()
                for j in range(len(pads) - 1):
                    blockers = self.router.find_blocking_nets_relaxed(
                        pads[j],
                        pads[j + 1],
                        saved_blocked,
                        saved_net,
                        per_net_timeout=per_net_timeout,
                    )
                    net_blockers.update(blockers)

                for blocker in net_blockers:
                    blocker_scores[blocker] = blocker_scores.get(blocker, 0) + 1

        return blocker_scores

    def neighborhood_ripup(
        self,
        failed_nets: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        present_cost_factor: float,
        mark_route_callback: callable,
        stall_count: int = 0,
        per_net_timeout: float | None = None,
        max_attempts: int = 3,
        initial_radius_factor: float = 1.0,
        escalation_factor: float = 2.0,
        ripup_history: dict[int, int] | None = None,
        max_ripups_per_net: int = 5,
    ) -> tuple[bool, int]:
        """Perform neighborhood rip-up for stuck nets (Issue #2274).

        When negotiated routing stalls with 0 conflicts but unrouted nets
        remain, this method:
        1. Uses relaxed A* to identify true blocking nets
        2. Scores blockers by how many stuck nets they block
        3. Rips up highest-scoring blockers and their neighborhood
        4. Routes the stuck net first, then re-routes displaced nets

        The rip-up radius escalates on repeated stalls.

        Args:
            failed_nets: Nets that failed to route.
            net_routes: Dict of net_id -> list of routes.
            routes_list: Master route list.
            pads_by_net: Dict of net_id -> list of pads.
            present_cost_factor: Current congestion cost factor.
            mark_route_callback: Callback to mark routes on the grid.
            stall_count: How many consecutive stalls have occurred.
            per_net_timeout: Optional per-net A* timeout.
            max_attempts: Maximum rip-up attempts per call.
            initial_radius_factor: Initial bounding-box expansion factor.
            escalation_factor: Multiplier applied to radius on each stall.
            ripup_history: Optional dict tracking per-net rip-up counts.
            max_ripups_per_net: Maximum rip-ups per net to prevent loops.

        Returns:
            Tuple of (improved, new_routed_count) where improved is True if
            more nets were routed than before.
        """
        if ripup_history is None:
            ripup_history = {}

        def _count_routed() -> int:
            """Count nets that have at least one route."""
            return sum(1 for routes in net_routes.values() if routes)

        initial_routed = _count_routed()
        radius_factor = initial_radius_factor * (escalation_factor ** stall_count)
        attempts = 0

        # Step 1: Find true blockers via relaxed A*
        blocker_scores = self.find_blocking_nets_relaxed(
            failed_nets, pads_by_net, per_net_timeout=per_net_timeout,
        )

        if not blocker_scores:
            # No relaxed path found for any stuck net -- truly unroutable
            return False, _count_routed()

        # Sort blockers by score descending (block the most stuck nets first)
        sorted_blockers = sorted(blocker_scores.items(), key=lambda x: -x[1])

        for blocker_net, score in sorted_blockers:
            if attempts >= max_attempts:
                break

            # Check ripup budget
            if ripup_history.get(blocker_net, 0) >= max_ripups_per_net:
                continue

            attempts += 1
            ripup_history[blocker_net] = ripup_history.get(blocker_net, 0) + 1

            # Compute bounding box around the blocker's route and expand by radius
            blocker_routes = net_routes.get(blocker_net, [])
            if not blocker_routes:
                continue

            # Get bounding box of blocker net's routes
            min_gx = float("inf")
            min_gy = float("inf")
            max_gx = float("-inf")
            max_gy = float("-inf")
            for route in blocker_routes:
                for seg in route.segments:
                    gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
                    gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
                    min_gx = min(min_gx, gx1, gx2)
                    min_gy = min(min_gy, gy1, gy2)
                    max_gx = max(max_gx, gx1, gx2)
                    max_gy = max(max_gy, gy1, gy2)

            # Expand bounding box by radius
            bbox_w = max_gx - min_gx + 1
            bbox_h = max_gy - min_gy + 1
            expand = int(max(bbox_w, bbox_h) * radius_factor)
            bb_x1 = int(min_gx) - expand
            bb_y1 = int(min_gy) - expand
            bb_x2 = int(max_gx) + expand
            bb_y2 = int(max_gy) + expand

            # Find all routed nets passing through the expanded bounding box
            neighborhood_nets: set[int] = {blocker_net}
            for net_id, routes in net_routes.items():
                if net_id in neighborhood_nets:
                    continue
                for route in routes:
                    found = False
                    for seg in route.segments:
                        gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
                        gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
                        # Check if segment intersects the bounding box
                        if (
                            max(gx1, gx2) >= bb_x1
                            and min(gx1, gx2) <= bb_x2
                            and max(gy1, gy2) >= bb_y1
                            and min(gy1, gy2) <= bb_y2
                        ):
                            neighborhood_nets.add(net_id)
                            found = True
                            break
                    if found:
                        break

            # Identify which failed nets could benefit from this rip-up
            affected_failed = [
                n for n in failed_nets
                if n not in net_routes or not net_routes.get(n)
            ]

            # Rip up the neighborhood
            self.rip_up_nets(list(neighborhood_nets), net_routes, routes_list)

            # Route the failed nets first (priority)
            for fn in affected_failed:
                fn_pads = pads_by_net.get(fn, [])
                if fn_pads and len(fn_pads) >= 2:
                    routes = self.route_net_negotiated(
                        fn_pads, present_cost_factor, mark_route_callback,
                        per_net_timeout=per_net_timeout,
                    )
                    if routes:
                        net_routes[fn] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            routes_list.append(route)

            # Re-route the displaced nets
            for net_id in neighborhood_nets:
                if net_id in net_routes and net_routes[net_id]:
                    continue  # Already re-routed as a failed net
                net_pads = pads_by_net.get(net_id, [])
                if net_pads and len(net_pads) >= 2:
                    routes = self.route_net_negotiated(
                        net_pads, present_cost_factor, mark_route_callback,
                        per_net_timeout=per_net_timeout,
                    )
                    if routes:
                        net_routes[net_id] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            routes_list.append(route)

            # Check if we improved
            current_routed = _count_routed()
            if current_routed > initial_routed:
                return True, current_routed

        final_routed = _count_routed()
        return final_routed > initial_routed, final_routed

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
    ) -> tuple[bool, int, int]:
        """Try escape strategies when router is stuck oscillating.

        When the negotiated router is stuck cycling between states without
        making progress (detected via detect_oscillation), this method
        tries strategies starting from strategy_index, cycling through all
        remaining strategies until one succeeds or all are exhausted.

        Args:
            overflow_history: List of overflow values from previous iterations
            net_routes: Dictionary of net_id -> list of routes
            routes_list: Master list of all routes
            pads_by_net: Dictionary of net_id -> list of pads
            net_order: List of net IDs in routing priority order
            present_cost_factor: Current congestion cost factor
            mark_route_callback: Callback to mark routes on the grid
            strategy_index: Index of strategy to start from (cycles through available)

        Returns:
            Tuple of (success, overflow, strategies_tried) where success indicates
            if overflow improved and strategies_tried is how many were attempted
        """
        strategies = [
            self._escape_shuffle_order,
            self._escape_reverse_order,
            self._escape_random_subset,
            self._escape_full_reorder,
        ]

        num_strategies = len(strategies)
        strategies_tried = 0

        for i in range(num_strategies):
            idx = (strategy_index + i) % num_strategies
            strategy = strategies[idx]
            strategies_tried += 1

            success, new_overflow = strategy(
                overflow_history=overflow_history,
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                net_order=net_order,
                present_cost_factor=present_cost_factor,
                mark_route_callback=mark_route_callback,
            )

            if success:
                return True, new_overflow, strategies_tried

        # All strategies exhausted without success
        current_overflow = overflow_history[-1] if overflow_history else 0
        return False, current_overflow, strategies_tried

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

        # Accept escape if overflow improved and at least some nets survived
        # (Issue #762: require rerouted_count > 0 to avoid false success on total loss)
        # (Issue #1638: relaxed from requiring ALL nets to allow partial progress)
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow

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

        # Accept escape if overflow improved and at least some nets survived
        # (Issue #762: require rerouted_count > 0 to avoid false success on total loss)
        # (Issue #1638: relaxed from requiring ALL nets to allow partial progress)
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow

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

        # Accept escape if overflow improved and at least some nets survived
        # (Issue #762: require rerouted_count > 0 to avoid false success on total loss)
        # (Issue #1638: relaxed from requiring ALL nets to allow partial progress)
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow

    def _escape_full_reorder(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
    ) -> tuple[bool, int]:
        """Escape strategy: rip up ALL nets and reroute in alternative order.

        Issue #1823: The existing escape strategies only perturb nets passing
        through overused cells.  If the root ordering places net A before net B,
        and A's path blocks B's only viable route without itself being in an
        overused cell, none of the first three strategies will fix this.

        This strategy rips up every routed net and reroutes them all in a
        completely different order (reversed net_order).  It is more expensive
        but can escape ordering-dependent local minima.
        """
        all_nets = list(net_routes.keys())
        if not all_nets:
            return False, overflow_history[-1] if overflow_history else 0

        # Rip up ALL routed nets
        self.rip_up_nets(all_nets, net_routes, routes_list)

        # Build alternative order: reverse of the priority net_order,
        # then append any nets not in net_order.
        net_set = set(all_nets)
        ordered = [n for n in reversed(net_order) if n in net_set]
        remaining = [n for n in all_nets if n not in set(net_order)]
        random.shuffle(remaining)
        reorder = ordered + remaining

        # Reroute all nets in the alternative order with boosted cost
        boosted_cost = present_cost_factor * 1.5
        rerouted_count = 0
        for net in reorder:
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                routes = self.route_net_negotiated(
                    net_pads, boosted_cost, mark_route_callback
                )
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Accept if overflow improved and at least some nets survived
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow
