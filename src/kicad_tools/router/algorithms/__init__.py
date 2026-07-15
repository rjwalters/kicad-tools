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
    GRACE_PASS_BUDGET_S,
    GRACE_PASS_PER_NET_S,
    GRACE_PASS_TIER_CAPS_S,
    PER_NET_CAP_FLOOR_S,
    PER_NET_CAP_STAGE_FRACTION,
    POST_NEGOTIATION_SWEEP_BUDGET_S,
    POST_NEGOTIATION_SWEEP_PER_NET_S,
    NegotiatedRouter,
    calculate_congestion_tuned_params,
    calculate_history_increment,
    calculate_present_cost,
    derive_iter_per_net_cap,
    derive_per_net_cap,
    detect_oscillation,
    detect_ripup_stagnation,
    run_initial_pass_grace,
    select_seg_seg_demotion_nets,
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
    # Initial-pass grace pass (Issue #3452)
    "GRACE_PASS_BUDGET_S",
    "GRACE_PASS_PER_NET_S",
    "GRACE_PASS_TIER_CAPS_S",
    "run_initial_pass_grace",
    # Post-negotiation rescue sweep (Issue #4159)
    "POST_NEGOTIATION_SWEEP_BUDGET_S",
    "POST_NEGOTIATION_SWEEP_PER_NET_S",
    # Per-net A* cap derivation (Issue #3474 R1)
    "PER_NET_CAP_FLOOR_S",
    "PER_NET_CAP_STAGE_FRACTION",
    "derive_per_net_cap",
    # Per-iteration per-net cap from remaining budget (Issue #3989)
    "derive_iter_per_net_cap",
    # Adaptive parameter functions (Issue #633, #2333)
    "calculate_congestion_tuned_params",
    "calculate_history_increment",
    "calculate_present_cost",
    "detect_oscillation",
    "detect_ripup_stagnation",
    "select_seg_seg_demotion_nets",
    "should_terminate_early",
]
