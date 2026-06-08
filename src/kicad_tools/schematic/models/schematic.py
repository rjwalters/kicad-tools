"""
KiCad Schematic Model

Main Schematic class and SnapMode enum for schematic document management.

This module composes functionality from specialized mixins:
- SchematicIOMixin: File loading and saving
- SchematicElementsMixin: Adding basic elements (symbols, wires, labels)
- SchematicWiringMixin: Specialized wiring helpers
- SchematicQueryMixin: Finding and querying elements
- SchematicLayoutMixin: Auto-layout and overlap detection
- SchematicModificationMixin: Removing elements
- SchematicValidationMixin: Validation and statistics
- SchematicNetlistMixin: Netlist extraction and connectivity queries
"""

import uuid
from enum import Enum
from pathlib import Path

from kicad_tools.sexp import SExp

from ..grid import DEFAULT_GRID
from .elements import (
    GlobalLabel,
    HierarchicalLabel,
    Junction,
    Label,
    NoConnect,
    PowerSymbol,
    Wire,
)
from .elements_mixin import SchematicElementsMixin
from .io_mixin import SchematicIOMixin
from .layout_mixin import SchematicLayoutMixin
from .modification_mixin import SchematicModificationMixin
from .netlist_mixin import SchematicNetlistMixin
from .query_mixin import SchematicQueryMixin
from .symbol import SymbolDef, SymbolInstance
from .validation_mixin import SchematicValidationMixin
from .wiring_mixin import SchematicWiringMixin


class SnapMode(Enum):
    """Grid snapping behavior modes."""

    OFF = "off"  # No snapping, no warnings
    WARN = "warn"  # Don't snap but warn on off-grid coordinates
    AUTO = "auto"  # Automatically snap to grid (default)
    STRICT = "strict"  # Snap and warn if original was off-grid


class Schematic(
    SchematicIOMixin,
    SchematicElementsMixin,
    SchematicWiringMixin,
    SchematicQueryMixin,
    SchematicLayoutMixin,
    SchematicModificationMixin,
    SchematicValidationMixin,
    SchematicNetlistMixin,
):
    """KiCad schematic document.

    This class provides a complete API for creating, loading, modifying,
    and saving KiCad schematic files (.kicad_sch).

    The functionality is organized into mixins for maintainability:
    - I/O: load(), write(), to_sexp()
    - Elements: add_symbol(), add_wire(), add_label(), etc.
    - Wiring: wire_pins(), wire_to_rail(), add_decoupling_pair(), etc.
    - Query: find_symbol(), find_wires(), find_label(), etc.
    - Layout: suggest_position(), find_overlapping_symbols()
    - Modification: remove_symbol(), remove_wire(), remove_net(), etc.
    - Validation: validate(), get_statistics()
    - Netlist: extract_netlist(), get_net_for_pin(), pins_on_net(), are_connected()

    Example:
        # Create a new schematic
        sch = Schematic("My Design")
        r1 = sch.add_symbol("Device:R", 100, 50, "R1", "10k")
        sch.add_wire((100, 50), (150, 50))
        sch.write("my_design.kicad_sch")

        # Load and modify existing schematic
        sch = Schematic.load("existing.kicad_sch")
        sch.add_symbol("Device:C", 200, 100, "C1", "100nF")
        sch.write("existing.kicad_sch")

        # Query connectivity
        assert sch.are_connected("U1", "VO", "C1", "1")
        print(sch.pins_on_net("+3.3V"))
    """

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
        local_symbol_libs: list[Path] | None = None,
    ):
        """Initialize a new schematic.

        Args:
            title: Schematic title (shown in title block)
            date: Date string (default: "2025-01")
            revision: Revision string (default: "A")
            company: Company name
            comment1: First comment line
            comment2: Second comment line
            paper: Paper size (default: "A4")
            project_name: Project name for sheet instances
            sheet_uuid: UUID for this sheet (auto-generated if None)
            parent_uuid: UUID of parent sheet (for hierarchical designs)
            page: Page number string
            grid: Grid spacing in mm (default: 2.54)
            snap_mode: Grid snapping behavior (default: SnapMode.AUTO)
            local_symbol_libs: Optional list of project-local ``.kicad_sym``
                files to consult during symbol lookup *in addition to* the
                stock KiCad symbol library paths.  Each entry must be a
                path to a ``.kicad_sym`` file (NOT a directory).  When
                provided, ``add_symbol()`` can resolve ``LIBNAME:SYMNAME``
                ids whose ``LIBNAME`` matches the file stem of any entry.
                Default ``None`` preserves prior behavior (stock libs only).
                See :meth:`resolve_lib_path` for the search semantics.
        """
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

        # Element collections
        self.symbols: list[SymbolInstance] = []
        self.power_symbols: list[PowerSymbol] = []
        self.wires: list[Wire] = []
        self.junctions: list[Junction] = []
        self.no_connects: list[NoConnect] = []
        self.labels: list[Label] = []
        self.hier_labels: list[HierarchicalLabel] = []
        self.global_labels: list[GlobalLabel] = []
        self.text_notes: list[tuple[str, float, float]] = []

        # Cache for loaded symbol definitions
        self._symbol_defs: dict[str, SymbolDef] = {}
        self._pwr_counter = 1

        # Embedded lib_symbols from loaded schematics (preserved for round-trip)
        self._embedded_lib_symbols: dict[str, SExp] = {}

        # Synthesized power-symbol lib_symbol definitions (per net name).
        # Populated by ``add_pwr_symbol()`` so that #PWR symbols can publish
        # arbitrary global net names (e.g. ``VMOTOR``, ``+3.3V``) without
        # relying on KiCad's stock ``power:`` library — whose symbol names
        # bake in the net name (``+24V``, ``+3V3``) and therefore drive the
        # WRONG global net when project convention uses ``VMOTOR`` / ``+3.3V``
        # as rail labels.  Key is the net name (lib_id local part).
        self._synthesized_pwr_defs: dict[str, SExp] = {}

        # Track continuous rails for T-connection warnings
        # Each entry: (y, x_start, x_end) for horizontal rails
        self._continuous_rails: list[tuple[float, float, float]] = []

        # Track the saved path for operations like run_erc()
        self._saved_path: Path | None = None

        # Project-local symbol libraries.  Each entry is a path to a
        # ``.kicad_sym`` file; symbol lookups consult these *before*
        # falling through to the stock KiCad library paths (matching
        # KiCad's own project-local library precedence).
        self.local_symbol_libs: list[Path] = list(local_symbol_libs or [])

    def resolve_lib_path(self, lib_name: str) -> Path | None:
        """Resolve a library name to a ``.kicad_sym`` file path.

        Searches ``self.local_symbol_libs`` first (project-local libs win
        over stock libs, matching KiCad's project-table precedence).
        Returns ``None`` if no match is found in the local libs — callers
        should fall through to the global ``KICAD_SYMBOL_PATHS`` search
        in that case.

        Args:
            lib_name: The library name portion of a lib_id (e.g.,
                ``"softstart_custom"`` from ``"softstart_custom:UCC27211"``).

        Returns:
            Path to the matching ``.kicad_sym`` file, or ``None`` if no
            local lib matches.
        """
        target = f"{lib_name}.kicad_sym"
        for lib_path in self.local_symbol_libs:
            if lib_path.name == target and lib_path.exists():
                return lib_path
        return None

    @property
    def sheet_path(self) -> str:
        """Get the sheet path for this schematic."""
        if self.parent_uuid:
            return f"/{self.parent_uuid}/{self.sheet_uuid}"
        return f"/{self.sheet_uuid}"
