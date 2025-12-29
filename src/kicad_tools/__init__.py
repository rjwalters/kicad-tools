"""
kicad-tools: Standalone Python tools for KiCad schematic and PCB files.

This package provides tools to parse, analyze, and modify KiCad files
without requiring a running KiCad instance.

Modules:
    core: S-expression parsing and file I/O
    schema: Data models for schematics, symbols, PCBs
    query: Fluent query interface for symbols and footprints
    parts: LCSC parts database integration
    export: Manufacturing export (Gerbers, BOM, CPL)
    drc: Design Rule Check report parsing
    erc: Electrical Rule Check report parsing
    manufacturers: PCB manufacturer design rules (JLCPCB, OSHPark, etc.)
    operations: Schematic and PCB operations
    router: PCB autorouter with A* pathfinding

Quick Start::

    from kicad_tools import Schematic, PCB, Project

    # Load and query schematic
    sch = Schematic.load("project.kicad_sch")
    u1 = sch.symbols.by_reference("U1")
    caps = sch.symbols.filter(value="100nF")

    # Load and query PCB
    pcb = PCB.load("project.kicad_pcb")
    smd_parts = pcb.footprints.smd()

    # Work with complete project
    project = Project.load("project.kicad_pro")
    result = project.cross_reference()
    project.export_assembly("output/", manufacturer="jlcpcb")
"""

__version__ = "0.2.0"

# Core S-expression handling
from kicad_tools.core.sexp import SExp
from kicad_tools.core.sexp_file import load_pcb, load_schematic, save_pcb, save_schematic

# Schema models
from kicad_tools.schema.schematic import Schematic
from kicad_tools.schema.symbol import SymbolInstance
from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.bom import BOM, BOMItem, extract_bom

# Project
from kicad_tools.project import Project

# Query API
from kicad_tools.query import (
    BaseQuery,
    SymbolQuery,
    SymbolList,
    FootprintQuery,
    FootprintList,
)

__all__ = [
    # Version
    "__version__",
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
    "BOM",
    "BOMItem",
    "extract_bom",
    # Project
    "Project",
    # Query API
    "BaseQuery",
    "SymbolQuery",
    "SymbolList",
    "FootprintQuery",
    "FootprintList",
]
