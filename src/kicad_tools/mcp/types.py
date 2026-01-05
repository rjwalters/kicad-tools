"""Type definitions for MCP tools.

Provides dataclasses for tool inputs and outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# =============================================================================
# Board Analysis Types
# =============================================================================


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


# =============================================================================
# Gerber Export Types
# =============================================================================


@dataclass
class GerberFile:
    """Information about a generated Gerber file."""

    filename: str
    """Name of the generated file."""

    layer: str
    """KiCad layer name (e.g., 'F.Cu', 'B.Mask')."""

    file_type: str
    """File category: 'copper', 'soldermask', 'silkscreen', 'paste', 'outline', 'drill'."""

    size_bytes: int
    """File size in bytes."""


@dataclass
class GerberExportResult:
    """Result of a Gerber export operation."""

    success: bool
    """Whether the export completed successfully."""

    output_dir: str
    """Directory containing the exported files."""

    zip_file: str | None = None
    """Path to zip archive if created, None otherwise."""

    files: list[GerberFile] = field(default_factory=list)
    """List of generated files with metadata."""

    layer_count: int = 0
    """Number of copper layers in the board."""

    warnings: list[str] = field(default_factory=list)
    """Any warnings encountered during export."""

    error: str | None = None
    """Error message if success is False."""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "output_dir": self.output_dir,
            "zip_file": self.zip_file,
            "files": [
                {
                    "filename": f.filename,
                    "layer": f.layer,
                    "file_type": f.file_type,
                    "size_bytes": f.size_bytes,
                }
                for f in self.files
            ],
            "layer_count": self.layer_count,
            "warnings": self.warnings,
            "error": self.error,
        }


# =============================================================================
# DRC Violation Types
# =============================================================================


@dataclass
class ViolationLocation:
    """Location of a DRC violation on the PCB.

    Attributes:
        x_mm: X coordinate in millimeters
        y_mm: Y coordinate in millimeters
        layer: PCB layer name (e.g., "F.Cu", "B.Cu")
    """

    x_mm: float
    y_mm: float
    layer: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "x_mm": round(self.x_mm, 3),
            "y_mm": round(self.y_mm, 3),
            "layer": self.layer,
        }


@dataclass
class AffectedItem:
    """An item affected by a DRC violation.

    Attributes:
        item_type: Type of item ("pad", "track", "via", "zone", "component")
        reference: Reference designator (e.g., "U1", "R15")
        net: Net name if applicable
    """

    item_type: str
    reference: str | None = None
    net: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "item_type": self.item_type,
            "reference": self.reference,
            "net": self.net,
        }


@dataclass
class DRCViolation:
    """A single DRC violation with location and fix suggestions.

    Attributes:
        id: Unique identifier for this violation
        type: Violation type (clearance, track_width, via_size, etc.)
        severity: Severity level (error, warning)
        message: Human-readable description of the violation
        location: Location on the PCB
        affected_items: Items involved in the violation
        fix_suggestion: Suggested fix for the violation
        required_value_mm: Minimum required value (when applicable)
        actual_value_mm: Measured value that violated the rule
    """

    id: str
    type: str
    severity: str
    message: str
    location: ViolationLocation
    affected_items: list[AffectedItem] = field(default_factory=list)
    fix_suggestion: str | None = None
    required_value_mm: float | None = None
    actual_value_mm: float | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "location": self.location.to_dict(),
            "affected_items": [i.to_dict() for i in self.affected_items],
            "fix_suggestion": self.fix_suggestion,
            "required_value_mm": self.required_value_mm,
            "actual_value_mm": self.actual_value_mm,
        }


@dataclass
class DRCResult:
    """Result of running a Design Rule Check on a PCB.

    Attributes:
        passed: Whether the DRC passed (no errors, warnings allowed)
        violation_count: Total number of violations
        error_count: Number of error-severity violations
        warning_count: Number of warning-severity violations
        violations: List of all violations found
        summary_by_type: Count of violations by type
        manufacturer: Manufacturer rules used for the check
        layers: Number of PCB layers checked against
    """

    passed: bool
    violation_count: int
    error_count: int
    warning_count: int
    violations: list[DRCViolation] = field(default_factory=list)
    summary_by_type: dict[str, int] = field(default_factory=dict)
    manufacturer: str = ""
    layers: int = 4

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "passed": self.passed,
            "violation_count": self.violation_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "violations": [v.to_dict() for v in self.violations],
            "summary_by_type": self.summary_by_type,
            "manufacturer": self.manufacturer,
            "layers": self.layers,
        }


# =============================================================================
# Gerber Export Types (continued)
# =============================================================================


# Layer name to file type mapping
LAYER_FILE_TYPES: dict[str, str] = {
    "F.Cu": "copper",
    "B.Cu": "copper",
    "In1.Cu": "copper",
    "In2.Cu": "copper",
    "In3.Cu": "copper",
    "In4.Cu": "copper",
    "In5.Cu": "copper",
    "In6.Cu": "copper",
    "F.Mask": "soldermask",
    "B.Mask": "soldermask",
    "F.SilkS": "silkscreen",
    "B.SilkS": "silkscreen",
    "F.Paste": "paste",
    "B.Paste": "paste",
    "Edge.Cuts": "outline",
}


def get_file_type(layer: str) -> str:
    """Get the file type for a given layer name."""
    return LAYER_FILE_TYPES.get(layer, "other")
