"""Base classes for circuit blocks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance

    from kicad_tools.intent.types import InterfaceCategory


@dataclass
class Port:
    """A connection point on a circuit block.

    Ports represent named electrical connection points with position and
    optional interface metadata for type-checked connections.

    The interface metadata fields are all optional to maintain backward
    compatibility — existing blocks continue to work with position-only ports.

    Attributes:
        name: Port identifier (e.g., "VIN", "D+", "SDA").
        x: X coordinate in schematic units.
        y: Y coordinate in schematic units.
        direction: Signal direction — "input", "output", "bidirectional",
            "passive", or "power".
        interface: High-level interface category (POWER, DIFFERENTIAL, BUS, etc.).
        interface_type: Specific interface variant (e.g., "usb2_high_speed",
            "i2c_fast", "power_3v3").
        parameters: Electrical parameters (e.g., {"voltage_min": 3.0,
            "voltage_max": 3.6}).
        group: Groups related ports (e.g., "usb_data", "spi_bus").
    """

    name: str
    x: float
    y: float
    direction: str = "passive"  # input, output, bidirectional, passive, power
    interface: InterfaceCategory | None = None
    interface_type: str | None = None
    parameters: dict[str, object] | None = None
    group: str | None = None

    def pos(self) -> tuple[float, float]:
        """Get position as tuple."""
        return (self.x, self.y)


class CircuitBlock:
    """
    Base class for reusable circuit blocks.

    A circuit block represents a common subcircuit pattern that can be
    instantiated multiple times in a schematic. Each block:
    - Places its components at specified coordinates
    - Wires internal connections
    - Exposes ports for external connections

    Ports are available in two forms:
    - ``ports``: dict mapping name to (x, y) tuple (backward compatible).
    - ``typed_ports``: dict mapping name to ``Port`` object with full metadata.

    Subclasses should implement their setup logic in __init__, calling
    super().__init__(sch, x, y) first and then setting up components,
    wiring, and ports.
    """

    def __init__(
        self,
        sch: Schematic = None,
        x: float = 0,
        y: float = 0,
    ):
        """
        Initialize base attributes.

        Args:
            sch: Schematic to add components to
            x: X coordinate of block origin
            y: Y coordinate of block origin
        """
        self.schematic: Schematic = sch
        self.x: float = x
        self.y: float = y
        self.ports: dict[str, tuple[float, float]] = {}
        self.typed_ports: dict[str, Port] = {}
        self.components: dict[str, SymbolInstance] = {}

    def port(self, name: str) -> tuple[float, float]:
        """Get a port position by name."""
        if name not in self.ports:
            available = list(self.ports.keys())
            raise KeyError(f"Port '{name}' not found. Available: {available}")
        return self.ports[name]

    def get_typed_port(self, name: str) -> Port:
        """Get a typed port by name.

        Returns the full Port object with interface metadata. Falls back to
        creating a basic Port from the position tuple if no typed port exists.

        Args:
            name: Port name.

        Returns:
            Port object with position and any interface metadata.

        Raises:
            KeyError: If port name not found.
        """
        if name in self.typed_ports:
            return self.typed_ports[name]
        if name in self.ports:
            pos = self.ports[name]
            return Port(name=name, x=pos[0], y=pos[1])
        available = list(self.ports.keys())
        raise KeyError(f"Port '{name}' not found. Available: {available}")
