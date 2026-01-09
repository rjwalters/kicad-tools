"""Layout preservation type definitions.

Provides data structures for:
- Hierarchical component addressing (ComponentAddress)
- Component layout capture and restoration (ComponentLayout, LayoutSnapshot)
- Trace, via, and zone layout data (TraceSegment, ViaLayout, ZoneLayout)
- Subcircuit layout extraction and application (SubcircuitLayout, ComponentOffset)
- Net mapping and remapping results (NetMapping, RemapResult)
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass(frozen=True)
class ComponentAddress:
    """
    Hierarchical address for a component in a schematic.

    Uses atopile-inspired hierarchical addressing:
    - `C1` - Component in root sheet
    - `power.C1` - Component in power subsheet
    - `power.ldo.C1` - Component in ldo subsheet of power sheet

    Attributes:
        full_path: Complete hierarchical path (e.g., "power.ldo.C1")
        sheet_path: Path to the sheet containing the component (e.g., "power.ldo")
        local_ref: Local reference designator (e.g., "C1")
        uuid: KiCad internal UUID for the component
    """

    full_path: str
    sheet_path: str
    local_ref: str
    uuid: str

    def __post_init__(self):
        """Validate address format."""
        if not self.local_ref:
            raise ValueError("local_ref cannot be empty")
        if not self.uuid:
            raise ValueError("uuid cannot be empty")

    @classmethod
    def from_parts(
        cls,
        sheet_path: str,
        local_ref: str,
        uuid: str,
    ) -> ComponentAddress:
        """
        Create a ComponentAddress from its parts.

        Args:
            sheet_path: Path to the containing sheet (empty for root)
            local_ref: Local reference designator
            uuid: KiCad component UUID

        Returns:
            ComponentAddress instance
        """
        if sheet_path:
            full_path = f"{sheet_path}.{local_ref}"
        else:
            full_path = local_ref

        return cls(
            full_path=full_path,
            sheet_path=sheet_path,
            local_ref=local_ref,
            uuid=uuid,
        )

    @property
    def depth(self) -> int:
        """
        Get the depth of this component in the hierarchy.

        Root level components have depth 0.
        """
        if not self.sheet_path:
            return 0
        return self.sheet_path.count(".") + 1

    @property
    def parent_path(self) -> str:
        """
        Get the parent sheet path.

        Returns empty string if at root level or one level deep.
        """
        if not self.sheet_path:
            return ""
        parts = self.sheet_path.rsplit(".", 1)
        return parts[0] if len(parts) > 1 else ""

    def __str__(self) -> str:
        """Return the full path as string representation."""
        return self.full_path

    def __repr__(self) -> str:
        return f"ComponentAddress({self.full_path!r}, uuid={self.uuid!r})"


# =============================================================================
# Layout Snapshot Types (for full PCB preservation)
# =============================================================================


@dataclass
class ComponentLayout:
    """
    Layout data for a component on the PCB.

    Captures the placement information needed to restore a component's
    position after PCB regeneration.

    Attributes:
        address: Hierarchical address of the component (e.g., "power.ldo.C1")
        x: X position in mm
        y: Y position in mm
        rotation: Rotation angle in degrees
        layer: PCB layer (e.g., "F.Cu", "B.Cu")
        locked: Whether the component position is locked
        reference: Reference designator (e.g., "C1")
        uuid: Component UUID from PCB file
    """

    address: str
    x: float
    y: float
    rotation: float
    layer: str
    locked: bool = False
    reference: str = ""
    uuid: str = ""

    def __post_init__(self):
        """Validate layout data."""
        if not self.address:
            raise ValueError("address cannot be empty")

    @property
    def position(self) -> tuple[float, float]:
        """Get position as (x, y) tuple."""
        return (self.x, self.y)

    def distance_to(self, other: ComponentLayout) -> float:
        """Calculate Euclidean distance to another component."""
        import math

        dx = self.x - other.x
        dy = self.y - other.y
        return math.sqrt(dx * dx + dy * dy)


@dataclass
class TraceSegment:
    """
    A single segment of a PCB trace.

    Represents a straight line segment of copper routing.

    Attributes:
        net_name: Name of the net this trace belongs to
        start: Starting point (x, y) in mm
        end: Ending point (x, y) in mm
        width: Trace width in mm
        layer: PCB layer (e.g., "F.Cu", "B.Cu")
        uuid: Segment UUID from PCB file
    """

    net_name: str
    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str
    uuid: str = ""

    @property
    def length(self) -> float:
        """Calculate the length of this segment in mm."""
        import math

        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        return math.sqrt(dx * dx + dy * dy)


@dataclass
class ViaLayout:
    """
    Via placement data.

    Attributes:
        net_name: Name of the net this via connects
        position: Position (x, y) in mm
        size: Via pad diameter in mm
        drill: Drill hole diameter in mm
        layers: List of layers the via connects (e.g., ["F.Cu", "B.Cu"])
        uuid: Via UUID from PCB file
    """

    net_name: str
    position: tuple[float, float]
    size: float
    drill: float
    layers: list[str] = field(default_factory=list)
    uuid: str = ""


@dataclass
class ZoneLayout:
    """
    Copper pour zone layout data.

    Attributes:
        net_name: Name of the net for this zone
        layer: PCB layer for the zone
        name: Optional zone name
        polygon: Boundary polygon points [(x, y), ...]
        priority: Zone fill priority
        uuid: Zone UUID from PCB file
    """

    net_name: str
    layer: str
    name: str = ""
    polygon: list[tuple[float, float]] = field(default_factory=list)
    priority: int = 0
    uuid: str = ""


@dataclass
class LayoutSnapshot:
    """
    Complete snapshot of PCB layout state.

    Captures all placement and routing information indexed by hierarchical
    component addresses. Used to preserve layout when regenerating PCB from
    modified schematic.

    Attributes:
        component_positions: Component layouts indexed by hierarchical address
        traces: Trace segments indexed by net name
        zones: Zone layouts indexed by zone name or net name
        vias: All via placements
        timestamp: When the snapshot was taken
        schematic_hash: Hash of source schematic for version tracking
        pcb_path: Path to the PCB file this snapshot was taken from
    """

    component_positions: dict[str, ComponentLayout] = field(default_factory=dict)
    traces: dict[str, list[TraceSegment]] = field(default_factory=dict)
    zones: dict[str, ZoneLayout] = field(default_factory=dict)
    vias: list[ViaLayout] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    schematic_hash: str = ""
    pcb_path: str = ""

    @property
    def component_count(self) -> int:
        """Number of components in the snapshot."""
        return len(self.component_positions)

    @property
    def trace_count(self) -> int:
        """Total number of trace segments."""
        return sum(len(segments) for segments in self.traces.values())

    @property
    def zone_count(self) -> int:
        """Number of zones in the snapshot."""
        return len(self.zones)

    @property
    def via_count(self) -> int:
        """Number of vias in the snapshot."""
        return len(self.vias)

    def get_component(self, address: str) -> ComponentLayout | None:
        """Get component layout by hierarchical address."""
        return self.component_positions.get(address)

    def get_traces_for_net(self, net_name: str) -> list[TraceSegment]:
        """Get all trace segments for a net."""
        return self.traces.get(net_name, [])

    def summary(self) -> dict:
        """Get a summary of the snapshot contents."""
        return {
            "components": self.component_count,
            "traces": self.trace_count,
            "zones": self.zone_count,
            "vias": self.via_count,
            "timestamp": self.timestamp.isoformat(),
            "pcb_path": self.pcb_path,
        }


# =============================================================================
# Subcircuit Types (for subcircuit-level preservation)
# =============================================================================


@dataclass
class ComponentOffset:
    """Offset of a component relative to a subcircuit's anchor.

    Stores position and rotation relative to the anchor component,
    allowing the subcircuit to be placed at any position while
    preserving internal component relationships.

    Attributes:
        ref: Local reference designator (e.g., "C1", "R2")
        dx: X offset from anchor position in mm
        dy: Y offset from anchor position in mm
        rotation_delta: Rotation relative to anchor in degrees
    """

    ref: str
    dx: float
    dy: float
    rotation_delta: float = 0.0

    def rotated(self, angle_deg: float) -> tuple[float, float]:
        """Get offset rotated by given angle.

        Args:
            angle_deg: Rotation angle in degrees

        Returns:
            Tuple of (rotated_dx, rotated_dy)
        """
        import math

        if angle_deg == 0:
            return self.dx, self.dy

        rad = math.radians(angle_deg)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)

        rotated_dx = self.dx * cos_a - self.dy * sin_a
        rotated_dy = self.dx * sin_a + self.dy * cos_a

        return rotated_dx, rotated_dy


@dataclass
class SubcircuitLayout:
    """Layout of a subcircuit with anchor-relative positioning.

    Represents the spatial arrangement of components within a subcircuit,
    using an anchor component as the reference point. All other components
    are stored as offsets from this anchor.

    This allows subcircuits to be:
    - Moved to new locations while preserving internal layout
    - Rotated as a unit (90, 180, 270 degrees)
    - Instantiated multiple times with consistent spacing

    Attributes:
        path: Hierarchical path to the subcircuit (e.g., "power.ldo")
        anchor_ref: Reference of the anchor component (e.g., "U1")
        anchor_position: Tuple of (x, y, rotation) for the anchor
        offsets: Dictionary mapping local refs to their ComponentOffset
        layer: PCB layer the subcircuit is on (e.g., "F.Cu")

    Example:
        >>> layout = SubcircuitLayout(
        ...     path="power.ldo",
        ...     anchor_ref="U3",
        ...     anchor_position=(50.0, 30.0, 0.0),
        ...     offsets={
        ...         "C1": ComponentOffset("C1", -2.0, -1.5, 0.0),
        ...         "C2": ComponentOffset("C2", 2.0, -1.5, 0.0),
        ...     }
        ... )
    """

    path: str
    anchor_ref: str
    anchor_position: tuple[float, float, float]  # x, y, rotation
    offsets: dict[str, ComponentOffset] = field(default_factory=dict)
    layer: str = "F.Cu"

    @property
    def component_count(self) -> int:
        """Total number of components (anchor + offsets)."""
        return 1 + len(self.offsets)

    @property
    def component_refs(self) -> list[str]:
        """List of all component references in the subcircuit."""
        return [self.anchor_ref] + list(self.offsets.keys())

    def get_position(self, ref: str) -> tuple[float, float, float] | None:
        """Get absolute position for a component in this layout.

        Args:
            ref: Local reference designator

        Returns:
            Tuple of (x, y, rotation) or None if not found
        """
        if ref == self.anchor_ref:
            return self.anchor_position

        offset = self.offsets.get(ref)
        if offset is None:
            return None

        anchor_x, anchor_y, anchor_rot = self.anchor_position

        # Rotate offset by anchor rotation
        rotated_dx, rotated_dy = offset.rotated(anchor_rot)

        return (
            anchor_x + rotated_dx,
            anchor_y + rotated_dy,
            (anchor_rot + offset.rotation_delta) % 360,
        )

    def with_anchor_position(self, new_position: tuple[float, float, float]) -> SubcircuitLayout:
        """Create a copy of this layout with a new anchor position.

        Args:
            new_position: New (x, y, rotation) for the anchor

        Returns:
            New SubcircuitLayout with updated anchor position
        """
        return SubcircuitLayout(
            path=self.path,
            anchor_ref=self.anchor_ref,
            anchor_position=new_position,
            offsets=dict(self.offsets),
            layer=self.layer,
        )

    def get_all_positions(self) -> dict[str, tuple[float, float, float]]:
        """Get absolute positions for all components.

        Returns:
            Dictionary mapping refs to (x, y, rotation) tuples
        """
        positions: dict[str, tuple[float, float, float]] = {}

        for ref in self.component_refs:
            pos = self.get_position(ref)
            if pos is not None:
                positions[ref] = pos

        return positions


# =============================================================================
# Net Mapping Types
# =============================================================================


class MatchReason(str, Enum):
    """Reason for net name matching."""

    EXACT = "exact"
    CONNECTIVITY = "connectivity"
    REMOVED = "removed"
    AMBIGUOUS = "ambiguous"


@dataclass
class NetMapping:
    """
    Mapping from old net name to new net name.

    Attributes:
        old_name: The net name in the old netlist.
        new_name: The net name in the new netlist, or None if removed.
        confidence: Confidence score for the mapping (0.0-1.0).
        match_reason: Why this mapping was detected.
        shared_pins: Number of shared pin connections (for connectivity matches).
    """

    old_name: str
    new_name: str | None
    confidence: float
    match_reason: MatchReason | str
    shared_pins: int = 0

    def __post_init__(self):
        """Convert string match_reason to enum if needed."""
        if isinstance(self.match_reason, str):
            with contextlib.suppress(ValueError):
                self.match_reason = MatchReason(self.match_reason)

    @property
    def is_exact(self) -> bool:
        """Check if this is an exact name match."""
        return self.match_reason == MatchReason.EXACT

    @property
    def is_removed(self) -> bool:
        """Check if the net was removed."""
        return self.new_name is None or self.match_reason == MatchReason.REMOVED

    @property
    def is_renamed(self) -> bool:
        """Check if the net was renamed (different name, same connectivity)."""
        return (
            self.new_name is not None
            and self.old_name != self.new_name
            and self.match_reason == MatchReason.CONNECTIVITY
        )


@dataclass
class SegmentRemap:
    """
    Record of a remapped trace segment.

    Attributes:
        segment_uuid: UUID of the segment.
        old_net_name: Original net name.
        new_net_name: New net name.
        old_net_id: Original net ID.
        new_net_id: New net ID.
    """

    segment_uuid: str
    old_net_name: str
    new_net_name: str
    old_net_id: int
    new_net_id: int


@dataclass
class OrphanedSegment:
    """
    A trace segment that could not be remapped.

    Attributes:
        segment_uuid: UUID of the segment.
        net_name: Original net name.
        net_id: Original net ID.
        reason: Why the segment couldn't be remapped.
    """

    segment_uuid: str
    net_name: str
    net_id: int
    reason: str


@dataclass
class RemapResult:
    """
    Result of remapping trace net assignments.

    Attributes:
        remapped_segments: List of successfully remapped segments.
        orphaned_segments: List of segments that couldn't be remapped.
        net_mappings: The net mappings used for remapping.
        new_nets: List of nets that are new (not in old design).
    """

    remapped_segments: list[SegmentRemap] = field(default_factory=list)
    orphaned_segments: list[OrphanedSegment] = field(default_factory=list)
    net_mappings: list[NetMapping] = field(default_factory=list)
    new_nets: list[str] = field(default_factory=list)

    @property
    def remapped_count(self) -> int:
        """Number of successfully remapped segments."""
        return len(self.remapped_segments)

    @property
    def orphaned_count(self) -> int:
        """Number of orphaned segments (need re-routing)."""
        return len(self.orphaned_segments)

    @property
    def renamed_nets(self) -> list[NetMapping]:
        """Get mappings where nets were renamed (not exact match)."""
        return [m for m in self.net_mappings if m.is_renamed]

    @property
    def removed_nets(self) -> list[NetMapping]:
        """Get mappings where nets were removed."""
        return [m for m in self.net_mappings if m.is_removed]

    def summary(self) -> dict:
        """Get a summary of the remapping results."""
        return {
            "remapped_segments": self.remapped_count,
            "orphaned_segments": self.orphaned_count,
            "total_mappings": len(self.net_mappings),
            "exact_matches": sum(1 for m in self.net_mappings if m.is_exact),
            "renamed_nets": len(self.renamed_nets),
            "removed_nets": len(self.removed_nets),
            "new_nets": len(self.new_nets),
        }


# =============================================================================
# Incremental Update Types
# =============================================================================


class ChangeType(str, Enum):
    """Type of change detected for a component."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


