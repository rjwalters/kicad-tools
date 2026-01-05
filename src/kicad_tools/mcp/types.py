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
# Session Management Types
# =============================================================================


@dataclass
class SessionInfo:
    """Information about an active placement session.

    Provides metadata and statistics about a placement session
    for monitoring and debugging purposes.

    Attributes:
        id: Unique session identifier (8-character UUID prefix).
        pcb_path: Path to the PCB file being edited.
        created_at: ISO 8601 timestamp when session was created.
        last_accessed: ISO 8601 timestamp when session was last accessed.
        pending_moves: Number of uncommitted component moves.
        components: Total number of components in the session.
        current_score: Current placement quality score (lower is better).
    """

    id: str
    pcb_path: str
    created_at: str  # ISO 8601 timestamp
    last_accessed: str  # ISO 8601 timestamp
    pending_moves: int
    components: int
    current_score: float

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "pcb_path": self.pcb_path,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "pending_moves": self.pending_moves,
            "components": self.components,
            "current_score": round(self.current_score, 4),
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


# =============================================================================
# Assembly Export Types
# =============================================================================


@dataclass
class BOMExportResult:
    """Result of BOM export operation.

    Attributes:
        output_path: Path to the generated BOM file
        component_count: Total number of components in BOM
        unique_parts: Number of unique part numbers
        missing_lcsc: Number of parts missing LCSC part numbers
    """

    output_path: str
    component_count: int
    unique_parts: int
    missing_lcsc: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "output_path": self.output_path,
            "component_count": self.component_count,
            "unique_parts": self.unique_parts,
            "missing_lcsc": self.missing_lcsc,
        }


@dataclass
class BOMItemResult:
    """A single item or group in standalone BOM export.

    Used by export_bom tool to provide detailed component information.

    Attributes:
        reference: Reference designator(s), comma-separated when grouped
        value: Component value (e.g., "10k", "100nF")
        footprint: Footprint name
        quantity: Number of components in this group
        lcsc_part: LCSC part number if available
        description: Component description if available
        manufacturer: Manufacturer name if available
        mpn: Manufacturer Part Number if available
    """

    reference: str
    value: str
    footprint: str
    quantity: int
    lcsc_part: str | None = None
    description: str | None = None
    manufacturer: str | None = None
    mpn: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "reference": self.reference,
            "value": self.value,
            "footprint": self.footprint,
            "quantity": self.quantity,
            "lcsc_part": self.lcsc_part,
            "description": self.description,
            "manufacturer": self.manufacturer,
            "mpn": self.mpn,
        }


@dataclass
class BOMGenerationResult:
    """Result of standalone BOM generation via export_bom tool.

    More comprehensive than BOMExportResult, includes full item details
    and supports data-only mode (no file output).

    Attributes:
        success: Whether the export completed successfully
        total_parts: Total number of component instances
        unique_parts: Number of unique part types (groups)
        output_path: Path to exported file (None if data-only)
        missing_lcsc: List of references missing LCSC part numbers
        items: List of BOM items with full details
        format: Export format used
        warnings: Any warnings encountered
        error: Error message if success is False
    """

    success: bool
    total_parts: int = 0
    unique_parts: int = 0
    output_path: str | None = None
    missing_lcsc: list[str] = field(default_factory=list)
    items: list[BOMItemResult] = field(default_factory=list)
    format: str = "csv"
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "total_parts": self.total_parts,
            "unique_parts": self.unique_parts,
            "output_path": self.output_path,
            "missing_lcsc": self.missing_lcsc,
            "items": [item.to_dict() for item in self.items],
            "format": self.format,
            "warnings": self.warnings,
            "error": self.error,
        }


@dataclass
class PnPExportResult:
    """Result of pick-and-place export operation.

    Attributes:
        output_path: Path to the generated PnP/CPL file
        component_count: Total number of placed components
        layers: Layers with components (["top"], ["bottom"], or ["top", "bottom"])
        rotation_corrections: Number of components with rotation corrections applied
    """

    output_path: str
    component_count: int
    layers: list[str] = field(default_factory=list)
    rotation_corrections: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "output_path": self.output_path,
            "component_count": self.component_count,
            "layers": self.layers,
            "rotation_corrections": self.rotation_corrections,
        }


