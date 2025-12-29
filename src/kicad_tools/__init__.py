"""
kicad-tools: Standalone Python tools for KiCad schematic and PCB files.

This package provides tools to parse, analyze, and modify KiCad files
without requiring a running KiCad instance.
"""

__version__ = "0.1.0"

from kicad_tools.core.sexp import SExp
from kicad_tools.core.sexp_file import load_schematic, save_schematic, load_pcb, save_pcb
from kicad_tools.schema.schematic import Schematic
from kicad_tools.schema.symbol import SymbolInstance
from kicad_tools.schema.pcb import PCB

__all__ = [
    "SExp",
    "load_schematic",
    "save_schematic",
    "load_pcb",
    "save_pcb",
    "Schematic",
    "SymbolInstance",
    "PCB",
]
