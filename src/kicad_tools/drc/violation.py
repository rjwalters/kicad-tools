"""DRC violation data structures."""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from kicad_tools.core.types import Severity

if TYPE_CHECKING:
    from kicad_tools.drc.suggestions import FixSuggestion
    from kicad_tools.exceptions import SourcePosition


class ViolationType(Enum):
    """Known DRC violation types."""

    # Clearance violations
    CLEARANCE = "clearance"
    CLEARANCE_SEGMENT_VIA = "clearance_segment_via"
    CLEARANCE_PAD_SEGMENT = "clearance_pad_segment"
    CLEARANCE_PAD_VIA = "clearance_pad_via"
    CLEARANCE_PAD_PAD = "clearance_pad_pad"
    CLEARANCE_SEGMENT_SEGMENT = "clearance_segment_segment"
    CLEARANCE_VIA_VIA = "clearance_via_via"
    COPPER_EDGE_CLEARANCE = "copper_edge_clearance"
    EDGE_CLEARANCE_TRACE = "edge_clearance_trace"
    EDGE_CLEARANCE_PAD = "edge_clearance_pad"
    EDGE_CLEARANCE_PAD_HOLE = "edge_clearance_pad_hole"
    EDGE_CLEARANCE_VIA = "edge_clearance_via"
    EDGE_CLEARANCE_ZONE = "edge_clearance_zone"
    COURTYARD_OVERLAP = "courtyard_overlap"

    # Connection issues
    UNCONNECTED_ITEMS = "unconnected_items"
    SHORTING_ITEMS = "shorting_items"

    # Via issues
    VIA_HOLE_LARGER_THAN_PAD = "via_hole_larger_than_pad"
    VIA_ANNULAR_WIDTH = "via_annular_width"
    MICRO_VIA_HOLE_TOO_SMALL = "micro_via_hole_too_small"

    # Track/trace dimension issues
    TRACK_WIDTH = "track_width"
    TRACK_ANGLE = "track_angle"
    DIMENSION_TRACE_WIDTH = "dimension_trace_width"
    DIMENSION_VIA_DRILL = "dimension_via_drill"
    DIMENSION_VIA_DIAMETER = "dimension_via_diameter"
    DIMENSION_ANNULAR_RING = "dimension_annular_ring"
    DIMENSION_DRILL_CLEARANCE = "dimension_drill_clearance"

    # Hole issues
    DRILL_HOLE_TOO_SMALL = "drill_hole_too_small"
    DRILL_CLEARANCE = "drill_clearance"
    NPTH_HOLE_TOO_SMALL = "npth_hole_too_small"
    HOLE_NEAR_HOLE = "hole_near_hole"

    # Silkscreen
    SILK_OVER_COPPER = "silk_over_copper"
    SILK_OVERLAP = "silk_overlap"
    SILKSCREEN_LINE_WIDTH = "silkscreen_line_width"
    SILKSCREEN_TEXT_HEIGHT = "silkscreen_text_height"
    SILKSCREEN_OVER_PAD = "silkscreen_over_pad"

    # Solder mask
    SOLDER_MASK_BRIDGE = "solder_mask_bridge"
    SOLDER_MASK_CLEARANCE = "solder_mask_clearance"
    MIN_PAD_SIZE = "min_pad_size"
    PTH_ANNULAR_RING = "pth_annular_ring"

    # Impedance
    IMPEDANCE = "impedance"

    # Misc
    FOOTPRINT = "footprint"
    MALFORMED_OUTLINE = "malformed_outline"
    DUPLICATE_FOOTPRINT = "duplicate_footprint"
    EXTRA_FOOTPRINT = "extra_footprint"
    MISSING_FOOTPRINT = "missing_footprint"

    # Unknown (catch-all)
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, s: str) -> "ViolationType":
        """Parse violation type from string.

        Handles three sources of type strings:
        - KiCad-cli DRC report types (e.g., "clearance", "track_width")
        - Validate-module rule_id values (e.g., "clearance_pad_pad",
          "dimension_trace_width", "silkscreen_line_width")
        - Free-form descriptions from older reports
        """
        s_lower = s.lower().strip()

        # Try direct enum value match first (covers both legacy and new members)
        for vtype in cls:
            if vtype.value == s_lower:
                return vtype

        # Explicit alias table for validate-module rule_ids and common
        # variants that don't match an enum value directly.  This table
        # is checked before the fuzzy heuristics so that specific rule
        # names are never misclassified.
        _ALIASES: dict[str, ViolationType] = {
            # clearance subtypes produced by validate clearance checker
            "clearance_pad_pad": cls.CLEARANCE_PAD_PAD,
            "clearance_pad_segment": cls.CLEARANCE_PAD_SEGMENT,
            "clearance_pad_via": cls.CLEARANCE_PAD_VIA,
            "clearance_segment_segment": cls.CLEARANCE_SEGMENT_SEGMENT,
            "clearance_segment_via": cls.CLEARANCE_SEGMENT_VIA,
            "clearance_via_via": cls.CLEARANCE_VIA_VIA,
            # clearance subtypes using "trace" (synonym for "segment")
            "clearance_pad_trace": cls.CLEARANCE_PAD_SEGMENT,
            "clearance_trace_trace": cls.CLEARANCE_SEGMENT_SEGMENT,
            "clearance_trace_via": cls.CLEARANCE_SEGMENT_VIA,
            # edge clearance subtypes
            "edge_clearance_trace": cls.EDGE_CLEARANCE_TRACE,
            "edge_clearance_pad": cls.EDGE_CLEARANCE_PAD,
            "edge_clearance_pad_hole": cls.EDGE_CLEARANCE_PAD_HOLE,
            "edge_clearance_via": cls.EDGE_CLEARANCE_VIA,
            "edge_clearance_zone": cls.EDGE_CLEARANCE_ZONE,
            # dimension rules from validate dimensions checker
            "dimension_trace_width": cls.DIMENSION_TRACE_WIDTH,
            "dimension_via_drill": cls.DIMENSION_VIA_DRILL,
            "dimension_via_diameter": cls.DIMENSION_VIA_DIAMETER,
            "dimension_annular_ring": cls.DIMENSION_ANNULAR_RING,
            "dimension_drill_clearance": cls.DIMENSION_DRILL_CLEARANCE,
            # silkscreen rules from validate silkscreen checker
            "silkscreen_line_width": cls.SILKSCREEN_LINE_WIDTH,
            "silkscreen_text_height": cls.SILKSCREEN_TEXT_HEIGHT,
            "silkscreen_over_pad": cls.SILKSCREEN_OVER_PAD,
            # solder mask rules from validate solder_mask checker
            "solder_mask_clearance": cls.SOLDER_MASK_CLEARANCE,
            "min_pad_size": cls.MIN_PAD_SIZE,
            "pth_annular_ring": cls.PTH_ANNULAR_RING,
            # impedance rule from validate impedance checker
            "impedance": cls.IMPEDANCE,
        }

        alias_match = _ALIASES.get(s_lower)
        if alias_match is not None:
            return alias_match

        # Fuzzy heuristics for free-form strings (e.g., KiCad-cli descriptions)
        # Check drill_clearance before general clearance (both contain "clearance")
        if "drill" in s_lower and "clearance" in s_lower:
            return cls.DRILL_CLEARANCE
        if "clearance" in s_lower:
            if "edge" in s_lower:
                return cls.COPPER_EDGE_CLEARANCE
            if "segment" in s_lower and "via" in s_lower:
                return cls.CLEARANCE_SEGMENT_VIA
            if "pad" in s_lower and "segment" in s_lower:
                return cls.CLEARANCE_PAD_SEGMENT
            if "pad" in s_lower and "via" in s_lower:
                return cls.CLEARANCE_PAD_VIA
            return cls.CLEARANCE
        if "unconnected" in s_lower:
            return cls.UNCONNECTED_ITEMS
        if "short" in s_lower:
            return cls.SHORTING_ITEMS
        if "courtyard" in s_lower:
            return cls.COURTYARD_OVERLAP
        if ("track" in s_lower or "trace" in s_lower) and "width" in s_lower:
            return cls.TRACK_WIDTH
        if "via" in s_lower:
            if "annular" in s_lower:
                return cls.VIA_ANNULAR_WIDTH
            if "hole" in s_lower and "larger" in s_lower:
                return cls.VIA_HOLE_LARGER_THAN_PAD
            if "micro" in s_lower:
                return cls.MICRO_VIA_HOLE_TOO_SMALL
        if "drill" in s_lower:
            return cls.DRILL_HOLE_TOO_SMALL
        if "silk" in s_lower:
            if "copper" in s_lower:
                return cls.SILK_OVER_COPPER
            if "line" in s_lower and "width" in s_lower:
                return cls.SILKSCREEN_LINE_WIDTH
            if "text" in s_lower and "height" in s_lower:
                return cls.SILKSCREEN_TEXT_HEIGHT
            if "over" in s_lower and "pad" in s_lower:
                return cls.SILKSCREEN_OVER_PAD
            return cls.SILK_OVERLAP
        if "solder" in s_lower and "mask" in s_lower:
            if "clearance" in s_lower:
                return cls.SOLDER_MASK_CLEARANCE
            return cls.SOLDER_MASK_BRIDGE
        if "impedance" in s_lower:
            return cls.IMPEDANCE
        if "footprint" in s_lower:
            if "duplicate" in s_lower:
                return cls.DUPLICATE_FOOTPRINT
            if "extra" in s_lower:
                return cls.EXTRA_FOOTPRINT
            if "missing" in s_lower:
                return cls.MISSING_FOOTPRINT
            return cls.FOOTPRINT
        if "outline" in s_lower:
            return cls.MALFORMED_OUTLINE

        return cls.UNKNOWN


