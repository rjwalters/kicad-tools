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

__all__ = [
    "CollisionResult",
    "Conflict",
    "ConflictSeverity",
    "ConflictType",
    "DesignRules",
    "DRCResult",
    "DRCViolation",
    "PlacementAnalyzer",
    "PlacementCollision",
    "PlacementFix",
    "PlacementFixer",
    "PlacementValidationResult",
]
