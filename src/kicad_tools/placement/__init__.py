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
from .strategy import PlacementStrategy, StrategyConfig
from .vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    PlacementBounds,
    PlacementVector,
    TransformedPad,
    bounds,
    decode,
    encode,
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
    "BayesianOptStrategy",
    "CMAESStrategy",
    "CollisionResult",
    "ComponentDef",
    "ComponentGroup",
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
    "SignalFlowResult",
    "StrategyConfig",
    "TransformedPad",
    "IterationRecord",
    "LayoutStyle",
    "OptimizationRecorder",
    "ParetoPoint",
    "bounds",
    "build_affinity_graph",
    "compute_hpwl",
    "compute_hpwl_breakdown",
    "decode",
    "detect_power_domains",
    "detect_signal_flow",
    "encode",
    "find_clusters",
    "plot_convergence",
    "plot_layout",
    "plot_pareto_front",
    "power_domain_clustering",
    "prior_mean_position",
    "schematic_proximity_prior",
]
