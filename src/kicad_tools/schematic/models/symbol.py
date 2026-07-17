"""
KiCad Symbol Models

Symbol definitions and instances for schematic generation.
"""

import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

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
from ..grid import get_symbol_search_paths
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
    _sexp_node: SExp | None = field(default=None, repr=False)
    _parent_node: SExp | None = field(default=None, repr=False)

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
                        unit=getattr(p, "unit", 0),
                    )
                    for p in cached.pins
                ],
            )

        # Parse library using SExp
        return cls._parse_library_sexp(lib_id, lib_paths)

    @classmethod
    def _parse_library_sexp(cls, lib_id: str, lib_paths: list[Path] = None) -> "SymbolDef":
        """Parse symbol definition from library using SExp parser."""
        from kicad_tools.sexp import parse_file

        if lib_paths is None:
            lib_paths = get_symbol_search_paths()

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

    def unit_count(self) -> int:
        """Return the number of distinct units this symbol contains.

        Single-unit symbols return 1.  Multi-unit symbols (e.g. LM393's
        three units: comparator A, comparator B, power) return the
        highest unit number observed across the parsed pins.  Used by the
        block library (and any caller that needs to place every unit) to
        size loops correctly without re-parsing the underlying library.

        Note: ``Pin.unit`` uses ``0`` as a "common to all units" sentinel
        (e.g. shared package power pins), so the unit count is the
        maximum of the non-zero unit numbers — with a floor of 1 so
        single-unit symbols (whose pins are all tagged ``unit=0``) still
        report a meaningful count.
        """
        return max((p.unit for p in self.pins if p.unit > 0), default=1)

    def get_pin_unit(self, pin_number: str) -> int:
        """Return the unit number that owns ``pin_number`` (default 1).

        For multi-unit symbols, the pin number is unique across the whole
        symbol but its position lives on whichever sub-unit declares it.
        Blocks that place several ``SymbolInstance``s for a single device
        use this to route ``pin_position(pin_number)`` to the correct
        instance.  Unknown pin numbers fall back to ``1`` so callers do
        not need defensive existence checks.

        Note: pins tagged with the ``0`` "common to all units" sentinel
        (shared package power pins) report as unit ``1`` here because
        callers using this helper want a concrete unit to place against;
        common pins can be reached through any unit instance.
        """
        for p in self.pins:
            if p.number == pin_number:
                return p.unit if p.unit > 0 else 1
        return 1

    @classmethod
    def _parse_pins_sexp(cls, sym_node: SExp) -> list[Pin]:
        """Parse pin definitions from symbol SExp node.

        Multi-unit KiCad symbols nest their pins inside child symbol nodes
        named ``<Name>_<unit>_<style>`` (e.g. ``LM393_2_1``).  Each pin is
        tagged with the unit number extracted from that wrapper name so
        downstream validation can filter by which units are actually
        placed.  Pins declared at the top level (or inside a unit-0
        wrapper like ``LM393_0_1``) keep ``unit=0`` to indicate they are
        common to every unit instance (shared package power pins, etc.).
        """
        pins = []

        unit_pattern = re.compile(r".+_(\d+)_\d+$")

        def find_pins(node: SExp, current_unit: int):
            """Recursively find all pin nodes, tracking the wrapping unit."""
            for child in node.children:
                if child.name == "pin":
                    pin = Pin.from_sexp(child)
                    if pin.number:  # Must have a pin number
                        pin.unit = current_unit
                        pins.append(pin)
                elif child.is_list:
                    # If this is a unit wrapper (Name_<unit>_<style>), update
                    # the unit context for its descendants.  The graphical
                    # body sub-symbol (``Name_0_1``) carries no pins; its
                    # ``unit=0`` tag also doubles as the "common to all
                    # units" sentinel for shared package pins.
                    next_unit = current_unit
                    if child.name == "symbol" and child.children:
                        first = child.children[0]
                        if first.is_atom:
                            m = unit_pattern.match(str(first.value))
                            if m:
                                next_unit = int(m.group(1))
                    find_pins(child, next_unit)

        find_pins(sym_node, 0)
        return pins

    def _add_prefix_to_node(self, node: SExp, lib_name: str, skip_extends: bool = False) -> SExp:
        """Clone a symbol node and add library prefix to symbol names.

        Recursively walks the SExp tree and prefixes:
        - Main symbol name: (symbol "Name" ...) -> (symbol "Lib:Name" ...)
        - Extends references: (extends "Parent") -> (extends "Lib:Parent")
        - Child symbol names (but NOT unit symbols like Name_0_1)

        Args:
            node: The SExp node to process
            lib_name: Library name to use as prefix
            skip_extends: If True, omit extends nodes entirely (for flattening)
        """
        if node.is_atom:
            return node  # Atoms are returned as-is

        # Skip extends nodes when flattening
        if skip_extends and node.name == "extends":
            return None

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
                        # Unit symbol - keep as-is without library prefix
                        # KiCad expects: parent "(symbol "Lib:Name" ...)"
                        #                unit   "(symbol "Name_0_1" ...)"
                        # NOT: unit "(symbol "Lib:Name_0_1" ...)"
                        new_node.append(child)
                elif node.name == "extends" and i == 0:
                    # First child of extends is parent name
                    new_node.append(SExp.atom(f"{lib_name}:{child.value}"))
                else:
                    new_node.append(child)
            else:
                # Recursively process list children
                result = self._add_prefix_to_node(child, lib_name, skip_extends)
                if result is not None:
                    new_node.append(result)

        return new_node

    def _flatten_with_parent(self, child_node: SExp, parent_node: SExp) -> SExp:
        """Flatten an inherited symbol by merging parent elements into child.

        When KiCad symbols use (extends "ParentName"), they inherit:
        - All graphical elements (polylines, rectangles, circles, arcs, text)
        - All pins
        - Settings like pin_names, exclude_from_sim, in_bom, on_board

        The child can override properties but inherits everything else.

        This method creates a self-contained symbol suitable for embedding
        in a schematic's lib_symbols section, which doesn't support extends.

        Args:
            child_node: The child symbol SExp node (has extends clause)
            parent_node: The parent symbol SExp node

        Returns:
            A new SExp node with parent elements merged into child
        """
        # Get the child's name (first atom child of symbol node)
        child_name = None
        for child in child_node.children:
            if child.is_atom:
                child_name = str(child.value)
                break

        parent_name = None
        for child in parent_node.children:
            if child.is_atom:
                parent_name = str(child.value)
                break

        if not child_name or not parent_name:
            # Can't flatten without names, return child as-is
            return child_node

        # Start with a new symbol node using the child's name
        flattened = SExp.list("symbol")
        flattened.append(SExp.atom(child_name))

        # Collect child's elements, skipping extends
        child_settings = {}  # name -> node for settings like pin_names
        child_properties = {}  # property name -> node
        child_other = []  # other elements from child

        for elem in child_node.children:
            if elem.is_atom:
                continue  # Skip the symbol name, already added
            if elem.name == "extends":
                continue  # Skip extends clause
            elif elem.name == "property":
                # Track properties by their name
                atoms = elem.get_atoms()
                if atoms:
                    child_properties[str(atoms[0])] = elem
            elif elem.name in (
                "pin_names",
                "exclude_from_sim",
                "in_bom",
                "on_board",
                "pin_numbers",
            ):
                child_settings[elem.name] = elem
            else:
                child_other.append(elem)

        # Collect parent's elements
        parent_settings = {}
        parent_properties = {}
        parent_unit_symbols = []  # Unit symbols like ParentName_0_1
        parent_other = []

        for elem in parent_node.children:
            if elem.is_atom:
                continue  # Skip the symbol name
            elif elem.name == "property":
                atoms = elem.get_atoms()
                if atoms:
                    parent_properties[str(atoms[0])] = elem
            elif elem.name in (
                "pin_names",
                "exclude_from_sim",
                "in_bom",
                "on_board",
                "pin_numbers",
            ):
                parent_settings[elem.name] = elem
            elif elem.name == "symbol":
                # This is a unit symbol like ParentName_0_1
                parent_unit_symbols.append(elem)
            else:
                parent_other.append(elem)

        # Add settings: child overrides parent
        for name in ("pin_names", "pin_numbers", "exclude_from_sim", "in_bom", "on_board"):
            if name in child_settings:
                flattened.append(child_settings[name])
            elif name in parent_settings:
                flattened.append(parent_settings[name])

        # Add properties: child overrides parent
        all_prop_names = set(parent_properties.keys()) | set(child_properties.keys())
        for prop_name in all_prop_names:
            if prop_name in child_properties:
                flattened.append(child_properties[prop_name])
            else:
                flattened.append(parent_properties[prop_name])

        # Add other elements from parent (arcs, circles, etc.)
        for elem in parent_other:
            flattened.append(elem)

        # Add unit symbols from parent, renaming them to use child's name
        for unit_sym in parent_unit_symbols:
            renamed = self._rename_unit_symbol(unit_sym, parent_name, child_name)
            flattened.append(renamed)

        # Add any other elements from child (shouldn't normally be any)
        for elem in child_other:
            flattened.append(elem)

        return flattened

    def _rename_unit_symbol(self, unit_sym: SExp, old_name: str, new_name: str) -> SExp:
        """Rename a unit symbol from ParentName_N_N to ChildName_N_N.

        Args:
            unit_sym: The unit symbol SExp node
            old_name: Parent symbol name
            new_name: Child symbol name

        Returns:
            New SExp node with renamed symbol
        """
        new_unit = SExp.list("symbol")

        for child in unit_sym.children:
            if child.is_atom:
                # This is the unit name, e.g., "ParentName_0_1"
                unit_name = str(child.value)
                if unit_name.startswith(old_name + "_"):
                    # Replace parent name with child name
                    suffix = unit_name[len(old_name) :]
                    new_unit.append(SExp.atom(new_name + suffix))
                else:
                    new_unit.append(child)
            else:
                # Copy other children as-is
                new_unit.append(child)

        return new_unit

    def to_sexp_nodes(self) -> list[SExp]:
        """Get symbol definition(s) as SExp nodes for embedding.

        Returns a list of SExp nodes ready for embedding in lib_symbols.

        When a symbol uses inheritance (extends), this method flattens the
        symbol by copying all elements from the parent into the child and
        removing the extends clause. This is necessary because KiCad's
        schematic parser doesn't support extends in embedded lib_symbols.
        """
        lib_name = self.lib_id.split(":")[0]
        nodes = []

        # If we have parsed SExp nodes, use them directly
        if self._sexp_node:
            if self._parent_node:
                # Flatten inherited symbol: merge parent into child
                flattened = self._flatten_with_parent(self._sexp_node, self._parent_node)
                nodes.append(self._add_prefix_to_node(flattened, lib_name))
            else:
                nodes.append(self._add_prefix_to_node(self._sexp_node, lib_name))
        else:
            # Fall back to parsing raw_sexp string
            from kicad_tools.sexp import parse_string

            # Split raw_sexp into individual (symbol ...) definitions
            # using parenthesis counting for proper nested structure handling
            symbol_texts = self._split_symbol_definitions(self.raw_sexp)

            if len(symbol_texts) >= 2:
                # We have parent + child - need to flatten
                try:
                    parent_parsed = parse_string(symbol_texts[0])
                    child_parsed = parse_string(symbol_texts[-1])
                    flattened = self._flatten_with_parent(child_parsed, parent_parsed)
                    nodes.append(self._add_prefix_to_node(flattened, lib_name))
                except Exception:
                    # If flattening fails, fall back to emitting all symbols
                    for text in symbol_texts:
                        try:
                            parsed = parse_string(text)
                            nodes.append(self._add_prefix_to_node(parsed, lib_name))
                        except Exception:
                            pass
            else:
                # Single symbol, no inheritance
                for text in symbol_texts:
                    try:
                        parsed = parse_string(text)
                        nodes.append(self._add_prefix_to_node(parsed, lib_name))
                    except Exception:
                        pass

        return nodes

    @staticmethod
    def _split_symbol_definitions(raw_sexp: str) -> list[str]:
        """Split raw_sexp into individual (symbol ...) definitions.

        Uses parenthesis counting to properly handle nested structures,
        which is necessary for complex symbols with many pins and properties.
        The previous regex-based approach failed on deeply nested symbols.
        """
        definitions = []
        i = 0
        while i < len(raw_sexp):
            # Find start of next symbol definition
            match = raw_sexp.find("(symbol", i)
            if match == -1:
                break

            # Find the end of this symbol definition by counting parens
            start = match
            depth = 0
            j = start
            in_string = False
            escape_next = False

            while j < len(raw_sexp):
                char = raw_sexp[j]

                if escape_next:
                    escape_next = False
                elif char == "\\":
                    escape_next = True
                elif char == '"' and not escape_next:
                    in_string = not in_string
                elif not in_string:
                    if char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                        if depth == 0:
                            # Found the matching close paren
                            definitions.append(raw_sexp[start : j + 1])
                            i = j + 1
                            break
                j += 1
            else:
                # Reached end without finding close paren - malformed, skip rest
                break

        return definitions

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
    properties: dict[str, str] = field(default_factory=dict)
    # BOM / DNP flags emitted into the placed symbol.  Defaults preserve
    # historical output exactly: in_bom=True -> "(in_bom yes)",
    # dnp=False -> "(dnp no)".  Set in_bom=False for bare pads that must
    # not appear in the BOM (test points, fiducials, mounting holes,
    # logos, mechanical-only parts); set dnp=True for do-not-populate
    # parts (issue #4303).
    in_bom: bool = True
    dnp: bool = False

    def find_pin(self, pin_name_or_number: str) -> Pin:
        """Resolve a pin name/number to its :class:`Pin` on this instance.

        Shares the exact-match / unit-aware / case-insensitive / alias /
        fuzzy-suggestion machinery used by :meth:`pin_position`, so callers
        that need the pin object itself (e.g. its ``angle`` for outward stub
        direction, issue #4161) do not reimplement the lookup.

        Args:
            pin_name_or_number: Pin name (e.g., "SCK") or number (e.g., "20")

        Returns:
            The matching :class:`Pin`.

        Raises:
            PinNotFoundError: If no pin matches the given name/number, with
                suggestions for similar pin names.
        """

        # Find the pin by exact match on name or number.  For multi-unit
        # symbols (e.g. LM393), the same pin name may appear on multiple
        # units (pin 3 = "+" on unit 1, pin 5 = "+" on unit 2).  Prefer
        # pins that belong to *this* instance's unit before falling back
        # to any matching pin -- otherwise asking unit 3 for pin 8 (V+,
        # the only pin numbered "8") would still return the correct
        # position, but asking unit 1 for pin "4" (V-, on unit 3) would
        # silently return a phantom position derived from unit 1's
        # outline (issue #3346).
        #
        # ``Pin.unit`` uses ``0`` as the "common to all units" sentinel
        # (shared package power pins, single-unit symbols whose pins are
        # all tagged ``unit=0``).  Treat those as matching any instance
        # unit so callers asking unit 2 for a shared GND pin still get
        # the right position.
        def _pin_matches_unit(pin: Pin) -> bool:
            pin_unit = getattr(pin, "unit", 0)
            return pin_unit == 0 or pin_unit == self.unit

        pin = None
        for p in self.symbol_def.pins:
            if (
                p.name == pin_name_or_number or p.number == pin_name_or_number
            ) and _pin_matches_unit(p):
                pin = p
                break

        # If no in-unit match, fall back to any exact match (this is
        # what preserves single-unit symbol behaviour for callers who
        # don't pass a unit number to ``add_symbol``).
        if pin is None:
            for p in self.symbol_def.pins:
                if p.name == pin_name_or_number or p.number == pin_name_or_number:
                    pin = p
                    break

        # If not found, try case-insensitive match (in-unit preferred)
        if pin is None:
            target_lower = pin_name_or_number.lower()
            for p in self.symbol_def.pins:
                if (
                    p.name.lower() == target_lower or p.number.lower() == target_lower
                ) and _pin_matches_unit(p):
                    pin = p
                    break
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

        return pin

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
        pin = self.find_pin(pin_name_or_number)

        # Get the wire connection point (end of pin) in symbol-local coordinates.
        # KiCad library symbols store pin positions in Y-UP coordinates (math
        # convention: +Y is above origin in the library's drawing).  KiCad
        # schematics use Y-DOWN screen coordinates (origin top-left, +Y is below).
        # We rotate in library Y-up coords, then negate Y to convert to schematic
        # Y-down before translating to the placed symbol position.  This matches
        # the proven implementation in ``kicad_tools.schema.library.get_pin_position``.
        conn_x, conn_y = pin.connection_point()

        # Apply rotation in library Y-up coordinates (standard CCW rotation matrix).
        rad = math.radians(self.rotation)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        rx = conn_x * cos_r - conn_y * sin_r
        ry = conn_x * sin_r + conn_y * cos_r

        # Convert from library Y-up to schematic Y-down by negating the Y offset.
        # Without this flip, pins with non-zero library Y end up on the wrong
        # side of the symbol (issue #2959), causing wires to terminate in empty
        # space and ERC to report pin_not_connected.
        ry = -ry

        # Translate to symbol position.  Round to 2 decimal places for
        # consistent wire matching.
        return (round(self.x + rx, 2), round(self.y + ry, 2))

    def all_pin_positions(self) -> dict[str, tuple[float, float]]:
        """Get positions of all pins."""
        return {p.name: self.pin_position(p.name) for p in self.symbol_def.pins}

    def bounding_box(self, padding: float = 2.54) -> tuple[float, float, float, float]:
        """Calculate the bounding box of this symbol instance.

        The bounding box is computed from pin positions plus optional padding.
        This provides a conservative estimate of the symbol's extent.

        Args:
            padding: Extra space around the symbol in mm (default: 2.54mm = 100mil)

        Returns:
            Tuple of (min_x, min_y, max_x, max_y) defining the bounding box
        """
        if not self.symbol_def.pins:
            # No pins - use a default size around the symbol center
            half_size = 5.08 + padding  # ~200mil default + padding
            return (
                self.x - half_size,
                self.y - half_size,
                self.x + half_size,
                self.y + half_size,
            )

        # Get all pin positions
        positions = [self.pin_position(p.name or p.number) for p in self.symbol_def.pins]

        min_x = min(p[0] for p in positions)
        max_x = max(p[0] for p in positions)
        min_y = min(p[1] for p in positions)
        max_y = max(p[1] for p in positions)

        return (
            min_x - padding,
            min_y - padding,
            max_x + padding,
            max_y + padding,
        )

    def overlaps(self, other: "SymbolInstance", padding: float = 2.54) -> bool:
        """Check if this symbol's bounding box overlaps with another.

        Args:
            other: Another SymbolInstance to check against
            padding: Extra space around each symbol in mm

        Returns:
            True if the bounding boxes overlap
        """
        box1 = self.bounding_box(padding)
        box2 = other.bounding_box(padding)

        # Check for non-overlap conditions
        if box1[2] < box2[0]:  # self.max_x < other.min_x
            return False
        if box1[0] > box2[2]:  # self.min_x > other.max_x
            return False
        if box1[3] < box2[1]:  # self.max_y < other.min_y
            return False
        if box1[1] > box2[3]:  # self.min_y > other.max_y
            return False

        return True

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
            SExp.list("in_bom", "yes" if self.in_bom else "no"),
            SExp.list("on_board", "yes"),
            SExp.list("dnp", "yes" if self.dnp else "no"),
            uuid_node(self.uuid_str),
        )

        # Add properties
        sym.append(symbol_property_node("Reference", self.reference, self.x, self.y - 5.08))
        sym.append(symbol_property_node("Value", self.value, self.x, self.y - 2.54))
        sym.append(symbol_property_node("Footprint", self.footprint, self.x, self.y, hide=True))
        sym.append(symbol_property_node("Datasheet", "~", self.x, self.y, hide=True))

        # Add custom properties (hidden by default)
        for prop_name, prop_value in self.properties.items():
            sym.append(symbol_property_node(prop_name, prop_value, self.x, self.y, hide=True))

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
        in_bom_tok = "yes" if self.in_bom else "no"
        dnp_tok = "yes" if self.dnp else "no"

        # Generate custom properties (hidden by default)
        custom_props = ""
        for prop_name, prop_value in self.properties.items():
            custom_props += f'''\t\t(property "{prop_name}" "{prop_value}"
\t\t\t(at {x} {y} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(hide yes)
\t\t\t)
\t\t)
'''

        return f'''\t(symbol
\t\t(lib_id "{self.symbol_def.lib_id}")
\t\t(at {x} {y} {int(self.rotation)})
\t\t(unit {self.unit})
\t\t(exclude_from_sim no)
\t\t(in_bom {in_bom_tok})
\t\t(on_board yes)
\t\t(dnp {dnp_tok})
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
{custom_props}{pin_uuids}
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

        # Get BOM / DNP flags (issue #4303).  KiCad omits or uses "yes"/"no"
        # tokens; default to the historical in_bom=True / dnp=False when the
        # token is absent so older schematics round-trip unchanged.
        in_bom_node = node.get("in_bom")
        in_bom = str(in_bom_node.get_first_atom()).lower() != "no" if in_bom_node else True
        dnp_node = node.get("dnp")
        dnp = str(dnp_node.get_first_atom()).lower() == "yes" if dnp_node else False

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
            in_bom=in_bom,
            dnp=dnp,
        )
