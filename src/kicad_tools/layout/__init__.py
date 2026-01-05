"""
Layout preservation and component addressing for KiCad designs.

This module provides hierarchical address-based component matching
to preserve layout when regenerating PCB from schematic changes.

Example usage:
    >>> from kicad_tools.layout import LayoutPreserver, capture_layout
    >>>
    >>> # Capture current layout before modifying schematic
    >>> preserver = LayoutPreserver("board.kicad_pcb", "board.kicad_sch")
    >>>
    >>> # After schematic modification and PCB regeneration
    >>> result = preserver.apply_to_new_pcb("board_new.kicad_pcb", "board_new.kicad_sch")
    >>> print(f"Preserved {len(result.matched_components)} component positions")
"""

from .addressing import AddressRegistry
from .preservation import LayoutPreserver, PreservationResult, preserve_layout
from .snapshot import SnapshotCapture, capture_layout
from .types import (
    ComponentAddress,
    ComponentLayout,
    LayoutSnapshot,
    TraceSegment,
    ViaLayout,
    ZoneLayout,
)

__all__ = [
    # Addressing
    "AddressRegistry",
    "ComponentAddress",
    # Snapshot
    "SnapshotCapture",
    "capture_layout",
    "LayoutSnapshot",
    # Preservation
    "LayoutPreserver",
    "PreservationResult",
    "preserve_layout",
    # Types
    "ComponentLayout",
    "TraceSegment",
    "ViaLayout",
    "ZoneLayout",
]
