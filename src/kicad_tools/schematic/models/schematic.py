"""
KiCad Schematic Model

Main Schematic class and SnapMode enum for schematic document management.
"""

import uuid
import warnings
from enum import Enum
from pathlib import Path
from typing import Optional

from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import (
    sheet_instances,
    text_node,
    title_block,
    uuid_node,
)

from ..grid import DEFAULT_GRID, is_on_grid, snap_to_grid
from ..logging import _log_debug, _log_info, _log_warning
from .elements import (
    HierarchicalLabel,
    Junction,
    Label,
    PowerSymbol,
    Wire,
)
from .symbol import SymbolDef, SymbolInstance


class SnapMode(Enum):
    """Grid snapping behavior modes."""

    OFF = "off"  # No snapping, no warnings
    WARN = "warn"  # Don't snap but warn on off-grid coordinates
    AUTO = "auto"  # Automatically snap to grid (default)
    STRICT = "strict"  # Snap and warn if original was off-grid


class Schematic:
    """KiCad schematic document."""

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

    @classmethod
    def load(cls, path: str | Path) -> "Schematic":
        """Load a schematic from a .kicad_sch file.

        This enables round-trip editing: load -> modify -> save.

        Args:
            path: Path to the .kicad_sch file

        Returns:
            A Schematic instance populated with all elements from the file

        Example:
            sch = Schematic.load("power.kicad_sch")
            sch.add_symbol("Device:R", 100, 100, "R5", "10k")
            sch.write("power.kicad_sch")
        """
        from kicad_sexp import parse_file

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Schematic file not found: {path}")

        doc = parse_file(path)
        return cls._from_sexp(doc)

    @classmethod
    def _from_sexp(cls, doc: SExp) -> "Schematic":
        """Create a Schematic from a parsed S-expression tree.

        This is the internal method that does the actual parsing.
        """
        # Extract title block info
        title = ""
        date = ""
        revision = ""
        company = ""
        comment1 = ""
        comment2 = ""

        tb = doc.get("title_block")
        if tb:
            title_node = tb.get("title")
            if title_node:
                title = str(title_node.get_first_atom() or "")
            date_node = tb.get("date")
            if date_node:
                date = str(date_node.get_first_atom() or "")
            rev_node = tb.get("rev")
            if rev_node:
                revision = str(rev_node.get_first_atom() or "")
            company_node = tb.get("company")
            if company_node:
                company = str(company_node.get_first_atom() or "")
            # Comments are numbered
            for comment_node in tb.find_all("comment"):
                atoms = comment_node.get_atoms()
                if len(atoms) >= 2:
                    num = int(atoms[0])
                    text = str(atoms[1])
                    if num == 1:
                        comment1 = text
                    elif num == 2:
                        comment2 = text

        # Get paper size
        paper_node = doc.get("paper")
        paper = str(paper_node.get_first_atom()) if paper_node else "A4"

        # Get UUID
        uuid_node_elem = doc.get("uuid")
        sheet_uuid = str(uuid_node_elem.get_first_atom()) if uuid_node_elem else str(uuid.uuid4())

        # Parse lib_symbols to get embedded symbol definitions
        embedded_lib_symbols: dict[str, SExp] = {}
        lib_symbols_node = doc.get("lib_symbols")
        if lib_symbols_node:
            for sym_node in lib_symbols_node.children:
                if sym_node.name == "symbol":
                    sym_name = str(sym_node.get_first_atom())
                    embedded_lib_symbols[sym_name] = sym_node

        # Create schematic instance with minimal init
        # We disable snapping for loaded schematics to preserve coordinates
        sch = cls(
            title=title,
            date=date,
            revision=revision,
            company=company,
            comment1=comment1,
            comment2=comment2,
            paper=paper,
            sheet_uuid=sheet_uuid,
            snap_mode=SnapMode.OFF,  # Preserve original coordinates
        )

        # Store embedded lib_symbols for round-trip
        sch._embedded_lib_symbols = embedded_lib_symbols

        # Parse sheet_instances to get project name and parent info
        sheet_instances_node = doc.get("sheet_instances")
        if sheet_instances_node:
            project_node = sheet_instances_node.get("project")
            if project_node:
                sch.project_name = str(project_node.get_first_atom() or "project")
                path_node = project_node.get("path")
                if path_node:
                    page_node = path_node.get("page")
                    if page_node:
                        sch.page = str(page_node.get_first_atom() or "1")

        # Parse placed symbols (those with lib_id)
        for child in doc.children:
            if child.name == "symbol" and child.get("lib_id"):
                if PowerSymbol.is_power_symbol(child):
                    pwr = PowerSymbol.from_sexp(child)
                    sch.power_symbols.append(pwr)
                else:
                    sym = SymbolInstance.from_sexp(
                        child, symbol_defs=sch._symbol_defs, lib_symbols=embedded_lib_symbols
                    )
                    sch.symbols.append(sym)
                    # Cache the symbol def
                    sch._symbol_defs[sym.symbol_def.lib_id] = sym.symbol_def

        # Parse wires
        for child in doc.children:
            if child.name == "wire":
                sch.wires.append(Wire.from_sexp(child))

        # Parse junctions
        for child in doc.children:
            if child.name == "junction":
                sch.junctions.append(Junction.from_sexp(child))

        # Parse labels
        for child in doc.children:
            if child.name == "label":
                sch.labels.append(Label.from_sexp(child))

        # Parse hierarchical labels
        for child in doc.children:
            if child.name == "hierarchical_label":
                sch.hier_labels.append(HierarchicalLabel.from_sexp(child))

        # Parse text notes
        for child in doc.children:
            if child.name == "text":
                text = str(child.get_first_atom() or "")
                at_node = child.get("at")
                if at_node:
                    atoms = at_node.get_atoms()
                    x = round(float(atoms[0]), 2)
                    y = round(float(atoms[1]), 2)
                    sch.text_notes.append((text, x, y))

        # Update power counter based on existing power symbols
        max_pwr = 0
        for pwr in sch.power_symbols:
            # Extract number from #PWR01, #PWR02, etc.
            if pwr.reference.startswith("#PWR"):
                try:
                    num = int(pwr.reference[4:])
                    max_pwr = max(max_pwr, num)
                except ValueError:
                    pass
        sch._pwr_counter = max_pwr + 1

        _log_info(
            f"Loaded schematic: {len(sch.symbols)} symbols, "
            f"{len(sch.power_symbols)} power symbols, "
            f"{len(sch.wires)} wires"
        )

        return sch

    @property
    def sheet_path(self) -> str:
        if self.parent_uuid:
            return f"/{self.parent_uuid}/{self.sheet_uuid}"
        return f"/{self.sheet_uuid}"

    def _snap_coord(self, value: float, context: str = "") -> float:
        """Apply grid snapping to a single coordinate based on snap_mode.

        Args:
            value: Coordinate value
            context: Context for warning messages

        Returns:
            Snapped or original value based on snap_mode
        """
        if self.snap_mode == SnapMode.OFF:
            return round(value, 2)

        on_grid = is_on_grid(value, self.grid)

        if self.snap_mode == SnapMode.WARN:
            if not on_grid:
                snapped = snap_to_grid(value, self.grid)
                warnings.warn(
                    f"Off-grid coordinate ({context}): {value} -> nearest: {snapped}", stacklevel=4
                )
            return round(value, 2)

        if self.snap_mode == SnapMode.STRICT:
            if not on_grid:
                snapped = snap_to_grid(value, self.grid)
                warnings.warn(
                    f"Auto-snapping off-grid coordinate ({context}): {value} -> {snapped}",
                    stacklevel=4,
                )
            return snap_to_grid(value, self.grid)

        # SnapMode.AUTO - silently snap
        return snap_to_grid(value, self.grid)

    def _snap_point(self, point: tuple[float, float], context: str = "") -> tuple[float, float]:
        """Apply grid snapping to a point based on snap_mode.

        Args:
            point: (x, y) coordinate tuple
            context: Context for warning messages

        Returns:
            Snapped or original point based on snap_mode
        """
        return (
            self._snap_coord(point[0], f"{context} x"),
            self._snap_coord(point[1], f"{context} y"),
        )

    def add_symbol(
        self,
        lib_id: str,
        x: float,
        y: float,
        ref: str,
        value: str = None,
        rotation: float = 0,
        footprint: str = "",
        snap: bool = True,
    ) -> SymbolInstance:
        """Add a symbol to the schematic.

        Args:
            lib_id: Library:Symbol format (e.g., "Audio:PCM5122PW")
            x, y: Center position (snapped to grid unless snap=False)
            ref: Reference designator (e.g., "U1")
            value: Value (defaults to symbol name)
            rotation: Rotation in degrees (0, 90, 180, 270)
            footprint: Footprint string
            snap: Whether to apply grid snapping (default: True)

        Returns:
            SymbolInstance with pin_position() method
        """
        # Apply grid snapping if enabled
        if snap:
            x = self._snap_coord(x, f"symbol {ref}")
            y = self._snap_coord(y, f"symbol {ref}")

        # Load symbol definition if not cached
        if lib_id not in self._symbol_defs:
            self._symbol_defs[lib_id] = SymbolDef.from_library(lib_id)

        sym_def = self._symbol_defs[lib_id]

        instance = SymbolInstance(
            symbol_def=sym_def,
            x=x,
            y=y,
            rotation=rotation,
            reference=ref,
            value=value or sym_def.name,
            footprint=footprint,
        )

        self.symbols.append(instance)
        _log_info(f"Added symbol {ref} ({lib_id}) at ({x}, {y})")
        _log_debug(f"  Symbol {ref} has {len(sym_def.pins)} pins")
        return instance

    def add_power(
        self, lib_id: str, x: float, y: float, rotation: float = 0, snap: bool = True
    ) -> PowerSymbol:
        """Add a power symbol (GND, VCC, etc.).

        Args:
            lib_id: Power symbol library ID (e.g., "power:GND")
            x, y: Position (snapped to grid unless snap=False)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
        """
        ref = f"#PWR{self._pwr_counter:02d}"
        self._pwr_counter += 1

        # Apply grid snapping if enabled
        if snap:
            x = self._snap_coord(x, f"power {lib_id}")
            y = self._snap_coord(y, f"power {lib_id}")
        else:
            x = round(x, 2)
            y = round(y, 2)

        # Load power symbol definition
        if lib_id not in self._symbol_defs:
            self._symbol_defs[lib_id] = SymbolDef.from_library(lib_id)

        pwr = PowerSymbol(
            lib_id=lib_id,
            x=x,
            y=y,
            rotation=rotation,
            reference=ref,
            _symbol_def=self._symbol_defs[lib_id],
        )
        self.power_symbols.append(pwr)
        _log_info(f"Added power symbol {lib_id.split(':')[1]} at ({x}, {y})")
        return pwr

    def add_pwr_flag(self, x: float, y: float) -> PowerSymbol:
        """Add a PWR_FLAG symbol to mark a power net as intentionally driven.

        This suppresses ERC errors about power pins not being driven.
        Place on the power net near where power enters the schematic.
        """
        return self.add_power("power:PWR_FLAG", x, y, rotation=0)

    def add_wire(self, p1: tuple[float, float], p2: tuple[float, float], snap: bool = True) -> Wire:
        """Add a wire between two points.

        Args:
            p1: Start point (x, y)
            p2: End point (x, y)
            snap: Whether to apply grid snapping (default: True)

        Returns:
            The Wire created
        """
        # Apply grid snapping if enabled
        if snap:
            p1 = self._snap_point(p1, "wire start")
            p2 = self._snap_point(p2, "wire end")

        wire = Wire.between(p1, p2)
        self.wires.append(wire)
        _log_debug(f"Added wire from ({p1[0]}, {p1[1]}) to ({p2[0]}, {p2[1]})")
        return wire

    def add_wire_path(self, *points: tuple[float, float], snap: bool = True) -> list[Wire]:
        """Add a series of connected wire segments.

        Args:
            points: Sequence of (x, y) points to connect
            snap: Whether to apply grid snapping (default: True)

        Returns:
            List of wires created
        """
        wires = []
        for i in range(len(points) - 1):
            wires.append(self.add_wire(points[i], points[i + 1], snap=snap))
        return wires

    def add_junction(self, x: float, y: float, snap: bool = True) -> Junction:
        """Add a junction point.

        Args:
            x, y: Junction position (snapped to grid unless snap=False)
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, "junction")
            y = self._snap_coord(y, "junction")
        else:
            x = round(x, 2)
            y = round(y, 2)
        junc = Junction(x=x, y=y)
        self.junctions.append(junc)
        return junc

    def add_label(
        self, text: str, x: float, y: float, rotation: float = 0, snap: bool = True
    ) -> Label:
        """Add a net label.

        Args:
            text: Label text
            x, y: Label position (snapped to grid unless snap=False)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, f"label {text}")
            y = self._snap_coord(y, f"label {text}")
        label = Label(text=text, x=x, y=y, rotation=rotation)
        self.labels.append(label)
        return label

    def add_hier_label(
        self,
        text: str,
        x: float,
        y: float,
        shape: str = "input",
        rotation: float = 0,
        snap: bool = True,
    ) -> HierarchicalLabel:
        """Add a hierarchical label.

        Args:
            text: Label text
            x, y: Label position (snapped to grid unless snap=False)
            shape: Label shape (input, output, bidirectional, passive)
            rotation: Rotation in degrees
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, f"hier_label {text}")
            y = self._snap_coord(y, f"hier_label {text}")
        hl = HierarchicalLabel(text=text, x=x, y=y, shape=shape, rotation=rotation)
        self.hier_labels.append(hl)
        return hl

    def add_text(self, text: str, x: float, y: float, snap: bool = True):
        """Add a text note.

        Args:
            text: Note text
            x, y: Text position (snapped to grid unless snap=False)
            snap: Whether to apply grid snapping (default: True)
        """
        if snap:
            x = self._snap_coord(x, "text")
            y = self._snap_coord(y, "text")
        self.text_notes.append((text, x, y))

    # =========================================================================
    # Wiring Helper Methods
    # =========================================================================

    def wire_pin_to_point(
        self,
        symbol: SymbolInstance,
        pin_name: str,
        target: tuple[float, float],
        route: str = "auto",
    ) -> list[Wire]:
        """Wire a symbol pin to a target point using orthogonal routing.

        Args:
            symbol: The symbol instance
            pin_name: Name or number of the pin
            target: (x, y) destination point
            route: Routing style - "auto", "vertical_first", "horizontal_first"

        Returns:
            List of wires created
        """
        pin_pos = symbol.pin_position(pin_name)
        return self._route_orthogonal(pin_pos, target, route)

    def wire_pins(
        self, sym1: SymbolInstance, pin1: str, sym2: SymbolInstance, pin2: str, route: str = "auto"
    ) -> list[Wire]:
        """Wire two symbol pins together using orthogonal routing.

        Args:
            sym1: First symbol
            pin1: Pin name/number on first symbol
            sym2: Second symbol
            pin2: Pin name/number on second symbol
            route: Routing style - "auto", "vertical_first", "horizontal_first"

        Returns:
            List of wires created
        """
        p1 = sym1.pin_position(pin1)
        p2 = sym2.pin_position(pin2)
        return self._route_orthogonal(p1, p2, route)

    def wire_to_rail(
        self,
        symbol: SymbolInstance,
        pin_name: str,
        rail_y: float,
        extend_to_x: float = None,
        add_junction: bool = True,
    ) -> list[Wire]:
        """Connect a pin vertically to a horizontal rail.

        Args:
            symbol: The symbol instance
            pin_name: Name or number of the pin
            rail_y: Y coordinate of the horizontal rail
            extend_to_x: If set, also add horizontal wire to this X position
            add_junction: Whether to add a junction at the rail (default True)

        Returns:
            List of wires created
        """
        pin_pos = symbol.pin_position(pin_name)
        wires = []

        # Vertical wire from pin to rail
        wires.append(self.add_wire(pin_pos, (pin_pos[0], rail_y)))

        # Add junction at rail intersection
        if add_junction:
            self.add_junction(pin_pos[0], rail_y)

        # Optional horizontal extension
        if extend_to_x is not None and extend_to_x != pin_pos[0]:
            wires.append(self.add_wire((pin_pos[0], rail_y), (extend_to_x, rail_y)))

        return wires

    def add_rail(
        self, y: float, x_start: float, x_end: float, net_label: str = None, snap: bool = True
    ) -> Wire:
        """Add a horizontal power/ground rail.

        Args:
            y: Y coordinate of the rail (snapped to grid)
            x_start: Starting X coordinate (snapped to grid)
            x_end: Ending X coordinate (snapped to grid)
            net_label: Optional net label to add at the start
            snap: Whether to apply grid snapping (default: True)

        Returns:
            The wire created
        """
        wire = self.add_wire((x_start, y), (x_end, y), snap=snap)
        if net_label:
            # Use the actual snapped wire coordinates for the label
            self.add_label(net_label, wire.x1, wire.y1, rotation=0, snap=False)
        return wire

    def wire_power_to_pin(
        self,
        power_lib_id: str,
        symbol: SymbolInstance,
        pin_name: str,
        power_offset: tuple[float, float] = (0, -10),
    ) -> PowerSymbol:
        """Add a power symbol and wire it to a pin.

        Args:
            power_lib_id: Power symbol library ID (e.g., "power:+3.3V")
            symbol: Target symbol
            pin_name: Target pin name
            power_offset: (dx, dy) offset from pin for power symbol placement

        Returns:
            The power symbol created
        """
        pin_pos = symbol.pin_position(pin_name)
        power_x = pin_pos[0] + power_offset[0]
        power_y = pin_pos[1] + power_offset[1]

        pwr = self.add_power(power_lib_id, power_x, power_y)
        self.add_wire(pin_pos, (power_x, power_y))

        return pwr

    def wire_decoupling_cap(
        self,
        cap: SymbolInstance,
        power_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> list[Wire]:
        """Wire a decoupling capacitor between power and ground rails.

        Assumes cap is vertical with pin 1 (top) to power and pin 2 (bottom) to ground.

        Args:
            cap: Capacitor symbol instance
            power_rail_y: Y coordinate of power rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction dots

        Returns:
            List of wires created
        """
        wires = []

        # Get pin positions (Device:C has pins 1 at top, 2 at bottom)
        try:
            pin1_pos = cap.pin_position("1")
            pin2_pos = cap.pin_position("2")
        except ValueError:
            # Try by name if numbered pins don't work
            pin1_pos = cap.pin_position("~")
            pin2_pos = cap.pin_position("~")

        # Wire top to power rail
        wires.append(self.add_wire(pin1_pos, (pin1_pos[0], power_rail_y)))
        if add_junctions:
            self.add_junction(pin1_pos[0], power_rail_y)

        # Wire bottom to ground rail
        wires.append(self.add_wire(pin2_pos, (pin2_pos[0], gnd_rail_y)))
        if add_junctions:
            self.add_junction(pin2_pos[0], gnd_rail_y)

        return wires

    def add_decoupling_pair(
        self,
        x: float,
        y: float,
        ic_pin: tuple[float, float],
        power_symbol: str,
        ref_100nf: str,
        ref_10uf: str,
        grid: float = 2.54,
        footprint_100nf: str = "Capacitor_SMD:C_0402_1005Metric",
        footprint_10uf: str = "Capacitor_SMD:C_0805_2012Metric",
    ) -> dict:
        """Add a decoupling capacitor pair (100nF + 10uF) with power symbol and wiring.

        This is a common pattern for IC power pins: two caps in parallel connected
        between a power symbol and an IC power pin.

        Args:
            x: X coordinate (center of cap pair)
            y: Y coordinate (center of caps)
            ic_pin: (x, y) position of the IC power pin to connect to
            power_symbol: Power symbol lib_id (e.g., "power:+3.3V")
            ref_100nf: Reference designator for 100nF cap
            ref_10uf: Reference designator for 10uF cap
            grid: Grid spacing in mm
            footprint_100nf: Footprint for 100nF cap
            footprint_10uf: Footprint for 10uF cap

        Returns:
            Dictionary with keys: cap_100nf, cap_10uf, power, wires
        """
        wires = []

        # Place capacitors side by side
        cap_100nf = self.add_symbol(
            "Device:C_Small",
            x=x - 2 * grid,
            y=y,
            ref=ref_100nf,
            value="100nF",
            footprint=footprint_100nf,
        )
        cap_10uf = self.add_symbol(
            "Device:C_Small",
            x=x + 2 * grid,
            y=y,
            ref=ref_10uf,
            value="10uF",
            footprint=footprint_10uf,
        )

        # Add power symbol above caps
        power = self.add_power(power_symbol, x=x, y=y - 3 * grid)

        # Wire from caps to power symbol (top side)
        wires.append(self.add_wire((x - 2 * grid, y - grid), (x - 2 * grid, y - 2 * grid)))
        wires.append(self.add_wire((x + 2 * grid, y - grid), (x + 2 * grid, y - 2 * grid)))
        wires.append(self.add_wire((x - 2 * grid, y - 2 * grid), (x + 2 * grid, y - 2 * grid)))
        self.add_junction(x, y - 2 * grid)

        # Wire from caps to IC pin (bottom side)
        wires.append(self.add_wire((x - 2 * grid, y + grid), (x - 2 * grid, y + 2 * grid)))
        wires.append(self.add_wire((x + 2 * grid, y + grid), (x + 2 * grid, y + 2 * grid)))
        wires.append(self.add_wire((x - 2 * grid, y + 2 * grid), (x + 2 * grid, y + 2 * grid)))
        self.add_junction(x, y + 2 * grid)

        # Wire from center of bottom bus to IC pin
        wires.append(self.add_wire((x, y + 2 * grid), ic_pin))

        _log_info(f"Added decoupling pair {ref_100nf}/{ref_10uf} at ({x}, {y}) -> IC pin")

        return {
            "cap_100nf": cap_100nf,
            "cap_10uf": cap_10uf,
            "power": power,
            "wires": wires,
        }

    def tie_pins_to_power(
        self,
        symbol: SymbolInstance,
        pin_names: list[str],
        power_symbol: str,
        x_offset: float = -4 * 2.54,
        grid: float = 2.54,
    ) -> PowerSymbol:
        """Tie multiple IC pins to a common power symbol.

        Args:
            symbol: The IC symbol instance
            pin_names: List of pin names to tie together
            power_symbol: Power symbol lib_id
            x_offset: X offset from pins for the power symbol
            grid: Grid spacing

        Returns:
            The PowerSymbol instance created
        """
        if not pin_names:
            raise ValueError("pin_names must not be empty")

        # Get all pin positions
        pin_positions = [symbol.pin_position(name) for name in pin_names]

        # Calculate center Y for power symbol
        y_coords = [p[1] for p in pin_positions]
        center_y = (min(y_coords) + max(y_coords)) / 2
        first_pin_x = pin_positions[0][0]

        # Power symbol position
        pwr_x = first_pin_x + x_offset
        pwr_y = center_y + 3 * grid if "GND" in power_symbol else center_y - 3 * grid

        # Add power symbol
        pwr = self.add_power(power_symbol, x=pwr_x, y=pwr_y)

        # Wire all pins to the common bus line
        bus_x = first_pin_x + x_offset

        for i, (pin_pos, pin_name) in enumerate(zip(pin_positions, pin_names)):
            # Horizontal wire from pin to bus
            self.add_wire(pin_pos, (bus_x, pin_pos[1]))

            # Vertical wire to next pin (if not last)
            if i < len(pin_positions) - 1:
                next_pin_y = pin_positions[i + 1][1]
                self.add_wire((bus_x, pin_pos[1]), (bus_x, next_pin_y))
                self.add_junction(bus_x, next_pin_y)

        # Wire from bus to power symbol
        last_pin_y = pin_positions[-1][1]
        self.add_wire((bus_x, last_pin_y), (bus_x, pwr_y))

        _log_info(f"Tied pins {pin_names} to {power_symbol}")

        return pwr

    def wire_ldo(
        self,
        ldo: SymbolInstance,
        input_rail_y: float,
        output_rail_y: float,
        gnd_rail_y: float,
        tie_en_to_vin: bool = True,
    ) -> list[Wire]:
        """Wire an LDO regulator to power rails.

        Args:
            ldo: LDO symbol instance
            input_rail_y: Y coordinate of input voltage rail
            output_rail_y: Y coordinate of output voltage rail
            gnd_rail_y: Y coordinate of ground rail
            tie_en_to_vin: Whether to tie EN pin to VIN

        Returns:
            List of wires created
        """
        wires = []

        # Get pin positions
        vin_pos = ldo.pin_position("VIN")
        vout_pos = ldo.pin_position("VOUT")
        gnd_pos = ldo.pin_position("GND")
        en_pos = ldo.pin_position("EN")

        # VIN to input rail
        wires.append(self.add_wire(vin_pos, (vin_pos[0], input_rail_y)))
        self.add_junction(vin_pos[0], input_rail_y)

        # VOUT to output rail
        wires.append(self.add_wire(vout_pos, (vout_pos[0], output_rail_y)))
        self.add_junction(vout_pos[0], output_rail_y)

        # GND to ground rail
        wires.append(self.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y)))
        self.add_junction(gnd_pos[0], gnd_rail_y)

        # EN tied to VIN if requested
        if tie_en_to_vin:
            wires.append(self.add_wire(en_pos, (en_pos[0], vin_pos[1])))
            self.add_junction(en_pos[0], vin_pos[1])

        return wires

    def _route_orthogonal(
        self, start: tuple[float, float], end: tuple[float, float], route: str = "auto"
    ) -> list[Wire]:
        """Route between two points using orthogonal (Manhattan) routing.

        Args:
            start: Starting point (x, y)
            end: Ending point (x, y)
            route: "auto", "vertical_first", or "horizontal_first"

        Returns:
            List of wires created
        """
        x1, y1 = start
        x2, y2 = end

        # If points are aligned, single wire
        if x1 == x2 or y1 == y2:
            return [self.add_wire(start, end)]

        # Choose routing direction
        if route == "auto":
            if abs(x2 - x1) < abs(y2 - y1):
                route = "horizontal_first"
            else:
                route = "vertical_first"

        if route == "horizontal_first":
            mid = (x2, y1)
        else:  # vertical_first
            mid = (x1, y2)

        return [self.add_wire(start, mid), self.add_wire(mid, end)]

    def connect_hier_label_to_pin(
        self,
        label_name: str,
        symbol: SymbolInstance,
        pin_name: str,
        label_offset: float = 15,
        shape: str = None,
    ) -> HierarchicalLabel:
        """Add a hierarchical label connected to a symbol pin.

        Args:
            label_name: Name for the hierarchical label
            symbol: Target symbol
            pin_name: Target pin name
            label_offset: Horizontal offset from pin for label placement
            shape: Override shape

        Returns:
            The hierarchical label created
        """
        pin_pos = symbol.pin_position(pin_name)

        # Determine label direction based on pin position relative to symbol center
        if pin_pos[0] < symbol.x:
            label_x = pin_pos[0] - label_offset
            auto_shape = "input"
            rotation = 0
        else:
            label_x = pin_pos[0] + label_offset
            auto_shape = "output"
            rotation = 180

        final_shape = shape if shape else auto_shape

        # Add wire from pin to label position
        self.add_wire(pin_pos, (label_x, pin_pos[1]))

        # Add the label
        return self.add_hier_label(label_name, label_x, pin_pos[1], final_shape, rotation)

    def wire_bus(
        self,
        symbols_and_pins: list[tuple[SymbolInstance, str]],
        bus_y: float = None,
        bus_x: float = None,
    ) -> list[Wire]:
        """Wire multiple pins to a common bus line.

        Either bus_y (horizontal bus) or bus_x (vertical bus) must be specified.

        Args:
            symbols_and_pins: List of (symbol, pin_name) tuples to connect
            bus_y: Y coordinate for horizontal bus
            bus_x: X coordinate for vertical bus

        Returns:
            List of wires created
        """
        if bus_y is None and bus_x is None:
            raise ValueError("Either bus_y or bus_x must be specified")

        wires = []
        bus_points = []

        for symbol, pin_name in symbols_and_pins:
            pin_pos = symbol.pin_position(pin_name)

            if bus_y is not None:
                bus_point = (pin_pos[0], bus_y)
            else:
                bus_point = (bus_x, pin_pos[1])

            wires.append(self.add_wire(pin_pos, bus_point))
            bus_points.append(bus_point)

        # Sort bus points and create bus wire
        if bus_y is not None:
            bus_points.sort(key=lambda p: p[0])
            if len(bus_points) > 1:
                wires.append(self.add_wire(bus_points[0], bus_points[-1]))
        else:
            bus_points.sort(key=lambda p: p[1])
            if len(bus_points) > 1:
                wires.append(self.add_wire(bus_points[0], bus_points[-1]))

        # Add junctions at bus connection points
        for point in bus_points:
            self.add_junction(point[0], point[1])

        return wires

    def print_symbol_pins(self, symbol: SymbolInstance, name: str = None):
        """Debug helper: Print all pin positions for a symbol."""
        display_name = name or symbol.reference
        print(f"\n{display_name} pins at ({symbol.x}, {symbol.y}) rot={symbol.rotation}:")
        for pin in symbol.symbol_def.pins:
            pos = symbol.pin_position(pin.name)
            print(f"  {pin.name} ({pin.number}): ({pos[0]:.2f}, {pos[1]:.2f})")

    def wire_ferrite_bead(self, fb: SymbolInstance, rail1_y: float, rail2_y: float) -> list[Wire]:
        """Wire a ferrite bead between two ground rails."""
        wires = []

        pin1_pos = fb.pin_position("1")
        pin2_pos = fb.pin_position("2")

        wires.append(self.add_wire(pin1_pos, (pin1_pos[0], rail1_y)))
        self.add_junction(pin1_pos[0], rail1_y)

        wires.append(self.add_wire(pin2_pos, (pin2_pos[0], rail2_y)))
        self.add_junction(pin2_pos[0], rail2_y)

        return wires

    def wire_oscillator(
        self,
        osc: SymbolInstance,
        power_rail_y: float,
        gnd_rail_y: float,
        output_label: str = None,
        tie_en_to_vdd: bool = True,
    ) -> list[Wire]:
        """Wire an oscillator to power rails.

        Args:
            osc: Oscillator symbol instance
            power_rail_y: Y coordinate of power rail
            gnd_rail_y: Y coordinate of ground rail
            output_label: If set, add a label at the output
            tie_en_to_vdd: Whether to tie EN pin to Vdd

        Returns:
            List of wires created
        """
        wires = []

        try:
            vdd_pos = osc.pin_position("Vdd")
            gnd_pos = osc.pin_position("GND")
            out_pos = osc.pin_position("OUT")
            en_pos = osc.pin_position("EN")
        except ValueError:
            # Try alternate pin names
            vdd_pos = osc.pin_position("4")
            gnd_pos = osc.pin_position("2")
            out_pos = osc.pin_position("3")
            en_pos = osc.pin_position("1")

        # Vdd to power rail
        wires.append(self.add_wire(vdd_pos, (vdd_pos[0], power_rail_y)))
        self.add_junction(vdd_pos[0], power_rail_y)

        # GND to ground rail
        wires.append(self.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y)))
        self.add_junction(gnd_pos[0], gnd_rail_y)

        # EN tied to Vdd if requested
        if tie_en_to_vdd:
            wires.extend(self._route_orthogonal(en_pos, (vdd_pos[0], power_rail_y)))
            self.add_junction(vdd_pos[0], power_rail_y)

        # Output label
        if output_label:
            self.add_label(output_label, out_pos[0] + 5, out_pos[1])
            wires.append(self.add_wire(out_pos, (out_pos[0] + 5, out_pos[1])))

        return wires

    # =========================================================================
    # Query Methods
    # =========================================================================

    def find_wires(
        self,
        endpoint: tuple[float, float] = None,
        near: tuple[float, float] = None,
        tolerance: float = None,
        connected_to_label: str = None,
    ) -> list[Wire]:
        """Find wires matching specified criteria."""
        if tolerance is None:
            tolerance = self.grid

        results = []

        if connected_to_label:
            label_pos = None
            for label in self.labels:
                if label.text == connected_to_label:
                    label_pos = (label.x, label.y)
                    break
            if label_pos is None:
                for hl in self.hier_labels:
                    if hl.text == connected_to_label:
                        label_pos = (hl.x, hl.y)
                        break
            if label_pos is None:
                return []
            near = label_pos

        for wire in self.wires:
            wire_p1 = (wire.x1, wire.y1)
            wire_p2 = (wire.x2, wire.y2)

            if endpoint:
                if self._points_equal(wire_p1, endpoint) or self._points_equal(wire_p2, endpoint):
                    results.append(wire)
            elif near:
                if self._point_near(wire_p1, near, tolerance) or self._point_near(
                    wire_p2, near, tolerance
                ):
                    results.append(wire)
            else:
                results.append(wire)

        return results

    def find_label(self, name: str) -> Optional[Label]:
        """Find a label by exact name."""
        for label in self.labels:
            if label.text == name:
                return label
        return None

    def find_labels(self, pattern: str = None) -> list[Label]:
        """Find labels matching a pattern."""
        import fnmatch

        if pattern is None:
            return list(self.labels)
        return [lbl for lbl in self.labels if fnmatch.fnmatch(lbl.text, pattern)]

    def find_hier_label(self, name: str) -> Optional[HierarchicalLabel]:
        """Find a hierarchical label by exact name."""
        for hl in self.hier_labels:
            if hl.text == name:
                return hl
        return None

    def find_hier_labels(self, pattern: str = None) -> list[HierarchicalLabel]:
        """Find hierarchical labels matching a pattern."""
        import fnmatch

        if pattern is None:
            return list(self.hier_labels)
        return [hl for hl in self.hier_labels if fnmatch.fnmatch(hl.text, pattern)]

    def find_symbol(self, reference: str) -> Optional[SymbolInstance]:
        """Find a symbol by reference designator."""
        for sym in self.symbols:
            if sym.reference == reference:
                return sym
        return None

    def find_symbols(self, pattern: str = None) -> list[SymbolInstance]:
        """Find symbols matching a pattern."""
        import fnmatch

        if pattern is None:
            return list(self.symbols)
        return [s for s in self.symbols if fnmatch.fnmatch(s.reference, pattern)]

    def _points_equal(
        self, p1: tuple[float, float], p2: tuple[float, float], tolerance: float = 0.01
    ) -> bool:
        """Check if two points are equal within tolerance."""
        return abs(p1[0] - p2[0]) < tolerance and abs(p1[1] - p2[1]) < tolerance

    def _point_near(
        self, p1: tuple[float, float], p2: tuple[float, float], tolerance: float
    ) -> bool:
        """Check if two points are within tolerance distance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) <= (tolerance * tolerance)

    # =========================================================================
    # Removal Methods
    # =========================================================================

    def remove_wire(self, wire: Wire) -> bool:
        """Remove a specific wire from the schematic."""
        try:
            self.wires.remove(wire)
            _log_info(f"Removed wire from ({wire.x1}, {wire.y1}) to ({wire.x2}, {wire.y2})")
            return True
        except ValueError:
            return False

    def remove_wires_at(self, point: tuple[float, float], tolerance: float = None) -> int:
        """Remove all wires with an endpoint at or near a point."""
        if tolerance is None:
            tolerance = self.grid

        wires_to_remove = self.find_wires(near=point, tolerance=tolerance)
        for wire in wires_to_remove:
            self.wires.remove(wire)

        if wires_to_remove:
            _log_info(f"Removed {len(wires_to_remove)} wire(s) near ({point[0]}, {point[1]})")

        return len(wires_to_remove)

    def remove_label(self, name: str) -> bool:
        """Remove a label by name."""
        label = self.find_label(name)
        if label:
            self.labels.remove(label)
            _log_info(f"Removed label '{name}' at ({label.x}, {label.y})")
            return True
        return False

    def remove_hier_label(self, name: str) -> bool:
        """Remove a hierarchical label by name."""
        hl = self.find_hier_label(name)
        if hl:
            self.hier_labels.remove(hl)
            _log_info(f"Removed hierarchical label '{name}' at ({hl.x}, {hl.y})")
            return True
        return False

    def remove_net(self, name: str, tolerance: float = None) -> dict:
        """Remove a net: its label and all directly connected wires."""
        if tolerance is None:
            tolerance = self.grid

        result = {"label_removed": False, "hier_label_removed": False, "wires_removed": 0}

        label = self.find_label(name)
        if label:
            label_pos = (label.x, label.y)
            self.labels.remove(label)
            result["label_removed"] = True
            result["wires_removed"] += self.remove_wires_at(label_pos, tolerance)

        hl = self.find_hier_label(name)
        if hl:
            hl_pos = (hl.x, hl.y)
            self.hier_labels.remove(hl)
            result["hier_label_removed"] = True
            result["wires_removed"] += self.remove_wires_at(hl_pos, tolerance)

        if result["label_removed"] or result["hier_label_removed"]:
            _log_info(
                f"Removed net '{name}': label={result['label_removed']}, "
                f"hier_label={result['hier_label_removed']}, "
                f"wires={result['wires_removed']}"
            )

        return result

    def remove_junction(self, x: float, y: float, tolerance: float = None) -> bool:
        """Remove a junction at a specific position."""
        if tolerance is None:
            tolerance = self.grid

        for junc in self.junctions:
            if self._point_near((junc.x, junc.y), (x, y), tolerance):
                self.junctions.remove(junc)
                _log_info(f"Removed junction at ({junc.x}, {junc.y})")
                return True
        return False

    def remove_symbol(self, reference: str) -> bool:
        """Remove a symbol by reference designator."""
        sym = self.find_symbol(reference)
        if sym:
            self.symbols.remove(sym)
            _log_info(f"Removed symbol {reference}")
            return True
        return False

    # =========================================================================
    # Output Methods
    # =========================================================================

    def _build_lib_symbols_node(self) -> SExp:
        """Build lib_symbols section as SExp node."""
        lib_symbols = SExp.list("lib_symbols")

        added_lib_ids = set()

        # First, add any embedded lib_symbols from loaded schematics
        for sym_name, sym_node in self._embedded_lib_symbols.items():
            lib_symbols.append(sym_node)
            added_lib_ids.add(sym_name)

        # Then add any new symbol defs that weren't embedded
        for sym_def in self._symbol_defs.values():
            if sym_def.lib_id not in added_lib_ids:
                for sym_node in sym_def.to_sexp_nodes():
                    lib_symbols.append(sym_node)
                    added_lib_ids.add(sym_def.lib_id)

        return lib_symbols

    def _build_text_note_node(self, text: str, x: float, y: float) -> SExp:
        """Build a text note as SExp node."""
        return text_node(text, x, y, str(uuid.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build complete schematic as SExp tree."""
        root = SExp.list(
            "kicad_sch",
            SExp.list("version", 20250114),
            SExp.list("generator", "eeschema"),
            SExp.list("generator_version", "9.0"),
            uuid_node(self.sheet_uuid),
            SExp.list("paper", self.paper),
        )

        # Title block
        root.append(
            title_block(
                title=self.title,
                date=self.date,
                revision=self.revision,
                company=self.company,
                comment1=self.comment1,
                comment2=self.comment2,
            )
        )

        # Library symbols
        root.append(self._build_lib_symbols_node())

        # Symbol instances
        for sym in self.symbols:
            root.append(sym.to_sexp_node(self.project_name, self.sheet_path))

        # Power symbols
        for pwr in self.power_symbols:
            root.append(pwr.to_sexp_node(self.project_name, self.sheet_path))

        # Wires
        for wire in self.wires:
            root.append(wire.to_sexp_node())

        # Junctions
        for junc in self.junctions:
            root.append(junc.to_sexp_node())

        # Labels
        for label in self.labels:
            root.append(label.to_sexp_node())

        # Hierarchical labels
        for hl in self.hier_labels:
            root.append(hl.to_sexp_node())

        # Text notes
        for text, x, y in self.text_notes:
            root.append(self._build_text_note_node(text, x, y))

        # Sheet instances
        root.append(sheet_instances(self.sheet_path, self.page))

        return root

    def to_sexp(self) -> str:
        """Generate complete schematic S-expression string."""
        return self.to_sexp_node().to_string()

    def write(self, path: str | Path):
        """Write schematic to file."""
        path = Path(path)
        content = self.to_sexp()
        path.write_text(content)
        _log_info(
            f"Wrote schematic to {path} ({len(self.symbols)} symbols, {len(self.wires)} wires)"
        )

    # =========================================================================
    # Validation and Debugging
    # =========================================================================

    def validate(self, fix_auto: bool = False) -> list[dict]:
        """Validate the schematic and return a list of issues.

        Args:
            fix_auto: If True, automatically fix issues where possible

        Returns:
            List of issue dictionaries
        """
        issues = []

        # Check for duplicate references
        refs = {}
        for sym in self.symbols:
            if sym.reference in refs:
                issues.append(
                    {
                        "severity": "error",
                        "type": "duplicate_reference",
                        "message": f"Duplicate reference '{sym.reference}' at ({sym.x}, {sym.y})",
                        "location": (sym.x, sym.y),
                        "fix_applied": False,
                    }
                )
            refs[sym.reference] = sym

        # Check for off-grid symbols
        for sym in self.symbols:
            if not is_on_grid(sym.x, self.grid) or not is_on_grid(sym.y, self.grid):
                issue = {
                    "severity": "warning",
                    "type": "off_grid_symbol",
                    "message": f"Symbol {sym.reference} at ({sym.x}, {sym.y}) is off-grid",
                    "location": (sym.x, sym.y),
                    "fix_applied": False,
                }
                if fix_auto:
                    sym.x = snap_to_grid(sym.x, self.grid)
                    sym.y = snap_to_grid(sym.y, self.grid)
                    issue["fix_applied"] = True
                    issue["message"] += f" -> snapped to ({sym.x}, {sym.y})"
                issues.append(issue)

        # Check for off-grid wire endpoints
        for wire in self.wires:
            for coord, name in [((wire.x1, wire.y1), "start"), ((wire.x2, wire.y2), "end")]:
                if not is_on_grid(coord[0], self.grid) or not is_on_grid(coord[1], self.grid):
                    issues.append(
                        {
                            "severity": "warning",
                            "type": "off_grid_wire",
                            "message": f"Wire {name} at ({coord[0]}, {coord[1]}) is off-grid",
                            "location": coord,
                            "fix_applied": False,
                        }
                    )

        # Check wire connectivity
        connectivity_issues = self._check_wire_connectivity()
        issues.extend(connectivity_issues)

        # Check for power pins without connections
        power_pin_issues = self._check_power_pins()
        issues.extend(power_pin_issues)

        # Log validation summary
        errors = sum(1 for i in issues if i["severity"] == "error")
        warnings_count = sum(1 for i in issues if i["severity"] == "warning")
        if issues:
            _log_info(f"Validation found {errors} errors, {warnings_count} warnings")
            for issue in issues:
                if issue["severity"] == "error":
                    _log_warning(f"  {issue['type']}: {issue['message']}")
                else:
                    _log_debug(f"  {issue['type']}: {issue['message']}")
        else:
            _log_info("Validation passed with no issues")

        return issues

    def _check_wire_connectivity(self) -> list[dict]:
        """Check for floating wire endpoints not connected to anything."""
        issues = []

        # Collect all connection points
        connection_points = set()

        # Pin positions
        for sym in self.symbols:
            for pin in sym.symbol_def.pins:
                pos = sym.pin_position(pin.name if pin.name else pin.number)
                connection_points.add((round(pos[0], 2), round(pos[1], 2)))

        # Power symbol positions
        for pwr in self.power_symbols:
            connection_points.add((round(pwr.x, 2), round(pwr.y, 2)))

        # Junction positions
        for junc in self.junctions:
            connection_points.add((round(junc.x, 2), round(junc.y, 2)))

        # Label positions
        for label in self.labels:
            connection_points.add((round(label.x, 2), round(label.y, 2)))

        # Hierarchical label positions
        for hl in self.hier_labels:
            connection_points.add((round(hl.x, 2), round(hl.y, 2)))

        # Wire endpoints and T-junctions
        wire_endpoints = []
        wire_segments = []
        for wire in self.wires:
            p1 = (round(wire.x1, 2), round(wire.y1, 2))
            p2 = (round(wire.x2, 2), round(wire.y2, 2))
            wire_endpoints.append(p1)
            wire_endpoints.append(p2)
            wire_segments.append((p1, p2))

        # Check each wire endpoint
        endpoint_counts = {}
        for ep in wire_endpoints:
            endpoint_counts[ep] = endpoint_counts.get(ep, 0) + 1

        for endpoint, count in endpoint_counts.items():
            if endpoint in connection_points:
                continue

            if count >= 2:
                continue

            # Check if it lies on another wire segment (T-junction)
            on_wire = False
            for seg_start, seg_end in wire_segments:
                if endpoint == seg_start or endpoint == seg_end:
                    continue
                if self._point_on_segment(endpoint, seg_start, seg_end):
                    on_wire = True
                    issues.append(
                        {
                            "severity": "warning",
                            "type": "missing_junction",
                            "message": f"Wire endpoint at ({endpoint[0]}, {endpoint[1]}) forms T-junction without junction dot",
                            "location": endpoint,
                            "fix_applied": False,
                        }
                    )
                    break

            if not on_wire:
                issues.append(
                    {
                        "severity": "error",
                        "type": "floating_wire",
                        "message": f"Wire endpoint at ({endpoint[0]}, {endpoint[1]}) is not connected to anything",
                        "location": endpoint,
                        "fix_applied": False,
                    }
                )

        return issues

    def _point_on_segment(self, point: tuple, seg_start: tuple, seg_end: tuple) -> bool:
        """Check if a point lies on a line segment (for orthogonal wires)."""
        px, py = point
        x1, y1 = seg_start
        x2, y2 = seg_end

        if x1 == x2 == px:  # Vertical segment
            return min(y1, y2) < py < max(y1, y2)
        if y1 == y2 == py:  # Horizontal segment
            return min(x1, x2) < px < max(x1, x2)
        return False

    def _check_power_pins(self) -> list[dict]:
        """Check for power pins that might not be properly connected."""
        issues = []

        connected_points = set()
        for wire in self.wires:
            connected_points.add((round(wire.x1, 2), round(wire.y1, 2)))
            connected_points.add((round(wire.x2, 2), round(wire.y2, 2)))
        for junc in self.junctions:
            connected_points.add((round(junc.x, 2), round(junc.y, 2)))

        for sym in self.symbols:
            for pin in sym.symbol_def.pins:
                if pin.pin_type in ("power_in", "power_out"):
                    pos = sym.pin_position(pin.name if pin.name else pin.number)
                    pos_rounded = (round(pos[0], 2), round(pos[1], 2))

                    if pos_rounded not in connected_points:
                        issues.append(
                            {
                                "severity": "warning",
                                "type": "unconnected_power_pin",
                                "message": f"Power pin {pin.name or pin.number} on {sym.reference} at ({pos[0]}, {pos[1]}) may be unconnected",
                                "location": pos_rounded,
                                "fix_applied": False,
                            }
                        )

        return issues

    def get_statistics(self) -> dict:
        """Get schematic statistics useful for agents."""
        return {
            "symbol_count": len(self.symbols),
            "wire_count": len(self.wires),
            "junction_count": len(self.junctions),
            "label_count": len(self.labels),
            "hier_label_count": len(self.hier_labels),
            "power_symbol_count": len(self.power_symbols),
            "references": sorted([s.reference for s in self.symbols]),
            "power_nets": sorted(set(p.lib_id.split(":")[1] for p in self.power_symbols)),
            "net_labels": sorted(set(lbl.text for lbl in self.labels)),
        }

    def find_symbols_by_value(self, value: str) -> list[SymbolInstance]:
        """Find all symbols with a given value."""
        return [s for s in self.symbols if s.value == value]

    def find_symbols_by_lib(self, lib_pattern: str) -> list[SymbolInstance]:
        """Find all symbols from a library matching a pattern."""
        import fnmatch

        return [s for s in self.symbols if fnmatch.fnmatch(s.symbol_def.lib_id, lib_pattern)]
