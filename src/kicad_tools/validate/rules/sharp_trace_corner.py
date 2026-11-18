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

Calibration (issue #8840, field data from the board behind epic #8838): JLC's
DFM tool flagged three vertices, all two-segment 45 deg fold-backs on 0.2/0.3 mm
tracks away from pads and vias.  The rule reports all three (pinned in
``tests/test_validate_sharp_trace_corner.py``).  On that board JLC flagged none
of the 95 joints that were branch points, at vias or inside pads; vertices
under same-net pad/via copper are skipped here for that reason, and because
there is no open wedge in the copper.  "Under" means inside the pad's real
copper outline, not just its inscribed circle (issue #9037).  Branch
junctions with an open wedge are still reported (operator ruling on PR #8848, 2026-11-12): this is stricter than
the observed fab behaviour, not looser.  Eight unflagged two-segment bends on
that board look like the flagged three; they are unknown, not negatives, so
they were not used to narrow the rule.  The angle threshold was not changed.

Sub-types (issue #9037): every finding carries ``corner_class`` --
``"bend"`` when exactly two segments meet at the vertex (the population
JLCDFM was observed to flag) or ``"junction"`` when three or more do.
Reporting is unchanged -- the tag only makes the populations separable, so
a consumer can gate on the fab-equivalent two-segment subset.  Stacked
duplicate segments count once, so a duplicated bend is not misclassified
as a junction.  The fab-DFM parity table attributes only the bend
population to JLCDFM's category (``sharp_trace_corner#bend``).

Inner layers (issue #9037 decision): in scope.  The fab's three flagged
vertices on the consumer board were all on outer layers (F.Cu/B.Cu), so
inner-layer behaviour was never observed from the fab either way; the rule
still reports inner-layer acute corners like outer ones because the angle
geometry of an acid trap is layer-independent and the findings are
warnings (a plain ``kct check`` still exits 0).  The same-net through-via
burial exemption applies on inner layers via :func:`_covers_layer`.

Rule ID generated:
    - sharp_trace_corner: two same-net, same-layer segments meet at an angle
      below the limit, away from same-net pad/via copper on that layer.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

SHARP_TRACE_CORNER_RULE_ID = "sharp_trace_corner"


def _copper_discs(pcb: PCB) -> list[tuple[str, tuple[str, ...], float, float, float, Any]]:
    """``(net_name, layers, x, y, radius, outline)`` of pad/via copper that can bury a vertex.

    A pad's ``outline`` is its true copper shape (the clearance kernel's
    polygon, roundrect/oval honoured), so a vertex anywhere inside the pad
    counts, not only one inside its inscribed circle (issue #9037: a vertex
    inside an elongated pad but off its centre was reported).  When no outline
    is available the inscribed circle (half the smaller dimension) is the
    fallback, so a vertex is only treated as buried when it is certainly
    under copper.  Vias are discs (``outline`` is ``None``).
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    from .clearance import CopperElement

    discs: list[tuple[str, tuple[str, ...], float, float, float, Any]] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            try:
                outline = CopperElement.from_pad(pad, fp).polygon
            except Exception:  # unmodelled pad shape: inscribed circle only
                outline = None
            discs.append(
                (
                    pad.net_name,
                    tuple(pad.layers),
                    fp.position[0] + dx,
                    fp.position[1] + dy,
                    min(pad.size) / 2,
                    outline,
                )
            )
    for via in pcb.vias:
        discs.append(
            (via.net_name, tuple(via.layers), via.position[0], via.position[1], via.size / 2, None)
        )
    return discs


def _covers_layer(d_layers: tuple[str, ...], layer: str) -> bool:
    """True when pad/via copper with ``d_layers`` exists on ``layer``.

    A through-via (outer layers listed) or a ``*.Cu`` through-hole pad spans
    every copper layer of the stack, including inner ones.
    """
    if layer in d_layers or "*.Cu" in d_layers:
        return True
    return "F.Cu" in d_layers and "B.Cu" in d_layers and layer.endswith(".Cu")


def _is_buried(discs, net_name: str, layer: str, point: tuple[float, float]) -> bool:
    candidate = None
    for d_net, d_layers, x, y, r, outline in discs:
        if d_net != net_name:
            continue
        if not _covers_layer(d_layers, layer):
            continue
        if math.dist(point, (x, y)) <= r:
            return True
        if outline is not None and not outline.is_empty:
            if candidate is None:
                from shapely.geometry import Point  # type: ignore[import-untyped]

                candidate = Point(point)
            if outline.covers(candidate):
                return True
    return False


_JUNCTION_TOL = 0.01  # mm, matches AcidTrapCheck._find_junction


def _far_end(seg, junction: tuple[float, float]) -> tuple[float, float]:
    if (
        abs(seg.start[0] - junction[0]) < _JUNCTION_TOL
        and abs(seg.start[1] - junction[1]) < _JUNCTION_TOL
    ):
        return (float(seg.end[0]), float(seg.end[1]))
    return (float(seg.start[0]), float(seg.start[1]))


def _point_segment_dist(p, a, b) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / len2))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _wedge_is_open(seg1, seg2, junction: tuple[float, float], angle_deg: float) -> bool:
    """Whether copper outlines of the two legs leave an unfilled acute notch.

    Judged from the copper outline, not the centrelines: the facing edges of
    the legs only separate at ``s0 = (w1 + w2) / (4 sin(theta / 2))`` along the
    legs, so a leg shorter than that (a width-filled hairpin) leaves solid
    copper, and a thin leg lying entirely inside a wider one adds no notch.

    The router's ``acute_foldback.wedge_is_open`` is a second copy of this
    predicate; ``tests/router/test_acute_foldback_parity_8930.py`` drives both
    over one shared geometry table, so a change to either exemption fails there.
    """
    half = math.radians(angle_deg) / 2
    sin_half = math.sin(half)
    if sin_half <= 0.0:
        # Collinear doubling-back: no wedge to be open.  Matches
        # ``acute_foldback.wedge_is_open`` (Issue #8930).
        return False
    s0 = (seg1.width + seg2.width) / (4 * sin_half)
    p1, p2 = _far_end(seg1, junction), _far_end(seg2, junction)
    if min(math.dist(junction, p1), math.dist(junction, p2)) <= s0:
        return False
    for thin, wide in ((seg1, seg2), (seg2, seg1)):
        margin = wide.width / 2 - thin.width / 2
        if margin >= 0 and all(
            _point_segment_dist(pt, wide.start, wide.end) <= margin for pt in (thin.start, thin.end)
        ):
            return False
    return True


class SharpTraceCornerRule(DRCRule):
    """Warn on acute angles where two traces of one net meet."""

    rule_id = SHARP_TRACE_CORNER_RULE_ID
    name = "Sharp Trace Corner"
    description = "Flags acute (< 90 deg) corners between connected trace segments"

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Return one ``sharp_trace_corner`` warning per acute junction."""
        del design_rules
        from kicad_tools.explain.checks.acid_trap import (
            ANGLE_TOLERANCE_DEG,
            MIN_CORNER_ANGLE_DEG,
            MIN_TRACE_ANGLE_DEG,
            AcidTrapCheck,
        )

        results = DRCResults()
        results.rules_checked = 1
        discs = _copper_discs(pcb)
        check = AcidTrapCheck()
        segs: dict[tuple[int, str], list] = {}
        for seg in pcb.segments:
            segs.setdefault((seg.net_number, seg.layer), []).append(seg)
        net_numbers = {}
        for num, _layer in segs:
            net = pcb.get_net(num)
            net_numbers[net.name if net else f"Net {num}"] = num

        # One report per (net, layer, location): stacked duplicate segments
        # would otherwise report the same vertex once per pair.
        found: dict[tuple, tuple[float, str, str, tuple[float, float], str, int]] = {}
        for mistake in check.check(pcb):
            net_name, layer = mistake.components[0], mistake.components[1]
            loc = mistake.location
            if not loc or net_name not in net_numbers:
                continue
            # A vertex under same-net pad/via copper has no etchant-trapping
            # wedge (pad-centre fan-in): not a fab defect.
            if _is_buried(discs, net_name, layer, loc):
                continue
            key = (net_name, layer, round(loc[0], 2), round(loc[1], 2))
            if key in found:
                continue
            touching = [
                s
                for s in segs.get((net_numbers[net_name], layer), [])
                if min(math.dist(loc, s.start), math.dist(loc, s.end)) < _JUNCTION_TOL * 1.5
            ]
            # Count distinct legs: stacked duplicate segments (issue #8840's
            # art artifact) share one (start, end) pair and must not turn a
            # two-segment bend into a "junction" (issue #9037).
            distinct: list = []
            seen_edges: set[tuple[tuple[float, float], tuple[float, float]]] = set()
            for s in touching:
                p_a = (round(s.start[0], 2), round(s.start[1], 2))
                p_b = (round(s.end[0], 2), round(s.end[1], 2))
                edge = (p_a, p_b) if p_a <= p_b else (p_b, p_a)
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    distinct.append(s)
            corner_class = "junction" if len(distinct) >= 3 else "bend"
            worst = None
            for i, s1 in enumerate(touching):
                for s2 in touching[i + 1 :]:
                    angle = check._calculate_angle(s1, s2, loc)
                    if angle is None or not (
                        MIN_CORNER_ANGLE_DEG <= angle < MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG
                    ):
                        continue
                    if _wedge_is_open(s1, s2, loc, angle) and (worst is None or angle < worst):
                        worst = angle
            if worst is not None:
                found[key] = (worst, net_name, layer, loc, corner_class, len(distinct))

        for angle, net_name, layer, loc, corner_class, n_legs in found.values():
            kind = f"; {n_legs}-segment junction" if corner_class == "junction" else ""
            results.add(
                DRCViolation(
                    rule_id=SHARP_TRACE_CORNER_RULE_ID,
                    severity="warning",
                    message=(
                        f"Sharp trace corner of {angle:.1f} deg on {net_name} ({layer}){kind}; "
                        f"reroute with 45/90 deg corners"
                    ),
                    location=loc,
                    layer=layer,
                    actual_value=float(angle),
                    nets=(net_name,),
                    corner_class=corner_class,
                )
            )
        return results
