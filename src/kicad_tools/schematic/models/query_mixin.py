"""
Schematic Query Mixin

Provides methods for finding and querying schematic elements.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from .elements import HierarchicalLabel, Label, Wire
from .symbol import SymbolInstance

if TYPE_CHECKING:
    pass


class SchematicQueryMixin:
    """Mixin providing query operations for Schematic class."""

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

    def find_label(self, name: str) -> Label | None:
        """Find a label by exact name."""
        for label in self.labels:
            if label.text == name:
                return label
        return None

    def find_labels(self, pattern: str = None) -> list[Label]:
        """Find labels matching a pattern."""
        if pattern is None:
            return list(self.labels)
        return [lbl for lbl in self.labels if fnmatch.fnmatch(lbl.text, pattern)]

    def find_hier_label(self, name: str) -> HierarchicalLabel | None:
        """Find a hierarchical label by exact name."""
        for hl in self.hier_labels:
            if hl.text == name:
                return hl
        return None

    def find_hier_labels(self, pattern: str = None) -> list[HierarchicalLabel]:
        """Find hierarchical labels matching a pattern."""
        if pattern is None:
            return list(self.hier_labels)
        return [hl for hl in self.hier_labels if fnmatch.fnmatch(hl.text, pattern)]

    def find_symbol(self, reference: str) -> SymbolInstance | None:
        """Find a symbol by reference designator."""
        for sym in self.symbols:
            if sym.reference == reference:
                return sym
        return None

    def find_symbols(self, pattern: str = None) -> list[SymbolInstance]:
        """Find symbols matching a pattern."""
        if pattern is None:
            return list(self.symbols)
        return [s for s in self.symbols if fnmatch.fnmatch(s.reference, pattern)]

    def find_symbols_by_value(self, value: str) -> list[SymbolInstance]:
        """Find all symbols with a given value."""
        return [s for s in self.symbols if s.value == value]

    def find_symbols_by_lib(self, lib_pattern: str) -> list[SymbolInstance]:
        """Find all symbols from a library matching a pattern."""
        return [s for s in self.symbols if fnmatch.fnmatch(s.symbol_def.lib_id, lib_pattern)]
