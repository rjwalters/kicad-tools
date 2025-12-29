"""ERC violation data structures."""

from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
    """Violation severity level."""

    ERROR = "error"
    WARNING = "warning"
    EXCLUSION = "exclusion"

    @classmethod
    def from_string(cls, s: str) -> "Severity":
        """Parse severity from string."""
        s_lower = s.lower().strip()
        if "error" in s_lower:
            return cls.ERROR
        elif "warning" in s_lower:
            return cls.WARNING
        elif "exclu" in s_lower:
            return cls.EXCLUSION
        return cls.WARNING


class ERCViolationType(Enum):
    """Known ERC violation types from KiCad."""

    # Connection errors
    PIN_NOT_CONNECTED = "pin_not_connected"
    PIN_NOT_DRIVEN = "pin_not_driven"
    POWER_PIN_NOT_DRIVEN = "power_pin_not_driven"
    NO_CONNECT_CONNECTED = "no_connect_connected"
    NO_CONNECT_DANGLING = "no_connect_dangling"

    # Pin conflicts
    CONFLICTING_NETCLASS = "conflicting_netclass"
    DIFFERENT_UNIT_FOOTPRINT = "different_unit_footprint"
    DIFFERENT_UNIT_NET = "different_unit_net"
    DUPLICATE_PIN_ERROR = "duplicate_pin_error"
    DUPLICATE_REFERENCE = "duplicate_reference"

    # Symbol/sheet errors
    ENDPOINT_OFF_GRID = "endpoint_off_grid"
    EXTRA_UNITS = "extra_units"
    GLOBAL_LABEL_DANGLING = "global_label_dangling"
    HIER_LABEL_MISMATCH = "hier_label_mismatch"
    LABEL_DANGLING = "label_dangling"
    LIB_SYMBOL_ISSUES = "lib_symbol_issues"
    MISSING_BIDI_PIN = "missing_bidi_pin"
    MISSING_INPUT_PIN = "missing_input_pin"
    MISSING_POWER_PIN = "missing_power_pin"
    MISSING_UNIT = "missing_unit"
    MULTIPLE_NET_NAMES = "multiple_net_names"

    # Schematic structure
    BUS_ENTRY_NEEDED = "bus_entry_needed"
    BUS_TO_BUS_CONFLICT = "bus_to_bus_conflict"
    BUS_TO_NET_CONFLICT = "bus_to_net_conflict"
    FOUR_WAY_JUNCTION = "four_way_junction"
    NET_NOT_BUS_MEMBER = "net_not_bus_member"
    SIMILAR_LABELS = "similar_labels"
    SIMULATION_MODEL = "simulation_model"
    UNRESOLVED_VARIABLE = "unresolved_variable"
    UNANNOTATED = "unannotated"
    UNSPECIFIED = "unspecified"
    WIRE_DANGLING = "wire_dangling"

    # Unknown
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, s: str) -> "ERCViolationType":
        """Parse violation type from string."""
        s_lower = s.lower().strip()

        # Try direct match
        for vtype in cls:
            if vtype.value == s_lower:
                return vtype

        return cls.UNKNOWN


# Human-readable descriptions for each type
ERC_TYPE_DESCRIPTIONS = {
    # Connection errors
    "pin_not_connected": "Unconnected pin",
    "pin_not_driven": "Input pin not driven",
    "power_pin_not_driven": "Power input not driven",
    "no_connect_connected": "No-connect pin is connected",
    "no_connect_dangling": "No-connect flag not connected to pin",
    # Pin conflicts
    "conflicting_netclass": "Conflicting netclass assignments",
    "different_unit_footprint": "Different footprint across symbol units",
    "different_unit_net": "Different nets on same pin across units",
    "duplicate_pin_error": "Duplicate pin in symbol",
    "duplicate_reference": "Duplicate reference designator",
    # Symbol/sheet errors
    "endpoint_off_grid": "Wire endpoint off grid",
    "extra_units": "Extra units in multi-unit symbol",
    "global_label_dangling": "Global label not connected",
    "hier_label_mismatch": "Hierarchical label mismatch",
    "label_dangling": "Label not connected",
    "lib_symbol_issues": "Library symbol issues",
    "missing_bidi_pin": "Missing bidirectional pin",
    "missing_input_pin": "Missing input pin",
    "missing_power_pin": "Missing power pin",
    "missing_unit": "Missing unit in multi-unit symbol",
    "multiple_net_names": "Wire has multiple net names",
    # Schematic structure
    "bus_entry_needed": "Bus entry needed",
    "bus_to_bus_conflict": "Bus to bus conflict",
    "bus_to_net_conflict": "Bus to net conflict",
    "four_way_junction": "Four-way wire junction",
    "net_not_bus_member": "Net label on bus wire",
    "similar_labels": "Similar labels (possible typo)",
    "simulation_model": "Simulation model issue",
    "unresolved_variable": "Unresolved text variable",
    "unannotated": "Symbol not annotated",
    "unspecified": "Unspecified error",
    "wire_dangling": "Wire not connected at both ends",
    # Unknown
    "unknown": "Unknown violation type",
}