@dataclass
class CostEstimate:
    """Estimated manufacturing costs.

    Attributes:
        pcb_cost_usd: Estimated PCB fabrication cost in USD
        assembly_cost_usd: Estimated assembly labor cost in USD
        parts_cost_usd: Estimated component parts cost in USD
        total_usd: Total estimated cost in USD
        notes: Additional notes about the estimate
    """

    pcb_cost_usd: float | None = None
    assembly_cost_usd: float | None = None
    parts_cost_usd: float | None = None
    total_usd: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "pcb_cost_usd": self.pcb_cost_usd,
            "assembly_cost_usd": self.assembly_cost_usd,
            "parts_cost_usd": self.parts_cost_usd,
            "total_usd": self.total_usd,
            "notes": self.notes,
        }


@dataclass
class AssemblyExportResult:
    """Result of a complete assembly package export.

    Attributes:
        success: Whether the export completed successfully
        output_dir: Directory containing all exported files
        manufacturer: Target manufacturer (jlcpcb, pcbway, seeed, generic)
        gerbers: Gerber export results if included
        bom: BOM export results if included
        pnp: Pick-and-place export results if included
        zip_file: Path to combined zip archive ready for upload
        warnings: Any warnings encountered during export
        cost_estimate: Optional cost estimate for manufacturing
        error: Error message if success is False
    """

    success: bool
    output_dir: str
    manufacturer: str
    gerbers: GerberExportResult | None = None
    bom: BOMExportResult | None = None
    pnp: PnPExportResult | None = None
    zip_file: str | None = None
    warnings: list[str] = field(default_factory=list)
    cost_estimate: CostEstimate | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "output_dir": self.output_dir,
            "manufacturer": self.manufacturer,
            "gerbers": self.gerbers.to_dict() if self.gerbers else None,
            "bom": self.bom.to_dict() if self.bom else None,
            "pnp": self.pnp.to_dict() if self.pnp else None,
            "zip_file": self.zip_file,
            "warnings": self.warnings,
            "cost_estimate": self.cost_estimate.to_dict() if self.cost_estimate else None,
            "error": self.error,
        }


# =============================================================================
# Placement Analysis Types
# =============================================================================


@dataclass
class PlacementScores:
    """Placement quality scores by category.

    Attributes:
        wire_length: Wire length score (lower is better, 0-100 normalized).
        congestion: Congestion score (lower is better, 0-100 normalized).
        thermal: Thermal quality score (higher is better, proper heat spreading).
        signal_integrity: Signal integrity score (higher is better).
        manufacturing: Manufacturing/DFM score (higher is better).
    """

    wire_length: float
    congestion: float
    thermal: float
    signal_integrity: float
    manufacturing: float

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "wire_length": round(self.wire_length, 1),
            "congestion": round(self.congestion, 1),
            "thermal": round(self.thermal, 1),
            "signal_integrity": round(self.signal_integrity, 1),
            "manufacturing": round(self.manufacturing, 1),
        }


@dataclass
class PlacementIssue:
    """A placement issue or recommendation.

    Attributes:
        severity: Issue severity ("critical", "warning", "suggestion").
        category: Issue category ("thermal", "routing", "si", "dfm").
        description: Human-readable description of the issue.
        affected_components: List of component reference designators involved.
        suggestion: Actionable suggestion to fix the issue.
        location: Optional (x, y) location in mm.
    """

    severity: str
    category: str
    description: str
    affected_components: list[str]
    suggestion: str
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result: dict = {
            "severity": self.severity,
            "category": self.category,
            "description": self.description,
            "affected_components": self.affected_components,
            "suggestion": self.suggestion,
        }
        if self.location is not None:
            result["location"] = {"x": round(self.location[0], 2), "y": round(self.location[1], 2)}
        return result


@dataclass
class PlacementCluster:
    """A detected functional cluster of components.

    Attributes:
        name: Cluster name (e.g., "mcu_cluster", "power_section").
        components: List of component reference designators in the cluster.
        centroid: Cluster center position (x, y) in mm.
        compactness_score: How compact the cluster is (0-100, higher is better).
    """

    name: str
    components: list[str]
    centroid: tuple[float, float]
    compactness_score: float

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "components": self.components,
            "centroid": {"x": round(self.centroid[0], 2), "y": round(self.centroid[1], 2)},
            "compactness_score": round(self.compactness_score, 1),
        }


