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

# Interface blocks (organized by protocol type)
from .interface import (
    CANTransceiver,
    DebugHeader,
    I2CPullups,
    USBConnector,
    create_can_transceiver_mcp2551,
    create_can_transceiver_sn65hvd230,
    create_can_transceiver_tja1050,
    create_i2c_pullups,
    create_jtag_header,
    create_swd_header,
    create_tag_connect_header,
    create_usb_micro_b,
    create_usb_type_c,
)

# MCU blocks
from .mcu import (
    BootModeSelector,
    MCUBlock,
    ResetButton,
    create_esp32_boot,
    create_generic_boot,
    create_reset_button,
    create_stm32_boot,
)

# Power blocks
from .power import (
    BarrelJackInput,
    BatteryInput,
    DecouplingCaps,
    LDOBlock,
    USBPowerInput,
    VoltageDivider,
    create_3v3_ldo,
    create_12v_barrel_jack,
    create_lipo_battery,
    create_usb_power,
    create_voltage_divider,
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
    "VoltageDivider",
    "create_3v3_ldo",
    "create_12v_barrel_jack",
    "create_usb_power",
    "create_lipo_battery",
    "create_voltage_divider",
    # Timing
    "OscillatorBlock",
    "CrystalOscillator",
    "create_mclk_oscillator",
    # Interface
    "CANTransceiver",
    "DebugHeader",
    "USBConnector",
    "I2CPullups",
    "create_can_transceiver_mcp2551",
    "create_can_transceiver_sn65hvd230",
    "create_can_transceiver_tja1050",
    "create_swd_header",
    "create_jtag_header",
    "create_tag_connect_header",
    "create_usb_type_c",
    "create_usb_micro_b",
    "create_i2c_pullups",
    # MCU
    "BootModeSelector",
    "MCUBlock",
    "ResetButton",
    "create_esp32_boot",
    "create_generic_boot",
    "create_reset_button",
    "create_stm32_boot",
]
