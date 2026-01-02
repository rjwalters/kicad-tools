"""
Schematic Validation Mixin

Provides validation and statistics functionality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..grid import is_on_grid, snap_to_grid
from ..logging import _log_debug, _log_info, _log_warning

if TYPE_CHECKING:
    pass


class SchematicValidationMixin:
    """Mixin providing validation operations for Schematic class."""

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
                if endpoint in (seg_start, seg_end):
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
            "power_nets": sorted({p.lib_id.split(":")[1] for p in self.power_symbols}),
            "net_labels": sorted({lbl.text for lbl in self.labels}),
        }
