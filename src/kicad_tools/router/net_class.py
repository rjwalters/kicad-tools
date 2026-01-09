"""
Net class auto-detection from schematic symbols.

This module provides automatic net classification based on:
1. Symbol library properties (lib_id patterns)
2. Pin function analysis (electrical types)
3. Enhanced net name pattern matching
4. Signal path analysis (component connectivity)

The auto-detection reduces manual configuration by intelligently
classifying nets as power, clock, high-speed, analog, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schematic.models import Pin, Schematic, SymbolInstance

    from .rules import NetClassRouting


class NetClass(Enum):
    """Net classification types for automatic routing configuration.

    Each net class has different routing requirements:
    - POWER: Wide traces, solid zone connections, prefer inner layers
    - GROUND: Similar to power, highest zone priority
    - CLOCK: Length-critical, controlled impedance, avoid noise coupling
    - HIGH_SPEED: Impedance-controlled, length matching, inner layers preferred
    - DIFFERENTIAL: Must be routed as pairs, tight length matching
    - ANALOG: Noise-sensitive, avoid crossing digital signals
    - RF: Impedance-controlled, short traces, proper termination
    - DEBUG: Low priority, can route last
    - SIGNAL: Default digital signals
    """

    POWER = "power"
    GROUND = "ground"
    CLOCK = "clock"
    HIGH_SPEED = "high_speed"
    DIFFERENTIAL = "differential"
    ANALOG = "analog"
    RF = "rf"
    DEBUG = "debug"
    SIGNAL = "signal"  # Default


# =============================================================================
# SYMBOL LIBRARY INDICATORS
# =============================================================================

# Maps lib_id patterns to net classes
# Pattern can use * for wildcards
SYMBOL_INDICATORS: dict[str, NetClass] = {
    # Power symbols and regulators
    "power:*": NetClass.POWER,
    "Device:Ferrite*": NetClass.POWER,
    "Regulator_Linear:*": NetClass.POWER,
    "Regulator_Switching:*": NetClass.POWER,
    # Clock and oscillator components
    "Device:Crystal*": NetClass.CLOCK,
    "Device:Resonator*": NetClass.CLOCK,
    "Oscillator:*": NetClass.CLOCK,
    "Timer:*": NetClass.CLOCK,
    # High-speed interfaces
    "Connector:USB*": NetClass.HIGH_SPEED,
    "Connector_USB:*": NetClass.HIGH_SPEED,
    "Interface:FT232*": NetClass.HIGH_SPEED,
    "Interface:FT2232*": NetClass.HIGH_SPEED,
    "Interface:CH340*": NetClass.HIGH_SPEED,
    "Interface:CP210*": NetClass.HIGH_SPEED,
    "Interface_USB:*": NetClass.HIGH_SPEED,
    "Interface_Ethernet:*": NetClass.HIGH_SPEED,
    "Interface_HDMI:*": NetClass.HIGH_SPEED,
    "Memory_Flash:*": NetClass.HIGH_SPEED,
    "Memory_RAM:*": NetClass.HIGH_SPEED,
    # RF components
    "RF_Module:*": NetClass.RF,
    "RF_Amplifier:*": NetClass.RF,
    "RF_Filter:*": NetClass.RF,
    "RF_Mixer:*": NetClass.RF,
    "RF_Switch:*": NetClass.RF,
    # Analog components
    "Amplifier_Operational:*": NetClass.ANALOG,
    "Amplifier_Audio:*": NetClass.ANALOG,
    "Amplifier_Instrumentation:*": NetClass.ANALOG,
    "Reference_Voltage:*": NetClass.ANALOG,
    "Sensor:*": NetClass.ANALOG,
    "Sensor_Temperature:*": NetClass.ANALOG,
    "Sensor_Pressure:*": NetClass.ANALOG,
    "Sensor_Humidity:*": NetClass.ANALOG,
    "Analog_ADC:*": NetClass.ANALOG,
    "Analog_DAC:*": NetClass.ANALOG,
    "Analog:*": NetClass.ANALOG,
    "Audio:*": NetClass.ANALOG,
    # Debug interfaces
    "Connector:Conn_ARM_JTAG*": NetClass.DEBUG,
    "Connector:Conn_ARM_SWD*": NetClass.DEBUG,
    "Connector_Debug:*": NetClass.DEBUG,
}


# =============================================================================
# NET NAME PATTERNS
# =============================================================================

# Comprehensive patterns for net name classification
NET_CLASS_PATTERNS: dict[NetClass, list[str]] = {
    NetClass.POWER: [
        r"^(VCC|VDD|VBUS|VIN|VOUT|PWR|POWER|AVDD|DVDD)",
        r"^[+-]?\d+\.?\d*V[ADPS]?$",  # +3.3V, -5V, 1.8VA, 3.3VD
        r"^(PVDD|PVCC|VBAT|VCORE|VCAP|VIO)$",
        r"_VCC$|_VDD$|_PWR$",
        r"^V\d+",  # V5, V3.3, etc.
    ],
    NetClass.GROUND: [
        r"^(GND|VSS|GNDA|GNDD|AGND|DGND|PGND|SGND|GROUND|CGND)$",
        r"^(CHASSIS|EARTH|SHIELD)$",
        r"_GND$|_VSS$|_AGND$|_DGND$",
    ],
    NetClass.CLOCK: [
        r"(CLK|CLOCK|MCLK|SCLK|PCLK|BCLK|LRCLK|FCLK|SYSCLK)",
        r"(OSC|XTAL|CRYSTAL|XIN|XOUT)",
        r"_CLK$|_SCK$",
        r"^(TCK|TCLK|JTCK)$",  # JTAG clock
    ],
    NetClass.HIGH_SPEED: [
        r"(USB|ETH|HDMI|LVDS|PCIE|SDIO|QSPI|OSPI)",
        r"(MIPI|DSI|CSI|RGMII|RMII|MII)",
        r"(SATA|SAS|DP|DISPLAYPORT)",
        r"[_-](DP|DM|D[+-])$",  # USB D+/D-
        r"(SD_D|SDIO_D|MMC_D)\d",  # SD/MMC data lines
        r"(QSPI_D|OSPI_D)\d",  # Quad SPI data
    ],
    NetClass.DIFFERENTIAL: [
        r"[_-]?[PN]$",  # _P, _N suffixes
        r"[+-]$",  # +/- suffixes
        r"_DIFF[PN]?$",
        r"(TX|RX)[PN]$",  # Differential TX/RX pairs
        r"(CLK|DATA)[PN]$",  # Differential clock/data
    ],
    NetClass.ANALOG: [
        r"(AIN|AOUT|SENSE|FB|COMP|ISET)",
        r"^VREF",  # Reference voltage
        r"^AN\d+",  # AN0, AN1, etc.
        r"(ADC|DAC)_?(CH)?\d*$",
        r"(AUDIO|MIC|SPK|LINE)(_[LRIO])?",
        r"(I2S|TDM|PDM)_(DIN|DOUT|SD|WS)",
    ],
    NetClass.RF: [
        r"(RF_|ANT_|ANTENNA)",
        r"(LNA|PA)_",  # Low noise amp, power amp
        r"^(RF|ANT|ANTENNA)\d*$",
        r"(TX_RF|RX_RF)",
    ],
    NetClass.DEBUG: [
        r"(SWDIO|SWCLK|SWDCLK|SWO)",
        r"(NRST|RESET|RST)",
        r"(TDI|TDO|TMS|TCK|TRST)",  # JTAG
        r"(DEBUG|DBG|TRACE)",
        r"(BOOT|PROG)",
    ],
}


# =============================================================================
# CLASSIFICATION FUNCTIONS
# =============================================================================


def _match_pattern(lib_id: str, pattern: str) -> bool:
    """Check if lib_id matches a pattern with wildcards."""
    # Convert glob-style pattern to regex
    regex = pattern.replace("*", ".*").replace("?", ".")
    return bool(re.match(regex, lib_id, re.IGNORECASE))


def classify_from_symbol(lib_id: str) -> NetClass | None:
    """Classify net based on connected symbol's library ID.

    Args:
        lib_id: Library ID of the symbol (e.g., "Audio:PCM5122PW")

    Returns:
        NetClass if symbol indicates a specific type, None otherwise
    """
    for pattern, net_class in SYMBOL_INDICATORS.items():
        if _match_pattern(lib_id, pattern):
            return net_class
    return None


def classify_from_pin_type(pin_types: set[str]) -> NetClass | None:
    """Classify net based on connected pin electrical types.

    Args:
        pin_types: Set of pin types connected to this net
                   (e.g., {"power_in", "passive"})

    Returns:
        NetClass based on pin type analysis
    """
    # Power pins indicate power net
    if "power_in" in pin_types or "power_out" in pin_types:
        return NetClass.POWER

    # If all pins are passive (resistors, capacitors), don't classify
    # as this could be any signal type
    if pin_types == {"passive"}:
        return None

    return None


def classify_from_name(net_name: str) -> NetClass | None:
    """Classify net based on name pattern matching.

    Uses comprehensive regex patterns to identify net class from common
    naming conventions. Patterns are checked in priority order to handle
    ambiguous cases (e.g., HDMI_CLK is HIGH_SPEED, not just CLOCK).

    Priority order (most specific first):
    1. GROUND - Very specific patterns
    2. HIGH_SPEED - Interface-specific signals
    3. RF - RF-specific signals
    4. DEBUG - Debug interface signals
    5. CLOCK - Clock signals
    6. POWER - Power supply signals
    7. ANALOG - Analog signals
    8. DIFFERENTIAL - Differential pair indicators

    Args:
        net_name: Name of the net (e.g., "+3.3V", "USB_DP", "GND")

    Returns:
        NetClass if name matches known patterns, None otherwise
    """
    name_upper = net_name.upper()

    # Check patterns in priority order (most specific first)
    check_order = [
        NetClass.GROUND,  # Very specific, check first
        NetClass.HIGH_SPEED,  # Interface names often contain CLK
        NetClass.RF,  # RF-specific
        NetClass.DEBUG,  # Debug interfaces
        NetClass.CLOCK,  # Clock signals
        NetClass.POWER,  # Power supplies
        NetClass.ANALOG,  # Analog signals
        NetClass.DIFFERENTIAL,  # Differential indicators (checked last)
    ]

    for net_class in check_order:
        patterns = NET_CLASS_PATTERNS.get(net_class, [])
        for pattern in patterns:
            if re.search(pattern, name_upper, re.IGNORECASE):
                return net_class

    return None


def is_differential_pair_name(net_name: str) -> bool:
    """Check if net name suggests it's part of a differential pair.

    Args:
        net_name: Name of the net

    Returns:
        True if name pattern suggests differential pair
    """
    name_upper = net_name.upper()
    diff_patterns = [
        r"[_-]?P$",
        r"[_-]?N$",
        r"\+$",
        r"-$",
        r"_DIFF[PN]?$",
        r"(TX|RX)[PN]$",
    ]
    return any(re.search(p, name_upper) for p in diff_patterns)


def find_differential_partner(net_name: str) -> str | None:
    """Find the expected partner name for a differential pair.

    Args:
        net_name: Name of one net in a differential pair

    Returns:
        Expected name of the partner net, or None if not identifiable
    """
    # Common suffixes and their partners (order matters - check longer first)
    suffix_pairs = [
        ("_DP", "_DM"),
        ("_DM", "_DP"),
        ("_DIFFP", "_DIFFN"),
        ("_DIFFN", "_DIFFP"),
        ("_P", "_N"),
        ("_N", "_P"),
        ("+", "-"),
        ("-", "+"),
        ("P", "N"),
        ("N", "P"),
    ]

    name_upper = net_name.upper()
    for suffix, partner_suffix in suffix_pairs:
        if name_upper.endswith(suffix.upper()):
            # Preserve original case for prefix
            prefix_len = len(net_name) - len(suffix)
            prefix = net_name[:prefix_len]
            # Match case of suffix if possible
            if net_name.endswith(suffix):
                return prefix + partner_suffix
            elif net_name.endswith(suffix.lower()):
                return prefix + partner_suffix.lower()
            else:
                # Mixed case - use upper suffix convention
                return prefix + partner_suffix

    return None


@dataclass
class NetClassification:
    """Result of net classification with confidence and source info."""

    net_class: NetClass
    confidence: float  # 0.0 to 1.0
    source: str  # "symbol", "pin_type", "name_pattern", "signal_path"
    details: str = ""  # Additional info about classification

    def __repr__(self) -> str:
        return f"NetClassification({self.net_class.value}, {self.confidence:.0%}, {self.source})"


def classify_net(
    net_name: str,
    connected_pins: list[tuple[str, Pin]] | None = None,
    connected_symbols: list[SymbolInstance] | None = None,
) -> NetClassification:
    """Classify a net using all available information sources.

    Classification priority (highest confidence first):
    1. Pin electrical type (power_in/power_out indicates power net)
    2. Symbol library ID (specific component types)
    3. Net name pattern matching
    4. Default to SIGNAL

    Args:
        net_name: Name of the net
        connected_pins: List of (symbol_ref, Pin) tuples for pins on this net
        connected_symbols: List of SymbolInstance objects connected to this net

    Returns:
        NetClassification with class, confidence, and source
    """
    # 1. Check pin electrical types (highest confidence for power)
    if connected_pins:
        pin_types = {pin.pin_type for _, pin in connected_pins}

        # Power pins are definitive
        if "power_in" in pin_types or "power_out" in pin_types:
            # Determine if it's power or ground
            name_lower = net_name.lower()
            if any(g in name_lower for g in ["gnd", "vss", "ground", "agnd", "dgnd"]):
                return NetClassification(
                    net_class=NetClass.GROUND,
                    confidence=0.95,
                    source="pin_type",
                    details="Power pin connected to ground-named net",
                )
            return NetClassification(
                net_class=NetClass.POWER,
                confidence=0.95,
                source="pin_type",
                details=f"Pin types: {pin_types}",
            )

    # 2. Check symbol library IDs
    if connected_symbols:
        for symbol in connected_symbols:
            lib_id = symbol.symbol_def.lib_id
            net_class = classify_from_symbol(lib_id)
            if net_class:
                return NetClassification(
                    net_class=net_class,
                    confidence=0.85,
                    source="symbol",
                    details=f"Symbol: {lib_id}",
                )

    # 3. Check net name patterns
    net_class = classify_from_name(net_name)
    if net_class:
        # Higher confidence for ground (very specific patterns)
        confidence = 0.80 if net_class == NetClass.GROUND else 0.70
        return NetClassification(
            net_class=net_class,
            confidence=confidence,
            source="name_pattern",
            details=f"Matched pattern for {net_class.value}",
        )

    # 4. Check for differential pair naming
    if is_differential_pair_name(net_name):
        return NetClassification(
            net_class=NetClass.DIFFERENTIAL,
            confidence=0.65,
            source="name_pattern",
            details="Differential pair naming convention",
        )

    # Default to SIGNAL
    return NetClassification(
        net_class=NetClass.SIGNAL,
        confidence=0.50,
        source="default",
        details="No specific classification matched",
    )


# =============================================================================
# ORCHESTRATION FUNCTIONS
# =============================================================================


def auto_classify_nets(
    net_names: dict[int, str],
    schematic: Schematic | None = None,
    min_confidence: float = 0.5,
) -> dict[int, NetClassification]:
    """Automatically classify all nets in a design.

    Args:
        net_names: Mapping of net ID to net name
        schematic: Optional Schematic for symbol/pin analysis
        min_confidence: Minimum confidence threshold (default 0.5)

    Returns:
        Dict mapping net ID to NetClassification
    """
    classifications: dict[int, NetClassification] = {}

    for net_id, net_name in net_names.items():
        # Get connected pins and symbols if schematic available
        connected_pins = None
        connected_symbols = None

        if schematic:
            # Find symbols/pins connected to this net
            # Note: This requires schematic net extraction which may need
            # implementation depending on the schematic model capabilities
            pass

        classification = classify_net(
            net_name=net_name,
            connected_pins=connected_pins,
            connected_symbols=connected_symbols,
        )

        if classification.confidence >= min_confidence:
            classifications[net_id] = classification

    return classifications


def apply_net_class_rules(
    classifications: dict[int, NetClassification],
    net_names: dict[int, str],
) -> dict[str, NetClassRouting]:
    """Apply routing rules based on net classifications.

    Creates NetClassRouting objects with appropriate parameters for
    each net class type.

    Args:
        classifications: Net ID to NetClassification mapping
        net_names: Net ID to name mapping

    Returns:
        Dict mapping net name to NetClassRouting object
    """
    from .rules import (
        NET_CLASS_AUDIO,
        NET_CLASS_CLOCK,
        NET_CLASS_DEBUG,
        NET_CLASS_DIGITAL,
        NET_CLASS_HIGH_SPEED,
        NET_CLASS_POWER,
        NetClassRouting,
    )

    # Map NetClass enum to predefined routing configs
    class_to_routing: dict[NetClass, NetClassRouting] = {
        NetClass.POWER: NET_CLASS_POWER,
        NetClass.GROUND: NetClassRouting(
            name="Ground",
            priority=1,
            trace_width=0.5,
            clearance=0.2,
            via_size=0.8,
            cost_multiplier=0.7,  # Prefer ground routing
            zone_priority=20,  # Highest zone priority
            zone_connection="solid",
            is_pour_net=True,
        ),
        NetClass.CLOCK: NET_CLASS_CLOCK,
        NetClass.HIGH_SPEED: NET_CLASS_HIGH_SPEED,
        NetClass.DIFFERENTIAL: NetClassRouting(
            name="Differential",
            priority=2,
            trace_width=0.15,
            clearance=0.15,
            cost_multiplier=0.85,
            length_critical=True,
        ),
        NetClass.ANALOG: NET_CLASS_AUDIO,
        NetClass.RF: NetClassRouting(
            name="RF",
            priority=2,
            trace_width=0.2,
            clearance=0.2,
            cost_multiplier=0.9,
            length_critical=True,
            noise_sensitive=True,
        ),
        NetClass.DEBUG: NET_CLASS_DEBUG,
        NetClass.SIGNAL: NET_CLASS_DIGITAL,
    }

    net_rules: dict[str, NetClassRouting] = {}

    for net_id, classification in classifications.items():
        if net_id in net_names:
            net_name = net_names[net_id]
            routing = class_to_routing.get(classification.net_class, NET_CLASS_DIGITAL)
            net_rules[net_name] = routing

    return net_rules


def classify_and_apply_rules(
    net_names: dict[int, str],
    schematic: Schematic | None = None,
    min_confidence: float = 0.5,
) -> dict[str, NetClassRouting]:
    """Convenience function to classify nets and apply routing rules.

    Combines auto_classify_nets() and apply_net_class_rules() into a
    single call for simpler integration.

    Args:
        net_names: Mapping of net ID to net name
        schematic: Optional Schematic for enhanced classification
        min_confidence: Minimum confidence threshold

    Returns:
        Dict mapping net name to NetClassRouting object
    """
    classifications = auto_classify_nets(net_names, schematic, min_confidence)
    return apply_net_class_rules(classifications, net_names)
