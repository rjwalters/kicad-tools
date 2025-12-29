"""
Symbol library models.

Represents KiCad symbol library definitions with pin geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.sexp import SExp, parse_sexp


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
    position: Tuple[float, float]  # Position relative to symbol origin
    rotation: float  # Degrees: 0=right, 90=up, 180=left, 270=down
    length: float

    @property
    def connection_offset(self) -> Tuple[float, float]:
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


@dataclass
class LibrarySymbol:
    """
    A symbol definition from a KiCad symbol library.

    Contains the symbol's graphical elements and pin definitions.
    """

    name: str
    properties: Dict[str, str] = field(default_factory=dict)
    pins: List[LibraryPin] = field(default_factory=list)
    units: int = 1

    @property
    def pin_count(self) -> int:
        return len(self.pins)

    def get_pin(self, number: str) -> Optional[LibraryPin]:
        """Get a pin by number."""
        for pin in self.pins:
            if pin.number == number:
                return pin
        return None

    def get_pins_by_name(self, name: str) -> List[LibraryPin]:
        """Get all pins with a given name (e.g., GND, VCC)."""
        return [p for p in self.pins if p.name == name]

    def get_pin_position(
        self,
        pin_number: str,
        instance_pos: Tuple[float, float],
        instance_rot: float = 0,
        mirror: str = "",
    ) -> Optional[Tuple[float, float]]:
        """
        Calculate the actual schematic position of a pin.

        Args:
            pin_number: The pin number to locate
            instance_pos: The symbol instance position in schematic
            instance_rot: The symbol instance rotation in degrees
            mirror: Mirror mode ("", "x", "y")

        Returns:
            (x, y) position in schematic coordinates, or None if pin not found
        """
        pin = self.get_pin(pin_number)
        if not pin:
            return None

        # Start with pin's local position
        x, y = pin.position

        # Apply mirror
        if mirror == "x":
            x = -x
        elif mirror == "y":
            y = -y

        # Apply rotation
        if instance_rot != 0:
            angle_rad = math.radians(instance_rot)
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)
            x, y = x * cos_a - y * sin_a, x * sin_a + y * cos_a

        # Apply translation
        return (instance_pos[0] + x, instance_pos[1] + y)

    def get_all_pin_positions(
        self,
        instance_pos: Tuple[float, float],
        instance_rot: float = 0,
        mirror: str = "",
    ) -> Dict[str, Tuple[float, float]]:
        """
        Get all pin positions for a symbol instance.

        Returns:
            Dict mapping pin number to (x, y) position
        """
        positions = {}
        for pin in self.pins:
            pos = self.get_pin_position(pin.number, instance_pos, instance_rot, mirror)
            if pos:
                positions[pin.number] = pos
        return positions

    @classmethod
    def from_sexp(cls, sexp: SExp) -> LibrarySymbol:
        """Parse from S-expression."""
        name = sexp.get_string(0) or ""

        # Parse properties
        properties = {}
        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0)
            prop_value = prop.get_string(1)
            if prop_name:
                properties[prop_name] = prop_value or ""

        # Parse pins from unit symbols
        pins = []
        for unit_sym in sexp.find_all("symbol"):
            _unit_name = unit_sym.get_string(0) or ""  # noqa: F841
            # Unit symbols have names like "TPA3116D2_1_1"
            # Format: {name}_{unit}_{variant}
            for pin_sexp in unit_sym.find_all("pin"):
                pins.append(LibraryPin.from_sexp(pin_sexp))

        return cls(
            name=name,
            properties=properties,
            pins=pins,
        )


@dataclass
class SymbolLibrary:
    """
    A KiCad symbol library (.kicad_sym file).

    Contains multiple symbol definitions.
    """

    path: str
    symbols: Dict[str, LibrarySymbol] = field(default_factory=dict)

    def get_symbol(self, name: str) -> Optional[LibrarySymbol]:
        """Get a symbol by name."""
        return self.symbols.get(name)

    def __len__(self) -> int:
        return len(self.symbols)

    @classmethod
    def load(cls, path: str) -> SymbolLibrary:
        """Load a symbol library from a .kicad_sym file."""
        text = Path(path).read_text()
        sexp = parse_sexp(text)

        if sexp.tag != "kicad_symbol_lib":
            raise ValueError(f"Not a KiCad symbol library: {path}")

        symbols = {}
        for sym_sexp in sexp.find_all("symbol"):
            sym = LibrarySymbol.from_sexp(sym_sexp)
            symbols[sym.name] = sym

        return cls(path=path, symbols=symbols)


class LibraryManager:
    """
    Manages multiple symbol libraries.

    Provides lookup of symbols by lib_id (e.g., "Device:R", "chorus-revA:TPA3116D2").
    """

    def __init__(self):
        self.libraries: Dict[str, SymbolLibrary] = {}
        self.search_paths: List[str] = []

    def add_library(self, name: str, library: SymbolLibrary) -> None:
        """Add a library with a given name."""
        self.libraries[name] = library

    def load_library(self, path: str, name: Optional[str] = None) -> SymbolLibrary:
        """Load a library from a file."""
        lib = SymbolLibrary.load(path)
        lib_name = name or Path(path).stem
        self.libraries[lib_name] = lib
        return lib

    def add_search_path(self, path: str) -> None:
        """Add a directory to search for libraries."""
        self.search_paths.append(path)

    def get_symbol(self, lib_id: str) -> Optional[LibrarySymbol]:
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
            return self.libraries[lib_name].get_symbol(sym_name)

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
        instance_pos: Tuple[float, float],
        instance_rot: float = 0,
        mirror: str = "",
    ) -> Dict[str, Tuple[float, float]]:
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