@dataclass
class RoutingEstimate:
    """Estimated routing difficulty based on placement.

    Attributes:
        estimated_routability: Routability score (0-100, higher is easier to route).
        congestion_hotspots: List of (x, y) positions with high congestion.
        difficult_nets: List of net names that will be difficult to route.
    """

    estimated_routability: float
    congestion_hotspots: list[tuple[float, float]] = field(default_factory=list)
    difficult_nets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "estimated_routability": round(self.estimated_routability, 1),
            "congestion_hotspots": [
                {"x": round(x, 2), "y": round(y, 2)} for x, y in self.congestion_hotspots
            ],
            "difficult_nets": self.difficult_nets,
        }


@dataclass
class PlacementAnalysis:
    """Complete placement quality analysis.

    This is the main result type returned by placement_analyze().
    Contains comprehensive information about placement quality including
    scores by category, identified issues, functional clusters, and
    routing difficulty estimate.

    Attributes:
        file_path: Absolute path to the analyzed PCB file.
        overall_score: Overall placement quality score (0-100).
        categories: Scores broken down by category.
        issues: List of identified placement issues.
        clusters: Detected functional clusters.
        routing_estimate: Estimated routing difficulty.
    """

    file_path: str
    overall_score: float
    categories: PlacementScores
    issues: list[PlacementIssue] = field(default_factory=list)
    clusters: list[PlacementCluster] = field(default_factory=list)
    routing_estimate: RoutingEstimate = field(
        default_factory=lambda: RoutingEstimate(estimated_routability=0.0)
    )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "file_path": self.file_path,
            "overall_score": round(self.overall_score, 1),
            "categories": self.categories.to_dict(),
            "issues": [i.to_dict() for i in self.issues],
            "clusters": [c.to_dict() for c in self.clusters],
            "routing_estimate": self.routing_estimate.to_dict(),
        }


# =============================================================================
# Clearance Measurement Types
# =============================================================================


@dataclass
class ClearanceMeasurement:
    """A single clearance measurement between two copper elements.

    Attributes:
        from_item: Reference of the first item (e.g., "U1-1", "Track-abc123")
        from_type: Type of the first item ("pad", "track", "via")
        to_item: Reference of the second item
        to_type: Type of the second item
        clearance_mm: Edge-to-edge clearance in millimeters
        location: (x, y) location where the minimum clearance was measured
        layer: PCB layer where the clearance was measured
    """

    from_item: str
    from_type: str
    to_item: str
    to_type: str
    clearance_mm: float
    location: tuple[float, float]
    layer: str

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "from_item": self.from_item,
            "from_type": self.from_type,
            "to_item": self.to_item,
            "to_type": self.to_type,
            "clearance_mm": round(self.clearance_mm, 4),
            "location": {"x": self.location[0], "y": self.location[1]},
            "layer": self.layer,
        }


@dataclass
class ClearanceResult:
    """Result of a clearance measurement between items.

    Attributes:
        item1: First item identifier (component ref or net name)
        item2: Second item identifier (component ref or net name)
        min_clearance_mm: Minimum clearance found between items
        location: (x, y) position where minimum clearance occurs
        layer: Layer where minimum clearance was found
        clearances: List of all individual clearance measurements
        passes_rules: Whether the clearance meets design rules
        required_clearance_mm: Required minimum clearance from design rules
    """

    item1: str
    item2: str
    min_clearance_mm: float
    location: tuple[float, float]
    layer: str
    clearances: list[ClearanceMeasurement] = field(default_factory=list)
    passes_rules: bool = True
    required_clearance_mm: float | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "item1": self.item1,
            "item2": self.item2,
            "min_clearance_mm": round(self.min_clearance_mm, 4),
            "location": {"x": self.location[0], "y": self.location[1]},
            "layer": self.layer,
            "clearances": [c.to_dict() for c in self.clearances],
            "passes_rules": self.passes_rules,
            "required_clearance_mm": self.required_clearance_mm,
        }

    def summary(self) -> str:
        """Generate a human-readable summary of the clearance result."""
        status = "PASSES" if self.passes_rules else "FAILS"
        summary = f"Clearance between {self.item1} and {self.item2}: {self.min_clearance_mm:.4f} mm [{status}]"
        if self.required_clearance_mm is not None:
            summary += f" (required: {self.required_clearance_mm:.4f} mm)"
        summary += (
            f"\nLocation: ({self.location[0]:.3f}, {self.location[1]:.3f}) mm on {self.layer}"
        )
        return summary


# =============================================================================
# Session Management Types
# =============================================================================


