"""Footprint placement DRC rules.

This module implements board placement validation rules that detect
footprints placed outside the board outline polygon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.core.geometry import point_in_polygon as _core_point_in_polygon

from ..violations import DRCResults, DRCViolation
from .base import DRCRule
from .edge import EdgeClearanceRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Test if a point is inside a polygon; the boundary counts as inside.

    Thin wrapper over :func:`kicad_tools.core.geometry.point_in_polygon`
    with ``boundary_inside=True`` (a footprint origin exactly on any board
    edge or vertex is acceptable; issue #7224).
    """
    return _core_point_in_polygon(x, y, polygon, boundary_inside=True)


class FootprintOutsideBoardRule(DRCRule):
    """Detect footprints whose centroid falls outside the board outline.

    For each footprint the rule tests whether its ``(at X Y)`` position
    lies inside the Edge.Cuts polygon.  Footprints outside the board are
    reported as errors together with the minimum distance from the
    footprint centroid to the nearest board edge segment.

    Rule IDs generated:
        - footprint_outside_board: Footprint centroid is outside the outline.
    """

    rule_id = "footprint_outside_board"
    name = "Footprint Placement"
    description = "Check that footprints are placed inside the board outline"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all footprints against the board outline polygon.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules (unused but required by interface).

        Returns:
            DRCResults containing placement violations.
        """
        results = DRCResults()

        outline_polygon = pcb.get_board_outline()
        if not outline_polygon or len(outline_polygon) < 3:
            # No usable board outline -- nothing to check.
            results.rules_checked += 1
            return results

        outline_segments = pcb.get_board_outline_segments()

        for footprint in pcb.footprints:
            fx, fy = footprint.position
            if not point_in_polygon(fx, fy, outline_polygon):
                # Compute distance to nearest edge for the message.
                distance = EdgeClearanceRule()._min_distance_to_outline((fx, fy), outline_segments)
                results.add(
                    DRCViolation(
                        rule_id="footprint_outside_board",
                        severity="error",
                        message=(
                            f"Footprint {footprint.reference} at "
                            f"({fx:.2f}, {fy:.2f}) is outside the board "
                            f"outline by {distance:.2f}mm"
                        ),
                        location=(fx, fy),
                        layer=footprint.layer,
                        actual_value=distance,
                        required_value=0.0,
                        items=(footprint.reference,),
                    )
                )

        results.rules_checked += 1
        return results
