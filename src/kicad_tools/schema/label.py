"""
Label models for net naming.

Labels assign names to nets and enable connections across the schematic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from ..core.sexp import SExp


@dataclass
class Label:
    """
    A local net label.

    Labels assign a name to a wire/net at a specific point.
    The same label name on different wires connects them.
    """

    text: str
    position: Tuple[float, float]
    rotation: float = 0
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Label:
        """Parse from S-expression."""
        text = sexp.get_string(0) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        uuid = ""

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        return cls(text=text, position=pos, rotation=rot, uuid=uuid)

    def __repr__(self) -> str:
        return f"Label({self.text!r}, {self.position})"


@dataclass
class HierarchicalLabel:
    """
    A hierarchical label for connections between sheets.

    Hierarchical labels connect signals across schematic hierarchy.
    They appear on both the sub-sheet and the parent sheet.
    """

    text: str
    position: Tuple[float, float]
    rotation: float = 0
    shape: str = "input"  # input, output, bidirectional, tri_state, passive
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> HierarchicalLabel:
        """Parse from S-expression."""
        text = sexp.get_string(0) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        shape = "input"
        uuid = ""

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        if s := sexp.find("shape"):
            shape = s.get_string(0) or "input"

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        return cls(text=text, position=pos, rotation=rot, shape=shape, uuid=uuid)

    def __repr__(self) -> str:
        return f"HierarchicalLabel({self.text!r}, shape={self.shape})"


@dataclass
class GlobalLabel:
    """
    A global label for project-wide connections.

    Global labels connect signals across all sheets in a project
    without needing hierarchical connections.
    """

    text: str
    position: Tuple[float, float]
    rotation: float = 0
    shape: str = "input"
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> GlobalLabel:
        """Parse from S-expression."""
        text = sexp.get_string(0) or ""
        pos = (0.0, 0.0)
        rot = 0.0
        shape = "input"
        uuid = ""

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        if s := sexp.find("shape"):
            shape = s.get_string(0) or "input"

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        return cls(text=text, position=pos, rotation=rot, shape=shape, uuid=uuid)

    def __repr__(self) -> str:
        return f"GlobalLabel({self.text!r})"


@dataclass
class PowerSymbol:
    """
    A power symbol (GND, +5V, +3.3V, etc.).

    Power symbols are special labels that connect to power nets.
    """

    lib_id: str
    position: Tuple[float, float]
    rotation: float = 0
    uuid: str = ""
    value: str = ""  # The net name (GND, +5V, etc.)

    @classmethod
    def from_symbol_sexp(cls, sexp: SExp) -> Optional[PowerSymbol]:
        """
        Try to parse a power symbol from a symbol S-expression.

        Returns None if this is not a power symbol.
        """
        lib_id = ""
        if lid := sexp.find("lib_id"):
            lib_id = lid.get_string(0) or ""

        # Power symbols typically have lib_id starting with "power:"
        if not lib_id.startswith("power:"):
            return None

        pos = (0.0, 0.0)
        rot = 0.0
        uuid = ""
        value = ""

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rot = at.get_float(2) or 0

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        for prop in sexp.find_all("property"):
            if prop.get_string(0) == "Value":
                value = prop.get_string(1) or ""
                break

        return cls(lib_id=lib_id, position=pos, rotation=rot, uuid=uuid, value=value)
