"""
Routing optimization and quality metrics.

Provides the RoutingOptimizer class for optimizing routing parameters
and the FigureOfMerit class for evaluating routing quality.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router import Autorouter, DesignRules
    from kicad_tools.router.failure_analysis import CongestionMap
    from kicad_tools.router.primitives import Pad

__all__ = ["FigureOfMerit", "RoutingOptimizer", "estimate_net_congestion"]


def estimate_net_congestion(
    net_pads: list[Pad],
    congestion_map: CongestionMap,
    margin: float = 2.0,
) -> float:
    """Estimate how congested a net's routing corridor will be.

    For each pair of pads, samples congestion along the Manhattan bounding box
    (routing corridor) between them. Returns the average congestion score.

    Args:
        net_pads: List of Pad objects for the net.
        congestion_map: CongestionMap to query for congestion values.
        margin: Margin around the routing corridor in mm. Default 2.0.

    Returns:
        Average congestion score (0.0-1.0) across all pad pairs.
        Returns 0.0 if the net has fewer than 2 pads.

    Example::

        from kicad_tools.optim import estimate_net_congestion

        pads = [pad1, pad2, pad3]
        congestion_map = router.get_congestion_map()
        score = estimate_net_congestion(pads, congestion_map)
        print(f"Net congestion: {score:.2f}")
    """
    if len(net_pads) < 2:
        return 0.0

    # Import Rectangle locally to avoid circular imports
    from kicad_tools.router.failure_analysis import Rectangle

    total_congestion = 0.0
    pair_count = 0

    # Sample congestion for each pair of pads
    for p1, p2 in combinations(net_pads, 2):
        # Create bounding box (routing corridor) between the two pads
        min_x = min(p1.x, p2.x) - margin
        max_x = max(p1.x, p2.x) + margin
        min_y = min(p1.y, p2.y) - margin
        max_y = max(p1.y, p2.y) + margin

        corridor = Rectangle(min_x, min_y, max_x, max_y)
        total_congestion += congestion_map.get_congestion(corridor)
        pair_count += 1

    return total_congestion / pair_count if pair_count > 0 else 0.0


@dataclass
class FigureOfMerit:
    """
    Figure of merit computation for routing quality.

    Provides metrics for evaluating routing results including completion rate,
    via count, wire length, and a composite score for optimization comparisons.

    Example::

        from kicad_tools.optim import FigureOfMerit

        fom = FigureOfMerit(
            nets_total=10,
            nets_routed=10,
            vias=5,
            segments=25,
            corners=12,
            total_length_mm=150.0,
            routing_time_s=2.5,
        )
        print(f"Completion: {fom.completion_rate:.0%}")
        print(f"Score: {fom.score:.1f}")
    """

    nets_total: int
    nets_routed: int
    vias: int
    segments: int
    corners: int
    total_length_mm: float
    routing_time_s: float
    drc_violations: int = 0

    @property
    def completion_rate(self) -> float:
        """
        Fraction of nets successfully routed (0.0 to 1.0).

        Returns:
            Completion rate, or 0.0 if no nets to route.
        """
        return self.nets_routed / self.nets_total if self.nets_total > 0 else 0.0

    @property
    def score(self) -> float:
        """
        Combined quality score (higher = better).

        Scoring:
        - Base score of 1000 for complete routing
        - Large penalty (-1000 per missing net) for incomplete routing
        - Penalties: -10 per via, -1 per corner, -0.1 per mm, -100 per DRC violation

        Returns:
            Composite score for comparing routing results.
        """
        if self.completion_rate < 1.0:
            # Incomplete routing: heavy penalty proportional to missing nets
            return -1000 * (1 - self.completion_rate)
        return (
            1000
            - self.vias * 10
            - self.corners * 1
            - self.total_length_mm * 0.1
            - self.drc_violations * 100
        )

    @classmethod
    def from_routes(
        cls,
        routes: list,
        nets_total: int,
        routing_time_s: float = 0.0,
        drc_violations: int = 0,
    ) -> FigureOfMerit:
        """
        Create FigureOfMerit from a list of Route objects.

        Args:
            routes: List of Route objects from autorouter
            nets_total: Total number of nets that should be routed
            routing_time_s: Time taken for routing in seconds
            drc_violations: Number of DRC violations found

        Returns:
            FigureOfMerit computed from routing results
        """
        # Count unique nets that were successfully routed
        routed_nets: set[int] = set()
        total_vias = 0
        total_segments = 0
        total_corners = 0
        total_length = 0.0

        for route in routes:
            if route.segments:
                routed_nets.add(route.net)

            total_vias += len(route.vias)
            total_segments += len(route.segments)

            # Count corners by detecting direction changes in segments
            prev_dx, prev_dy = None, None
            for seg in route.segments:
                dx = seg.x2 - seg.x1
                dy = seg.y2 - seg.y1
                # Normalize direction
                length = math.sqrt(dx * dx + dy * dy)
                if length > 1e-10:
                    dx, dy = dx / length, dy / length
                    if prev_dx is not None:
                        # Check if direction changed (dot product < ~1)
                        dot = prev_dx * dx + prev_dy * dy
                        if dot < 0.99:  # Not same direction
                            total_corners += 1
                    prev_dx, prev_dy = dx, dy
                    total_length += length

        return cls(
            nets_total=nets_total,
            nets_routed=len(routed_nets),
            vias=total_vias,
            segments=total_segments,
            corners=total_corners,
            total_length_mm=total_length,
            routing_time_s=routing_time_s,
            drc_violations=drc_violations,
        )


class RoutingOptimizer:
    """
    Routing parameter optimizer using metaheuristics.

    Provides methods to optimize routing parameters such as via cost,
    net ordering, and grid resolution to achieve better routing results.

    Example::

        from kicad_tools.optim import RoutingOptimizer
        from kicad_tools.router import Autorouter, DesignRules

        optimizer = RoutingOptimizer()

        # Optimize via cost using binary search
        def create_router(via_cost: float) -> Autorouter:
            rules = DesignRules(cost_via=via_cost)
            router = Autorouter(100, 80, rules=rules)
            # ... add components ...
            return router

        best_cost, best_fom = optimizer.optimize_via_cost(create_router)
        print(f"Optimal via cost: {best_cost}, Score: {best_fom.score}")
    """

    def __init__(self, base_rules: DesignRules | None = None) -> None:
        """
        Initialize the routing optimizer.

        Args:
            base_rules: Optional base design rules to use as defaults.
                       If None, methods will use their own defaults.
        """
        self.base_rules = base_rules

    def _evaluate_routing(
        self, router: Autorouter, route_method: str = "route_all"
    ) -> FigureOfMerit:
        """
        Evaluate routing quality for a configured router.

        Args:
            router: Autorouter instance with components added
            route_method: Name of routing method to call

        Returns:
            FigureOfMerit for the routing result
        """
        import time

        nets_total = len([n for n in router.nets if n > 0])

        start_time = time.time()
        method = getattr(router, route_method)
        routes = method()
        routing_time = time.time() - start_time

        return FigureOfMerit.from_routes(
            routes=routes,
            nets_total=nets_total,
            routing_time_s=routing_time,
        )

    def optimize_via_cost(
        self,
        router_factory: Callable[[float], Autorouter],
        min_cost: float = 1.0,
        max_cost: float = 20.0,
        tolerance: float = 0.5,
    ) -> tuple[float, FigureOfMerit]:
        """
        Binary search for optimal via cost.

        Higher via cost = fewer vias but may fail to route.
        Finds the highest via cost that still routes all nets.

        Args:
            router_factory: Callable that creates an Autorouter given via cost.
                           Should add all components and be ready for route_all().
            min_cost: Minimum via cost to try
            max_cost: Maximum via cost to try
            tolerance: Stop when search range is within this tolerance

        Returns:
            Tuple of (optimal via cost, FigureOfMerit at that cost)

        Example::

            def create_router(via_cost: float) -> Autorouter:
                rules = DesignRules(cost_via=via_cost)
                router = Autorouter(100, 80, rules=rules)
                # ... add components ...
                return router

            best_cost, fom = optimizer.optimize_via_cost(create_router)
        """
        best_cost = min_cost
        best_fom: FigureOfMerit | None = None

        while max_cost - min_cost > tolerance:
            mid = (min_cost + max_cost) / 2
            router = router_factory(mid)
            fom = self._evaluate_routing(router)

            if fom.completion_rate == 1.0:
                # Successful routing, try higher via cost
                best_cost = mid
                best_fom = fom
                min_cost = mid
            else:
                # Failed to route all nets, need lower via cost
                max_cost = mid

        # If we never got a successful routing, try the minimum cost
        if best_fom is None:
            router = router_factory(min_cost)
            best_fom = self._evaluate_routing(router)
            best_cost = min_cost

        return best_cost, best_fom

    def optimize_net_order(
        self,
        router_factory: Callable[[], Autorouter],
        method: str = "greedy",
        iterations: int = 1000,
        congestion_map: CongestionMap | None = None,
    ) -> tuple[list[int], FigureOfMerit]:
        """
        Find optimal net routing order.

        The order in which nets are routed affects success rate.
        Early nets get preferred paths, later nets route around them.

        Args:
            router_factory: Callable that creates a fresh Autorouter instance.
                           Should add all components and be ready for routing.
            method: Optimization method:
                - "greedy": Route shortest/simplest nets first
                - "critical_first": Route timing-critical and power nets first
                - "congestion": Route through congested areas first (requires congestion_map)
                - "hybrid": Combine critical_first with congestion awareness (requires congestion_map)
                - "simulated_annealing": Probabilistic optimization (uses iterations)
            iterations: Number of iterations for simulated_annealing method
            congestion_map: Optional CongestionMap for congestion-aware ordering.
                           Required for "congestion" and "hybrid" methods.

        Returns:
            Tuple of (optimal net order, FigureOfMerit with that order)

        Example::

            optimizer = RoutingOptimizer()
            router = router_factory()
            congestion_map = router.get_congestion_map()

            # Use hybrid ordering combining power/clock priority with congestion
            order, fom = optimizer.optimize_net_order(
                router_factory,
                method="hybrid",
                congestion_map=congestion_map,
            )
        """
        import random

        # Create initial router to get net information
        router = router_factory()
        net_ids = [n for n in router.nets if n > 0]

        if not net_ids:
            return [], FigureOfMerit(0, 0, 0, 0, 0, 0.0, 0.0)

        if method == "greedy":
            # Sort by number of pads (fewer pads = simpler net = route first)
            order = sorted(net_ids, key=lambda n: len(router.nets.get(n, [])))

        elif method == "critical_first":
            # Route power and clock nets first (they get priority paths)
            def net_priority(net_id: int) -> tuple[int, int]:
                net_name = router.net_names.get(net_id, "").lower()
                # Power nets get highest priority (0)
                if any(p in net_name for p in ["vcc", "vdd", "gnd", "+3", "+5", "pwr"]):
                    return (0, len(router.nets.get(net_id, [])))
                # Clock nets next (1)
                if any(p in net_name for p in ["clk", "clock", "mclk", "sclk"]):
                    return (1, len(router.nets.get(net_id, [])))
                # Everything else by pad count
                return (2, len(router.nets.get(net_id, [])))

            order = sorted(net_ids, key=net_priority)

        elif method == "congestion":
            # Route through congested areas first while routing options exist
            if congestion_map is None:
                raise ValueError(
                    "congestion_map is required for 'congestion' method. "
                    "Use router.get_congestion_map() to create one."
                )

            order = self._order_by_congestion(router, net_ids, congestion_map)

        elif method == "hybrid":
            # Combine critical_first with congestion awareness
            if congestion_map is None:
                raise ValueError(
                    "congestion_map is required for 'hybrid' method. "
                    "Use router.get_congestion_map() to create one."
                )

            order = self._order_hybrid(router, net_ids, congestion_map)

        elif method == "simulated_annealing":
            # Start with greedy order
            order = sorted(net_ids, key=lambda n: len(router.nets.get(n, [])))

            # Evaluate initial order
            router = router_factory()
            routes = router.route_all(net_order=order)
            best_fom = FigureOfMerit.from_routes(routes, len(net_ids))
            best_order = order.copy()

            temperature = 1.0
            cooling_rate = 0.995

            for i in range(iterations):
                # Random swap
                new_order = order.copy()
                if len(new_order) >= 2:
                    idx1, idx2 = random.sample(range(len(new_order)), 2)
                    new_order[idx1], new_order[idx2] = new_order[idx2], new_order[idx1]

                # Evaluate new order
                router = router_factory()
                routes = router.route_all(net_order=new_order)
                new_fom = FigureOfMerit.from_routes(routes, len(net_ids))

                # Accept or reject
                delta = new_fom.score - best_fom.score
                if delta > 0 or random.random() < math.exp(delta / temperature):
                    order = new_order
                    if new_fom.score > best_fom.score:
                        best_fom = new_fom
                        best_order = new_order.copy()

                temperature *= cooling_rate

            return best_order, best_fom

        else:
            raise ValueError(f"Unknown optimization method: {method}")

        # Evaluate the determined order
        router = router_factory()
        routes = router.route_all(net_order=order)
        fom = FigureOfMerit.from_routes(routes, len(net_ids))

        return order, fom

    def _order_by_congestion(
        self,
        router: Autorouter,
        net_ids: list[int],
        congestion_map: CongestionMap,
    ) -> list[int]:
        """Order nets by congestion level (highest congestion first).

        Nets passing through congested areas are routed first while routing
        options still exist. This gives congested nets more path choices.

        Args:
            router: Autorouter with pads and nets defined.
            net_ids: List of net IDs to order.
            congestion_map: CongestionMap for querying congestion levels.

        Returns:
            Net IDs sorted by descending congestion score.
        """
        scored_nets: list[tuple[float, int]] = []

        for net_id in net_ids:
            pad_keys = router.nets.get(net_id, [])
            net_pads = [router.pads[key] for key in pad_keys if key in router.pads]

            congestion_score = estimate_net_congestion(net_pads, congestion_map)
            scored_nets.append((congestion_score, net_id))

        # Sort by congestion descending (highest congestion first)
        scored_nets.sort(reverse=True, key=lambda x: x[0])

        return [net_id for _, net_id in scored_nets]

    def _order_hybrid(
        self,
        router: Autorouter,
        net_ids: list[int],
        congestion_map: CongestionMap,
    ) -> list[int]:
        """Combine priority-based ordering with congestion awareness.

        Orders nets in tiers:
        1. Power nets (VCC, VDD, GND, etc.) - critical infrastructure
        2. High-speed signals (CLK, etc.) - timing sensitive
        3. Remaining nets sorted by congestion (highest first)

        Within power and high-speed tiers, nets are also sorted by congestion.

        Args:
            router: Autorouter with pads and nets defined.
            net_ids: List of net IDs to order.
            congestion_map: CongestionMap for querying congestion levels.

        Returns:
            Net IDs in hybrid priority order.
        """
        power_nets: list[tuple[float, int]] = []
        highspeed_nets: list[tuple[float, int]] = []
        other_nets: list[tuple[float, int]] = []

        for net_id in net_ids:
            net_name = router.net_names.get(net_id, "").lower()
            pad_keys = router.nets.get(net_id, [])
            net_pads = [router.pads[key] for key in pad_keys if key in router.pads]

            congestion_score = estimate_net_congestion(net_pads, congestion_map)

            # Classify net by type
            if any(p in net_name for p in ["vcc", "vdd", "gnd", "+3", "+5", "pwr", "vss"]):
                power_nets.append((congestion_score, net_id))
            elif any(p in net_name for p in ["clk", "clock", "mclk", "sclk"]):
                highspeed_nets.append((congestion_score, net_id))
            else:
                other_nets.append((congestion_score, net_id))

        # Sort each tier by congestion (highest first within tier)
        power_nets.sort(reverse=True, key=lambda x: x[0])
        highspeed_nets.sort(reverse=True, key=lambda x: x[0])
        other_nets.sort(reverse=True, key=lambda x: x[0])

        # Combine: power first, then high-speed, then remaining by congestion
        result: list[int] = []
        result.extend(net_id for _, net_id in power_nets)
        result.extend(net_id for _, net_id in highspeed_nets)
        result.extend(net_id for _, net_id in other_nets)

        return result

    def optimize_grid_resolution(
        self,
        router_factory: Callable[[float], Autorouter],
        min_resolution: float = 0.05,
        max_resolution: float = 0.5,
        steps: int = 5,
    ) -> tuple[float, FigureOfMerit]:
        """
        Find coarsest grid that still routes all nets.

        Coarser grids are faster but may fail on tight layouts.
        This finds the optimal trade-off.

        Args:
            router_factory: Callable that creates an Autorouter given grid resolution.
                           Should configure DesignRules with the provided resolution.
            min_resolution: Finest grid resolution to try (mm)
            max_resolution: Coarsest grid resolution to try (mm)
            steps: Number of resolution steps to try

        Returns:
            Tuple of (optimal resolution, FigureOfMerit at that resolution)
        """
        best_resolution = min_resolution
        best_fom: FigureOfMerit | None = None

        # Try resolutions from coarse to fine
        for i in range(steps):
            # Linear interpolation from max to min
            resolution = max_resolution - (max_resolution - min_resolution) * i / (steps - 1)

            router = router_factory(resolution)
            fom = self._evaluate_routing(router)

            if fom.completion_rate == 1.0:
                # Found a working resolution
                if best_fom is None or resolution > best_resolution:
                    best_resolution = resolution
                    best_fom = fom
                break  # Coarsest working resolution found

        # If no resolution worked, use the finest
        if best_fom is None:
            router = router_factory(min_resolution)
            best_fom = self._evaluate_routing(router)
            best_resolution = min_resolution

        return best_resolution, best_fom
