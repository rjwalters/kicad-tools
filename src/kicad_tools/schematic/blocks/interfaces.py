"""Typed port subclasses for interface-aware circuit connections.

Provides specialized Port types that carry electrical and protocol metadata,
enabling type-checked connections between circuit blocks.

Example::

    from kicad_tools.schematic.blocks.interfaces import PowerPort, DataPort
    from kicad_tools.intent.types import InterfaceCategory

    # Power port with voltage range
    vout = PowerPort(
        name="VOUT", x=100, y=50,
        direction="output",
        voltage_min=3.135, voltage_max=3.465,
    )

    # USB data port
    dp = DataPort(
        name="D+", x=120, y=60,
        direction="bidirectional",
        protocol="usb2",
        signal_role="d+",
    )
"""

from __future__ import annotations

from dataclasses import dataclass

from kicad_tools.intent.types import InterfaceCategory

from .base import Port


@dataclass
class PowerPort(Port):
    """Port carrying power with voltage and current ratings.

    Attributes:
        voltage_min: Minimum voltage in volts (e.g., 3.135 for 3.3V -5%).
        voltage_max: Maximum voltage in volts (e.g., 3.465 for 3.3V +5%).
        max_current: Maximum current in amps.
    """

    voltage_min: float | None = None
    voltage_max: float | None = None
    max_current: float | None = None

    def __post_init__(self) -> None:
        if self.interface is None:
            self.interface = InterfaceCategory.POWER
        if self.direction == "passive":
            self.direction = "power"

    def voltage_overlaps(self, other: PowerPort) -> bool:
        """Check if voltage ranges overlap with another power port.

        Returns True if ranges overlap or if either port has no voltage info.
        """
        if (
            self.voltage_min is None
            or self.voltage_max is None
            or other.voltage_min is None
            or other.voltage_max is None
        ):
            return True
        return self.voltage_min <= other.voltage_max and other.voltage_min <= self.voltage_max


@dataclass
class DataPort(Port):
    """Port carrying a data signal with protocol information.

    Attributes:
        protocol: Communication protocol (e.g., "usb2", "spi", "i2c", "uart").
        signal_role: Role within the protocol (e.g., "clock", "data",
            "chip_select", "d+", "d-", "sda", "scl").
    """

    protocol: str | None = None
    signal_role: str | None = None

    def __post_init__(self) -> None:
        if self.interface is None and self.protocol:
            self.interface = _protocol_to_category(self.protocol)
        if self.interface_type is None and self.protocol:
            self.interface_type = self.protocol


def _protocol_to_category(protocol: str) -> InterfaceCategory:
    """Map protocol name to InterfaceCategory."""
    differential = {"usb2", "usb3", "lvds", "ethernet"}
    bus = {"spi", "i2c", "parallel", "can"}
    if protocol.lower() in differential:
        return InterfaceCategory.DIFFERENTIAL
    if protocol.lower() in bus:
        return InterfaceCategory.BUS
    return InterfaceCategory.SINGLE_ENDED
