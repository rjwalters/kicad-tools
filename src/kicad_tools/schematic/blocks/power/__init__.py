"""
Power-related circuit blocks for KiCad schematics.

This package provides reusable circuit blocks for power supply design:

- **Regulators**: LDO and buck converter blocks
- **Inputs**: Barrel jack, USB, and battery input circuits
- **Passives**: Decoupling capacitors and voltage dividers

Usage:
    from kicad_tools.schematic.blocks import LDOBlock, BuckConverter, DecouplingCaps

    # Or import from power subpackage directly
    from kicad_tools.schematic.blocks.power import create_3v3_ldo
"""

# Regulators
# Power inputs
from .inputs import (
    BarrelJackInput,
    BatteryInput,
    USBPowerInput,
    create_12v_barrel_jack,
    create_lipo_battery,
    create_usb_power,
)

# Passive components
from .passives import (
    DecouplingCaps,
    VoltageDivider,
    create_voltage_divider,
)
from .regulators import (
    BuckConverter,
    LDOBlock,
    create_3v3_buck,
    create_3v3_ldo,
    create_5v_buck,
    create_12v_buck,
)

__all__ = [
    # Regulators
    "LDOBlock",
    "BuckConverter",
    "create_3v3_ldo",
    "create_5v_buck",
    "create_3v3_buck",
    "create_12v_buck",
    # Inputs
    "BarrelJackInput",
    "USBPowerInput",
    "BatteryInput",
    "create_12v_barrel_jack",
    "create_usb_power",
    "create_lipo_battery",
    # Passives
    "DecouplingCaps",
    "VoltageDivider",
    "create_voltage_divider",
]
