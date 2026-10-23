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

from .geometry import Pt, dist, seg_seg_dist, segs_intersect

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


def _fit_tie_width(
    pts: tuple[Pt, ...],
    layer: int,
    net: int,
    foreign: list[Pad],
    accepted,
    fixed_runs,
    *,
    on_layer,
    need,
    reach: float,
    width: float,
    min_width: float | None,
    blocked,
    margin: float = 0.0,
    pad_floors: bool = False,
) -> float | None:
    """The copper width centerline ``pts`` can carry on ``layer``, or ``None``.

    The exact-copper verification of :func:`plan_equivalent_pin_tie` (issue
    #6698), shared with the length-balanced crossing ties (issue #7869):
    ``room`` is twice the smallest (centerline gap - required clearance -
    ``margin``) over every other-net pad of ``foreign`` on ``layer`` and every
    other-net run of ``accepted`` / ``fixed_runs`` there.  The class ``width``
    is kept when it fits; otherwise the copper necks down to the widest
    ``_TIE_WIDTH_STEP`` multiple that fits, never below ``min_width``
    (``None``: no neck-down).  ``blocked(a, b, layer, half)`` vetoes a leg.
    ``pad_floors`` (the crossing ties, #7869) also holds every pad to its own
    authored minimum (:attr:`Pad.authored_clearance`: the floor of a pad
    whose net id was neutralised, as board 06rf's 0.2 mm +3V3 pads are).
    """
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
        required = need(pad.net)
        if pad_floors:
            required = max(required, float(getattr(pad, "authored_clearance", 0.0) or 0.0))
        room = min(room, 2.0 * (_tie_pad_gap(pts, pad) - required - margin))
    runs = [
        (t.layer, a, b, t.width / 2.0, t.net)
        for t in accepted
        for a, b in zip(t.points, t.points[1:], strict=False)
    ] + list(fixed_runs)
    for r_layer, a, b, half, r_net in runs:
        if r_net == net or r_layer != layer:
            continue
        gap = min(seg_seg_dist(p, q, a, b) for p, q in zip(pts, pts[1:], strict=False))
        room = min(room, 2.0 * (gap - half - need(r_net) - margin))
    if room >= width - 1e-9:
        w = width
    elif min_width is None:
        return None
    else:
        w = math.floor(room / _TIE_WIDTH_STEP + 1e-6) * _TIE_WIDTH_STEP
        w = round(w, 4)
        if w < min_width - 1e-9:
            if room < min_width - 1e-9:
                return None
            w = min_width
    if w <= 0:
        return None
    if blocked is not None and any(
        blocked(p, q, layer, w / 2.0) for p, q in zip(pts, pts[1:], strict=False)
    ):
        return None
    return w


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
            w = _fit_tie_width(
                pts,
                layer,
                net,
                foreign,
                accepted,
                fixed_runs,
                on_layer=on_layer,
                need=need,
                reach=reach,
                width=width,
                min_width=min_width,
                blocked=blocked,
            )
            if w is None:
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


# ---------------------------------------------------------------------------
# Issue #7869: length-balanced crossing ties
# ---------------------------------------------------------------------------

#: The two length-balanced crossing ties are planned to within this of each
#: other (mm).  A small fraction of the tightest pair skew budget the
#: planner meets (USB 2.0 HS: 1.0 mm), so the pair leaves the connector
#: balanced and the rest of the budget stays with the routed body.
CROSSING_TIE_MATCH_MM = 0.05

#: Generated crossing-tie shapes keep this much MORE than the required
#: clearance (mm), so the 4-decimal coordinates a board file stores cannot
#: round a tie that clears exactly at the limit into a violation.  It only
#: tightens the check; the required clearance itself is unchanged.
_CROSSING_MARGIN_MM = 0.005

#: Parameter grid of the generated shapes (mm).
_GRID_MM = 0.05

#: Form U's run levels are solved on this finer grid (mm): the hairpin sits
#: between two pad rows with its turnaround a full Q16 run, so on board 06rf
#: it fits with no coarse-grid slack (issue #7869).
_FINE_GRID_MM = 0.01

