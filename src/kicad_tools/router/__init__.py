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
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.2,
    )

    router = Autorouter(width=100, height=80, rules=rules)
    router.add_component("U1", pads=[...])
    result = router.route_all()
    print(f"Routed {result.routed_nets}/{result.total_nets} nets")
"""

from .analysis import (
    BlockingObstacle,
    CongestionZone,
    NetRoutabilityReport,
    ObstacleType,
    RoutabilityAnalyzer,
    RoutabilityReport,
    RouteAlternative,
    RoutingFailureDiagnostic,
    RoutingSeverity,
    analyze_routing_failure,
)
from .bus import (
    BusGroup,
    BusRoutingConfig,
    BusRoutingMode,
    BusSignal,
    analyze_buses,
    detect_bus_signals,
    group_buses,
)
from .core import AdaptiveAutorouter, Autorouter, RoutingResult
from .diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    DifferentialPairRules,
    DifferentialPairType,
    DifferentialSignal,
    LengthMismatchWarning,
    analyze_differential_pairs,
    detect_differential_pairs,
    detect_differential_signals,
    group_differential_pairs,
)
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
from .io import (
    ClearanceViolation,
    PCBDesignRules,
    detect_layer_stack,
    generate_netclass_setup,
    load_pcb_for_routing,
    merge_routes_into_pcb,
    parse_pcb_design_rules,
    route_pcb,
    validate_grid_resolution,
    validate_routes,
)
from .layers import Layer, LayerDefinition, LayerStack, LayerType, ViaDefinition, ViaRules, ViaType
from .optimizer import (
    CollisionChecker,
    GridCollisionChecker,
    OptimizationConfig,
    OptimizationStats,
    TraceOptimizer,
)  # noqa: F401 - optimizer is now a package
from .pathfinder import AStarNode, Router
from .primitives import GridCell, Obstacle, Pad, Point, Route, Segment, Via
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
    ZoneRules,
    create_net_class_map,
)
from .zones import (
    ConnectionType,
    FilledZone,
    ThermalRelief,
    ZoneFiller,
    ZoneManager,
    fill_zones_by_priority,
    get_connection_type,
)

__all__ = [
    # High-level API
    "Autorouter",
    "AdaptiveAutorouter",
    "RoutingResult",
    # Bus routing
    "BusGroup",
    "BusRoutingConfig",
    "BusRoutingMode",
    "BusSignal",
    "analyze_buses",
    "detect_bus_signals",
    "group_buses",
    # Differential pair routing
    "DifferentialPair",
    "DifferentialPairConfig",
    "DifferentialPairRules",
    "DifferentialPairType",
    "DifferentialSignal",
    "LengthMismatchWarning",
    "analyze_differential_pairs",
    "detect_differential_pairs",
    "detect_differential_signals",
    "group_differential_pairs",
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
    "ZoneRules",
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
    "detect_layer_stack",
    "generate_netclass_setup",
    "merge_routes_into_pcb",
    # Optimizer
    "TraceOptimizer",
    "OptimizationConfig",
    "OptimizationStats",
    "CollisionChecker",
    "GridCollisionChecker",
    # Zones
    "ZoneManager",
    "ZoneFiller",
    "FilledZone",
    "ThermalRelief",
    "ConnectionType",
    "fill_zones_by_priority",
    "get_connection_type",
    # Analysis
    "RoutabilityAnalyzer",
    "RoutabilityReport",
    "NetRoutabilityReport",
    "RoutingFailureDiagnostic",
    "BlockingObstacle",
    "CongestionZone",
    "RouteAlternative",
    "ObstacleType",
    "RoutingSeverity",
    "analyze_routing_failure",
]
