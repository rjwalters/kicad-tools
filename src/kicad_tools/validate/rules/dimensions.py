"""Dimension DRC rules for trace width, via drill, and annular ring.

This module implements dimension validation rules that check minimum sizes
for traces, vias, and annular rings against manufacturer design rules.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class DimensionRules(DRCRule):
    """Check dimension rules for traces and vias.

    Validates:
    - Minimum trace width
    - Minimum via drill diameter
    - Minimum via outer diameter
    - Minimum annular ring (via outer - via drill) / 2
    - Drill-to-drill clearance (hole edge to hole edge)
    """

    rule_id = "dimensions"
    name = "Dimension Rules"
    description = "Check trace width, via drill, and annular ring dimensions"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all dimension rules.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing dimension violations
        """
        results = DRCResults()

        # Check trace widths
        self._check_trace_widths(pcb, design_rules, results)

        # Check via dimensions
        self._check_via_dimensions(pcb, design_rules, results)

        # Check drill-to-drill clearance
        self._check_drill_clearance(pcb, design_rules, results)

        # 5 rule categories checked
        results.rules_checked = 5

        return results

    def _check_trace_widths(
        self,
        pcb: PCB,
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check minimum trace width for all segments."""
        min_width = design_rules.min_trace_width_mm

        for segment in pcb.segments:
            if segment.width < min_width:
                # Get net name if available
                net = pcb.get_net(segment.net_number)
                net_name = net.name if net else f"net:{segment.net_number}"

                results.add(
                    DRCViolation(
                        rule_id="dimension_trace_width",
                        severity="error",
                        message=(f"Trace width {segment.width:.3f}mm < minimum {min_width:.3f}mm"),
                        location=segment.start,
                        layer=segment.layer,
                        actual_value=segment.width,
                        required_value=min_width,
                        items=(net_name,),
                    )
                )

    def _check_via_dimensions(
        self,
        pcb: PCB,
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check via drill, diameter, and annular ring."""
        min_drill = design_rules.min_via_drill_mm
        min_diameter = design_rules.min_via_diameter_mm
        min_annular = design_rules.min_annular_ring_mm

        for via in pcb.vias:
            # Get net name for items list
            net = pcb.get_net(via.net_number)
            net_name = net.name if net else f"net:{via.net_number}"

            # Check drill diameter
            if via.drill < min_drill:
                results.add(
                    DRCViolation(
                        rule_id="dimension_via_drill",
                        severity="error",
                        message=(f"Via drill {via.drill:.3f}mm < minimum {min_drill:.3f}mm"),
                        location=via.position,
                        layer=via.layers[0] if via.layers else None,
                        actual_value=via.drill,
                        required_value=min_drill,
                        items=(net_name,),
                    )
                )

            # Check via outer diameter
            if via.size < min_diameter:
                results.add(
                    DRCViolation(
                        rule_id="dimension_via_diameter",
                        severity="error",
                        message=(f"Via diameter {via.size:.3f}mm < minimum {min_diameter:.3f}mm"),
                        location=via.position,
                        layer=via.layers[0] if via.layers else None,
                        actual_value=via.size,
                        required_value=min_diameter,
                        items=(net_name,),
                    )
                )

            # Check annular ring: (outer diameter - drill) / 2
            annular_ring = (via.size - via.drill) / 2
            if annular_ring < min_annular:
                results.add(
                    DRCViolation(
                        rule_id="dimension_annular_ring",
                        severity="error",
                        message=(
                            f"Annular ring {annular_ring:.3f}mm < minimum {min_annular:.3f}mm"
                        ),
                        location=via.position,
                        layer=via.layers[0] if via.layers else None,
                        actual_value=annular_ring,
                        required_value=min_annular,
                        items=(net_name,),
                    )
                )

    def _check_drill_clearance(
        self,
        pcb: PCB,
        design_rules: DesignRules,
        results: DRCResults,
    ) -> None:
        """Check drill-to-drill clearance.

        Uses min_clearance_mm as the minimum edge-to-edge distance between
        drill holes. This applies to both vias and through-hole pads.
        """
        min_clearance = design_rules.min_clearance_mm

        # Collect all drill holes: vias + through-hole pads
        drills: list[tuple[tuple[float, float], float, str]] = []

        # Add vias
        for via in pcb.vias:
            net = pcb.get_net(via.net_number)
            net_name = net.name if net else f"net:{via.net_number}"
            drills.append((via.position, via.drill, net_name))

        # Add through-hole pads from footprints
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.type == "thru_hole" and pad.drill > 0:
                    # Pad position is relative to footprint
                    abs_x = fp.position[0] + pad.position[0]
                    abs_y = fp.position[1] + pad.position[1]
                    net_name = pad.net_name if pad.net_name else f"net:{pad.net_number}"
                    drills.append(
                        ((abs_x, abs_y), pad.drill, f"{fp.reference}-{pad.number}:{net_name}")
                    )

        # Check all pairs for clearance
        # Edge-to-edge distance = center-to-center - (r1 + r2)
        for i, (pos1, drill1, item1) in enumerate(drills):
            for pos2, drill2, item2 in drills[i + 1 :]:
                # Calculate center-to-center distance
                dx = pos2[0] - pos1[0]
                dy = pos2[1] - pos1[1]
                center_distance = math.sqrt(dx * dx + dy * dy)

                # Edge-to-edge distance
                edge_distance = center_distance - (drill1 / 2) - (drill2 / 2)

                if edge_distance < min_clearance:
                    # Use midpoint as violation location
                    mid_x = (pos1[0] + pos2[0]) / 2
                    mid_y = (pos1[1] + pos2[1]) / 2

                    results.add(
                        DRCViolation(
                            rule_id="dimension_drill_clearance",
                            severity="error",
                            message=(
                                f"Drill-to-drill clearance {edge_distance:.3f}mm < "
                                f"minimum {min_clearance:.3f}mm"
                            ),
                            location=(mid_x, mid_y),
                            layer=None,  # Drills span multiple layers
                            actual_value=edge_distance,
                            required_value=min_clearance,
                            items=(item1, item2),
                        )
                    )
