#!/usr/bin/env python3
"""
KiCad Schematic Helper Library

Programmatically generate KiCad 8/9 schematic files with proper symbol
embedding, pin position calculation, and wire routing.

Features:
    - Automatic grid snapping (1.27mm default, configurable)
    - Symbol library extraction with pin position calculation
    - Orthogonal wire routing helpers
    - Power symbol and rail management

Usage:
    from kicad_sch_helper import Schematic, SnapMode, GridSize

    # Create schematic with auto-snapping (default)
    sch = Schematic("My Schematic", "2025-01", "A")

    # Or with specific grid settings
    sch = Schematic(
        "My Schematic", "2025-01", "A",
        grid=GridSize.SCH_FINE.value,  # 0.635mm (25 mil)
        snap_mode=SnapMode.STRICT      # Snap and warn on off-grid
    )

    # Add a symbol - coordinates auto-snap to grid
    dac = sch.add_symbol("Audio:PCM5122PW", x=100, y=100, ref="U1")

    # Get pin position (already grid-aligned from symbol placement)
    sck_pos = dac.pin_position("SCK")

    # Add a wire - endpoints auto-snap
    sch.add_wire(sck_pos, (50, sck_pos[1]))

    # Bypass snapping for specific elements
    sch.add_junction(x=1.5, y=2.5, snap=False)

    # Write schematic
    sch.write("output.kicad_sch")

Grid Snapping Modes:
    SnapMode.OFF    - No snapping, no warnings
    SnapMode.WARN   - Don't snap but warn on off-grid coordinates
    SnapMode.AUTO   - Silently snap to grid (default)
    SnapMode.STRICT - Snap and warn if original was off-grid

Available Grid Sizes:
    GridSize.SCH_COARSE     - 2.54mm (100 mil)
    GridSize.SCH_STANDARD   - 1.27mm (50 mil) - default
    GridSize.SCH_FINE       - 0.635mm (25 mil)
    GridSize.SCH_ULTRA_FINE - 0.254mm (10 mil)
    GridSize.PCB_*          - PCB grids (0.1mm to 1.0mm)
"""

import math
import re
import uuid
import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# S-expression support for structured output
from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import (
    at,
    hier_label_node,
    junction_node,
    label_node,
    pin_uuid_node,
    sheet_instances,
    symbol_instances_node,
    symbol_property_node,
    text_node,
    title_block,
    uuid_node,
    wire_node,
)
from kicad_tools.sexp.builders import fmt as sexp_fmt

# Symbol registry for caching and better error messages
try:
    from kicad_tools.schematic.registry import get_registry as _get_symbol_registry

    _REGISTRY_AVAILABLE = True
except ImportError:
    _REGISTRY_AVAILABLE = False
    _get_symbol_registry = None

import logging
from difflib import SequenceMatcher

# =============================================================================
# Logging Configuration (Agent-Focused)
# =============================================================================

# Create a logger for the KiCad helper module
_logger = logging.getLogger("kicad_sch_helper")
_logger.addHandler(logging.NullHandler())  # Default: no output


def enable_verbose(level: str = "INFO", format: str = None) -> None:
    """Enable verbose logging for debugging.

    This helps agents understand what operations are being performed
    and diagnose issues with schematic generation.

    Args:
        level: Logging level - "DEBUG", "INFO", "WARNING", "ERROR"
        format: Optional custom format string

    Example:
        # Enable verbose output before problematic operations
        enable_verbose("DEBUG")

        sch = Schematic("Test")
        sch.add_symbol("Device:C", 100, 100, "C1")  # Will log operation

        # Disable when done
        disable_verbose()
    """
    _logger.setLevel(getattr(logging, level.upper()))

    # Remove existing handlers
    for handler in _logger.handlers[:]:
        if not isinstance(handler, logging.NullHandler):
            _logger.removeHandler(handler)

    # Add console handler
    handler = logging.StreamHandler()
    handler.setLevel(getattr(logging, level.upper()))

    if format is None:
        format = "[%(levelname)s] %(message)s"

    handler.setFormatter(logging.Formatter(format))
    _logger.addHandler(handler)


def disable_verbose() -> None:
    """Disable verbose logging."""
    _logger.setLevel(logging.WARNING)
    for handler in _logger.handlers[:]:
        if not isinstance(handler, logging.NullHandler):
            _logger.removeHandler(handler)


def _log_debug(msg: str) -> None:
    """Log a debug message."""
    _logger.debug(msg)


def _log_info(msg: str) -> None:
    """Log an info message."""
    _logger.info(msg)


def _log_warning(msg: str) -> None:
    """Log a warning message."""
    _logger.warning(msg)


# =============================================================================
# Error Handling and Suggestions
# =============================================================================


