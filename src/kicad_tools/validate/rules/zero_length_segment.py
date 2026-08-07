"""Zero-length copper segment rule (issue #4651).

Zero-length segments -- copper trace segments whose start and end
coincide -- are always routing artifacts: they contribute no
connectivity, confuse length-matching math, and can survive save/load
round-trips indefinitely.  The advisory routing-quality metrics
(:mod:`kicad_tools.analysis.routing_quality`, issue #4623) detect and
count them (``zero_length_count``) but exclude them from every
statistic; until this rule nothing ever *reported* them as findings.

This rule promotes each zero-length segment to a real, per-segment DRC
finding so the artifacts become actionable and -- because it is a
normal ``DRCRule`` flowing through :meth:`DRCChecker.check_all` -- each
finding is waivable through the central ``.kct_waivers.json`` mechanism
(:mod:`kicad_tools.validate.rules.waivers`, issue #4417) with no extra
plumbing.

**Detection parity:** the rule uses the exact same predicate as the
analysis module (``length <= COORD_EPSILON_MM``, imported from
:mod:`kicad_tools.analysis.routing_quality`), so the advisory stanza's
``zero_length_count`` and this rule's violation count can never
diverge on the same board.

**Severity:** ``warning``, not ``error``.  The issue-#4651 fleet
pre-check (2026-08-07) found the repository's own board-05
(``bldc_controller_routed.kicad_pcb``) carrying 5 zero-length
segments, so defaulting to error would immediately fail a shipping
board.  Warning surfaces the artifacts everywhere (and is fatal under
``--strict``) while the waiver path and future cleanup harden it.

**Waiver matching:** each finding carries the segment's net in
``nets`` and a stable ``layer@(x,y)`` identifier in ``items`` (the
sheet-absolute coordinate, matching the reported finding location, to
4 decimals), so a waiver entry can target either one specific segment
(by ``items``) or every zero-length artifact on a net (by ``nets``).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.analysis.routing_quality import COORD_EPSILON_MM

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class ZeroLengthSegmentRule(DRCRule):
    """Flag every zero-length copper trace segment as a routing artifact."""

    rule_id = "zero_length_segment"
    name = "Zero-Length Segment"
    description = (
        "Detects copper trace segments whose start and end coincide "
        "(always routing artifacts; excluded from routing-quality stats)"
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Emit one warning per zero-length segment in ``pcb.segments``.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules (unused by this rule but required
                by the base-class interface).

        Returns:
            DRCResults with one per-segment ``zero_length_segment``
            warning carrying net + layer + position (needed for
            ``.kct_waivers.json`` matching).
        """
        results = DRCResults()
        results.rules_checked = 1

        # Compose the waiver-matching ``items`` id in sheet-absolute
        # coordinates so it agrees with the reported finding location
        # (``DRCChecker._absolutize`` shifts ``location`` but never
        # touches ``items``).
        ox, oy = getattr(pcb, "board_origin", (0.0, 0.0))

        for seg in pcb.segments:
            dx = seg.end[0] - seg.start[0]
            dy = seg.end[1] - seg.start[1]
            if math.hypot(dx, dy) > COORD_EPSILON_MM:
                continue

            x, y = seg.start
            net_label = seg.net_name if seg.net_name else f"net {seg.net_number}"
            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="warning",
                    message=(
                        f"Zero-length segment on '{net_label}' ({seg.layer}) -- "
                        f"routing artifact carrying no copper; remove it "
                        f"(width {seg.width:.3f} mm)"
                    ),
                    location=(x, y),
                    layer=seg.layer,
                    actual_value=0.0,
                    items=(f"{seg.layer}@({x + ox:.4f},{y + oy:.4f})",),
                    nets=(seg.net_name,) if seg.net_name else (),
                )
            )

        return results
