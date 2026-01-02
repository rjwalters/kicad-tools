"""
KiCad Circuit Blocks

Reusable circuit block abstractions for common schematic patterns.
Each block encapsulates component placement, wiring, and port definitions.

Usage:
    from kicad_tools.schematic.blocks import LDOBlock, LEDIndicator, DecouplingCaps

    # Create an LDO power supply section
    ldo = LDOBlock(
        sch, x=100, y=80,
        ref_prefix="U1",
        input_voltage=5.0,
        output_voltage=3.3,
        input_cap="10uF",
        output_caps=["10uF", "100nF"]
    )

    # Connect to rails
    sch.add_wire(ldo.ports["VIN"], (100, RAIL_5V))
    sch.add_wire(ldo.ports["VOUT"], (100, RAIL_3V3))

    # Add LED indicator
    led = LEDIndicator(sch, x=150, y=80, ref_prefix="D1", label="PWR")
    led.connect_to_rails(RAIL_3V3, RAIL_GND)
"""

# Base classes
from .base import CircuitBlock, Port

# Indicator blocks
from .indicators import LEDIndicator, create_power_led, create_status_led

# Interface blocks
from .interface import (
    DebugHeader,
    I2CPullups,
    USBConnector,
    create_i2c_pullups,
    create_jtag_header,
    create_swd_header,
    create_tag_connect_header,
    create_usb_micro_b,
    create_usb_type_c,
)

# MCU blocks
from .mcu import MCUBlock, ResetButton, create_reset_button

# Power blocks
from .power import (
    BarrelJackInput,
    BatteryInput,
    DecouplingCaps,
    LDOBlock,
    USBPowerInput,
    create_12v_barrel_jack,
    create_3v3_ldo,
    create_lipo_battery,
    create_usb_power,
)

# Timing blocks
from .timing import (
    CrystalOscillator,
    OscillatorBlock,
    create_mclk_oscillator,
)

__all__ = [
    # Base classes
    "Port",
    "CircuitBlock",
    # Indicators
    "LEDIndicator",
    "create_power_led",
    "create_status_led",
    # Power
    "DecouplingCaps",
    "LDOBlock",
    "BarrelJackInput",
    "USBPowerInput",
    "BatteryInput",
    "create_3v3_ldo",
    "create_12v_barrel_jack",
    "create_usb_power",
    "create_lipo_battery",
    # Timing
    "OscillatorBlock",
    "CrystalOscillator",
    "create_mclk_oscillator",
    # Interface
    "DebugHeader",
    "USBConnector",
    "I2CPullups",
    "create_swd_header",
    "create_jtag_header",
    "create_tag_connect_header",
    "create_usb_type_c",
    "create_usb_micro_b",
    "create_i2c_pullups",
    # MCU
    "MCUBlock",
    "ResetButton",
    "create_reset_button",
]
