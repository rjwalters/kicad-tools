"""
PCB Vocabulary - Qualitative spatial concepts for LLM reasoning.

This module defines the conceptual vocabulary that enables LLMs to reason
about PCB layout without dealing with precise coordinates. The vocabulary
captures:

1. Spatial Relationships - "near", "between", "north of"
2. Functional Groups - "power section", "analog zone"
3. Net Types - "clock signal", "high-current power"
4. Routing Priorities - what to route first and why

These concepts allow the LLM to make strategic decisions:
- "Route MCLK around the analog section via the northern path"
- "Keep decoupling caps within 3mm of their IC"
- "Use the eastern routing channel for GPIO signals"
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SpatialRelation(Enum):
    """Qualitative spatial relationships between objects."""

    # Cardinal directions
    NORTH_OF = "north_of"
    SOUTH_OF = "south_of"
    EAST_OF = "east_of"
    WEST_OF = "west_of"
    NORTHEAST_OF = "northeast_of"
    NORTHWEST_OF = "northwest_of"
    SOUTHEAST_OF = "southeast_of"
    SOUTHWEST_OF = "southwest_of"

    # Proximity
    NEAR = "near"  # Within 5mm
    ADJACENT = "adjacent"  # Within 2mm
    FAR = "far"  # More than 20mm

    # Containment
    INSIDE = "inside"
    OUTSIDE = "outside"
    AT_EDGE = "at_edge"
    AT_CORNER = "at_corner"

    # Between
    BETWEEN = "between"
    AMONG = "among"

    # Alignment
    ALIGNED_HORIZONTAL = "aligned_horizontal"
    ALIGNED_VERTICAL = "aligned_vertical"
    DIAGONAL = "diagonal"

    @classmethod
    def from_positions(
        cls,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        threshold: float = 3.0,
    ) -> list["SpatialRelation"]:
        """Determine spatial relations from object 1 to object 2."""
        relations = []

        dx = x2 - x1
        dy = y2 - y1
        dist = (dx**2 + dy**2) ** 0.5

        # Proximity
        if dist < 2:
            relations.append(cls.ADJACENT)
        elif dist < 5:
            relations.append(cls.NEAR)
        elif dist > 20:
            relations.append(cls.FAR)

        # Direction (from obj1's perspective, where is obj2?)
        if abs(dx) > threshold or abs(dy) > threshold:
            if dy < -threshold and abs(dx) < threshold:
                relations.append(cls.NORTH_OF)  # obj2 is north of obj1
            elif dy > threshold and abs(dx) < threshold:
                relations.append(cls.SOUTH_OF)
            elif dx > threshold and abs(dy) < threshold:
                relations.append(cls.EAST_OF)
            elif dx < -threshold and abs(dy) < threshold:
                relations.append(cls.WEST_OF)
            elif dy < -threshold and dx > threshold:
                relations.append(cls.NORTHEAST_OF)
            elif dy < -threshold and dx < -threshold:
                relations.append(cls.NORTHWEST_OF)
            elif dy > threshold and dx > threshold:
                relations.append(cls.SOUTHEAST_OF)
            elif dy > threshold and dx < -threshold:
                relations.append(cls.SOUTHWEST_OF)

        # Alignment
        if abs(dy) < 1.0:
            relations.append(cls.ALIGNED_HORIZONTAL)
        if abs(dx) < 1.0:
            relations.append(cls.ALIGNED_VERTICAL)

        return relations


@dataclass
class SpatialRegion:
    """A named region of the PCB for reasoning.

    Regions provide a way to talk about areas of the board:
    - "the analog section"
    - "near the GPIO header"
    - "the northern routing channel"
    """

    name: str
    description: str
    bounds: tuple[float, float, float, float]  # x1, y1, x2, y2
    is_keepout: bool = False
    is_routing_channel: bool = False
    priority: int = 0  # For overlapping regions

    @property
    def center(self) -> tuple[float, float]:
        """Center point of region."""
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    @property
    def width(self) -> float:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> float:
        return self.bounds[3] - self.bounds[1]

    def contains(self, x: float, y: float) -> bool:
        """Check if point is inside region."""
        x1, y1, x2, y2 = self.bounds
        return x1 <= x <= x2 and y1 <= y <= y2

    def overlaps(self, other: "SpatialRegion") -> bool:
        """Check if regions overlap."""
        x1, y1, x2, y2 = self.bounds
        ox1, oy1, ox2, oy2 = other.bounds
        return not (x2 < ox1 or ox2 < x1 or y2 < oy1 or oy2 < y1)


class NetType(Enum):
    """Classification of net types for routing priority."""

    GROUND = "ground"  # GND, VSS - highest priority, may use planes
    POWER = "power"  # VCC, VDD, +3.3V - high priority, wider traces
    CLOCK = "clock"  # CLK, MCLK - sensitive, length matching
    HIGH_SPEED = "high_speed"  # >50MHz signals, impedance controlled
    DIFFERENTIAL = "differential"  # Paired signals, matched routing
    ANALOG = "analog"  # Sensitive to noise, keep away from digital
    I2C = "i2c"  # SDA, SCL - moderate priority
    SPI = "spi"  # MOSI, MISO, SCK, CS - moderate priority
    GPIO = "gpio"  # General purpose, low priority
    SIGNAL = "signal"  # Default signal type

    @classmethod
    def classify(cls, net_name: str) -> "NetType":
        """Classify a net by its name."""
        name = net_name.lower()

        # Ground nets
        if any(g in name for g in ["gnd", "vss", "ground", "agnd", "dgnd"]):
            return cls.GROUND

        # Power nets
        if any(
            p in name
            for p in ["+", "vcc", "vdd", "3v3", "5v", "12v", "vin", "vout", "pwr"]
        ):
            return cls.POWER

        # Clock nets
        if any(c in name for c in ["clk", "clock", "mclk", "bclk", "lrclk", "xtal"]):
            return cls.CLOCK

        # I2C
        if any(i in name for i in ["sda", "scl", "i2c"]):
            return cls.I2C

        # SPI
        if any(s in name for s in ["mosi", "miso", "sck", "sclk", "cs", "nss", "spi"]):
            return cls.SPI

        # Analog
        if any(a in name for a in ["ain", "aout", "analog", "vref"]):
            return cls.ANALOG

        # GPIO
        if "gpio" in name:
            return cls.GPIO

        return cls.SIGNAL


@dataclass
class RoutingPriority:
    """Routing priority for a net or net class.

    Priority determines:
    1. Order of routing (lower number = routed first)
    2. Trace width and clearance
    3. Layer preference
    4. Special constraints (length matching, impedance)
    """

    priority: int  # 1 = highest priority
    trace_width: float = 0.2  # mm
    clearance: float = 0.2  # mm
    preferred_layers: list[str] = field(default_factory=list)
    avoid_regions: list[str] = field(default_factory=list)  # Region names
    length_match_group: Optional[str] = None
    max_length: Optional[float] = None
    min_length: Optional[float] = None
    via_preference: str = "minimize"  # "minimize", "allow", "prefer_layer_change"

    @classmethod
    def for_net_type(cls, net_type: NetType) -> "RoutingPriority":
        """Create default priority for a net type."""
        if net_type == NetType.GROUND:
            return cls(
                priority=1,
                trace_width=0.3,
                clearance=0.2,
                preferred_layers=["In1.Cu"],  # Ground plane
                via_preference="allow",
            )
        elif net_type == NetType.POWER:
            return cls(
                priority=2,
                trace_width=0.4,
                clearance=0.2,
                preferred_layers=["F.Cu", "B.Cu"],
                via_preference="allow",
            )
        elif net_type == NetType.CLOCK:
            return cls(
                priority=3,
                trace_width=0.2,
                clearance=0.3,  # Extra clearance
                preferred_layers=["F.Cu"],  # Single layer preferred
                avoid_regions=["analog"],
                via_preference="minimize",
            )
        elif net_type == NetType.ANALOG:
            return cls(
                priority=4,
                trace_width=0.2,
                clearance=0.3,
                preferred_layers=["F.Cu"],
                avoid_regions=["digital", "power"],
                via_preference="minimize",
            )
        elif net_type == NetType.SPI:
            return cls(
                priority=5,
                trace_width=0.2,
                clearance=0.2,
                via_preference="allow",
            )
        elif net_type == NetType.I2C:
            return cls(
                priority=6,
                trace_width=0.2,
                clearance=0.2,
                via_preference="allow",
            )
        else:
            return cls(
                priority=10,
                trace_width=0.2,
                clearance=0.2,
                via_preference="allow",
            )


@dataclass
class ComponentGroup:
    """A functional group of components.

    Groups help organize placement and routing:
    - "power supply" - regulator, caps, inductors
    - "DAC section" - DAC IC, decoupling, output filtering
    - "clock generation" - oscillator, load caps
    """

    name: str
    description: str
    components: list[str]  # Reference designators
    function: str  # "power", "analog", "digital", "io"
    preferred_region: Optional[str] = None  # Preferred board region
    internal_routing_priority: int = 1  # Route within group first

    def __contains__(self, ref: str) -> bool:
        return ref in self.components


# =============================================================================
# Region Templates
# =============================================================================


def create_hat_regions(width: float = 65.0, height: float = 56.0) -> list[SpatialRegion]:
    """Create standard regions for a Raspberry Pi HAT layout."""
    # Assuming origin at top-left, Y increasing downward (KiCad convention)
    regions = [
        # GPIO header area (top-right for standard HAT)
        SpatialRegion(
            name="gpio_header",
            description="Raspberry Pi GPIO header area - fixed position",
            bounds=(width - 55, 0, width, 12),
            is_keepout=False,
        ),
        # Mounting holes (corners)
        SpatialRegion(
            name="mounting_tl",
            description="Top-left mounting hole keepout",
            bounds=(0, 0, 6, 6),
            is_keepout=True,
        ),
        SpatialRegion(
            name="mounting_tr",
            description="Top-right mounting hole keepout",
            bounds=(width - 6, 0, width, 6),
            is_keepout=True,
        ),
        SpatialRegion(
            name="mounting_bl",
            description="Bottom-left mounting hole keepout",
            bounds=(0, height - 6, 6, height),
            is_keepout=True,
        ),
        SpatialRegion(
            name="mounting_br",
            description="Bottom-right mounting hole keepout",
            bounds=(width - 6, height - 6, width, height),
            is_keepout=True,
        ),
        # Routing channels
        SpatialRegion(
            name="north_channel",
            description="Northern routing channel below GPIO header",
            bounds=(5, 12, width - 5, 20),
            is_routing_channel=True,
        ),
        SpatialRegion(
            name="south_channel",
            description="Southern routing channel above bottom edge",
            bounds=(5, height - 15, width - 5, height - 5),
            is_routing_channel=True,
        ),
        SpatialRegion(
            name="west_channel",
            description="Western routing channel along left edge",
            bounds=(0, 10, 10, height - 10),
            is_routing_channel=True,
        ),
        SpatialRegion(
            name="east_channel",
            description="Eastern routing channel along right edge",
            bounds=(width - 10, 10, width, height - 10),
            is_routing_channel=True,
        ),
        # Center working area
        SpatialRegion(
            name="center",
            description="Central component placement area",
            bounds=(10, 20, width - 10, height - 15),
        ),
    ]
    return regions


# =============================================================================
# Vocabulary for Prompts
# =============================================================================


def describe_position(
    x: float, y: float, board_width: float, board_height: float
) -> str:
    """Generate a natural language description of a position on the board."""
    # Normalize to 0-1
    nx = x / board_width
    ny = y / board_height

    # Horizontal position
    if nx < 0.33:
        h_pos = "western"
    elif nx > 0.67:
        h_pos = "eastern"
    else:
        h_pos = "central"

    # Vertical position (assuming Y increases downward)
    if ny < 0.33:
        v_pos = "northern"
    elif ny > 0.67:
        v_pos = "southern"
    else:
        v_pos = "middle"

    # Edge detection
    if nx < 0.1:
        return f"at the west edge, {v_pos} section"
    elif nx > 0.9:
        return f"at the east edge, {v_pos} section"
    elif ny < 0.1:
        return f"at the north edge, {h_pos} section"
    elif ny > 0.9:
        return f"at the south edge, {h_pos} section"
    else:
        return f"in the {v_pos}-{h_pos} area"


def describe_distance(d: float) -> str:
    """Convert distance in mm to natural language."""
    if d < 1:
        return "very close"
    elif d < 3:
        return "adjacent"
    elif d < 10:
        return "nearby"
    elif d < 20:
        return "moderately far"
    else:
        return "far"


def describe_net_type(net_type: NetType) -> str:
    """Generate routing guidance for a net type."""
    guidance = {
        NetType.GROUND: "Ground net - consider using plane or wide traces, star topology from single point",
        NetType.POWER: "Power net - use wider traces (0.4mm+), keep paths short, add decoupling at destination",
        NetType.CLOCK: "Clock signal - minimize trace length, avoid vias, keep away from analog section, consider guard traces",
        NetType.HIGH_SPEED: "High-speed signal - control impedance, minimize stubs, use ground reference",
        NetType.DIFFERENTIAL: "Differential pair - route together, match lengths, maintain consistent spacing",
        NetType.ANALOG: "Analog signal - isolate from digital noise, use ground shielding, minimize length",
        NetType.I2C: "I2C bus - moderate priority, can share routing channels with other I2C signals",
        NetType.SPI: "SPI bus - route clock first, keep signals grouped, watch for crosstalk",
        NetType.GPIO: "GPIO signal - low priority, flexible routing, use available channels",
        NetType.SIGNAL: "General signal - standard routing rules apply",
    }
    return guidance.get(net_type, "Standard routing")
