"""
Schematic design tools for KiCad.

This module provides tools for *creating* KiCad schematic content:
- Symbol generation from datasheets or pin lists
- Circuit block generation (common circuits like LDO, oscillator, etc.)
- Symbol library registry management

For *reading and parsing* existing schematics, use ``kicad_tools.schema``.
"""

from .registry import SymbolRegistry
from .symbol_generator import (
    PACKAGE_TEMPLATES,
    PinDef,
    PinSide,
    PinStyle,
    PinType,
    SymbolDef,
    apply_template,
    create_pins_from_template,
    detect_pin_side,
    detect_pin_style,
    detect_pin_type,
    generate_symbol_sexp,
    parse_csv,
    parse_datasheet_text,
    parse_json,
)

# Helper and blocks are imported but not re-exported at top level
# to avoid pulling in heavy dependencies unless needed

__all__ = [
    # Symbol generation
    "PinType",
    "PinStyle",
    "PinSide",
    "PinDef",
    "SymbolDef",
    "detect_pin_type",
    "detect_pin_side",
    "detect_pin_style",
    "generate_symbol_sexp",
    "parse_json",
    "parse_csv",
    "parse_datasheet_text",
    "apply_template",
    "create_pins_from_template",
    "PACKAGE_TEMPLATES",
    # Registry
    "SymbolRegistry",
]
