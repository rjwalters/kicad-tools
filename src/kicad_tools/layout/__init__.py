"""Layout preservation for KiCad PCBs.

This module provides tools for preserving and applying component layouts
within subcircuits, and net remapping for schematic changes:

- Hierarchical component addressing (ComponentAddress, AddressRegistry)
- Extraction of relative positions within a group of components
- Application of layouts to new positions while preserving relationships
- Support for rotating subcircuits (90, 180, 270 degrees)
- Net name change detection and trace remapping
"""

from .addressing import AddressRegistry
from .net_mapping import NetMapper, remap_traces
from .subcircuit import (
    ComponentInfo,
    SubcircuitExtractor,
    apply_subcircuit,
    rotate_point,
)
from .types import (
    ComponentAddress,
    ComponentOffset,
    MatchReason,
    NetMapping,
    OrphanedSegment,
    RemapResult,
    SegmentRemap,
    SubcircuitLayout,
)

__all__ = [
    "AddressRegistry",
    "ComponentAddress",
    "ComponentInfo",
    "ComponentOffset",
    "MatchReason",
    "NetMapper",
    "NetMapping",
    "OrphanedSegment",
    "RemapResult",
    "SegmentRemap",
    "SubcircuitExtractor",
    "SubcircuitLayout",
    "apply_subcircuit",
    "remap_traces",
    "rotate_point",
]
