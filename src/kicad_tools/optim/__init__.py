"""
Placement and routing optimization module.

Provides algorithms for component placement optimization:

**Physics-based (force-directed):**
- Charge-based repulsion from board/component outlines
- Spring-based attraction between net-connected pins
- Converges to local minima quickly

**Evolutionary (genetic algorithm):**
- Population-based global search
- Crossover and mutation operators
- Escapes local minima through exploration
- Hybrid mode combines evolutionary + physics

Example (physics-based)::

    from kicad_tools.optim import PlacementOptimizer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    optimizer = PlacementOptimizer.from_pcb(pcb)

    # Run simulation
    optimizer.run(iterations=1000, dt=0.01)

    # Get optimized placements
    for comp in optimizer.components:
        print(f"{comp.ref}: ({comp.x:.2f}, {comp.y:.2f}) @ {comp.rotation:.1f} deg")

Example (evolutionary)::

    from kicad_tools.optim import EvolutionaryPlacementOptimizer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    optimizer = EvolutionaryPlacementOptimizer.from_pcb(pcb)

    # Run evolutionary optimization
    best = optimizer.optimize(generations=100, population_size=50)

    # Or use hybrid: evolutionary global search + physics refinement
    physics_opt = optimizer.optimize_hybrid(generations=50)
    physics_opt.write_to_pcb(pcb)
    pcb.save("optimized.kicad_pcb")
"""

from kicad_tools.optim.alignment import (
    AlignmentConstraint,
    AlignmentType,
    align_components,
    align_to_reference,
    apply_alignment_constraints,
    distribute_components,
    snap_to_grid,
)
from kicad_tools.optim.clustering import ClusterDetector, detect_functional_clusters
from kicad_tools.optim.components import (
    ClusterType,
    Component,
    FunctionalCluster,
    Keepout,
    Pin,
    Spring,
)
from kicad_tools.optim.config import PlacementConfig
from kicad_tools.optim.constraint_loader import (
    load_constraints_from_yaml,
    save_constraints_to_yaml,
)
from kicad_tools.optim.constraints import (
    ConstraintType,
    ConstraintViolation,
    GroupingConstraint,
    SpatialConstraint,
    expand_member_patterns,
    validate_grouping_constraints,
)
from kicad_tools.optim.edge_placement import (
    BoardEdges,
    Edge,
    EdgeConstraint,
    EdgeSide,
    detect_edge_components,
    get_board_edges,
)
from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
    Individual,
)
from kicad_tools.optim.geometry import Polygon, Vector2D
from kicad_tools.optim.keepout import (
    KeepoutType,
    KeepoutViolation,
    KeepoutZone,
    add_keepout_zones,
    create_keepout_from_board_edge,
    create_keepout_from_component,
    create_keepout_from_mounting_hole,
    create_keepout_polygon,
    detect_keepout_zones,
    load_keepout_zones_from_yaml,
    validate_keepout_violations,
)
from kicad_tools.optim.placement import PlacementOptimizer
from kicad_tools.optim.query import (
    Rectangle,
    find_best_position,
    process_json_request,
    query_alignment,
    query_position,
    query_swap,
)
from kicad_tools.optim.routing import FigureOfMerit, RoutingOptimizer
from kicad_tools.optim.session import (
    Move,
    MoveResult,
    PlacementSession,
    RoutingImpact,
    SessionState,
    Violation,
)
from kicad_tools.optim.session import PlacementSuggestion as SessionPlacementSuggestion
from kicad_tools.optim.signal_integrity import (
    NetClassification,
    SignalClass,
    SignalIntegrityHint,
    add_si_constraints,
    analyze_placement_for_si,
    classify_nets,
    get_si_score,
)
from kicad_tools.optim.suggestions import (
    AlternativePosition,
    ForceContribution,
    PlacementSuggestion,
    RationaleType,
    explain_placement,
    generate_placement_suggestions,
    suggest_improvement,
)
from kicad_tools.optim.thermal import (
    ThermalClass,
    ThermalConfig,
    ThermalConstraint,
    ThermalProperties,
    classify_thermal_properties,
    detect_thermal_constraints,
    get_thermal_summary,
)

__all__ = [
    # Alignment
    "AlignmentType",
    "AlignmentConstraint",
    "snap_to_grid",
    "align_components",
    "distribute_components",
    "align_to_reference",
    "apply_alignment_constraints",
    # Placement optimization
    "PlacementOptimizer",
    "EvolutionaryPlacementOptimizer",
    "RoutingOptimizer",
    "FigureOfMerit",
    # Session and query API
    "PlacementSession",
    "MoveResult",
    "RoutingImpact",
    "SessionPlacementSuggestion",
    "SessionState",
    "Move",
    "Violation",
    "Rectangle",
    # Query functions
    "query_position",
    "query_swap",
    "query_alignment",
    "find_best_position",
    "process_json_request",
    # Geometry
    "Vector2D",
    "Polygon",
    # Components
    "Component",
    "Spring",
    "Keepout",
    "Pin",
    # Config
    "PlacementConfig",
    "EvolutionaryConfig",
    "Individual",
    # Functional clustering
    "FunctionalCluster",
    "ClusterType",
    "ClusterDetector",
    "detect_functional_clusters",
    # Constraints
    "GroupingConstraint",
    "SpatialConstraint",
    "ConstraintType",
    "ConstraintViolation",
    "validate_grouping_constraints",
    "expand_member_patterns",
    "load_constraints_from_yaml",
    "save_constraints_to_yaml",
    # Edge placement
    "EdgeConstraint",
    "EdgeSide",
    "Edge",
    "BoardEdges",
    "detect_edge_components",
    "get_board_edges",
    # Signal integrity
    "SignalClass",
    "NetClassification",
    "SignalIntegrityHint",
    "classify_nets",
    "analyze_placement_for_si",
    "get_si_score",
    "add_si_constraints",
    # Thermal awareness
    "ThermalClass",
    "ThermalConfig",
    "ThermalConstraint",
    "ThermalProperties",
    "classify_thermal_properties",
    "detect_thermal_constraints",
    "get_thermal_summary",
    # Keepout zone management
    "KeepoutType",
    "KeepoutZone",
    "KeepoutViolation",
    "create_keepout_from_component",
    "create_keepout_from_mounting_hole",
    "create_keepout_from_board_edge",
    "create_keepout_polygon",
    "detect_keepout_zones",
    "add_keepout_zones",
    "validate_keepout_violations",
    "load_keepout_zones_from_yaml",
    # Placement suggestions
    "PlacementSuggestion",
    "AlternativePosition",
    "ForceContribution",
    "RationaleType",
    "generate_placement_suggestions",
    "explain_placement",
    "suggest_improvement",
]
