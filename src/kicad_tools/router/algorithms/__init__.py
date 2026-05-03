"""Routing algorithms for the autorouter.

This package contains various routing algorithms:
- MST (Minimum Spanning Tree) based routing
- Negotiated congestion routing (PathFinder-style)
- Monte Carlo multi-start routing
- Evolutionary (GA-style) routing optimization
- Two-phase global+detailed routing
- Hierarchical global-to-detailed routing

NOTE: These algorithms use the pure Python pathfinder by default. A C++
backend is available that provides 10-100x speedup for the core A* loop.
Build it with 'kct build-native' for production use.
"""

from .evolutionary import EvolutionaryRoutingOptimizer, RoutingChromosome
from .hierarchical import HierarchicalRouter
from .monte_carlo import MonteCarloRouter
from .mst import MSTRouter
from .negotiated import (
    NegotiatedRouter,
    calculate_congestion_tuned_params,
    calculate_history_increment,
    calculate_present_cost,
    detect_oscillation,
    should_terminate_early,
)
from .steiner import build_rsmt
from .two_phase import TwoPhaseRouter

__all__ = [
    "EvolutionaryRoutingOptimizer",
    "HierarchicalRouter",
    "MSTRouter",
    "NegotiatedRouter",
    "MonteCarloRouter",
    "RoutingChromosome",
    "TwoPhaseRouter",
    "build_rsmt",
    # Adaptive parameter functions (Issue #633, #2333)
    "calculate_congestion_tuned_params",
    "calculate_history_increment",
    "calculate_present_cost",
    "detect_oscillation",
    "should_terminate_early",
]
