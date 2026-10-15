"""Pad-side net-class clearance for negotiated lattice copper (issue #6395).

KiCad holds two foreign coppers to ``max(class(a), class(b))``.  The lattice
search already applies a connection's own class to its copper, and committed
copper keeps the stored side symmetrically, but a PAD only ever contributed the
board-global clearance (plus an authored ``.kicad_pro`` floor, #6243).  A
0.15 mm ``Digital`` track could therefore pass a 0.2 mm ``Power`` pad -- most
often a skipped pour net's pad, neutralised to net 0 -- at 0.175 mm.  Board 05
shipped 6 such findings in both DRC engines once #6390 declared the classes.

Why a post-negotiation repair rather than a bigger pad keep-out
---------------------------------------------------------------
Growing the search's pad masks by the pad's class was measured first (the
issue's evidence and this fix's own runs).  The shortfalls are tiny (0.01 to
0.05 mm), but the lattice resolves position in 0.1/0.2 mm rows, so a 0.025 mm
requirement moves a track by a whole row.  On board 05 that reshuffled U2's
west pin field, where negotiation already needed 9 of 12 passes, and stranded
PHASE_A (32/33) at 1.5x the run time.  On board 04 it closed the only egress row
of an LQFP pin next to a GND pin, though a track on that row clears it at
exactly 0.200 mm.  More passes, failed-first ordering and class-priority
ordering did not recover either.

The same shortfalls are cheap to remove geometrically: every one sits on a
track with slack on its other side.  So the search is left byte-identical and
this pass, run once on the best negotiated pass, moves the offending segment
away from the pad by the deficit.  A move is kept only when every segment it
touches is clear of every foreign pad (pair rule), of all committed and fixed
copper and fills (the search's own predicate), of Edge.Cuts and of keepout rule
areas.  Pads are judged by the exact clearance kernel, which is what both DRC
engines measure.  The move is first tried against the router's pre-save check
as well, which models a round pad as its full square, so that the self-check
agrees with DRC whenever a move allows it.  A shortfall it cannot clear is left
in place and counted, and the pre-save clearance check still reports it.

Scope: single-ended connections (``repairable``; coupled and uncoupled-fallback
pair legs are not passed in, so they never move).  Copper of a diff-pair NET
that is repairable -- the single-ended stubs to a pair net's extra pads, or a
declined pair routed entirely on the ordinary star -- is held to a length
budget (issue #6437): a move is kept only while the PAIR's skew -- the
difference of the two nets' accumulated length changes -- stays within
:data:`PAIR_LENGTH_TOLERANCE_MM` (board 03 USB_D: +0.009 mm).  Budgeting each
net alone would let opposite-sign moves on both nets add up to twice that.  Not
moving it is worse: the router's post-route backstop then demotes the whole net
for the shortfall.  Pair-net shortfalls left in place (over budget, or on a
non-repairable leg) are counted as ``pair_skipped``.  Via-vs-pad
shortfalls are reported (``via_found`` / ``via_remaining``) but never repaired:
moving a via moves copper on every layer plus all its incident segments.  A
board with an HV pairwise projection (``--voltage-map``) is skipped, since its
attach-zone waivers are not modelled here; the skip is reported as
``skipped == "voltage-map"`` so the router can log it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..authored_clearance import pad_authored_floor
from ..clearance_shapes import copper_gap, pad_shape, segment_shape, via_shape
from ..primitives import Pad, Route, Segment, pad_half_extents

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .pathfinder import LatticePathfinder

__all__ = [
    "PAIR_LENGTH_TOLERANCE_MM",
    "ClassPadRepairStats",
    "pad_class_requirements",
    "repair_class_pad_pairs",
]

#: Extra gap past the requirement for a moved segment, mm.  Absorbs the 4-decimal
#: rounding of the KiCad writer (5e-5) with a wide safety factor.
_MARGIN = 0.002
#: A gap within this of the requirement counts as met (the kernel's own scale).
_EPS = 1e-6
#: Vertex identity tolerance, mm (lattice copper shares exact float endpoints).
_KEY_DIGITS = 6

_Vertex = tuple[int, float, float]  # (layer index, x, y), rounded

#: Largest total routed-length change a repair may make to one diff-pair net,
#: mm (issue #6437).  This is epic #6441 P5's skew-regression threshold
#: (#6447: "cannot change the skew of a pair net by more than 0.01 mm").  It
#: bounds the pair's skew change (``|drift[P] - drift[N]|``), not each net's
#: own drift: opposite-sign moves on the two nets would otherwise double it.
PAIR_LENGTH_TOLERANCE_MM = 0.01


@dataclass
class ClassPadRepairStats:
    """Outcome of :func:`repair_class_pad_pairs` (all counts are track/pad pairs)."""

    found: int = 0
    repaired: int = 0
    remaining: int = 0
    #: Shortfalls on diff-pair net copper left in place: on a pair leg, or a
    #: move refused by the length budget (issue #6437).  Refused movable ones
    #: also count in ``remaining``.
    pair_skipped: int = 0
    #: Via-vs-pad shortfalls found / left (vias are never moved, so equal).
    via_found: int = 0
    via_remaining: int = 0
    #: Why the repair did not run at all (``"voltage-map"``), else ``None``.
    skipped: str | None = None
    #: Issue #6489: pads still short of their class after the repair, over EVERY
    #: route (pair legs and unmovable copper included), as pad index -> the
    #: clearance that pad's pairs require.  The lattice grows exactly these
    #: pads' keep-out and negotiates again; empty means nothing is left.
    unrepaired_pads: dict[int, float] = field(default_factory=dict)
    #: Track-vs-pad shortfalls left over every route (the figure the
    #: re-negotiation must reduce to be kept).
    total_remaining: int = 0


@dataclass
class _Shortfall:
    key: object
    seg_index: int
    pad_index: int
    required: float
    gap: float


def _vkey(layer: int, x: float, y: float) -> _Vertex:
    return (layer, round(x, _KEY_DIGITS), round(y, _KEY_DIGITS))


def _seg_len(seg: Segment) -> float:
    return math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)


def _point_in_pad(x: float, y: float, pad: Pad) -> bool:
    hw, hh = pad_half_extents(pad)
    return abs(x - pad.x) <= hw + _EPS and abs(y - pad.y) <= hh + _EPS


def pad_class_requirements(pf: LatticePathfinder) -> list[float] | None:
    """Each pad's own clearance requirement, aligned with ``pf.pads``.

    ``max(pad class, authored floor, board base)``: the pad side of every
    track-vs-pad pair the repair enforces.  ``None`` when the router installed
    no pad classes (:meth:`LatticePathfinder.set_pad_class_clearances`).
    """
    classes = pf.pad_class_clearances
    if classes is None:
        return None
    floors = pf.rules.net_clearance_floors
    base = pf.rules.trace_clearance
    return [max(classes[i], pad_authored_floor(p, floors), base) for i, p in enumerate(pf.pads)]


def repair_class_pad_pairs(
    pf: LatticePathfinder,
    routes: dict[object, Route],
    net_classes: Mapping[object, object | None],
    *,
    repairable: set[object] | None = None,
    pair_nets: Collection[int] | Mapping[int, int] = frozenset(),
    pair_length_tolerance: float = PAIR_LENGTH_TOLERANCE_MM,
    max_passes: int = 4,
) -> ClassPadRepairStats:
    """Clear track-vs-pad shortfalls of the pad's own class (in place).

    Args:
        pf: the pathfinder that negotiated ``routes`` (its rules, layer stack,
            pads, per-pad class clearances, fixed copper, fills, Edge.Cuts and
            keepouts define legality).
        routes: the negotiated routes, keyed by connection key; segments are
            mutated in place when a repair is kept.
        net_classes: connection key -> net class (``None`` = board default),
            for every key in ``routes``.
        repairable: keys whose copper may move (default: all of ``routes``).
        pair_nets: diff-pair nets (issue #6437), as a mapping of net id to its
            partner's net id (a bare collection of ids budgets each net alone).
            Their repairable copper may move only while the pair's skew change
            stays within ``pair_length_tolerance``; shortfalls left on them are
            counted in ``pair_skipped``.
        pair_length_tolerance: the per-pair skew budget, mm.
        max_passes: repair sweeps; each re-detects shortfalls on the moved copper.
    """
    stats = ClassPadRepairStats()
    classes = pf.pad_class_clearances
    if classes is None or not routes:
        return stats
    if pf._pairwise is not None:
        stats.skipped = "voltage-map"
        return stats
    pads: Sequence[Pad] = pf.pads
    base = pf.rules.trace_clearance
    pad_req = pad_class_requirements(pf)
    assert pad_req is not None  # ``classes`` was checked above
    if not any(r > base + _EPS for r in pad_req):
        return stats
    pair_set = frozenset(pair_nets)
    partners: Mapping[int, int] = pair_nets if isinstance(pair_nets, Mapping) else {}
    movable = set(routes) if repairable is None else set(repairable) & set(routes)
    # Issue #6437: running length change of each pair net; the budget bounds
    # the difference between a net's drift and its partner's.
    pair_drift: dict[int, float] = defaultdict(float)
    pad_layers = [set(pf._pad_layer_indices(p)) for p in pads]
    layer_index: dict[Any, int | None] = {}

    def lidx(layer: Any) -> int | None:
        if layer not in layer_index:
            try:
                layer_index[layer] = pf.layer_stack.layer_enum_to_index(layer)
            except Exception:
                layer_index[layer] = None
        return layer_index[layer]

    own_clr = {key: pf._conn_geometry(net_classes.get(key), routes[key].net)[1] for key in routes}

    def pads_near(seg: Segment, reach: float) -> list[int]:
        x0, x1 = min(seg.x1, seg.x2) - reach, max(seg.x1, seg.x2) + reach
        y0, y1 = min(seg.y1, seg.y2) - reach, max(seg.y1, seg.y2) + reach
        out = []
        for i, p in enumerate(pads):
            hw, hh = pad_half_extents(p)
            if p.x + hw < x0 or p.x - hw > x1 or p.y + hh < y0 or p.y - hh > y1:
                continue
            out.append(i)
        return out

    reach_max = max(pad_req) + 0.5

    def pad_shortfalls(key: object, seg: Segment, *, class_only: bool) -> list:
        """Foreign pads ``seg`` is too close to: ``(pad, required, gap)``."""
        layer = lidx(seg.layer)
        if layer is None:
            return []
        net = routes[key].net
        own = own_clr[key]
        found = []
        shape = segment_shape(seg)
        for i in pads_near(seg, seg.width / 2.0 + reach_max):
            pad = pads[i]
            if pad.net == net or layer not in pad_layers[i]:
                continue
            required = max(own, pad_req[i])
            if class_only and pad_req[i] <= own + _EPS:
                continue  # the search already held this pair
            # The exact clearance kernel: what both DRC engines measure and,
            # since issue #6718, what ``validate_routes`` measures too.
            gap = copper_gap(shape, pad_shape(pad, roundrect_as_rect=True))
            if gap < required - _EPS:
                found.append((i, required, gap))
        return found

    def build_committed() -> Any:
        committed = pf._fresh_committed()
        for key, route in routes.items():
            clr = own_clr[key]
            for seg in route.segments:
                layer = lidx(seg.layer)
                if layer is None:
                    continue
                committed.add_run(
                    layer, [(seg.x1, seg.y1), (seg.x2, seg.y2)], route.net, seg.width / 2.0, clr
                )
            for via in route.vias:
                committed.add_via((via.x, via.y), route.net, clr, radius=via.diameter / 2.0)
        return committed

    def detect() -> list[_Shortfall]:
        out = []
        for key in movable:
            for idx, seg in enumerate(routes[key].segments):
                for i, required, gap in pad_shortfalls(key, seg, class_only=True):
                    out.append(_Shortfall(key, idx, i, required, gap))
        out.sort(key=lambda s: s.gap - s.required)
        return out

    def pair_left() -> int:
        """Shortfalls still on diff-pair net copper (never hidden, #6437)."""
        return sum(
            len(pad_shortfalls(key, seg, class_only=True))
            for key, route in routes.items()
            if route.net in pair_set
            for seg in route.segments
        )

    def record_left() -> None:
        """Issue #6489: shortfalls left over every route, and their pads."""
        left: dict[int, float] = {}
        total = 0
        for key, route in routes.items():
            for seg in route.segments:
                for i, required, _gap in pad_shortfalls(key, seg, class_only=True):
                    total += 1
                    left[i] = max(left.get(i, 0.0), required)
        stats.unrepaired_pads = left
        stats.total_remaining = total

    # Via-vs-pad shortfalls: reported only (a via move drags every layer).
    n_layers = pf.layer_stack.num_layers
    for key, route in routes.items():
        own = own_clr[key]
        for via in route.vias:
            drilled = getattr(via, "drilled_layers", via.layers)
            la, lb = lidx(drilled[0]), lidx(drilled[1])
            if la is None or lb is None:
                span = set(range(n_layers))
            else:
                span = set(range(min(la, lb), max(la, lb) + 1))
            shape = via_shape(via)
            r = via.diameter / 2.0 + reach_max
            for i, pad in enumerate(pads):
                if pad.net == route.net or pad_req[i] <= own + _EPS:
                    continue
                if not (span & pad_layers[i]):
                    continue
                hw, hh = pad_half_extents(pad)
                if abs(pad.x - via.x) > hw + r or abs(pad.y - via.y) > hh + r:
                    continue
                if (
                    copper_gap(shape, pad_shape(pad, roundrect_as_rect=True))
                    < max(own, pad_req[i]) - _EPS
                ):
                    stats.via_found += 1
    stats.via_remaining = stats.via_found

    candidates = detect()
    stats.found = len(candidates)
    if not candidates:
        stats.pair_skipped = pair_left()
        record_left()
        return stats

    committed = None
    for _ in range(max_passes):
        if not candidates:
            break
        moved_any = False
        for sf in candidates:
            seg = routes[sf.key].segments[sf.seg_index]
            # A shortfall may already be gone (an earlier move).
            still = [
                s for s in pad_shortfalls(sf.key, seg, class_only=True) if s[0] == sf.pad_index
            ]
            if not still:
                continue
            if committed is None:
                committed = build_committed()

            def pads_ok(key: object, new: Segment) -> bool:
                return not pad_shortfalls(key, new, class_only=False)

            budget = (
                (pair_drift, partners, pair_length_tolerance)
                if routes[sf.key].net in pair_set
                else None
            )
            if _try_move(
                pf, routes, movable, sf, still[0], own_clr, pads_ok, committed, lidx, budget
            ):
                committed = None  # geometry changed: rebuild before the next check
                moved_any = True
        candidates = detect()
        if not moved_any:
            break
    stats.remaining = len(candidates)
    stats.repaired = max(0, stats.found - stats.remaining)
    stats.pair_skipped = pair_left()
    record_left()
    return stats


def _try_move(
    pf: LatticePathfinder,
    routes: dict[object, Route],
    movable: set[object],
    sf: _Shortfall,
    shortfall: tuple[int, float, float],
    own_clr: dict[object, float],
    pads_ok: Any,
    committed: Any,
    lidx: Any,
    budget: tuple[dict[int, float], Mapping[int, int], float] | None = None,
) -> bool:
    """Translate (or swing) the offending segment off the pad; keep if clean.

    ``budget`` (diff-pair nets, issue #6437) is ``(drift, partners, tolerance)``:
    the move is refused when it would take the pair's skew change
    ``|drift[net] - drift[partner]|`` beyond ``tolerance`` (a net with no known
    partner is held to ``|drift[net]|``); a kept move updates ``drift``.
    """
    route = routes[sf.key]
    seg = route.segments[sf.seg_index]
    layer = lidx(seg.layer)
    if layer is None:
        return False
    pad = pf.pads[sf.pad_index]
    _i, required, gap = shortfall
    dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return False
    # Unit normal pointing from the pad toward the segment's closest point.
    t = max(0.0, min(1.0, ((pad.x - seg.x1) * dx + (pad.y - seg.y1) * dy) / (length * length)))
    cx, cy = seg.x1 + t * dx, seg.y1 + t * dy
    nx, ny = -dy / length, dx / length
    if (cx - pad.x) * nx + (cy - pad.y) * ny < 0:
        nx, ny = -nx, -ny
    step = required - gap + _MARGIN

    # Every same-net segment (any connection of this net) sharing a vertex.
    net = route.net
    incident: dict[_Vertex, list[tuple[object, int, int]]] = defaultdict(list)
    for key, other in routes.items():
        if other.net != net:
            continue
        for idx, s in enumerate(other.segments):
            sl = lidx(s.layer)
            if sl is None:
                continue
            incident[_vkey(sl, s.x1, s.y1)].append((key, idx, 0))
            incident[_vkey(sl, s.x2, s.y2)].append((key, idx, 1))
    via_points = {
        (round(v.x, _KEY_DIGITS), round(v.y, _KEY_DIGITS))
        for key, other in routes.items()
        if other.net == net
        for v in other.vias
    }

    def pinned(v: _Vertex) -> bool:
        lay, x, y = v
        refs = incident.get(v, [])
        if len(refs) < 2 or any(k not in movable for k, _i, _e in refs):
            return True  # a terminal end, or shared with copper we may not move
        if (x, y) in via_points:
            return True
        return any(
            p.net == net and lay in set(pf._pad_layer_indices(p)) and _point_in_pad(x, y, p)
            for p in pf.pads
        )

    va = _vkey(layer, seg.x1, seg.y1)
    vb = _vkey(layer, seg.x2, seg.y2)
    free_a, free_b = not pinned(va), not pinned(vb)
    attempts: list[dict[_Vertex, tuple[float, float]]] = []
    for k in (1.0, 2.0):
        d = step * k
        if free_a and free_b:
            attempts.append({va: (nx * d, ny * d), vb: (nx * d, ny * d)})
        # Swing about a pinned end: the far end moves enough to clear the
        # closest point (``t`` measured from the pinned end).
        if free_b and not free_a and t > 0.2:
            attempts.append({vb: (nx * d / t, ny * d / t)})
        if free_a and not free_b and t < 0.8:
            attempts.append({va: (nx * d / (1.0 - t), ny * d / (1.0 - t))})

    boundary = pf._escape_boundary
    keepouts = pf._keepouts
    for moves in attempts:
        touched: dict[tuple[object, int], Segment] = {}
        for v, (mx, my) in moves.items():
            for key, idx, end in incident[v]:
                cur = touched.get((key, idx)) or routes[key].segments[idx]
                new = Segment(
                    cur.x1, cur.y1, cur.x2, cur.y2, cur.width, cur.layer, cur.net, cur.net_name
                )
                if end == 0:
                    new.x1, new.y1 = cur.x1 + mx, cur.y1 + my
                else:
                    new.x2, new.y2 = cur.x2 + mx, cur.y2 + my
                touched[(key, idx)] = new
        ok = True
        for (key, idx), new in touched.items():
            half = new.width / 2.0
            a, b = (new.x1, new.y1), (new.x2, new.y2)
            nl = lidx(new.layer)
            if (
                nl is None
                or not pads_ok(key, new)
                or not committed.seg_clear(a, b, nl, routes[key].net, half, own_clr[key])
                or not boundary.segment_clear(a, b, half)
                or (
                    keepouts is not None
                    and keepouts.segment_blocked(a, b, nl, routes[key].net, half)
                )
            ):
                ok = False
                break
        if not ok:
            continue
        if budget is not None:
            drift, partners, tolerance = budget
            delta = sum(
                _seg_len(new) - _seg_len(routes[key].segments[idx])
                for (key, idx), new in touched.items()
            )
            partner_drift = drift[partners[net]] if net in partners else 0.0
            if abs(drift[net] + delta - partner_drift) > tolerance + _EPS:
                continue  # would move the pair's skew beyond P5's threshold
            drift[net] += delta
        for (key, idx), new in touched.items():
            old = routes[key].segments[idx]
            old.x1, old.y1, old.x2, old.y2 = new.x1, new.y1, new.x2, new.y2
        return True
    return False