@dataclass
class ComponentPosition:
    """Position information for a component.

    Attributes:
        ref: Component reference designator (e.g., "C1", "R5")
        x: X position in millimeters
        y: Y position in millimeters
        rotation: Rotation in degrees
        fixed: Whether component is fixed/locked
    """

    ref: str
    x: float
    y: float
    rotation: float
    fixed: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "ref": self.ref,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "rotation": round(self.rotation, 1),
            "fixed": self.fixed,
        }


@dataclass
class RoutingImpactInfo:
    """Routing impact information for a move.

    Attributes:
        affected_nets: List of nets affected by the move
        estimated_length_change_mm: Estimated change in routing length
        crossing_changes: Change in net crossing count
    """

    affected_nets: list[str] = field(default_factory=list)
    estimated_length_change_mm: float = 0.0
    crossing_changes: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "affected_nets": self.affected_nets,
            "estimated_length_change_mm": round(self.estimated_length_change_mm, 3),
            "crossing_changes": self.crossing_changes,
        }


@dataclass
class ViolationInfo:
    """Information about a placement constraint violation.

    Attributes:
        type: Violation type (e.g., "clearance", "overlap", "boundary")
        description: Human-readable description
        severity: Severity level ("error", "warning", "info")
        component: Component reference if applicable
        location: (x, y) location if applicable
    """

    type: str
    description: str
    severity: str = "error"
    component: str = ""
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "type": self.type,
            "description": self.description,
            "severity": self.severity,
            "component": self.component,
            "location": list(self.location) if self.location else None,
        }


@dataclass
class StartSessionResult:
    """Result of starting a placement session.

    Attributes:
        success: Whether session was started successfully
        session_id: Unique session identifier
        component_count: Number of components in the session
        fixed_count: Number of fixed (unmovable) components
        initial_score: Initial placement score
        error_message: Error message if success is False
    """

    success: bool
    session_id: str = ""
    component_count: int = 0
    fixed_count: int = 0
    initial_score: float = 0.0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "session_id": self.session_id,
            "component_count": self.component_count,
            "fixed_count": self.fixed_count,
            "initial_score": round(self.initial_score, 4),
            "error_message": self.error_message,
        }


@dataclass
class QueryMoveResult:
    """Result of querying a hypothetical move.

    Attributes:
        success: Whether the query was successful
        would_succeed: Whether applying this move would succeed
        score_delta: Change in placement score (negative = improvement)
        new_violations: New violations that would be created
        resolved_violations: Existing violations that would be resolved
        affected_components: Components that share nets with moved component
        routing_impact: Impact on routing
        warnings: Any warnings about the move
        error_message: Error message if success is False
    """

    success: bool
    would_succeed: bool = False
    score_delta: float = 0.0
    new_violations: list[ViolationInfo] = field(default_factory=list)
    resolved_violations: list[ViolationInfo] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)
    routing_impact: RoutingImpactInfo | None = None
    warnings: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "would_succeed": self.would_succeed,
            "score_delta": round(self.score_delta, 4),
            "new_violations": [v.to_dict() for v in self.new_violations],
            "resolved_violations": [v.to_dict() for v in self.resolved_violations],
            "affected_components": self.affected_components,
            "routing_impact": self.routing_impact.to_dict() if self.routing_impact else None,
            "warnings": self.warnings,
            "error_message": self.error_message,
        }


@dataclass
class ApplyMoveResult:
    """Result of applying a move within a session.

    Attributes:
        success: Whether the move was applied successfully
        move_id: Index of this move for potential undo
        component: Updated component position
        new_score: New placement score after move
        score_delta: Change in placement score
        pending_moves: Total number of pending moves in session
        error_message: Error message if success is False
    """

    success: bool
    move_id: int = 0
    component: ComponentPosition | None = None
    new_score: float = 0.0
    score_delta: float = 0.0
    pending_moves: int = 0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "move_id": self.move_id,
            "component": self.component.to_dict() if self.component else None,
            "new_score": round(self.new_score, 4),
            "score_delta": round(self.score_delta, 4),
            "pending_moves": self.pending_moves,
            "error_message": self.error_message,
        }


@dataclass
class CommitResult:
    """Result of committing session changes to PCB file.

    Attributes:
        success: Whether changes were committed successfully
        output_path: Path to the saved PCB file
        moves_applied: Number of moves that were applied
        initial_score: Score at session start
        final_score: Score after all moves
        score_improvement: Total score improvement (positive = better)
        components_moved: List of component references that were moved
        session_closed: Whether the session was closed
        error_message: Error message if success is False
    """

    success: bool
    output_path: str = ""
    moves_applied: int = 0
    initial_score: float = 0.0
    final_score: float = 0.0
    score_improvement: float = 0.0
    components_moved: list[str] = field(default_factory=list)
    session_closed: bool = False
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "output_path": self.output_path,
            "moves_applied": self.moves_applied,
            "initial_score": round(self.initial_score, 4),
            "final_score": round(self.final_score, 4),
            "score_improvement": round(self.score_improvement, 4),
            "components_moved": self.components_moved,
            "session_closed": self.session_closed,
            "error_message": self.error_message,
        }


