"""Edge clearance DRC rules.

This module implements board edge validation rules that check minimum
copper-to-edge and hole-to-edge clearances.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.core.geometry import point_to_segment_distance as _core_pt_seg_dist

from ..clearance_shapes import ALL_LAYERS, min_gap_to_outline, outline_shapes
from ..clearance_shapes import make_pad as _kernel_make_pad
from ..clearance_shapes import pad_center as _kernel_pad_center
from ..clearance_shapes import segment_shape as _kernel_segment_shape
from ..clearance_shapes import via_shape as _kernel_via_shape
from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad


# Floating-point tolerance for edge clearance comparisons (0.1 micron).
# Zone boundaries inset by exactly ``edge_clearance`` via Shapely ``buffer``
# can land at distances like 0.29999... instead of 0.300 due to IEEE-754
# rounding.  A 0.1-micron epsilon is well below manufacturing precision and
# prevents false-positive DRC violations at exact boundaries.
_CLEARANCE_EPSILON_MM = 1e-4


def _pad_kernel_shape(pad: Pad, footprint: Footprint):
    """This pad's exact kernel shape, for the board-edge gap.

    Mirrors :func:`kicad_tools.validate.clearance_shapes.pad_shape`, the
    canonical board-file pad translation, but reads ``shape`` /
    ``roundrect_rratio`` / ``rotation`` / ``drill`` through ``getattr`` with
    the same defaults a plain rectangle pad would have.  A real
    :class:`~kicad_tools.schema.pcb.Pad` always carries every one of these
    fields, so this is a no-op there; several of this rule's own regression
    tests drive it with a bare ``SimpleNamespace`` carrying only
    ``position``/``size``, and this is what keeps those fixtures valid
    without requiring them to restate KiCad's own defaults.
    """
    cx, cy = _kernel_pad_center(pad, footprint)
    return _kernel_make_pad(
        getattr(pad, "shape", "rect"),
        pad.size[0],
        pad.size[1],
        getattr(pad, "roundrect_rratio", 0.25),
        getattr(pad, "rotation", 0.0),
        cx,
        cy,
        ALL_LAYERS,
        getattr(pad, "drill", 0.0),
    )


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

        # Get board outline segments for distance calculations.
        # These are now in board-relative coordinates.
        outline_segments = pcb.get_board_outline_segments()
        if not outline_segments:
            # No board outline defined, can't check edge clearances
            return results

        # Board origin needed to convert sheet-absolute element coordinates
        # (segments, vias, zones) into board-relative space so they match
        # the outline coordinate frame.
        origin = pcb.board_origin

        # Check each type of copper element
        self._check_segments(pcb, outline_segments, design_rules, results, origin)
        self._check_vias(pcb, outline_segments, design_rules, results, origin)
        self._check_pads(pcb, outline_segments, design_rules, results)
        self._check_zones(pcb, outline_segments, design_rules, results, origin)

        return results

    def _check_segments(
        self,
        pcb: PCB,
        outline_segments: list[tuple[tuple[float, float], tuple[float, float]]],
        design_rules: DesignRules,
        results: DRCResults,
        origin: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """Check trace segment clearances to board edge.

        Epic #5509 Phase 4d: the gap is the shared clearance kernel's exact
        segment-to-polyline distance (:func:`~kicad_tools.validate.clearance_shapes.min_gap_to_outline`
        on the *whole* segment), not the old "check both endpoints against the
        outline, ignore the interior" reading.  That mattered whenever an
        outline *vertex* pokes closest to a point in the middle of a trace --
        invisible to an endpoints-only scan but not to kicad-cli.

        ``segment.start`` / ``segment.end`` are board-relative after
        :meth:`PCB.load` (see ``_detect_board_origin`` docstring), so we
        compare them directly against the board-relative outline.  The
        ``origin`` parameter is retained for API stability but is no
        longer used to translate segment endpoints.
        """
        min_clearance = design_rules.min_copper_to_edge_mm
        del origin  # board-relative invariant: no per-call translation needed

        edges = outline_shapes(outline_segments)

        for segment in pcb.segments:
            gap = min_gap_to_outline(_kernel_segment_shape(segment), edges)

            if gap < min_clearance - _CLEARANCE_EPSILON_MM:
                # Report at whichever endpoint sits closer to the outline --
                # the same witness the old endpoints-only scan would have
                # picked, so a finding still points near the trouble spot
                # even though the *verdict* now comes from the full segment.
                dist_start = self._min_distance_to_outline(segment.start, outline_segments)
                dist_end = self._min_distance_to_outline(segment.end, outline_segments)
                point = segment.start if dist_start <= dist_end else segment.end
                results.add(
                    DRCViolation(
                        rule_id="edge_clearance_trace",
                        severity="error",
                        message=(
                            f"Trace to board edge {gap:.3f}mm < minimum {min_clearance:.2f}mm"
                        ),
                        location=point,
                        layer=segment.layer,
                        actual_value=gap,
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
        origin: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """Check via clearances to board edge.

        Vias use min_hole_to_edge_mm which is typically stricter than copper clearance.

        Epic #5509 Phase 4d: the gap is the shared clearance kernel's
        ``copper_gap`` for a via's circular copper against the outline --
        identical arithmetic to the retired ``distance - via.size / 2``
        reading (a via is already exactly a disc, so there was no
        approximation here to fix), kept on the kernel for the dedup.

        ``via.position`` is board-relative after :meth:`PCB.load`; the
        ``origin`` parameter is retained for API stability.
        """
        min_clearance = design_rules.min_hole_to_edge_mm
        del origin  # board-relative invariant: no per-call translation needed

        edges = outline_shapes(outline_segments)

        for via in pcb.vias:
            gap = min_gap_to_outline(_kernel_via_shape(via), edges)

            if gap < min_clearance - _CLEARANCE_EPSILON_MM:
                results.add(
                    DRCViolation(
                        rule_id="edge_clearance_via",
                        severity="error",
                        message=(f"Via to board edge {gap:.3f}mm < minimum {min_clearance:.2f}mm"),
                        location=via.position,
                        layer=via.layers[0] if via.layers else None,
                        actual_value=gap,
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

        Epic #5509 Phase 4d: the gap is the shared clearance kernel's exact
        rotated pad polygon (:func:`_pad_kernel_shape`) against the outline,
        in place of the retired "circle of radius ``max(w, h) / 2`` centred
        on the pad" reading.  That circle over-reported copper at every
        corner of a non-square or rotated pad -- an over-rejection, the same
        ``roundrect-corner-gap`` failure mode fixed for pad-vs-pad pairs in
        :mod:`kicad_tools.validate.rules.clearance`.  A through-hole pad's
        *hole* is not modelled here, matching the pre-existing convention:
        this rule has always measured the pad's copper envelope for both
        the SMD and through-hole branches, only switching which rule
        threshold (``min_copper_to_edge_mm`` vs. ``min_hole_to_edge_mm``)
        applies to it.
        """
        min_copper_clearance = design_rules.min_copper_to_edge_mm
        min_hole_clearance = design_rules.min_hole_to_edge_mm

        edges = outline_shapes(outline_segments)

        for footprint in pcb.footprints:
            fp_x, fp_y = footprint.position
            # KiCad applies the footprint orientation as a NEGATED angle relative
            # to standard CCW math (verified vs pcbnew, issue #3739). Use -rotation
            # so the 2D rotation matrix below matches KiCad's pad world positions.
            fp_rotation = math.radians(-footprint.rotation)
            cos_rot = math.cos(fp_rotation)
            sin_rot = math.sin(fp_rotation)

            for pad in footprint.pads:
                # Transform pad position to board coordinates -- kept
                # alongside _pad_kernel_shape's own (equivalent) pad-centre
                # computation so the violation's reported location is
                # unchanged by this switch.
                pad_local_x, pad_local_y = pad.position
                pad_x = fp_x + (pad_local_x * cos_rot - pad_local_y * sin_rot)
                pad_y = fp_y + (pad_local_x * sin_rot + pad_local_y * cos_rot)
                pad_pos = (pad_x, pad_y)

                gap = min_gap_to_outline(_pad_kernel_shape(pad, footprint), edges)

                # Select clearance rule based on pad type
                if pad.type == "thru_hole":
                    min_clearance = min_hole_clearance
                    rule_id = "edge_clearance_pad_hole"
                else:
                    min_clearance = min_copper_clearance
                    rule_id = "edge_clearance_pad"

                if gap < min_clearance - _CLEARANCE_EPSILON_MM:
                    results.add(
                        DRCViolation(
                            rule_id=rule_id,
                            severity="error",
                            message=(
                                f"Pad {pad.number} to board edge {gap:.3f}mm "
                                f"< minimum {min_clearance:.2f}mm"
                            ),
                            location=pad_pos,
                            layer=footprint.layer,
                            actual_value=gap,
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
        origin: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """Check zone copper clearances to board edge.

        Checks the filled polygon vertices of each zone.

        Zone polygon vertices are board-relative after :meth:`PCB.load`;
        the ``origin`` parameter is retained for API stability.
        """
        min_clearance = design_rules.min_copper_to_edge_mm
        del origin  # board-relative invariant: no per-call translation needed

        for zone in pcb.zones:
            # Rule areas prohibit copper; their boundary is not copper, even
            # when it extends to (or beyond) the board edge.
            if getattr(zone, "keepout", None) is not None:
                continue
            # Check filled polygons (actual copper) rather than boundary
            polygons_to_check = zone.filled_polygons if zone.filled_polygons else [zone.polygon]

            for polygon in polygons_to_check:
                for point in polygon:
                    # Zone polygon vertices are already in board-relative space.
                    board_point = point
                    distance = self._min_distance_to_outline(board_point, outline_segments)

                    if distance < min_clearance - _CLEARANCE_EPSILON_MM:
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

        Thin wrapper around :func:`kicad_tools.core.geometry.point_to_segment_distance`
        that accepts tuple arguments for API compatibility.

        Args:
            point: (x, y) coordinate
            seg_start: Start of line segment (x, y)
            seg_end: End of line segment (x, y)

        Returns:
            Distance from point to the closest point on the segment
        """
        return _core_pt_seg_dist(
            point[0], point[1], seg_start[0], seg_start[1], seg_end[0], seg_end[1]
        )
