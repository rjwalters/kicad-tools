"""
Netclass templates for common design types.

Provides predefined netclass configurations for different PCB design types
(audio, power supply, digital, mixed-signal). Each template includes
appropriate trace widths, clearances, and pattern assignments.
"""

from dataclasses import dataclass, field
from typing import Any

from .project_file import (
    add_netclass_definition,
    add_netclass_patterns,
    get_netclass_definitions,
)


@dataclass
class NetclassTemplate:
    """Template for a single netclass."""

    name: str
    track_width: float
    clearance: float
    via_diameter: float = 0.6
    via_drill: float = 0.3
    pcb_color: str | None = None
    patterns: list[str] = field(default_factory=list)


@dataclass
class DesignTypeTemplate:
    """Template for a complete design type with multiple netclasses."""

    name: str
    description: str
    netclasses: list[NetclassTemplate] = field(default_factory=list)


# =============================================================================
# NETCLASS COLOR PALETTE
# =============================================================================

NETCLASS_COLORS = {
    "Power": "rgba(255, 0, 0, 0.800)",  # Red
    "Ground": "rgba(139, 69, 19, 0.800)",  # Brown
    "Audio": "rgba(0, 128, 0, 0.800)",  # Green
    "Clock": "rgba(255, 165, 0, 0.800)",  # Orange
    "I2S": "rgba(0, 191, 255, 0.800)",  # Deep Sky Blue
    "SPI": "rgba(138, 43, 226, 0.800)",  # Blue Violet
    "I2C": "rgba(255, 20, 147, 0.800)",  # Deep Pink
    "HighSpeed": "rgba(255, 215, 0, 0.800)",  # Gold
    "Debug": "rgba(128, 128, 128, 0.800)",  # Gray
    "Control": "rgba(0, 128, 128, 0.800)",  # Teal
    "HighCurrent": "rgba(178, 34, 34, 0.800)",  # Firebrick
    "Analog": "rgba(46, 139, 87, 0.800)",  # Sea Green
    "RF": "rgba(255, 140, 0, 0.800)",  # Dark Orange
}


# =============================================================================
# DESIGN TYPE TEMPLATES
# =============================================================================

AUDIO_TEMPLATE = DesignTypeTemplate(
    name="audio",
    description="Audio DAC/ADC design with I2S and analog paths",
    netclasses=[
        NetclassTemplate(
            name="Power",
            track_width=0.4,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Power"],
            patterns=["VCC*", "VDD*", "+*V", "V_*", "PWR_*", "VBUS*"],
        ),
        NetclassTemplate(
            name="Ground",
            track_width=0.5,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Ground"],
            patterns=["GND", "GND*", "AGND*", "DGND*", "PGND*", "*_GND"],
        ),
        NetclassTemplate(
            name="Audio",
            track_width=0.3,
            clearance=0.2,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Audio"],
            patterns=["AUDIO_*", "AUD_*", "DAC_*", "ADC_*", "LINE_*", "HP_*", "SPK_*"],
        ),
        NetclassTemplate(
            name="I2S",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["I2S"],
            patterns=["I2S_*", "I2S*", "BCLK*", "LRCLK*", "MCLK*", "SDATA*", "DOUT*", "DIN*"],
        ),
        NetclassTemplate(
            name="Clock",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Clock"],
            patterns=["CLK*", "*_CLK", "OSC*", "XTAL*"],
        ),
        NetclassTemplate(
            name="SPI",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["SPI"],
            patterns=["SPI_*", "MOSI*", "MISO*", "SCK*", "CS_*", "SS_*"],
        ),
        NetclassTemplate(
            name="I2C",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["I2C"],
            patterns=["I2C_*", "SDA*", "SCL*"],
        ),
        NetclassTemplate(
            name="Debug",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Debug"],
            patterns=["SWDIO*", "SWCLK*", "NRST*", "TDI*", "TDO*", "TCK*", "TMS*", "JTAG_*"],
        ),
    ],
)

