"""
Interface circuit blocks for various communication protocols.

This package provides circuit blocks for common hardware interfaces:
- Debug headers (SWD, JTAG, Tag-Connect)
- USB connectors (Type-C, Micro-B, Mini-B, Type-A)
- I2C bus components (pull-ups, filtering)
- CAN bus transceivers (with termination and ESD protection)

Example:
    from kicad_tools.schematic.blocks import DebugHeader, USBConnector

    # Create an SWD debug header
    swd = DebugHeader(sch, x=250, y=50, interface="swd", pins=10)

    # Create a USB Type-C connector with ESD protection
    usb = USBConnector(sch, x=50, y=100, connector_type="type-c")
"""

# Debug interfaces
# CAN interfaces
from .can import (
    CANTransceiver,
    create_can_transceiver_mcp2551,
    create_can_transceiver_sn65hvd230,
    create_can_transceiver_tja1050,
)
from .debug import (
    DebugHeader,
    create_jtag_header,
    create_swd_header,
    create_tag_connect_header,
)

# I2C interfaces
from .i2c import (
    I2CPullups,
    create_i2c_pullups,
)

# USB interfaces
from .usb import (
    USBConnector,
    create_usb_micro_b,
    create_usb_type_c,
)

__all__ = [
    # Debug
    "DebugHeader",
    "create_swd_header",
    "create_jtag_header",
    "create_tag_connect_header",
    # USB
    "USBConnector",
    "create_usb_type_c",
    "create_usb_micro_b",
    # I2C
    "I2CPullups",
    "create_i2c_pullups",
    # CAN
    "CANTransceiver",
    "create_can_transceiver_mcp2551",
    "create_can_transceiver_sn65hvd230",
    "create_can_transceiver_tja1050",
]
