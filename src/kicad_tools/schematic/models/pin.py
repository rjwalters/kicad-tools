"""
KiCad Pin Model

Represents a symbol pin with position and properties.
"""

from dataclasses import dataclass

from kicad_tools.sexp import SExp


@dataclass
class Pin:
    """Represents a symbol pin with position and properties.

    Pin coordinates in KiCad symbol definitions:
    - (x, y) is the wire CONNECTION point (where wires attach to the pin)
    - angle is the direction the pin graphic extends INTO the symbol body
    - length is how far the pin graphic extends into the symbol

    For example, a resistor's left pin might have:
    - (x, y) = (-2.54, 0) - where wires connect, outside the symbol
    - angle = 0 (extending right, toward the symbol body)
    - length = 2.54 (reaching the symbol body edge)
    """

    name: str
    number: str
    x: float  # Wire connection point relative to symbol center
    y: float
    angle: float  # Direction pin extends INTO symbol (0=right, 90=up, 180=left, 270=down)
    length: float
    pin_type: str = "passive"

    def connection_point(self) -> tuple[float, float]:
        """Get the wire connection point.

        The connection point is where wires attach to the pin.
        In KiCad symbol definitions, this is the (x, y) position.

        Returns:
            (x, y) tuple of the wire connection point in symbol-local coordinates
        """
        return (self.x, self.y)

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
