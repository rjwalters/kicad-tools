"""Courtyard-overlap DRC rule with pair-level waiver support (Issue #4137).

This module implements a pure-Python courtyard-overlap check that reads the
true ``F.CrtYd`` / ``B.CrtYd`` polygon geometry from each footprint (not the
pads-bbox approximation in :mod:`kicad_tools.placement.analyzer`) and flags
pairs of footprints on the same board side whose courtyards intersect.

Overlapping pairs that match a loaded waiver entry
(``.courtyard_waivers.json``) are reported as ``waived=True`` -- visible and
counted, but excluded from the error count so the gate passes.  This makes the
courtyard gate fully headless and independent of ``kicad-cli``'s per-instance
``drc_exclusions`` handling (which is not honored by ``kicad-cli`` 10.0.4
headless).

Courtyard-outline extraction supports two representations:

* ``fp_rect`` -- a single axis-aligned rectangle (fast path).
* ``fp_line`` -- a closed loop of line segments chained by endpoint matching.

A courtyard that cannot be resolved from these (e.g. an ``fp_poly``-only
outline, or an ``fp_line`` chain that does not close) emits an ``info``-severity
"could not resolve courtyard outline" finding instead of silently skipping the
footprint, so the gap is visible rather than a silent false negative.

Note on ``fp_poly``: the schema parser now records ``fp_poly`` vertices in
:attr:`FootprintGraphic.points`.  This rule *does* consume them when present
(a closed polygon is directly usable), so the "unresolved" info finding is
reserved for genuinely-degenerate outlines (no CrtYd geometry, non-closing
line chains, or fewer than three vertices).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.types import Layer
from kicad_tools.geometry.courtyard import (
    _chain_lines,
    _courtyard_polygon,
    _courtyard_side,
    _fp_transform,
    _has_courtyard_geometry,
    _rect_ring,
    _side_has_geometry,
)

from ..violations import DRCResults, DRCViolation

if TYPE_CHECKING:
    from kicad_tools.validate.rules.courtyard_waivers import CourtyardWaivers

# Re-exported for backward compatibility: these courtyard-geometry helpers now
# live in :mod:`kicad_tools.geometry.courtyard` (extracted in #4182 so that
# both ``kct check`` and ``kct placement check`` share one source of truth),
# but historically imported from this module.
__all__ = [
    "COURTYARD_RULE_ID",
    "COURTYARD_UNRESOLVED_RULE_ID",
    "COURTYARD_UNUSED_WAIVER_RULE_ID",
    "CourtyardOverlapRule",
    "_chain_lines",
    "_courtyard_polygon",
    "_courtyard_side",
    "_fp_transform",
    "_has_courtyard_geometry",
    "_rect_ring",
]

# Rule id for courtyard-overlap findings.  Matches the ``rule`` field expected
# in ``.courtyard_waivers.json`` entries and resolves to
# ``ViolationType.COURTYARD_OVERLAP`` via ``ViolationType.from_string`` (which
# maps any rule id containing ``"courtyard"``).
COURTYARD_RULE_ID = "courtyards_overlap"

# Rule id for the advisory "could not resolve courtyard outline" info finding.
COURTYARD_UNRESOLVED_RULE_ID = "courtyard_outline_unresolved"

# Rule id for the advisory "unused waiver" info finding.
COURTYARD_UNUSED_WAIVER_RULE_ID = "courtyard_waiver_unused"


class CourtyardOverlapRule:
    """Detect footprints whose courtyards overlap on the same board side.

    For each unordered pair of footprints on the same side (front/back) the
    rule builds their real ``F.CrtYd`` / ``B.CrtYd`` polygons and tests for a
    positive-area intersection.  Overlapping pairs are reported as
    ``severity="error"`` unless matched by a waiver entry, in which case the
    finding is emitted with ``waived=True`` (visible, counted, non-blocking).

    Rule IDs generated:
        - ``courtyards_overlap``: two courtyards overlap (error, or waived).
        - ``courtyard_outline_unresolved``: a footprint's courtyard could not
          be resolved to a polygon (info).
        - ``courtyard_waiver_unused``: a loaded waiver names a pair not present
          on the board (info).
    """

    rule_id = COURTYARD_RULE_ID
    name = "Courtyard Overlap"
    description = "Check that footprint courtyards do not overlap on the same board side"

    def __init__(self, waivers: CourtyardWaivers | None = None) -> None:
        self.waivers = waivers

    def check(self, pcb: Any) -> DRCResults:
        """Check all footprint courtyard pairs for overlap.

        Args:
            pcb: The PCB to check.

        Returns:
            DRCResults containing courtyard-overlap findings (error / waived),
            plus advisory info findings for unresolved outlines and unused
            waivers.
        """
        require_shapely("courtyard-overlap check")
        from shapely.geometry import Polygon  # type: ignore[import-untyped]

        results = DRCResults()
        results.rules_checked += 1

        footprints = list(pcb.footprints)

        # Resolve each footprint's courtyard polygon per side.  A footprint
        # can carry courtyards on both sides (e.g. tall parts), so key by
        # (reference, side).
        polygons: dict[tuple[str, str], Any] = {}
        for fp in footprints:
            has_geom = _has_courtyard_geometry(fp)
            resolved_any = False
            for side in ("F", "B"):
                # Only attempt sides that actually have geometry.
                if not _side_has_geometry(fp, side):
                    continue
                poly = _courtyard_polygon(fp, side, Polygon)
                if poly is not None:
                    polygons[(fp.reference, side)] = poly
                    resolved_any = True
            if has_geom and not resolved_any:
                # Geometry exists on a CrtYd layer but we could not build a
                # polygon from it -- surface the gap.
                results.add(
                    DRCViolation(
                        rule_id=COURTYARD_UNRESOLVED_RULE_ID,
                        severity="info",
                        message=(
                            f"Could not resolve courtyard outline for "
                            f"{fp.reference} (unsupported or non-closing "
                            f"courtyard geometry); pair overlap not checked "
                            f"for this footprint"
                        ),
                        location=fp.position,
                        layer=fp.layer,
                        items=(fp.reference,),
                    )
                )

        # Pairwise overlap test within each side.
        keys = list(polygons.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                ref_a, side_a = keys[i]
                ref_b, side_b = keys[j]
                if side_a != side_b:
                    # Courtyards on different board sides never conflict.
                    continue
                if ref_a == ref_b:
                    continue
                poly_a = polygons[keys[i]]
                poly_b = polygons[keys[j]]
                if not poly_a.intersects(poly_b):
                    continue
                inter = poly_a.intersection(poly_b)
                if inter.is_empty or inter.area <= 0:
                    # Exactly-touching (zero-area) courtyards do not conflict.
                    continue
                self._emit_overlap(results, ref_a, ref_b, side_a, inter.area)

        # Advisory: waivers that did not match any board pair (stale entries).
        if self.waivers is not None:
            present_refs = {fp.reference for fp in footprints}
            for entry in self.waivers.entries:
                missing = [r for r in entry.refs if r not in present_refs]
                if missing:
                    results.add(
                        DRCViolation(
                            rule_id=COURTYARD_UNUSED_WAIVER_RULE_ID,
                            severity="info",
                            message=(
                                f"Unused courtyard waiver for "
                                f"{entry.refs[0]}/{entry.refs[1]}: "
                                f"component(s) {', '.join(missing)} not present "
                                f"on the board (stale waiver -- consider "
                                f"pruning)"
                            ),
                            items=tuple(entry.refs),
                        )
                    )

        return results

    def _emit_overlap(
        self,
        results: DRCResults,
        ref_a: str,
        ref_b: str,
        side: str,
        overlap_area: float,
    ) -> None:
        """Emit a courtyard-overlap finding, waived when a waiver matches."""
        waiver = None
        if self.waivers is not None:
            waiver = self.waivers.match(ref_a, ref_b)

        side_layer = Layer.F_CRTYD.value if side == "F" else Layer.B_CRTYD.value
        base_message = (
            f"Courtyards of {ref_a} and {ref_b} overlap ({overlap_area:.3f} mm^2) on {side_layer}"
        )

        if waiver is not None:
            results.add(
                DRCViolation(
                    rule_id=COURTYARD_RULE_ID,
                    severity="error",
                    message=f"{base_message} [WAIVED: {waiver.reason}]",
                    layer=side_layer,
                    actual_value=round(overlap_area, 4),
                    items=(ref_a, ref_b),
                    waived=True,
                    waiver_reason=waiver.reason,
                    waiver_issue=waiver.issue,
                )
            )
        else:
            results.add(
                DRCViolation(
                    rule_id=COURTYARD_RULE_ID,
                    severity="error",
                    message=base_message,
                    layer=side_layer,
                    actual_value=round(overlap_area, 4),
                    items=(ref_a, ref_b),
                )
            )
