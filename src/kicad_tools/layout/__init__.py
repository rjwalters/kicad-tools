"""Layout preservation for KiCad PCBs.

This module provides tools for preserving and applying component layouts
when regenerating PCB from schematic changes:

- Hierarchical component addressing (ComponentAddress, AddressRegistry)
- Layout snapshot capture and restoration (LayoutPreserver, SnapshotCapture)
- Subcircuit extraction and positioning (SubcircuitExtractor, SubcircuitLayout)
- Net name change detection and trace remapping (NetMapper)
- Incremental layout updates (ChangeDetector, IncrementalUpdater)

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
from .incremental import (
    ChangeDetector,
    IncrementalUpdater,
    SnapshotBuilder,
    apply_incremental_update,
    detect_layout_changes,
)
from .net_mapping import NetMapper, remap_traces
from .preservation import LayoutPreserver, PreservationResult, preserve_layout
from .snapshot import SnapshotCapture, capture_layout
from .subcircuit import (
    ComponentInfo,
    SubcircuitExtractor,
    apply_subcircuit,
    rotate_point,
)
from .types import (
    ChangeType,
    ComponentAddress,
    ComponentLayout,
    ComponentOffset,
    ComponentState,
    IncrementalSnapshot,
    LayoutChange,
    LayoutSnapshot,
    MatchReason,
    NetMapping,
    OrphanedSegment,
    RemapResult,
    SegmentRemap,
    SubcircuitLayout,
    TraceSegment,
    UpdateResult,
    ViaLayout,
    ZoneLayout,
)

__all__ = [
    # Addressing
    "AddressRegistry",
    "ChangeDetector",
    "ChangeType",
    "ComponentAddress",
    # Snapshot & Preservation
    "SnapshotCapture",
    "capture_layout",
    "IncrementalSnapshot",
    "LayoutSnapshot",
    "LayoutPreserver",
    "PreservationResult",
    "preserve_layout",
    # Layout Types
    "ComponentLayout",
    "TraceSegment",
    "ViaLayout",
    "ZoneLayout",
    # Subcircuit
    "ComponentInfo",
    "ComponentOffset",
    "ComponentState",
    "IncrementalUpdater",
    "LayoutChange",
    "SubcircuitExtractor",
    "SubcircuitLayout",
    "apply_subcircuit",
    "rotate_point",
    # Net Mapping
    "MatchReason",
    "NetMapper",
    "NetMapping",
    "OrphanedSegment",
    "RemapResult",
    "SegmentRemap",
    "SnapshotBuilder",
    "UpdateResult",
    "apply_incremental_update",
    "detect_layout_changes",
    "remap_traces",
]
