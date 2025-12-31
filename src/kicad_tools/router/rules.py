"""
Design rules and net class routing parameters.

This module provides:
- DesignRules: Trace width, clearance, via parameters, and A* costs
- NetClassRouting: Per-net-class routing preferences
- Predefined net classes for common use cases
"""

from dataclasses import dataclass, field

from .layers import Layer


@dataclass
class ZoneRules:
    """Design rules specific to zone (copper pour) handling.

    These parameters control how zones interact with traces, pads, and vias
    during routing. They mirror KiCad's zone settings.

    Attributes:
        clearance: Zone-to-trace clearance in mm
        min_thickness: Minimum copper width within zone in mm
        thermal_gap: Gap between pad and zone copper for thermal relief in mm
        thermal_bridge_width: Width of thermal relief spokes in mm
        thermal_spoke_count: Number of thermal relief spokes (typically 2 or 4)
        thermal_spoke_angle: Rotation of spoke pattern in degrees (0 or 45)
        pth_connection: Connection type for PTH pads ("thermal", "solid", "none")
        smd_connection: Connection type for SMD pads ("thermal", "solid", "none")
        via_connection: Connection type for vias ("thermal", "solid", "none")
        remove_islands: Whether to remove isolated copper islands
        island_min_area: Minimum area for island removal in mm²
    """

    clearance: float = 0.2  # Zone-to-trace clearance (mm)
    min_thickness: float = 0.2  # Minimum copper width (mm)
    thermal_gap: float = 0.3  # Gap for thermal relief (mm)
    thermal_bridge_width: float = 0.3  # Spoke width (mm)
    thermal_spoke_count: int = 4  # Number of spokes
    thermal_spoke_angle: float = 45.0  # Spoke rotation (degrees)
    pth_connection: str = "thermal"  # PTH pad connection type
    smd_connection: str = "thermal"  # SMD pad connection type
    via_connection: str = "solid"  # Via connection type
    remove_islands: bool = True  # Remove isolated islands
    island_min_area: float = 0.5  # Minimum island area (mm²)


@dataclass
class DesignRules:
    """Design rules for routing."""

    trace_width: float = 0.2  # mm
    trace_clearance: float = 0.2  # mm
    via_drill: float = 0.35  # mm (JLCPCB min is 0.3, use 0.35 for margin)
    via_diameter: float = 0.7  # mm (0.35 drill + 0.35 annular ring)
    via_clearance: float = 0.2  # mm
    grid_resolution: float = 0.1  # mm (routing grid)

    # Layer preferences
    preferred_layer: Layer = Layer.F_CU
    alternate_layer: Layer = Layer.B_CU

    # Costs for A* (tune these for routing style)
    cost_straight: float = 1.0
    cost_diagonal: float = 1.414
    cost_turn: float = 5.0  # Penalty for changing direction (bends)
    cost_via: float = 10.0  # Penalty for layer change
    cost_layer_inner: float = 5.0  # Penalty for using inner layers

    # Congestion-aware routing
    cost_congestion: float = 2.0  # Multiplier for congested regions
    congestion_threshold: float = 0.3  # Density above which region is congested
    congestion_grid_size: int = 10  # Cells per congestion region

    # Zone-specific rules
    zone_rules: ZoneRules = field(default_factory=ZoneRules)

    # Zone routing costs
    cost_zone_same_net: float = 0.1  # Low cost - encourage using zone copper
    cost_zone_clearance: float = 2.0  # Cost near zone boundaries


@dataclass
class NetClassRouting:
    """Routing parameters for a net class."""

    name: str
    priority: int = 5  # 1=highest, 10=lowest
    trace_width: float = 0.2  # Override trace width
    clearance: float = 0.2  # Override clearance
    via_size: float = 0.6  # Override via diameter
    cost_multiplier: float = 1.0  # Cost multiplier (lower = prefer this net)
    length_critical: bool = False  # Must minimize length
    noise_sensitive: bool = False  # Avoid crossing other nets

    # Zone-related parameters
    zone_priority: int = 0  # Zone fill priority (higher = fills first)
    zone_connection: str = "thermal"  # Default connection type ("thermal", "solid", "none")
    is_pour_net: bool = False  # This net is used for copper pours (e.g., GND, VCC)