@dataclass
class RollbackResult:
    """Result of rolling back session changes.

    Attributes:
        success: Whether rollback was successful
        moves_discarded: Number of moves that were discarded
        session_closed: Whether the session was closed
        error_message: Error message if success is False
    """

    success: bool
    moves_discarded: int = 0
    session_closed: bool = False
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "moves_discarded": self.moves_discarded,
            "session_closed": self.session_closed,
            "error_message": self.error_message,
        }


@dataclass
class UndoResult:
    """Result of undoing the last move.

    Attributes:
        success: Whether undo was successful
        restored_component: Position of restored component
        pending_moves: Remaining pending moves
        current_score: Score after undo
        error_message: Error message if success is False
    """

    success: bool
    restored_component: ComponentPosition | None = None
    pending_moves: int = 0
    current_score: float = 0.0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "restored_component": (
                self.restored_component.to_dict() if self.restored_component else None
            ),
            "pending_moves": self.pending_moves,
            "current_score": round(self.current_score, 4),
            "error_message": self.error_message,
        }


# =============================================================================
# Routing Types
# =============================================================================


@dataclass
class NetRoutingStatus:
    """Routing status for a single net.

    Attributes:
        name: Net name (e.g., "GND", "SPI_CLK")
        status: Routing status ("unrouted", "partial", "complete")
        pins: Number of pads/pins on this net
        routed_connections: Number of connections already routed
        total_connections: Total number of connections needed (pins - 1 for tree)
        estimated_length_mm: Estimated routing length in millimeters
        difficulty: Estimated routing difficulty ("easy", "medium", "hard")
        reason: Explanation of difficulty rating if not easy
    """

    name: str
    status: str  # "unrouted", "partial", "complete"
    pins: int
    routed_connections: int
    total_connections: int
    estimated_length_mm: float
    difficulty: str  # "easy", "medium", "hard"
    reason: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "status": self.status,
            "pins": self.pins,
            "routed_connections": self.routed_connections,
            "total_connections": self.total_connections,
            "estimated_length_mm": round(self.estimated_length_mm, 2),
            "difficulty": self.difficulty,
            "reason": self.reason,
        }


@dataclass
class UnroutedNetsResult:
    """Result of get_unrouted_nets operation.

    Attributes:
        total_nets: Total number of nets in the design
        unrouted_count: Number of completely unrouted nets
        partial_count: Number of partially routed nets
        complete_count: Number of fully routed nets
        nets: List of nets needing routing (unrouted and partial)
    """

    total_nets: int
    unrouted_count: int
    partial_count: int
    complete_count: int
    nets: list[NetRoutingStatus] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "total_nets": self.total_nets,
            "unrouted_count": self.unrouted_count,
            "partial_count": self.partial_count,
            "complete_count": self.complete_count,
            "nets": [n.to_dict() for n in self.nets],
        }


@dataclass
class RouteNetResult:
    """Result of route_net operation.

    Attributes:
        success: Whether the routing operation succeeded
        net_name: Name of the net that was routed
        routed_connections: Number of connections successfully routed
        total_connections: Total connections that needed routing
        trace_length_mm: Total trace length in millimeters
        vias_used: Number of vias placed
        layers_used: List of layer names used for routing
        output_path: Path where the result was saved
        error_message: Error message if success is False
        suggestions: Suggestions if routing failed or was incomplete
    """

    success: bool
    net_name: str
    routed_connections: int = 0
    total_connections: int = 0
    trace_length_mm: float = 0.0
    vias_used: int = 0
    layers_used: list[str] = field(default_factory=list)
    output_path: str | None = None
    error_message: str | None = None
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "net_name": self.net_name,
            "routed_connections": self.routed_connections,
            "total_connections": self.total_connections,
            "trace_length_mm": round(self.trace_length_mm, 2),
            "vias_used": self.vias_used,
            "layers_used": self.layers_used,
            "output_path": self.output_path,
            "error_message": self.error_message,
            "suggestions": self.suggestions,
        }
