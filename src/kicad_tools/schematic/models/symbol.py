"""
KiCad Symbol Models

Symbol definitions and instances for schematic generation.
"""

import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import (
    at,
    pin_uuid_node,
    symbol_instances_node,
    symbol_property_node,
    uuid_node,
)
from kicad_tools.sexp.builders import fmt as sexp_fmt

from ..exceptions import LibraryNotFoundError, PinNotFoundError, SymbolNotFoundError
from ..grid import KICAD_SYMBOL_PATHS
from ..helpers import _expand_pin_aliases, _find_similar, _fmt_coord
from .pin import Pin

if TYPE_CHECKING:
    pass

# Symbol registry for caching and better error messages
try:
    from kicad_tools.schematic.registry import get_registry as _get_symbol_registry

    _REGISTRY_AVAILABLE = True
except ImportError:
    _REGISTRY_AVAILABLE = False
    _get_symbol_registry = None


@dataclass
class SymbolDef:
    """Symbol definition extracted from library."""

    lib_id: str
    name: str
    raw_sexp: str  # Original S-expression for embedding (legacy, kept for compatibility)
    pins: list[Pin] = field(default_factory=list)
    # Parsed SExp nodes for structured access (optional, used when parsed with SExp)
    _sexp_node: Optional[SExp] = field(default=None, repr=False)
    _parent_node: Optional[SExp] = field(default=None, repr=False)

    @classmethod
    def from_library(cls, lib_id: str, lib_paths: list[Path] = None) -> "SymbolDef":
        """Extract symbol definition from KiCad library.

        Uses the SymbolRegistry for caching when available, falling back to
        SExp-based parsing otherwise.

        Args:
            lib_id: Library:Symbol format (e.g., "Audio:PCM5122PW")
            lib_paths: Optional list of library search paths
        """
        # Use registry for caching when available
        if _REGISTRY_AVAILABLE and lib_paths is None:
            registry = _get_symbol_registry()
            cached = registry.get(lib_id)
            # Convert registry SymbolDef to local SymbolDef
            return cls(
                lib_id=cached.lib_id,
                name=cached.name,
                raw_sexp=cached.raw_sexp,
                pins=[
                    Pin(
                        name=p.name,
                        number=p.number,
                        x=p.x,
                        y=p.y,
                        angle=p.angle,
                        length=p.length,
                        pin_type=p.pin_type,
                    )
                    for p in cached.pins
                ],
            )

        # Parse library using SExp
        return cls._parse_library_sexp(lib_id, lib_paths)

    @classmethod
    def _parse_library_sexp(cls, lib_id: str, lib_paths: list[Path] = None) -> "SymbolDef":
        """Parse symbol definition from library using SExp parser."""
        from kicad_sexp import parse_file

        if lib_paths is None:
            lib_paths = KICAD_SYMBOL_PATHS

        lib_name, sym_name = lib_id.split(":", 1)
        lib_file = f"{lib_name}.kicad_sym"

        # Find library file
        lib_path = None
        searched = []
        for search_path in lib_paths:
            candidate = search_path / lib_file
            searched.append(candidate)
            if candidate.exists():
                lib_path = candidate
                break

        if lib_path is None:
            raise LibraryNotFoundError(lib_file, searched)

        # Parse library with SExp
        lib_doc = parse_file(lib_path)

        # Collect all top-level symbol names for error messages
        all_symbols = []
        for child in lib_doc.children:
            if child.name == "symbol" and child.children:
                # Symbol name is first atom after "symbol"
                name_atom = child.children[0]
                if name_atom.is_atom and "_" not in str(name_atom.value):
                    all_symbols.append(str(name_atom.value))

        # Find the target symbol
        sym_node = None
        for child in lib_doc.children:
            if child.name == "symbol" and child.children:
                if str(child.children[0].value) == sym_name:
                    sym_node = child
                    break

        if sym_node is None:
            suggestions = _find_similar(sym_name, all_symbols)
            raise SymbolNotFoundError(
                symbol_name=sym_name,
                library_file=lib_file,
                available_symbols=all_symbols,
                suggestions=suggestions,
            )

        # Check if this symbol extends another (symbol inheritance)
        parent_node = None
        extends_node = sym_node.get("extends")
        if extends_node and extends_node.children:
            parent_name = str(extends_node.children[0].value)
            for child in lib_doc.children:
                if child.name == "symbol" and child.children:
                    if str(child.children[0].value) == parent_name:
                        parent_node = child
                        break

        # Parse pins from symbol (and parent if inherited)
        pins = cls._parse_pins_sexp(sym_node)
        if parent_node:
            # Parent pins are inherited
            pins.extend(cls._parse_pins_sexp(parent_node))

        # Generate raw_sexp string for backward compatibility
        raw_sexp = sym_node.to_string()
        if parent_node:
            raw_sexp = parent_node.to_string() + "\n" + raw_sexp

        return cls(
            lib_id=lib_id,
            name=sym_name,
            raw_sexp=raw_sexp,
            pins=pins,
            _sexp_node=sym_node,
            _parent_node=parent_node,
        )

    @classmethod
    def _parse_pins_sexp(cls, sym_node: SExp) -> list[Pin]:
        """Parse pin definitions from symbol SExp node."""
        pins = []

        def find_pins(node: SExp):
            """Recursively find all pin nodes."""
            for child in node.children:
                if child.name == "pin":
                    pin = Pin.from_sexp(child)
                    if pin.number:  # Must have a pin number
                        pins.append(pin)
                elif child.is_list:
                    find_pins(child)

        find_pins(sym_node)
        return pins

    def _add_prefix_to_node(self, node: SExp, lib_name: str) -> SExp:
        """Clone a symbol node and add library prefix to symbol names.

        Recursively walks the SExp tree and prefixes:
        - Main symbol name: (symbol "Name" ...) -> (symbol "Lib:Name" ...)
        - Extends references: (extends "Parent") -> (extends "Lib:Parent")
        - Child symbol names (but NOT unit symbols like Name_0_1)
        """
        if node.is_atom:
            return node  # Atoms are returned as-is

        # Clone the list node
        new_node = SExp.list(node.name)

        for i, child in enumerate(node.children):
            if child.is_atom:
                # Check if this is a symbol name that needs prefixing
                if node.name == "symbol" and i == 0:
                    # First child of a symbol is its name
                    sym_name = str(child.value)
                    # Only prefix main symbols, not unit symbols (which have _N_N suffix)
                    if not re.match(r".+_\d+_\d+$", sym_name):
                        new_node.append(SExp.atom(f"{lib_name}:{sym_name}"))
                    else:
                        # Unit symbol - prefix the base name part
                        # e.g., "AP2204K-1.5_0_1" -> "Lib:AP2204K-1.5_0_1"
                        match = re.match(r"(.+?)(_\d+_\d+)$", sym_name)
                        if match:
                            base, suffix = match.groups()
                            new_node.append(SExp.atom(f"{lib_name}:{base}{suffix}"))
                        else:
                            new_node.append(child)
                elif node.name == "extends" and i == 0:
                    # First child of extends is parent name
                    new_node.append(SExp.atom(f"{lib_name}:{child.value}"))
                else:
                    new_node.append(child)
            else:
                # Recursively process list children
                new_node.append(self._add_prefix_to_node(child, lib_name))

        return new_node

    def to_sexp_nodes(self) -> list[SExp]:
        """Get symbol definition(s) as SExp nodes for embedding.

        Returns a list because symbols with inheritance require both
        parent and child symbol definitions.
        """
        lib_name = self.lib_id.split(":")[0]
        nodes = []

        # If we have parsed SExp nodes, use them directly
        if self._sexp_node:
            if self._parent_node:
                nodes.append(self._add_prefix_to_node(self._parent_node, lib_name))
            nodes.append(self._add_prefix_to_node(self._sexp_node, lib_name))
        else:
            # Fall back to parsing raw_sexp string
            from kicad_sexp import parse_string

            # Parse the raw_sexp which may contain multiple symbols
            # wrapped each (symbol ...) in parsing
            parts = re.findall(
                r'\(symbol\s+"[^"]+(?:_\d+_\d+)?"[^)]*(?:\([^)]*\)[^)]*)*\)', self.raw_sexp
            )
            for part in parts:
                try:
                    parsed = parse_string(part)
                    nodes.append(self._add_prefix_to_node(parsed, lib_name))
                except Exception:
                    # If parsing fails, skip this part
                    pass

            # If no parts found, try parsing the whole thing
            if not nodes:
                try:
                    parsed = parse_string(self.raw_sexp)
                    nodes.append(self._add_prefix_to_node(parsed, lib_name))
                except Exception:
                    pass

        return nodes

    def get_embedded_sexp(self) -> str:
        """Get the symbol definition formatted for embedding in schematic.

        Uses to_sexp_nodes() to build structured SExp, then serializes.
        """
        nodes = self.to_sexp_nodes()
        return "\n".join(n.to_string(indent=2) for n in nodes)


