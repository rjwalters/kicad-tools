"""
Placement conflict detection and resolution for KiCad PCBs.

This module provides tools to detect and resolve component placement conflicts:
- Courtyard overlaps
- Hole-to-hole violations
- Pad-to-pad clearance violations
- Silkscreen-to-pad conflicts
- Edge clearance violations

Usage:
    from kicad_tools.placement import PlacementAnalyzer, PlacementFixer

    analyzer = PlacementAnalyzer()
    conflicts = analyzer.find_conflicts(pcb_path)

    fixer = PlacementFixer()
    fixes = fixer.suggest_fixes(conflicts)
"""

import contextlib

from .analyzer import DesignRules, PlacementAnalyzer

with contextlib.suppress(ImportError):
    from .bo_strategy import BayesianOptStrategy

from .cost import (
    BlockRegion,
    compute_block_boundary_violation,
    compute_inter_block_spacing_violation,
)
from .cmaes_strategy import CMAESStrategy
from .collision import (
    CollisionResult,
    DRCResult,
    DRCViolation,
    PlacementCollision,
    PlacementValidationResult,
)
from .conflict import (
    Conflict,
    ConflictSeverity,
    ConflictType,
    PlacementFix,
)
from .fixer import PlacementFixer
from .multi_fidelity import (
    DefaultFidelitySelector,
    FidelityConfig,
    FidelityLevel,
    FidelityResult,
    FidelitySelector,
    RoutabilityResult,
    evaluate_placement_multifidelity,
    make_adaptive_evaluator,
    make_fixed_fidelity_evaluator,
)
from .priors import (
    AffinityGraph,
    ComponentGroup,
    SignalFlowResult,
    build_affinity_graph,
    detect_power_domains,
    detect_signal_flow,
    find_clusters,
    power_domain_clustering,
    prior_mean_position,
    schematic_proximity_prior,
)
from .slide_off import SlideOffResult, slide_off_overlaps
from .strategy import PlacementStrategy, StrategyConfig
from .vector import (
    BlockGroupDef,
    ComponentDef,
    PadDef,
    PlacedComponent,
    PlacementBounds,
    PlacementVector,
    RelativeOffset,
    TransformedPad,
    bounds,
    bounds_with_blocks,
    decode,
    decode_with_blocks,
    encode,
    encode_with_blocks,
    move_block,
    rotate_block,
    swap_blocks,
)
from .visualization import (
    IterationRecord,
    LayoutStyle,
    OptimizationRecorder,
    ParetoPoint,
    plot_convergence,
    plot_layout,
    plot_pareto_front,
)
from .wirelength import (
    HPWLResult,
    NetWirelength,
    compute_hpwl,
    compute_hpwl_breakdown,
)

__all__ = [
    "AffinityGraph",
    "BlockGroupDef",
    "BlockRegion",
    "SlideOffResult",
    "BayesianOptStrategy",
    "CMAESStrategy",
    "CollisionResult",
    "ComponentDef",
    "ComponentGroup",
    "DefaultFidelitySelector",
    "FidelityConfig",
    "FidelityLevel",
    "FidelityResult",
    "FidelitySelector",
    "HPWLResult",
    "Conflict",
    "ConflictSeverity",
    "ConflictType",
    "DesignRules",
    "DRCResult",
    "DRCViolation",
    "NetWirelength",
    "PadDef",
    "PlacedComponent",
    "PlacementAnalyzer",
    "PlacementBounds",
    "PlacementCollision",
    "PlacementFix",
    "PlacementFixer",
    "PlacementStrategy",
    "PlacementValidationResult",
    "PlacementVector",
    "RelativeOffset",
    "RoutabilityResult",
    "SignalFlowResult",
    "StrategyConfig",
    "TransformedPad",
    "IterationRecord",
    "LayoutStyle",
    "OptimizationRecorder",
    "ParetoPoint",
    "bounds",
    "bounds_with_blocks",
    "build_affinity_graph",
    "compute_block_boundary_violation",
    "compute_hpwl",
    "compute_hpwl_breakdown",
    "compute_inter_block_spacing_violation",
    "decode",
    "decode_with_blocks",
    "detect_power_domains",
    "detect_signal_flow",
    "encode",
    "encode_with_blocks",
    "evaluate_placement_multifidelity",
    "find_clusters",
    "make_adaptive_evaluator",
    "make_fixed_fidelity_evaluator",
    "move_block",
    "plot_convergence",
    "plot_layout",
    "plot_pareto_front",
    "power_domain_clustering",
    "prior_mean_position",
    "rotate_block",
    "schematic_proximity_prior",
    "slide_off_overlaps",
    "swap_blocks",
]
