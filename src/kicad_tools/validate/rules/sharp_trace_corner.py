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
      below the limit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

SHARP_TRACE_CORNER_RULE_ID = "sharp_trace_corner"


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
        for mistake in AcidTrapCheck().check(pcb):
            net_name, layer = mistake.components[0], mistake.components[1]
            angle = mistake.measurements["angle_deg"]
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
