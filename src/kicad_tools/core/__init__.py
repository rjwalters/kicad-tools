"""KiCad core utilities for parsing and generating S-expression files."""

from kicad_tools.sexp import SExp, parse_sexp, serialize_sexp

from .severity import SeverityMixin
from .sexp_file import (
    load_pcb,
    load_schematic,
    load_symbol_lib,
    save_pcb,
    save_schematic,
    save_symbol_lib,
)

__all__ = [
    "SExp",
    "parse_sexp",
    "serialize_sexp",
    "load_schematic",
    "save_schematic",
    "load_pcb",
    "save_pcb",
    "load_symbol_lib",
    "save_symbol_lib",
    "SeverityMixin",
]
