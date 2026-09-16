"""Board06 compatibility imports for the shared physical escape policy."""

from kicad_tools.zones.pour_escape import (
    Escape,
    EscapeRules,
    _kct_managed_floor_minima,
    find_escape,
)

__all__ = ["Escape", "EscapeRules", "find_escape", "_kct_managed_floor_minima"]