@dataclass
class ComponentState:
    """
    State of a component in a layout.

    Captures position, rotation, and layer information for
    comparison and preservation during incremental updates.

    Attributes:
        reference: Reference designator (e.g., "U1", "C3")
        address: Hierarchical address (e.g., "power.ldo.U1")
        position: Tuple of (x, y) position in mm
        rotation: Rotation in degrees
        layer: PCB layer (e.g., "F.Cu", "B.Cu")
        footprint: Footprint name/library
        uuid: Component UUID
    """

    reference: str
    address: str
    position: tuple[float, float]
    rotation: float
    layer: str
    footprint: str = ""
    uuid: str = ""

    @property
    def position_tuple(self) -> tuple[float, float, float]:
        """Get position as (x, y, rotation) tuple."""
        return (self.position[0], self.position[1], self.rotation)


@dataclass
class LayoutChange:
    """
    Describes a change detected between old and new design state.

    Used by ChangeDetector to report what changed and by
    IncrementalUpdater to determine what actions to take.

    Attributes:
        change_type: Type of change (added, removed, modified, unchanged)
        component_address: Hierarchical address of the component
        old_state: Component state in old layout (None if added)
        new_state: Component state in new design (None if removed)
        affected_nets: List of net names connected to this component
    """

    change_type: ChangeType | str
    component_address: str
    old_state: ComponentState | None = None
    new_state: ComponentState | None = None
    affected_nets: list[str] = field(default_factory=list)

    def __post_init__(self):
        """Convert string change_type to enum if needed."""
        if isinstance(self.change_type, str):
            with contextlib.suppress(ValueError):
                self.change_type = ChangeType(self.change_type)

    @property
    def is_added(self) -> bool:
        """Check if component was added."""
        return self.change_type == ChangeType.ADDED

    @property
    def is_removed(self) -> bool:
        """Check if component was removed."""
        return self.change_type == ChangeType.REMOVED

    @property
    def is_modified(self) -> bool:
        """Check if component was modified."""
        return self.change_type == ChangeType.MODIFIED

    @property
    def is_unchanged(self) -> bool:
        """Check if component was unchanged."""
        return self.change_type == ChangeType.UNCHANGED


