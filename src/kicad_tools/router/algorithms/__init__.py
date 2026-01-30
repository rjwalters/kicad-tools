"""Routing algorithms for the autorouter.

This package contains various routing algorithms:
- MST (Minimum Spanning Tree) based routing
- Negotiated congestion routing (PathFinder-style)
- Monte Carlo multi-start routing
- Two-phase global+detailed routing
- Hierarchical global-to-detailed routing
"""

from .hierarchical import HierarchicalRouter
from .monte_carlo import MonteCarloRouter
from .mst import MSTRouter
from .negotiated import (
    NegotiatedRouter,
    calculate_history_increment,
    calculate_present_cost,
    detect_oscillation,
    should_terminate_early,
)
from .two_phase import TwoPhaseRouter

__all__ = [
    "HierarchicalRouter",
    "MSTRouter",
    "NegotiatedRouter",
    "MonteCarloRouter",
    "TwoPhaseRouter",
    # Adaptive parameter functions (Issue #633)
    "calculate_history_increment",
    "calculate_present_cost",
    "detect_oscillation",
    "should_terminate_early",
]
