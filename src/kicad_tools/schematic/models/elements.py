"""
KiCad Schematic Element Models

Wire, Junction, Label, HierarchicalLabel, and PowerSymbol classes.
"""

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import (
    at,
    hier_label_node,
    junction_node,
    label_node,
    pin_uuid_node,
    symbol_instances_node,
    symbol_property_node,
    uuid_node,
    wire_node,
)

if TYPE_CHECKING:
    from .symbol import SymbolDef


@dataclass
class Wire:
    """A wire segment connecting two points."""

    x1: float
    y1: float
    x2: float
    y2: float
    uuid_str: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def between(cls, p1: tuple[float, float], p2: tuple[float, float]) -> "Wire":
        """Create a wire between two points."""
        # Round coordinates for consistent matching
        return cls(x1=round(p1[0], 2), y1=round(p1[1], 2), x2=round(p2[0], 2), y2=round(p2[1], 2))

    def to_sexp_node(self) -> SExp:
        """Build S-expression tree for this wire."""
        return wire_node(self.x1, self.y1, self.x2, self.y2, self.uuid_str)

    def to_sexp(self) -> str:
        """Generate S-expression string (delegates to to_sexp_node)."""
        return self.to_sexp_node().to_string(indent=1)

    @classmethod
    def from_sexp(cls, node: SExp) -> "Wire":
        """Parse a Wire from an S-expression node.

        Expected format:
            (wire (pts (xy x1 y1) (xy x2 y2)) (stroke ...) (uuid ...))
        """
        pts_node = node["pts"]
        xy_nodes = [c for c in pts_node.children if c.name == "xy"]
        if len(xy_nodes) < 2:
            raise ValueError("Wire must have at least 2 xy points")

        p1_atoms = xy_nodes[0].get_atoms()
        p2_atoms = xy_nodes[1].get_atoms()

        uuid_node_elem = node.get("uuid")
        uuid_str = uuid_node_elem.get_first_atom() if uuid_node_elem else str(uuid.uuid4())

        return cls(
            x1=round(float(p1_atoms[0]), 2),
            y1=round(float(p1_atoms[1]), 2),
            x2=round(float(p2_atoms[0]), 2),
            y2=round(float(p2_atoms[1]), 2),
            uuid_str=str(uuid_str),
        )


@dataclass
class Junction:
    """A junction point where wires connect."""

    x: float
    y: float
    uuid_str: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        # Round coordinates for consistent matching
        self.x = round(self.x, 2)
        self.y = round(self.y, 2)

    def to_sexp_node(self) -> SExp:
        """Build S-expression tree for this junction."""
        return junction_node(self.x, self.y, self.uuid_str)

    def to_sexp(self) -> str:
        """Generate S-expression string (delegates to to_sexp_node)."""
        return self.to_sexp_node().to_string(indent=1)

    @classmethod
    def from_sexp(cls, node: SExp) -> "Junction":
        """Parse a Junction from an S-expression node.

        Expected format:
            (junction (at x y) (diameter 0) (color ...) (uuid ...))
        """
        at_node = node["at"]
        atoms = at_node.get_atoms()

        uuid_node_elem = node.get("uuid")
        uuid_str = uuid_node_elem.get_first_atom() if uuid_node_elem else str(uuid.uuid4())

        return cls(x=round(float(atoms[0]), 2), y=round(float(atoms[1]), 2), uuid_str=str(uuid_str))


@dataclass
class Label:
    """A net label."""

    text: str
    x: float
    y: float
    rotation: float = 0
    uuid_str: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build S-expression tree for this label."""
        return label_node(self.text, self.x, self.y, self.rotation, self.uuid_str)

    def to_sexp(self) -> str:
        """Generate S-expression string (delegates to to_sexp_node)."""
        return self.to_sexp_node().to_string(indent=1)

    @classmethod
    def from_sexp(cls, node: SExp) -> "Label":
        """Parse a Label from an S-expression node.

        Expected format:
            (label "text" (at x y [rotation]) ... (uuid ...))
        """
        # Get text from first atom child
        text = node.get_first_atom()
        if text is None:
            raise ValueError("Label must have text")

        at_node = node["at"]
        atoms = at_node.get_atoms()
        x = round(float(atoms[0]), 2)
        y = round(float(atoms[1]), 2)
        rotation = float(atoms[2]) if len(atoms) > 2 else 0

        uuid_node_elem = node.get("uuid")
        uuid_str = uuid_node_elem.get_first_atom() if uuid_node_elem else str(uuid.uuid4())

        return cls(text=str(text), x=x, y=y, rotation=rotation, uuid_str=str(uuid_str))


@dataclass
class HierarchicalLabel:
    """A hierarchical sheet label."""

    text: str
    x: float
    y: float
    shape: str = "input"  # input, output, bidirectional, passive
    rotation: float = 0
    uuid_str: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build S-expression tree for this hierarchical label."""
        return hier_label_node(self.text, self.x, self.y, self.shape, self.rotation, self.uuid_str)

    def to_sexp(self) -> str:
        """Generate S-expression string (delegates to to_sexp_node)."""
        return self.to_sexp_node().to_string(indent=1)

    @classmethod
    def from_sexp(cls, node: SExp) -> "HierarchicalLabel":
        """Parse a HierarchicalLabel from an S-expression node.

        Expected format:
            (hierarchical_label "text" (shape output) (at x y [rotation]) ... (uuid ...))
        """
        # Get text from first atom child
        text = node.get_first_atom()
        if text is None:
            raise ValueError("HierarchicalLabel must have text")

        # Get shape
        shape_node = node.get("shape")
        shape = shape_node.get_first_atom() if shape_node else "input"

        at_node = node["at"]
        atoms = at_node.get_atoms()
        x = round(float(atoms[0]), 2)
        y = round(float(atoms[1]), 2)
        rotation = float(atoms[2]) if len(atoms) > 2 else 0

        uuid_node_elem = node.get("uuid")
        uuid_str = uuid_node_elem.get_first_atom() if uuid_node_elem else str(uuid.uuid4())

        return cls(
            text=str(text), x=x, y=y, shape=str(shape), rotation=rotation, uuid_str=str(uuid_str)
        )


