"""Edge clearance DRC rules.

This module implements board edge validation rules that check minimum
copper-to-edge and hole-to-edge clearances.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class EdgeClearanceRule(DRCRule):
    """Check copper and hole clearances to board edge.

    Validates that all copper elements (traces, pads, zones) and holes (vias)
    maintain minimum clearance from the board edge as specified by
    manufacturer design rules.

    Rule IDs generated:
        - edge_clearance_trace: Trace too close to board edge
        - edge_clearance_pad: Pad too close to board edge
        - edge_clearance_via: Via too close to board edge
        - edge_clearance_zone: Zone copper too close to board edge
    """

    rule_id = "edge_clearance"
    name = "Edge Clearance"
    description = "Check copper-to-edge and hole-to-edge clearances"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all edge clearance rules.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing edge clearance violations
        """
        results = DRCResults()

        # Get board outline segments for distance calculations
        outline_segments = pcb.get_board_outline_segments()
        if not outline_segments:
            # No board outline defined, can't check edge clearances
            return results

        # Check each type of copper element
        self._check_segments(pcb, outline_segments, design_rules, results)
        self._check_vias(pcb, outline_segments, design_rules, results)
        self._check_pads(pcb, outline_segments, design_rules, results)
        self._check_zones(pcb, outline_segments, design_rules, results)

        return results

    def _check_segments(
        self,
        pcb: PCB,
        outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check trace segment clearances to board edge."""
        min_clearance = design_rules.min_copper_to_edge_mm

        for segment in pcb.segments:
            # Check both endpoints and consider trace width
            half_width = segment.width / 2

            for point in [segment.start, segment.end]:
                distance = self._min_distance_to_outline(point, outline_segments)
                # Actual clearance from trace edge (not centerline)
                actual_clearance = distance - half_width

                if actual_clearance < min_clearance:
                    results.add(
                        DRCViolation(
                            rule_id="edge_clearance_trace",
                            severity="error",
                            message=(
                                f"Trace to board edge {actual_clearance:.3f}mm "
                                f"< minimum {min_clearance:.2f}mm"
                            ),
                            location=point,
                            layer=segment.layer,
                            actual_value=actual_clearance,
                            required_value=min_clearance,
                            items=(f"Net {segment.net_number}",),
                        )
                    )

        results.rules_checked += 1

    def _check_vias(
        self,
        pcb: PCB,
        outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check via clearances to board edge.

        Vias use min_hole_to_edge_mm which is typically stricter than copper clearance.
        """
        min_clearance = design_rules.min_hole_to_edge_mm

        for via in pcb.vias:
            distance = self._min_distance_to_outline(via.position, outline_segments)
            # Via edge is at position - size/2
            half_size = via.size / 2
            actual_clearance = distance - half_size

            if actual_clearance < min_clearance:
                results.add(
                    DRCViolation(
                        rule_id="edge_clearance_via",
                        severity="error",
                        message=(
                            f"Via to board edge {actual_clearance:.3f}mm "
                            f"< minimum {min_clearance:.2f}mm"
                        ),
                        location=via.position,
                        layer=via.layers[0] if via.layers else None,
                        actual_value=actual_clearance,
                        required_value=min_clearance,
                        items=(f"Net {via.net_number}",),
                    )
                )

        results.rules_checked += 1

    def _check_pads(
        self,
        pcb: PCB,
        outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check pad clearances to board edge.

        Through-hole pads use min_hole_to_edge_mm.
        SMD pads use min_copper_to_edge_mm.
        """
        min_copper_clearance = design_rules.min_copper_to_edge_mm
        min_hole_clearance = design_rules.min_hole_to_edge_mm

        for footprint in pcb.footprints:
            fp_x, fp_y = footprint.position
            fp_rotation = math.radians(footprint.rotation)
            cos_rot = math.cos(fp_rotation)
            sin_rot = math.sin(fp_rotation)

            for pad in footprint.pads:
                # Transform pad position to board coordinates
                pad_local_x, pad_local_y = pad.position
                pad_x = fp_x + (pad_local_x * cos_rot - pad_local_y * sin_rot)
                pad_y = fp_y + (pad_local_x * sin_rot + pad_local_y * cos_rot)
                pad_pos = (pad_x, pad_y)

                distance = self._min_distance_to_outline(pad_pos, outline_segments)

                # Calculate pad edge offset (use larger dimension for safety)
                half_size = max(pad.size[0], pad.size[1]) / 2
                actual_clearance = distance - half_size

                # Select clearance rule based on pad type
                if pad.type == "thru_hole":
                    min_clearance = min_hole_clearance
                    rule_id = "edge_clearance_pad_hole"
                else:
                    min_clearance = min_copper_clearance
                    rule_id = "edge_clearance_pad"

                if actual_clearance < min_clearance:
                    results.add(
                        DRCViolation(
                            rule_id=rule_id,
                            severity="error",
                            message=(
                                f"Pad {pad.number} to board edge {actual_clearance:.3f}mm "
                                f"< minimum {min_clearance:.2f}mm"
                            ),
                            location=pad_pos,
                            layer=footprint.layer,
                            actual_value=actual_clearance,
                            required_value=min_clearance,
                            items=(footprint.reference, f"Pad {pad.number}"),
                        )
                    )

        results.rules_checked += 1

    def _check_zones(
        self,
        pcb: PCB,
        outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check zone copper clearances to board edge.

        Checks the filled polygon vertices of each zone.
        """
        min_clearance = design_rules.min_copper_to_edge_mm

        for zone in pcb.zones:
            # Check filled polygons (actual copper) rather than boundary
            polygons_to_check = zone.filled_polygons if zone.filled_polygons else [zone.polygon]

            for polygon in polygons_to_check:
                for point in polygon:
                    distance = self._min_distance_to_outline(point, outline_segments)

                    if distance < min_clearance:
                        results.add(
                            DRCViolation(
                                rule_id="edge_clearance_zone",
                                severity="error",
                                message=(
                                    f"Zone copper to board edge {distance:.3f}mm "
                                    f"< minimum {min_clearance:.2f}mm"
                                ),
                                location=point,
                                layer=zone.layer,
                                actual_value=distance,
                                required_value=min_clearance,
                                items=(zone.net_name or f"Net {zone.net_number}",),
                            )
                        )

        results.rules_checked += 1

    def _min_distance_to_outline(
        self,
        point: tuple[float, float],
        outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> float:
        """Calculate minimum distance from a point to the board outline.

        Args:
            point: (x, y) coordinate in mm
            outline_segments: List of line segments forming the board outline

        Returns:
            Minimum distance in mm from point to any outline segment
        """
        min_dist = float("inf")

        for seg_start, seg_end in outline_segments:
            dist = self._point_to_segment_distance(point, seg_start, seg_end)
            min_dist = min(min_dist, dist)

        return min_dist

    @staticmethod
    def _point_to_segment_distance(
        point: tuple[float, float],
        seg_start: tuple[float, float],
        seg_end: tuple[float, float],
    ) -> float:
        """Calculate distance from a point to a line segment.

        Args:
            point: (x, y) coordinate
            seg_start: Start of line segment (x, y)
            seg_end: End of line segment (x, y)

        Returns:
            Distance from point to the closest point on the segment
        """
        px, py = point
        x1, y1 = seg_start
        x2, y2 = seg_end

        # Vector from seg_start to seg_end
        dx = x2 - x1
        dy = y2 - y1

        # Length squared of segment
        length_sq = dx * dx + dy * dy

        if length_sq == 0:
            # Segment is a point
            return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)

        # Project point onto segment line, clamped to [0, 1]
        t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / length_sq))

        # Find closest point on segment
        closest_x = x1 + t * dx
        closest_y = y1 + t * dy

        # Return distance to closest point
        return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)
