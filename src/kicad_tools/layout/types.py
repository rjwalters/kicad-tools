"""
Types for layout preservation module.

Defines data structures for:
- ComponentAddress: hierarchical component identification
- ComponentLayout: component placement data
- TraceSegment: PCB trace routing data
- ViaLayout: via placement data
- ZoneLayout: copper pour zone data
- LayoutSnapshot: complete PCB layout state
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
