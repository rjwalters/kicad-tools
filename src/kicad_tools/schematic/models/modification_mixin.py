"""
Schematic Modification Mixin

Provides methods for removing and modifying schematic elements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logging import _log_info
from .elements import Wire

if TYPE_CHECKING:
    pass


class SchematicModificationMixin:
    """Mixin providing modification operations for Schematic class."""

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
