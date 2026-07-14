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

import math
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.types import Layer

from ..violations import DRCResults, DRCViolation

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Footprint, FootprintGraphic
    from kicad_tools.validate.rules.courtyard_waivers import CourtyardWaivers

# Rule id for courtyard-overlap findings.  Matches the ``rule`` field expected
# in ``.courtyard_waivers.json`` entries and resolves to
# ``ViolationType.COURTYARD_OVERLAP`` via ``ViolationType.from_string`` (which
# maps any rule id containing ``"courtyard"``).
COURTYARD_RULE_ID = "courtyards_overlap"

# Rule id for the advisory "could not resolve courtyard outline" info finding.
COURTYARD_UNRESOLVED_RULE_ID = "courtyard_outline_unresolved"

# Rule id for the advisory "unused waiver" info finding.
COURTYARD_UNUSED_WAIVER_RULE_ID = "courtyard_waiver_unused"

# Endpoint-matching tolerance (mm) when chaining ``fp_line`` segments into a
# closed courtyard ring.  Courtyard vertices are authored on a coarse grid;
# 1e-3 mm is comfortably below any real feature yet absorbs float noise.
_CHAIN_EPSILON_MM = 1e-3


def _fp_transform(footprint: Footprint):
    """Return a ``(x, y) -> (X, Y)`` local->board transform for a footprint.

    Mirrors ``validate.rules.silkscreen._fp_transform``: KiCad negates the
    footprint orientation angle relative to CCW math (verified in #3739), so
    the rotation applied here is ``radians(-rotation)``.
    """
    fp_x, fp_y = footprint.position
    fp_rotation = math.radians(-footprint.rotation)
    cos_rot = math.cos(fp_rotation)
    sin_rot = math.sin(fp_rotation)

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        lx, ly = point
        return (
            fp_x + (lx * cos_rot - ly * sin_rot),
            fp_y + (lx * sin_rot + ly * cos_rot),
        )

    return transform


def _courtyard_side(layer: str) -> str | None:
    """Return ``"F"`` / ``"B"`` for a courtyard layer, else ``None``."""
    if layer == Layer.F_CRTYD.value:
        return "F"
    if layer == Layer.B_CRTYD.value:
        return "B"
    return None


def _rect_ring(graphic: FootprintGraphic) -> list[tuple[float, float]]:
    """Return the 5-point closed ring for an ``fp_rect`` graphic (local space)."""
    sx, sy = graphic.start
    ex, ey = graphic.end
    return [(sx, sy), (ex, sy), (ex, ey), (sx, ey), (sx, sy)]


def _chain_lines(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[tuple[float, float]] | None:
    """Chain line segments into a single closed ring by endpoint matching.

    Returns the ordered ring of vertices (first == last) when the segments
    form exactly one closed loop, else ``None`` (open chain, branching, or
    disconnected components -- not a resolvable simple courtyard).
    """
    if not segments:
        return None

    def close(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= _CHAIN_EPSILON_MM

    remaining = list(segments)
    start, current = remaining.pop(0)
    ring: list[tuple[float, float]] = [start, current]

    while remaining:
        for idx, (a, b) in enumerate(remaining):
            if close(a, current):
                current = b
                ring.append(current)
                remaining.pop(idx)
                break
            if close(b, current):
                current = a
                ring.append(current)
                remaining.pop(idx)
                break
        else:
            # No segment continues the chain -> not a single closed loop.
            return None

    # A closed loop returns to its start.
    if not close(ring[0], ring[-1]):
        return None
    return ring


def _courtyard_polygon(footprint: Footprint, side: str, Polygon: Any):
    """Build a shapely polygon for a footprint's courtyard on ``side``.

    ``side`` is ``"F"`` or ``"B"``.  Returns ``None`` when no courtyard
    geometry on that side can be resolved into a valid closed polygon.
    """
    target_layer = Layer.F_CRTYD.value if side == "F" else Layer.B_CRTYD.value
    transform = _fp_transform(footprint)

    rects: list[FootprintGraphic] = []
    polys: list[FootprintGraphic] = []
    line_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for graphic in footprint.graphics:
        if graphic.layer != target_layer:
            continue
        if graphic.graphic_type == "rect":
            rects.append(graphic)
        elif graphic.graphic_type == "poly":
            polys.append(graphic)
        elif graphic.graphic_type == "line":
            line_segments.append((graphic.start, graphic.end))
        # circle / arc courtyards are not modeled (rare for courtyards).

    ring: list[tuple[float, float]] | None = None
    if rects:
        # A single rect fully describes the courtyard; if multiple exist we
        # take the first (the common single-rect case).
        ring = _rect_ring(rects[0])
    elif polys and len(polys[0].points) >= 3:
        ring = list(polys[0].points)
    elif line_segments:
        ring = _chain_lines(line_segments)

    if ring is None or len(ring) < 3:
        return None

    board_ring = [transform(p) for p in ring]
    polygon = Polygon(board_ring)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty or polygon.area <= 0:
        return None
    return polygon


def _has_courtyard_geometry(footprint: Footprint) -> bool:
    """True if the footprint has any F/B CrtYd graphic at all."""
    return any(_courtyard_side(graphic.layer) is not None for graphic in footprint.graphics)


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
                if not any(
                    graphic.layer == (Layer.F_CRTYD.value if side == "F" else Layer.B_CRTYD.value)
                    for graphic in fp.graphics
                ):
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
