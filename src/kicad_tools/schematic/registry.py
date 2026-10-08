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

import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path

from .exceptions import LibraryNotFoundError, SymbolNotFoundError
from .grid import get_symbol_search_paths


def _default_symbol_paths() -> list[Path]:
    """Get platform-appropriate KiCad symbol paths.

    Thin delegation to :func:`kicad_tools.schematic.grid.get_symbol_search_paths`,
    the single source of truth for symbol library discovery (honors
    ``KICAD_SYMBOL_DIR`` at call time).
    """
    return get_symbol_search_paths()


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
    # Unit number this pin belongs to (0 = common to all units).  Set from
    # the wrapping ``Name_<unit>_<style>`` symbol when present.
    unit: int = 0

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


_SEXP_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')
_SYMBOL_HEAD = re.compile(r'\(symbol\s+"((?:[^"\\]|\\.)*)"')


def _scan_toplevel_symbols_exact(content: str) -> dict[str, tuple[int, int]]:
    """Locate every top-level ``(symbol "name" ...)`` in a ``.kicad_sym`` file.

    Paren-depth-aware and string-aware, so it is independent of the file's
    indentation (tabs in KiCad 8+, two spaces in KiCad 7) and of parens that
    appear inside quoted strings.  Returns ``{name: (start, end)}`` where the
    span covers the balanced ``(symbol ...)`` form.  Unit sub-symbols such as
    ``C_Small_0_1`` live at depth 2 and are therefore never returned.

    Raises:
        ValueError: if the parentheses are unbalanced (truncated/corrupt file).
    """
    spans: dict[str, tuple[int, int]] = {}
    depth = 0
    start = -1
    for m in _SEXP_TOKEN.finditer(content):
        tok = m.group()
        if tok == "(":
            if depth == 1:
                start = m.start()
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced parentheses in symbol library")
            if depth == 1 and start >= 0:
                head = _SYMBOL_HEAD.match(content, start)
                if head:
                    spans[head.group(1)] = (start, m.end())
                start = -1
    if depth != 0:
        raise ValueError("Unbalanced parentheses in symbol library (truncated file?)")
    return spans


_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_SYMBOL_CANDIDATE = re.compile(r'\(symbol\s+"')


def _balance(text: str) -> int:
    """Net paren depth of ``text`` ignoring parens inside quoted strings."""
    t = _STRING.sub("", text) if '"' in text else text
    return t.count("(") - t.count(")")


def scan_toplevel_symbols(content: str) -> dict[str, tuple[int, int]]:
    """Locate top-level ``(symbol ...)`` forms; indentation-agnostic.

    Fast path: candidate ``(symbol "`` heads are found with a C-speed regex
    and their nesting depth is derived from balanced-paren counts of the
    text between them (quoted strings stripped), so no per-token Python loop
    is needed on multi-MB libraries.  Spans are sanity-checked and the whole file
    must balance; on any doubt the exact token-by-token scanner is used
    instead.  Raises ``ValueError`` for unbalanced input.
    """
    heads: list[int] = []
    depth = 0
    last = 0
    for m in _SYMBOL_CANDIDATE.finditer(content):
        c = m.start()
        seg = content[last:c]
        # A candidate inside a quoted string splits it: skip those.
        if (seg.count('"') - seg.count('\\"')) % 2:
            continue
        depth += _balance(seg)
        last = c
        if depth == 1:
            heads.append(c)
    if not heads:
        return _scan_toplevel_symbols_exact(content)

    close = content.rstrip().rfind(")")
    spans: dict[str, tuple[int, int]] = {}
    for i, start in enumerate(heads):
        limit = heads[i + 1] if i + 1 < len(heads) else close
        end = limit
        while end > start and content[end - 1].isspace():
            end -= 1
        head = _SYMBOL_HEAD.match(content, start)
        if not head or end <= start or content[end - 1] != ")":
            return _scan_toplevel_symbols_exact(content)
        spans[head.group(1)] = (start, end)
    if _balance(content) != 0:
        return _scan_toplevel_symbols_exact(content)
    return spans


