"""
Legacy S-expression parser for KiCad files.

.. note::
    This module provides feature-rich S-expression parsing with round-trip
    editing support. It is used internally by kicad_tools.schematic and
    kicad_tools.pcb modules.

    For new code parsing KiCad files, consider ``kicad_tools.core.sexp``
    which provides a simpler API, or use the high-level schema models
    in ``kicad_tools.schema``.

This module supports:
- Round-trip editing with format preservation
- Deep S-expression manipulation (find_all, get_atoms, etc.)
- Document-level operations
"""

from .parser import (
    Document,
    ParseError,
    Parser,
    SExp,
    parse_file,
    parse_string,
)

__all__ = [
    # Parser classes and functions
    "SExp",
    "Parser",
    "ParseError",
    "Document",
    "parse_string",
    "parse_file",
]
