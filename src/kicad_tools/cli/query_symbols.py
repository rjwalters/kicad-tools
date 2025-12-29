#!/usr/bin/env python3
"""
Query KiCad symbol libraries.

Provides information about symbols including pins, properties, and footprints.
Useful for understanding component pinouts when designing schematics.

Usage:
    python3 scripts/kicad/query-symbols.py lib/symbols/TPA3116D2.kicad_sym
    python3 scripts/kicad/query-symbols.py lib/symbols/*.kicad_sym --list
    python3 scripts/kicad/query-symbols.py lib/symbols/TPA3116D2.kicad_sym --pins
    python3 scripts/kicad/query-symbols.py lib/symbols/TPA3116D2.kicad_sym --symbol TPA3116D2
    python3 scripts/kicad/query-symbols.py lib/symbols/*.kicad_sym --find "amplifier"
    python3 scripts/kicad/query-symbols.py lib/symbols/TPA3116D2.kicad_sym --json
    python3 scripts/kicad/query-symbols.py lib/symbols/TPA3116D2.kicad_sym --compare lib/symbols/TPA3255DDV.kicad_sym
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kicad_tools.core.sexp import SExp, parse_sexp

# Pin type descriptions
PIN_TYPES = {
    "input": "Input",
    "output": "Output",
    "bidirectional": "Bidirectional",
    "tri_state": "Tri-State",
    "passive": "Passive",
    "free": "Free (unspecified)",
    "unspecified": "Unspecified",
    "power_in": "Power Input",
    "power_out": "Power Output",
    "open_collector": "Open Collector",
    "open_emitter": "Open Emitter",
    "no_connect": "No Connect",
}


@dataclass
class SymbolPin:
    """A pin on a symbol."""

    number: str
    name: str
    pin_type: str
    shape: str = "line"
    position: tuple[float, float] = (0, 0)
    rotation: int = 0
    length: float = 2.54

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "SymbolPin":
        """Parse pin from S-expression."""
        # (pin TYPE SHAPE (at X Y ROT) (length L) (name "N") (number "N"))
        pin_type = sexp.get_string(0) or "unspecified"
        shape = sexp.get_string(1) or "line"

        pos = (0.0, 0.0)
        rotation = 0
        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)
            rotation = at.get_int(2) or 0

        length = 2.54
        if len_node := sexp.find("length"):
            length = len_node.get_float(0) or 2.54

        name = ""
        if name_node := sexp.find("name"):
            name = name_node.get_string(0) or ""

        number = ""
        if num_node := sexp.find("number"):
            number = num_node.get_string(0) or ""

        return cls(
            number=number,
            name=name,
            pin_type=pin_type,
            shape=shape,
            position=pos,
            rotation=rotation,
            length=length,
        )

    @property
    def type_description(self) -> str:
        """Get human-readable pin type."""
        return PIN_TYPES.get(self.pin_type, self.pin_type)

    @property
    def direction(self) -> str:
        """Get pin direction based on rotation."""
        dirs = {0: "right", 90: "up", 180: "left", 270: "down"}
        return dirs.get(self.rotation, "unknown")


@dataclass
class Symbol:
    """A symbol definition."""

    name: str
    reference: str = ""
    value: str = ""
    footprint: str = ""
    datasheet: str = ""
    description: str = ""
    keywords: str = ""
    pins: list[SymbolPin] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    in_bom: bool = True
    on_board: bool = True

    @classmethod
    def from_sexp(cls, sexp: SExp) -> "Symbol":
        """Parse symbol from S-expression."""
        name = sexp.get_string(0) or ""

        # Parse standard properties
        reference = ""
        value = ""
        footprint = ""
        datasheet = ""
        description = ""
        keywords = ""
        properties = {}

        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0) or ""
            prop_value = prop.get_string(1) or ""

            if prop_name == "Reference":
                reference = prop_value
            elif prop_name == "Value":
                value = prop_value
            elif prop_name == "Footprint":
                footprint = prop_value
            elif prop_name == "Datasheet":
                datasheet = prop_value
            elif prop_name == "Description":
                description = prop_value
            elif prop_name == "ki_keywords":
                keywords = prop_value
            else:
                properties[prop_name] = prop_value

        # Parse flags
        in_bom = True
        on_board = True
        if bom_node := sexp.find("in_bom"):
            in_bom = bom_node.get_string(0) != "no"
        if board_node := sexp.find("on_board"):
            on_board = board_node.get_string(0) != "no"

        # Parse pins from sub-symbols
        pins = []
        for subsym in sexp.find_all("symbol"):
            for pin in subsym.find_all("pin"):
                pins.append(SymbolPin.from_sexp(pin))

        return cls(
            name=name,
            reference=reference,
            value=value,
            footprint=footprint,
            datasheet=datasheet,
            description=description,
            keywords=keywords,
            pins=pins,
            properties=properties,
            in_bom=in_bom,
            on_board=on_board,
        )

    @property
    def pin_count(self) -> int:
        """Total number of pins."""
        return len(self.pins)

    @property
    def power_pins(self) -> list[SymbolPin]:
        """Get power input/output pins."""
        return [p for p in self.pins if "power" in p.pin_type]

    @property
    def input_pins(self) -> list[SymbolPin]:
        """Get input pins."""
        return [p for p in self.pins if p.pin_type == "input"]

    @property
    def output_pins(self) -> list[SymbolPin]:
        """Get output pins."""
        return [p for p in self.pins if p.pin_type == "output"]

    def get_pin_by_number(self, number: str) -> Optional[SymbolPin]:
        """Get pin by pin number."""
        for pin in self.pins:
            if pin.number == number:
                return pin
        return None

    def get_pin_by_name(self, name: str) -> Optional[SymbolPin]:
        """Get pin by name (case-insensitive partial match)."""
        name_lower = name.lower()
        for pin in self.pins:
            if name_lower in pin.name.lower():
                return pin
        return None

    def get_pins_by_type(self, pin_type: str) -> list[SymbolPin]:
        """Get all pins of a specific type."""
        return [p for p in self.pins if p.pin_type == pin_type]


@dataclass
class SymbolLibrary:
    """A symbol library file."""

    path: Path
    version: int = 0
    generator: str = ""
    symbols: list[Symbol] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "SymbolLibrary":
        """Load a symbol library from file."""
        text = path.read_text(encoding="utf-8")
        sexp = parse_sexp(text)

        if sexp.tag != "kicad_symbol_lib":
            raise ValueError(f"Not a symbol library: {sexp.tag}")

        version = 0
        generator = ""

        if v := sexp.find("version"):
            version = v.get_int(0) or 0
        if g := sexp.find("generator"):
            generator = g.get_string(0) or ""

        symbols = []
        for sym in sexp.find_all("symbol"):
            symbols.append(Symbol.from_sexp(sym))

        return cls(
            path=path,
            version=version,
            generator=generator,
            symbols=symbols,
        )

    def get_symbol(self, name: str) -> Optional[Symbol]:
        """Get symbol by exact name."""
        for sym in self.symbols:
            if sym.name == name:
                return sym
        return None

    def find_symbols(self, query: str) -> list[Symbol]:
        """Find symbols matching query (searches name, description, keywords)."""
        query_lower = query.lower()
        results = []
        for sym in self.symbols:
            if (
                query_lower in sym.name.lower()
                or query_lower in sym.description.lower()
                or query_lower in sym.keywords.lower()
            ):
                results.append(sym)
        return results


def print_library_summary(lib: SymbolLibrary):
    """Print library summary."""
    print(f"\n{'=' * 60}")
    print(f"SYMBOL LIBRARY: {lib.path.name}")
    print(f"{'=' * 60}")
    print(f"Path: {lib.path}")
    print(f"Version: {lib.version}")
    print(f"Generator: {lib.generator}")
    print(f"Symbols: {len(lib.symbols)}")

    if lib.symbols:
        print(f"\n{'─' * 60}")
        print("SYMBOLS:")
        for sym in lib.symbols:
            desc = (
                f" - {sym.description[:50]}..."
                if len(sym.description) > 50
                else f" - {sym.description}"
                if sym.description
                else ""
            )
            print(f"  {sym.name} ({sym.pin_count} pins){desc}")


def print_symbol_detail(sym: Symbol, show_pins: bool = True):
    """Print detailed symbol information."""
    print(f"\n{'=' * 60}")
    print(f"SYMBOL: {sym.name}")
    print(f"{'=' * 60}")

    print("\nProperties:")
    print(f"  Reference: {sym.reference}")
    print(f"  Value: {sym.value}")
    if sym.footprint:
        print(f"  Footprint: {sym.footprint}")
    if sym.datasheet:
        print(f"  Datasheet: {sym.datasheet}")
    if sym.description:
        print(f"  Description: {sym.description}")
    if sym.keywords:
        print(f"  Keywords: {sym.keywords}")

    print("\nFlags:")
    print(f"  In BOM: {'Yes' if sym.in_bom else 'No'}")
    print(f"  On Board: {'Yes' if sym.on_board else 'No'}")

    # Pin summary
    print(f"\nPin Summary ({sym.pin_count} total):")
    type_counts = {}
    for pin in sym.pins:
        type_counts[pin.pin_type] = type_counts.get(pin.pin_type, 0) + 1

    for ptype, count in sorted(type_counts.items()):
        desc = PIN_TYPES.get(ptype, ptype)
        print(f"  {desc}: {count}")

    if show_pins and sym.pins:
        print(f"\n{'─' * 60}")
        print("PINS:")
        print(f"  {'#':<6} {'Name':<20} {'Type':<15} {'Dir':<6}")
        print(f"  {'-' * 6} {'-' * 20} {'-' * 15} {'-' * 6}")

        # Sort pins by number (numeric if possible)
        def pin_sort_key(p):
            try:
                return (0, int(p.number))
            except ValueError:
                return (1, p.number)

        for pin in sorted(sym.pins, key=pin_sort_key):
            print(f"  {pin.number:<6} {pin.name:<20} {pin.type_description:<15} {pin.direction:<6}")


def print_pins_table(sym: Symbol, group_by_type: bool = False):
    """Print pins in tabular format."""
    print(f"\n{'=' * 60}")
    print(f"PINS: {sym.name} ({sym.pin_count} total)")
    print(f"{'=' * 60}")

    if group_by_type:
        # Group by type
        by_type: dict[str, list[SymbolPin]] = {}
        for pin in sym.pins:
            by_type.setdefault(pin.pin_type, []).append(pin)

        for ptype in sorted(by_type.keys()):
            pins = by_type[ptype]
            print(f"\n{PIN_TYPES.get(ptype, ptype)} ({len(pins)}):")
            for pin in sorted(pins, key=lambda p: (p.name, p.number)):
                print(f"  {pin.number:>4}: {pin.name}")
    else:
        # Sort by number
        def pin_sort_key(p):
            try:
                return (0, int(p.number))
            except ValueError:
                return (1, p.number)

        print(f"\n{'#':<6} {'Name':<25} {'Type':<15}")
        print(f"{'-' * 6} {'-' * 25} {'-' * 15}")

        for pin in sorted(sym.pins, key=pin_sort_key):
            print(f"{pin.number:<6} {pin.name:<25} {pin.type_description:<15}")


def compare_symbols(sym1: Symbol, sym2: Symbol):
    """Compare two symbols side by side."""
    print(f"\n{'=' * 70}")
    print(f"COMPARISON: {sym1.name} vs {sym2.name}")
    print(f"{'=' * 70}")

    # Basic comparison
    print(f"\n{'Property':<20} {sym1.name:<25} {sym2.name:<25}")
    print(f"{'-' * 20} {'-' * 25} {'-' * 25}")
    print(f"{'Pin Count':<20} {sym1.pin_count:<25} {sym2.pin_count:<25}")
    print(f"{'Footprint':<20} {sym1.footprint[:23]:<25} {sym2.footprint[:23]:<25}")

    # Pin type comparison
    print(f"\n{'Pin Types':<20} {sym1.name:<25} {sym2.name:<25}")
    print(f"{'-' * 20} {'-' * 25} {'-' * 25}")

    all_types = set()
    for pin in sym1.pins:
        all_types.add(pin.pin_type)
    for pin in sym2.pins:
        all_types.add(pin.pin_type)

    for ptype in sorted(all_types):
        count1 = len([p for p in sym1.pins if p.pin_type == ptype])
        count2 = len([p for p in sym2.pins if p.pin_type == ptype])
        desc = PIN_TYPES.get(ptype, ptype)
        print(f"{desc:<20} {count1:<25} {count2:<25}")

    # Find matching pin names
    names1 = {p.name for p in sym1.pins}
    names2 = {p.name for p in sym2.pins}
    common = names1 & names2
    only1 = names1 - names2
    only2 = names2 - names1

    print(f"\n{'─' * 70}")
    print("Pin Name Analysis:")
    print(f"  Common pins: {len(common)}")
    print(f"  Only in {sym1.name}: {len(only1)}")
    print(f"  Only in {sym2.name}: {len(only2)}")

    if common:
        print(f"\nCommon Pin Names ({len(common)}):")
        for name in sorted(common):
            pin1 = sym1.get_pin_by_name(name)
            pin2 = sym2.get_pin_by_name(name)
            if pin1 and pin2:
                match = "=" if pin1.number == pin2.number else "!="
                print(f"  {name:<20} #{pin1.number:<5} {match} #{pin2.number:<5}")

    if only1:
        print(f"\nOnly in {sym1.name}:")
        for name in sorted(only1):
            pin = sym1.get_pin_by_name(name)
            if pin:
                print(f"  #{pin.number}: {name}")

    if only2:
        print(f"\nOnly in {sym2.name}:")
        for name in sorted(only2):
            pin = sym2.get_pin_by_name(name)
            if pin:
                print(f"  #{pin.number}: {name}")


def print_json_output(libs: list[SymbolLibrary], symbol_filter: Optional[str] = None):
    """Print libraries/symbols as JSON."""
    output = []

    for lib in libs:
        symbols = lib.symbols
        if symbol_filter:
            symbols = [s for s in symbols if symbol_filter.lower() in s.name.lower()]

        lib_data = {
            "path": str(lib.path),
            "version": lib.version,
            "generator": lib.generator,
            "symbols": [
                {
                    "name": s.name,
                    "reference": s.reference,
                    "value": s.value,
                    "footprint": s.footprint,
                    "datasheet": s.datasheet,
                    "description": s.description,
                    "keywords": s.keywords,
                    "pin_count": s.pin_count,
                    "in_bom": s.in_bom,
                    "on_board": s.on_board,
                    "pins": [
                        {
                            "number": p.number,
                            "name": p.name,
                            "type": p.pin_type,
                            "direction": p.direction,
                        }
                        for p in sorted(s.pins, key=lambda x: x.number)
                    ],
                }
                for s in symbols
            ],
        }
        output.append(lib_data)

    print(json.dumps(output if len(output) > 1 else output[0], indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Query KiCad symbol libraries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "libraries", nargs="*", type=Path, help="Symbol library file(s) (.kicad_sym)"
    )
    parser.add_argument(
        "--symbol", "-s", type=str, metavar="NAME", help="Show specific symbol by name"
    )
    parser.add_argument(
        "--find", "-f", type=str, metavar="QUERY", help="Search for symbols matching query"
    )
    parser.add_argument(
        "--compare",
        "-c",
        type=Path,
        metavar="LIB2",
        help="Compare symbol with another library's symbol",
    )

    # Output modes
    parser.add_argument("--list", "-l", action="store_true", help="List all symbols in library")
    parser.add_argument("--pins", "-p", action="store_true", help="Show detailed pin information")
    parser.add_argument("--group-pins", action="store_true", help="Group pins by type")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # Find libraries if not specified
    if not args.libraries:
        parser.print_help()
        print("\nError: No symbol libraries specified")
        print("\nUsage: kicad-lib-symbols path/to/library.kicad_sym")
        return 1

    # Load libraries
    libraries = []
    for lib_path in args.libraries:
        if not lib_path.exists():
            print(f"Warning: Library not found: {lib_path}")
            continue
        try:
            libraries.append(SymbolLibrary.load(lib_path))
        except Exception as e:
            print(f"Warning: Failed to load {lib_path}: {e}")

    if not libraries:
        print("Error: No valid libraries found")
        return 1

    # Handle compare mode
    if args.compare:
        if not args.symbol:
            print("Error: --compare requires --symbol to specify which symbol to compare")
            return 1

        try:
            lib2 = SymbolLibrary.load(args.compare)
        except Exception as e:
            print(f"Error loading comparison library: {e}")
            return 1

        sym1 = None
        for lib in libraries:
            sym1 = lib.get_symbol(args.symbol)
            if sym1:
                break

        if not sym1:
            print(f"Error: Symbol '{args.symbol}' not found in source library")
            return 1

        # Find symbol in compare library (try exact name first, then first symbol)
        sym2 = lib2.get_symbol(args.symbol)
        if not sym2 and lib2.symbols:
            sym2 = lib2.symbols[0]

        if not sym2:
            print("Error: No symbols in comparison library")
            return 1

        compare_symbols(sym1, sym2)
        return 0

    # JSON output
    if args.json:
        print_json_output(libraries, args.symbol or args.find)
        return 0

    # Find mode
    if args.find:
        print(f"\nSearching for: {args.find}")
        found = []
        for lib in libraries:
            for sym in lib.find_symbols(args.find):
                found.append((lib, sym))

        if not found:
            print("No symbols found")
            return 0

        print(f"Found {len(found)} symbol(s):\n")
        for lib, sym in found:
            print(f"  {lib.path.name}: {sym.name}")
            if sym.description:
                print(f"    {sym.description[:70]}")
        return 0

    # Specific symbol
    if args.symbol:
        for lib in libraries:
            sym = lib.get_symbol(args.symbol)
            if sym:
                if args.pins or args.group_pins:
                    print_pins_table(sym, args.group_pins)
                else:
                    print_symbol_detail(sym, show_pins=True)
                return 0

        print(f"Symbol '{args.symbol}' not found in any library")
        return 1

    # List mode or default
    for lib in libraries:
        if args.list or len(libraries) > 1:
            print_library_summary(lib)
        elif lib.symbols:
            # Single library, show first symbol detail
            if args.pins or args.group_pins:
                for sym in lib.symbols:
                    print_pins_table(sym, args.group_pins)
            else:
                for sym in lib.symbols:
                    print_symbol_detail(sym, show_pins=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
