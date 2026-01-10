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

        # Check for unconnected pins (all types, not just power)
        unconnected_pin_issues = self._check_unconnected_pins()
        issues.extend(unconnected_pin_issues)

        # Check for disconnected labels (labels not on wires)
        disconnected_label_issues = self._check_disconnected_labels()
        issues.extend(disconnected_label_issues)

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

        # Pin positions - use pin.number for unique identification
        # (many symbols like Device:C, Device:R have pins all named "~")
        for sym in self.symbols:
            for pin in sym.symbol_def.pins:
                pos = sym.pin_position(pin.number)
                connection_points.add((round(pos[0], 2), round(pos[1], 2)))

        # Power symbol positions
        for pwr in self.power_symbols:
            connection_points.add((round(pwr.x, 2), round(pwr.y, 2)))

        # Junction positions
        for junc in self.junctions:
            connection_points.add((round(junc.x, 2), round(junc.y, 2)))

        # Label positions (local labels)
        for label in self.labels:
            connection_points.add((round(label.x, 2), round(label.y, 2)))

        # Global label positions
        for gl in self.global_labels:
            connection_points.add((round(gl.x, 2), round(gl.y, 2)))

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

    def _check_unconnected_pins(self) -> list[dict]:
        """Check for unconnected pins on all symbols.

        This checks ALL pin types (not just power pins) for proper connections.
        Pins are considered connected if they touch:
        - A wire endpoint
        - A junction
        - A power symbol
        - A no_connect marker

        Passive pins on simple 2-pin components (like resistors, capacitors)
        are skipped as they commonly have one end floating during design.
        """
        issues = []

        # Build set of all valid connection points
        connected_points = set()

        # Wire endpoints
        for wire in self.wires:
            connected_points.add((round(wire.x1, 2), round(wire.y1, 2)))
            connected_points.add((round(wire.x2, 2), round(wire.y2, 2)))

        # Junctions
        for junc in self.junctions:
            connected_points.add((round(junc.x, 2), round(junc.y, 2)))

        # Power symbols (they connect at their position)
        for pwr in self.power_symbols:
            connected_points.add((round(pwr.x, 2), round(pwr.y, 2)))

        # No-connect markers (pins with these are intentionally unconnected)
        no_connect_points = set()
        for nc in self.no_connects:
            no_connect_points.add((round(nc.x, 2), round(nc.y, 2)))

        # Collect wire segments for T-junction detection
        wire_segments = []
        for wire in self.wires:
            p1 = (round(wire.x1, 2), round(wire.y1, 2))
            p2 = (round(wire.x2, 2), round(wire.y2, 2))
            wire_segments.append((p1, p2))

        for sym in self.symbols:
            # Skip simple 2-pin passive components (resistors, capacitors, etc.)
            # These often have one pin floating during design
            is_simple_passive = len(sym.symbol_def.pins) == 2 and all(
                p.pin_type == "passive" for p in sym.symbol_def.pins
            )

            for pin in sym.symbol_def.pins:
                # Skip passive pins on simple 2-pin devices
                if is_simple_passive and pin.pin_type == "passive":
                    continue

                pos = sym.pin_position(pin.number)
                pos_rounded = (round(pos[0], 2), round(pos[1], 2))

                # Skip if marked with no_connect
                if pos_rounded in no_connect_points:
                    continue

                # Check if connected to a wire endpoint or junction
                if pos_rounded in connected_points:
                    continue

                # Check if on a wire segment (T-junction without explicit junction)
                on_wire = False
                for seg_start, seg_end in wire_segments:
                    if self._point_on_segment(pos_rounded, seg_start, seg_end):
                        on_wire = True
                        break

                if on_wire:
                    continue

                # Pin is unconnected
                display_name = pin.name if pin.name and pin.name != "~" else pin.number

                # Determine severity based on pin type
                if pin.pin_type in ("power_in", "power_out"):
                    severity = "error"
                    issue_type = "unconnected_power_pin"
                elif pin.pin_type in ("input", "output", "bidirectional"):
                    severity = "error"
                    issue_type = "unconnected_pin"
                else:
                    severity = "warning"
                    issue_type = "unconnected_pin"

                issues.append(
                    {
                        "severity": severity,
                        "type": issue_type,
                        "message": f"Pin {display_name} ({pin.pin_type}) on {sym.reference} at ({pos[0]}, {pos[1]}) is not connected",
                        "location": pos_rounded,
                        "fix_applied": False,
                    }
                )

        return issues

    def _check_disconnected_labels(self) -> list[dict]:
        """Check for labels that are not connected to any wire.

        Labels must be placed at wire endpoints or on wire segments to be valid.
        A label floating in space (not touching a wire) is an error.
        """
        issues = []

        # Collect wire endpoints
        wire_endpoints = set()
        for wire in self.wires:
            wire_endpoints.add((round(wire.x1, 2), round(wire.y1, 2)))
            wire_endpoints.add((round(wire.x2, 2), round(wire.y2, 2)))

        # Collect wire segments for checking if label is on a wire
        wire_segments = []
        for wire in self.wires:
            p1 = (round(wire.x1, 2), round(wire.y1, 2))
            p2 = (round(wire.x2, 2), round(wire.y2, 2))
            wire_segments.append((p1, p2))

        # Check local labels
        for label in self.labels:
            pos = (round(label.x, 2), round(label.y, 2))
            if not self._is_on_wire_network(pos, wire_endpoints, wire_segments):
                issues.append(
                    {
                        "severity": "error",
                        "type": "disconnected_label",
                        "message": f"Label '{label.text}' at ({label.x}, {label.y}) is not connected to any wire",
                        "location": pos,
                        "fix_applied": False,
                    }
                )

        # Check global labels
        for gl in self.global_labels:
            pos = (round(gl.x, 2), round(gl.y, 2))
            if not self._is_on_wire_network(pos, wire_endpoints, wire_segments):
                issues.append(
                    {
                        "severity": "error",
                        "type": "disconnected_label",
                        "message": f"Global label '{gl.text}' at ({gl.x}, {gl.y}) is not connected to any wire",
                        "location": pos,
                        "fix_applied": False,
                    }
                )

        # Check hierarchical labels
        for hl in self.hier_labels:
            pos = (round(hl.x, 2), round(hl.y, 2))
            if not self._is_on_wire_network(pos, wire_endpoints, wire_segments):
                issues.append(
                    {
                        "severity": "error",
                        "type": "disconnected_label",
                        "message": f"Hierarchical label '{hl.text}' at ({hl.x}, {hl.y}) is not connected to any wire",
                        "location": pos,
                        "fix_applied": False,
                    }
                )

        return issues

    def _is_on_wire_network(
        self,
        point: tuple,
        wire_endpoints: set,
        wire_segments: list,
    ) -> bool:
        """Check if a point is on the wire network (endpoint or on segment)."""
        # Check if at a wire endpoint
        if point in wire_endpoints:
            return True

        # Check if on a wire segment
        for seg_start, seg_end in wire_segments:
            if self._point_on_segment(point, seg_start, seg_end):
                return True

        return False

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