def _string_similarity(a: str, b: str) -> float:
    """Calculate similarity ratio between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _find_similar(
    target: str, candidates: list[str], threshold: float = 0.4, max_results: int = 5
) -> list[str]:
    """Find similar strings from a list of candidates.

    Args:
        target: The string to match against
        candidates: List of candidate strings
        threshold: Minimum similarity score (0.0 to 1.0)
        max_results: Maximum number of suggestions to return

    Returns:
        List of similar strings, sorted by similarity (best first)
    """
    scored = []
    target_lower = target.lower()

    for candidate in candidates:
        # Exact prefix match gets highest score
        if candidate.lower().startswith(target_lower):
            score = 0.9 + (len(target) / len(candidate)) * 0.1
        elif target_lower.startswith(candidate.lower()):
            score = 0.85
        else:
            score = _string_similarity(target, candidate)

        if score >= threshold:
            scored.append((candidate, score))

    scored.sort(key=lambda x: -x[1])
    return [s[0] for s in scored[:max_results]]


# Common pin name aliases (maps alternate names to canonical names)
# This helps agents find pins even when using slightly different naming conventions
PIN_ALIASES = {
    # Power pins
    "vcc": ["vdd", "v+", "vin", "vcc", "vbat", "vsup", "vpwr", "avcc", "dvcc", "vddio"],
    "vdd": ["vcc", "v+", "vin", "vdd", "vbat", "vsup", "vpwr", "avdd", "dvdd", "vddio"],
    "gnd": ["vss", "v-", "gnda", "gndd", "agnd", "dgnd", "ground", "com", "vee", "pgnd"],
    "vss": ["gnd", "v-", "gnda", "gndd", "agnd", "dgnd", "ground", "vee"],
    "avcc": ["avdd", "vcc", "vdd", "va"],
    "dvcc": ["dvdd", "vcc", "vdd", "vd"],
    "agnd": ["gnda", "gnd", "vss", "va-"],
    "dgnd": ["gndd", "gnd", "vss", "vd-"],
    # Enable/Chip Select pins
    "en": ["enable", "ena", "ce", "chip_enable", "~en", "en/", "oe", "stby"],
    "enable": ["en", "ena", "ce", "oe"],
    "ce": ["en", "enable", "cs", "chip_enable"],
    "cs": ["~cs", "cs/", "ncs", "ss", "~ss", "nss", "ce", "chip_select"],
    "ss": ["~ss", "nss", "cs", "~cs", "ncs", "slave_select"],
    "oe": ["~oe", "noe", "output_enable", "en"],
    # I2C pins
    "sda": ["data", "sdio", "i2c_sda", "twi_sda", "ser_data"],
    "scl": ["i2c_scl", "twi_scl", "i2c_clk", "ser_clk"],
    # SPI pins
    "sck": ["sclk", "clk", "clock", "spi_clk", "ser_clk"],
    "sclk": ["sck", "clk", "clock", "spi_clk"],
    "mosi": ["sdi", "din", "data_in", "si", "spi_mosi", "dout"],
    "miso": ["sdo", "dout", "data_out", "so", "spi_miso", "din"],
    "sdi": ["mosi", "din", "si", "data_in"],
    "sdo": ["miso", "dout", "so", "data_out"],
    # UART/Serial pins
    "tx": ["txd", "uart_tx", "ser_tx", "dout", "td"],
    "rx": ["rxd", "uart_rx", "ser_rx", "din", "rd"],
    "txd": ["tx", "uart_tx", "dout"],
    "rxd": ["rx", "uart_rx", "din"],
    "rts": ["~rts", "nrts", "uart_rts"],
    "cts": ["~cts", "ncts", "uart_cts"],
    # Clock pins
    "clk": ["clock", "sclk", "sck", "bclk", "mclk", "clkin", "xin", "osc_in"],
    "mclk": ["master_clk", "clk", "clock", "xtal"],
    "bclk": ["bit_clk", "sclk", "i2s_bclk"],
    "lrclk": ["wclk", "ws", "lrck", "i2s_lrclk", "frame_sync", "fs"],
    "wclk": ["lrclk", "ws", "lrck", "word_clk"],
    # Reset pins
    "rst": ["reset", "~reset", "nreset", "~rst", "rstn", "mrst", "por"],
    "reset": ["rst", "~reset", "nreset", "~rst", "rstn"],
    "nreset": ["~reset", "rstn", "rst", "reset"],
    # Interrupt pins
    "int": ["~int", "irq", "~irq", "interrupt", "intr"],
    "irq": ["int", "~int", "interrupt", "~irq"],
    # Audio I2S pins
    "dout": ["sdo", "data_out", "i2s_dout", "sdout"],
    "din": ["sdi", "data_in", "i2s_din", "sdin"],
}


def _expand_pin_aliases(name: str) -> list[str]:
    """Get a list of possible alias names for a pin."""
    name_lower = name.lower().replace("~", "").replace("/", "")
    aliases = PIN_ALIASES.get(name_lower, [])
    return [name] + [a for a in aliases if a != name_lower]


def _group_pins_by_type(pins: list) -> dict[str, list]:
    """Group pins by their electrical type for organized display."""
    groups = {
        "power": [],
        "input": [],
        "output": [],
        "bidirectional": [],
        "passive": [],
        "other": [],
    }

    type_mapping = {
        "power_in": "power",
        "power_out": "power",
        "input": "input",
        "output": "output",
        "bidirectional": "bidirectional",
        "tri_state": "output",
        "passive": "passive",
        "unspecified": "other",
        "open_collector": "output",
        "open_emitter": "output",
        "no_connect": "other",
    }

    for pin in pins:
        group = type_mapping.get(pin.pin_type, "other")
        groups[group].append(pin)

    # Remove empty groups
    return {k: v for k, v in groups.items() if v}


def _format_pin_list(pins: list, indent: str = "  ") -> str:
    """Format a list of pins for display in error messages."""
    if not pins:
        return f"{indent}(none)"

    lines = []
    for pin in pins:
        if pin.name and pin.name != pin.number:
            lines.append(f"{indent}{pin.name} (pin {pin.number})")
        else:
            lines.append(f"{indent}pin {pin.number}")
    return "\n".join(lines)


class PinNotFoundError(ValueError):
    """Raised when a pin cannot be found on a symbol."""

    def __init__(
        self, pin_name: str, symbol_name: str, available_pins: list, suggestions: list[str] = None
    ):
        self.pin_name = pin_name
        self.symbol_name = symbol_name
        self.available_pins = available_pins
        self.suggestions = suggestions or []

        # Build error message
        msg_parts = [f"Pin '{pin_name}' not found on {symbol_name}"]

        if self.suggestions:
            msg_parts.append(f"\n\nDid you mean: {', '.join(self.suggestions)}?")

        # Group pins by type for organized display
        grouped = _group_pins_by_type(available_pins)
        if grouped:
            msg_parts.append("\n\nAvailable pins:")
            for group_name, pins in grouped.items():
                if pins:
                    msg_parts.append(f"\n  [{group_name}]")
                    msg_parts.append("\n" + _format_pin_list(pins, "    "))

        super().__init__("".join(msg_parts))


class SymbolNotFoundError(ValueError):
    """Raised when a symbol cannot be found in a library."""

    def __init__(
        self,
        symbol_name: str,
        library_file: str,
        available_symbols: list[str] = None,
        suggestions: list[str] = None,
    ):
        self.symbol_name = symbol_name
        self.library_file = library_file
        self.available_symbols = available_symbols or []
        self.suggestions = suggestions or []

        msg_parts = [f"Symbol '{symbol_name}' not found in {library_file}"]

        if self.suggestions:
            msg_parts.append(f"\n\nDid you mean: {', '.join(self.suggestions)}?")
        elif self.available_symbols:
            # Show first 10 available symbols
            shown = self.available_symbols[:10]
            msg_parts.append(f"\n\nAvailable symbols ({len(self.available_symbols)} total):")
            for sym in shown:
                msg_parts.append(f"\n  {sym}")
            if len(self.available_symbols) > 10:
                msg_parts.append(f"\n  ... and {len(self.available_symbols) - 10} more")

        super().__init__("".join(msg_parts))


class LibraryNotFoundError(FileNotFoundError):
    """Raised when a KiCad library file cannot be found."""

    def __init__(self, library_name: str, searched_paths: list[Path]):
        self.library_name = library_name
        self.searched_paths = searched_paths

        msg_parts = [f"Library '{library_name}' not found"]
        msg_parts.append("\n\nSearched paths:")
        for path in searched_paths:
            exists_marker = "" if path.exists() else " (not found)"
            msg_parts.append(f"\n  {path}{exists_marker}")

        msg_parts.append("\n\nTo fix:")
        msg_parts.append("\n  1. Verify library name spelling")
        msg_parts.append("\n  2. Check if KiCad is installed at the expected location")
        msg_parts.append("\n  3. Add custom library paths via lib_paths parameter")

        super().__init__("".join(msg_parts))


# =============================================================================
# Grid Constants and Snapping
# =============================================================================


class GridSize(Enum):
    """Standard KiCad grid sizes."""

    # Schematic grids (in mm)
    SCH_COARSE = 2.54  # 100 mil - large component spacing
    SCH_STANDARD = 1.27  # 50 mil - standard schematic grid
    SCH_FINE = 0.635  # 25 mil - fine placement
    SCH_ULTRA_FINE = 0.254  # 10 mil - text/label alignment

    # PCB grids (in mm)
    PCB_COARSE = 1.0  # 1mm - coarse placement
    PCB_STANDARD = 0.5  # 0.5mm - standard placement
    PCB_FINE = 0.25  # 0.25mm - fine placement
    PCB_ULTRA_FINE = 0.1  # 0.1mm - precision placement


# Default grid for schematic operations
DEFAULT_GRID = GridSize.SCH_STANDARD.value  # 1.27mm


def snap_to_grid(value: float, grid: float = DEFAULT_GRID) -> float:
    """Snap a coordinate to the nearest grid point.

    Args:
        value: Coordinate value in mm
        grid: Grid spacing in mm (default: 1.27mm for schematics)

    Returns:
        Snapped coordinate value, rounded to 2 decimal places
    """
    snapped = round(value / grid) * grid
    return round(snapped, 2)


def snap_point(point: tuple[float, float], grid: float = DEFAULT_GRID) -> tuple[float, float]:
    """Snap a point (x, y) to the nearest grid intersection.

    Args:
        point: (x, y) coordinate tuple
        grid: Grid spacing in mm

    Returns:
        Snapped (x, y) tuple
    """
    return (snap_to_grid(point[0], grid), snap_to_grid(point[1], grid))


def is_on_grid(value: float, grid: float = DEFAULT_GRID, tolerance: float = 0.001) -> bool:
    """Check if a coordinate is on the grid.

    Args:
        value: Coordinate value to check
        grid: Grid spacing in mm
        tolerance: Allowed deviation from grid (default: 0.001mm)

    Returns:
        True if value is within tolerance of a grid point
    """
    remainder = abs(value % grid)
    return remainder < tolerance or (grid - remainder) < tolerance


def check_grid_alignment(
    point: tuple[float, float], grid: float = DEFAULT_GRID, context: str = "", warn: bool = True
) -> bool:
    """Check if a point is on the grid, optionally warning if not.

    Args:
        point: (x, y) coordinate tuple
        grid: Grid spacing in mm
        context: Context string for warning message
        warn: Whether to emit a warning if off-grid

    Returns:
        True if point is on grid
    """
    x_ok = is_on_grid(point[0], grid)
    y_ok = is_on_grid(point[1], grid)

    if not (x_ok and y_ok) and warn:
        snapped = snap_point(point, grid)
        ctx = f" ({context})" if context else ""
        warnings.warn(
            f"Off-grid coordinate{ctx}: ({point[0]}, {point[1]}) -> "
            f"nearest grid: ({snapped[0]}, {snapped[1]})",
            stacklevel=3,
        )

    return x_ok and y_ok


# Default KiCad library paths
KICAD_SYMBOL_PATHS = [
    Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
    Path("/usr/share/kicad/symbols"),
    Path.home() / ".local/share/kicad/symbols",
]


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

    def connection_point(self) -> tuple[float, float]:
        """Get the wire connection point (end of pin)."""
        return (self.x, self.y)

    @classmethod
    def from_sexp(cls, node: SExp) -> "Pin":
        """Parse a pin from its S-expression node.

        Expected format:
            (pin TYPE STYLE (at X Y ANGLE) (length L) (name "N" ...) (number "N" ...))
        """
        # First atom after "pin" is the type
        pin_type = node.children[0].value if node.children else "passive"

        # Find (at X Y ANGLE)
        at_node = node.get("at")
        if at_node and len(at_node.children) >= 3:
            x = float(at_node.children[0].value)
            y = float(at_node.children[1].value)
            angle = float(at_node.children[2].value)
        else:
            x, y, angle = 0, 0, 0

        # Find (length L)
        length_node = node.get("length")
        length = float(length_node.children[0].value) if length_node else 2.54

        # Find (name "NAME" ...)
        name_node = node.get("name")
        name = str(name_node.children[0].value) if name_node else ""

        # Find (number "NUM" ...)
        number_node = node.get("number")
        number = str(number_node.children[0].value) if number_node else ""

        return cls(
            name=name, number=number, x=x, y=y, angle=angle, length=length, pin_type=pin_type
        )


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
        - Main symbol name: (symbol "Name" ...) → (symbol "Lib:Name" ...)
        - Extends references: (extends "Parent") → (extends "Lib:Parent")
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
                        # e.g., "AP2204K-1.5_0_1" → "Lib:AP2204K-1.5_0_1"
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


def _fmt_coord(val: float) -> str:
    """Format a coordinate value with consistent precision.

    Rounds to 2 decimal places and removes trailing zeros for cleaner output.
    This ensures wire endpoints match pin positions exactly.
    """
    rounded = round(val, 2)
    # Format with up to 2 decimal places, remove trailing zeros
    if rounded == int(rounded):
        return str(int(rounded))
    else:
        return f"{rounded:.2f}".rstrip("0").rstrip(".")


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
        uuid_node = node.get("uuid")
        uuid_str = str(uuid_node.get_first_atom()) if uuid_node else str(uuid.uuid4())

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

        uuid_node = node.get("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else str(uuid.uuid4())

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

        uuid_node = node.get("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else str(uuid.uuid4())

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

        uuid_node = node.get("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else str(uuid.uuid4())

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

        uuid_node = node.get("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else str(uuid.uuid4())

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

    _symbol_def: Optional[SymbolDef] = field(default=None, repr=False)

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
        uuid_node = node.get("uuid")
        uuid_str = str(uuid_node.get_first_atom()) if uuid_node else str(uuid.uuid4())

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


class SnapMode(Enum):
    """Grid snapping behavior modes."""

    OFF = "off"  # No snapping, no warnings
    WARN = "warn"  # Don't snap but warn on off-grid coordinates
    AUTO = "auto"  # Automatically snap to grid (default)
    STRICT = "strict"  # Snap and warn if original was off-grid


class Schematic:
    """KiCad schematic document."""

    def __init__(
        self,
        title: str,
        date: str = "2025-01",
        revision: str = "A",
        company: str = "",
        comment1: str = "",
        comment2: str = "",
        paper: str = "A4",
        project_name: str = "project",
        sheet_uuid: str = None,
        parent_uuid: str = None,
        page: str = "1",
        grid: float = DEFAULT_GRID,
        snap_mode: SnapMode = SnapMode.AUTO,
    ):
        self.title = title
        self.date = date
        self.revision = revision
        self.company = company
        self.comment1 = comment1
        self.comment2 = comment2
        self.paper = paper
        self.project_name = project_name
        self.sheet_uuid = sheet_uuid or str(uuid.uuid4())
        self.parent_uuid = parent_uuid
        self.page = page

        # Grid configuration
        self.grid = grid
        self.snap_mode = snap_mode

        self.symbols: list[SymbolInstance] = []
        self.power_symbols: list[PowerSymbol] = []
        self.wires: list[Wire] = []
        self.junctions: list[Junction] = []
        self.labels: list[Label] = []
        self.hier_labels: list[HierarchicalLabel] = []
        self.text_notes: list[tuple[str, float, float]] = []

        # Cache for loaded symbol definitions
        self._symbol_defs: dict[str, SymbolDef] = {}
        self._pwr_counter = 1

        # Embedded lib_symbols from loaded schematics (preserved for round-trip)
        self._embedded_lib_symbols: dict[str, SExp] = {}

    @classmethod
    def load(cls, path: str | Path) -> "Schematic":
        """Load a schematic from a .kicad_sch file.

        This enables round-trip editing: load → modify → save.

        Args:
            path: Path to the .kicad_sch file

        Returns:
            A Schematic instance populated with all elements from the file

        Example:
            sch = Schematic.load("power.kicad_sch")
            sch.add_symbol("Device:R", 100, 100, "R5", "10k")
            sch.write("power.kicad_sch")
        """
        from kicad_sexp import parse_file

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Schematic file not found: {path}")

        doc = parse_file(path)
        return cls._from_sexp(doc)

    @classmethod
    def _from_sexp(cls, doc: SExp) -> "Schematic":
        """Create a Schematic from a parsed S-expression tree.

        This is the internal method that does the actual parsing.
        """
        # Extract title block info
        title = ""
        date = ""
        revision = ""
        company = ""
        comment1 = ""
        comment2 = ""

        tb = doc.get("title_block")
        if tb:
            title_node = tb.get("title")
            if title_node:
                title = str(title_node.get_first_atom() or "")
            date_node = tb.get("date")
            if date_node:
                date = str(date_node.get_first_atom() or "")
            rev_node = tb.get("rev")
            if rev_node:
                revision = str(rev_node.get_first_atom() or "")
            company_node = tb.get("company")
            if company_node:
                company = str(company_node.get_first_atom() or "")
            # Comments are numbered
            for comment_node in tb.find_all("comment"):
                atoms = comment_node.get_atoms()
                if len(atoms) >= 2:
                    num = int(atoms[0])
                    text = str(atoms[1])
                    if num == 1:
                        comment1 = text
                    elif num == 2:
                        comment2 = text

        # Get paper size
        paper_node = doc.get("paper")
        paper = str(paper_node.get_first_atom()) if paper_node else "A4"

        # Get UUID
        uuid_node = doc.get("uuid")
        sheet_uuid = str(uuid_node.get_first_atom()) if uuid_node else str(uuid.uuid4())

        # Parse lib_symbols to get embedded symbol definitions
        embedded_lib_symbols: dict[str, SExp] = {}
        lib_symbols_node = doc.get("lib_symbols")
        if lib_symbols_node:
            for sym_node in lib_symbols_node.children:
                if sym_node.name == "symbol":
                    sym_name = str(sym_node.get_first_atom())
                    embedded_lib_symbols[sym_name] = sym_node

        # Create schematic instance with minimal init
        # We disable snapping for loaded schematics to preserve coordinates
        sch = cls(
            title=title,
            date=date,
            revision=revision,
            company=company,
            comment1=comment1,
            comment2=comment2,
            paper=paper,
            sheet_uuid=sheet_uuid,
            snap_mode=SnapMode.OFF,  # Preserve original coordinates
        )

        # Store embedded lib_symbols for round-trip
        sch._embedded_lib_symbols = embedded_lib_symbols

        # Parse sheet_instances to get project name and parent info
        sheet_instances_node = doc.get("sheet_instances")
        if sheet_instances_node:
            project_node = sheet_instances_node.get("project")
            if project_node:
                sch.project_name = str(project_node.get_first_atom() or "project")
                path_node = project_node.get("path")
                if path_node:
                    page_node = path_node.get("page")
                    if page_node:
                        sch.page = str(page_node.get_first_atom() or "1")

        # Parse placed symbols (those with lib_id)
        for child in doc.children:
            if child.name == "symbol" and child.get("lib_id"):
                if PowerSymbol.is_power_symbol(child):
                    pwr = PowerSymbol.from_sexp(child)
                    sch.power_symbols.append(pwr)
                else:
                    sym = SymbolInstance.from_sexp(
                        child, symbol_defs=sch._symbol_defs, lib_symbols=embedded_lib_symbols
                    )
                    sch.symbols.append(sym)
                    # Cache the symbol def
                    sch._symbol_defs[sym.symbol_def.lib_id] = sym.symbol_def

        # Parse wires
        for child in doc.children:
            if child.name == "wire":
                sch.wires.append(Wire.from_sexp(child))

        # Parse junctions
        for child in doc.children:
            if child.name == "junction":
                sch.junctions.append(Junction.from_sexp(child))

        # Parse labels
        for child in doc.children:
            if child.name == "label":
                sch.labels.append(Label.from_sexp(child))

        # Parse hierarchical labels
        for child in doc.children:
            if child.name == "hierarchical_label":
                sch.hier_labels.append(HierarchicalLabel.from_sexp(child))

        # Parse text notes
        for child in doc.children:
            if child.name == "text":
                text = str(child.get_first_atom() or "")
                at_node = child.get("at")
                if at_node:
                    atoms = at_node.get_atoms()
                    x = round(float(atoms[0]), 2)
                    y = round(float(atoms[1]), 2)
                    sch.text_notes.append((text, x, y))

        # Update power counter based on existing power symbols
        max_pwr = 0
        for pwr in sch.power_symbols:
            # Extract number from #PWR01, #PWR02, etc.
            if pwr.reference.startswith("#PWR"):
                try:
                    num = int(pwr.reference[4:])
                    max_pwr = max(max_pwr, num)
                except ValueError:
                    pass
        sch._pwr_counter = max_pwr + 1

        _log_info(
            f"Loaded schematic: {len(sch.symbols)} symbols, "
            f"{len(sch.power_symbols)} power symbols, "
            f"{len(sch.wires)} wires"
        )

        return sch

    @property
    def sheet_path(self) -> str:
        if self.parent_uuid:
            return f"/{self.parent_uuid}/{self.sheet_uuid}"
        return f"/{self.sheet_uuid}"

    def _snap_coord(self, value: float, context: str = "") -> float:
        """Apply grid snapping to a single coordinate based on snap_mode.

        Args:
            value: Coordinate value
            context: Context for warning messages

        Returns:
            Snapped or original value based on snap_mode
        """
        if self.snap_mode == SnapMode.OFF:
            return round(value, 2)

        on_grid = is_on_grid(value, self.grid)

        if self.snap_mode == SnapMode.WARN:
            if not on_grid:
                snapped = snap_to_grid(value, self.grid)
                warnings.warn(
                    f"Off-grid coordinate ({context}): {value} -> nearest: {snapped}", stacklevel=4
                )
            return round(value, 2)

        if self.snap_mode == SnapMode.STRICT:
            if not on_grid:
                snapped = snap_to_grid(value, self.grid)
                warnings.warn(
                    f"Auto-snapping off-grid coordinate ({context}): {value} -> {snapped}",
                    stacklevel=4,
                )
            return snap_to_grid(value, self.grid)

        # SnapMode.AUTO - silently snap
        return snap_to_grid(value, self.grid)

    def _snap_point(self, point: tuple[float, float], context: str = "") -> tuple[float, float]:
        """Apply grid snapping to a point based on snap_mode.

        Args:
            point: (x, y) coordinate tuple
            context: Context for warning messages

        Returns:
            Snapped or original point based on snap_mode
        """
        return (
            self._snap_coord(point[0], f"{context} x"),
            self._snap_coord(point[1], f"{context} y"),
        )

    def add_symbol(
        self,
        lib_id: str,
        x: float,
        y: float,
        ref: str,
        value: str = None,
        rotation: float = 0,
        footprint: str = "",
        snap: bool = True,
    ) -> SymbolInstance:
        """Add a symbol to the schematic.

        Args:
            lib_id: Library:Symbol format (e.g., "Audio:PCM5122PW")
            x, y: Center position (snapped to grid unless snap=False)
            ref: Reference designator (e.g., "U1")
            value: Value (defaults to symbol name)
            rotation: Rotation in degrees (0, 90, 180, 270)
            footprint: Footprint string
            snap: Whether to apply grid snapping (default: True)

        Returns:
            SymbolInstance with pin_position() method
        """
        # Apply grid snapping if enabled
        if snap:
            x = self._snap_coord(x, f"symbol {ref}")
            y = self._snap_coord(y, f"symbol {ref}")

        # Load symbol definition if not cached
        if lib_id not in self._symbol_defs:
            self._symbol_defs[lib_id] = SymbolDef.from_library(lib_id)

        sym_def = self._symbol_defs[lib_id]

        instance = SymbolInstance(
            symbol_def=sym_def,
            x=x,
            y=y,
            rotation=rotation,
            reference=ref,
            value=value or sym_def.name,
            footprint=footprint,
        )

        self.symbols.append(instance)
        _log_info(f"Added symbol {ref} ({lib_id}) at ({x}, {y})")
        _log_debug(f"  Symbol {ref} has {len(sym_def.pins)} pins")
        return instance

    def add_power(
        self, lib_id: str, x: float, y: float, rotation: float = 0, snap: bool = True
    ) -> PowerSymbol:
        """Add a power symbol (GND, VCC, etc.).

        Args:
            lib_id: Power symbol library ID (e.g., "power:GND")
            x, y: Position (snapped to grid unless snap=False)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
        """
        ref = f"#PWR{self._pwr_counter:02d}"
        self._pwr_counter += 1

        # Apply grid snapping if enabled
        if snap:
            x = self._snap_coord(x, f"power {lib_id}")
            y = self._snap_coord(y, f"power {lib_id}")
        else:
            x = round(x, 2)
            y = round(y, 2)

        # Load power symbol definition
        if lib_id not in self._symbol_defs:
            self._symbol_defs[lib_id] = SymbolDef.from_library(lib_id)

        pwr = PowerSymbol(
            lib_id=lib_id,
            x=x,
            y=y,
            rotation=rotation,
            reference=ref,
            _symbol_def=self._symbol_defs[lib_id],
        )
        self.power_symbols.append(pwr)
        _log_info(f"Added power symbol {lib_id.split(':')[1]} at ({x}, {y})")
        return pwr

    def add_pwr_flag(self, x: float, y: float) -> PowerSymbol:
        """Add a PWR_FLAG symbol to mark a power net as intentionally driven.

        This suppresses ERC errors about power pins not being driven.
        Place on the power net near where power enters the schematic.
        """
        return self.add_power("power:PWR_FLAG", x, y, rotation=0)

    def add_wire(self, p1: tuple[float, float], p2: tuple[float, float], snap: bool = True) -> Wire:
        """Add a wire between two points.

        Args:
            p1: Start point (x, y)
            p2: End point (x, y)
            snap: Whether to apply grid snapping (default: True)

        Returns:
            The Wire created
        """
        # Apply grid snapping if enabled
        if snap:
            p1 = self._snap_point(p1, "wire start")
            p2 = self._snap_point(p2, "wire end")

        wire = Wire.between(p1, p2)
        self.wires.append(wire)
        _log_debug(f"Added wire from ({p1[0]}, {p1[1]}) to ({p2[0]}, {p2[1]})")
        return wire

    def add_wire_path(self, *points: tuple[float, float], snap: bool = True) -> list[Wire]:
        """Add a series of connected wire segments.

        Args:
            points: Sequence of (x, y) points to connect
            snap: Whether to apply grid snapping (default: True)

        Returns:
            List of wires created
        """
        wires = []
        for i in range(len(points) - 1):
            wires.append(self.add_wire(points[i], points[i + 1], snap=snap))
        return wires

    def add_junction(self, x: float, y: float, snap: bool = True) -> Junction:
        """Add a junction point.

        Args:
            x, y: Junction position (snapped to grid unless snap=False)
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, "junction")
            y = self._snap_coord(y, "junction")
        else:
            x = round(x, 2)
            y = round(y, 2)
        junc = Junction(x=x, y=y)
        self.junctions.append(junc)
        return junc

    def add_label(
        self, text: str, x: float, y: float, rotation: float = 0, snap: bool = True
    ) -> Label:
        """Add a net label.

        Args:
            text: Label text
            x, y: Label position (snapped to grid unless snap=False)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, f"label {text}")
            y = self._snap_coord(y, f"label {text}")
        label = Label(text=text, x=x, y=y, rotation=rotation)
        self.labels.append(label)
        return label

    def add_hier_label(
        self,
        text: str,
        x: float,
        y: float,
        shape: str = "input",
        rotation: float = 0,
        snap: bool = True,
    ) -> HierarchicalLabel:
        """Add a hierarchical label.

        Args:
            text: Label text
            x, y: Label position (snapped to grid unless snap=False)
            shape: Label shape (input, output, bidirectional, passive)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, f"hier_label {text}")
            y = self._snap_coord(y, f"hier_label {text}")
        hl = HierarchicalLabel(text=text, x=x, y=y, shape=shape, rotation=rotation)
        self.hier_labels.append(hl)
        return hl

    def add_text(self, text: str, x: float, y: float, snap: bool = True):
        """Add a text note.

        Args:
            text: Note text
            x, y: Text position (snapped to grid unless snap=False)
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, "text")
            y = self._snap_coord(y, "text")
        self.text_notes.append((text, x, y))

    # =========================================================================
    # Wiring Helper Methods
    # =========================================================================

    def wire_pin_to_point(
        self,
        symbol: SymbolInstance,
        pin_name: str,
        target: tuple[float, float],
        route: str = "auto",
    ) -> list[Wire]:
        """Wire a symbol pin to a target point using orthogonal routing.

        Args:
            symbol: The symbol instance
            pin_name: Name or number of the pin
            target: (x, y) destination point
            route: Routing style - "auto", "vertical_first", "horizontal_first"

        Returns:
            List of wires created
        """
        pin_pos = symbol.pin_position(pin_name)
        return self._route_orthogonal(pin_pos, target, route)

    def wire_pins(
        self, sym1: SymbolInstance, pin1: str, sym2: SymbolInstance, pin2: str, route: str = "auto"
    ) -> list[Wire]:
        """Wire two symbol pins together using orthogonal routing.

        Args:
            sym1: First symbol
            pin1: Pin name/number on first symbol
            sym2: Second symbol
            pin2: Pin name/number on second symbol
            route: Routing style - "auto", "vertical_first", "horizontal_first"

        Returns:
            List of wires created
        """
        p1 = sym1.pin_position(pin1)
        p2 = sym2.pin_position(pin2)
        return self._route_orthogonal(p1, p2, route)

    def wire_to_rail(
        self,
        symbol: SymbolInstance,
        pin_name: str,
        rail_y: float,
        extend_to_x: float = None,
        add_junction: bool = True,
    ) -> list[Wire]:
        """Connect a pin vertically to a horizontal rail.

        Args:
            symbol: The symbol instance
            pin_name: Name or number of the pin
            rail_y: Y coordinate of the horizontal rail
            extend_to_x: If set, also add horizontal wire to this X position
            add_junction: Whether to add a junction at the rail (default True)

        Returns:
            List of wires created
        """
        pin_pos = symbol.pin_position(pin_name)
        wires = []

        # Vertical wire from pin to rail
        wires.append(self.add_wire(pin_pos, (pin_pos[0], rail_y)))

        # Add junction at rail intersection
        if add_junction:
            self.add_junction(pin_pos[0], rail_y)

        # Optional horizontal extension
        if extend_to_x is not None and extend_to_x != pin_pos[0]:
            wires.append(self.add_wire((pin_pos[0], rail_y), (extend_to_x, rail_y)))

        return wires

    def add_rail(
        self, y: float, x_start: float, x_end: float, net_label: str = None, snap: bool = True
    ) -> Wire:
        """Add a horizontal power/ground rail.

        Args:
            y: Y coordinate of the rail (snapped to grid)
            x_start: Starting X coordinate (snapped to grid)
            x_end: Ending X coordinate (snapped to grid)
            net_label: Optional net label to add at the start
            snap: Whether to apply grid snapping (default: True)

        Returns:
            The wire created
        """
        wire = self.add_wire((x_start, y), (x_end, y), snap=snap)
        if net_label:
            # Use the actual snapped wire coordinates for the label
            self.add_label(net_label, wire.x1, wire.y1, rotation=0, snap=False)
        return wire

    def wire_power_to_pin(
        self,
        power_lib_id: str,
        symbol: SymbolInstance,
        pin_name: str,
        power_offset: tuple[float, float] = (0, -10),
    ) -> PowerSymbol:
        """Add a power symbol and wire it to a pin.

        Args:
            power_lib_id: Power symbol library ID (e.g., "power:+3.3V")
            symbol: Target symbol
            pin_name: Target pin name
            power_offset: (dx, dy) offset from pin for power symbol placement

        Returns:
            The power symbol created
        """
        pin_pos = symbol.pin_position(pin_name)
        power_x = pin_pos[0] + power_offset[0]
        power_y = pin_pos[1] + power_offset[1]

        pwr = self.add_power(power_lib_id, power_x, power_y)
        self.add_wire(pin_pos, (power_x, power_y))

        return pwr

    def wire_decoupling_cap(
        self,
        cap: SymbolInstance,
        power_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> list[Wire]:
        """Wire a decoupling capacitor between power and ground rails.

        Assumes cap is vertical with pin 1 (top) to power and pin 2 (bottom) to ground.

        Args:
            cap: Capacitor symbol instance
            power_rail_y: Y coordinate of power rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction dots

        Returns:
            List of wires created
        """
        wires = []

        # Get pin positions (Device:C has pins 1 at top, 2 at bottom)
        try:
            pin1_pos = cap.pin_position("1")
            pin2_pos = cap.pin_position("2")
        except ValueError:
            # Try by name if numbered pins don't work
            pin1_pos = cap.pin_position("~")
            pin2_pos = cap.pin_position("~")

        # Wire top to power rail
        wires.append(self.add_wire(pin1_pos, (pin1_pos[0], power_rail_y)))
        if add_junctions:
            self.add_junction(pin1_pos[0], power_rail_y)

        # Wire bottom to ground rail
        wires.append(self.add_wire(pin2_pos, (pin2_pos[0], gnd_rail_y)))
        if add_junctions:
            self.add_junction(pin2_pos[0], gnd_rail_y)

        return wires

    def add_decoupling_pair(
        self,
        x: float,
        y: float,
        ic_pin: tuple[float, float],
        power_symbol: str,
        ref_100nf: str,
        ref_10uf: str,
        grid: float = 2.54,
        footprint_100nf: str = "Capacitor_SMD:C_0402_1005Metric",
        footprint_10uf: str = "Capacitor_SMD:C_0805_2012Metric",
    ) -> dict:
        """Add a decoupling capacitor pair (100nF + 10uF) with power symbol and wiring.

        This is a common pattern for IC power pins: two caps in parallel connected
        between a power symbol and an IC power pin. The caps are placed horizontally
        with the power symbol above and wires connecting to the IC pin below.

        Layout:
                    [PWR]           <- power symbol
                      |
               ┌──────┴──────┐     <- horizontal bus to power
               │             │
              [C1]         [C2]    <- 100nF and 10uF caps
               │             │
               └──────┬──────┘     <- horizontal bus to IC
                      |
                   [IC PIN]        <- target IC power pin

        Args:
            x: X coordinate (center of cap pair)
            y: Y coordinate (center of caps)
            ic_pin: (x, y) position of the IC power pin to connect to
            power_symbol: Power symbol lib_id (e.g., "power:+3.3V", "power:+3.3VA")
            ref_100nf: Reference designator for 100nF cap (e.g., "C1")
            ref_10uf: Reference designator for 10uF cap (e.g., "C2")
            grid: Grid spacing in mm (default 2.54mm)
            footprint_100nf: Footprint for 100nF cap
            footprint_10uf: Footprint for 10uF cap

        Returns:
            Dictionary with keys:
                - cap_100nf: SymbolInstance for 100nF capacitor
                - cap_10uf: SymbolInstance for 10uF capacitor
                - power: PowerSymbol instance
                - wires: List of all wires created

        Example:
            dvdd_pin = dac.pin_position("DVDD")
            result = sch.add_decoupling_pair(
                x=dvdd_pin[0],
                y=dvdd_pin[1] - 10*GRID,
                ic_pin=dvdd_pin,
                power_symbol="power:+3.3V",
                ref_100nf="C1",
                ref_10uf="C2"
            )
        """
        wires = []

        # Place capacitors side by side (100nF left, 10uF right)
        cap_100nf = self.add_symbol(
            "Device:C_Small",
            x=x - 2 * grid,
            y=y,
            ref=ref_100nf,
            value="100nF",
            footprint=footprint_100nf,
        )
        cap_10uf = self.add_symbol(
            "Device:C_Small",
            x=x + 2 * grid,
            y=y,
            ref=ref_10uf,
            value="10uF",
            footprint=footprint_10uf,
        )

        # Add power symbol above caps
        power = self.add_power(power_symbol, x=x, y=y - 3 * grid)

        # Wire from caps to power symbol (top side)
        # Vertical wires from each cap up to bus level
        wires.append(self.add_wire((x - 2 * grid, y - grid), (x - 2 * grid, y - 2 * grid)))
        wires.append(self.add_wire((x + 2 * grid, y - grid), (x + 2 * grid, y - 2 * grid)))
        # Horizontal bus connecting caps at power level
        wires.append(self.add_wire((x - 2 * grid, y - 2 * grid), (x + 2 * grid, y - 2 * grid)))
        self.add_junction(x, y - 2 * grid)

        # Wire from caps to IC pin (bottom side)
        # Vertical wires from each cap down to bus level
        wires.append(self.add_wire((x - 2 * grid, y + grid), (x - 2 * grid, y + 2 * grid)))
        wires.append(self.add_wire((x + 2 * grid, y + grid), (x + 2 * grid, y + 2 * grid)))
        # Horizontal bus connecting caps
        wires.append(self.add_wire((x - 2 * grid, y + 2 * grid), (x + 2 * grid, y + 2 * grid)))
        self.add_junction(x, y + 2 * grid)

        # Wire from center of bottom bus to IC pin
        wires.append(self.add_wire((x, y + 2 * grid), ic_pin))

        _log_info(f"Added decoupling pair {ref_100nf}/{ref_10uf} at ({x}, {y}) -> IC pin")

        return {
            "cap_100nf": cap_100nf,
            "cap_10uf": cap_10uf,
            "power": power,
            "wires": wires,
        }

    def tie_pins_to_power(
        self,
        symbol: SymbolInstance,
        pin_names: list[str],
        power_symbol: str,
        x_offset: float = -4 * 2.54,
        grid: float = 2.54,
    ) -> PowerSymbol:
        """Tie multiple IC pins to a common power symbol (GND or VCC).

        This is a common pattern for configuration pins that need to be
        tied to a fixed level. The pins are connected together vertically
        and joined to a power symbol.

        Layout:
            [PIN1] ───┐
                      ├─── [PWR]
            [PIN2] ───┘

        Args:
            symbol: The IC symbol instance
            pin_names: List of pin names to tie together
            power_symbol: Power symbol lib_id (e.g., "power:GND", "power:+3.3V")
            x_offset: X offset from pins for the power symbol
            grid: Grid spacing

        Returns:
            The PowerSymbol instance created

        Example:
            # Tie MODE1 and MODE2 to GND for I2C mode
            sch.tie_pins_to_power(dac, ["MODE1", "MODE2/MS"], "power:GND")

            # Tie XSMT to VCC (not muted)
            sch.tie_pins_to_power(dac, ["XSMT"], "power:+3.3V", x_offset=-4*GRID)
        """
        if not pin_names:
            raise ValueError("pin_names must not be empty")

        # Get all pin positions
        pin_positions = [symbol.pin_position(name) for name in pin_names]

        # Calculate center Y for power symbol
        y_coords = [p[1] for p in pin_positions]
        center_y = (min(y_coords) + max(y_coords)) / 2
        first_pin_x = pin_positions[0][0]

        # Power symbol position
        pwr_x = first_pin_x + x_offset
        pwr_y = center_y + 3 * grid if "GND" in power_symbol else center_y - 3 * grid

        # Add power symbol
        pwr = self.add_power(power_symbol, x=pwr_x, y=pwr_y)

        # Wire all pins to the common bus line
        bus_x = first_pin_x + x_offset

        for i, (pin_pos, pin_name) in enumerate(zip(pin_positions, pin_names)):
            # Horizontal wire from pin to bus
            self.add_wire(pin_pos, (bus_x, pin_pos[1]))

            # Vertical wire to next pin (if not last)
            if i < len(pin_positions) - 1:
                next_pin_y = pin_positions[i + 1][1]
                self.add_wire((bus_x, pin_pos[1]), (bus_x, next_pin_y))
                self.add_junction(bus_x, next_pin_y)

        # Wire from bus to power symbol
        last_pin_y = pin_positions[-1][1]
        self.add_wire((bus_x, last_pin_y), (bus_x, pwr_y))

        _log_info(f"Tied pins {pin_names} to {power_symbol}")

        return pwr

    def wire_ldo(
        self,
        ldo: SymbolInstance,
        input_rail_y: float,
        output_rail_y: float,
        gnd_rail_y: float,
        tie_en_to_vin: bool = True,
    ) -> list[Wire]:
        """Wire an LDO regulator (AP2204K-1.5 or similar) to power rails.

        Args:
            ldo: LDO symbol instance
            input_rail_y: Y coordinate of input voltage rail (+5V typically)
            output_rail_y: Y coordinate of output voltage rail (+3.3V typically)
            gnd_rail_y: Y coordinate of ground rail
            tie_en_to_vin: Whether to tie EN pin to VIN (always enabled)

        Returns:
            List of wires created
        """
        wires = []

        # Get pin positions
        vin_pos = ldo.pin_position("VIN")
        vout_pos = ldo.pin_position("VOUT")
        gnd_pos = ldo.pin_position("GND")
        en_pos = ldo.pin_position("EN")

        # VIN to input rail
        wires.append(self.add_wire(vin_pos, (vin_pos[0], input_rail_y)))
        self.add_junction(vin_pos[0], input_rail_y)

        # VOUT to output rail
        wires.append(self.add_wire(vout_pos, (vout_pos[0], output_rail_y)))
        self.add_junction(vout_pos[0], output_rail_y)

        # GND to ground rail
        wires.append(self.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y)))
        self.add_junction(gnd_pos[0], gnd_rail_y)

        # EN tied to VIN if requested
        if tie_en_to_vin:
            # Wire from EN up to same Y as VIN, then over to VIN
            wires.append(self.add_wire(en_pos, (en_pos[0], vin_pos[1])))
            self.add_junction(en_pos[0], vin_pos[1])

        return wires

    def _route_orthogonal(
        self, start: tuple[float, float], end: tuple[float, float], route: str = "auto"
    ) -> list[Wire]:
        """Route between two points using orthogonal (Manhattan) routing.

        Args:
            start: Starting point (x, y)
            end: Ending point (x, y)
            route: "auto", "vertical_first", or "horizontal_first"

        Returns:
            List of wires created
        """
        x1, y1 = start
        x2, y2 = end

        # If points are aligned, single wire
        if x1 == x2 or y1 == y2:
            return [self.add_wire(start, end)]

        # Choose routing direction
        if route == "auto":
            # Prefer shorter first segment
            if abs(x2 - x1) < abs(y2 - y1):
                route = "horizontal_first"
            else:
                route = "vertical_first"

        if route == "horizontal_first":
            mid = (x2, y1)
        else:  # vertical_first
            mid = (x1, y2)

        return [self.add_wire(start, mid), self.add_wire(mid, end)]

    def connect_hier_label_to_pin(
        self,
        label_name: str,
        symbol: SymbolInstance,
        pin_name: str,
        label_offset: float = 15,
        shape: str = None,
    ) -> HierarchicalLabel:
        """Add a hierarchical label connected to a symbol pin.

        Args:
            label_name: Name for the hierarchical label
            symbol: Target symbol
            pin_name: Target pin name
            label_offset: Horizontal offset from pin for label placement
            shape: Override shape ("input", "output", "bidirectional", "passive")

        Returns:
            The hierarchical label created
        """
        pin_pos = symbol.pin_position(pin_name)

        # Determine label direction based on pin position relative to symbol center
        if pin_pos[0] < symbol.x:
            # Pin is on left side, label goes further left
            label_x = pin_pos[0] - label_offset
            auto_shape = "input"
            rotation = 0  # Arrow pointing right (into schematic)
        else:
            # Pin is on right side, label goes further right
            label_x = pin_pos[0] + label_offset
            auto_shape = "output"
            rotation = 180  # Arrow pointing left (into schematic)

        # Use provided shape or auto-detected
        final_shape = shape if shape else auto_shape

        # Add wire from pin to label position
        self.add_wire(pin_pos, (label_x, pin_pos[1]))

        # Add the label
        return self.add_hier_label(label_name, label_x, pin_pos[1], final_shape, rotation)

    def wire_bus(
        self,
        symbols_and_pins: list[tuple[SymbolInstance, str]],
        bus_y: float = None,
        bus_x: float = None,
    ) -> list[Wire]:
        """Wire multiple pins to a common bus line.

        Either bus_y (horizontal bus) or bus_x (vertical bus) must be specified.

        Args:
            symbols_and_pins: List of (symbol, pin_name) tuples to connect
            bus_y: Y coordinate for horizontal bus (pins connect vertically)
            bus_x: X coordinate for vertical bus (pins connect horizontally)

        Returns:
            List of wires created
        """
        if bus_y is None and bus_x is None:
            raise ValueError("Either bus_y or bus_x must be specified")

        wires = []
        bus_points = []

        for symbol, pin_name in symbols_and_pins:
            pin_pos = symbol.pin_position(pin_name)

            if bus_y is not None:
                # Horizontal bus - vertical connections
                bus_point = (pin_pos[0], bus_y)
            else:
                # Vertical bus - horizontal connections
                bus_point = (bus_x, pin_pos[1])

            wires.append(self.add_wire(pin_pos, bus_point))
            bus_points.append(bus_point)

        # Sort bus points and create bus wire
        if bus_y is not None:
            bus_points.sort(key=lambda p: p[0])  # Sort by X
            if len(bus_points) > 1:
                wires.append(self.add_wire(bus_points[0], bus_points[-1]))
        else:
            bus_points.sort(key=lambda p: p[1])  # Sort by Y
            if len(bus_points) > 1:
                wires.append(self.add_wire(bus_points[0], bus_points[-1]))

        # Add junctions at bus connection points
        for point in bus_points:
            self.add_junction(point[0], point[1])

        return wires

    def print_symbol_pins(self, symbol: SymbolInstance, name: str = None):
        """Debug helper: Print all pin positions for a symbol.

        Args:
            symbol: The symbol instance to inspect
            name: Optional name to display (defaults to reference)
        """
        display_name = name or symbol.reference
        print(f"\n{display_name} pins at ({symbol.x}, {symbol.y}) rot={symbol.rotation}:")
        for pin in symbol.symbol_def.pins:
            pos = symbol.pin_position(pin.name)
            print(f"  {pin.name} ({pin.number}): ({pos[0]:.2f}, {pos[1]:.2f})")

    def wire_ferrite_bead(self, fb: SymbolInstance, rail1_y: float, rail2_y: float) -> list[Wire]:
        """Wire a ferrite bead between two ground rails.

        Args:
            fb: Ferrite bead symbol instance
            rail1_y: Y coordinate of first rail (connects to pin 1)
            rail2_y: Y coordinate of second rail (connects to pin 2)

        Returns:
            List of wires created
        """
        wires = []

        pin1_pos = fb.pin_position("1")
        pin2_pos = fb.pin_position("2")

        wires.append(self.add_wire(pin1_pos, (pin1_pos[0], rail1_y)))
        self.add_junction(pin1_pos[0], rail1_y)

        wires.append(self.add_wire(pin2_pos, (pin2_pos[0], rail2_y)))
        self.add_junction(pin2_pos[0], rail2_y)

        return wires

    def wire_oscillator(
        self,
        osc: SymbolInstance,
        power_rail_y: float,
        gnd_rail_y: float,
        output_label: str = None,
        tie_en_to_vdd: bool = True,
    ) -> list[Wire]:
        """Wire an oscillator (like ASE-xxxMHz) to power rails.

        Args:
            osc: Oscillator symbol instance
            power_rail_y: Y coordinate of power rail
            gnd_rail_y: Y coordinate of ground rail
            output_label: If set, add a label at the output
            tie_en_to_vdd: Whether to tie EN pin to Vdd

        Returns:
            List of wires created
        """
        wires = []

        # Common oscillator pinout (ASE-xxxMHz):
        # Pin 1: EN (enable)
        # Pin 2: GND
        # Pin 3: OUT
        # Pin 4: Vdd

        try:
            vdd_pos = osc.pin_position("Vdd")
            gnd_pos = osc.pin_position("GND")
            out_pos = osc.pin_position("OUT")
            en_pos = osc.pin_position("EN")
        except ValueError:
            # Try alternate pin names
            vdd_pos = osc.pin_position("4")
            gnd_pos = osc.pin_position("2")
            out_pos = osc.pin_position("3")
            en_pos = osc.pin_position("1")

        # Vdd to power rail
        wires.append(self.add_wire(vdd_pos, (vdd_pos[0], power_rail_y)))
        self.add_junction(vdd_pos[0], power_rail_y)

        # GND to ground rail
        wires.append(self.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y)))
        self.add_junction(gnd_pos[0], gnd_rail_y)

        # EN tied to Vdd if requested
        if tie_en_to_vdd:
            wires.extend(self._route_orthogonal(en_pos, (vdd_pos[0], power_rail_y)))
            self.add_junction(vdd_pos[0], power_rail_y)

        # Output label
        if output_label:
            self.add_label(output_label, out_pos[0] + 5, out_pos[1])
            wires.append(self.add_wire(out_pos, (out_pos[0] + 5, out_pos[1])))

        return wires

    # =========================================================================
    # Query Methods (for editing existing schematics)
    # =========================================================================

    def find_wires(
        self,
        endpoint: tuple[float, float] = None,
        near: tuple[float, float] = None,
        tolerance: float = None,
        connected_to_label: str = None,
    ) -> list[Wire]:
        """Find wires matching specified criteria.

        Args:
            endpoint: Exact endpoint (x, y) to match (either end of wire)
            near: Point (x, y) to search near (either end of wire)
            tolerance: Distance tolerance for 'near' search (default: grid size)
            connected_to_label: Find wires connected to a label's position

        Returns:
            List of matching Wire objects

        Example:
            # Find wires ending at a specific point
            wires = sch.find_wires(endpoint=(165, 110))

            # Find wires near a point
            wires = sch.find_wires(near=(165, 110), tolerance=2.54)

            # Find wires connected to a label
            wires = sch.find_wires(connected_to_label="I2S_DIN")
        """
        if tolerance is None:
            tolerance = self.grid

        results = []

        # If searching by label, first find the label position
        if connected_to_label:
            label_pos = None
            # Check regular labels
            for label in self.labels:
                if label.text == connected_to_label:
                    label_pos = (label.x, label.y)
                    break
            # Check hierarchical labels
            if label_pos is None:
                for hl in self.hier_labels:
                    if hl.text == connected_to_label:
                        label_pos = (hl.x, hl.y)
                        break
            if label_pos is None:
                return []  # Label not found
            near = label_pos

        for wire in self.wires:
            wire_p1 = (wire.x1, wire.y1)
            wire_p2 = (wire.x2, wire.y2)

            if endpoint:
                # Exact match on either endpoint
                if self._points_equal(wire_p1, endpoint) or self._points_equal(wire_p2, endpoint):
                    results.append(wire)
            elif near:
                # Check if either endpoint is within tolerance
                if self._point_near(wire_p1, near, tolerance) or self._point_near(
                    wire_p2, near, tolerance
                ):
                    results.append(wire)
            else:
                # No filter - return all wires
                results.append(wire)

        return results

    def find_label(self, name: str) -> Optional[Label]:
        """Find a label by exact name.

        Args:
            name: Label text to find

        Returns:
            Label object if found, None otherwise
        """
        for label in self.labels:
            if label.text == name:
                return label
        return None

    def find_labels(self, pattern: str = None) -> list[Label]:
        """Find labels matching a pattern.

        Args:
            pattern: Glob-style pattern (e.g., "I2S_*") or None for all

        Returns:
            List of matching Label objects
        """
        import fnmatch

        if pattern is None:
            return list(self.labels)
        return [lbl for lbl in self.labels if fnmatch.fnmatch(lbl.text, pattern)]

    def find_hier_label(self, name: str) -> Optional[HierarchicalLabel]:
        """Find a hierarchical label by exact name.

        Args:
            name: Label text to find

        Returns:
            HierarchicalLabel object if found, None otherwise
        """
        for hl in self.hier_labels:
            if hl.text == name:
                return hl
        return None

    def find_hier_labels(self, pattern: str = None) -> list[HierarchicalLabel]:
        """Find hierarchical labels matching a pattern.

        Args:
            pattern: Glob-style pattern (e.g., "I2S_*") or None for all

        Returns:
            List of matching HierarchicalLabel objects
        """
        import fnmatch

        if pattern is None:
            return list(self.hier_labels)
        return [hl for hl in self.hier_labels if fnmatch.fnmatch(hl.text, pattern)]

    def find_symbol(self, reference: str) -> Optional[SymbolInstance]:
        """Find a symbol by reference designator.

        Args:
            reference: Reference designator (e.g., "U3", "C1")

        Returns:
            SymbolInstance if found, None otherwise
        """
        for sym in self.symbols:
            if sym.reference == reference:
                return sym
        return None

    def find_symbols(self, pattern: str = None) -> list[SymbolInstance]:
        """Find symbols matching a pattern.

        Args:
            pattern: Glob-style pattern (e.g., "C*", "U?") or None for all

        Returns:
            List of matching SymbolInstance objects
        """
        import fnmatch

        if pattern is None:
            return list(self.symbols)
        return [s for s in self.symbols if fnmatch.fnmatch(s.reference, pattern)]

    def _points_equal(
        self, p1: tuple[float, float], p2: tuple[float, float], tolerance: float = 0.01
    ) -> bool:
        """Check if two points are equal within tolerance."""
        return abs(p1[0] - p2[0]) < tolerance and abs(p1[1] - p2[1]) < tolerance

    def _point_near(
        self, p1: tuple[float, float], p2: tuple[float, float], tolerance: float
    ) -> bool:
        """Check if two points are within tolerance distance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) <= (tolerance * tolerance)

    # =========================================================================
    # Removal Methods (for editing existing schematics)
    # =========================================================================

    def remove_wire(self, wire: Wire) -> bool:
        """Remove a specific wire from the schematic.

        Args:
            wire: The Wire object to remove

        Returns:
            True if wire was removed, False if not found
        """
        try:
            self.wires.remove(wire)
            _log_info(f"Removed wire from ({wire.x1}, {wire.y1}) to ({wire.x2}, {wire.y2})")
            return True
        except ValueError:
            return False

    def remove_wires_at(self, point: tuple[float, float], tolerance: float = None) -> int:
        """Remove all wires with an endpoint at or near a point.

        Args:
            point: (x, y) coordinate
            tolerance: Distance tolerance (default: grid size)

        Returns:
            Number of wires removed
        """
        if tolerance is None:
            tolerance = self.grid

        wires_to_remove = self.find_wires(near=point, tolerance=tolerance)
        for wire in wires_to_remove:
            self.wires.remove(wire)

        if wires_to_remove:
            _log_info(f"Removed {len(wires_to_remove)} wire(s) near ({point[0]}, {point[1]})")

        return len(wires_to_remove)

    def remove_label(self, name: str) -> bool:
        """Remove a label by name.

        Args:
            name: Label text to remove

        Returns:
            True if label was removed, False if not found
        """
        label = self.find_label(name)
        if label:
            self.labels.remove(label)
            _log_info(f"Removed label '{name}' at ({label.x}, {label.y})")
            return True
        return False

    def remove_hier_label(self, name: str) -> bool:
        """Remove a hierarchical label by name.

        Args:
            name: Label text to remove

        Returns:
            True if label was removed, False if not found
        """
        hl = self.find_hier_label(name)
        if hl:
            self.hier_labels.remove(hl)
            _log_info(f"Removed hierarchical label '{name}' at ({hl.x}, {hl.y})")
            return True
        return False

    def remove_net(self, name: str, tolerance: float = None) -> dict:
        """Remove a net: its label and all directly connected wires.

        This is a convenience method that removes a label/hier_label and any
        wires that have an endpoint at the label's position.

        Args:
            name: Net name (label text)
            tolerance: Distance tolerance for wire matching (default: grid size)

        Returns:
            Dict with keys:
                - label_removed: bool
                - hier_label_removed: bool
                - wires_removed: int

        Example:
            result = sch.remove_net("I2S_DIN")
            print(f"Removed {result['wires_removed']} wires")
        """
        if tolerance is None:
            tolerance = self.grid

        result = {"label_removed": False, "hier_label_removed": False, "wires_removed": 0}

        # Find and remove regular label
        label = self.find_label(name)
        if label:
            label_pos = (label.x, label.y)
            self.labels.remove(label)
            result["label_removed"] = True
            # Remove wires connected to label
            result["wires_removed"] += self.remove_wires_at(label_pos, tolerance)

        # Find and remove hierarchical label
        hl = self.find_hier_label(name)
        if hl:
            hl_pos = (hl.x, hl.y)
            self.hier_labels.remove(hl)
            result["hier_label_removed"] = True
            # Remove wires connected to hier_label
            result["wires_removed"] += self.remove_wires_at(hl_pos, tolerance)

        if result["label_removed"] or result["hier_label_removed"]:
            _log_info(
                f"Removed net '{name}': label={result['label_removed']}, "
                f"hier_label={result['hier_label_removed']}, "
                f"wires={result['wires_removed']}"
            )

        return result

    def remove_junction(self, x: float, y: float, tolerance: float = None) -> bool:
        """Remove a junction at a specific position.

        Args:
            x, y: Junction position
            tolerance: Distance tolerance (default: grid size)

        Returns:
            True if junction was removed, False if not found
        """
        if tolerance is None:
            tolerance = self.grid

        for junc in self.junctions:
            if self._point_near((junc.x, junc.y), (x, y), tolerance):
                self.junctions.remove(junc)
                _log_info(f"Removed junction at ({junc.x}, {junc.y})")
                return True
        return False

    def remove_symbol(self, reference: str) -> bool:
        """Remove a symbol by reference designator.

        Note: This does NOT remove connected wires. Use remove_symbol_and_wires()
        for that.

        Args:
            reference: Reference designator (e.g., "U3")

        Returns:
            True if symbol was removed, False if not found
        """
        sym = self.find_symbol(reference)
        if sym:
            self.symbols.remove(sym)
            _log_info(f"Removed symbol {reference}")
            return True
        return False

    def _build_lib_symbols_node(self) -> SExp:
        """Build lib_symbols section as SExp node.

        For loaded schematics, uses the preserved embedded lib_symbols.
        For newly created schematics, uses SymbolDef.to_sexp_nodes().
        """
        lib_symbols = SExp.list("lib_symbols")

        # Track which lib_ids we've already added
        added_lib_ids = set()

        # First, add any embedded lib_symbols from loaded schematics
        # These take priority to preserve exact formatting
        for sym_name, sym_node in self._embedded_lib_symbols.items():
            lib_symbols.append(sym_node)
            added_lib_ids.add(sym_name)

        # Then add any new symbol defs that weren't embedded
        for sym_def in self._symbol_defs.values():
            if sym_def.lib_id not in added_lib_ids:
                # Each symbol def may produce multiple nodes (parent + child for inheritance)
                for sym_node in sym_def.to_sexp_nodes():
                    lib_symbols.append(sym_node)
                    added_lib_ids.add(sym_def.lib_id)

        return lib_symbols

    def _build_text_note_node(self, text: str, x: float, y: float) -> SExp:
        """Build a text note as SExp node."""
        return text_node(text, x, y, str(uuid.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build complete schematic as SExp tree."""
        # Build root with header
        root = SExp.list(
            "kicad_sch",
            SExp.list("version", 20250114),
            SExp.list("generator", "eeschema"),
            SExp.list("generator_version", "9.0"),
            uuid_node(self.sheet_uuid),
            SExp.list("paper", self.paper),
        )

        # Title block
        root.append(
            title_block(
                title=self.title,
                date=self.date,
                revision=self.revision,
                company=self.company,
                comment1=self.comment1,
                comment2=self.comment2,
            )
        )

        # Library symbols (parsed from string until Phase 5)
        root.append(self._build_lib_symbols_node())

        # Symbol instances
        for sym in self.symbols:
            root.append(sym.to_sexp_node(self.project_name, self.sheet_path))

        # Power symbols
        for pwr in self.power_symbols:
            root.append(pwr.to_sexp_node(self.project_name, self.sheet_path))

        # Wires
        for wire in self.wires:
            root.append(wire.to_sexp_node())

        # Junctions
        for junc in self.junctions:
            root.append(junc.to_sexp_node())

        # Labels
        for label in self.labels:
            root.append(label.to_sexp_node())

        # Hierarchical labels
        for hl in self.hier_labels:
            root.append(hl.to_sexp_node())

        # Text notes
        for text, x, y in self.text_notes:
            root.append(self._build_text_note_node(text, x, y))

        # Sheet instances
        root.append(sheet_instances(self.sheet_path, self.page))

        return root

    def to_sexp(self) -> str:
        """Generate complete schematic S-expression string."""
        return self.to_sexp_node().to_string()

    def write(self, path: str | Path):
        """Write schematic to file."""
        path = Path(path)
        content = self.to_sexp()
        path.write_text(content)
        _log_info(
            f"Wrote schematic to {path} ({len(self.symbols)} symbols, {len(self.wires)} wires)"
        )

    # =========================================================================
    # Validation and Debugging (Agent-Focused)
    # =========================================================================

    def validate(self, fix_auto: bool = False) -> list[dict]:
        """Validate the schematic and return a list of issues.

        This method checks for common problems that agents might introduce:
        - Duplicate reference designators
        - Unconnected power pins
        - Off-grid coordinates
        - Floating wire endpoints
        - Missing power flags

        Args:
            fix_auto: If True, automatically fix issues where possible

        Returns:
            List of issue dictionaries with keys:
                - severity: "error", "warning", or "info"
                - type: Issue type identifier
                - message: Human-readable description
                - location: Optional (x, y) or reference
                - fix_applied: True if auto-fixed (when fix_auto=True)

        Example:
            issues = sch.validate()
            errors = [i for i in issues if i['severity'] == 'error']
            if errors:
                print(f"Found {len(errors)} errors")
                for e in errors:
                    print(f"  {e['type']}: {e['message']}")
        """
        issues = []

        # Check for duplicate references
        refs = {}
        for sym in self.symbols:
            if sym.reference in refs:
                issues.append(
                    {
                        "severity": "error",
                        "type": "duplicate_reference",
                        "message": f"Duplicate reference '{sym.reference}' at ({sym.x}, {sym.y})",
                        "location": (sym.x, sym.y),
                        "fix_applied": False,
                    }
                )
            refs[sym.reference] = sym

        # Check for off-grid symbols
        for sym in self.symbols:
            if not is_on_grid(sym.x, self.grid) or not is_on_grid(sym.y, self.grid):
                issue = {
                    "severity": "warning",
                    "type": "off_grid_symbol",
                    "message": f"Symbol {sym.reference} at ({sym.x}, {sym.y}) is off-grid",
                    "location": (sym.x, sym.y),
                    "fix_applied": False,
                }
                if fix_auto:
                    sym.x = snap_to_grid(sym.x, self.grid)
                    sym.y = snap_to_grid(sym.y, self.grid)
                    issue["fix_applied"] = True
                    issue["message"] += f" -> snapped to ({sym.x}, {sym.y})"
                issues.append(issue)

        # Check for off-grid wire endpoints
        for wire in self.wires:
            for coord, name in [((wire.x1, wire.y1), "start"), ((wire.x2, wire.y2), "end")]:
                if not is_on_grid(coord[0], self.grid) or not is_on_grid(coord[1], self.grid):
                    issues.append(
                        {
                            "severity": "warning",
                            "type": "off_grid_wire",
                            "message": f"Wire {name} at ({coord[0]}, {coord[1]}) is off-grid",
                            "location": coord,
                            "fix_applied": False,
                        }
                    )

        # Check wire connectivity (floating endpoints)
        connectivity_issues = self._check_wire_connectivity()
        issues.extend(connectivity_issues)

        # Check for power pins without connections
        power_pin_issues = self._check_power_pins()
        issues.extend(power_pin_issues)

        # Log validation summary
        errors = sum(1 for i in issues if i["severity"] == "error")
        warnings = sum(1 for i in issues if i["severity"] == "warning")
        if issues:
            _log_info(f"Validation found {errors} errors, {warnings} warnings")
            for issue in issues:
                if issue["severity"] == "error":
                    _log_warning(f"  {issue['type']}: {issue['message']}")
                else:
                    _log_debug(f"  {issue['type']}: {issue['message']}")
        else:
            _log_info("Validation passed with no issues")

        return issues

    def _check_wire_connectivity(self) -> list[dict]:
        """Check for floating wire endpoints not connected to anything."""
        issues = []

        # Collect all connection points
        connection_points = set()

        # Pin positions
        for sym in self.symbols:
            for pin in sym.symbol_def.pins:
                pos = sym.pin_position(pin.name if pin.name else pin.number)
                connection_points.add((round(pos[0], 2), round(pos[1], 2)))

        # Power symbol positions (they connect at their position)
        for pwr in self.power_symbols:
            connection_points.add((round(pwr.x, 2), round(pwr.y, 2)))

        # Junction positions
        for junc in self.junctions:
            connection_points.add((round(junc.x, 2), round(junc.y, 2)))

        # Label positions
        for label in self.labels:
            connection_points.add((round(label.x, 2), round(label.y, 2)))

        # Hierarchical label positions
        for hl in self.hier_labels:
            connection_points.add((round(hl.x, 2), round(hl.y, 2)))

        # Wire endpoints and T-junctions
        wire_endpoints = []
        wire_segments = []
        for wire in self.wires:
            p1 = (round(wire.x1, 2), round(wire.y1, 2))
            p2 = (round(wire.x2, 2), round(wire.y2, 2))
            wire_endpoints.append(p1)
            wire_endpoints.append(p2)
            wire_segments.append((p1, p2))

        # Check each wire endpoint
        endpoint_counts = {}
        for ep in wire_endpoints:
            endpoint_counts[ep] = endpoint_counts.get(ep, 0) + 1

        for endpoint, count in endpoint_counts.items():
            # An endpoint is valid if:
            # 1. It connects to a symbol pin, power symbol, junction, or label
            # 2. It connects to another wire (count >= 2)
            # 3. It lies on another wire segment (T-junction)

            if endpoint in connection_points:
                continue  # Connected to a pin/power/junction/label

            if count >= 2:
                continue  # Connected to another wire at this point

            # Check if it lies on another wire segment (T-junction without junction dot)
            on_wire = False
            for seg_start, seg_end in wire_segments:
                if endpoint == seg_start or endpoint == seg_end:
                    continue  # This is the wire's own endpoint
                if self._point_on_segment(endpoint, seg_start, seg_end):
                    on_wire = True
                    # This is a T-junction without a junction dot - warn
                    issues.append(
                        {
                            "severity": "warning",
                            "type": "missing_junction",
                            "message": f"Wire endpoint at ({endpoint[0]}, {endpoint[1]}) forms T-junction without junction dot",
                            "location": endpoint,
                            "fix_applied": False,
                        }
                    )
                    break

            if not on_wire:
                issues.append(
                    {
                        "severity": "error",
                        "type": "floating_wire",
                        "message": f"Wire endpoint at ({endpoint[0]}, {endpoint[1]}) is not connected to anything",
                        "location": endpoint,
                        "fix_applied": False,
                    }
                )

        return issues

    def _point_on_segment(self, point: tuple, seg_start: tuple, seg_end: tuple) -> bool:
        """Check if a point lies on a line segment (for orthogonal wires)."""
        px, py = point
        x1, y1 = seg_start
        x2, y2 = seg_end

        # For orthogonal wires, check if point is on the line
        if x1 == x2 == px:  # Vertical segment
            return min(y1, y2) < py < max(y1, y2)
        if y1 == y2 == py:  # Horizontal segment
            return min(x1, x2) < px < max(x1, x2)
        return False

    def _check_power_pins(self) -> list[dict]:
        """Check for power pins that might not be properly connected."""
        issues = []

        # Collect all wire endpoints and junction points for connection checking
        connected_points = set()
        for wire in self.wires:
            connected_points.add((round(wire.x1, 2), round(wire.y1, 2)))
            connected_points.add((round(wire.x2, 2), round(wire.y2, 2)))
        for junc in self.junctions:
            connected_points.add((round(junc.x, 2), round(junc.y, 2)))

        for sym in self.symbols:
            for pin in sym.symbol_def.pins:
                if pin.pin_type in ("power_in", "power_out"):
                    pos = sym.pin_position(pin.name if pin.name else pin.number)
                    pos_rounded = (round(pos[0], 2), round(pos[1], 2))

                    if pos_rounded not in connected_points:
                        issues.append(
                            {
                                "severity": "warning",
                                "type": "unconnected_power_pin",
                                "message": f"Power pin {pin.name or pin.number} on {sym.reference} at ({pos[0]}, {pos[1]}) may be unconnected",
                                "location": pos_rounded,
                                "fix_applied": False,
                            }
                        )

        return issues

    def get_statistics(self) -> dict:
        """Get schematic statistics useful for agents.

        Returns:
            Dictionary with counts and summary information:
                - symbol_count: Number of placed symbols
                - wire_count: Number of wire segments
                - junction_count: Number of junctions
                - label_count: Number of net labels
                - unique_nets: Estimated number of unique nets
                - power_symbols: Count of power symbols
                - references: List of all reference designators
        """
        return {
            "symbol_count": len(self.symbols),
            "wire_count": len(self.wires),
            "junction_count": len(self.junctions),
            "label_count": len(self.labels),
            "hier_label_count": len(self.hier_labels),
            "power_symbol_count": len(self.power_symbols),
            "references": sorted([s.reference for s in self.symbols]),
            "power_nets": sorted(set(p.lib_id.split(":")[1] for p in self.power_symbols)),
            "net_labels": sorted(set(lbl.text for lbl in self.labels)),
        }

    def find_symbols_by_value(self, value: str) -> list[SymbolInstance]:
        """Find all symbols with a given value.

        Args:
            value: Value to search for (e.g., "100nF", "PCM5122PW")

        Returns:
            List of matching SymbolInstance objects
        """
        return [s for s in self.symbols if s.value == value]

    def find_symbols_by_lib(self, lib_pattern: str) -> list[SymbolInstance]:
        """Find all symbols from a library matching a pattern.

        Args:
            lib_pattern: Library pattern with optional wildcards
                        (e.g., "Device:*", "*:PCM*", "Audio:*")

        Returns:
            List of matching SymbolInstance objects
        """
        import fnmatch

        return [s for s in self.symbols if fnmatch.fnmatch(s.symbol_def.lib_id, lib_pattern)]


# =============================================================================
# Discovery Functions (Agent-Focused)
# =============================================================================


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
        f"Position: ({symbol.x}, {symbol.y}) rotation={symbol.rotation}°",
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


if __name__ == "__main__":
    # Test: Extract PCM5122PW and print pin positions
    print("Testing KiCad Schematic Helper\n")

    try:
        sym = SymbolDef.from_library("Audio:PCM5122PW")
        print(f"Loaded: {sym.lib_id}")
        print(f"Pins ({len(sym.pins)}):")
        for pin in sym.pins:
            print(
                f"  {pin.number:>2}: {pin.name:<20} at ({pin.x:>6.2f}, {pin.y:>6.2f}) angle={pin.angle}"
            )
    except Exception as e:
        print(f"Error: {e}")