#: Upper bounds on the shapes verified per search, so a connector with no
#: room declines in bounded time (the wrap grid is ~28k shapes, the padded
#: grid ~90k; most fail the cheap angle / Q16 / cached-room screens before
#: the exact check, and a full padded pass takes ~2 s).  On 06rf the
#: lengthened-wrap pass finds its hairpin ~44k shapes in.
_MAX_WRAP_CHECKS = 40000
_MAX_PAD_CHECKS = 100000

#: Outer-run chamfers a re-planned (lengthened) wrap may use (mm), smallest
#: first; the floor is the Q32 foot chamfer, as for form U's turnaround.
_WRAP_CHAMFERS_MM = (0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5)

_SQRT2 = math.sqrt(2.0)

#: The eight octilinear unit directions a crossing frame may be aligned to.
_OCTILINEAR = tuple(
    (round(math.cos(math.radians(a)), 12), round(math.sin(math.radians(a)), 12))
    for a in range(0, 360, 45)
)


def _grid(lo: float, hi: float, step: float = _GRID_MM) -> list[float]:
    n = int(math.floor((hi - lo) / step + 1e-9))
    return [round(lo + i * step, 6) for i in range(n + 1)]


def _polyline_length(pts) -> float:
    return sum(dist(p, q) for p, q in zip(pts, pts[1:], strict=False))


def _dedupe(pts: list[Pt]) -> list[Pt]:
    out: list[Pt] = []
    for p in pts:
        if not out or dist(out[-1], p) > 1e-9:
            out.append(p)
    return out


def corner_turns_deg(points) -> list[float]:
    """Turn angle (degrees) at every interior vertex of ``points``.

    0 is straight on, 45 is the Q32 chamfer corner, 90 a square corner and
    anything above 90 an acute (hairpin) corner.
    """
    turns = []
    for a, b, c in zip(points, points[1:], points[2:], strict=False):
        h1 = math.atan2(b[1] - a[1], b[0] - a[0])
        h2 = math.atan2(c[1] - b[1], c[0] - b[0])
        d = abs(math.degrees(h2 - h1)) % 360.0
        turns.append(min(d, 360.0 - d))
    return turns


def _all_45(local: list[Pt]) -> bool:
    """Every leg octilinear and every corner exactly a 45 degree turn (Q32)."""
    for a, b in zip(local, local[1:], strict=False):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dx > 1e-9 and dy > 1e-9 and abs(dx - dy) > 1e-6:
            return False
    return all(abs(t - 45.0) < 1e-6 for t in corner_turns_deg(local))


def _flats_ok(local: list[Pt], min_flat: float) -> bool:
    """Q16: every interior non-diagonal leg is at least ``min_flat`` long.

    That is every run along the rows *and* every run across them: a
    hairpin's turnaround, the leg that joins its two runs, is the flat top
    Q16 measures in a serpentine (the skew-bump gate's jog,
    :func:`~kicad_tools.router.diffpair_bump_gates.bump_gate_violation`,
    and the "flat tops and connectors" ``jog_spaced`` sizes).  Only the
    diagonal legs (the Q32 chamfers) and the first and last legs (the pad
    leads, the gate's lead-in and lead-out) are exempt.  The frame is
    octilinear, so a leg that is not diagonal is axis-aligned in it.
    """
    for a, b in list(zip(local, local[1:], strict=False))[1:-1]:
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if (dx < 1e-9 or dy < 1e-9) and max(dx, dy) < min_flat - 1e-9:
            return False
    return True


def _self_clear(pts: list[Pt], spacing: float) -> bool:
    """No leg comes within ``spacing`` of a leg three or more legs away."""
    segs = list(zip(pts, pts[1:], strict=False))
    for i in range(len(segs)):
        for j in range(i + 3, len(segs)):
            if seg_seg_dist(*segs[i], *segs[j]) < spacing - 1e-9:
                return False
    return True


