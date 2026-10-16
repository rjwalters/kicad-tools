"""Equivalent-pin ties: short same-net links between equivalent pins (#6698).

Moved unchanged from :mod:`kicad_tools.router.lattice.coupled` (issue #6837,
slice 2c-i).  The planner generates the two octilinear dogleg candidates and
verifies them against exact pad copper; every engine-specific legality
question (layer presence, extra clearance, rule-area vetoes, fixed runs) is
answered by caller-supplied callbacks, so no engine type is imported here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .geometry import Pt, dist, seg_seg_dist

if TYPE_CHECKING:
    from ..primitives import Pad

#: An extra pad is tied to its anchor only when both sit on the same part and
#: the anchor is this close (epic #6441's ``EquivalentPinSet.max_tie_stub_mm``).
MAX_TIE_STUB_MM = 3.5

#: Necked tie widths are floored to this grid (mm), so a tie lands on a round
#: width (board 03: 0.202 mm of room -> 0.20 mm) with the remainder as margin.
_TIE_WIDTH_STEP = 0.01


@dataclass(frozen=True)
class PlannedTie:
    """A generated same-net tie between two equivalent pins (issue #6698).

    Attributes:
        net: The tie's net id (the extra's and the anchor's).
        layer: The caller's layer token (a lattice layer index in core).
        points: The octilinear centerline, extra pad centre first.
        width: Emitted copper width (mm), necked below ``class_width`` when
            the class width does not clear.
        class_width: The net class width the tie started from.
        length: Centerline length (mm).
    """

    net: int
    layer: int
    points: tuple[Pt, ...]
    width: float
    class_width: float
    length: float


def _tie_pad_gap(points: tuple[Pt, ...], pad: Pad) -> float:
    """Exact distance from the centerline ``points`` to ``pad``'s copper.

    A circle is measured analytically.  Rect / oval use the same shapely
    copper :func:`kicad_tools.geometry.copper.pad_waiver_polygon` builds for the audit; a
    shape it does not model (``roundrect`` -- it returns ``None`` -- or
    custom) falls back to the pad's bounding box, which contains the copper,
    so the result can only be pessimistic.  ``None`` is never "no obstacle".
    """
    from ..primitives import pad_half_extents

    if pad.shape == "circle" or (
        pad.shape == "oval" and abs(pad.width - pad.height) < 1e-6 and pad.width > 0
    ):
        from .geometry import seg_pt_dist

        radius = max(pad.width, pad.height) / 2.0
        return (
            min(seg_pt_dist(a, b, (pad.x, pad.y)) for a, b in zip(points, points[1:], strict=False))
            - radius
        )
    from shapely.geometry import LineString, box  # type: ignore[import-untyped]

    from kicad_tools.geometry.copper import pad_waiver_polygon

    polygon = pad_waiver_polygon(pad.x, pad.y, pad.width, pad.height, pad.shape, pad.rotation)
    if polygon is None:
        hw, hh = pad_half_extents(pad)
        polygon = box(pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh)
    return float(LineString(points).distance(polygon))


def plan_equivalent_pin_tie(
    extra: Pad,
    anchor: Pad,
    foreign: list[Pad],
    layers: list[int] | tuple[int, ...],
    *,
    width: float,
    clearance: float,
    min_width: float | None,
    accepted: list[PlannedTie] | tuple[PlannedTie, ...] = (),
    on_layer=None,
    clearance_for=None,
    fixed_runs: list[tuple[int, Pt, Pt, float, int]] | tuple = (),
    blocked=None,
) -> PlannedTie | None:
    """Generate and verify a short same-net tie from ``extra`` to ``anchor``.

    Issue #6698: a USB-C receptacle's two rows carry the same D+/D- on pads
    0.85 mm apart, too tight for the lattice (axis-aligned pad keep-outs and
    a board-global agent radius) to find the 1.70 mm row-to-row link a human
    draws.  The link is a fixed two-leg shape, so it is generated here and
    verified against EXACT copper instead of searched.

    Both octilinear doglegs (:func:`kicad_tools.router.quantize.dogleg_points`)
    are tried on every layer of ``layers`` (the routing layers both pads
    occupy).  Each candidate keeps the class ``width`` when it clears;
    otherwise it necks down to the widest ``_TIE_WIDTH_STEP`` multiple that
    clears, never below ``min_width`` (``None``: no neck-down at all).
    "Clears" means the trace copper is at least the required clearance from

    * every other-net pad in ``foreign`` present on that layer
      (``on_layer(pad, layer)``; default: every layer), the requirement being
      ``max(clearance, clearance_for(pad.net))``;
    * every other-net tie in ``accepted`` and run in ``fixed_runs``
      (``(layer, a, b, half_width, net)``) on that layer;

    and ``blocked(a, b, layer, half)`` (a rule-area veto) is false for every
    leg.  Returns the shortest clearing candidate (ties broken by the wider
    copper, then layer order), or ``None`` when none clears -- the caller
    then routes the extra exactly as before.
    """
    from ..quantize import dogleg_points

    if extra.net != anchor.net:
        return None
    net = extra.net
    if on_layer is None:
        # No layer model: treat every pad as present on every layer (never
        # under-mask -- the #3906 direction).
        def on_layer(_pad: Pad, _layer: int) -> bool:
            return True

    def need(other_net: int) -> float:
        extra_clr = clearance_for(other_net) if clearance_for is not None else 0.0
        return max(clearance, extra_clr or 0.0)

    start = (extra.x, extra.y)
    end = (anchor.x, anchor.y)
    shapes: list[tuple[Pt, ...]] = []
    for axis_first in (False, True):
        pts = tuple(
            (float(x), float(y))
            for x, y in dogleg_points(start[0], start[1], end[0], end[1], axis_first=axis_first)
        )
        if pts not in shapes:
            shapes.append(pts)

    reach = width / 2.0 + max(
        [clearance]
        + ([clearance_for(p.net) or 0.0 for p in foreign] if clearance_for is not None else [])
    )
    best: tuple[tuple[float, float, int, int], PlannedTie] | None = None
    for layer_rank, layer in enumerate(layers):
        for shape_rank, pts in enumerate(shapes):
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            # The widest copper the candidate can carry: twice the smallest
            # (centerline gap - required clearance) over every obstacle.
            room = math.inf
            for pad in foreign:
                if pad.net == net or not on_layer(pad, layer):
                    continue
                span = max(pad.width, pad.height) + reach
                if (
                    pad.x < min(xs) - span
                    or pad.x > max(xs) + span
                    or pad.y < min(ys) - span
                    or pad.y > max(ys) + span
                ):
                    continue
                room = min(room, 2.0 * (_tie_pad_gap(pts, pad) - need(pad.net)))
            runs = [
                (t.layer, a, b, t.width / 2.0, t.net)
                for t in accepted
                for a, b in zip(t.points, t.points[1:], strict=False)
            ] + list(fixed_runs)
            for r_layer, a, b, half, r_net in runs:
                if r_net == net or r_layer != layer:
                    continue
                gap = min(seg_seg_dist(p, q, a, b) for p, q in zip(pts, pts[1:], strict=False))
                room = min(room, 2.0 * (gap - half - need(r_net)))
            if room >= width - 1e-9:
                w = width
            elif min_width is None:
                continue
            else:
                w = math.floor(room / _TIE_WIDTH_STEP + 1e-6) * _TIE_WIDTH_STEP
                w = round(w, 4)
                if w < min_width - 1e-9:
                    if room < min_width - 1e-9:
                        continue
                    w = min_width
            if w <= 0:
                continue
            if blocked is not None and any(
                blocked(p, q, layer, w / 2.0) for p, q in zip(pts, pts[1:], strict=False)
            ):
                continue
            length = sum(dist(p, q) for p, q in zip(pts, pts[1:], strict=False))
            key = (round(length, 6), -w, layer_rank, shape_rank)
            if best is None or key < best[0]:
                best = (
                    key,
                    PlannedTie(
                        net=net,
                        layer=layer,
                        points=pts,
                        width=w,
                        class_width=width,
                        length=length,
                    ),
                )
    return best[1] if best is not None else None