@dataclass
class SymbolInstance:
    """A placed symbol instance in the schematic."""

    symbol_def: SymbolDef
    x: float
    y: float
    rotation: float  # Degrees: 0, 90, 180, 270
    reference: str
    value: str
    unit: int = 1
    uuid_str: str = field(default_factory=lambda: str(uuid.uuid4()))
    footprint: str = ""

    def pin_position(self, pin_name_or_number: str) -> tuple[float, float]:
        """Get absolute position of a pin after placement and rotation.

        Args:
            pin_name_or_number: Pin name (e.g., "SCK") or number (e.g., "20")

        Returns:
            (x, y) tuple of absolute pin position, rounded to 2 decimal places

        Raises:
            PinNotFoundError: If no pin matches the given name/number, with
                suggestions for similar pin names
        """
        # Find the pin by exact match on name or number
        pin = None
        for p in self.symbol_def.pins:
            if p.name == pin_name_or_number or p.number == pin_name_or_number:
                pin = p
                break

        # If not found, try case-insensitive match
        if pin is None:
            target_lower = pin_name_or_number.lower()
            for p in self.symbol_def.pins:
                if p.name.lower() == target_lower or p.number.lower() == target_lower:
                    pin = p
                    break

        # If still not found, try alias matching
        if pin is None:
            aliases = _expand_pin_aliases(pin_name_or_number)
            for alias in aliases[1:]:  # Skip first (original name)
                alias_lower = alias.lower()
                for p in self.symbol_def.pins:
                    if p.name.lower() == alias_lower:
                        pin = p
                        break
                if pin:
                    break

        if pin is None:
            # Build list of all pin names for fuzzy matching
            all_names = []
            for p in self.symbol_def.pins:
                if p.name:
                    all_names.append(p.name)
                all_names.append(p.number)

            # Find similar names
            suggestions = _find_similar(pin_name_or_number, all_names)

            raise PinNotFoundError(
                pin_name=pin_name_or_number,
                symbol_name=f"{self.reference} ({self.symbol_def.lib_id})",
                available_pins=self.symbol_def.pins,
                suggestions=suggestions,
            )

        # Apply rotation transformation
        # Note: KiCad schematic uses Y-down, but symbol definitions use Y-up
        # So we negate the Y component when translating
        rad = math.radians(self.rotation)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        # Rotate pin position around origin (in symbol's Y-up coordinate system)
        rx = pin.x * cos_r - pin.y * sin_r
        ry = pin.x * sin_r + pin.y * cos_r

        # Translate to symbol position (flip Y for schematic's Y-down system)
        # Round to 2 decimal places for consistent wire matching
        return (round(self.x + rx, 2), round(self.y - ry, 2))

    def all_pin_positions(self) -> dict[str, tuple[float, float]]:
        """Get positions of all pins."""
        return {p.name: self.pin_position(p.name) for p in self.symbol_def.pins}

    def to_sexp_node(self, project_name: str, sheet_path: str) -> SExp:
        """Build S-expression tree for this symbol instance."""
        # Note: x, y formatting reserved for future position string output
        _x = sexp_fmt(self.x)  # noqa: F841
        _y = sexp_fmt(self.y)  # noqa: F841

        # Build main symbol node
        sym = SExp.list(
            "symbol",
            SExp.list("lib_id", self.symbol_def.lib_id),
            at(self.x, self.y, self.rotation),
            SExp.list("unit", self.unit),
            SExp.list("exclude_from_sim", "no"),
            SExp.list("in_bom", "yes"),
            SExp.list("on_board", "yes"),
            SExp.list("dnp", "no"),
            uuid_node(self.uuid_str),
        )

        # Add properties
        sym.append(symbol_property_node("Reference", self.reference, self.x, self.y - 5.08))
        sym.append(symbol_property_node("Value", self.value, self.x, self.y - 2.54))
        sym.append(symbol_property_node("Footprint", self.footprint, self.x, self.y, hide=True))
        sym.append(symbol_property_node("Datasheet", "~", self.x, self.y, hide=True))

        # Add pin UUIDs
        for pin in self.symbol_def.pins:
            sym.append(pin_uuid_node(pin.number, str(uuid.uuid4())))

        # Add instances
        sym.append(symbol_instances_node(project_name, sheet_path, self.reference, self.unit))

        return sym

    def to_sexp(self, project_name: str, sheet_path: str) -> str:
        """Generate S-expression for this symbol instance."""
        # Generate pin UUID mappings
        pin_uuids = "\n".join(
            f'\t\t(pin "{p.number}" (uuid "{uuid.uuid4()}"))' for p in self.symbol_def.pins
        )

        # Use _fmt_coord to avoid floating-point precision issues
        x = _fmt_coord(self.x)
        y = _fmt_coord(self.y)
        ref_y = _fmt_coord(self.y - 5.08)
        val_y = _fmt_coord(self.y - 2.54)

        return f'''\t(symbol
\t\t(lib_id "{self.symbol_def.lib_id}")
\t\t(at {x} {y} {int(self.rotation)})
\t\t(unit {self.unit})
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "{self.uuid_str}")
\t\t(property "Reference" "{self.reference}"
\t\t\t(at {x} {ref_y} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(property "Value" "{self.value}"
\t\t\t(at {x} {val_y} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(property "Footprint" "{self.footprint}"
\t\t\t(at {x} {y} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(hide yes)
\t\t\t)
\t\t)
\t\t(property "Datasheet" "~"
\t\t\t(at {x} {y} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(hide yes)
\t\t\t)
\t\t)
{pin_uuids}
\t\t(instances
\t\t\t(project "{project_name}"
\t\t\t\t(path "{sheet_path}"
\t\t\t\t\t(reference "{self.reference}")
\t\t\t\t\t(unit {self.unit})
\t\t\t\t)
\t\t\t)
\t\t)
\t)'''

    @classmethod
    def from_sexp(
        cls,
        node: SExp,
        symbol_defs: dict[str, "SymbolDef"] = None,
        lib_symbols: dict[str, SExp] = None,
    ) -> "SymbolInstance":
        """Parse a SymbolInstance from an S-expression node.

        Expected format:
            (symbol
                (lib_id "Library:Symbol")
                (at x y [rotation])
                (unit N)
                ...
                (uuid "...")
                (property "Reference" "U1" ...)
                (property "Value" "value" ...)
                (property "Footprint" "..." ...)
                ...
            )

        Args:
            node: The S-expression node to parse
            symbol_defs: Optional dict of already-parsed SymbolDefs keyed by lib_id
            lib_symbols: Optional dict of embedded lib_symbol SExp nodes keyed by lib_id
                (from schematic's lib_symbols section)
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

        # Get unit
        unit_node = node.get("unit")
        unit = int(unit_node.get_first_atom()) if unit_node else 1

        # Get UUID
        uuid_node_elem = node.get("uuid")
        uuid_str = str(uuid_node_elem.get_first_atom()) if uuid_node_elem else str(uuid.uuid4())

        # Get properties
        reference = ""
        value = ""
        footprint = ""
        for prop_node in node.find_all("property"):
            atoms = prop_node.get_atoms()
            if len(atoms) >= 2:
                prop_name = str(atoms[0])
                prop_value = str(atoms[1])
                if prop_name == "Reference":
                    reference = prop_value
                elif prop_name == "Value":
                    value = prop_value
                elif prop_name == "Footprint":
                    footprint = prop_value

        # Get or create SymbolDef
        symbol_def = None

        # First try the provided symbol_defs dict
        if symbol_defs and lib_id in symbol_defs:
            symbol_def = symbol_defs[lib_id]

        # Next try to parse from embedded lib_symbols
        if symbol_def is None and lib_symbols and lib_id in lib_symbols:
            lib_sym_node = lib_symbols[lib_id]
            # Create a minimal SymbolDef from embedded symbol
            pins = SymbolDef._parse_pins_sexp(lib_sym_node)
            symbol_def = SymbolDef(
                lib_id=lib_id,
                name=lib_id.split(":")[1] if ":" in lib_id else lib_id,
                raw_sexp=lib_sym_node.to_string(),
                pins=pins,
                _sexp_node=lib_sym_node,
            )

        # Finally try to look up from library
        if symbol_def is None:
            try:
                symbol_def = SymbolDef.from_library(lib_id)
            except (LibraryNotFoundError, SymbolNotFoundError):
                # Create a placeholder SymbolDef with no pins
                # This allows loading schematics even if libraries aren't available
                symbol_def = SymbolDef(
                    lib_id=lib_id,
                    name=lib_id.split(":")[1] if ":" in lib_id else lib_id,
                    raw_sexp="",
                    pins=[],
                )

        return cls(
            symbol_def=symbol_def,
            x=x,
            y=y,
            rotation=rotation,
            reference=reference,
            value=value,
            unit=unit,
            uuid_str=uuid_str,
            footprint=footprint,
        )
