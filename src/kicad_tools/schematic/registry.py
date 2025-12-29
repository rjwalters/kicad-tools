#!/usr/bin/env python3
"""
KiCad Symbol Registry

A caching registry for KiCad symbols that:
- Caches parsed symbols to avoid re-reading library files
- Provides fuzzy matching and helpful error messages for pin lookups
- Lists available symbols from libraries
- Maps OPL part numbers to symbols

Usage:
    from kicad_symbol_registry import SymbolRegistry

    registry = SymbolRegistry()

    # Get a symbol (cached after first load)
    symbol = registry.get("Device:LED")

    # List all symbols in a library
    symbols = registry.list_library("Device")

    # Search for symbols by name pattern
    matches = registry.search("LDO")

    # Get pin with helpful errors
    pin = registry.get_pin(symbol, "anode")  # Fuzzy matches to "A"
"""

import os
import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Optional


# Default KiCad library paths (platform-specific)
def _default_symbol_paths() -> list[Path]:
    """Get platform-appropriate KiCad symbol paths."""
    paths = []

    # macOS
    paths.append(Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"))

    # Linux
    paths.append(Path("/usr/share/kicad/symbols"))
    paths.append(Path("/usr/local/share/kicad/symbols"))

    # User local (both platforms)
    paths.append(Path.home() / ".local/share/kicad/symbols")

    # Environment variable override
    if "KICAD_SYMBOL_DIR" in os.environ:
        paths.insert(0, Path(os.environ["KICAD_SYMBOL_DIR"]))

    return [p for p in paths if p.exists()]


@dataclass
class Pin:
    """Represents a symbol pin with position and properties."""

    name: str
    number: str
    x: float  # Position relative to symbol center
    y: float
    angle: float  # Pin direction in degrees
    length: float
    pin_type: str = "passive"
    electrical_type: str = ""

    def connection_point(self) -> tuple[float, float]:
        """Get the wire connection point (end of pin)."""
        return (self.x, self.y)

    def __repr__(self) -> str:
        return f"Pin({self.name!r}, num={self.number!r}, type={self.pin_type})"


@dataclass
class SymbolDef:
    """Symbol definition extracted from library."""

    lib_id: str
    name: str
    raw_sexp: str  # Original S-expression for embedding
    pins: list[Pin] = field(default_factory=list)
    description: str = ""
    keywords: str = ""
    footprint: str = ""
    datasheet: str = ""

    @property
    def library(self) -> str:
        """Get library name from lib_id."""
        return self.lib_id.split(":")[0]

    def get_pin(self, name_or_number: str) -> Pin:
        """Get a pin by name or number with fuzzy matching."""
        # Exact match first
        for pin in self.pins:
            if pin.name == name_or_number or pin.number == name_or_number:
                return pin

        # Case-insensitive match
        name_lower = name_or_number.lower()
        for pin in self.pins:
            if pin.name.lower() == name_lower or pin.number.lower() == name_lower:
                return pin

        # Fuzzy match on names
        pin_names = [p.name for p in self.pins if p.name]
        close_names = get_close_matches(name_or_number, pin_names, n=3, cutoff=0.6)

        # Fuzzy match on numbers
        pin_numbers = [p.number for p in self.pins]
        close_numbers = get_close_matches(name_or_number, pin_numbers, n=3, cutoff=0.6)

        # Build helpful error message
        suggestions = []
        if close_names:
            suggestions.append(f"Similar names: {close_names}")
        if close_numbers:
            suggestions.append(f"Similar numbers: {close_numbers}")

        all_pins = [f"{p.name}({p.number})" for p in self.pins]

        error_msg = f"Pin '{name_or_number}' not found in {self.lib_id}."
        if suggestions:
            error_msg += f" {'; '.join(suggestions)}."
        error_msg += f"\nAvailable pins: {all_pins}"

        raise KeyError(error_msg)

    def has_pin(self, name_or_number: str) -> bool:
        """Check if a pin exists."""
        try:
            self.get_pin(name_or_number)
            return True
        except KeyError:
            return False

    def pins_by_type(self, pin_type: str) -> list[Pin]:
        """Get all pins of a specific type (power_in, passive, etc)."""
        return [p for p in self.pins if p.pin_type == pin_type]

    def power_pins(self) -> list[Pin]:
        """Get all power input pins (VCC, VDD, GND, VSS, etc)."""
        return [p for p in self.pins if p.pin_type in ("power_in", "power_out")]

    def get_embedded_sexp(self) -> str:
        """Get the symbol definition formatted for embedding in schematic."""
        lib_name = self.library
        sym_name = self.name
        result = self.raw_sexp

        # Only add library prefix to the MAIN symbol definition, not unit symbols
        result = re.sub(
            rf'\(symbol "{re.escape(sym_name)}"(?!_\d)', f'(symbol "{lib_name}:{sym_name}"', result
        )

        # Also update extends references to use the library prefix
        result = re.sub(r'\(extends "([^"]+)"\)', f'(extends "{lib_name}:\\1")', result)

        # Add proper indentation for embedding in lib_symbols
        lines = result.split("\n")
        indented_lines = []
        for line in lines:
            if line.strip():
                if line.lstrip().startswith('(symbol "') and not line.startswith("\t"):
                    indented_lines.append("\t\t" + line)
                else:
                    indented_lines.append("\t" + line)
        return "\n".join(indented_lines)


@dataclass
class LibraryIndex:
    """Index of symbols in a library file."""

    path: Path
    name: str
    symbols: dict[str, int] = field(default_factory=dict)  # name -> byte offset
    _content: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_file(cls, path: Path) -> "LibraryIndex":
        """Build index from library file."""
        name = path.stem
        content = path.read_text()

        # Find all top-level symbol definitions
        symbols = {}
        # Match (symbol "name" at the start of a line with one tab
        for match in re.finditer(r'^\t\(symbol "([^"]+)"', content, re.MULTILINE):
            sym_name = match.group(1)
            # Skip unit symbols (have _N_N suffix)
            if not re.match(r".+_\d+_\d+$", sym_name):
                symbols[sym_name] = match.start()

        return cls(path=path, name=name, symbols=symbols, _content=content)

    def get_content(self) -> str:
        """Get library file content (cached)."""
        if self._content is None:
            self._content = self.path.read_text()
        return self._content

    def clear_content_cache(self):
        """Clear cached content to free memory."""
        self._content = None


class SymbolRegistry:
    """
    Caching registry for KiCad symbol definitions.

    Features:
    - Lazy loading: libraries are indexed on first access
    - Caching: parsed symbols are cached for reuse
    - Fuzzy search: find symbols by partial name
    - OPL mapping: map Seeed OPL part numbers to symbols
    """

    def __init__(self, lib_paths: Optional[list[Path]] = None):
        """
        Initialize registry.

        Args:
            lib_paths: Custom library search paths (uses defaults if None)
        """
        self.lib_paths = lib_paths or _default_symbol_paths()
        self._library_index: dict[str, LibraryIndex] = {}
        self._symbol_cache: dict[str, SymbolDef] = {}
        self._opl_mapping: dict[str, str] = {}

        # Initialize default OPL mappings
        self._init_opl_mappings()

    def _init_opl_mappings(self):
        """Initialize Seeed OPL part number mappings."""
        self._opl_mapping = {
            # Regulators
            "XC6206P332MR-G": "Regulator_Linear:XC6206PxxxMR",
            "XC6206-3.3V": "Regulator_Linear:AP2204K-1.5",  # Compatible pinout
            # Passive components
            "470R_FB": "Device:FerriteBead_Small",
            # Discretes
            "LED_0603": "Device:LED",
            "R_0603": "Device:R",
            "C_0603": "Device:C",
            "C_0805": "Device:C",
            # Connectors
            "PJ-312": "Connector_Audio:AudioJack3",
            # Oscillators
            "TCXO_24.576MHz": "Oscillator:ASE-xxxMHz",
        }

    def register_opl(self, opl_part: str, lib_id: str):
        """Register an OPL part number to symbol mapping."""
        self._opl_mapping[opl_part] = lib_id

    def resolve_opl(self, opl_part: str) -> str:
        """Resolve an OPL part number to a lib_id."""
        if opl_part in self._opl_mapping:
            return self._opl_mapping[opl_part]
        raise KeyError(
            f"Unknown OPL part: {opl_part}. Known parts: {list(self._opl_mapping.keys())}"
        )

    def _get_library_index(self, lib_name: str) -> LibraryIndex:
        """Get or build library index."""
        if lib_name not in self._library_index:
            lib_file = f"{lib_name}.kicad_sym"

            # Search for library
            lib_path = None
            for search_path in self.lib_paths:
                candidate = search_path / lib_file
                if candidate.exists():
                    lib_path = candidate
                    break

            if lib_path is None:
                available = self.list_libraries()
                close = get_close_matches(lib_name, available, n=5, cutoff=0.4)
                error_msg = f"Library not found: {lib_file}"
                if close:
                    error_msg += f". Similar: {close}"
                raise FileNotFoundError(error_msg)

            self._library_index[lib_name] = LibraryIndex.from_file(lib_path)

        return self._library_index[lib_name]

    def _parse_symbol(self, lib_name: str, sym_name: str) -> SymbolDef:
        """Parse a symbol from library content."""
        index = self._get_library_index(lib_name)
        content = index.get_content()

        # Extract symbol definition
        pattern = rf'\(symbol "{re.escape(sym_name)}"[\s\S]*?(?=\n\t\(symbol "|\n\)$)'
        match = re.search(pattern, content)

        if not match:
            available = list(index.symbols.keys())
            close = get_close_matches(sym_name, available, n=5, cutoff=0.4)
            error_msg = f"Symbol not found: {sym_name} in {lib_name}"
            if close:
                error_msg += f". Similar: {close}"
            raise ValueError(error_msg)

        raw_sexp = match.group(0)

        # Handle symbol inheritance (extends)
        extends_match = re.search(r'\(extends\s+"([^"]+)"\)', raw_sexp)
        if extends_match:
            parent_name = extends_match.group(1)
            parent_pattern = rf'\(symbol "{re.escape(parent_name)}"[\s\S]*?(?=\n\t\(symbol "|\n\)$)'
            parent_match = re.search(parent_pattern, content)
            if parent_match:
                raw_sexp = parent_match.group(0) + "\n" + raw_sexp

        # Parse metadata
        description = ""
        desc_match = re.search(r'\(property "Description"\s+"([^"]*)"', raw_sexp)
        if desc_match:
            description = desc_match.group(1)

        keywords = ""
        kw_match = re.search(r'\(property "ki_keywords"\s+"([^"]*)"', raw_sexp)
        if kw_match:
            keywords = kw_match.group(1)

        footprint = ""
        fp_match = re.search(r'\(property "Footprint"\s+"([^"]*)"', raw_sexp)
        if fp_match:
            footprint = fp_match.group(1)

        datasheet = ""
        ds_match = re.search(r'\(property "Datasheet"\s+"([^"]*)"', raw_sexp)
        if ds_match:
            datasheet = ds_match.group(1)

        # Parse pins
        pins = self._parse_pins(raw_sexp)

        return SymbolDef(
            lib_id=f"{lib_name}:{sym_name}",
            name=sym_name,
            raw_sexp=raw_sexp,
            pins=pins,
            description=description,
            keywords=keywords,
            footprint=footprint,
            datasheet=datasheet,
        )

    def _parse_pins(self, sexp: str) -> list[Pin]:
        """Parse pin definitions from symbol S-expression."""
        pins = []

        # Split on "(pin " to get individual pin sections
        pin_sections = re.split(r"\n\s*\(pin\s+", sexp)[1:]

        for section in pin_sections:
            # Extract pin type and style from start
            type_match = re.match(r"(\w+)\s+(\w+)", section)
            if not type_match:
                continue

            pin_type = type_match.group(1)

            # Extract position: (at X Y ANGLE)
            at_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)", section)
            if not at_match:
                continue

            # Extract length
            length_match = re.search(r"\(length\s+([-\d.]+)\)", section)
            length = float(length_match.group(1)) if length_match else 2.54

            # Extract name
            name_match = re.search(r'\(name\s+"([^"]*)"', section)
            name = name_match.group(1) if name_match else ""

            # Extract number
            number_match = re.search(r'\(number\s+"([^"]*)"', section)
            number = number_match.group(1) if number_match else ""

            if number:  # Must have at least a pin number
                pins.append(
                    Pin(
                        name=name,
                        number=number,
                        x=float(at_match.group(1)),
                        y=float(at_match.group(2)),
                        angle=float(at_match.group(3)),
                        length=length,
                        pin_type=pin_type,
                    )
                )

        return pins

    def get(self, lib_id: str) -> SymbolDef:
        """
        Get a symbol by library:name ID.

        Args:
            lib_id: Symbol identifier (e.g., "Device:LED" or OPL part number)

        Returns:
            SymbolDef with parsed pins and metadata
        """
        # Check if this is an OPL part number
        if lib_id in self._opl_mapping:
            lib_id = self._opl_mapping[lib_id]

        # Check cache
        if lib_id in self._symbol_cache:
            return self._symbol_cache[lib_id]

        # Parse lib:symbol format
        if ":" not in lib_id:
            raise ValueError(f"Invalid lib_id format: {lib_id}. Expected 'Library:Symbol'")

        lib_name, sym_name = lib_id.split(":", 1)

        # Parse and cache
        symbol = self._parse_symbol(lib_name, sym_name)
        self._symbol_cache[lib_id] = symbol

        return symbol

    def list_libraries(self) -> list[str]:
        """List all available libraries."""
        libraries = set()
        for path in self.lib_paths:
            if path.exists():
                for f in path.glob("*.kicad_sym"):
                    libraries.add(f.stem)
        return sorted(libraries)

    def list_library(self, lib_name: str) -> list[str]:
        """List all symbols in a library."""
        index = self._get_library_index(lib_name)
        return sorted(index.symbols.keys())

    def search(self, pattern: str, limit: int = 20) -> list[str]:
        """
        Search for symbols matching a pattern.

        Args:
            pattern: Regex pattern or substring to search for
            limit: Maximum results to return

        Returns:
            List of matching lib_id strings
        """
        results = []
        pattern_re = re.compile(pattern, re.IGNORECASE)

        for lib_name in self.list_libraries():
            try:
                for sym_name in self.list_library(lib_name):
                    if pattern_re.search(sym_name):
                        results.append(f"{lib_name}:{sym_name}")
                        if len(results) >= limit:
                            return results
            except Exception:
                continue  # Skip libraries that fail to parse

        return results

    def search_by_keyword(self, keyword: str, limit: int = 20) -> list[tuple[str, str]]:
        """
        Search for symbols by keyword in their metadata.

        Returns:
            List of (lib_id, description) tuples
        """
        results = []
        keyword_lower = keyword.lower()

        for lib_name in self.list_libraries():
            try:
                index = self._get_library_index(lib_name)
                content = index.get_content()

                for sym_name in index.symbols.keys():
                    # Quick check in content around symbol definition
                    pattern = rf'\(symbol "{re.escape(sym_name)}"[\s\S]*?(?=\n\t\(symbol "|\n\)$)'
                    match = re.search(pattern, content)
                    if match and keyword_lower in match.group(0).lower():
                        # Get the symbol to extract description
                        try:
                            sym = self.get(f"{lib_name}:{sym_name}")
                            results.append((sym.lib_id, sym.description))
                            if len(results) >= limit:
                                return results
                        except Exception:
                            results.append((f"{lib_name}:{sym_name}", ""))

            except Exception:
                continue

        return results

    def clear_cache(self):
        """Clear all cached symbols and library indexes."""
        self._symbol_cache.clear()
        for index in self._library_index.values():
            index.clear_content_cache()
        self._library_index.clear()

    def cache_stats(self) -> dict:
        """Get cache statistics.

        Returns detailed information about cached symbols, indexed libraries,
        and memory usage.
        """
        import sys

        # Calculate memory for content caches
        content_memory = sum(
            sys.getsizeof(idx._content) if idx._content else 0
            for idx in self._library_index.values()
        )

        # Symbols per library
        symbols_per_lib = {name: len(idx.symbols) for name, idx in self._library_index.items()}

        # Which libraries have content cached
        content_cached = [
            name for name, idx in self._library_index.items() if idx._content is not None
        ]

        return {
            "cached_symbols": len(self._symbol_cache),
            "indexed_libraries": len(self._library_index),
            "opl_mappings": len(self._opl_mapping),
            "symbols_per_library": symbols_per_lib,
            "content_cached_libraries": content_cached,
            "content_cache_bytes": content_memory,
            "cached_symbol_names": list(self._symbol_cache.keys()),
        }

    def preload_library(self, lib_name: str):
        """Preload all symbols from a library into cache."""
        for sym_name in self.list_library(lib_name):
            self.get(f"{lib_name}:{sym_name}")


