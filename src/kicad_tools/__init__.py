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
    constraints: Constraint locking for multi-stage optimization
    progress: Progress callback infrastructure for long-running operations

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

    # Use progress callbacks for long operations
    from kicad_tools.progress import ProgressCallback, ProgressContext
    def on_progress(progress, message, cancelable):
        print(f"{progress*100:.0f}%: {message}")
        return True  # Continue
    # ... use with router, DRC, export functions
"""

# Get version from package metadata (single source of truth: pyproject.toml)
try:
    from importlib.metadata import version as _get_version

    __version__ = _get_version("kicad-tools")
except Exception:
    __version__ = "0.0.0.dev"  # Fallback for editable installs without metadata

# Core S-expression handling
# Constraints - Multi-stage optimization locking
from kicad_tools.constraints import (
    ConstraintManager,
    ConstraintViolation,
    LockType,
)
from kicad_tools.core.sexp_file import load_pcb, load_schematic, save_pcb, save_schematic

# Progress callback infrastructure
from kicad_tools.progress import (
    ProgressCallback,
    ProgressContext,
    ProgressEvent,
    SubProgressCallback,
    create_json_callback,
    create_print_callback,
    get_current_callback,
    null_progress,
    report_progress,
)

# Project
from kicad_tools.project import Project

# Query API
from kicad_tools.query import (
    BaseQuery,
    FootprintList,
    FootprintQuery,
    SymbolList,
    SymbolQuery,
)
from kicad_tools.schema.bom import BOM, BOMItem, extract_bom
from kicad_tools.schema.pcb import PCB

# Schema models
from kicad_tools.schema.schematic import Schematic
from kicad_tools.schema.symbol import SymbolInstance
from kicad_tools.sexp import SExp

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
    # Reasoning - LLM-driven PCB layout
    "PCBReasoningAgent",
    "PCBState",
    "CommandInterpreter",
    # Constraints - Multi-stage optimization locking
    "ConstraintManager",
    "ConstraintViolation",
    "LockType",
    # Progress - callback infrastructure for long operations
    "ProgressCallback",
    "ProgressContext",
    "ProgressEvent",
    "SubProgressCallback",
    "create_json_callback",
    "create_print_callback",
    "get_current_callback",
    "null_progress",
    "report_progress",
]

# Issue #5240: ``kicad_tools.reasoning`` (the LLM-driven layout agent) is by
# far the most expensive part of importing this package -- measured
# locally, ``from kicad_tools.reasoning import agent`` alone accounts for
# ~4.5s of this module's ~5.3s total import cost (its own transitive chain
# pulls in ``drc``/``validate``/``analysis``), yet nothing in this repo's
# src/tests/boards/scripts imports ``PCBReasoningAgent``, ``PCBState``, or
# ``CommandInterpreter`` from the top-level ``kicad_tools`` package -- only
# ``kicad_tools.reasoning`` directly (the ``kct reason`` command path).
# Every OTHER ``kct <command>`` and every test/recipe that merely does
# ``import kicad_tools`` (or ``from kicad_tools import Schematic, PCB,
# ...``) was paying this cost regardless of whether it ever touches
# reasoning.  Resolve these three names lazily (:pep:`562`) instead: the
# first actual access still imports ``kicad_tools.reasoning`` exactly as
# before (nothing loses functionality -- ``from kicad_tools import
# PCBReasoningAgent`` keeps working), but the cost is no longer paid by
# every other import of this package.
_LAZY_REASONING_ATTRS = frozenset({"PCBReasoningAgent", "PCBState", "CommandInterpreter"})


def __getattr__(name: str):
    if name in _LAZY_REASONING_ATTRS:
        from kicad_tools import reasoning as _reasoning

        value = getattr(_reasoning, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_REASONING_ATTRS)
