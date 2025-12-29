"""
Base classes for PCB manufacturer profiles.

Provides dataclasses for design rules, assembly capabilities, and
manufacturer profiles that can be used across different fabrication houses.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DesignRules:
    """PCB design constraints for a specific layer count and copper weight."""

    # Trace constraints
    min_trace_width_mm: float
    min_clearance_mm: float

    # Via constraints
    min_via_drill_mm: float
    min_via_diameter_mm: float
    min_annular_ring_mm: float

    # Hole constraints
    min_hole_diameter_mm: float = 0.3
    max_hole_diameter_mm: float = 6.3

    # Edge constraints
    min_copper_to_edge_mm: float = 0.3
    min_hole_to_edge_mm: float = 0.5

    # Silkscreen constraints
    min_silkscreen_width_mm: float = 0.15
    min_silkscreen_height_mm: float = 0.8

    # Solder mask
    min_solder_mask_dam_mm: float = 0.1
    min_solder_mask_clearance_mm: float = 0.05

    # Board specifications
    board_thickness_mm: float = 1.6
    outer_copper_oz: float = 1.0
    inner_copper_oz: float = 0.5

    @property
    def min_trace_width_mil(self) -> float:
        """Trace width in mils (thousandths of an inch)."""
        return self.min_trace_width_mm / 0.0254

    @property
    def min_clearance_mil(self) -> float:
        """Clearance in mils."""
        return self.min_clearance_mm / 0.0254

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "min_trace_width_mm": self.min_trace_width_mm,
            "min_clearance_mm": self.min_clearance_mm,
            "min_via_drill_mm": self.min_via_drill_mm,
            "min_via_diameter_mm": self.min_via_diameter_mm,
            "min_annular_ring_mm": self.min_annular_ring_mm,
            "min_hole_diameter_mm": self.min_hole_diameter_mm,
            "max_hole_diameter_mm": self.max_hole_diameter_mm,
            "min_copper_to_edge_mm": self.min_copper_to_edge_mm,
            "min_hole_to_edge_mm": self.min_hole_to_edge_mm,
            "min_silkscreen_width_mm": self.min_silkscreen_width_mm,
            "min_silkscreen_height_mm": self.min_silkscreen_height_mm,
            "min_solder_mask_dam_mm": self.min_solder_mask_dam_mm,
            "board_thickness_mm": self.board_thickness_mm,
            "outer_copper_oz": self.outer_copper_oz,
            "inner_copper_oz": self.inner_copper_oz,
        }


@dataclass
class AssemblyCapabilities:
    """PCBA assembly constraints."""

    # Component constraints
    min_component_pitch_mm: float = 0.4
    min_bga_pitch_mm: float = 0.5
    max_component_height_mm: float = 25.0

    # Supported package types
    supported_packages: list[str] = field(
        default_factory=lambda: [
            "0201",
            "0402",
            "0603",
            "0805",
            "1206",
            "1210",
            "SOT-23",
            "SOT-223",
            "SOT-363",
            "SOIC-8",
            "SOIC-14",
            "SOIC-16",
            "TSSOP-14",
            "TSSOP-16",
            "TSSOP-20",
            "TSSOP-28",
            "QFN",
            "QFP",
            "LQFP",
            "BGA",
        ]
    )

    # Assembly type
    supports_double_sided: bool = True
    supports_bga: bool = True
    supports_fine_pitch: bool = True  # < 0.5mm pitch

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "min_component_pitch_mm": self.min_component_pitch_mm,
            "min_bga_pitch_mm": self.min_bga_pitch_mm,
            "max_component_height_mm": self.max_component_height_mm,
            "supported_packages": self.supported_packages,
            "supports_double_sided": self.supports_double_sided,
            "supports_bga": self.supports_bga,
            "supports_fine_pitch": self.supports_fine_pitch,
        }


@dataclass
class PartsLibrary:
    """Parts library information for a manufacturer."""

    name: str  # "LCSC", "OPL", etc.
    search_url_template: str  # URL with {part_number} placeholder
    catalog_url: Optional[str] = None

    # Library tiers
    tiers: dict[str, dict] = field(default_factory=dict)
    # e.g., {"basic": {"lead_time_days": 3, "setup_fee": 0},
    #        "extended": {"lead_time_days": 7, "setup_fee": 3}}

    def get_search_url(self, part_number: str) -> str:
        """Get search URL for a part number."""
        return self.search_url_template.format(part_number=part_number)


@dataclass
class ManufacturerProfile:
    """Complete manufacturer profile with all capabilities."""

    # Basic info
    id: str  # "jlcpcb", "seeed", etc.
    name: str  # "JLCPCB", "Seeed Fusion", etc.
    website: str

    # Design rules keyed by configuration
    # e.g., "2layer_1oz", "4layer_1oz", "4layer_2oz"
    design_rules: dict[str, DesignRules]

    # Assembly capabilities (None if PCB-only)
    assembly: Optional[AssemblyCapabilities] = None

    # Parts library (None if no standard library)
    parts_library: Optional[PartsLibrary] = None

    # Lead times in working days
    lead_times: dict[str, int] = field(
        default_factory=lambda: {
            "pcb_standard": 5,
            "pcb_expedited": 2,
            "pcba_standard": 7,
            "pcba_extended": 20,
        }
    )

    # BOM format for this manufacturer
    bom_format: str = "generic"

    # Supported layer counts
    supported_layers: list[int] = field(default_factory=lambda: [1, 2, 4, 6])

    # Pricing model
    pricing_model: str = "per_pcb"  # "per_pcb", "per_sqin", etc.

    def get_design_rules(self, layers: int = 4, copper_oz: float = 1.0) -> DesignRules:
        """Get design rules for a specific configuration."""
        # Try exact match first
        key = f"{layers}layer_{copper_oz:.0f}oz"
        if key in self.design_rules:
            return self.design_rules[key]

        # Try without copper weight
        key = f"{layers}layer_1oz"
        if key in self.design_rules:
            return self.design_rules[key]

        # Fall back to most conservative (2-layer)
        if "2layer_1oz" in self.design_rules:
            return self.design_rules["2layer_1oz"]

        # Return first available
        return list(self.design_rules.values())[0]

    def get_part_search_url(self, part_number: str) -> Optional[str]:
        """Get URL to search for a part in manufacturer's library."""
        if self.parts_library:
            return self.parts_library.get_search_url(part_number)
        return None

    def supports_assembly(self) -> bool:
        """Check if manufacturer offers PCBA services."""
        return self.assembly is not None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "website": self.website,
            "supported_layers": self.supported_layers,
            "bom_format": self.bom_format,
            "pricing_model": self.pricing_model,
            "lead_times": self.lead_times,
            "supports_assembly": self.supports_assembly(),
            "has_parts_library": self.parts_library is not None,
        }
