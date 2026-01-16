"""Assembly export types for MCP tools.

Provides dataclasses for BOM, pick-and-place, and assembly package exports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .gerber import GerberExportResult


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
