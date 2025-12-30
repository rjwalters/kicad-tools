"""
KiCad Library Discovery Functions

Utilities for finding and searching KiCad symbol libraries.
"""

import re
from pathlib import Path

from .exceptions import LibraryNotFoundError
from .grid import KICAD_SYMBOL_PATHS
from .helpers import _group_pins_by_type
from .models.pin import Pin
from .models.symbol import SymbolInstance


def list_libraries(lib_paths: list[Path] = None) -> list[str]:
    """List all available KiCad symbol libraries.

    Args:
        lib_paths: Optional list of library search paths

    Returns:
        Sorted list of library names (without .kicad_sym extension)

    Example:
        libs = list_libraries()
        print(f"Available libraries: {libs[:10]}...")  # First 10
    """
    if lib_paths is None:
        lib_paths = KICAD_SYMBOL_PATHS

    libraries = set()
    for search_path in lib_paths:
        if search_path.exists():
            for lib_file in search_path.glob("*.kicad_sym"):
                libraries.add(lib_file.stem)

    return sorted(libraries)


def list_symbols(library: str, lib_paths: list[Path] = None) -> list[str]:
    """List all symbols in a KiCad library.

    Args:
        library: Library name (e.g., "Device", "Audio")
        lib_paths: Optional list of library search paths

    Returns:
        Sorted list of symbol names

    Example:
        symbols = list_symbols("Audio")
        print(f"Audio symbols: {symbols}")
    """
    if lib_paths is None:
        lib_paths = KICAD_SYMBOL_PATHS

    lib_file = f"{library}.kicad_sym"

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

    content = lib_path.read_text()

    # Extract symbol names (excluding unit symbols like "Name_0_1")
    symbols = re.findall(r'\(symbol "([^"_][^"]*)"(?!_\d)', content)

    return sorted(set(symbols))


def search_symbols(pattern: str, lib_paths: list[Path] = None) -> list[str]:
    """Search for symbols across all libraries matching a pattern.

    Args:
        pattern: Search pattern with wildcards (e.g., "*LDO*", "PCM*", "*5122*")
        lib_paths: Optional list of library search paths

    Returns:
        List of matching lib_id strings (e.g., ["Audio:PCM5122PW", "Audio:PCM5142PW"])

    Example:
        matches = search_symbols("*5122*")
        for m in matches:
            print(f"  {m}")
    """
    import fnmatch

    if lib_paths is None:
        lib_paths = KICAD_SYMBOL_PATHS

    results = []
    for lib_name in list_libraries(lib_paths):
        try:
            symbols = list_symbols(lib_name, lib_paths)
            for sym_name in symbols:
                if fnmatch.fnmatch(sym_name.lower(), pattern.lower()):
                    results.append(f"{lib_name}:{sym_name}")
        except Exception:
            continue  # Skip libraries that fail to parse

    return sorted(results)


def find_pins(symbol: SymbolInstance, pattern: str) -> list[Pin]:
    """Find pins on a symbol matching a pattern.

    Useful for agents to discover available pins without knowing exact names.

    Args:
        symbol: SymbolInstance to search
        pattern: Pin name pattern with wildcards (e.g., "*CLK*", "GPIO*", "P?0")

    Returns:
        List of matching Pin objects

    Example:
        dac = sch.add_symbol("Audio:PCM5122PW", 100, 100, "U1")
        clock_pins = find_pins(dac, "*CLK*")
        for pin in clock_pins:
            print(f"  {pin.name} (pin {pin.number}): {pin.pin_type}")
    """
    import fnmatch

    matches = []
    for pin in symbol.symbol_def.pins:
        # Match against name or number
        if fnmatch.fnmatch(pin.name.lower(), pattern.lower()):
            matches.append(pin)
        elif fnmatch.fnmatch(pin.number.lower(), pattern.lower()):
            matches.append(pin)

    return matches


def get_pins_by_type(symbol: SymbolInstance, pin_type: str) -> list[Pin]:
    """Get all pins of a specific type from a symbol.

    Args:
        symbol: SymbolInstance to search
        pin_type: Pin type (e.g., "power_in", "input", "output", "bidirectional")

    Returns:
        List of matching Pin objects

    Example:
        power_pins = get_pins_by_type(mcu, "power_in")
        for pin in power_pins:
            print(f"  {pin.name}: needs power connection")
    """
    return [p for p in symbol.symbol_def.pins if p.pin_type == pin_type]


def describe_symbol(symbol: SymbolInstance) -> str:
    """Generate a human-readable description of a symbol and its pins.

    Useful for agents to understand a symbol's interface.

    Args:
        symbol: SymbolInstance to describe

    Returns:
        Multi-line string description

    Example:
        dac = sch.add_symbol("Audio:PCM5122PW", 100, 100, "U1")
        print(describe_symbol(dac))
    """
    lines = [
        f"Symbol: {symbol.reference} ({symbol.symbol_def.lib_id})",
        f"Value: {symbol.value}",
        f"Position: ({symbol.x}, {symbol.y}) rotation={symbol.rotation}deg",
        f"Pins ({len(symbol.symbol_def.pins)}):",
    ]

    # Group pins by type
    grouped = _group_pins_by_type(symbol.symbol_def.pins)

    for group_name, pins in grouped.items():
        if pins:
            lines.append(f"  [{group_name}]")
            for pin in pins:
                pos = symbol.pin_position(pin.name if pin.name else pin.number)
                lines.append(
                    f"    {pin.name or pin.number} (pin {pin.number}): at ({pos[0]:.2f}, {pos[1]:.2f})"
                )

    return "\n".join(lines)
