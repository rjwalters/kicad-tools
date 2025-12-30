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

from .analyzer import PlacementAnalyzer
from .conflict import (
    Conflict,
    ConflictSeverity,
    ConflictType,
    PlacementFix,
)
from .fixer import PlacementFixer

__all__ = [
    "PlacementAnalyzer",
    "PlacementFixer",
    "Conflict",
    "ConflictType",
    "ConflictSeverity",
    "PlacementFix",
]
