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

from .cache import (
    CachedNetRoute,
    CachedRoutingResult,
    CacheKey,
    RoutingCache,
    compute_pad_positions_hash,
    get_default_cache_path,
)
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
from .core import AdaptiveAutorouter, Autorouter, RoutingFailure, RoutingResult
from .cpp_backend import (
    CppGrid,
    CppPathfinder,
    create_hybrid_router,
    get_backend_info,
    is_cpp_available,
)
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
from .escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
    PackageInfo,
    PackageType,
    detect_package_type,
    get_package_info,
    is_dense_package,
    is_fine_pitch_ssop,
)
from .failure_analysis import (
    BlockingElement,
    CongestionMap,
    FailureAnalysis,
    FailureCause,
    PathAttempt,
    RootCauseAnalyzer,
)
from .fine_pitch import (
    ComponentGridAnalysis,
    FinePitchReport,
    FinePitchSeverity,
    OffGridPad,
    analyze_fine_pitch_components,
)
from .grid import RoutingGrid
from .adaptive_grid import (
    AdaptiveGridResult,
    AdaptiveGridRouter,
    identify_fine_pitch_components,
)
from .subgrid import (
    SubGridAnalysis,
    SubGridEscape,
    SubGridPad,
    SubGridResult,
    SubGridRouter,
    compute_subgrid_resolution,
)
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
    GridAdjustment,
    GridAutoSelection,
    GridResolutionError,
    PadPosition,
    PCBDesignRules,
    adjust_grid_for_compliance,
    auto_select_grid_resolution,
    detect_layer_stack,
    extract_pad_positions,
    generate_netclass_setup,
    load_pcb_for_routing,
    merge_routes_into_pcb,
    parse_pcb_design_rules,
    recommend_grid_for_board_size,
    route_pcb,
    validate_grid_resolution,
    validate_routes,
)
from .layers import Layer, LayerDefinition, LayerStack, LayerType, ViaDefinition, ViaRules, ViaType
from .length import (
    LengthTracker,
    LengthViolation,
    ViolationType,
    create_match_group,
)
from .mfr_limits import (
    MFR_JLCPCB,
    MFR_LIMITS,
    MFR_OSHPARK,
    MFR_PCBWAY,
    MfrLimits,
    RelaxationTier,
    get_mfr_limits,
    get_relaxation_tiers,
)
from .net_class import (
    NET_CLASS_PATTERNS,
    SYMBOL_INDICATORS,
    NetClass,
    NetClassification,
    apply_net_class_rules,
    auto_classify_nets,
    classify_and_apply_rules,
    classify_from_name,
    classify_from_pin_type,
    classify_from_symbol,
    classify_net,
    find_differential_partner,
    is_differential_pair_name,
)
from .optimizer import (
    CollisionChecker,
    GridCollisionChecker,
    OptimizationConfig,
    OptimizationStats,
    TraceOptimizer,
)  # noqa: F401 - optimizer is now a package
from .output import (
    format_failed_nets_summary,
    get_routing_diagnostics_json,
    print_routing_diagnostics_json,
    show_fine_pitch_warnings,
    show_routing_summary,
)
from .parallel import (
    BoundingBox,
    NetGroup,
    ParallelRouter,
    ParallelRoutingResult,
    find_independent_groups,
    find_route_conflicts,
    resolve_parallel_conflicts,
)
from .pathfinder import AStarNode, Router
from .placement_feedback import (
    PlacementAdjustment,
    PlacementFeedbackLoop,
    PlacementFeedbackResult,
)
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
    LengthConstraint,
    NetClassRouting,
    ZoneRules,
    create_net_class_map,
)
from .global_router import CorridorAssignment, GlobalRouter, GlobalRoutingResult
from .orchestrator import RoutingOrchestrator
from .region_graph import Region, RegionEdge, RegionGraph
from .sparse import SparseRouter, SparseRoutingGraph, Waypoint
from .strategies import (
    AlternativeStrategy,
    DRCViolation,
    PerformanceStats,
    RepairAction,
    RoutingMetrics,
    RoutingResult,
    RoutingStrategy,
)
from .tuning import (
    COST_PROFILES,
    BoardCharacteristics,
    CostParams,
    CostProfile,
    RoutingQualityScore,
    TuningResult,
    analyze_board,
    create_adaptive_router,
    evaluate_routing_quality,
    quick_tune,
    select_profile,
    tune_parameters,
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
    "RoutingFailure",
    "RoutingResult",
    # C++ backend
    "is_cpp_available",
    "get_backend_info",
    "create_hybrid_router",
    "CppGrid",
    "CppPathfinder",
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
    # Sparse routing (performance optimizations)
    "SparseRouter",
    "SparseRoutingGraph",
    "Waypoint",
    # Hierarchical routing (Issue #1095)
    "RegionGraph",
    "Region",
    "RegionEdge",
    "GlobalRouter",
    "GlobalRoutingResult",
    "CorridorAssignment",
    # Routing orchestration (Issue #1138)
    "RoutingOrchestrator",
    "RoutingStrategy",
    "RoutingResult",
    "RoutingMetrics",
    "PerformanceStats",
    "AlternativeStrategy",
    "RepairAction",
    "DRCViolation",
    # Parallel routing
    "ParallelRouter",
    "ParallelRoutingResult",
    "BoundingBox",
    "NetGroup",
    "find_independent_groups",
    "find_route_conflicts",
    "resolve_parallel_conflicts",
    # Cost tuning
    "CostParams",
    "CostProfile",
    "COST_PROFILES",
    "BoardCharacteristics",
    "RoutingQualityScore",
    "TuningResult",
    "analyze_board",
    "quick_tune",
    "tune_parameters",
    "select_profile",
    "create_adaptive_router",
    "evaluate_routing_quality",
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
    "LengthConstraint",
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
    # Length tracking
    "LengthTracker",
    "LengthViolation",
    "ViolationType",
    "create_match_group",
    # I/O
    "route_pcb",
    "load_pcb_for_routing",
    "detect_layer_stack",
    "generate_netclass_setup",
    "merge_routes_into_pcb",
    "ClearanceViolation",
    "GridAdjustment",
    "GridAutoSelection",
    "GridResolutionError",
    "PCBDesignRules",
    "PadPosition",
    "adjust_grid_for_compliance",
    "auto_select_grid_resolution",
    "extract_pad_positions",
    "recommend_grid_for_board_size",
    "parse_pcb_design_rules",
    "validate_grid_resolution",
    "validate_routes",
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
    # Failure Analysis (Root Cause)
    "FailureCause",
    "FailureAnalysis",
    "BlockingElement",
    "PathAttempt",
    "CongestionMap",
    "RootCauseAnalyzer",
    # Placement-Routing Feedback
    "PlacementFeedbackLoop",
    "PlacementFeedbackResult",
    "PlacementAdjustment",
    # Escape Routing (dense packages)
    "EscapeRouter",
    "EscapeRoute",
    "EscapeDirection",
    "PackageType",
    "PackageInfo",
    "is_dense_package",
    "is_fine_pitch_ssop",
    "detect_package_type",
    "get_package_info",
    # Net Class Auto-Detection (Issue #634)
    "NetClass",
    "NetClassification",
    "SYMBOL_INDICATORS",
    "NET_CLASS_PATTERNS",
    "classify_from_symbol",
    "classify_from_pin_type",
    "classify_from_name",
    "classify_net",
    "auto_classify_nets",
    "apply_net_class_rules",
    "classify_and_apply_rules",
    "is_differential_pair_name",
    "find_differential_partner",
    # Output and Diagnostics
    "format_failed_nets_summary",
    "show_fine_pitch_warnings",
    "show_routing_summary",
    "get_routing_diagnostics_json",
    "print_routing_diagnostics_json",
    # Manufacturer Limits and Adaptive Rules
    "MfrLimits",
    "RelaxationTier",
    "MFR_LIMITS",
    "MFR_JLCPCB",
    "MFR_OSHPARK",
    "MFR_PCBWAY",
    "get_mfr_limits",
    "get_relaxation_tiers",
    # Fine-Pitch Component Analysis (Issue #1008)
    "FinePitchReport",
    "FinePitchSeverity",
    "ComponentGridAnalysis",
    "OffGridPad",
    "analyze_fine_pitch_components",
    # Sub-Grid Routing for Fine-Pitch Components (Issue #1109)
    "SubGridRouter",
    "SubGridAnalysis",
    "SubGridEscape",
    "SubGridPad",
    "SubGridResult",
    "compute_subgrid_resolution",
    # Adaptive Grid Routing (Issue #1135)
    "AdaptiveGridRouter",
    "AdaptiveGridResult",
    "identify_fine_pitch_components",
    # Routing Cache (Issue #1071)
    "RoutingCache",
    "CacheKey",
    "CachedRoutingResult",
    "CachedNetRoute",
    "compute_pad_positions_hash",
    "get_default_cache_path",
]
