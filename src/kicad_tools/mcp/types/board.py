"""Board analysis types for MCP tools.

Provides dataclasses for PCB board analysis including dimensions,
layers, components, nets, zones, and routing status.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BoardDimensions:
    """Board physical dimensions extracted from Edge.Cuts outline.

    Attributes:
        width_mm: Board width in millimeters
        height_mm: Board height in millimeters
        area_mm2: Board area in square millimeters
        outline_type: Type of outline ("rectangle", "polygon", "complex")
    """

    width_mm: float
    height_mm: float
    area_mm2: float
    outline_type: str  # "rectangle", "polygon", "complex"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "width_mm": round(self.width_mm, 2),
            "height_mm": round(self.height_mm, 2),
            "area_mm2": round(self.area_mm2, 2),
            "outline_type": self.outline_type,
        }


@dataclass
class LayerInfo:
    """Information about PCB copper layers.

    Attributes:
        copper_layers: Number of copper layers (2, 4, 6, etc.)
        layer_names: Names of all copper layers
        has_internal_planes: Whether board has internal power/ground planes
    """

    copper_layers: int
    layer_names: list[str]
    has_internal_planes: bool

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "copper_layers": self.copper_layers,
            "layer_names": self.layer_names,
            "has_internal_planes": self.has_internal_planes,
        }


@dataclass
class ComponentSummary:
    """Summary of components on the PCB.

    Attributes:
        total_count: Total number of components
        smd_count: Number of SMD (surface mount) components
        through_hole_count: Number of through-hole components
        by_type: Component counts by type (e.g., {"resistor": 45, "capacitor": 23})
        fixed_count: Number of components marked as locked/fixed
        unplaced_count: Number of components not yet placed (at origin)
    """

    total_count: int
    smd_count: int
    through_hole_count: int
    by_type: dict[str, int]
    fixed_count: int
    unplaced_count: int

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "total_count": self.total_count,
            "smd_count": self.smd_count,
            "through_hole_count": self.through_hole_count,
            "by_type": self.by_type,
            "fixed_count": self.fixed_count,
            "unplaced_count": self.unplaced_count,
        }


@dataclass
class NetFanout:
    """Information about a high-fanout net.

    Attributes:
        net_name: Name of the net
        connection_count: Number of pad connections
    """

    net_name: str
    connection_count: int

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "net_name": self.net_name,
            "connection_count": self.connection_count,
        }


@dataclass
class NetSummary:
    """Summary of nets on the PCB.

    Attributes:
        total_nets: Total number of nets (excluding unconnected net 0)
        routed_nets: Number of fully routed nets
        unrouted_nets: Number of unrouted or partially routed nets
        power_nets: List of power/ground net names (GND, VCC, 3V3, etc.)
        high_fanout_nets: Nets with more than 10 connections
    """

    total_nets: int
    routed_nets: int
    unrouted_nets: int
    power_nets: list[str]
    high_fanout_nets: list[NetFanout]

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "total_nets": self.total_nets,
            "routed_nets": self.routed_nets,
            "unrouted_nets": self.unrouted_nets,
            "power_nets": self.power_nets,
            "high_fanout_nets": [n.to_dict() for n in self.high_fanout_nets],
        }


@dataclass
class ZoneInfo:
    """Information about a copper zone (pour).

    Attributes:
        net_name: Net this zone is connected to
        layer: Layer the zone is on
        priority: Zone fill priority
        is_filled: Whether the zone has been filled
    """

    net_name: str
    layer: str
    priority: int
    is_filled: bool

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "net_name": self.net_name,
            "layer": self.layer,
            "priority": self.priority,
            "is_filled": self.is_filled,
        }


@dataclass
class RoutingStatus:
    """Routing completion status.

    Attributes:
        completion_percent: Percentage of routing complete (0-100)
        total_airwires: Number of unrouted connections (airwires)
        total_trace_length_mm: Total trace length in millimeters
        via_count: Number of vias on the board
    """

    completion_percent: float
    total_airwires: int
    total_trace_length_mm: float
    via_count: int

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "completion_percent": round(self.completion_percent, 1),
            "total_airwires": self.total_airwires,
            "total_trace_length_mm": round(self.total_trace_length_mm, 2),
            "via_count": self.via_count,
        }


@dataclass
class BoardAnalysis:
    """Complete analysis of a KiCad PCB file.

    This is the main result type returned by analyze_board().
    Contains comprehensive information about the PCB including
    dimensions, layers, components, nets, zones, and routing status.

    Attributes:
        file_path: Absolute path to the analyzed PCB file
        board_dimensions: Physical dimensions and outline type
        layers: Copper layer information
        components: Component summary statistics
        nets: Net summary and routing status
        zones: List of copper pour zones
        routing_status: Overall routing completion status
    """

    file_path: str
    board_dimensions: BoardDimensions
    layers: LayerInfo
    components: ComponentSummary
    nets: NetSummary
    zones: list[ZoneInfo] = field(default_factory=list)
    routing_status: RoutingStatus = field(
        default_factory=lambda: RoutingStatus(
            completion_percent=0.0,
            total_airwires=0,
            total_trace_length_mm=0.0,
            via_count=0,
        )
    )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "file_path": self.file_path,
            "board_dimensions": self.board_dimensions.to_dict(),
            "layers": self.layers.to_dict(),
            "components": self.components.to_dict(),
            "nets": self.nets.to_dict(),
            "zones": [z.to_dict() for z in self.zones],
            "routing_status": self.routing_status.to_dict(),
        }
