"""
Pluggable heuristics for A* pathfinding.

This module provides:
- Heuristic: Abstract base class for A* heuristics
- HeuristicContext: Context passed to heuristics for state access
- ManhattanHeuristic: Simple Manhattan distance (baseline)
- CongestionAwareHeuristic: Congestion-aware with direction bias (default)
- DirectionBiasHeuristic: Manhattan with strong direction preference

Heuristics can be swapped at runtime to experiment with different routing strategies.

Usage:
    from kicad_tools.router.heuristics import CongestionAwareHeuristic

    router = Router(grid, rules, heuristic=CongestionAwareHeuristic())
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .rules import DesignRules


@dataclass
class HeuristicContext:
    """Context providing state access for heuristic computation.

    This is passed to heuristics so they can access grid state, rules,
    and goal information without tight coupling to Router internals.
    """

    # Goal coordinates (grid cells)
    goal_x: int
    goal_y: int
    goal_layer: int

    # Design rules for cost parameters
    rules: DesignRules

    # Net class cost multiplier (lower = prefer this net)
    cost_multiplier: float = 1.0

    # Congestion lookup function: (gx, gy, layer) -> float
    # Returns congestion level [0, 1] for the cell's region
    get_congestion: Optional[Callable[[int, int, int], float]] = None

    # Congestion cost function: (gx, gy, layer) -> float
    # Returns additional cost based on congestion at location
    get_congestion_cost: Optional[Callable[[int, int, int], float]] = None


class Heuristic(ABC):
    """Abstract base class for A* heuristics.

    Heuristics estimate the cost from a position to the goal.
    They must be admissible (never overestimate) for optimal A*,
    but can be inadmissible for weighted/greedy A* variants.

    Subclasses implement `estimate()` to return the heuristic value.
    """

    @property
    def name(self) -> str:
        """Human-readable name for this heuristic."""
        return self.__class__.__name__

    @abstractmethod
    def estimate(
        self,
        x: int,
        y: int,
        layer: int,
        direction: Tuple[int, int],
        context: HeuristicContext,
    ) -> float:
        """Estimate cost from (x, y, layer) to goal.

        Args:
            x, y: Current grid cell coordinates
            layer: Current layer index
            direction: (dx, dy) direction of travel from parent node
                       (0, 0) if this is the start node
            context: HeuristicContext with goal, rules, and state access

        Returns:
            Estimated cost to reach goal (should be admissible for optimal A*)
        """
        pass


class ManhattanHeuristic(Heuristic):
    """Simple Manhattan distance heuristic.

    This is the baseline admissible heuristic - purely geometric,
    ignores congestion and direction. Fast but may explore more nodes.

    Cost = |dx| + |dy| + layer_change_cost
    """

    @property
    def name(self) -> str:
        return "Manhattan"

    def estimate(
        self,
        x: int,
        y: int,
        layer: int,
        direction: Tuple[int, int],
        context: HeuristicContext,
    ) -> float:
        dx = abs(x - context.goal_x)
        dy = abs(y - context.goal_y)
        dl = abs(layer - context.goal_layer) * context.rules.cost_via

        base_cost = (dx + dy) * context.rules.cost_straight + dl
        return base_cost * context.cost_multiplier


class DirectionBiasHeuristic(Heuristic):
    """Manhattan with direction alignment penalty.

    Adds a penalty when current direction doesn't align with goal direction.
    This encourages straighter paths with fewer turns.

    Good for: Clean routing with minimal bends
    Trade-off: May miss shortcuts that require direction changes
    """

    def __init__(self, turn_penalty_factor: float = 0.5):
        """
        Args:
            turn_penalty_factor: Fraction of turn cost to add in heuristic
                                 (0.5 = half the actual turn cost)
        """
        self.turn_penalty_factor = turn_penalty_factor

    @property
    def name(self) -> str:
        return f"DirectionBias({self.turn_penalty_factor})"

    def estimate(
        self,
        x: int,
        y: int,
        layer: int,
        direction: Tuple[int, int],
        context: HeuristicContext,
    ) -> float:
        dx = abs(x - context.goal_x)
        dy = abs(y - context.goal_y)
        dl = abs(layer - context.goal_layer) * context.rules.cost_via

        base_cost = (dx + dy) * context.rules.cost_straight + dl

        # Direction alignment penalty
        direction_cost = 0.0
        if direction != (0, 0) and (dx + dy > 0):
            pdx, pdy = direction
            # Calculate ideal direction to goal
            goal_dx = context.goal_x - x
            goal_dy = context.goal_y - y
            gdx = 1 if goal_dx > 0 else (-1 if goal_dx < 0 else 0)
            gdy = 1 if goal_dy > 0 else (-1 if goal_dy < 0 else 0)

            # Penalty if not aligned with goal direction
            if (pdx, pdy) != (gdx, gdy):
                direction_cost = context.rules.cost_turn * self.turn_penalty_factor

        return (base_cost + direction_cost) * context.cost_multiplier


class CongestionAwareHeuristic(Heuristic):
    """Congestion-aware heuristic with direction bias.

    This is the default heuristic that was previously hardcoded.
    It samples congestion at current position and midpoint to goal,
    adding penalties for congested areas.

    Good for: Avoiding routing hotspots, spreading routes evenly
    Trade-off: Slightly more computation per node
    """

    def __init__(
        self,
        congestion_weight: float = 1.0,
        midpoint_weight: float = 0.5,
        turn_penalty_factor: float = 0.3,
    ):
        """
        Args:
            congestion_weight: Weight for current-position congestion
            midpoint_weight: Weight for midpoint congestion sampling
            turn_penalty_factor: Fraction of turn cost for direction misalignment
        """
        self.congestion_weight = congestion_weight
        self.midpoint_weight = midpoint_weight
        self.turn_penalty_factor = turn_penalty_factor

    @property
    def name(self) -> str:
        return "CongestionAware"

    def estimate(
        self,
        x: int,
        y: int,
        layer: int,
        direction: Tuple[int, int],
        context: HeuristicContext,
    ) -> float:
        dx = abs(x - context.goal_x)
        dy = abs(y - context.goal_y)
        dl = abs(layer - context.goal_layer) * context.rules.cost_via

        base_cost = (dx + dy) * context.rules.cost_straight + dl

        # Congestion estimate
        congestion_cost = 0.0
        if dx + dy > 0 and context.get_congestion_cost is not None:
            # Sample at current position
            congestion_cost += context.get_congestion_cost(x, y, layer) * self.congestion_weight
            # Sample at midpoint
            mid_x = (x + context.goal_x) // 2
            mid_y = (y + context.goal_y) // 2
            congestion_cost += (
                context.get_congestion_cost(mid_x, mid_y, layer) * self.midpoint_weight
            )

        # Direction alignment penalty
        direction_cost = 0.0
        if direction != (0, 0) and (dx + dy > 0):
            pdx, pdy = direction
            goal_dx = context.goal_x - x
            goal_dy = context.goal_y - y
            gdx = 1 if goal_dx > 0 else (-1 if goal_dx < 0 else 0)
            gdy = 1 if goal_dy > 0 else (-1 if goal_dy < 0 else 0)

            if (pdx, pdy) != (gdx, gdy):
                direction_cost = context.rules.cost_turn * self.turn_penalty_factor

        return (base_cost + congestion_cost + direction_cost) * context.cost_multiplier


class WeightedCongestionHeuristic(Heuristic):
    """Heavily weighted congestion avoidance.

    Uses stronger congestion penalties and samples more points along
    the estimated path. Better at avoiding congested areas but may
    produce longer routes.

    Good for: Very congested boards, spreading routes
    Trade-off: Longer routes, more via usage
    """

    def __init__(self, num_samples: int = 3, congestion_multiplier: float = 2.0):
        """
        Args:
            num_samples: Number of points to sample along path to goal
            congestion_multiplier: Multiplier for congestion penalties
        """
        self.num_samples = num_samples
        self.congestion_multiplier = congestion_multiplier

    @property
    def name(self) -> str:
        return f"WeightedCongestion(x{self.congestion_multiplier})"

    def estimate(
        self,
        x: int,
        y: int,
        layer: int,
        direction: Tuple[int, int],
        context: HeuristicContext,
    ) -> float:
        dx = abs(x - context.goal_x)
        dy = abs(y - context.goal_y)
        dl = abs(layer - context.goal_layer) * context.rules.cost_via

        base_cost = (dx + dy) * context.rules.cost_straight + dl

        # Sample congestion at multiple points
        congestion_cost = 0.0
        if dx + dy > 0 and context.get_congestion_cost is not None:
            for i in range(self.num_samples):
                t = i / max(1, self.num_samples - 1)
                sample_x = int(x + t * (context.goal_x - x))
                sample_y = int(y + t * (context.goal_y - y))
                sample_cost = context.get_congestion_cost(sample_x, sample_y, layer)
                congestion_cost += sample_cost * self.congestion_multiplier / self.num_samples

        return (base_cost + congestion_cost) * context.cost_multiplier


class GreedyHeuristic(Heuristic):
    """Greedy heuristic that heavily weights distance to goal.

    Multiplies the base heuristic by a factor > 1, making A* behave
    more like greedy best-first search. Finds paths faster but they
    may not be optimal.

    Good for: Fast routing when optimality isn't critical
    Trade-off: Suboptimal paths, more vias
    """

    def __init__(self, greed_factor: float = 2.0):
        """
        Args:
            greed_factor: Multiplier for heuristic (1.0 = optimal A*, >1 = greedy)
        """
        self.greed_factor = greed_factor

    @property
    def name(self) -> str:
        return f"Greedy(x{self.greed_factor})"

    def estimate(
        self,
        x: int,
        y: int,
        layer: int,
        direction: Tuple[int, int],
        context: HeuristicContext,
    ) -> float:
        dx = abs(x - context.goal_x)
        dy = abs(y - context.goal_y)
        dl = abs(layer - context.goal_layer) * context.rules.cost_via

        base_cost = (dx + dy) * context.rules.cost_straight + dl

        # Apply greed factor to encourage faster exploration toward goal
        return base_cost * self.greed_factor * context.cost_multiplier


# Default heuristic instance
DEFAULT_HEURISTIC = CongestionAwareHeuristic()
