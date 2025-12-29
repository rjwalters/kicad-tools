"""
Symbol instance model.

Represents a component instance placed in a schematic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core.sexp import SExp


@dataclass
class SymbolPin:
    """A pin on a symbol instance."""

    number: str
    uuid: str
    name: Optional[str] = None

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
    position: Tuple[float, float] = (0, 0)
    rotation: float = 0
    visible: bool = True

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
        if effects := sexp.find("effects"):
            if effects.find("hide"):
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
    position: Tuple[float, float] = (0, 0)
    rotation: float = 0
    mirror: str = ""  # "", "x", "y"
    unit: int = 1
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False
    properties: Dict[str, SymbolProperty] = field(default_factory=dict)
    pins: List[SymbolPin] = field(default_factory=list)
    _sexp: Optional[SExp] = field(default=None, repr=False)

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

    def get_property(self, name: str) -> Optional[str]:
        """Get a property value by name."""
        if name in self.properties:
            return self.properties[name].value
        return None

    def __repr__(self) -> str:
        return f"SymbolInstance({self.reference!r}, lib={self.lib_id!r}, pos={self.position})"
