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

from .analyzer import DesignRules, PlacementAnalyzer
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
    "CMAESStrategy",
    "CollisionResult",
    "ComponentDef",
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
    "StrategyConfig",
    "TransformedPad",
    "IterationRecord",
    "LayoutStyle",
    "OptimizationRecorder",
    "ParetoPoint",
    "bounds",
    "compute_hpwl",
    "compute_hpwl_breakdown",
    "decode",
    "encode",
    "plot_convergence",
    "plot_layout",
    "plot_pareto_front",
]