@dataclass
class Location:
    """Position on the PCB."""

    x_mm: float
    y_mm: float
    layer: str = ""

    @classmethod
    def from_string(cls, s: str) -> Optional["Location"]:
        """Parse location from string like '@(162.4500 mm, 100.3250 mm)'."""
        import re

        # Match @(x mm, y mm) pattern
        match = re.search(r"@\s*\(\s*([\d.]+)\s*mm\s*,\s*([\d.]+)\s*mm\s*\)", s)
        if match:
            return cls(
                x_mm=float(match.group(1)),
                y_mm=float(match.group(2)),
            )

        # Also try pos:{x:..., y:...} for JSON format
        match = re.search(r'"x"\s*:\s*([\d.]+).*?"y"\s*:\s*([\d.]+)', s)
        if match:
            return cls(
                x_mm=float(match.group(1)),
                y_mm=float(match.group(2)),
            )

        return None

    def __str__(self) -> str:
        layer_str = f" on {self.layer}" if self.layer else ""
        return f"({self.x_mm:.2f}, {self.y_mm:.2f}) mm{layer_str}"


@dataclass
class DRCViolation:
    """Represents a single DRC violation.

    Includes both board-level location (x_mm, y_mm) and optional file-level
    source position (file:line:column) for precise error reporting.
    """

    type: ViolationType
    type_str: str  # Original type string from report
    severity: Severity
    message: str
    rule: str = ""
    locations: list[Location] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)

    # Extracted numeric values (when available)
    required_value_mm: float | None = None
    actual_value_mm: float | None = None

    # Source position in KiCad file (file:line:column)
    source_position: "SourcePosition | None" = None

    # Fix suggestions (populated by generate_fix_suggestions)
    suggestions: list["FixSuggestion"] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        """Check if this is an error (vs warning)."""
        return self.severity == Severity.ERROR

    @property
    def is_clearance(self) -> bool:
        """Check if this is a clearance violation."""
        return self.type in (
            ViolationType.CLEARANCE,
            ViolationType.CLEARANCE_SEGMENT_VIA,
            ViolationType.CLEARANCE_PAD_SEGMENT,
            ViolationType.CLEARANCE_PAD_VIA,
            ViolationType.CLEARANCE_PAD_PAD,
            ViolationType.CLEARANCE_SEGMENT_SEGMENT,
            ViolationType.CLEARANCE_VIA_VIA,
            ViolationType.COPPER_EDGE_CLEARANCE,
            ViolationType.EDGE_CLEARANCE_TRACE,
            ViolationType.EDGE_CLEARANCE_PAD,
            ViolationType.EDGE_CLEARANCE_PAD_HOLE,
            ViolationType.EDGE_CLEARANCE_VIA,
            ViolationType.EDGE_CLEARANCE_ZONE,
        )

    @property
    def is_connection(self) -> bool:
        """Check if this is a connection issue."""
        return self.type in (ViolationType.UNCONNECTED_ITEMS, ViolationType.SHORTING_ITEMS)

    @property
    def primary_location(self) -> Location | None:
        """Get the first location if available."""
        return self.locations[0] if self.locations else None

    @property
    def location_str(self) -> str:
        """Format location as file:line:col or board coordinates."""
        if self.source_position:
            return str(self.source_position)
        elif self.primary_location:
            return str(self.primary_location)
        return ""

    @property
    def delta_mm(self) -> float | None:
        """Calculate the difference between required and actual values.

        For clearance violations, this is how much more clearance is needed.
        Returns None if values are not available.
        """
        if self.required_value_mm is None or self.actual_value_mm is None:
            return None
        return self.required_value_mm - self.actual_value_mm

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {
            "type": self.type.value,
            "type_str": self.type_str,
            "severity": self.severity.value,
            "message": self.message,
            "rule": self.rule,
            "locations": [
                {"x_mm": loc.x_mm, "y_mm": loc.y_mm, "layer": loc.layer} for loc in self.locations
            ],
            "items": self.items,
            "nets": self.nets,
            "required_value_mm": self.required_value_mm,
            "actual_value_mm": self.actual_value_mm,
            "delta_mm": self.delta_mm,
        }
        if self.source_position:
            result["source_position"] = self.source_position.to_dict()
        if self.suggestions:
            # Handle both FixSuggestion objects and plain strings
            result["suggestions"] = [
                s.to_dict() if hasattr(s, "to_dict") else str(s) for s in self.suggestions
            ]
        return result

    def __str__(self) -> str:
        # Prefer file:line:col format if available
        if self.source_position:
            return f"{self.source_position}: [{self.type_str}] {self.message}"
        loc_str = ""
        if self.primary_location:
            loc_str = f" at {self.primary_location}"
        return f"[{self.type_str}]: {self.message}{loc_str}"
