"""Layout preservation for KiCad PCBs.

This module provides tools for preserving and applying component layouts
within subcircuits, enabling:

- Hierarchical component addressing (ComponentAddress, AddressRegistry)
- Extraction of relative positions within a group of components
- Application of layouts to new positions while preserving relationships
- Support for rotating subcircuits (90, 180, 270 degrees)
- Multiple instances of the same subcircuit layout

Usage:
    from kicad_tools.layout import SubcircuitExtractor, apply_subcircuit
    from kicad_tools.schema import PCB

    pcb = PCB.load("board.kicad_pcb")

    # Extract layout from existing subcircuit
    extractor = SubcircuitExtractor()
    layout = extractor.extract(
        pcb,
        component_refs=["U3", "C1", "C2", "R1"],
        subcircuit_path="power.ldo"
    )

    # Apply to new position (rotated 90 degrees)
    apply_subcircuit(
        pcb,
        layout,
        new_anchor_position=(80.0, 40.0, 90.0)
    )

    pcb.save("board_modified.kicad_pcb")
"""

from .addressing import AddressRegistry
from .subcircuit import (
    ComponentInfo,
    SubcircuitExtractor,
    apply_subcircuit,
    rotate_point,
)
from .types import ComponentAddress, ComponentOffset, SubcircuitLayout

__all__ = [
    "AddressRegistry",
    "ComponentAddress",
    "ComponentInfo",
    "ComponentOffset",
    "SubcircuitExtractor",
    "SubcircuitLayout",
    "apply_subcircuit",
    "rotate_point",
]
