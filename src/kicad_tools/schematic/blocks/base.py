"""Base classes for circuit blocks."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance


@dataclass
class Port:
    """A connection point on a circuit block."""

    name: str
    x: float
    y: float
    direction: str = "passive"  # input, output, bidirectional, passive, power

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

    Subclasses should implement their setup logic in __init__, calling
    super().__init__() first and then setting up components, wiring, and ports.
    """

    def __init__(self):
        """Initialize base attributes."""
        self.schematic: Schematic = None
        self.x: float = 0
        self.y: float = 0
        self.ports: dict[str, tuple[float, float]] = {}
        self.components: dict[str, SymbolInstance] = {}

    def port(self, name: str) -> tuple[float, float]:
        """Get a port position by name."""
        if name not in self.ports:
            available = list(self.ports.keys())
            raise KeyError(f"Port '{name}' not found. Available: {available}")
        return self.ports[name]
