"""
Schematic Layout Mixin

Provides auto-layout and overlap detection functionality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logging import _log_info, _log_warning
from .symbol import SymbolDef, SymbolInstance

if TYPE_CHECKING:
    pass


class SchematicLayoutMixin:
    """Mixin providing auto-layout operations for Schematic class."""

    def find_overlapping_symbols(
        self, padding: float = 2.54
    ) -> list[tuple[SymbolInstance, SymbolInstance]]:
        """Find all pairs of overlapping symbols in the schematic.

        Uses bounding box collision detection to identify symbols that
        overlap or are too close together.

        Args:
            padding: Minimum spacing between symbols in mm (default: 2.54mm)

        Returns:
            List of (symbol1, symbol2) tuples where the symbols overlap.
            Each pair is returned only once (no duplicates like (A,B) and (B,A)).

        Example:
            >>> overlaps = sch.find_overlapping_symbols()
            >>> for sym1, sym2 in overlaps:
            ...     print(f"{sym1.reference} overlaps {sym2.reference}")
        """
        overlaps = []
        n = len(self.symbols)

        for i in range(n):
            for j in range(i + 1, n):
                if self.symbols[i].overlaps(self.symbols[j], padding):
                    overlaps.append((self.symbols[i], self.symbols[j]))

        return overlaps

    def suggest_position(
        self,
        lib_id: str,
        near: tuple[float, float],
        avoid_overlaps: bool = True,
        padding: float = 2.54,
        search_radius: float = 50.8,
        rotation: float = 0,
    ) -> tuple[float, float]:
        """Suggest a non-overlapping position for a new symbol.

        Searches in a spiral pattern around the preferred location to find
        a position that doesn't overlap with existing symbols.

        Args:
            lib_id: Library:Symbol format (e.g., "Device:R")
            near: Preferred (x, y) position
            avoid_overlaps: Whether to check for overlaps (default: True)
            padding: Minimum spacing between symbols in mm (default: 2.54mm)
            search_radius: Maximum distance to search from preferred position
            rotation: Symbol rotation in degrees

        Returns:
            (x, y) tuple of suggested position, snapped to grid

        Example:
            >>> pos = sch.suggest_position("Device:R", near=(100, 50))
            >>> print(f"Suggested position: {pos}")
        """
        # Snap the preferred position to grid
        x = self._snap_coord(near[0], "suggest_position")
        y = self._snap_coord(near[1], "suggest_position")

        if not avoid_overlaps or not self.symbols:
            return (x, y)

        # Load symbol definition to get bounding box
        if lib_id not in self._symbol_defs:
            self._symbol_defs[lib_id] = SymbolDef.from_library(lib_id)

        sym_def = self._symbol_defs[lib_id]

        # Create a temporary symbol to check for overlaps
        temp_symbol = SymbolInstance(
            symbol_def=sym_def,
            x=x,
            y=y,
            rotation=rotation,
            reference="_TEMP_",
            value="",
        )

        # Check if the preferred position is clear
        if not self._position_overlaps(temp_symbol, padding):
            return (x, y)

        # Search in a spiral pattern for a clear position
        # Start with grid-sized steps, expand outward
        step = self.grid
        max_steps = int(search_radius / step)

        for distance in range(1, max_steps + 1):
            # Check positions at this distance in a square pattern
            for dx in range(-distance, distance + 1):
                for dy in range(-distance, distance + 1):
                    # Only check the perimeter of the square
                    if abs(dx) != distance and abs(dy) != distance:
                        continue

                    test_x = self._snap_coord(x + dx * step, "search")
                    test_y = self._snap_coord(y + dy * step, "search")

                    temp_symbol.x = test_x
                    temp_symbol.y = test_y

                    if not self._position_overlaps(temp_symbol, padding):
                        _log_info(f"Auto-layout: moved from ({x}, {y}) to ({test_x}, {test_y})")
                        return (test_x, test_y)

        # If no clear position found within radius, return original with warning
        _log_warning(f"Auto-layout: no clear position found within {search_radius}mm of ({x}, {y})")
        return (x, y)

    def _position_overlaps(self, symbol: SymbolInstance, padding: float) -> bool:
        """Check if a symbol position overlaps with any existing symbols."""
        return any(symbol.overlaps(existing, padding) for existing in self.symbols)