# =============================================================================
# PREDEFINED NET CLASSES
# =============================================================================

NET_CLASS_POWER = NetClassRouting(
    name="Power",
    priority=1,
    trace_width=0.5,
    clearance=0.2,
    via_size=0.8,
    cost_multiplier=0.8,
    zone_priority=10,  # Fill power zones first
    zone_connection="solid",  # Direct connection for power
    is_pour_net=True,  # Power nets often have pours
)

NET_CLASS_CLOCK = NetClassRouting(
    name="Clock",
    priority=2,
    trace_width=0.2,
    clearance=0.2,
    cost_multiplier=0.9,
    length_critical=True,
)

NET_CLASS_HIGH_SPEED = NetClassRouting(
    name="HighSpeed",
    priority=2,
    trace_width=0.2,
    clearance=0.15,
    cost_multiplier=0.85,
    length_critical=True,
)

NET_CLASS_AUDIO = NetClassRouting(
    name="Audio",
    priority=3,
    trace_width=0.2,
    clearance=0.15,
    cost_multiplier=1.0,
    noise_sensitive=True,
)

NET_CLASS_DIGITAL = NetClassRouting(
    name="Digital",
    priority=4,
    trace_width=0.2,
    clearance=0.15,
    cost_multiplier=1.0,
)

NET_CLASS_DEBUG = NetClassRouting(
    name="Debug",
    priority=5,
    trace_width=0.2,
    clearance=0.15,
    cost_multiplier=1.2,  # Route last, less important
)

NET_CLASS_DEFAULT = NetClassRouting(
    name="Default",
    priority=10,
    trace_width=0.2,
    clearance=0.2,
    cost_multiplier=1.0,
)


def create_net_class_map(
    power_nets: list[str] | None = None,
    clock_nets: list[str] | None = None,
    high_speed_nets: list[str] | None = None,
    audio_nets: list[str] | None = None,
    debug_nets: list[str] | None = None,
) -> dict[str, NetClassRouting]:
    """Create a net class mapping from net name lists.

    Args:
        power_nets: List of power net names (e.g., ["+5V", "+3.3V", "GND"])
        clock_nets: List of clock net names (e.g., ["MCLK", "BCLK"])
        high_speed_nets: List of high-speed signal nets (e.g., ["SPI_CLK"])
        audio_nets: List of audio signal nets (e.g., ["AUDIO_L", "AUDIO_R"])
        debug_nets: List of debug/low-priority nets (e.g., ["SWDIO", "NRST"])

    Returns:
        Dict mapping net names to NetClassRouting objects
    """
    net_class_map: dict[str, NetClassRouting] = {}

    if power_nets:
        for net in power_nets:
            net_class_map[net] = NET_CLASS_POWER

    if clock_nets:
        for net in clock_nets:
            net_class_map[net] = NET_CLASS_CLOCK

    if high_speed_nets:
        for net in high_speed_nets:
            net_class_map[net] = NET_CLASS_HIGH_SPEED

    if audio_nets:
        for net in audio_nets:
            net_class_map[net] = NET_CLASS_AUDIO

    if debug_nets:
        for net in debug_nets:
            net_class_map[net] = NET_CLASS_DEBUG

    return net_class_map


# Default net class map with common net names
DEFAULT_NET_CLASS_MAP: dict[str, NetClassRouting] = create_net_class_map(
    power_nets=["+5V", "+3.3V", "+3.3VA", "+1.8V", "VCC", "VDD", "GND", "GNDA", "PGND"],
    clock_nets=["CLK", "MCLK", "BCLK", "LRCLK", "SCK"],
    audio_nets=["AUDIO_L", "AUDIO_R", "I2S_DIN", "I2S_DOUT"],
    debug_nets=["SWDIO", "SWCLK", "NRST", "TDI", "TDO", "TCK", "TMS"],
)
