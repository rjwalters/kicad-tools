"""Layout preservation for KiCad PCBs.

This module provides tools for preserving and applying component layouts
within subcircuits, and net remapping for schematic changes:

- Hierarchical component addressing (ComponentAddress, AddressRegistry)
- Extraction of relative positions within a group of components
- Application of layouts to new positions while preserving relationships
- Support for rotating subcircuits (90, 180, 270 degrees)
- Net name change detection and trace remapping
- Incremental layout updates (ChangeDetector, IncrementalUpdater)
"""

from .addressing import AddressRegistry
from .incremental import (
    ChangeDetector,
    IncrementalUpdater,
    SnapshotBuilder,
    apply_incremental_update,
    detect_layout_changes,
)
from .net_mapping import NetMapper, remap_traces
from .subcircuit import (
    ComponentInfo,
    SubcircuitExtractor,
    apply_subcircuit,
    rotate_point,
)
from .types import (
    ChangeType,
    ComponentAddress,
    ComponentOffset,
    ComponentState,
    LayoutChange,
    LayoutSnapshot,
    MatchReason,
    NetMapping,
    OrphanedSegment,
    RemapResult,
    SegmentRemap,
    SubcircuitLayout,
    UpdateResult,
)

__all__ = [
    "AddressRegistry",
    "ChangeDetector",
    "ChangeType",
    "ComponentAddress",
    "ComponentInfo",
    "ComponentOffset",
    "ComponentState",
    "IncrementalUpdater",
    "LayoutChange",
    "LayoutSnapshot",
    "MatchReason",
    "NetMapper",
    "NetMapping",
    "OrphanedSegment",
    "RemapResult",
    "SegmentRemap",
    "SnapshotBuilder",
    "SubcircuitExtractor",
    "SubcircuitLayout",
    "UpdateResult",
    "apply_incremental_update",
    "apply_subcircuit",
    "detect_layout_changes",
    "remap_traces",
    "rotate_point",
]
