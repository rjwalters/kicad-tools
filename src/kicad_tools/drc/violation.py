"""DRC violation data structures."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(Enum):
    """Violation severity level."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @classmethod
    def from_string(cls, s: str) -> "Severity":
        """Parse severity from string."""
        s_lower = s.lower().strip()
        if "error" in s_lower:
            return cls.ERROR
        elif "warning" in s_lower:
            return cls.WARNING
        return cls.INFO


class ViolationType(Enum):
    """Known DRC violation types."""

    # Clearance violations
    CLEARANCE = "clearance"
    COPPER_EDGE_CLEARANCE = "copper_edge_clearance"
    COURTYARD_OVERLAP = "courtyard_overlap"

    # Connection issues
    UNCONNECTED_ITEMS = "unconnected_items"
    SHORTING_ITEMS = "shorting_items"

    # Via issues
    VIA_HOLE_LARGER_THAN_PAD = "via_hole_larger_than_pad"
    VIA_ANNULAR_WIDTH = "via_annular_width"
    MICRO_VIA_HOLE_TOO_SMALL = "micro_via_hole_too_small"

    # Track issues
    TRACK_WIDTH = "track_width"
    TRACK_ANGLE = "track_angle"

    # Hole issues
    DRILL_HOLE_TOO_SMALL = "drill_hole_too_small"
    NPTH_HOLE_TOO_SMALL = "npth_hole_too_small"
    HOLE_NEAR_HOLE = "hole_near_hole"

    # Silkscreen
    SILK_OVER_COPPER = "silk_over_copper"
    SILK_OVERLAP = "silk_overlap"

    # Solder mask
    SOLDER_MASK_BRIDGE = "solder_mask_bridge"

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
        """Parse violation type from string."""
        s_lower = s.lower().strip()

        # Try direct match
        for vtype in cls:
            if vtype.value == s_lower:
                return vtype

        # Try partial matches for common patterns
        if "clearance" in s_lower:
            if "edge" in s_lower:
                return cls.COPPER_EDGE_CLEARANCE
            return cls.CLEARANCE
        if "unconnected" in s_lower:
            return cls.UNCONNECTED_ITEMS
        if "short" in s_lower:
            return cls.SHORTING_ITEMS
        if "courtyard" in s_lower:
            return cls.COURTYARD_OVERLAP
        if "track" in s_lower and "width" in s_lower:
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
            return cls.SILK_OVERLAP
        if "solder" in s_lower and "mask" in s_lower:
            return cls.SOLDER_MASK_BRIDGE
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
    """Represents a single DRC violation."""

    type: ViolationType
    type_str: str  # Original type string from report
    severity: Severity
    message: str
    rule: str = ""
    locations: list[Location] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)

    # Extracted numeric values (when available)
    required_value_mm: Optional[float] = None
    actual_value_mm: Optional[float] = None

    @property
    def is_error(self) -> bool:
        """Check if this is an error (vs warning)."""
        return self.severity == Severity.ERROR

    @property
    def is_clearance(self) -> bool:
        """Check if this is a clearance violation."""
        return self.type in (ViolationType.CLEARANCE, ViolationType.COPPER_EDGE_CLEARANCE)

    @property
    def is_connection(self) -> bool:
        """Check if this is a connection issue."""
        return self.type in (ViolationType.UNCONNECTED_ITEMS, ViolationType.SHORTING_ITEMS)

    @property
    def primary_location(self) -> Optional[Location]:
        """Get the first location if available."""
        return self.locations[0] if self.locations else None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
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
        }

    def __str__(self) -> str:
        loc_str = ""
        if self.primary_location:
            loc_str = f" at {self.primary_location}"
        return f"[{self.type_str}]: {self.message}{loc_str}"