class _CrossingFrame:
    """Local frame of a crossing: origin the wrap tie's start pad, ``u``
    towards the padded tie's start pad (the pad the wrap goes round), ``v``
    away from the far row.  Octilinear in the frame is octilinear on the
    board because ``u`` is snapped to one of the eight octilinear directions.
    """

    def __init__(self, origin: Pt, toward: Pt, far: Pt) -> None:
        dx, dy = toward[0] - origin[0], toward[1] - origin[1]
        n = math.hypot(dx, dy)
        self.ok = n > 1e-9
        if not self.ok:
            return
        ux, uy = dx / n, dy / n
        snap = min(_OCTILINEAR, key=lambda d: math.hypot(d[0] - ux, d[1] - uy))
        self.ok = math.hypot(snap[0] - ux, snap[1] - uy) < 1e-6
        self.o = origin
        self.u = snap
        self.n = (-snap[1], snap[0])
        if self.local(far)[1] > 0:
            self.n = (snap[1], -snap[0])

    def local(self, p: Pt) -> Pt:
        dx, dy = p[0] - self.o[0], p[1] - self.o[1]
        return (dx * self.u[0] + dy * self.u[1], dx * self.n[0] + dy * self.n[1])

    def world(self, p: Pt) -> Pt:
        return (
            self.o[0] + p[0] * self.u[0] + p[1] * self.n[0],
            self.o[1] + p[0] * self.u[1] + p[1] * self.n[1],
        )


def _wrap_shape(z: float, s: float, e: float, g: float, f: float, end: Pt) -> list[Pt]:
    """The wrap tie in the frame: up and over the partner pad, down beyond it.

    ``(0,0)`` -> optional lead ``z`` along +v -> 45 deg to the outer run at
    ``v = z + s`` -> outer run to ``u = g - e`` -> 45 deg chamfer ``e`` down
    to the descent column ``u = g`` -> descent -> 45 deg onto the far pad's
    column -> optional final lead ``f`` into the far pad.
    """
    a_u, a_v = end
    top = z + s
    v4 = a_v + f + (g - a_u)
    pts: list[Pt] = [(0.0, 0.0)]
    if z > 0:
        pts.append((0.0, z))
    pts += [(s, top), (g - e, top), (g, top - e), (g, v4), (a_u, a_v + f)]
    if f > 0:
        pts.append((a_u, a_v))
    return pts


def _c_shape(y1: float, k: float, j: float, m: float, start: Pt, end: Pt) -> list[Pt]:
    """The padded tie, form C: down, a run away from the wrap, down, in.

    ``start`` -> optional lead ``y1`` along -v -> 45 deg ``k`` onto the run
    at ``v_top`` -> run along -u -> 45 deg ``j`` onto the column
    ``u = end_u - m`` -> down -> 45 deg ``m`` into ``end``.
    """
    s_u, s_v = start
    e_u, e_v = end
    top = s_v - y1 - k
    col = e_u - m
    pts: list[Pt] = [start]
    if y1 > 0:
        pts.append((s_u, s_v - y1))
    pts += [(s_u - k, top), (col + j, top), (col, top - j), (col, e_v + m), end]
    return pts


def _hairpin(y1: float, k: float, d: float, m: float, lu: float, start: Pt, end: Pt) -> list[Pt]:
    """The padded tie, form U: form C's run, turned back under itself.

    ``start`` -> lead/45 deg onto the top run -> run along -u to the turn
    column ``u = lu`` -> 45 deg ``d`` corners round the turn -> bottom run
    back along +u at ``v = end_v + m`` -> 45 deg ``m`` into ``end``.
    """
    s_u, s_v = start
    e_u, e_v = end
    top = s_v - y1 - k
    bot = e_v + m
    pts: list[Pt] = [start]
    if y1 > 0:
        pts.append((s_u, s_v - y1))
    pts += [
        (s_u - k, top),
        (lu + d, top),
        (lu, top - d),
        (lu, bot + d),
        (lu + d, bot),
        (e_u - m, bot),
        end,
    ]
    return pts