# Category groupings for display
ERC_CATEGORIES = {
    "Connection": [
        "pin_not_connected",
        "pin_not_driven",
        "power_pin_not_driven",
        "no_connect_connected",
        "no_connect_dangling",
    ],
    "Pin Conflicts": [
        "conflicting_netclass",
        "different_unit_footprint",
        "different_unit_net",
        "duplicate_pin_error",
        "duplicate_reference",
    ],
    "Labels": [
        "global_label_dangling",
        "hier_label_mismatch",
        "label_dangling",
        "multiple_net_names",
        "similar_labels",
    ],
    "Structure": [
        "bus_entry_needed",
        "bus_to_bus_conflict",
        "bus_to_net_conflict",
        "endpoint_off_grid",
        "four_way_junction",
        "net_not_bus_member",
        "wire_dangling",
    ],
    "Symbols": [
        "extra_units",
        "lib_symbol_issues",
        "missing_bidi_pin",
        "missing_input_pin",
        "missing_power_pin",
        "missing_unit",
        "simulation_model",
        "unannotated",
    ],
    "Other": ["unresolved_variable", "unspecified", "unknown"],
}


@dataclass
class ERCViolation:
    """Represents a single ERC violation."""

    type: ERCViolationType
    type_str: str  # Original type string from report
    severity: Severity
    description: str
    sheet: str = ""
    pos_x: float = 0
    pos_y: float = 0
    items: list[str] = field(default_factory=list)
    excluded: bool = False

    @property
    def is_error(self) -> bool:
        """Check if this is an error (vs warning)."""
        return self.severity == Severity.ERROR

    @property
    def is_connection_issue(self) -> bool:
        """Check if this is a connection-related violation."""
        return self.type in (
            ERCViolationType.PIN_NOT_CONNECTED,
            ERCViolationType.PIN_NOT_DRIVEN,
            ERCViolationType.POWER_PIN_NOT_DRIVEN,
            ERCViolationType.NO_CONNECT_CONNECTED,
            ERCViolationType.NO_CONNECT_DANGLING,
        )

    @property
    def is_label_issue(self) -> bool:
        """Check if this is a label-related violation."""
        return self.type in (
            ERCViolationType.GLOBAL_LABEL_DANGLING,
            ERCViolationType.HIER_LABEL_MISMATCH,
            ERCViolationType.LABEL_DANGLING,
            ERCViolationType.MULTIPLE_NET_NAMES,
            ERCViolationType.SIMILAR_LABELS,
        )

    @property
    def type_description(self) -> str:
        """Get human-readable description of the violation type."""
        return ERC_TYPE_DESCRIPTIONS.get(self.type_str, self.type_str.replace("_", " ").title())

    @property
    def location_str(self) -> str:
        """Format location for display."""
        if self.sheet:
            return f"{self.sheet} at ({self.pos_x:.1f}, {self.pos_y:.1f})"
        elif self.pos_x or self.pos_y:
            return f"({self.pos_x:.1f}, {self.pos_y:.1f})"
        return ""

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "type": self.type.value,
            "type_str": self.type_str,
            "type_description": self.type_description,
            "severity": self.severity.value,
            "description": self.description,
            "sheet": self.sheet,
            "position": {"x": self.pos_x, "y": self.pos_y},
            "items": self.items,
            "excluded": self.excluded,
        }

    def __str__(self) -> str:
        loc_str = f" at {self.location_str}" if self.location_str else ""
        return f"[{self.type_str}]: {self.description}{loc_str}"
