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


def _covers_layer(d_layers: tuple[str, ...], layer: str) -> bool:
    """True when pad/via copper with ``d_layers`` exists on ``layer``.

    A through-via (outer layers listed) or a ``*.Cu`` through-hole pad spans
    every copper layer of the stack, including inner ones.
    """
    if layer in d_layers or "*.Cu" in d_layers:
        return True
    return "F.Cu" in d_layers and "B.Cu" in d_layers and layer.endswith(".Cu")


def _is_buried(discs, net_name: str, layer: str, point: tuple[float, float]) -> bool:
    for d_net, d_layers, x, y, r in discs:
        if d_net != net_name:
            continue
        if not _covers_layer(d_layers, layer):
            continue
        if math.dist(point, (x, y)) <= r:
            return True
    return False


_JUNCTION_TOL = 0.01  # mm, matches AcidTrapCheck._find_junction


def _far_end(seg, junction: tuple[float, float]) -> tuple[float, float]:
    if (
        abs(seg.start[0] - junction[0]) < _JUNCTION_TOL
        and abs(seg.start[1] - junction[1]) < _JUNCTION_TOL
    ):
        return seg.end
    return seg.start


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
    """
    half = math.radians(angle_deg) / 2
    s0 = (seg1.width + seg2.width) / (4 * math.sin(half))
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
        found: dict[tuple, tuple[float, str, str, tuple[float, float]]] = {}
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
                found[key] = (worst, net_name, layer, loc)

        for angle, net_name, layer, loc in found.values():
            results.add(
                DRCViolation(
                    rule_id=SHARP_TRACE_CORNER_RULE_ID,
                    severity="warning",
                    message=(
                        f"Sharp trace corner of {angle:.1f} deg on {net_name} ({layer}); "
                        f"reroute with 45/90 deg corners"
                    ),
                    location=loc,
                    layer=layer,
                    actual_value=float(angle),
                    nets=(net_name,),
                )
            )
        return results