def summarize_issues_by_type(issues: list[dict]) -> dict[str, list[dict]]:
    """Group validation issues by their type.

    Args:
        issues: List of issue dictionaries from validate()

    Returns:
        Dictionary mapping issue type to list of issues of that type.
        Example: {"off_grid_symbol": [...], "missing_junction": [...]}
    """
    by_type: dict[str, list[dict]] = {}
    for issue in issues:
        issue_type = issue.get("type", "unknown")
        if issue_type not in by_type:
            by_type[issue_type] = []
        by_type[issue_type].append(issue)
    return by_type


def format_validation_summary(
    issues: list[dict],
    verbose: bool = False,
    max_per_type: int = 3,
) -> str:
    """Format validation issues as a human-readable summary.

    Args:
        issues: List of issue dictionaries from validate()
        verbose: If True, show individual issue details
        max_per_type: Maximum issues to show per type when verbose

    Returns:
        Formatted string summarizing the validation results
    """
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    lines = []
    lines.append(f"Errors: {len(errors)}")
    lines.append(f"Warnings: {len(warnings)}")

    # Summarize warnings by type
    if warnings:
        warnings_by_type = summarize_issues_by_type(warnings)
        lines.append("")
        lines.append("Warning summary:")
        for issue_type, type_issues in sorted(warnings_by_type.items()):
            lines.append(f"  - {len(type_issues)} {issue_type}")
            if verbose:
                for issue in type_issues[:max_per_type]:
                    lines.append(f"      {issue.get('message', str(issue))}")
                if len(type_issues) > max_per_type:
                    lines.append(f"      ... and {len(type_issues) - max_per_type} more")

    # Show errors (always show details)
    if errors:
        lines.append("")
        lines.append("Errors:")
        for err in errors[:10]:
            lines.append(f"  ERROR: {err.get('message', str(err))}")
        if len(errors) > 10:
            lines.append(f"  ... and {len(errors) - 10} more errors")

    if not verbose and warnings:
        lines.append("")
        lines.append("Use --verbose or -v to see warning details.")

    return "\n".join(lines)