POWER_SUPPLY_TEMPLATE = DesignTypeTemplate(
    name="power_supply",
    description="Power supply design with high current paths",
    netclasses=[
        NetclassTemplate(
            name="HighCurrent",
            track_width=0.8,
            clearance=0.2,
            via_diameter=1.0,
            via_drill=0.5,
            pcb_color=NETCLASS_COLORS["HighCurrent"],
            patterns=["VIN*", "VOUT*", "SW*", "PHASE*", "BOOST*", "BUCK*"],
        ),
        NetclassTemplate(
            name="Power",
            track_width=0.5,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Power"],
            patterns=["VCC*", "VDD*", "+*V", "V_*", "VBUS*", "VREG*"],
        ),
        NetclassTemplate(
            name="Ground",
            track_width=0.8,
            clearance=0.2,
            via_diameter=1.0,
            via_drill=0.5,
            pcb_color=NETCLASS_COLORS["Ground"],
            patterns=["GND*", "PGND*", "AGND*", "*_GND"],
        ),
        NetclassTemplate(
            name="Control",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Control"],
            patterns=["FB_*", "EN_*", "COMP*", "SS_*", "PGOOD*", "FAULT*"],
        ),
        NetclassTemplate(
            name="Analog",
            track_width=0.25,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Analog"],
            patterns=["SENSE*", "ISENSE*", "VSENSE*", "REF*"],
        ),
    ],
)

DIGITAL_TEMPLATE = DesignTypeTemplate(
    name="digital",
    description="Digital design with high-speed signals",
    netclasses=[
        NetclassTemplate(
            name="Power",
            track_width=0.4,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Power"],
            patterns=["VCC*", "VDD*", "+*V", "V_*", "VCORE*", "VIO*"],
        ),
        NetclassTemplate(
            name="Ground",
            track_width=0.5,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Ground"],
            patterns=["GND*", "DGND*", "*_GND"],
        ),
        NetclassTemplate(
            name="HighSpeed",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["HighSpeed"],
            patterns=["USB_*", "HDMI_*", "ETH_*", "LVDS_*", "DP_*", "DDR_*"],
        ),
        NetclassTemplate(
            name="Clock",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Clock"],
            patterns=["CLK*", "*_CLK", "OSC*", "XTAL*", "REF_CLK*"],
        ),
        NetclassTemplate(
            name="SPI",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["SPI"],
            patterns=["SPI_*", "MOSI*", "MISO*", "SCK*", "CS_*", "QSPI_*"],
        ),
        NetclassTemplate(
            name="I2C",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["I2C"],
            patterns=["I2C_*", "SDA*", "SCL*"],
        ),
        NetclassTemplate(
            name="Debug",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Debug"],
            patterns=[
                "SWDIO*",
                "SWCLK*",
                "NRST*",
                "TDI*",
                "TDO*",
                "TCK*",
                "TMS*",
                "JTAG_*",
                "UART_*",
                "TX*",
                "RX*",
            ],
        ),
    ],
)

MIXED_SIGNAL_TEMPLATE = DesignTypeTemplate(
    name="mixed_signal",
    description="Mixed-signal design with analog and digital domains",
    netclasses=[
        NetclassTemplate(
            name="Power",
            track_width=0.4,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Power"],
            patterns=["VCC*", "VDD*", "+*V", "V_*", "AVDD*", "DVDD*"],
        ),
        NetclassTemplate(
            name="Ground",
            track_width=0.5,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Ground"],
            patterns=["GND", "GND*", "AGND*", "DGND*", "*_GND"],
        ),
        NetclassTemplate(
            name="Analog",
            track_width=0.3,
            clearance=0.2,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Analog"],
            patterns=["AIN*", "AOUT*", "ADC_*", "DAC_*", "VREF*", "SENSE*"],
        ),
        NetclassTemplate(
            name="Clock",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Clock"],
            patterns=["CLK*", "*_CLK", "OSC*", "XTAL*"],
        ),
        NetclassTemplate(
            name="SPI",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["SPI"],
            patterns=["SPI_*", "MOSI*", "MISO*", "SCK*", "CS_*"],
        ),
        NetclassTemplate(
            name="I2C",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["I2C"],
            patterns=["I2C_*", "SDA*", "SCL*"],
        ),
        NetclassTemplate(
            name="Debug",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Debug"],
            patterns=["SWDIO*", "SWCLK*", "NRST*", "TDI*", "TDO*", "TCK*", "TMS*", "JTAG_*"],
        ),
    ],
)

