"""
kicad-tools: Standalone Python tools for KiCad schematic and PCB files.

This package provides tools to parse, analyze, and modify KiCad files
without requiring a running KiCad instance.

Modules:
    core: S-expression parsing and file I/O
    schema: Data models for schematics, symbols, PCBs
    drc: Design Rule Check report parsing
    erc: Electrical Rule Check report parsing
    manufacturers: PCB manufacturer design rules (JLCPCB, OSHPark, etc.)
    operations: Schematic and PCB operations
    router: PCB autorouter with A* pathfinding
"""

__version__ = "0.1.0"

# Core S-expression handling
from kicad_tools.core.sexp import SExp
from kicad_tools.core.sexp_file import load_pcb, load_schematic, save_pcb, save_schematic
from kicad_tools.schema.pcb import PCB

# Schema models
from kicad_tools.schema.schematic import Schematic
from kicad_tools.schema.symbol import SymbolInstance

__all__ = [
    # Core
    "SExp",
    "load_schematic",
    "save_schematic",
    "load_pcb",
    "save_pcb",
    # Schema
    "Schematic",
    "SymbolInstance",
    "PCB",
]
