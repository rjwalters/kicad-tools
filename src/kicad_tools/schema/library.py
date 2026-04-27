"""
Symbol library models.

Represents KiCad symbol library definitions with pin geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp, parse_file, parse_string, serialize_sexp

# Valid KiCad pin types
VALID_PIN_TYPES = frozenset(
    {
        "input",
        "output",
        "bidirectional",
        "power_in",
        "power_out",
        "passive",
        "unspecified",
        "tri_state",
        "open_collector",
        "open_emitter",
        "no_connect",
    }
)

# Valid fill types for graphical shapes
VALID_FILL_TYPES = frozenset({"none", "outline", "background"})

# Valid stroke types for graphical shapes
VALID_STROKE_TYPES = frozenset({"default", "dash", "dot", "dash_dot", "dash_dot_dot", "solid"})

# Union type for all graphical shape dataclasses
SymbolGraphic = "SymbolPolyline | SymbolCircle | SymbolArc | SymbolRectangle"


def _validate_fill_type(fill_type: str) -> None:
    """Validate that a fill type is a recognized KiCad value."""
    if fill_type not in VALID_FILL_TYPES:
        raise ValueError(
            f"Invalid fill_type '{fill_type}'. Must be one of: {sorted(VALID_FILL_TYPES)}"
        )


def _validate_stroke_type(stroke_type: str) -> None:
    """Validate that a stroke type is a recognized KiCad value."""
    if stroke_type not in VALID_STROKE_TYPES:
        raise ValueError(
            f"Invalid stroke_type '{stroke_type}'. Must be one of: {sorted(VALID_STROKE_TYPES)}"
        )


def _stroke_sexp(width: float, stroke_type: str) -> SExp:
    """Build a (stroke (width N) (type T)) S-expression node."""
    return SExp.list(
        "stroke",
        SExp.list("width", width),
        SExp.list("type", stroke_type),
    )


def _fill_sexp(fill_type: str) -> SExp:
    """Build a (fill (type T)) S-expression node."""
    return SExp.list("fill", SExp.list("type", fill_type))


@dataclass
class SymbolPolyline:
    """A polyline graphical element in a symbol.

    For closed polygons, repeat the first point as the last point.
    """

    points: list[tuple[float, float]]
    stroke_width: float = 0.0
    stroke_type: str = "default"
    fill_type: str = "none"

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("A polyline requires at least 2 points")
        _validate_fill_type(self.fill_type)
        _validate_stroke_type(self.stroke_type)

    def to_sexp_node(self) -> SExp:
        """Generate ``(polyline (pts ...) (stroke ...) (fill ...))``."""
        pts_children: list[SExp] = []
        for x, y in self.points:
            pts_children.append(SExp.list("xy", x, y))
        pts_node = SExp(name="pts", children=pts_children)

        return SExp(
            name="polyline",
            children=[
                pts_node,
                _stroke_sexp(self.stroke_width, self.stroke_type),
                _fill_sexp(self.fill_type),
            ],
        )

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SymbolPolyline:
        """Parse a ``(polyline ...)`` S-expression node."""
        points: list[tuple[float, float]] = []
        if pts_node := sexp.find("pts"):
            for xy in pts_node.find_all("xy"):
                x = xy.get_float(0) or 0.0
                y = xy.get_float(1) or 0.0
                points.append((x, y))

        stroke_width = 0.0
        stroke_type = "default"
        if stroke := sexp.find("stroke"):
            if w := stroke.find("width"):
                stroke_width = w.get_float(0) or 0.0
            if t := stroke.find("type"):
                stroke_type = t.get_string(0) or "default"

        fill_type = "none"
        if fill := sexp.find("fill"):
            if t := fill.find("type"):
                fill_type = t.get_string(0) or "none"

        return cls(
            points=points,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )


@dataclass
class SymbolCircle:
    """A circle graphical element in a symbol."""

    center: tuple[float, float]
    radius: float
    stroke_width: float = 0.0
    stroke_type: str = "default"
    fill_type: str = "none"

    def __post_init__(self) -> None:
        if self.radius <= 0:
            raise ValueError("Circle radius must be positive")
        _validate_fill_type(self.fill_type)
        _validate_stroke_type(self.stroke_type)

    def to_sexp_node(self) -> SExp:
        """Generate ``(circle (center ...) (radius ...) (stroke ...) (fill ...))``."""
        return SExp(
            name="circle",
            children=[
                SExp.list("center", self.center[0], self.center[1]),
                SExp.list("radius", self.radius),
                _stroke_sexp(self.stroke_width, self.stroke_type),
                _fill_sexp(self.fill_type),
            ],
        )

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SymbolCircle:
        """Parse a ``(circle ...)`` S-expression node."""
        center = (0.0, 0.0)
        if c := sexp.find("center"):
            center = (c.get_float(0) or 0.0, c.get_float(1) or 0.0)

        radius = 0.0
        if r := sexp.find("radius"):
            radius = r.get_float(0) or 0.0

        stroke_width = 0.0
        stroke_type = "default"
        if stroke := sexp.find("stroke"):
            if w := stroke.find("width"):
                stroke_width = w.get_float(0) or 0.0
            if t := stroke.find("type"):
                stroke_type = t.get_string(0) or "default"

        fill_type = "none"
        if fill := sexp.find("fill"):
            if t := fill.find("type"):
                fill_type = t.get_string(0) or "none"

        return cls(
            center=center,
            radius=radius,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )


@dataclass
class SymbolArc:
    """An arc graphical element in a symbol.

    KiCad arcs are defined by start, mid (midpoint on the arc), and end points.
    """

    start: tuple[float, float]
    mid: tuple[float, float]
    end: tuple[float, float]
    stroke_width: float = 0.0
    stroke_type: str = "default"
    fill_type: str = "none"

    def __post_init__(self) -> None:
        _validate_fill_type(self.fill_type)
        _validate_stroke_type(self.stroke_type)

    def to_sexp_node(self) -> SExp:
        """Generate ``(arc (start ...) (mid ...) (end ...) (stroke ...) (fill ...))``."""
        return SExp(
            name="arc",
            children=[
                SExp.list("start", self.start[0], self.start[1]),
                SExp.list("mid", self.mid[0], self.mid[1]),
                SExp.list("end", self.end[0], self.end[1]),
                _stroke_sexp(self.stroke_width, self.stroke_type),
                _fill_sexp(self.fill_type),
            ],
        )

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SymbolArc:
        """Parse an ``(arc ...)`` S-expression node."""
        start = (0.0, 0.0)
        mid = (0.0, 0.0)
        end = (0.0, 0.0)

        if s := sexp.find("start"):
            start = (s.get_float(0) or 0.0, s.get_float(1) or 0.0)
        if m := sexp.find("mid"):
            mid = (m.get_float(0) or 0.0, m.get_float(1) or 0.0)
        if e := sexp.find("end"):
            end = (e.get_float(0) or 0.0, e.get_float(1) or 0.0)

        stroke_width = 0.0
        stroke_type = "default"
        if stroke := sexp.find("stroke"):
            if w := stroke.find("width"):
                stroke_width = w.get_float(0) or 0.0
            if t := stroke.find("type"):
                stroke_type = t.get_string(0) or "default"

        fill_type = "none"
        if fill := sexp.find("fill"):
            if t := fill.find("type"):
                fill_type = t.get_string(0) or "none"

        return cls(
            start=start,
            mid=mid,
            end=end,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )


@dataclass
class SymbolRectangle:
    """A rectangle graphical element in a symbol."""

    start: tuple[float, float]
    end: tuple[float, float]
    stroke_width: float = 0.0
    stroke_type: str = "default"
    fill_type: str = "none"

    def __post_init__(self) -> None:
        _validate_fill_type(self.fill_type)
        _validate_stroke_type(self.stroke_type)

    def to_sexp_node(self) -> SExp:
        """Generate ``(rectangle (start ...) (end ...) (stroke ...) (fill ...))``."""
        return SExp(
            name="rectangle",
            children=[
                SExp.list("start", self.start[0], self.start[1]),
                SExp.list("end", self.end[0], self.end[1]),
                _stroke_sexp(self.stroke_width, self.stroke_type),
                _fill_sexp(self.fill_type),
            ],
        )

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SymbolRectangle:
        """Parse a ``(rectangle ...)`` S-expression node."""
        start = (0.0, 0.0)
        end = (0.0, 0.0)

        if s := sexp.find("start"):
            start = (s.get_float(0) or 0.0, s.get_float(1) or 0.0)
        if e := sexp.find("end"):
            end = (e.get_float(0) or 0.0, e.get_float(1) or 0.0)

        stroke_width = 0.0
        stroke_type = "default"
        if stroke := sexp.find("stroke"):
            if w := stroke.find("width"):
                stroke_width = w.get_float(0) or 0.0
            if t := stroke.find("type"):
                stroke_type = t.get_string(0) or "default"

        fill_type = "none"
        if fill := sexp.find("fill"):
            if t := fill.find("type"):
                fill_type = t.get_string(0) or "none"

        return cls(
            start=start,
            end=end,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )


@dataclass
class LibraryPin:
    """
    A pin definition in a symbol library.

    The pin's connection point in schematic coordinates is calculated as:
    - Start from the pin's (at) position
    - The connection point is at the opposite end of the pin length
    """

    number: str
    name: str
    type: str  # power_in, passive, input, output, bidirectional, etc.
    position: tuple[float, float]  # Position relative to symbol origin
    rotation: float  # Degrees: 0=right, 90=up, 180=left, 270=down
    length: float
    unit: int = 1  # Unit number for multi-unit symbols (1-indexed)
    shape: str = "line"  # Pin shape: line, inverted, clock, etc.

    @property
    def connection_offset(self) -> tuple[float, float]:
        """
        Get the connection point offset from the pin's at position.

        The connection point is where wires attach, at the end of the pin.
        """
        # Pin rotation: 0=pointing right, 90=up, 180=left, 270=down
        # Connection point is at the tip of the pin (opposite from IC body)
        # No offset needed - position is already the connection point
        # The length extends INTO the symbol body
        # Note: angle calculation reserved for future pin offset calculations
        _ = math.radians(self.rotation)  # noqa: F841
        return (0, 0)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> LibraryPin:
        """Parse from S-expression."""
        pin_type = sexp.get_string(0) or "passive"
        _pin_shape = sexp.get_string(1) or "line"  # noqa: F841 - reserved for rendering

        pos = (0.0, 0.0)
        rot = 0.0
        length = 2.54

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        if ln := sexp.find("length"):
            length = ln.get_float(0) or 2.54

        name = ""
        number = ""

        if name_node := sexp.find("name"):
            name = name_node.get_string(0) or ""

        if num_node := sexp.find("number"):
            number = num_node.get_string(0) or ""

        return cls(
            number=number,
            name=name,
            type=pin_type,
            position=pos,
            rotation=rot,
            length=length,
        )

    def to_sexp_node(self) -> SExp:
        """Generate S-expression for this pin.

        Format:
            (pin <type> <shape>
              (at <x> <y> <rotation>)
              (length <length>)
              (name "<name>" (effects (font (size 1.27 1.27))))
              (number "<number>" (effects (font (size 1.27 1.27))))
            )
        """
        # Build effects for name and number
        font_effects = SExp.list("effects", SExp.list("font", SExp.list("size", 1.27, 1.27)))

        children: list[SExp] = [
            SExp(value=self.type),
            SExp(value=self.shape),
            SExp.list("at", self.position[0], self.position[1], self.rotation),
            SExp.list("length", self.length),
            SExp.list("name", self.name, font_effects),
            SExp.list("number", self.number, font_effects),
        ]

        return SExp(name="pin", children=children)


KICAD_GRID = 1.27  # mm -- standard KiCad schematic grid


def _snap_to_kicad_grid(
    value: float,
    grid: float = KICAD_GRID,
    tolerance: float = 0.2,
) -> float:
    """Snap *value* to the nearest multiple of *grid* if within *tolerance*.

    KiCad library pin offsets are exact multiples of 1.27 mm.  After
    rotation by ``sin``/``cos`` the result drifts by a tiny amount (e.g.
    ``3.81 * cos(90deg) = 4.66e-16`` instead of ``0``).  This helper
    snaps to the grid when the residual is small, leaving values that
    are genuinely off-grid untouched.
    """
    nearest = round(value / grid) * grid
    if abs(value - nearest) <= tolerance:
        return nearest
    return value


@dataclass
class LibrarySymbol:
    """
    A symbol definition from a KiCad symbol library.

    Contains the symbol's graphical elements and pin definitions.
    """

    name: str
    properties: dict[str, str] = field(default_factory=dict)
    pins: list[LibraryPin] = field(default_factory=list)
    graphics: list[SymbolPolyline | SymbolCircle | SymbolArc | SymbolRectangle] = field(
        default_factory=list
    )
    units: int = 1
    extends: str | None = None

    @property
    def pin_count(self) -> int:
        return len(self.pins)

    def get_pin(self, number: str) -> LibraryPin | None:
        """Get a pin by number."""
        for pin in self.pins:
            if pin.number == number:
                return pin
        return None

    def get_pins_by_name(self, name: str) -> list[LibraryPin]:
        """Get all pins with a given name (e.g., GND, VCC)."""
        return [p for p in self.pins if p.name == name]

    def get_pin_position(
        self,
        pin_number: str,
        instance_pos: tuple[float, float],
        instance_rot: float = 0,
        mirror: str = "",
        snap_to_grid: bool = True,
    ) -> tuple[float, float] | None:
        """
        Calculate the actual schematic position of a pin.

        Args:
            pin_number: The pin number to locate
            instance_pos: The symbol instance position in schematic
            instance_rot: The symbol instance rotation in degrees
            mirror: Mirror mode ("", "x", "y")
            snap_to_grid: If True, snap rotated pin offsets to the nearest
                1.27mm grid point when within tolerance.  This eliminates
                floating-point drift from trig-based rotation.

        Returns:
            (x, y) position in schematic coordinates, or None if pin not found
        """
        pin = self.get_pin(pin_number)
        if not pin:
            return None

        # Start with pin's local position in library coordinates (Y-up).
        # Mirror and rotation are applied in library coordinate space,
        # then Y is negated to convert to schematic coordinates (Y-down).
        x, y = pin.position

        # Apply mirror (in library coords, Y-up)
        if mirror == "x":
            x = -x
        elif mirror == "y":
            y = -y

        # Apply rotation (in library coords, Y-up)
        if instance_rot != 0:
            angle_rad = math.radians(instance_rot)
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)
            x, y = x * cos_a - y * sin_a, x * sin_a + y * cos_a

        # Convert from library coords (Y-up) to schematic coords (Y-down)
        y = -y

        # Snap rotated offset to nearest 1.27mm grid point when close.
        # Pin offsets in library coordinates are exact multiples of 1.27mm.
        # Axis-aligned rotations (0/90/180/270) should preserve this, but
        # floating-point trig introduces drift (e.g. cos(90deg) ~ 6e-17
        # instead of 0).  Snap each offset coordinate to the nearest
        # multiple of 1.27mm if within 0.2mm -- this eliminates trig drift
        # without affecting genuinely non-grid positions.
        if snap_to_grid:
            x = _snap_to_kicad_grid(x)
            y = _snap_to_kicad_grid(y)

        # Apply translation
        return (instance_pos[0] + x, instance_pos[1] + y)

    def get_all_pin_positions(
        self,
        instance_pos: tuple[float, float],
        instance_rot: float = 0,
        mirror: str = "",
        snap_to_grid: bool = True,
    ) -> dict[str, tuple[float, float]]:
        """
        Get all pin positions for a symbol instance.

        Returns:
            Dict mapping pin number to (x, y) position
        """
        positions = {}
        for pin in self.pins:
            pos = self.get_pin_position(
                pin.number, instance_pos, instance_rot, mirror,
                snap_to_grid=snap_to_grid,
            )
            if pos:
                positions[pin.number] = pos
        return positions

    @classmethod
    def from_sexp(cls, sexp: SExp) -> LibrarySymbol:
        """Parse from S-expression."""
        name = sexp.get_string(0) or ""

        # Parse extends (derived symbol relationship)
        extends: str | None = None
        extends_node = sexp.find("extends")
        if extends_node is not None:
            extends = extends_node.get_string(0) or None

        # Parse properties
        properties = {}
        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0)
            prop_value = prop.get_string(1)
            if prop_name:
                properties[prop_name] = prop_value or ""

        # Parse pins and graphics from unit symbols
        pins: list[LibraryPin] = []
        graphics: list[SymbolPolyline | SymbolCircle | SymbolArc | SymbolRectangle] = []
        for unit_sym in sexp.find_all("symbol"):
            _unit_name = unit_sym.get_string(0) or ""  # noqa: F841
            # Unit symbols have names like "TPA3116D2_1_1"
            # Format: {name}_{unit}_{variant}
            for pin_sexp in unit_sym.find_all("pin"):
                pins.append(LibraryPin.from_sexp(pin_sexp))

            # Parse graphical primitives
            for polyline_sexp in unit_sym.find_all("polyline"):
                graphics.append(SymbolPolyline.from_sexp(polyline_sexp))
            for circle_sexp in unit_sym.find_all("circle"):
                graphics.append(SymbolCircle.from_sexp(circle_sexp))
            for arc_sexp in unit_sym.find_all("arc"):
                graphics.append(SymbolArc.from_sexp(arc_sexp))
            for rect_sexp in unit_sym.find_all("rectangle"):
                graphics.append(SymbolRectangle.from_sexp(rect_sexp))

        return cls(
            name=name,
            properties=properties,
            pins=pins,
            graphics=graphics,
            extends=extends,
        )

    def add_pin(
        self,
        number: str,
        name: str,
        pin_type: str,
        position: tuple[float, float],
        rotation: float = 0,
        length: float = 2.54,
        unit: int = 1,
        shape: str = "line",
    ) -> LibraryPin:
        """Add a pin to the symbol.

        Args:
            number: Pin number (e.g., "1", "2")
            name: Pin name (e.g., "VCC", "GND", "IN")
            pin_type: Pin electrical type (must be in VALID_PIN_TYPES)
            position: (x, y) position relative to symbol origin
            rotation: Pin rotation in degrees (0=right, 90=up, 180=left, 270=down)
            length: Pin length in mm (default 2.54)
            unit: Unit number for multi-unit symbols (1-indexed, default 1)
            shape: Pin shape (default "line")

        Returns:
            The created LibraryPin

        Raises:
            ValueError: If pin_type is not valid
        """
        if pin_type not in VALID_PIN_TYPES:
            raise ValueError(
                f"Invalid pin type '{pin_type}'. Must be one of: {sorted(VALID_PIN_TYPES)}"
            )

        pin = LibraryPin(
            number=number,
            name=name,
            type=pin_type,
            position=position,
            rotation=rotation,
            length=length,
            unit=unit,
            shape=shape,
        )
        self.pins.append(pin)
        return pin

    def add_property(self, name: str, value: str) -> None:
        """Add a property to the symbol.

        Args:
            name: Property name (e.g., "Reference", "Value", "Footprint")
            value: Property value
        """
        self.properties[name] = value

    def set_property(self, name: str, value: str) -> None:
        """Set a property value (alias for add_property).

        Args:
            name: Property name
            value: Property value
        """
        self.properties[name] = value

    # -- Graphical shape methods -----------------------------------------------

    def add_polyline(
        self,
        points: list[tuple[float, float]],
        *,
        stroke_width: float = 0.0,
        stroke_type: str = "default",
        fill_type: str = "none",
    ) -> SymbolPolyline:
        """Add a polyline (open line strip) to the symbol body.

        Args:
            points: Ordered list of (x, y) vertices (minimum 2).
            stroke_width: Line width in mm (0 = KiCad default).
            stroke_type: Stroke style (``default``, ``dash``, ``dot``, ...).
            fill_type: Fill mode (``none``, ``outline``, ``background``).

        Returns:
            The created ``SymbolPolyline``.
        """
        shape = SymbolPolyline(
            points=list(points),
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )
        self.graphics.append(shape)
        return shape

    def add_polygon(
        self,
        points: list[tuple[float, float]],
        *,
        stroke_width: float = 0.0,
        stroke_type: str = "default",
        fill_type: str = "outline",
    ) -> SymbolPolyline:
        """Add a closed polygon to the symbol body.

        If the last point does not equal the first, it is automatically
        appended to close the polygon.

        Args:
            points: Ordered list of (x, y) vertices (minimum 3 unique).
            stroke_width: Line width in mm (0 = KiCad default).
            stroke_type: Stroke style.
            fill_type: Fill mode (defaults to ``outline`` for filled polygon).

        Returns:
            The created ``SymbolPolyline`` (closed).
        """
        if len(points) < 3:
            raise ValueError("A polygon requires at least 3 points")
        pts = list(points)
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return self.add_polyline(
            pts,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )

    def add_circle(
        self,
        center: tuple[float, float],
        radius: float,
        *,
        stroke_width: float = 0.0,
        stroke_type: str = "default",
        fill_type: str = "none",
    ) -> SymbolCircle:
        """Add a circle to the symbol body.

        Args:
            center: (x, y) center coordinate.
            radius: Radius in mm (must be positive).
            stroke_width: Line width in mm.
            stroke_type: Stroke style.
            fill_type: Fill mode.

        Returns:
            The created ``SymbolCircle``.
        """
        shape = SymbolCircle(
            center=center,
            radius=radius,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )
        self.graphics.append(shape)
        return shape

    def add_arc(
        self,
        start: tuple[float, float],
        mid: tuple[float, float],
        end: tuple[float, float],
        *,
        stroke_width: float = 0.0,
        stroke_type: str = "default",
        fill_type: str = "none",
    ) -> SymbolArc:
        """Add an arc to the symbol body.

        Args:
            start: (x, y) start point.
            mid: (x, y) midpoint on the arc.
            end: (x, y) end point.
            stroke_width: Line width in mm.
            stroke_type: Stroke style.
            fill_type: Fill mode.

        Returns:
            The created ``SymbolArc``.
        """
        shape = SymbolArc(
            start=start,
            mid=mid,
            end=end,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )
        self.graphics.append(shape)
        return shape

    def add_rectangle(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        stroke_width: float = 0.0,
        stroke_type: str = "default",
        fill_type: str = "none",
    ) -> SymbolRectangle:
        """Add a rectangle to the symbol body.

        Args:
            start: (x, y) of one corner.
            end: (x, y) of the opposite corner.
            stroke_width: Line width in mm.
            stroke_type: Stroke style.
            fill_type: Fill mode.

        Returns:
            The created ``SymbolRectangle``.
        """
        shape = SymbolRectangle(
            start=start,
            end=end,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
            fill_type=fill_type,
        )
        self.graphics.append(shape)
        return shape

    def get_pins_for_unit(self, unit: int) -> list[LibraryPin]:
        """Get all pins belonging to a specific unit.

        Args:
            unit: Unit number (1-indexed)

        Returns:
            List of pins for that unit
        """
        return [p for p in self.pins if p.unit == unit]

    def to_sexp_node(self) -> SExp:
        """Generate S-expression for this symbol.

        For a standalone symbol the format is::

            (symbol "<name>"
              (property "Reference" "U" (at 0 0 0) (effects ...))
              (property "Value" "<name>" (at 0 0 0) (effects ...))
              ...
              (symbol "<name>_0_1"
                (polyline ...)
                (circle ...)
                ...
              )
              (symbol "<name>_1_1"
                (pin ...)
                (pin ...)
              )
            )

        For a derived symbol (``extends`` is set) the format is::

            (symbol "<name>"
              (extends "<base_name>")
              (property ...)
              ...
            )

        Derived symbols inherit pins and graphics from their base and
        must NOT contain unit sub-symbols.

        The ``_0_1`` sub-symbol holds graphical decoration (body shapes).
        The ``_N_1`` sub-symbols hold pins for each unit.
        """
        children: list[SExp] = [SExp(value=self.name)]

        # Emit extends node for derived symbols
        if self.extends:
            children.append(SExp.list("extends", self.extends))

        # KiCad uses the fully-qualified lib_id (e.g. "Connector_Generic:Conn_01x04")
        # for the top-level symbol name, but the *short* name (after the colon)
        # for the unit sub-symbol names (e.g. "Conn_01x04_0_1").
        short_name = self.name.split(":", 1)[1] if ":" in self.name else self.name

        # Add properties with position and effects
        prop_y_offset = 0.0
        for prop_name, prop_value in self.properties.items():
            # Hide non-essential properties
            hide = prop_name not in ("Reference", "Value")
            effects_children = [SExp.list("font", SExp.list("size", 1.27, 1.27))]
            if hide:
                effects_children.append(SExp.list("hide", "yes"))

            prop_node = SExp.list(
                "property",
                prop_name,
                prop_value,
                SExp.list("at", 0, prop_y_offset, 0),
                SExp(name="effects", children=effects_children),
            )
            children.append(prop_node)
            prop_y_offset += 2.54

        # Derived symbols inherit graphics and pins from the base --
        # they must NOT emit unit sub-symbols.
        if not self.extends:
            # Add _0_1 sub-symbol for graphical shapes (body decoration)
            if self.graphics:
                gfx_name = f"{short_name}_0_1"
                gfx_children: list[SExp] = [SExp(value=gfx_name)]
                for graphic in self.graphics:
                    gfx_children.append(graphic.to_sexp_node())
                children.append(SExp(name="symbol", children=gfx_children))

            # Add unit symbols with their pins
            for unit_idx in range(1, self.units + 1):
                unit_name = f"{short_name}_{unit_idx}_1"
                unit_children: list[SExp] = [SExp(value=unit_name)]

                # Add pins for this unit
                for pin in self.pins:
                    if pin.unit == unit_idx:
                        unit_children.append(pin.to_sexp_node())

                children.append(SExp(name="symbol", children=unit_children))

        return SExp(name="symbol", children=children)


@dataclass
class SymbolLibrary:
    """
    A KiCad symbol library (.kicad_sym file).

    Contains multiple symbol definitions.
    """

    path: str
    symbols: dict[str, LibrarySymbol] = field(default_factory=dict)
    version: str = ""
    generator: str = "kicad_tools"
    _sexp: SExp | None = field(default=None, repr=False)

    def get_symbol(self, name: str) -> LibrarySymbol | None:
        """Get a symbol by name."""
        return self.symbols.get(name)

    def resolve_base(self, symbol: LibrarySymbol) -> LibrarySymbol:
        """Walk the ``extends`` chain and return the root base symbol.

        If *symbol* is not a derived symbol (``extends is None``), it is
        returned unchanged.

        Raises:
            ValueError: If a base symbol in the chain cannot be found in
                this library.
        """
        visited: set[str] = set()
        current = symbol
        while current.extends is not None:
            if current.name in visited:
                raise ValueError(
                    f"Circular extends chain detected at '{current.name}'"
                )
            visited.add(current.name)
            base = self.symbols.get(current.extends)
            if base is None:
                raise ValueError(
                    f"Base symbol '{current.extends}' (extended by "
                    f"'{current.name}') not found in library '{self.path}'"
                )
            current = base
        return current

    def __len__(self) -> int:
        return len(self.symbols)

    def save(self, path: str | None = None) -> None:
        """
        Save the symbol library to a .kicad_sym file.

        Args:
            path: Path to save to. If None, saves to original path.

        Raises:
            ValueError: If no path is provided and no original path exists.
        """
        save_path = path or self.path
        if not save_path:
            raise ValueError("No path specified for save")

        # Generate S-expression
        sexp = self._to_sexp()

        # Serialize and write
        content = serialize_sexp(sexp) + "\n"
        Path(save_path).write_text(content, encoding="utf-8")

    def _to_sexp(self) -> SExp:
        """Convert library to S-expression for serialization."""
        if self._sexp is not None:
            # Round-trip: use original S-expression as base
            return self._sexp

        # For new/modified libraries, use to_sexp_node() which properly
        # serializes all symbols
        return self.to_sexp_node()

    @classmethod
    def create(cls, path: str, version: str | None = None) -> SymbolLibrary:
        """
        Create a new empty symbol library.

        Args:
            path: Path where the library will be saved.
            version: Optional version string (defaults to current date YYYYMMDD).

        Returns:
            A new empty SymbolLibrary instance.

        Example:
            >>> lib = SymbolLibrary.create("my-symbols.kicad_sym")
            >>> lib.save()  # Creates the file
        """
        return cls(
            path=path,
            symbols={},
            version=version or datetime.now().strftime("%Y%m%d"),
            generator="kicad_tools",
            _sexp=None,
        )

    @classmethod
    def load(cls, path: str) -> SymbolLibrary:
        """Load a symbol library from a .kicad_sym file."""
        sexp = parse_file(path)

        if sexp.tag != "kicad_symbol_lib":
            raise ValueError(f"Not a KiCad symbol library: {path}")

        # Extract version and generator
        version = ""
        generator = ""
        if version_node := sexp.find("version"):
            version = version_node.get_string(0) or ""
        if generator_node := sexp.find("generator"):
            generator = generator_node.get_string(0) or ""

        symbols = {}
        for sym_sexp in sexp.find_all("symbol"):
            sym = LibrarySymbol.from_sexp(sym_sexp)
            symbols[sym.name] = sym

        return cls(
            path=path,
            symbols=symbols,
            version=version,
            generator=generator,
            _sexp=sexp,
        )

    @classmethod
    def load_from_string(cls, text: str) -> SymbolLibrary:
        """Load a symbol library from a string (for testing / in-memory use)."""
        sexp = parse_string(text)

        if sexp.tag != "kicad_symbol_lib":
            raise ValueError("Not a KiCad symbol library string")

        version = ""
        generator = ""
        if version_node := sexp.find("version"):
            version = version_node.get_string(0) or ""
        if generator_node := sexp.find("generator"):
            generator = generator_node.get_string(0) or ""

        symbols = {}
        for sym_sexp in sexp.find_all("symbol"):
            sym = LibrarySymbol.from_sexp(sym_sexp)
            symbols[sym.name] = sym

        return cls(
            path="<string>",
            symbols=symbols,
            version=version,
            generator=generator,
            _sexp=sexp,
        )

    def create_symbol(self, name: str, units: int = 1) -> LibrarySymbol:
        """Create a new symbol in the library.

        Args:
            name: Symbol name (e.g., "MyNewPart")
            units: Number of units for multi-unit symbols (default 1)

        Returns:
            The created LibrarySymbol

        Raises:
            ValueError: If a symbol with this name already exists
        """
        if name in self.symbols:
            raise ValueError(f"Symbol '{name}' already exists in library")

        sym = LibrarySymbol(name=name, units=units)
        self.symbols[name] = sym
        return sym

    def to_sexp_node(self) -> SExp:
        """Generate S-expression for the entire library.

        Format:
            (kicad_symbol_lib
              (version 20231120)
              (generator "kicad_tools")
              (generator_version "1.0")
              (symbol ...)
              (symbol ...)
            )
        """
        children: list[SExp] = [
            SExp.list("version", 20231120),
            SExp.list("generator", "kicad_tools"),
            SExp.list("generator_version", "1.0"),
        ]

        # Add all symbols
        for sym in self.symbols.values():
            children.append(sym.to_sexp_node())

        return SExp(name="kicad_symbol_lib", children=children)

    def create_symbol_from_datasheet(
        self,
        name: str,
        pins: Any,
        layout: str = "functional",
        datasheet_url: str = "",
        manufacturer: str = "",
        description: str = "",
        footprint: str = "",
        properties: dict[str, str] | None = None,
        interactive: bool = False,
    ) -> LibrarySymbol:
        """
        Create a symbol from datasheet-extracted pins.

        This is a convenience method that uses the SymbolGenerator to create
        a symbol from extracted pin data and add it to this library.

        Args:
            name: Symbol name (e.g., "STM32F103C8T6")
            pins: PinTable or list of ExtractedPin from datasheet parsing
            layout: Pin layout style ("functional", "physical", "simple")
            datasheet_url: URL to the component datasheet
            manufacturer: Component manufacturer
            description: Component description
            footprint: KiCad footprint reference (e.g., "Package_QFP:LQFP-48")
            properties: Additional properties to set
            interactive: If True, prompt for confirmation (not yet implemented)

        Returns:
            The created LibrarySymbol

        Example:
            >>> from kicad_tools.datasheet import DatasheetParser
            >>> from kicad_tools.schema.library import SymbolLibrary
            >>>
            >>> parser = DatasheetParser("STM32F103.pdf")
            >>> pins = parser.extract_pins(package="LQFP48")
            >>>
            >>> lib = SymbolLibrary.create("myproject.kicad_sym")
            >>> sym = lib.create_symbol_from_datasheet(
            ...     name="STM32F103C8T6",
            ...     pins=pins,
            ...     datasheet_url="https://example.com/stm32f103.pdf",
            ... )
            >>> lib.save()
        """
        from kicad_tools.datasheet.symbol_generator import create_symbol_from_datasheet

        return create_symbol_from_datasheet(
            library=self,
            name=name,
            pins=pins,
            layout=layout,
            datasheet_url=datasheet_url,
            manufacturer=manufacturer,
            description=description,
            footprint=footprint,
            properties=properties,
            interactive=interactive,
        )


class LibraryManager:
    """
    Manages multiple symbol libraries.

    Provides lookup of symbols by lib_id (e.g., "Device:R", "chorus-revA:TPA3116D2").
    """

    def __init__(self):
        self.libraries: dict[str, SymbolLibrary] = {}
        self.search_paths: list[str] = []

    def add_library(self, name: str, library: SymbolLibrary) -> None:
        """Add a library with a given name."""
        self.libraries[name] = library

    def load_library(self, path: str, name: str | None = None) -> SymbolLibrary:
        """Load a library from a file."""
        lib = SymbolLibrary.load(path)
        lib_name = name or Path(path).stem
        self.libraries[lib_name] = lib
        return lib

    def load_embedded(self, schematic: Any) -> None:
        """Load embedded symbol definitions from a schematic.

        KiCad schematics store inline symbol definitions in a ``lib_symbols``
        section.  This method parses those definitions and registers them so
        that :meth:`get_symbol` can resolve pin positions without requiring the
        on-disk library files.

        Args:
            schematic: A :class:`~kicad_tools.schema.schematic.Schematic`
                instance (or any object exposing a ``lib_symbols`` property
                that returns an :class:`SExp` node or ``None``).
        """
        lib_symbols = schematic.lib_symbols
        if lib_symbols is None:
            return

        for sym_sexp in lib_symbols.find_all("symbol"):
            sym = LibrarySymbol.from_sexp(sym_sexp)
            # Embedded symbol names use lib_id format, e.g. "Device:R"
            lib_id = sym.name
            if ":" in lib_id:
                lib_name, short_name = lib_id.split(":", 1)
            else:
                lib_name = lib_id
                short_name = lib_id

            if lib_name not in self.libraries:
                self.libraries[lib_name] = SymbolLibrary(
                    path="<embedded>", symbols={}
                )
            if short_name not in self.libraries[lib_name].symbols:
                self.libraries[lib_name].symbols[short_name] = sym

    def add_search_path(self, path: str) -> None:
        """Add a directory to search for libraries."""
        self.search_paths.append(path)

    def get_symbol(self, lib_id: str) -> LibrarySymbol | None:
        """
        Get a symbol by lib_id.

        Args:
            lib_id: Library ID in format "library:symbol" (e.g., "Device:R")

        Returns:
            The LibrarySymbol if found, None otherwise
        """
        if ":" not in lib_id:
            # Search all libraries
            for lib in self.libraries.values():
                if sym := lib.get_symbol(lib_id):
                    return sym
            return None

        lib_name, sym_name = lib_id.split(":", 1)

        # Check loaded libraries
        if lib_name in self.libraries:
            result = self.libraries[lib_name].get_symbol(sym_name)
            if result is not None:
                return result
            # Symbol not in the already-loaded library (e.g. a partial
            # library built from embedded schematic symbols).  Fall through
            # to search paths so we can load the full on-disk library and
            # merge in the missing symbol.

        # Try to find and load the library
        for search_path in self.search_paths:
            lib_path = Path(search_path) / f"{lib_name}.kicad_sym"
            if lib_path.exists():
                self.load_library(str(lib_path), lib_name)
                return self.libraries[lib_name].get_symbol(sym_name)

        return None

    def get_pin_positions(
        self,
        lib_id: str,
        instance_pos: tuple[float, float],
        instance_rot: float = 0,
        mirror: str = "",
    ) -> dict[str, tuple[float, float]]:
        """
        Get all pin positions for a symbol instance.

        Args:
            lib_id: Library ID (e.g., "chorus-revA:TPA3116D2")
            instance_pos: Symbol position in schematic
            instance_rot: Symbol rotation in degrees
            mirror: Mirror mode

        Returns:
            Dict mapping pin number to (x, y) position
        """
        sym = self.get_symbol(lib_id)
        if not sym:
            return {}
        return sym.get_all_pin_positions(instance_pos, instance_rot, mirror)
