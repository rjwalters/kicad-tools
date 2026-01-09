"""
KiCad Pin Model

Represents a symbol pin with position and properties.
"""

import math
from dataclasses import dataclass

from kicad_tools.sexp import SExp


@dataclass
class Pin:
    """Represents a symbol pin with position and properties.

    Pin coordinates in KiCad symbol definitions:
    - (x, y) is the pin's BASE position (where it attaches to the symbol body)
    - angle is the direction the pin points (0=right, 90=up, 180=left, 270=down)
    - length is how far the pin extends from the base

    The wire connection point is at the END of the pin, calculated as:
        connection_x = x + length * cos(angle)
        connection_y = y + length * sin(angle)
    """

    name: str
    number: str
    x: float  # Base position relative to symbol center (NOT wire connection point)
    y: float
    angle: float  # Pin direction in degrees (0=right, 90=up, 180=left, 270=down)
    length: float
    pin_type: str = "passive"

    def connection_point(self) -> tuple[float, float]:
        """Get the wire connection point (end of pin).

        The connection point is where wires attach to the pin.
        It's at the END of the pin, not the base.

        Returns:
            (x, y) tuple of the wire connection point in symbol-local coordinates
        """
        # Calculate the end point of the pin based on angle and length
        # KiCad angles: 0=right, 90=up, 180=left, 270=down
        rad = math.radians(self.angle)
        conn_x = self.x + self.length * math.cos(rad)
        conn_y = self.y + self.length * math.sin(rad)
        return (conn_x, conn_y)

    @classmethod
    def from_sexp(cls, node: SExp) -> "Pin":
        """Parse a pin from its S-expression node.

        Expected format:
            (pin TYPE STYLE (at X Y ANGLE) (length L) (name "N" ...) (number "N" ...))
        """
        # First atom after "pin" is the type
        pin_type = node.children[0].value if node.children else "passive"

        # Find (at X Y ANGLE)
        at_node = node.get("at")
        if at_node and len(at_node.children) >= 3:
            x = float(at_node.children[0].value)
            y = float(at_node.children[1].value)
            angle = float(at_node.children[2].value)
        else:
            x, y, angle = 0, 0, 0

        # Find (length L)
        length_node = node.get("length")
        length = float(length_node.children[0].value) if length_node else 2.54

        # Find (name "NAME" ...)
        name_node = node.get("name")
        name = str(name_node.children[0].value) if name_node else ""

        # Find (number "NUM" ...)
        number_node = node.get("number")
        number = str(number_node.children[0].value) if number_node else ""

        return cls(
            name=name, number=number, x=x, y=y, angle=angle, length=length, pin_type=pin_type
        )
