"""
Schematic Elements Mixin

Provides basic element addition operations (symbols, wires, labels, etc.)
and grid snapping functionality.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from ..grid import is_on_grid, snap_to_grid
from ..logging import _log_debug, _log_info
from .elements import (
    HierarchicalLabel,
    Junction,
    Label,
    PowerSymbol,
    Wire,
)
from .symbol import SymbolDef, SymbolInstance

if TYPE_CHECKING:
    pass


class SchematicElementsMixin:
    """Mixin providing element addition and grid snapping for Schematic class."""

    def _snap_coord(self, value: float, context: str = "") -> float:
        """Apply grid snapping to a single coordinate based on snap_mode.

        Args:
            value: Coordinate value
            context: Context for warning messages

        Returns:
            Snapped or original value based on snap_mode
        """
        from .schematic import SnapMode

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
        auto_layout: bool = False,
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
            auto_layout: If True, automatically adjust position to avoid overlaps

        Returns:
            SymbolInstance with pin_position() method

        Example:
            # Add with auto-layout to avoid overlaps
            sym = sch.add_symbol(
                "Device:R", x=100, y=50, ref="R1",
                value="10k", auto_layout=True
            )
        """
        # Apply grid snapping if enabled
        if snap:
            x = self._snap_coord(x, f"symbol {ref}")
            y = self._snap_coord(y, f"symbol {ref}")

        # Apply auto-layout to find non-overlapping position
        if auto_layout:
            x, y = self.suggest_position(
                lib_id=lib_id,
                near=(x, y),
                avoid_overlaps=True,
                rotation=rotation,
            )

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