# Global registry instance (singleton pattern)
_global_registry: Optional[SymbolRegistry] = None


def get_registry() -> SymbolRegistry:
    """Get the global symbol registry instance."""
    global _global_registry
    if _global_registry is None:
        _global_registry = SymbolRegistry()
    return _global_registry


def get_symbol(lib_id: str) -> SymbolDef:
    """Convenience function to get a symbol from the global registry."""
    return get_registry().get(lib_id)


# CLI for testing
if __name__ == "__main__":
    import sys

    registry = SymbolRegistry()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python kicad_symbol_registry.py list               - List all libraries")
        print("  python kicad_symbol_registry.py list <library>     - List symbols in library")
        print("  python kicad_symbol_registry.py get <lib:symbol>   - Show symbol details")
        print("  python kicad_symbol_registry.py search <pattern>   - Search for symbols")
        print("  python kicad_symbol_registry.py pins <lib:symbol>  - List all pins")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "list":
        if len(sys.argv) < 3:
            print("Available libraries:")
            for lib in registry.list_libraries():
                print(f"  {lib}")
        else:
            lib_name = sys.argv[2]
            print(f"Symbols in {lib_name}:")
            for sym in registry.list_library(lib_name):
                print(f"  {sym}")

    elif cmd == "get":
        if len(sys.argv) < 3:
            print("Error: specify lib:symbol")
            sys.exit(1)
        lib_id = sys.argv[2]
        sym = registry.get(lib_id)
        print(f"Symbol: {sym.lib_id}")
        print(f"  Description: {sym.description}")
        print(f"  Keywords: {sym.keywords}")
        print(f"  Footprint: {sym.footprint}")
        print(f"  Pins: {len(sym.pins)}")
        for pin in sym.pins:
            print(f"    {pin.name} ({pin.number}): {pin.pin_type} @ ({pin.x}, {pin.y})")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Error: specify search pattern")
            sys.exit(1)
        pattern = sys.argv[2]
        print(f"Searching for '{pattern}':")
        for lib_id in registry.search(pattern):
            print(f"  {lib_id}")

    elif cmd == "pins":
        if len(sys.argv) < 3:
            print("Error: specify lib:symbol")
            sys.exit(1)
        lib_id = sys.argv[2]
        sym = registry.get(lib_id)
        print(f"Pins for {sym.lib_id}:")

        # Group by type
        by_type = {}
        for pin in sym.pins:
            by_type.setdefault(pin.pin_type, []).append(pin)

        for pin_type, pins in sorted(by_type.items()):
            print(f"\n  {pin_type}:")
            for pin in pins:
                print(f"    {pin.name:15} ({pin.number:4}) @ ({pin.x:6.2f}, {pin.y:6.2f})")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
