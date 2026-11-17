"""Global-fiducial presence DRC rule (Issue #8532).

Pick-and-place machines register the board against global fiducials.  A
board that passes every electrical and fabrication gate can still be
unassemblable by machine when a populated side carries fewer than three of
them.  This rule counts fiducial footprints (library item name starting
with ``Fiducial`` in any library, a ``FID<n>`` reference, or a ``fiducial``
tag; see ``_is_fiducial``) on each
*populated* side (a side with at least one non-``board_only``, BOM-included
footprint) and warns when there are fewer than :data:`MIN_GLOBAL_FIDUCIALS`.

Rule ID generated:
    - fiducials_missing: a populated side has fewer than three fiducials.

A fully unpopulated board raises nothing; fiducials on an unpopulated side
do not satisfy a populated one.  ``kct pcb add-fiducials`` is the fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

FIDUCIALS_MISSING_RULE_ID = "fiducials_missing"

#: Minimum global fiducials per populated side (an L-shaped, non-collinear set).
MIN_GLOBAL_FIDUCIALS = 3


class FiducialsRule(DRCRule):
    """Check that every populated board side carries enough global fiducials."""

    rule_id = FIDUCIALS_MISSING_RULE_ID
    name = "Global Fiducials"
    description = "Checks that each populated board side has at least 3 global fiducials"

    def __init__(self, *, min_count: int = MIN_GLOBAL_FIDUCIALS, severity: str = "warning") -> None:
        if severity not in ("error", "warning", "info"):
            raise ValueError(f"invalid severity {severity!r}")
        if min_count < 1:
            raise ValueError("min_count must be >= 1")
        self.min_count = min_count
        self.severity = severity

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Return one ``fiducials_missing`` warning per under-fiducialled populated side."""
        del design_rules
        # Reuse the placement command's notion of "populated" and "fiducial"
        # so check and fix agree by construction.
        from kicad_tools.cli.pcb_add_fiducials import _existing_fiducials, _populated_sides

        results = DRCResults()
        results.rules_checked = 1
        for side in _populated_sides(pcb):
            found = _existing_fiducials(pcb, side)
            if found >= self.min_count:
                continue
            results.add(
                DRCViolation(
                    rule_id=FIDUCIALS_MISSING_RULE_ID,
                    severity=self.severity,
                    message=(
                        f"{side} side has {found} global fiducial(s), fewer than the "
                        f"{self.min_count} needed for machine assembly -- run "
                        f"`kct pcb add-fiducials`, or waive via .kct_waivers.json"
                    ),
                    layer="F.Cu" if side == "front" else "B.Cu",
                    actual_value=float(found),
                    required_value=float(self.min_count),
                )
            )
        return results
