"""
PCB Autorouter module.

Provides A* pathfinding-based autorouting with:
- Net class awareness (power, clock, audio, digital)
- Multi-layer support with via management
- Congestion-aware routing with negotiated costs
- Pluggable heuristics (Manhattan, DirectionBias, CongestionAware, etc.)

Example::

    from kicad_tools.router import Autorouter, DesignRules

    rules = DesignRules(
        grid_resolution=0.25,
        trace_width=0.2,
        clearance=0.15,
    )

    router = Autorouter(width=100, height=80, rules=rules)
    router.add_component("U1", pads=[...])
    result = router.route_all()
    print(f"Routed {result.routed_nets}/{result.total_nets} nets")
"""

from .core import AdaptiveAutorouter, Autorouter, RoutingResult
from .grid import RoutingGrid
from .heuristics import (
    CongestionAwareHeuristic,
    DirectionBiasHeuristic,
    GreedyHeuristic,
    Heuristic,
    HeuristicContext,
    ManhattanHeuristic,
    WeightedCongestionHeuristic,
)
from .io import load_pcb_for_routing, route_pcb
from .layers import Layer, LayerDefinition, LayerStack, LayerType, ViaDefinition, ViaRules, ViaType
from .pathfinder import AStarNode, Router
from .primitives import GridCell, Obstacle, Pad, Point, Route, Segment, Via
from .optimizer import OptimizationConfig, OptimizationStats, TraceOptimizer
from .rules import (
    DEFAULT_NET_CLASS_MAP,
    NET_CLASS_AUDIO,
    NET_CLASS_CLOCK,
    NET_CLASS_DEBUG,
    NET_CLASS_DEFAULT,
    NET_CLASS_DIGITAL,
    NET_CLASS_HIGH_SPEED,
    NET_CLASS_POWER,
    DesignRules,
    NetClassRouting,
    create_net_class_map,
)

__all__ = [
    # High-level API
    "Autorouter",
    "AdaptiveAutorouter",
    "RoutingResult",
    # Grid
    "RoutingGrid",
    # Pathfinding
    "Router",
    "AStarNode",
    # Heuristics
    "Heuristic",
    "HeuristicContext",
    "ManhattanHeuristic",
    "DirectionBiasHeuristic",
    "CongestionAwareHeuristic",
    "WeightedCongestionHeuristic",
    "GreedyHeuristic",
    # Layers
    "Layer",
    "LayerType",
    "LayerStack",
    "LayerDefinition",
    "ViaType",
    "ViaDefinition",
    "ViaRules",
    # Primitives
    "Point",
    "GridCell",
    "Via",
    "Segment",
    "Route",
    "Pad",
    "Obstacle",
    # Rules
    "DesignRules",
    "NetClassRouting",
    "create_net_class_map",
    "DEFAULT_NET_CLASS_MAP",
    "NET_CLASS_POWER",
    "NET_CLASS_CLOCK",
    "NET_CLASS_HIGH_SPEED",
    "NET_CLASS_AUDIO",
    "NET_CLASS_DIGITAL",
    "NET_CLASS_DEBUG",
    "NET_CLASS_DEFAULT",
    # I/O
    "route_pcb",
    "load_pcb_for_routing",
    # Optimizer
    "TraceOptimizer",
    "OptimizationConfig",
    "OptimizationStats",
]
