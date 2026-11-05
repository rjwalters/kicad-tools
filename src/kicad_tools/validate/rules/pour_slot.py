"""Pour-slot advisory: parallel traces too close for the zone fill (Issue #8435).

Two different-net tracks that run side by side on a layer carrying a copper
zone leave a strip of pour between them.  The zone keeps ``clearance`` away
from each track, so the strip is ``gap - 2 * clearance`` wide (``gap`` is the
edge-to-edge distance).  A strip narrower than the zone's ``min_thickness``
cannot be filled, so KiCad drops it and the plane gets a *slot* -- a long
copper-free channel that passes every DRC gate.

Rule id ``pour_slot`` (always ``severity="warning"``).  A pair is reported
when the tracks are parallel, on the same layer as a pour zone, on different
nets, overlap along their length, and::

    0 < gap < 2 * clearance + min_thickness

``clearance`` is ``max(zone clearance, netclass clearance of either net)``.
Touching/overlapping tracks (``gap <= 0``) are a DRC matter and not reported.

Which fill-gap case the threshold matches (see :meth:`Zone.fill_inflation`):
that docstring's ``gap <= min_thickness`` row is about when two *fill
fragments* are electrically bonded, which is a different question.  Here the
question is whether a strip *between tracks* survives the fill, and KiCad
discards fill islands narrower than ``min_thickness`` after clearing each
track by ``clearance``; hence ``2 * clearance + min_thickness``.  This is a
geometric advisory, not a native-DRC oracle, and it is independent of the
board's file version.

The repair pass (moving/packing traces) is the scope of #8440.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

RULE_ID = "pour_slot"

#: Sine of the largest angle (between track directions) treated as parallel.
PARALLEL_SIN_TOL = 0.02
#: Minimum side-by-side overlap (mm) worth reporting.
MIN_OVERLAP_MM = 0.01


def _unit(seg: Any) -> tuple[float, float, float] | None:
    dx, dy = seg.end[0] - seg.start[0], seg.end[1] - seg.start[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    return dx / length, dy / length, length


class PourSlotRule(DRCRule):
    """Report parallel different-net track pairs that leave a slot in a pour."""

    rule_id = RULE_ID
    name = "Pour Slot"
    description = (
        "Reports parallel different-net tracks closer than 2 x clearance + "
        "min_thickness on a poured layer, which leave a slot in the plane (advisory)."
    )

    def __init__(self, netclass_clearances: Mapping[str, float] | None = None) -> None:
        #: Net name -> authored netclass clearance (mm); optional.
        self.netclass_clearances = dict(netclass_clearances or {})

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule[RULE_ID] = 1

        # layer -> (zone clearance, min_thickness): the widest requirement of
        # the pours on that layer is the conservative (largest) threshold.
        zones: dict[str, list[Any]] = defaultdict(list)
        for zone in pcb.zones:
            if getattr(zone, "keepout", None) is not None:
                continue
            for layer in {zone.layer, *getattr(zone, "layers", [])}:
                if layer:
                    zones[layer].append(zone)
        if not zones:
            return results

        by_layer: dict[str, list[tuple[Any, tuple[float, float, float]]]] = defaultdict(list)
        for seg in pcb.segments:
            if seg.layer in zones and seg.width > 0 and seg.net_number:
                u = _unit(seg)
                if u is not None:
                    by_layer[seg.layer].append((seg, u))

        for layer, items in by_layer.items():
            self._check_layer(layer, items, zones[layer], results)
        return results

    # ------------------------------------------------------------------

    def _pair_clearance(self, zlist: list[Any], a: Any, b: Any) -> tuple[float, float]:
        """(clearance, min_thickness) for a pair: zone vs netclass maximum."""
        clearance = max(z.clearance for z in zlist)
        thickness = max(z.min_thickness for z in zlist)
        for seg in (a, b):
            clearance = max(clearance, self.netclass_clearances.get(seg.net_name, 0.0))
        return clearance, thickness

    def _check_layer(
        self,
        layer: str,
        items: list[tuple[Any, tuple[float, float, float]]],
        zlist: list[Any],
        results: DRCResults,
    ) -> None:
        max_clear = max([z.clearance for z in zlist] + list(self.netclass_clearances.values()))
        max_thick = max(z.min_thickness for z in zlist)
        max_w = max(s.width for s, _u in items)
        reach = 2 * max_clear + max_thick + max_w  # centre-to-centre search radius
        cell = max(reach, 0.1)

        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (seg, _u) in enumerate(items):
            xs = (seg.start[0], seg.end[0])
            ys = (seg.start[1], seg.end[1])
            for cx in range(int((min(xs) - reach) // cell), int((max(xs) + reach) // cell) + 1):
                for cy in range(int((min(ys) - reach) // cell), int((max(ys) + reach) // cell) + 1):
                    grid[(cx, cy)].append(i)

        seen: set[tuple[int, int]] = set()
        for members in grid.values():
            for ai, i in enumerate(members):
                for j in members[ai + 1 :]:
                    key = (i, j) if i < j else (j, i)
                    if key in seen:
                        continue
                    seen.add(key)
                    self._check_pair(layer, items[key[0]], items[key[1]], zlist, results)

    def _check_pair(
        self,
        layer: str,
        first: tuple[Any, tuple[float, float, float]],
        second: tuple[Any, tuple[float, float, float]],
        zlist: list[Any],
        results: DRCResults,
    ) -> None:
        a, (ux, uy, la) = first
        b, (vx, vy, _lb) = second
        if a.net_number == b.net_number:
            return
        if abs(ux * vy - uy * vx) > PARALLEL_SIN_TOL:
            return

        # Project b's ends onto a's frame: t along a, d perpendicular to a.
        def frame(p: tuple[float, float]) -> tuple[float, float]:
            rx, ry = p[0] - a.start[0], p[1] - a.start[1]
            return rx * ux + ry * uy, -rx * uy + ry * ux

        tb0, db0 = frame(b.start)
        tb1, db1 = frame(b.end)
        overlap = min(la, max(tb0, tb1)) - max(0.0, min(tb0, tb1))
        if overlap < MIN_OVERLAP_MM:
            return
        centre_dist = abs((db0 + db1) / 2)
        gap = centre_dist - (a.width + b.width) / 2
        clearance, thickness = self._pair_clearance(zlist, a, b)
        threshold = 2 * clearance + thickness
        if gap <= 1e-6 or gap >= threshold - 1e-6:
            return

        mid_t = (max(0.0, min(tb0, tb1)) + min(la, max(tb0, tb1))) / 2
        loc = (a.start[0] + ux * mid_t, a.start[1] + uy * mid_t)
        na = a.net_name or f"net {a.net_number}"
        nb = b.net_name or f"net {b.net_number}"
        results.add(
            DRCViolation(
                rule_id=RULE_ID,
                severity="warning",
                message=(
                    f"Pour slot: [{na}] and [{nb}] run parallel on {layer} for "
                    f"{overlap:.2f} mm with {gap:.3f} mm gap; the pour between them "
                    f"(< {threshold:.3f} mm = 2 x {clearance:.3f} clearance + "
                    f"{thickness:.3f} min_thickness) cannot fill, leaving a slot"
                ),
                location=loc,
                layer=layer,
                actual_value=gap,
                required_value=threshold,
                nets=(na, nb),
            )
        )
