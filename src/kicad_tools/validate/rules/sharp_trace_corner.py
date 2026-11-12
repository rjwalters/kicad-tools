"""Sharp (acute) trace corner DRC rule (Issue #8840).

JLCDFM flags acute trace corners ("acid traps") that ``kct check`` never
reported: the only detector was the ``explain``-mode
:class:`~kicad_tools.explain.checks.acid_trap.AcidTrapCheck`.  This rule runs
that same junction/angle logic (no second copy) and reports each finding as a
``sharp_trace_corner`` warning.

Threshold: :data:`~kicad_tools.explain.checks.acid_trap.MIN_TRACE_ANGLE_DEG`
(90 deg, less ``ANGLE_TOLERANCE_DEG``).  **Unverified** -- this value was not
traced against a JLCDFM report or a fab capability page (retrieved
2026-11-12: none consulted); it is the repo's existing acid-trap limit.

Rule ID generated:
    - sharp_trace_corner: two same-net, same-layer segments meet at an angle
      below the limit, away from same-net pad/via copper on that layer.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

SHARP_TRACE_CORNER_RULE_ID = "sharp_trace_corner"


def _copper_discs(pcb: PCB) -> list[tuple[str, tuple[str, ...], float, float, float]]:
    """``(net_name, layers, x, y, radius)`` of pad/via copper that can bury a vertex.

    Pads use the inscribed circle (half the smaller dimension), so a vertex is
    only treated as buried when it is certainly under copper.
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    discs = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            discs.append(
                (
                    pad.net_name,
                    tuple(pad.layers),
                    fp.position[0] + dx,
                    fp.position[1] + dy,
                    min(pad.size) / 2,
                )
            )
    for via in pcb.vias:
        discs.append(
            (via.net_name, tuple(via.layers), via.position[0], via.position[1], via.size / 2)
        )
    return discs


def _is_buried(discs, net_name: str, layer: str, point: tuple[float, float]) -> bool:
    for d_net, d_layers, x, y, r in discs:
        if d_net != net_name:
            continue
        if layer not in d_layers and "*.Cu" not in d_layers:
            continue
        if math.dist(point, (x, y)) <= r:
            return True
    return False


class SharpTraceCornerRule(DRCRule):
    """Warn on acute angles where two traces of one net meet."""

    rule_id = SHARP_TRACE_CORNER_RULE_ID
    name = "Sharp Trace Corner"
    description = "Flags acute (< 90 deg) corners between connected trace segments"

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Return one ``sharp_trace_corner`` warning per acute junction."""
        del design_rules
        from kicad_tools.explain.checks.acid_trap import AcidTrapCheck

        results = DRCResults()
        results.rules_checked = 1
        discs = _copper_discs(pcb)
        for mistake in AcidTrapCheck().check(pcb):
            net_name, layer = mistake.components[0], mistake.components[1]
            angle = mistake.measurements["angle_deg"]
            # A vertex under same-net pad/via copper has no etchant-trapping
            # wedge (pad-centre fan-in): not a fab defect.
            if mistake.location and _is_buried(discs, net_name, layer, mistake.location):
                continue
            results.add(
                DRCViolation(
                    rule_id=SHARP_TRACE_CORNER_RULE_ID,
                    severity="warning",
                    message=(
                        f"Sharp trace corner of {angle:.1f} deg on {net_name} ({layer}); "
                        f"reroute with 45/90 deg corners"
                    ),
                    location=mistake.location,
                    layer=layer,
                    actual_value=float(angle),
                    nets=(net_name,),
                )
            )
        return results