@dataclass
class PowerSymbol:
    """A power symbol (GND, VCC, etc.)."""

    lib_id: str  # e.g., "power:GND", "power:+3.3V"
    x: float
    y: float
    rotation: float = 0
    reference: str = "#PWR?"
    uuid_str: str = field(default_factory=lambda: str(uuid.uuid4()))

    _symbol_def: Optional["SymbolDef"] = field(default=None, repr=False)

    def to_sexp_node(self, project_name: str, sheet_path: str) -> SExp:
        """Build S-expression tree for this power symbol."""
        value = self.lib_id.split(":")[1]

        # Build symbol node with all standard fields
        sym = SExp.list(
            "symbol",
            SExp.list("lib_id", self.lib_id),
            at(self.x, self.y, self.rotation),
            SExp.list("unit", 1),
            SExp.list("exclude_from_sim", "no"),
            SExp.list("in_bom", "yes"),
            SExp.list("on_board", "yes"),
            SExp.list("dnp", "no"),
            uuid_node(self.uuid_str),
        )

        # Add properties - Reference (hidden), Value (visible), Footprint, Datasheet
        sym.append(
            symbol_property_node("Reference", self.reference, self.x, self.y + 2.54, hide=True)
        )
        sym.append(symbol_property_node("Value", value, self.x, self.y + 5.08, hide=False))
        sym.append(symbol_property_node("Footprint", "", self.x, self.y, hide=True))
        sym.append(symbol_property_node("Datasheet", "", self.x, self.y, hide=True))

        # Power symbols always have pin "1"
        sym.append(pin_uuid_node("1", str(uuid.uuid4())))

        # Add instances section
        sym.append(symbol_instances_node(project_name, sheet_path, self.reference, 1))

        return sym

    def to_sexp(self, project_name: str, sheet_path: str) -> str:
        """Generate S-expression string (delegates to to_sexp_node)."""
        return self.to_sexp_node(project_name, sheet_path).to_string(indent=1)

    @classmethod
    def from_sexp(cls, node: SExp) -> "PowerSymbol":
        """Parse a PowerSymbol from an S-expression node.

        Expected format:
            (symbol
                (lib_id "power:GND")
                (at x y [rotation])
                ...
                (uuid "...")
                (property "Reference" "#PWR01" ...)
                ...
            )

        Power symbols are identified by lib_id starting with "power:".
        """
        # Get lib_id
        lib_id_node = node["lib_id"]
        lib_id = str(lib_id_node.get_first_atom())

        # Get position
        at_node = node["at"]
        atoms = at_node.get_atoms()
        x = round(float(atoms[0]), 2)
        y = round(float(atoms[1]), 2)
        rotation = float(atoms[2]) if len(atoms) > 2 else 0

        # Get UUID
        uuid_node_elem = node.get("uuid")
        uuid_str = str(uuid_node_elem.get_first_atom()) if uuid_node_elem else str(uuid.uuid4())

        # Get reference from properties
        reference = "#PWR?"
        for prop_node in node.find_all("property"):
            atoms = prop_node.get_atoms()
            if len(atoms) >= 2 and str(atoms[0]) == "Reference":
                reference = str(atoms[1])
                break

        return cls(
            lib_id=lib_id, x=x, y=y, rotation=rotation, reference=reference, uuid_str=uuid_str
        )

    @staticmethod
    def is_power_symbol(node: SExp) -> bool:
        """Check if an S-expression node represents a power symbol.

        Power symbols are identified by:
        1. lib_id starting with "power:"
        2. Reference starting with "#PWR"
        """
        lib_id_node = node.get("lib_id")
        if lib_id_node:
            lib_id = str(lib_id_node.get_first_atom())
            if lib_id.startswith("power:"):
                return True

        # Also check reference for #PWR pattern
        for prop_node in node.find_all("property"):
            atoms = prop_node.get_atoms()
            if len(atoms) >= 2:
                if str(atoms[0]) == "Reference" and str(atoms[1]).startswith("#PWR"):
                    return True

        return False
