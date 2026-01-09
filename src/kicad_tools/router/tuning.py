"""
Auto-tuning cost parameters for PCB routing optimization.

This module provides automatic tuning of routing cost parameters using:
- Heuristic-based quick tuning from board characteristics
- Gradient-free optimization for thorough parameter search
- Preset cost profiles for common scenarios
- Adaptive cost adjustment during routing

Cost parameters significantly impact routing quality:
- Via cost: Higher values force more single-layer routing
- Turn cost: Higher values encourage straighter traces
- Congestion cost: Higher values spread routes across the board
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .core import Autorouter
    from .primitives import Pad
    from .rules import DesignRules


class CostProfile(Enum):
    """Preset cost profiles for common routing scenarios."""

    SPARSE = "sparse"
    STANDARD = "standard"
    DENSE = "dense"
    MINIMIZE_VIAS = "minimize_vias"
    MINIMIZE_LENGTH = "minimize_length"
    HIGH_SPEED = "high_speed"


@dataclass
class CostParams:
    """Cost parameters for A* routing.

    These parameters control how the router evaluates different path options.
    Lower costs make paths more attractive; higher costs discourage them.

    Attributes:
        via: Penalty for layer changes (higher = fewer vias)
        turn: Penalty for direction changes (higher = straighter traces)
        congestion: Multiplier for congested regions (higher = more spread out)
        straight: Base cost for straight movement
        diagonal: Cost for diagonal movement (typically sqrt(2))
        layer_inner: Penalty for using inner layers
    """

    via: float = 10.0
    turn: float = 5.0
    congestion: float = 2.0
    straight: float = 1.0
    diagonal: float = 1.414
    layer_inner: float = 5.0

    def apply_to_rules(self, rules: DesignRules) -> DesignRules:
        """Create a copy of rules with these cost parameters applied.

        Args:
            rules: Base design rules

        Returns:
            New DesignRules with updated cost parameters
        """
        from dataclasses import replace

        return replace(
            rules,
            cost_via=self.via,
            cost_turn=self.turn,
            cost_congestion=self.congestion,
            cost_straight=self.straight,
            cost_diagonal=self.diagonal,
            cost_layer_inner=self.layer_inner,
        )

    def scale(self, factor: float) -> CostParams:
        """Scale all costs by a factor.

        Args:
            factor: Multiplier for all costs

        Returns:
            New CostParams with scaled values
        """
        return CostParams(
            via=self.via * factor,
            turn=self.turn * factor,
            congestion=self.congestion * factor,
            straight=self.straight * factor,
            diagonal=self.diagonal * factor,
            layer_inner=self.layer_inner * factor,
        )


# Preset cost profiles
COST_PROFILES: dict[CostProfile, CostParams] = {
    CostProfile.SPARSE: CostParams(
        via=5.0,
        turn=2.0,
        congestion=1.5,
        straight=1.0,
        diagonal=1.414,
        layer_inner=3.0,
    ),
    CostProfile.STANDARD: CostParams(
        via=10.0,
        turn=5.0,
        congestion=2.0,
        straight=1.0,
        diagonal=1.414,
        layer_inner=5.0,
    ),
    CostProfile.DENSE: CostParams(
        via=15.0,
        turn=6.0,
        congestion=4.0,
        straight=1.0,
        diagonal=1.414,
        layer_inner=8.0,
    ),
    CostProfile.MINIMIZE_VIAS: CostParams(
        via=25.0,
        turn=3.0,
        congestion=2.0,
        straight=1.0,
        diagonal=1.414,
        layer_inner=10.0,
    ),
    CostProfile.MINIMIZE_LENGTH: CostParams(
        via=5.0,
        turn=8.0,
        congestion=1.5,
        straight=1.0,
        diagonal=1.2,  # Encourage diagonal shortcuts
        layer_inner=3.0,
    ),
    CostProfile.HIGH_SPEED: CostParams(
        via=20.0,
        turn=10.0,  # Minimize bends for signal integrity
        congestion=3.0,
        straight=1.0,
        diagonal=1.414,
        layer_inner=5.0,
    ),
}


@dataclass
class BoardCharacteristics:
    """Analyzed characteristics of a PCB for tuning decisions.

    Attributes:
        total_pads: Total number of pads on the board
        total_nets: Number of nets to route
        board_area: Board area in mm²
        pin_density: Pads per mm²
        avg_net_size: Average number of pads per net
        avg_net_span: Average bounding box diagonal of nets (mm)
        layer_count: Number of routing layers
        aspect_ratio: Board width/height ratio
    """

    total_pads: int = 0
    total_nets: int = 0
    board_area: float = 0.0
    pin_density: float = 0.0
    avg_net_size: float = 0.0
    avg_net_span: float = 0.0
    layer_count: int = 2
    aspect_ratio: float = 1.0


@dataclass
class RoutingQualityScore:
    """Quality metrics for evaluating routing results.

    Attributes:
        completion_rate: Fraction of nets successfully routed (0-1)
        total_vias: Number of vias used
        total_length: Total trace length in mm
        total_segments: Number of trace segments
        avg_via_per_net: Average vias per routed net
        congestion_max: Maximum congestion level (0-1)
        drc_violations: Number of DRC violations (if checked)
        score: Combined quality score (higher is better)
    """

    completion_rate: float = 0.0
    total_vias: int = 0
    total_length: float = 0.0
    total_segments: int = 0
    avg_via_per_net: float = 0.0
    congestion_max: float = 0.0
    drc_violations: int = 0
    score: float = 0.0


@dataclass
class TuningResult:
    """Result of parameter tuning.

    Attributes:
        params: Best cost parameters found
        profile: Selected cost profile (if using profiles)
        characteristics: Board characteristics used for tuning
        quality: Routing quality achieved with these parameters
        iterations: Number of optimization iterations performed
        tuning_time_ms: Time spent tuning in milliseconds
    """

    params: CostParams
    profile: CostProfile | None = None
    characteristics: BoardCharacteristics | None = None
    quality: RoutingQualityScore | None = None
    iterations: int = 0
    tuning_time_ms: float = 0.0


def analyze_board(
    nets: dict[int, list[tuple[str, str]]],
    pads: dict[tuple[str, str], Pad],
    board_width: float,
    board_height: float,
    layer_count: int = 2,
) -> BoardCharacteristics:
    """Analyze board characteristics for tuning decisions.

    Args:
        nets: Dictionary mapping net ID to list of (ref, pin) tuples
        pads: Dictionary mapping (ref, pin) to Pad objects
        board_width: Board width in mm
        board_height: Board height in mm
        layer_count: Number of routing layers

    Returns:
        BoardCharacteristics with computed metrics
    """
    total_pads = len(pads)
    total_nets = len([n for n in nets.keys() if n != 0])
    board_area = board_width * board_height

    if board_area == 0:
        board_area = 1.0  # Avoid division by zero

    pin_density = total_pads / board_area

    # Calculate average net size and span
    net_sizes: list[int] = []
    net_spans: list[float] = []

    for net_id, pad_keys in nets.items():
        if net_id == 0:  # Skip unconnected net
            continue

        pad_objs = [pads.get(k) for k in pad_keys]
        pad_objs = [p for p in pad_objs if p is not None]

        if len(pad_objs) >= 2:
            net_sizes.append(len(pad_objs))

            # Calculate bounding box diagonal
            min_x = min(p.x for p in pad_objs)
            max_x = max(p.x for p in pad_objs)
            min_y = min(p.y for p in pad_objs)
            max_y = max(p.y for p in pad_objs)
            span = math.sqrt((max_x - min_x) ** 2 + (max_y - min_y) ** 2)
            net_spans.append(span)

    avg_net_size = sum(net_sizes) / len(net_sizes) if net_sizes else 2.0
    avg_net_span = sum(net_spans) / len(net_spans) if net_spans else 10.0

    return BoardCharacteristics(
        total_pads=total_pads,
        total_nets=total_nets,
        board_area=board_area,
        pin_density=pin_density,
        avg_net_size=avg_net_size,
        avg_net_span=avg_net_span,
        layer_count=layer_count,
        aspect_ratio=board_width / board_height if board_height > 0 else 1.0,
    )


def select_profile(characteristics: BoardCharacteristics) -> CostProfile:
    """Auto-select a cost profile based on board characteristics.

    Args:
        characteristics: Analyzed board characteristics

    Returns:
        Recommended CostProfile for this board
    """
    # Density thresholds (pads per mm²)
    if characteristics.pin_density < 0.01:
        return CostProfile.SPARSE
    elif characteristics.pin_density < 0.05:
        return CostProfile.STANDARD
    else:
        return CostProfile.DENSE


def quick_tune(
    characteristics: BoardCharacteristics,
    base_params: CostParams | None = None,
) -> CostParams:
    """Fast heuristic-based parameter tuning.

    Uses board characteristics to compute optimal cost parameters
    without running actual routing trials.

    Args:
        characteristics: Analyzed board characteristics
        base_params: Optional base parameters to adjust from

    Returns:
        CostParams tuned for this board
    """
    if base_params is None:
        base_params = COST_PROFILES[CostProfile.STANDARD]

    # Density-based adjustments
    density = characteristics.pin_density

    # Via cost: Higher for dense boards (force more creative routing)
    # Lower for multi-layer boards (vias less expensive)
    via_cost = 5.0 + 200.0 * density  # Scale with density
    via_cost /= characteristics.layer_count / 2  # Lower for more layers
    via_cost = max(3.0, min(30.0, via_cost))

    # Turn cost: Higher for longer average net spans
    # (longer nets benefit more from straighter paths)
    turn_cost = 3.0 + characteristics.avg_net_span * 0.1
    turn_cost = max(2.0, min(10.0, turn_cost))

    # Congestion cost: Higher for dense boards
    congestion_cost = 1.5 + 50.0 * density
    congestion_cost = max(1.5, min(8.0, congestion_cost))

    # Inner layer cost: Lower for boards with many layers
    layer_inner = 8.0 / (characteristics.layer_count / 2)
    layer_inner = max(2.0, min(10.0, layer_inner))

    return CostParams(
        via=via_cost,
        turn=turn_cost,
        congestion=congestion_cost,
        straight=base_params.straight,
        diagonal=base_params.diagonal,
        layer_inner=layer_inner,
    )


def evaluate_routing_quality(
    router: Autorouter,
    params: CostParams,
) -> RoutingQualityScore:
    """Evaluate routing quality with given cost parameters.

    Performs a full routing pass and measures quality metrics.

    Args:
        router: Autorouter instance (will be modified)
        params: Cost parameters to evaluate

    Returns:
        RoutingQualityScore with measured metrics
    """
    # Apply parameters to router's rules
    router.rules = params.apply_to_rules(router.rules)

    # Reset router state
    router.routes.clear()
    router.grid.reset_route_usage()

    # Route all nets
    routes = router.route_all()

    # Calculate metrics
    total_nets = len([n for n in router.nets.keys() if n != 0])
    routed_nets = len({r.net for r in routes})
    completion_rate = routed_nets / total_nets if total_nets > 0 else 1.0

    total_vias = sum(len(r.vias) for r in routes)
    total_segments = sum(len(r.segments) for r in routes)
    total_length = sum(
        math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) for r in routes for s in r.segments
    )

    avg_via_per_net = total_vias / routed_nets if routed_nets > 0 else 0.0

    # Get max congestion from grid
    congestion_max = (
        float(router.grid.congestion.max()) / 100.0 if hasattr(router.grid, "congestion") else 0.0
    )

    # Compute combined score (higher is better)
    score = (
        completion_rate * 100  # Prioritize completion
        - total_vias * 0.5  # Penalize vias
        - total_length * 0.01  # Penalize length
    )

    return RoutingQualityScore(
        completion_rate=completion_rate,
        total_vias=total_vias,
        total_length=total_length,
        total_segments=total_segments,
        avg_via_per_net=avg_via_per_net,
        congestion_max=congestion_max,
        drc_violations=0,
        score=score,
    )


def tune_parameters(
    router: Autorouter,
    max_iterations: int = 10,
    method: str = "nelder-mead",
    initial_params: CostParams | None = None,
) -> TuningResult:
    """Tune cost parameters using gradient-free optimization.

    Runs multiple routing trials with different parameters to find
    the best configuration for this specific board.

    Args:
        router: Autorouter instance to optimize
        max_iterations: Maximum number of optimization iterations
        method: Optimization method ("nelder-mead", "powell", or "quick")
        initial_params: Starting parameters (default: quick_tune result)

    Returns:
        TuningResult with best parameters found
    """
    import time

    start_time = time.time()

    # Analyze board characteristics
    characteristics = analyze_board(
        nets=router.nets,
        pads=router.pads,
        board_width=router.grid.width,
        board_height=router.grid.height,
        layer_count=router.grid.num_layers,
    )

    # Get initial parameters
    if initial_params is None:
        initial_params = quick_tune(characteristics)

    if method == "quick":
        # Just use heuristic tuning
        elapsed_ms = (time.time() - start_time) * 1000
        return TuningResult(
            params=initial_params,
            profile=select_profile(characteristics),
            characteristics=characteristics,
            quality=None,
            iterations=0,
            tuning_time_ms=elapsed_ms,
        )

    # Optimization bounds
    bounds = [
        (3.0, 30.0),  # via cost
        (2.0, 10.0),  # turn cost
        (1.5, 8.0),  # congestion cost
    ]

    best_params = initial_params
    best_score = float("-inf")
    iterations = 0

    def objective(x: list[float]) -> float:
        """Objective function for optimization (minimize negative score)."""
        nonlocal best_params, best_score, iterations
        iterations += 1

        params = CostParams(
            via=x[0],
            turn=x[1],
            congestion=x[2],
            straight=initial_params.straight,
            diagonal=initial_params.diagonal,
            layer_inner=initial_params.layer_inner,
        )

        quality = evaluate_routing_quality(router, params)

        if quality.score > best_score:
            best_score = quality.score
            best_params = params

        return -quality.score  # Minimize negative score

    # Run optimization
    try:
        from scipy.optimize import minimize

        x0 = [initial_params.via, initial_params.turn, initial_params.congestion]

        minimize(
            objective,
            x0=x0,
            method=method,
            bounds=bounds,
            options={"maxiter": max_iterations},
        )
    except ImportError:
        # scipy not available, use simple grid search
        for via in [5.0, 10.0, 15.0, 20.0]:
            for turn in [3.0, 5.0, 7.0]:
                for congestion in [2.0, 4.0, 6.0]:
                    if iterations >= max_iterations:
                        break
                    objective([via, turn, congestion])

    elapsed_ms = (time.time() - start_time) * 1000

    # Evaluate final quality
    final_quality = evaluate_routing_quality(router, best_params)

    return TuningResult(
        params=best_params,
        profile=select_profile(characteristics),
        characteristics=characteristics,
        quality=final_quality,
        iterations=iterations,
        tuning_time_ms=elapsed_ms,
    )


@dataclass
class AdaptiveCostState:
    """State for adaptive cost adjustment during routing.

    Tracks routing progress and adjusts costs dynamically.
    """

    params: CostParams
    iteration: int = 0
    failed_nets: list[int] = field(default_factory=list)
    overflow_count: int = 0
    stuck_count: int = 0


def create_adaptive_router(
    router: Autorouter,
    initial_params: CostParams | None = None,
    max_iterations: int = 5,
) -> Callable[[], list]:
    """Create an adaptive routing function that adjusts costs during routing.

    Returns a function that routes all nets while dynamically adjusting
    cost parameters based on routing progress.

    Args:
        router: Autorouter instance
        initial_params: Starting cost parameters
        max_iterations: Maximum adaptation iterations

    Returns:
        Callable that performs adaptive routing and returns routes
    """
    if initial_params is None:
        characteristics = analyze_board(
            nets=router.nets,
            pads=router.pads,
            board_width=router.grid.width,
            board_height=router.grid.height,
            layer_count=router.grid.num_layers,
        )
        initial_params = quick_tune(characteristics)

    state = AdaptiveCostState(params=initial_params)

    def route_with_adaptation() -> list:
        """Route all nets with adaptive cost adjustment."""
        all_routes = []

        for iteration in range(max_iterations):
            state.iteration = iteration

            # Apply current parameters
            router.rules = state.params.apply_to_rules(router.rules)

            # Reset and route
            router.routes.clear()
            router.grid.reset_route_usage()
            routes = router.route_all()

            # Check results
            total_nets = len([n for n in router.nets.keys() if n != 0])
            routed_nets = len({r.net for r in routes})

            if routed_nets == total_nets:
                # All nets routed successfully
                return routes

            # Track failed nets
            routed_net_ids = {r.net for r in routes}
            state.failed_nets = [
                n for n in router.nets.keys() if n != 0 and n not in routed_net_ids
            ]

            # Adapt parameters based on failure mode
            if state.overflow_count > 0:
                # Increase congestion penalty to spread routes
                state.params = CostParams(
                    via=state.params.via,
                    turn=state.params.turn,
                    congestion=min(state.params.congestion * 1.3, 10.0),
                    straight=state.params.straight,
                    diagonal=state.params.diagonal,
                    layer_inner=state.params.layer_inner,
                )
            elif len(state.failed_nets) > 0 and state.stuck_count > 2:
                # Stuck, try reducing via cost to find alternative paths
                state.params = CostParams(
                    via=max(state.params.via * 0.8, 3.0),
                    turn=state.params.turn,
                    congestion=state.params.congestion,
                    straight=state.params.straight,
                    diagonal=state.params.diagonal,
                    layer_inner=state.params.layer_inner,
                )
                state.stuck_count = 0
            else:
                state.stuck_count += 1

            all_routes = routes

        return all_routes

    return route_with_adaptation
