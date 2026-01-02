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
"""

import uuid
from enum import Enum

from kicad_tools.sexp import SExp

from ..grid import DEFAULT_GRID
from .elements import (
    HierarchicalLabel,
    Junction,
    Label,
    PowerSymbol,
    Wire,
)
from .elements_mixin import SchematicElementsMixin
from .io_mixin import SchematicIOMixin
from .layout_mixin import SchematicLayoutMixin
from .modification_mixin import SchematicModificationMixin
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
        self.labels: list[Label] = []
        self.hier_labels: list[HierarchicalLabel] = []
        self.text_notes: list[tuple[str, float, float]] = []

        # Cache for loaded symbol definitions
        self._symbol_defs: dict[str, SymbolDef] = {}
        self._pwr_counter = 1

        # Embedded lib_symbols from loaded schematics (preserved for round-trip)
        self._embedded_lib_symbols: dict[str, SExp] = {}

    @property
    def sheet_path(self) -> str:
        """Get the sheet path for this schematic."""
        if self.parent_uuid:
            return f"/{self.parent_uuid}/{self.sheet_uuid}"
        return f"/{self.sheet_uuid}"