@dataclass
class IncrementalSnapshot:
    """
    Snapshot of component positions in a PCB layout for incremental updates.

    Captures the state of all components for later comparison
    when detecting changes for incremental updates. This is a lighter-weight
    snapshot focused on component state, not full layout with traces/zones.

    Attributes:
        component_states: Dictionary mapping addresses to ComponentState
        net_connections: Dictionary mapping component addresses to their nets
        created_at: Timestamp when snapshot was taken (ISO format)
    """

    component_states: dict[str, ComponentState] = field(default_factory=dict)
    net_connections: dict[str, list[str]] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self):
        """Set timestamp if not provided."""
        if not self.created_at:
            from datetime import datetime, timezone

            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def component_count(self) -> int:
        """Number of components in snapshot."""
        return len(self.component_states)

    def get_state(self, address: str) -> ComponentState | None:
        """Get component state by address."""
        return self.component_states.get(address)

    def get_nets(self, address: str) -> list[str]:
        """Get nets connected to a component."""
        return self.net_connections.get(address, [])

    def addresses(self) -> set[str]:
        """Get all component addresses."""
        return set(self.component_states.keys())

    def to_dict(self) -> dict:
        """Convert to serializable dictionary."""
        return {
            "component_states": {
                addr: {
                    "reference": state.reference,
                    "address": state.address,
                    "position": list(state.position),
                    "rotation": state.rotation,
                    "layer": state.layer,
                    "footprint": state.footprint,
                    "uuid": state.uuid,
                }
                for addr, state in self.component_states.items()
            },
            "net_connections": dict(self.net_connections),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> IncrementalSnapshot:
        """Create from serialized dictionary."""
        component_states = {}
        for addr, state_data in data.get("component_states", {}).items():
            pos = state_data.get("position", [0, 0])
            component_states[addr] = ComponentState(
                reference=state_data.get("reference", ""),
                address=state_data.get("address", addr),
                position=(pos[0], pos[1]),
                rotation=state_data.get("rotation", 0.0),
                layer=state_data.get("layer", "F.Cu"),
                footprint=state_data.get("footprint", ""),
                uuid=state_data.get("uuid", ""),
            )

        return cls(
            component_states=component_states,
            net_connections=data.get("net_connections", {}),
            created_at=data.get("created_at", ""),
        )


@dataclass
class UpdateResult:
    """
    Result of applying incremental layout updates.

    Provides summary of what was updated, preserved, and what
    needs additional attention (routing, placement).

    Attributes:
        added_components: List of addresses for newly added components
        removed_components: List of addresses for removed components
        updated_components: List of addresses for modified components
        preserved_components: Count of components with preserved positions
        affected_nets: List of net names that need re-routing
        errors: List of any errors encountered during update
    """

    added_components: list[str] = field(default_factory=list)
    removed_components: list[str] = field(default_factory=list)
    updated_components: list[str] = field(default_factory=list)
    preserved_components: int = 0
    affected_nets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        """Total number of components changed (added + removed + updated)."""
        return (
            len(self.added_components) + len(self.removed_components) + len(self.updated_components)
        )

    @property
    def has_errors(self) -> bool:
        """Check if any errors occurred."""
        return len(self.errors) > 0

    def summary(self) -> dict:
        """Get a summary of the update results."""
        return {
            "added": len(self.added_components),
            "removed": len(self.removed_components),
            "updated": len(self.updated_components),
            "preserved": self.preserved_components,
            "total_changes": self.total_changes,
            "affected_nets": len(self.affected_nets),
            "errors": len(self.errors),
        }