RF_TEMPLATE = DesignTypeTemplate(
    name="rf",
    description="RF design with controlled impedance traces",
    netclasses=[
        NetclassTemplate(
            name="Power",
            track_width=0.4,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Power"],
            patterns=["VCC*", "VDD*", "+*V", "V_*", "VPA*", "VRF*"],
        ),
        NetclassTemplate(
            name="Ground",
            track_width=0.5,
            clearance=0.15,
            via_diameter=0.8,
            via_drill=0.4,
            pcb_color=NETCLASS_COLORS["Ground"],
            patterns=["GND*", "RFGND*", "*_GND"],
        ),
        NetclassTemplate(
            name="RF",
            track_width=0.35,  # Typical 50-ohm trace on FR4
            clearance=0.25,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["RF"],
            patterns=["RF_*", "ANT*", "LNA_*", "PA_*", "MIX_*", "LO_*", "IF_*"],
        ),
        NetclassTemplate(
            name="Clock",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Clock"],
            patterns=["CLK*", "*_CLK", "OSC*", "XTAL*", "TCXO*", "REFCLK*"],
        ),
        NetclassTemplate(
            name="Control",
            track_width=0.2,
            clearance=0.1,
            via_diameter=0.6,
            via_drill=0.3,
            pcb_color=NETCLASS_COLORS["Control"],
            patterns=["SPI_*", "I2C_*", "EN_*", "CTRL_*"],
        ),
    ],
)


# Registry of all available templates
DESIGN_TYPE_TEMPLATES: dict[str, DesignTypeTemplate] = {
    "audio": AUDIO_TEMPLATE,
    "power_supply": POWER_SUPPLY_TEMPLATE,
    "digital": DIGITAL_TEMPLATE,
    "mixed_signal": MIXED_SIGNAL_TEMPLATE,
    "rf": RF_TEMPLATE,
}


def get_available_design_types() -> list[str]:
    """Get list of available design type names."""
    return list(DESIGN_TYPE_TEMPLATES.keys())


def get_design_template(design_type: str) -> DesignTypeTemplate:
    """
    Get a design type template by name.

    Args:
        design_type: Name of the design type

    Returns:
        DesignTypeTemplate for the specified type

    Raises:
        ValueError: If design type is not found
    """
    if design_type not in DESIGN_TYPE_TEMPLATES:
        available = ", ".join(DESIGN_TYPE_TEMPLATES.keys())
        raise ValueError(f"Unknown design type '{design_type}'. Available: {available}")
    return DESIGN_TYPE_TEMPLATES[design_type]


def apply_design_template(
    data: dict[str, Any],
    design_type: str,
    update_default: bool = True,
) -> None:
    """
    Apply a design type template to project data.

    This adds netclass definitions and patterns from the template
    to the project's net_settings section.

    Args:
        data: Project data dictionary
        design_type: Name of the design type template to apply
        update_default: If True, update the Default netclass with reasonable values
    """
    template = get_design_template(design_type)

    # Optionally update Default netclass
    if update_default:
        # Use values from the most generic netclass in the template
        add_netclass_definition(
            data,
            name="Default",
            track_width=0.2,
            clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
        )

    # Add netclasses from template
    for netclass in template.netclasses:
        add_netclass_definition(
            data,
            name=netclass.name,
            track_width=netclass.track_width,
            clearance=netclass.clearance,
            via_diameter=netclass.via_diameter,
            via_drill=netclass.via_drill,
            pcb_color=netclass.pcb_color,
        )

        # Add patterns for this netclass
        if netclass.patterns:
            add_netclass_patterns(data, netclass.name, netclass.patterns)


def get_netclass_summary(data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Get a summary of netclasses defined in project data.

    Args:
        data: Project data dictionary

    Returns:
        List of dictionaries with netclass info (name, track_width, clearance, pattern_count)
    """
    from .project_file import get_netclass_patterns

    classes = get_netclass_definitions(data)
    patterns = get_netclass_patterns(data)

    # Count patterns per netclass
    pattern_counts: dict[str, int] = {}
    for p in patterns:
        nc = p.get("netclass", "")
        pattern_counts[nc] = pattern_counts.get(nc, 0) + 1

    return [
        {
            "name": cls.get("name", "Unknown"),
            "track_width": cls.get("track_width", 0.25),
            "clearance": cls.get("clearance", 0.2),
            "via_diameter": cls.get("via_diameter", 0.6),
            "pattern_count": pattern_counts.get(cls.get("name", ""), 0),
        }
        for cls in classes
    ]
