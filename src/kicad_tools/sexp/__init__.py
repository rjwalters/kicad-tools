"""
S-expression parser for KiCad files.

This is the canonical S-expression implementation for kicad_tools.
It provides feature-rich S-expression parsing with:
- Round-trip editing with format preservation
- Deep S-expression manipulation (find_all, get_atoms, etc.)
- Document-level operations
- Full backward compatibility with the legacy core/sexp.py API

Usage:
    from kicad_tools.sexp import SExp, parse_sexp, parse_string, parse_file

    # Parse from string
    doc = parse_string(text)  # or parse_sexp(text) for backward compat

    # Parse from file
    doc = parse_file("project.kicad_sch")

    # Access elements using either new or legacy API
    doc.name  # or doc.tag for backward compat
    doc.children  # or doc.values for backward compat
"""

from .parser import (
    Document,
    ParseError,
    Parser,
    SExp,
    SExpParser,
    SExpSerializer,
    parse_file,
    parse_sexp,
    parse_string,
    serialize_sexp,
)

__all__ = [
    # Parser classes and functions
    "SExp",
    "Parser",
    "ParseError",
    "Document",
    "parse_string",
    "parse_file",
    # Backward compatibility with core/sexp.py
    "parse_sexp",
    "serialize_sexp",
    "SExpParser",
    "SExpSerializer",
]