def _ties_uncoupled_mm(
    a: PlannedTie, b: PlannedTie, pads: list[Pad], on_layer, layer: int
) -> float:
    """The pair's uncoupled figure (mm) over the two ties ``a`` and ``b`` alone.

    ``kct check``'s ``diff_pair_uncoupled`` measure
    (:func:`~kicad_tools.validate.rules.diffpair_uncoupled_length.measure_uncoupled`):
    each tie against the other, the larger of the two, with the ties
    themselves and the pads near them as the copper that can stand between
    two parallel runs.  The tie planner does not know which net is P (whose
    stored direction anchors KiCad's blocking probe), so both are tried and
    the worse figure kept.
    """
    from kicad_tools.validate.rules.diffpair_uncoupled_length import (
        Copper,
        PadRect,
        measure_uncoupled,
    )

    lname = f"L{layer}"

    def segs(t: PlannedTie):
        return [
            (p[0], p[1], q[0], q[1], lname, t.width)
            for p, q in zip(t.points, t.points[1:], strict=False)
        ]

    sa, sb = segs(a), segs(b)
    pts = list(a.points) + list(b.points)
    lo_x = min(p[0] for p in pts) - 1.0
    hi_x = max(p[0] for p in pts) + 1.0
    lo_y = min(p[1] for p in pts) - 1.0
    hi_y = max(p[1] for p in pts) + 1.0
    rects = []
    for pad in pads:
        if not on_layer(pad, layer) or not (lo_x <= pad.x <= hi_x and lo_y <= pad.y <= hi_y):
            continue
        ang = math.radians(float(getattr(pad, "rotation", 0.0) or 0.0))
        rects.append(
            PadRect(
                pad.x,
                pad.y,
                pad.width / 2.0,
                pad.height / 2.0,
                math.cos(ang),
                math.sin(ang),
                frozenset({lname}),
                str(pad.net),
            )
        )
    copper = Copper([(s[0], s[1], s[2], s[3], s[5], lname) for s in sa + sb], rects)
    na, nb = str(a.net), str(b.net)
    return max(
        max(
            measure_uncoupled(sa, sb, copper, lead, na, nb),
            measure_uncoupled(sb, sa, copper, not lead, nb, na),
        )
        for lead in (True, False)
    )


def plan_crossing_ties(
    wrap_extra: Pad,
    wrap_anchor: Pad,
    pad_extra: Pad,
    pad_anchor: Pad,
    foreign: list[Pad],
    layers: list[int] | tuple[int, ...],
    *,
    wrap_rules: dict,
    pad_rules: dict,
    accepted: list[PlannedTie] | tuple[PlannedTie, ...] = (),
    on_layer=None,
    clearance_for=None,
    fixed_runs: list[tuple[int, Pt, Pt, float, int]] | tuple = (),
) -> tuple[PlannedTie, PlannedTie] | None:
    """Plan two CROSSING same-part ties as one length-matched set (#7869).

    A mirrored USB-C receptacle (06rf J1: A6/B6 D+, A7/B7 D-) puts the two
    row-to-row ties of a pair across each other, so on one layer only one
    of them can take the direct dogleg.  The other has to go round a pad of
    its partner and comes out ~2 mm longer -- skew the tuner cannot remove
    within its bounds.  Both ties are generated here instead:

    * the WRAP tie (``wrap_extra`` -> ``wrap_anchor``) goes up and over the
      partner's pad in the same row and comes down beyond it
      (:func:`_wrap_shape`);
    * the PADDED tie (``pad_extra`` -> ``pad_anchor``) takes the room left
      inside the wrap, its length set to the wrap's with a run away from the
      wrap (:func:`_c_shape`) or, when that is too short, the same run
      turned back under itself (:func:`_hairpin`).  When even the shortest
      padded tie that fits is longer than the wrap, the wrap is re-planned
      to its length instead, the wrap of that length whose two ties leave
      the least uncoupled copper (:func:`_ties_uncoupled_mm`) kept.

    Every leg is octilinear and every corner a 45 degree turn (Q32); every
    interior non-diagonal leg -- runs along the rows and across them, a
    hairpin's turnaround included (:func:`_flats_ok`) -- is at least
    :data:`~kicad_tools.router.diffpair_bump_gates.MIN_JOG_WIDTHS` x the
    class width (Q16).  Nothing here is a tuning pattern: both ties are
    plain copper, committed like any other tie.  Both shapes are verified against exact copper with
    the :func:`plan_equivalent_pin_tie` rules (``_fit_tie_width``), each
    other included, with :data:`_CROSSING_MARGIN_MM` to spare.  The wrap
    is tried round the extras' row first, then round the anchors' row.

    ``wrap_rules`` / ``pad_rules`` carry each net's ``width``,
    ``clearance``, ``min_width`` and ``blocked`` (as for
    :func:`plan_equivalent_pin_tie`).  Returns ``(wrap, padded)`` with
    lengths within :data:`CROSSING_TIE_MATCH_MM`, or ``None`` -- the caller
    then declines both ties (all or nothing, #6697).
    """
    if (
        wrap_extra.net != wrap_anchor.net
        or pad_extra.net != pad_anchor.net
        or wrap_extra.net == pad_extra.net
    ):
        return None
    if not segs_intersect(
        (wrap_extra.x, wrap_extra.y),
        (wrap_anchor.x, wrap_anchor.y),
        (pad_extra.x, pad_extra.y),
        (pad_anchor.x, pad_anchor.y),
    ):
        return None
    if on_layer is None:

        def on_layer(_pad: Pad, _layer: int) -> bool:
            return True

    # The class width everywhere first; a necked tie only when no pair of
    # class-width ties fits (the #6698 neck-down rule, per shape).
    for neck in (False, True):
        for layer in layers:
            for flip in (False, True):
                if flip:
                    ends = (wrap_anchor, wrap_extra, pad_anchor, pad_extra)
                else:
                    ends = (wrap_extra, wrap_anchor, pad_extra, pad_anchor)
                got = _plan_crossing_on_layer(
                    *ends,
                    foreign,
                    layer,
                    wrap_rules=wrap_rules,
                    pad_rules=pad_rules,
                    accepted=accepted,
                    on_layer=on_layer,
                    clearance_for=clearance_for,
                    fixed_runs=fixed_runs,
                    neck=neck,
                )
                if got is None:
                    continue
                if flip:
                    # Ties are recorded extra pad centre first.
                    wrap, padded = (
                        PlannedTie(
                            net=t.net,
                            layer=t.layer,
                            points=tuple(reversed(t.points)),
                            width=t.width,
                            class_width=t.class_width,
                            length=t.length,
                        )
                        for t in got
                    )
                    return wrap, padded
                return got
    return None


