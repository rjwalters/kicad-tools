"""
Symbol instance model.

Represents a component instance placed in a schematic.
"""

from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass, field

from kicad_tools.sexp import SExp


@dataclass
class SymbolPin:
    """A pin on a symbol instance."""

    number: str
    uuid: str
    name: str | None = None

    def to_sexp(self) -> SExp:
        """Convert to S-expression.

        Format: ``(pin "1" (uuid "..."))``
        """
        pin_uuid = self.uuid or str(uuid_mod.uuid4())
        return SExp.list("pin", self.number, SExp.list("uuid", pin_uuid))

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SymbolPin:
        """Parse from S-expression."""
        number = sexp.get_string(0) or ""
        uuid = ""
        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""
        return cls(number=number, uuid=uuid)


@dataclass
class SymbolProperty:
    """A property on a symbol (Reference, Value, Footprint, etc.)."""

    name: str
    value: str
    position: tuple[float, float] = (0, 0)
    rotation: float = 0
    visible: bool = True

    def to_sexp(self) -> SExp:
        """Convert to S-expression.

        Format::

            (property "Reference" "R1" (at X Y 0)
              (effects (font (size 1.27 1.27))))

        Hidden properties include ``(hide yes)`` in the effects block.
        """
        effects_children: list[SExp] = [SExp.list("font", SExp.list("size", 1.27, 1.27))]
        if not self.visible:
            effects_children.append(SExp.list("hide", "yes"))

        return SExp.list(
            "property",
            self.name,
            self.value,
            SExp.list("at", self.position[0], self.position[1], self.rotation),
            SExp(name="effects", children=effects_children),
        )

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SymbolProperty:
        """Parse from S-expression."""
        name = sexp.get_string(0) or ""
        value = sexp.get_string(1) or ""
        pos = (0.0, 0.0)
        rot = 0.0

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        # Check visibility in effects
        visible = True
        if (effects := sexp.find("effects")) and effects.find("hide"):
            visible = False

        return cls(name=name, value=value, position=pos, rotation=rot, visible=visible)


@dataclass
class SymbolInstance:
    """
    A symbol instance placed in a schematic.

    This represents a component like a resistor, capacitor, or IC
    that has been placed at a specific location.
    """

    lib_id: str
    uuid: str
    position: tuple[float, float] = (0, 0)
    rotation: float = 0
    mirror: str = ""  # "", "x", "y"
    unit: int = 1
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False
    properties: dict[str, SymbolProperty] = field(default_factory=dict)
    pins: list[SymbolPin] = field(default_factory=list)
    _sexp: SExp | None = field(default=None, repr=False)

    @property
    def reference(self) -> str:
        """Get the reference designator (e.g., 'R1', 'U1')."""
        if "Reference" in self.properties:
            return self.properties["Reference"].value
        return ""

    @property
    def value(self) -> str:
        """Get the value (e.g., '10k', 'TPA3116D2')."""
        if "Value" in self.properties:
            return self.properties["Value"].value
        return ""

    @property
    def footprint(self) -> str:
        """Get the footprint assignment."""
        if "Footprint" in self.properties:
            return self.properties["Footprint"].value
        return ""

    @property
    def datasheet(self) -> str:
        """Get the datasheet URL."""
        if "Datasheet" in self.properties:
            return self.properties["Datasheet"].value
        return ""

    def to_sexp(self) -> SExp:
        """Convert to S-expression for insertion into a schematic.

        Produces the standard KiCad 8 symbol instance format::

            (symbol
              (lib_id "Device:R")
              (at X Y ROT)
              (mirror x)          ; only if mirror is set
              (unit 1)
              (in_bom yes)
              (on_board yes)
              (dnp no)
              (uuid "...")
              (property "Reference" "R1" ...)
              (property "Value" "10k" ...)
              (pin "1" (uuid "..."))
              (pin "2" (uuid "..."))
              (instances ...)
            )
        """
        sym = SExp.list("symbol")
        sym.append(SExp.list("lib_id", self.lib_id))
        if self.rotation != 0:
            sym.append(SExp.list("at", self.position[0], self.position[1], self.rotation))
        else:
            sym.append(SExp.list("at", self.position[0], self.position[1], 0))
        if self.mirror:
            sym.append(SExp.list("mirror", self.mirror))
        sym.append(SExp.list("unit", self.unit))
        sym.append(SExp.list("in_bom", "yes" if self.in_bom else "no"))
        sym.append(SExp.list("on_board", "yes" if self.on_board else "no"))
        sym.append(SExp.list("dnp", "yes" if self.dnp else "no"))
        sym.append(SExp.list("uuid", self.uuid))

        # Properties
        for prop in self.properties.values():
            sym.append(prop.to_sexp())

        # Pins
        for pin in self.pins:
            sym.append(pin.to_sexp())

        return sym

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SymbolInstance:
        """Parse from S-expression."""
        # Get lib_id
        lib_id = ""
        if lid := sexp.find("lib_id"):
            lib_id = lid.get_string(0) or ""

        # Get UUID
        uuid = ""
        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        # Get position and rotation
        pos = (0.0, 0.0)
        rot = 0.0
        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        # Get mirror
        mirror = ""
        if m := sexp.find("mirror"):
            mirror = m.get_string(0) or ""

        # Get unit
        unit = 1
        if u := sexp.find("unit"):
            unit = u.get_int(0) or 1

        # Get flags
        in_bom = True
        if ib := sexp.find("in_bom"):
            in_bom = ib.get_string(0) != "no"

        on_board = True
        if ob := sexp.find("on_board"):
            on_board = ob.get_string(0) != "no"

        dnp = False
        if d := sexp.find("dnp"):
            dnp = d.get_string(0) == "yes"

        # Get properties
        properties = {}
        for prop in sexp.find_all("property"):
            sp = SymbolProperty.from_sexp(prop)
            properties[sp.name] = sp

        # Get pins
        pins = []
        for pin in sexp.find_all("pin"):
            pins.append(SymbolPin.from_sexp(pin))

        return cls(
            lib_id=lib_id,
            uuid=uuid,
            position=pos,
            rotation=rot,
            mirror=mirror,
            unit=unit,
            in_bom=in_bom,
            on_board=on_board,
            dnp=dnp,
            properties=properties,
            pins=pins,
            _sexp=sexp,
        )

    def get_property(self, name: str) -> str | None:
        """Get a property value by name."""
        if name in self.properties:
            return self.properties[name].value
        return None

    def __repr__(self) -> str:
        return f"SymbolInstance({self.reference!r}, lib={self.lib_id!r}, pos={self.position})"