@dataclass
class LibraryIndex:
    """Index of symbols in a library file."""

    path: Path
    name: str
    symbols: dict[str, int] = field(default_factory=dict)  # name -> byte offset
    spans: dict[str, tuple[int, int]] = field(default_factory=dict, repr=False)
    _content: str | None = field(default=None, repr=False)

    @classmethod
    def from_file(cls, path: Path) -> "LibraryIndex":
        """Build index from library file (indentation-agnostic)."""
        name = path.stem
        content = path.read_text()
        try:
            spans = scan_toplevel_symbols(content)
        except ValueError as e:
            raise ValueError(f"Cannot parse symbol library {path}: {e}") from e
        # Unit-style names (``Name_<unit>_<style>``) are never real parents.
        spans = {n: sp for n, sp in spans.items() if not re.match(r".+_\d+_\d+$", n)}
        symbols = {n: sp[0] for n, sp in spans.items()}
        return cls(path=path, name=name, symbols=symbols, spans=spans, _content=content)

    def get_symbol_text(self, sym_name: str) -> str | None:
        """Exact S-expression text of a top-level symbol, or None."""
        span = self.spans.get(sym_name)
        if span is None:
            return None
        return self.get_content()[span[0] : span[1]]

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

    def __init__(self, lib_paths: list[Path] | None = None):
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
            "XC6206-3.3V": "Regulator_Linear:XC6206PxxxMR",  # SOT-23-3 (3-pin)
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
                err = LibraryNotFoundError(lib_file, list(self.lib_paths))
                if close:
                    err.args = (f"{err.args[0]}\n\nSimilar libraries: {close}",)
                raise err

            self._library_index[lib_name] = LibraryIndex.from_file(lib_path)

        return self._library_index[lib_name]

    def _parse_symbol(self, lib_name: str, sym_name: str) -> SymbolDef:
        """Parse a symbol from library content."""
        index = self._get_library_index(lib_name)

        # Exact top-level lookup via paren-depth scan -- never a fuzzy/regex
        # match, so a miss can never return some other symbol (issue #6218).
        raw_sexp = index.get_symbol_text(sym_name)

        if raw_sexp is None:
            available = list(index.symbols.keys())
            close = get_close_matches(sym_name, available, n=5, cutoff=0.4)
            raise SymbolNotFoundError(
                sym_name, f"{lib_name}.kicad_sym", available_symbols=available, suggestions=close
            )

        # Handle symbol inheritance (extends): the parent is a direct child
        # form of this symbol, not any "(extends" later in the file.
        extends_match = re.match(r'\(symbol\s+"[^"]*"\s*\(extends\s+"([^"]+)"\)', raw_sexp)
        if extends_match:
            parent_name = extends_match.group(1)
            parent_text = index.get_symbol_text(parent_name)
            if parent_text is None:
                raise SymbolNotFoundError(
                    parent_name,
                    f"{lib_name}.kicad_sym",
                    available_symbols=list(index.symbols.keys()),
                    suggestions=get_close_matches(parent_name, list(index.symbols), n=5),
                )
            raw_sexp = parent_text + "\n" + raw_sexp

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
        """Parse pin definitions from symbol S-expression.

        Multi-unit symbols in KiCad nest pins inside child ``symbol``
        nodes named ``<Name>_<unit>_<style>`` (e.g. ``LM393_2_1``).  This
        parser walks the raw text and remembers which unit wrapper the
        most recent pin section appeared under so each :class:`Pin` is
        tagged with the correct unit number.  Unit ``0`` is reserved for
        shared/common pins (e.g. package-wide power pins declared at the
        top level or in a ``Name_0_<style>`` wrapper).  This is what lets
        multi-unit symbols (e.g. LM393's pins 4 & 8 living on unit 3)
        round-trip through :class:`SymbolInstance.pin_position` without
        returning a phantom unit-1 position for an off-unit pin (issue
        #3346) while preserving the validator's ability to filter pins
        by which units are actually placed (issue #3349).
        """
        pins = []

        # Walk text positions of unit-symbol wrappers so we can map each
        # pin section's offset back to the unit that owns it.  This is a
        # lightweight alternative to a full SExp parse and stays
        # consistent with the existing regex-based approach used here.
        unit_marker = re.compile(r'\(symbol\s+"[^"]+_(\d+)_\d+"')
        unit_positions: list[tuple[int, int]] = [
            (m.start(), int(m.group(1))) for m in unit_marker.finditer(sexp)
        ]

        def _unit_for(pos: int) -> int:
            current = 0
            for start, unit in unit_positions:
                if start < pos:
                    current = unit
                else:
                    break
            return current

        # Use the same anchor pattern for both splitting and offset
        # tracking so the two stay aligned.  ``pin_names`` properties
        # do not start with a newline so they're correctly excluded.
        section_starts = [m.start() for m in re.finditer(r"\n\s*\(pin\s+", sexp)]
        pin_sections = re.split(r"\n\s*\(pin\s+", sexp)[1:]

        for section, offset in zip(pin_sections, section_starts, strict=False):
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
                        unit=_unit_for(offset),
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

                for sym_name in index.symbols:
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
_global_registry: SymbolRegistry | None = None


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