def _plan_crossing_on_layer(
    w_start: Pad,
    w_end: Pad,
    p_start: Pad,
    p_end: Pad,
    foreign: list[Pad],
    layer: int,
    *,
    wrap_rules: dict,
    pad_rules: dict,
    accepted,
    on_layer,
    clearance_for,
    fixed_runs,
    neck: bool,
) -> tuple[PlannedTie, PlannedTie] | None:
    from ..diffpair_bump_gates import MIN_JOG_WIDTHS
    from ..diffpair_length_tuning import SKEW_BUMP_FOOT_CHAMFER_MM

    # Form U's four turnaround chamfers, smallest first.  The floor is the
    # smallest 45 degree corner Q32 already ships (the skew tuner's foot
    # chamfer): a hairpin between two pad rows needs its whole turnaround
    # to be a Q16 run, which leaves the chamfers only what is left over.
    hairpin_chamfers = (SKEW_BUMP_FOOT_CHAMFER_MM, 0.025, 0.05, 0.1, 0.15)

    frame = _CrossingFrame((w_start.x, w_start.y), (p_start.x, p_start.y), (w_end.x, w_end.y))
    if not frame.ok:
        return None
    w_end_l = frame.local((w_end.x, w_end.y))
    p_start_l = frame.local((p_start.x, p_start.y))
    p_end_l = frame.local((p_end.x, p_end.y))
    # Both far pads lie across from the near row.
    if w_end_l[1] >= -1e-9 or p_end_l[1] >= -1e-9 or w_end_l[0] <= 1e-9:
        return None
    pitch = p_start_l[0]
    w_ends = ((w_start.x, w_start.y), (w_end.x, w_end.y))
    p_ends = ((p_start.x, p_start.y), (p_end.x, p_end.y))

    def checker(net: int, rules: dict):
        width = rules["width"]
        clearance = rules["clearance"]

        def need(other: int) -> float:
            extra = clearance_for(other) if clearance_for is not None else 0.0
            return float(max(clearance, extra or 0.0))

        reach = width / 2.0 + max(
            [clearance]
            + ([clearance_for(p.net) or 0.0 for p in foreign] if clearance_for is not None else [])
        )
        min_width = rules.get("min_width") if neck else None
        floor = min_width if min_width is not None else width
        cache: dict[tuple[Pt, Pt], float] = {}

        def seg_room(a: Pt, b: Pt) -> float:
            key = (a, b)
            got = cache.get(key)
            if got is None:
                got = math.inf
                for pad in foreign:
                    if pad.net == net or not on_layer(pad, layer):
                        continue
                    span = max(pad.width, pad.height) + reach
                    if (
                        pad.x < min(a[0], b[0]) - span
                        or pad.x > max(a[0], b[0]) + span
                        or pad.y < min(a[1], b[1]) - span
                        or pad.y > max(a[1], b[1]) + span
                    ):
                        continue
                    required = max(
                        need(pad.net), float(getattr(pad, "authored_clearance", 0.0) or 0.0)
                    )
                    got = min(
                        got,
                        2.0 * (_tie_pad_gap((a, b), pad) - required - _CROSSING_MARGIN_MM),
                    )
                cache[key] = got
            return got

        def fit(
            local: list[Pt], others: list[PlannedTie], ends: tuple[Pt, Pt]
        ) -> PlannedTie | None:
            local = _dedupe(local)
            if len(local) < 2:
                return None
            if not _all_45(local) or not _flats_ok(local, MIN_JOG_WIDTHS * width):
                return None
            pts = [frame.world(p) for p in local]
            # The ends are the pad centres exactly.
            pts[0], pts[-1] = ends
            if not _self_clear(pts, width + clearance):
                return None
            # Cheap screen on the cached pad rooms, then the full check.
            if any(seg_room(a, b) < floor - 1e-9 for a, b in zip(pts, pts[1:], strict=False)):
                return None
            tpts = tuple((float(x), float(y)) for x, y in pts)
            w = _fit_tie_width(
                tpts,
                layer,
                net,
                foreign,
                list(accepted) + others,
                fixed_runs,
                on_layer=on_layer,
                need=need,
                reach=reach,
                width=width,
                min_width=min_width,
                blocked=rules.get("blocked"),
                margin=_CROSSING_MARGIN_MM,
                pad_floors=True,
            )
            if w is None:
                return None
            return PlannedTie(
                net=net,
                layer=layer,
                points=tpts,
                width=w,
                class_width=width,
                length=_polyline_length(tpts),
            )

        return fit

    fit_wrap = checker(w_start.net, wrap_rules)
    fit_pad = checker(p_start.net, pad_rules)

    # 1. The shortest wrap that clears.
    wraps = []
    for z in (0.0, 0.1, 0.2, 0.3):
        for s in _grid(_GRID_MM, 1.2):
            for e in (0.05, 0.1, 0.2, 0.3, 0.4, 0.5):
                for g in _grid(pitch + _GRID_MM, pitch + 1.2):
                    if g <= w_end_l[0] + 1e-9:
                        continue
                    for f in (0.0, 0.2):
                        local = _wrap_shape(z, s, e, g, f, w_end_l)
                        wraps.append((_polyline_length(local), (z, s, e, g, f)))
    wraps.sort()
    wrap = None
    wrap_params = None
    for _len, params in wraps[:_MAX_WRAP_CHECKS]:
        wrap = fit_wrap(_wrap_shape(*params, w_end_l), [], w_ends)
        if wrap is not None:
            wrap_params = params
            break
    if wrap is None or wrap_params is None:
        return None

    # 2. A padded tie of the wrap's length, solved for its free knob.
    def padded_shapes(target: float | None):
        """(order key, local points) of every padded candidate.

        ``target``: the knob (form C's ``m``, form U's turn column) is
        solved for that length; ``None``: the knob takes its shortest
        Q16-legal value.
        """
        min_flat = MIN_JOG_WIDTHS * pad_rules["width"]
        s_u, s_v = p_start_l
        e_u, e_v = p_end_l
        out = []
        for y1 in (0.0, 0.1, 0.2, 0.3):
            for k in _grid(_GRID_MM, 1.5):
                for j in (0.05, 0.1, 0.2, 0.3):
                    if target is None:
                        m = _GRID_MM
                    else:
                        # Signed leg sum: the length is linear in m (slope
                        # sqrt 2) while every leg keeps its direction.
                        top = s_v - y1 - k
                        base = y1 + k * _SQRT2 + (s_u - k - e_u - j) + j * _SQRT2 + (top - j - e_v)
                        m = (target - base) / _SQRT2
                    if m <= 1e-6:
                        continue
                    local = _c_shape(y1, k, j, m, p_start_l, p_end_l)
                    out.append(((0, m, _polyline_length(local)), local))
            # Form U on the fine grid: its two runs sit between two pad rows
            # and its turnaround is itself a Q16 run, so the run levels are
            # solved to _FINE_GRID_MM, not the coarse grid.
            for k in _grid(_FINE_GRID_MM, 1.5, _FINE_GRID_MM):
                top = s_v - y1 - k
                for d in hairpin_chamfers:
                    for m in _grid(_FINE_GRID_MM, 0.8, _FINE_GRID_MM):
                        # Q16 on the turnaround (top run to bottom run).
                        if top - (e_v + m) - 2.0 * d < min_flat - 1e-9:
                            continue
                        # The farthest turn column Q16 allows (shortest).
                        lu_max = min(
                            p_start_l[0] - k - d - min_flat,
                            p_end_l[0] - m - d - min_flat,
                        )
                        if target is None:
                            lu = lu_max
                        else:
                            # Signed leg sum: linear in lu (slope -2).
                            top = s_v - y1 - k
                            base = (
                                y1
                                + k * _SQRT2
                                + (s_u - k - d)
                                + 2.0 * d * _SQRT2
                                + (top - (e_v + m) - 2.0 * d)
                                + (e_u - m - d)
                                + m * _SQRT2
                            )
                            lu = (base - target) / 2.0
                            if lu > lu_max + 1e-9:
                                continue
                        local = _hairpin(y1, k, d, m, lu, p_start_l, p_end_l)
                        out.append(((1, -lu, _polyline_length(local)), local))
        out.sort(key=lambda item: item[0])
        return out

    target = wrap.length
    for _key, local in padded_shapes(target)[:_MAX_PAD_CHECKS]:
        padded = fit_pad(local, [wrap], p_ends)
        if padded is not None and abs(padded.length - wrap.length) <= CROSSING_TIE_MATCH_MM:
            return wrap, padded

    # 3. Nothing that short fits inside the wrap: take the shortest padded
    #    tie that does, and re-plan the wrap to its length.  Every wrap of
    #    that length that clears is scored by the pair's uncoupled figure
    #    over the two ties (:func:`_ties_uncoupled_mm`), and the best is
    #    kept: the extra length goes where the two ties run beside each
    #    other, not into copper the checker counts as uncoupled (issue
    #    #7869, AC 5).  The descent column stays no farther out than the
    #    shortest wrap's (``g <= g0``): a column moved towards the next pad
    #    out crowds that pad's own escape (on 06rf, 0.11 mm towards B5 necked
    #    USB_CC2 to 0.2 mm and failed its impedance check).
    longer = sorted(
        (item for item in padded_shapes(None) if item[0][2] > target),
        key=lambda item: item[0][2],
    )
    a_u, a_v = w_end_l
    g0 = wrap_params[3]
    for _key, local in longer[:_MAX_PAD_CHECKS]:
        padded = fit_pad(local, [wrap], p_ends)
        if padded is None:
            continue
        best: tuple[tuple, PlannedTie, PlannedTie] | None = None
        checks = 0
        for g in _grid(max(pitch + _GRID_MM, g0 - 0.1), g0, _FINE_GRID_MM):
            for s in _grid(_GRID_MM, 1.2, _FINE_GRID_MM * 2.5):
                for e in _WRAP_CHAMFERS_MM:
                    # The wrap's length is linear in its lead z (slope 2)
                    # and independent of its final lead f.
                    rest = s * _SQRT2 + e * (_SQRT2 - 2.0) - a_v + a_u + (g - a_u) * _SQRT2
                    z = (padded.length - rest) / 2.0
                    if z < -1e-9 or g - e < s + 1e-9:
                        continue
                    z = max(z, 0.0)
                    for f in _grid(0.0, 1.0, 0.1):
                        if checks >= _MAX_WRAP_CHECKS:
                            break
                        checks += 1
                        cand = fit_wrap(_wrap_shape(z, s, e, g, f, w_end_l), [padded], w_ends)
                        if cand is None or abs(cand.length - padded.length) > CROSSING_TIE_MATCH_MM:
                            continue
                        again = fit_pad(local, [cand], p_ends)
                        if again is None:
                            continue
                        score = _ties_uncoupled_mm(cand, again, foreign, on_layer, layer)
                        key = (
                            round(score, 6),
                            round(abs(cand.length - again.length), 6),
                            -g,
                            s,
                            e,
                            f,
                        )
                        if best is None or key < best[0]:
                            best = (key, cand, again)
        if best is not None:
            return best[1], best[2]
    return None
