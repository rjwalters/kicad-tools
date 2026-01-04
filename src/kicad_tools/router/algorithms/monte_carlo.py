"""Monte Carlo multi-start routing algorithm.

This module provides randomized net ordering to escape local minima
caused by unfortunate routing order decisions.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..primitives import Route


class MonteCarloRouter:
    """Monte Carlo multi-start router.

    Tries multiple random net orderings within priority tiers,
    keeping the best result to escape local minima.
    """

    def __init__(self, total_nets: int):
        """Initialize the Monte Carlo router.

        Args:
            total_nets: Total number of nets (excluding net 0)
        """
        self.total_nets = total_nets

    def shuffle_within_tiers(
        self,
        net_order: list[int],
        get_priority: callable,
    ) -> list[int]:
        """Shuffle nets but preserve priority tier ordering.

        Args:
            net_order: Original net order
            get_priority: Function that takes net_id and returns (priority, pad_count)

        Returns:
            New net order with shuffled tiers
        """
        # Group by priority tier
        tiers: dict[int, list[int]] = {}
        for net in net_order:
            priority, _ = get_priority(net)
            if priority not in tiers:
                tiers[priority] = []
            tiers[priority].append(net)

        # Shuffle within each tier and reassemble
        result: list[int] = []
        for priority in sorted(tiers.keys()):
            tier_nets = tiers[priority].copy()
            random.shuffle(tier_nets)
            result.extend(tier_nets)

        return result

    def evaluate_solution(self, routes: list[Route]) -> float:
        """Score a routing solution (higher = better).

        Scoring prioritizes:
        1. Completion rate (primary - weighted heavily)
        2. Lower via count (secondary)
        3. Shorter total length (tertiary)

        Args:
            routes: List of routes in the solution

        Returns:
            Solution score (higher is better)
        """
        if not routes:
            return 0.0

        routed_nets = len({r.net for r in routes})
        completion_rate = routed_nets / self.total_nets if self.total_nets > 0 else 0

        total_vias = sum(len(r.vias) for r in routes)
        total_length = sum(
            math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) for r in routes for s in r.segments
        )

        # Completion rate is most important (1000x weight)
        # Penalize vias and length slightly
        return completion_rate * 1000 - total_vias * 0.1 - total_length * 0.01
