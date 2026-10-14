"""Differential-pair uncoupled-length and via-count DRC rule.

Issue #6443 (piece (b) of epic #6441 phase P1).  For an *engaged* pair the
rule reports

* the **uncoupled length** of each net -- copper that runs beside no
  partner copper -- and compares the larger of the two (the figure KiCad's
  ``diff_pair_uncoupled`` checks) against the pair's limit, and
* the **via count** of each net against ``max_vias_per_net``, plus any
  difference between the P and N via counts.

How KiCad measures (pinned by fixture, kicad-cli 10.0.6)
========================================================

The ``.kicad_dru`` constraint ``diff_pair_uncoupled (max X)`` is evaluated
per pair, and the figure compared with ``X`` is, measured on synthetic
two-net boards (``tests/test_diffpair_uncoupled_length_6443.py``):

* **Per net, not summed.**  ``uncoupled(net) = routed length of the net's
  tracks - the part of them that is coupled``; the pair's figure is
  ``max(uncoupled(P), uncoupled(N))``.  A 20 mm P net with a 12 mm
  coupled N net reports 8 mm; the harness's older ``P + N`` figure would
  say 8 mm too, but P 20 / N 16 with 12 mm coupled reports 8 mm (the
  larger side), where the sum is 12 mm.
* **Coupled means parallel, pair by pair.**  A P segment and a same-layer N
  segment couple over the overlap of their projections when their lines do
  not meet within KiCad's 32-bit coordinate range (anti-parallel counts; 20 mm
  traces 0.4 mm apart still couple at 0.005 degrees, not at 0.02).  Every such
  pair counts unless other copper stands between the two traces on the probe
  KiCad runs between the overlaps' nearest points (:func:`_blocked`).  There
  is **no distance limit** unless a ``diff_pair_gap (max ..)`` constraint is
  present, and kct emits only ``(min ..)``.  Two parallel traces 10 mm apart
  are coupled.
* **Via barrels are not counted.**  A via is not track length: a pair whose
  vias sit side by side loses nothing, and offset vias cost only the
  track copper that no longer has a partner beside it.

:func:`measure_uncoupled` reproduces exactly that, so kct and kicad-cli agree
on pass/fail per pair (to 1 um on every committed pair, board 06's regression
fixture and the pinned probes, Issue #6554); the known divergences (arcs are
not measured, pads are modelled as rectangles, and KiCad's number is known to
misreport on meanders) are listed in
``docs/guides/diff-pairs/08-uncoupled-length.md``.

The limit
=========

``max_uncoupled_total_mm`` of the net class when set (explicit, ``error``).
Otherwise the protocol's per-terminal allowance (6.35 mm for USB2, TI
SPRAAR7J §3.3) times the number of components with a pad on the net: every
component along the net is another terminal at which the pair may
legitimately leave its partner.  A limit that is an ``uncited`` table value
is a ``warning``; the severity is resolved with the limit by
:func:`~kicad_tools.validate.diffpair_engagement.resolve_pair_thresholds`.

Graceful degradation matches the sibling pair rules: no engaged pairs, no
findings.  A pair with an unrouted half (P0's "coupled outcome without
copper") is skipped: it has no uncoupled length, only no copper.

The per-terminal figure (kct-only, Issue #6615)
===============================================

Besides KiCad's total, the rule reports the figure epic #6441 / #6443
designed: for every **terminal** of a net (a footprint with a pad on it), the
uncoupled copper that lies **outside every terminal's gather-ignore region**
and is charged to that terminal, against ``max_uncoupled_per_terminal_mm``.
KiCad has no equivalent, so the finding says it is kct-only.

* **Gather-ignore region** (:func:`net_terminals`).  The footprint's courtyard
  (``F.CrtYd`` and ``B.CrtYd``, unioned; every copper layer), read through
  :mod:`kicad_tools.geometry.courtyard`.  ``NetClassRouting.gather_ignore``
  selects it: ``"courtyard"`` (the default) is the courtyard as drawn,
  a number ``r`` is the courtyard grown by ``r`` mm, and ``"none"`` ignores
  nothing.  The default radius is **0 mm**: #6443 / #6441 name "courtyard plus
  a radius" without settling a value, and the protocol allowance itself
  (TI SPRAAR7J §3.3, "breakout within 0.25 in of the package") already
  measures from the package, so any non-zero default would hide copper the
  source counts.
* **No courtyard** (none drawn, or not resolvable to a closed polygon): the
  bounding box of the footprint's pads (each pad as its circumscribed
  circle) grown by :data:`PAD_BBOX_FALLBACK_MARGIN_MM`, the IPC-7351 nominal
  courtyard excess.  The finding names the fallback.
* **Attribution** (:func:`_attribute`).  The net's tracks form a graph
  (shared endpoints, T-junctions, vias).  Every endpoint inside a terminal's
  region or on one of its pads is that terminal's source; a multi-source
  shortest-path walk gives every point of copper its nearest terminal *along
  the copper*, and each segment is split where the two ends' terminals meet.
  So a two-terminal net checks each end independently.  Copper not connected
  to any terminal is charged to the terminal nearest in a straight line.
* A net with no terminal (no pads on the board) has no per-terminal figure.
  Arcs are not measured, as for the total.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_engagement import PairThresholds

#: KiCad stores board coordinates as integer nanometres.  The pairing below
#: repeats its integer projections, whose rounding decides at which end of a
#: diagonal overlap the blocking probe runs (Issue #6554).
_NM_PER_MM = 1_000_000
#: KiCad's coordinate type is a 32-bit int: two lines whose intersection lies
#: outside it count as parallel (so 0.005 degrees can still couple, 0.02 not).
_COORD_MIN, _COORD_MAX = -(2**31), 2**31 - 1

#: Slack (mm) on distance comparisons that KiCad makes in integer nanometres.
_TOUCH_EPS = 1e-9

#: Float-comparison slack (mm) on a limit, as the sibling skew rule has.
UNCOUPLED_EPSILON_MM = 1e-6

#: An integer-nanometre point.
IPoint = tuple[int, int]
#: One coupled primitive pair: indices into the P and N lists, the ends of the
#: overlap on P (``pa``, ``pb``) and their projections on N (``na``, ``nb``).
Pairing = tuple[int, int, IPoint, IPoint, IPoint, IPoint]

# Phrases that mark a via-count finding.  Both via sites build their message
# from these and :func:`is_via_count_finding` matches on them, so the
# ``kct check`` summary can label via counts ``vias`` (Issue #6576).
VIA_OVER_LIMIT_PHRASE = "vias, limit"
VIA_UNEQUAL_PHRASE = "unequal via counts"
# Phrase that marks a per-terminal finding (Issue #6615).
PER_TERMINAL_PHRASE = "per-terminal uncoupled length"
#: The note every per-terminal finding carries.
KCT_ONLY_NOTE = "kct-only: KiCad has no per-terminal uncoupled check"

#: ``NetClassRouting.gather_ignore`` default: the courtyard as drawn (0 mm radius).
GATHER_IGNORE_DEFAULT = "courtyard"
#: Margin (mm) around the pads' bounding box for a footprint without a usable
#: courtyard: the IPC-7351 nominal courtyard excess.
PAD_BBOX_FALLBACK_MARGIN_MM = 0.25
#: Snap tolerance (mm) for joining track endpoints into one graph node.
_NODE_TOL_MM = 1e-3


def is_via_count_finding(v: DRCViolation) -> bool:
    """True for a via-count finding (its measured/limit values are via counts)."""
    return v.rule_id == "diffpair_uncoupled_length" and (
        VIA_OVER_LIMIT_PHRASE in v.message or VIA_UNEQUAL_PHRASE in v.message
    )


def is_per_terminal_finding(v: DRCViolation) -> bool:
    """True for a per-terminal uncoupled-length finding (Issue #6615)."""
    return v.rule_id == "diffpair_uncoupled_length" and PER_TERMINAL_PHRASE in v.message


#: ``(x1, y1, x2, y2, layer)``, optionally followed by the track width (needed
#: only to decide whether other copper stands between two traces).
Segment = tuple[float, float, float, float, str] | tuple[float, float, float, float, str, float]
#: ``(x1, y1, x2, y2, width, layer or None)``: a track (layer set) or a via (``None``:
#: all layers, a zero-length segment) that may stand between a pair's traces.
Obstacle = tuple[float, float, float, float, float, str | None]


class PadRect(NamedTuple):
    """A pad as a rotated rectangle in board coordinates."""

    cx: float
    cy: float
    hx: float  # half extents along the pad's own axes
    hy: float
    cos: float
    sin: float
    layers: frozenset[str]
    net: str


@dataclass(frozen=True)
class Copper:
    """The board copper a pair's traces may be separated by (see :func:`measure_uncoupled`)."""

    tracks: Sequence[Obstacle] = ()
    pads: Sequence[PadRect] = ()


#: ``(footprint reference, uncoupled mm charged to it, region source)``.
TerminalFigure = tuple[str, float, str]


@dataclass(frozen=True)
class PairMeasure:
    """What :func:`measure_pair` found on one pair."""

    p_name: str
    n_name: str
    p_routed_mm: float
    n_routed_mm: float
    p_uncoupled_mm: float
    n_uncoupled_mm: float
    vias_p: int
    vias_n: int
    components_p: int
    components_n: int
    #: Per-terminal figures of each net (Issue #6615), sorted by reference.
    #: Empty when the net has no terminal on the board.
    p_terminals: tuple[TerminalFigure, ...] = ()
    n_terminals: tuple[TerminalFigure, ...] = ()

    @property
    def uncoupled_mm(self) -> float:
        """The KiCad figure: the larger of the two nets' uncoupled lengths."""
        return max(self.p_uncoupled_mm, self.n_uncoupled_mm)

    @property
    def per_terminal_mm(self) -> float | None:
        """The largest per-terminal figure of either net, ``None`` without terminals."""
        figures = [mm for _, mm, _ in (*self.p_terminals, *self.n_terminals)]
        return max(figures) if figures else None

    @property
    def components(self) -> int:
        """Components on the pair's nets (the larger side, normally equal)."""
        return max(self.components_p, self.components_n)

    @property
    def routed(self) -> bool:
        """Both halves carry copper."""
        return self.p_routed_mm > 0.0 and self.n_routed_mm > 0.0


def _length(seg: Segment) -> float:
    return math.hypot(seg[2] - seg[0], seg[3] - seg[1])


def _seg_dist(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> float:
    """Minimum distance between segments ``p1-p2`` and ``q1-q2`` (either may be a point)."""

    def point_seg(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        dx, dy = b[0] - a[0], b[1] - a[1]
        d2 = dx * dx + dy * dy
        t = 0.0 if d2 <= 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d2))
        return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

    def orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(p1, p2, q1), orient(p1, p2, q2)
    d3, d4 = orient(q1, q2, p1), orient(q1, q2, p2)
    if d1 * d2 < 0.0 and d3 * d4 < 0.0:
        return 0.0  # proper crossing
    return min(
        point_seg(p1, q1, q2),
        point_seg(p2, q1, q2),
        point_seg(q1, p1, p2),
        point_seg(q2, p1, p2),
    )


def _width(seg: Segment) -> float:
    return float(seg[5]) if len(seg) > 5 else 0.0


def _pad_local(pad: PadRect, x: float, y: float) -> tuple[float, float]:
    """``(x, y)`` in the pad's own axes (KiCad's negated-angle convention)."""
    dx, dy = x - pad.cx, y - pad.cy
    return dx * pad.cos - dy * pad.sin, dx * pad.sin + dy * pad.cos


def _pad_on_layer(pad: PadRect, layer: str) -> bool:
    return layer in pad.layers or "*.Cu" in pad.layers


def _pad_seg_dist(pad: PadRect, p: tuple[float, float], q: tuple[float, float]) -> float:
    """Distance from segment ``p-q`` to the pad rectangle (0 when they intersect)."""
    x1, y1 = _pad_local(pad, *p)
    x2, y2 = _pad_local(pad, *q)
    hx, hy = pad.hx, pad.hy
    # Liang-Barsky clip of the segment against the box.
    t0, t1 = 0.0, 1.0
    dx, dy = x2 - x1, y2 - y1
    for pk, qk in ((-dx, x1 + hx), (dx, hx - x1), (-dy, y1 + hy), (dy, hy - y1)):
        if pk == 0.0:
            if qk < 0.0:
                break
        else:
            r = qk / pk
            if pk < 0.0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                break
    else:
        return 0.0

    def to_box(x: float, y: float) -> float:
        return math.hypot(max(abs(x) - hx, 0.0), max(abs(y) - hy, 0.0))

    corners = [(sx * hx, sy * hy) for sx in (-1, 1) for sy in (-1, 1)]
    return min(
        to_box(x1, y1),
        to_box(x2, y2),
        *(_seg_dist((x1, y1), (x2, y2), c, c) for c in corners),
    )


def _nm(x: float, y: float) -> IPoint:
    return (round(x * _NM_PER_MM), round(y * _NM_PER_MM))


def _mm(p: IPoint) -> tuple[float, float]:
    return (p[0] / _NM_PER_MM, p[1] / _NM_PER_MM)


def _rescale(num: int, val: int, den: int) -> int:
    """``num * val / den`` rounded half away from zero, in C++ integer arithmetic."""
    prod = num * val
    half = abs(den) // 2 if den > 0 else -(abs(den) // 2)
    q = prod - half if (prod < 0) != (den < 0) else prod + half
    r = abs(q) // abs(den)
    return r if (q >= 0) == (den > 0) else -r


def _cross(a: IPoint, b: IPoint) -> int:
    return a[0] * b[1] - a[1] * b[0]


def _dot(a: IPoint, b: IPoint) -> int:
    return a[0] * b[0] + a[1] * b[1]


def _sub(a: IPoint, b: IPoint) -> IPoint:
    return (a[0] - b[0], a[1] - b[1])


def _project(a: IPoint, b: IPoint, p: IPoint, clamp: bool) -> IPoint:
    """``p`` projected on the line ``a-b`` (onto the segment when ``clamp``), in nm."""
    d = _sub(b, a)
    l2 = _dot(d, d)
    if l2 == 0:
        return a
    t = _dot(d, _sub(p, a))
    if clamp and t < 0:
        return a
    if clamp and t > l2:
        return b
    return (a[0] + _rescale(t, d[0], l2), a[1] + _rescale(t, d[1], l2))


def _parallel(p0: IPoint, p1: IPoint, n0: IPoint, n1: IPoint) -> bool:
    """KiCad's test for a P/N primitive pair: the two *lines* do not meet.

    Exactly parallel, not collinear, lines never meet; otherwise they meet
    unless the intersection lies outside KiCad's 32-bit coordinate range.
    """
    d1, d2, off = _sub(p1, p0), _sub(n1, n0), _sub(n0, p0)
    det = _cross(d2, d1)
    q = _cross(d1, off)
    if det == 0:
        return q != 0
    x = n0[0] + _rescale(q, d2[0], det)
    y = n0[1] + _rescale(q, d2[1], det)
    return not (_COORD_MIN <= x <= _COORD_MAX and _COORD_MIN <= y <= _COORD_MAX)


def _overlap(
    p0: IPoint, p1: IPoint, n0: IPoint, n1: IPoint
) -> tuple[IPoint, IPoint, IPoint, IPoint] | None:
    """The part of P beside N and its projection on N, or ``None`` without overlap."""
    d = _sub(p1, p0)
    l2 = _dot(d, d)
    ta = _dot(d, _sub(_project(p0, p1, n0, False), p0))
    tb = _dot(d, _sub(_project(p0, p1, n1, False), p0))
    if l2 <= min(ta, tb) or max(ta, tb) <= 0:
        return None
    t = sorted((0, l2, ta, tb))
    pa = (p0[0] + _rescale(d[0], t[1], l2), p0[1] + _rescale(d[1], t[1], l2))
    pb = (p0[0] + _rescale(d[0], t[2], l2), p0[1] + _rescale(d[1], t[2], l2))
    return pa, pb, _project(n0, n1, pa, False), _project(n0, n1, pb, False)


def _probe(pa: IPoint, pb: IPoint, na: IPoint, nb: IPoint) -> tuple[IPoint, IPoint]:
    """The nearest points of the two overlaps: the first strictly closest candidate.

    For exactly parallel overlaps every candidate is equally close up to the
    1 nm rounding of the projections, which is what picks the end.
    """
    cand = (
        (pa, _project(na, nb, pa, True)),
        (pb, _project(na, nb, pb, True)),
        (_project(pa, pb, na, True), na),
        (_project(pa, pb, nb, True), nb),
    )
    best = cand[0]
    for c in cand[1:]:
        if _dot(_sub(c[0], c[1]), _sub(c[0], c[1])) < _dot(
            _sub(best[0], best[1]), _sub(best[0], best[1])
        ):
            best = c
    return best


def _pad_contains(pad: PadRect, p: tuple[float, float]) -> bool:
    """KiCad's ray-cast point-in-pad test, in nm: a point on an edge may be in or out."""
    pts: list[IPoint] = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        lx, ly = sx * pad.hx, sy * pad.hy
        pts.append(_nm(pad.cx + lx * pad.cos + ly * pad.sin, pad.cy - lx * pad.sin + ly * pad.cos))
    x, y = _nm(*p)
    inside = False
    for i, p1 in enumerate(pts):
        p2 = pts[(i + 1) % len(pts)]
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        if dy == 0:
            continue
        if (p1[1] >= y) != (p2[1] >= y) and x - p1[0] < _rescale(dx, y - p1[1], dy):
            inside = not inside
    return inside


def _exits(pad: PadRect, seg: Segment) -> bool:
    """True when exactly one end of ``seg`` lies in ``pad``."""
    return _pad_contains(pad, (seg[0], seg[1])) != _pad_contains(pad, (seg[2], seg[3]))


def _blocked(
    p: Segment, n: Segment, ends: tuple[IPoint, IPoint, IPoint, IPoint], copper: Copper
) -> bool:
    """True when other copper stands between a coupled P/N primitive pair.

    The probe runs between the nearest points of the two overlaps (see
    :func:`_probe`).  Any track (of any net, the pair's own included), via or
    pad on the layer that reaches it blocks the pairing, except the two
    traces themselves, a track or via that touches an end of either overlap
    (it is connected to them), and a pad that either trace leaves (one end in
    the pad, the other out).  A trace lying inside its own pad is therefore not
    paired with a partner outside it: the pad stands between them.
    """
    q_p, q_n = _probe(*ends)
    q1, q2 = _mm(q_n), _mm(q_p)
    corners = [_mm(e) for e in ends]
    layer = p[4]
    minx, maxx = min(q1[0], q2[0]), max(q1[0], q2[0])
    miny, maxy = min(q1[1], q2[1]), max(q1[1], q2[1])
    own = {(p[0], p[1], p[2], p[3]), (n[0], n[1], n[2], n[3])}
    for ob in copper.tracks:
        if ob[5] is not None and (ob[5] != layer or (ob[0], ob[1], ob[2], ob[3]) in own):
            continue
        r = ob[4] / 2.0
        if (
            max(ob[0], ob[2]) + r < minx
            or min(ob[0], ob[2]) - r > maxx
            or max(ob[1], ob[3]) + r < miny
            or min(ob[1], ob[3]) - r > maxy
        ):
            continue
        c1, c2 = (ob[0], ob[1]), (ob[2], ob[3])
        dist = _seg_dist(c1, c2, q1, q2)
        if dist > _TOUCH_EPS and dist >= r - _TOUCH_EPS:
            continue
        if any(_seg_dist(e, e, c1, c2) <= r + _TOUCH_EPS for e in corners):
            continue
        return True
    for pad in copper.pads:
        if not _pad_on_layer(pad, layer):
            continue
        reach = math.hypot(pad.hx, pad.hy)
        if (
            pad.cx + reach < minx
            or pad.cx - reach > maxx
            or pad.cy + reach < miny
            or pad.cy - reach > maxy
        ):
            continue
        if _pad_seg_dist(pad, q1, q2) > _TOUCH_EPS:
            continue
        if _exits(pad, p) or _exits(pad, n):
            continue
        return True
    return False


def _pairings(
    p_segs: Sequence[Segment], n_segs: Sequence[Segment], copper: Copper | None
) -> list[Pairing]:
    """Every coupled P/N primitive pair, as KiCad forms them (Issue #6554).

    A P segment and a same-layer N segment pair when :func:`_parallel` and
    their projections overlap; every such pair counts (pairs are summed, not
    unioned), unless :func:`_blocked` finds copper between them.  Without
    ``copper`` nothing blocks.
    """
    out: list[Pairing] = []
    n_nm = [(_nm(b[0], b[1]), _nm(b[2], b[3])) for b in n_segs]
    for i, a in enumerate(p_segs):
        p0, p1 = _nm(a[0], a[1]), _nm(a[2], a[3])
        if _dot(_sub(p1, p0), _sub(p1, p0)) <= 2:
            continue
        for j, b in enumerate(n_segs):
            n0, n1 = n_nm[j]
            if b[4] != a[4] or _dot(_sub(n1, n0), _sub(n1, n0)) <= 2:
                continue
            if not _parallel(p0, p1, n0, n1):
                continue
            ends = _overlap(p0, p1, n0, n1)
            if ends is None or (copper is not None and _blocked(a, b, ends, copper)):
                continue
            out.append((i, j, *ends))
    return out


def _along(seg: Segment, q: tuple[float, float]) -> float:
    """Distance (mm) from ``seg``'s start to ``q``'s projection, clamped to the segment."""
    lo = _length(seg)
    if lo <= 0.0:
        return 0.0
    t = ((q[0] - seg[0]) * (seg[2] - seg[0]) + (q[1] - seg[1]) * (seg[3] - seg[1])) / lo
    return max(0.0, min(lo, t))


def _coupled_spans(
    own: Sequence[Segment],
    partner: Sequence[Segment],
    copper: Copper | None = None,
    own_leads: bool = True,
) -> list[list[tuple[float, float]]]:
    """Per ``own`` segment, the spans (from its start) that couple to some partner.

    One span per pair from :func:`_pairings`.  KiCad forms the pairs from the
    P net's side, so ``own_leads`` says whether ``own`` is the P net; the N
    net's spans are the projections of the same pairs.  This is the single
    pairing rule behind both the total (:func:`measure_uncoupled`) and the
    per-terminal figure.
    """
    p_segs, n_segs = (own, partner) if own_leads else (partner, own)
    out: list[list[tuple[float, float]]] = [[] for _ in own]
    for i, j, pa, pb, na, nb in _pairings(p_segs, n_segs, copper):
        k, a, b = (i, pa, pb) if own_leads else (j, na, nb)
        s, e = sorted((_along(own[k], _mm(a)), _along(own[k], _mm(b))))
        if e > s:
            out[k].append((s, e))
    return out


def _coupled_length(
    own: Sequence[Segment],
    partner: Sequence[Segment],
    copper: Copper | None = None,
    own_leads: bool = True,
) -> float:
    """Sum, over unblocked parallel ``own``/``partner`` pairs, of their overlap.

    Pairs are summed, not unioned: two partners beside the same stretch both
    count, as in KiCad (which can report a negative uncoupled length).
    """
    return sum(b - a for spans in _coupled_spans(own, partner, copper, own_leads) for a, b in spans)


def measure_uncoupled(
    own: Sequence[Segment],
    partner: Sequence[Segment],
    copper: Copper | None = None,
    own_leads: bool = True,
) -> float:
    """Uncoupled length (mm) of ``own`` against ``partner``: KiCad's per-net figure.

    ``copper`` is the rest of the board's copper (see :class:`Copper`); without
    it every parallel overlap couples.  ``own_leads`` says whether ``own`` is
    the P net (KiCad forms the pairs from P's side).
    """
    coupled = _coupled_length(own, partner, copper, own_leads)
    return max(0.0, sum(_length(s) for s in own) - coupled)


def _board_copper(pcb: PCB) -> Copper:
    """Every track, via and pad of ``pcb`` as pairing obstacles."""
    from kicad_tools.core.geometry import rotate_pad_offset

    tracks: list[Obstacle] = [
        (s.start[0], s.start[1], s.end[0], s.end[1], s.width, s.layer) for s in pcb.segments
    ]
    tracks += [
        (v.position[0], v.position[1], v.position[0], v.position[1], v.size, None) for v in pcb.vias
    ]
    pads: list[PadRect] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            ox, oy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            ang = math.radians(pad.rotation)
            pads.append(
                PadRect(
                    fp.position[0] + ox,
                    fp.position[1] + oy,
                    pad.size[0] / 2.0,
                    pad.size[1] / 2.0,
                    math.cos(ang),
                    math.sin(ang),
                    frozenset(pad.layers),
                    pad.net_name or "",
                )
            )
    return Copper(tracks, pads)


# -- per-terminal figure (Issue #6615) ----------------------------------------

Interval = tuple[float, float]


def _subtract(base: list[Interval], cut: list[Interval]) -> list[Interval]:
    """``base`` minus the union of ``cut`` (all intervals as ``(start, end)``)."""
    out: list[Interval] = []
    cuts = sorted(c for c in cut if c[1] > c[0])
    for start, end in base:
        cursor = start
        for c0, c1 in cuts:
            if c1 <= cursor or c0 >= end:
                continue
            if c0 > cursor:
                out.append((cursor, c0))
            cursor = max(cursor, c1)
            if cursor >= end:
                break
        if cursor < end:
            out.append((cursor, end))
    return [(a, b) for a, b in out if b - a > 1e-9]


def uncoupled_intervals(own: Segment, coupled: Sequence[Interval]) -> list[Interval]:
    """Spans of ``own`` (distance from its start) outside the ``coupled`` spans.

    ``coupled`` come from :func:`_coupled_spans`, the pairing behind
    :func:`measure_uncoupled`.  Overlapping partners are unioned here, so the
    spans sum to at least that segment's share of the total (which sums pairs).
    """
    return _subtract([(0.0, _length(own))], list(coupled))


@dataclass(frozen=True)
class Terminal:
    """One terminal of a net: a footprint with a pad on it (Issue #6615).

    Attributes:
        reference: The footprint reference (``"J1"``).
        region: The gather-ignore region as a shapely geometry, or ``None``
            when ``gather_ignore == "none"``.
        region_source: ``"courtyard"``, ``"pad bbox"`` (no usable courtyard,
            see :data:`PAD_BBOX_FALLBACK_MARGIN_MM`) or ``"none"``.
        pads: ``(x, y, radius)`` of the footprint's pads on the net, each as
            its circumscribed circle in board coordinates.
    """

    reference: str
    region: Any
    region_source: str
    pads: tuple[tuple[float, float, float], ...]

    def contains(self, x: float, y: float) -> bool:
        """True when ``(x, y)`` is inside the region or on one of the pads."""
        for px, py, r in self.pads:
            if math.hypot(x - px, y - py) <= r + _NODE_TOL_MM:
                return True
        if self.region is None:
            return False
        from shapely.geometry import Point  # type: ignore[import-untyped]

        return bool(self.region.buffer(_NODE_TOL_MM).covers(Point(x, y)))

    def distance(self, x: float, y: float) -> float:
        """Straight-line distance from ``(x, y)`` to the region or a pad."""
        best = min((math.hypot(x - px, y - py) - r for px, py, r in self.pads), default=math.inf)
        if self.region is not None:
            from shapely.geometry import Point

            best = min(best, self.region.distance(Point(x, y)))
        return max(0.0, best)


def _gather_radius(gather_ignore: str | float) -> float | None:
    """Radius (mm) the region is grown by, ``None`` for ``"none"``."""
    if gather_ignore is None or gather_ignore == "courtyard":
        return 0.0
    if gather_ignore == "none":
        return None
    return float(gather_ignore)


def _footprint_region(fp: Any, radius: float) -> tuple[Any, str]:
    """``(region, source)`` of one footprint: its courtyard, else its pads' box."""
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from kicad_tools.geometry.courtyard import _courtyard_polygon, _fp_transform

    polys = [p for p in (_courtyard_polygon(fp, side, Polygon) for side in ("F", "B")) if p]
    if polys:
        region, source = unary_union(polys), "courtyard"
    else:
        transform = _fp_transform(fp)
        circles = []
        for pad in fp.pads:
            x, y = transform(pad.position)
            circles.append((x, y, math.hypot(pad.size[0], pad.size[1]) / 2.0))
        if not circles:
            return None, "none"
        m = PAD_BBOX_FALLBACK_MARGIN_MM
        region = box(
            min(x - r for x, _, r in circles) - m,
            min(y - r for _, y, r in circles) - m,
            max(x + r for x, _, r in circles) + m,
            max(y + r for _, y, r in circles) + m,
        )
        source = "pad bbox"
    if radius > 0.0:
        region = region.buffer(radius)
    return region, source


def net_terminals(
    pcb: PCB, net_name: str, gather_ignore: str | float = GATHER_IGNORE_DEFAULT
) -> list[Terminal]:
    """The terminals of ``net_name`` with their gather-ignore regions, by reference."""
    from kicad_tools.geometry.courtyard import _fp_transform

    radius = _gather_radius(gather_ignore)
    out: list[Terminal] = []
    for fp in pcb.footprints:
        on_net = [p for p in fp.pads if p.net_name == net_name]
        if not on_net:
            continue
        transform = _fp_transform(fp)
        pads = tuple(
            (*transform(p.position), math.hypot(p.size[0], p.size[1]) / 2.0) for p in on_net
        )
        if radius is None:
            region, source = None, "none"
        else:
            region, source = _footprint_region(fp, radius)
        out.append(Terminal(fp.reference, region, source, pads))
    return sorted(out, key=lambda t: t.reference)


def _inside_intervals(seg: Segment, region: Any) -> list[Interval]:
    """Spans of ``seg`` (distance from its start) inside ``region``."""
    if region is None:
        return []
    from shapely.geometry import LineString, Point

    line = LineString([(seg[0], seg[1]), (seg[2], seg[3])])
    hit = line.intersection(region)
    spans: list[Interval] = []
    for part in getattr(hit, "geoms", [hit]):
        if part.is_empty or part.geom_type != "LineString":
            continue
        a, b = (line.project(Point(c)) for c in (part.coords[0], part.coords[-1]))
        if abs(b - a) > 1e-9:
            spans.append((min(a, b), max(a, b)))
    return spans


def _attribute(
    segs: list[Segment],
    counted: list[list[Interval]],
    vias: list[tuple[float, float, float]],
    terminals: list[Terminal],
) -> dict[str, float]:
    """Charge each segment's ``counted`` spans to its nearest terminal along the copper.

    ``counted[i]`` are spans of ``segs[i]``.  Returns ``{reference: mm}`` for
    every terminal (zero when nothing is charged to it).
    """
    totals = {t.reference: 0.0 for t in terminals}
    if not terminals:
        return totals

    # Graph nodes: track endpoints (per layer), joined by vias.
    nodes: list[tuple[float, float, str]] = []

    def node(x: float, y: float, layer: str) -> int:
        for i, (nx, ny, nl) in enumerate(nodes):
            if nl == layer and abs(nx - x) <= _NODE_TOL_MM and abs(ny - y) <= _NODE_TOL_MM:
                return i
        nodes.append((x, y, layer))
        return len(nodes) - 1

    ends = [(node(s[0], s[1], s[4]), node(s[2], s[3], s[4])) for s in segs]
    adj: dict[int, list[tuple[int, float]]] = {}

    def link(a: int, b: int, w: float) -> None:
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    # Split every segment at the nodes lying on it (T-junctions).
    pieces: list[list[tuple[float, float, int, int]]] = []  # (t0, t1, node0, node1)
    for i, s in enumerate(segs):
        length = _length(s)
        cuts = [(0.0, ends[i][0]), (length, ends[i][1])]
        if length > 0.0:
            ux, uy = (s[2] - s[0]) / length, (s[3] - s[1]) / length
            for k, (x, y, layer) in enumerate(nodes):
                if layer != s[4] or k in ends[i]:
                    continue
                along = (x - s[0]) * ux + (y - s[1]) * uy
                off = abs((x - s[0]) * uy - (y - s[1]) * ux)
                if 0.0 < along < length and off <= _NODE_TOL_MM:
                    cuts.append((along, k))
        cuts.sort()
        row = []
        for (t0, n0), (t1, n1) in zip(cuts, cuts[1:], strict=False):
            row.append((t0, t1, n0, n1))
            link(n0, n1, t1 - t0)
        pieces.append(row)
    for k, (vx, vy, vr) in enumerate(vias):
        hub = len(nodes) + k
        for i, (x, y, _layer) in enumerate(nodes):
            if math.hypot(x - vx, y - vy) <= vr + _NODE_TOL_MM:
                link(hub, i, 0.0)

    # Multi-source shortest paths from every terminal.
    dist: dict[int, float] = {}
    owner: dict[int, str] = {}
    heap: list[tuple[float, str, int]] = []
    for i, (x, y, _layer) in enumerate(nodes):
        for t in terminals:
            if t.contains(x, y):
                heap.append((0.0, t.reference, i))
                break
    heapq.heapify(heap)
    while heap:
        d, ref, i = heapq.heappop(heap)
        if i in dist:
            continue
        dist[i], owner[i] = d, ref
        for j, w in adj.get(i, []):
            if j not in dist:
                heapq.heappush(heap, (d + w, ref, j))

    def nearest(x: float, y: float) -> str:
        return min(terminals, key=lambda t: (t.distance(x, y), t.reference)).reference

    for s, spans, row in zip(segs, counted, pieces, strict=True):
        if not spans:
            continue
        length = _length(s)
        for t0, t1, n0, n1 in row:
            d0, d1 = dist.get(n0), dist.get(n1)
            if d0 is None and d1 is None:
                tm = (t0 + t1) / 2.0
                f = tm / length if length > 0.0 else 0.0
                ref = nearest(s[0] + f * (s[2] - s[0]), s[1] + f * (s[3] - s[1]))
                shares = [(t0, t1, ref)]
            elif d1 is None:
                shares = [(t0, t1, owner[n0])]
            elif d0 is None:
                shares = [(t0, t1, owner[n1])]
            else:
                # The point where both ends are equally far along the copper.
                split = min(t1, max(t0, t0 + (d1 + (t1 - t0) - d0) / 2.0))
                shares = [(t0, split, owner[n0]), (split, t1, owner[n1])]
            for a, b, ref in shares:
                for c0, c1 in spans:
                    overlap = min(b, c1) - max(a, c0)
                    if overlap > 0.0:
                        totals[ref] += overlap
    return totals


def measure_per_terminal(
    pcb: PCB,
    own: list[Segment],
    partner: list[Segment],
    own_id: int,
    own_name: str,
    gather_ignore: str | float = GATHER_IGNORE_DEFAULT,
    copper: Copper | None = None,
    own_leads: bool = True,
) -> tuple[TerminalFigure, ...]:
    """Per-terminal uncoupled figures of one net (Issue #6615).

    The uncoupled spans of ``own`` minus every terminal's gather-ignore
    region, charged to terminals by :func:`_attribute`.  Empty without
    terminals.
    """
    terminals = net_terminals(pcb, own_name, gather_ignore)
    if not terminals:
        return ()
    regions = [t.region for t in terminals if t.region is not None]
    coupled = _coupled_spans(own, partner, copper, own_leads)
    counted = []
    for s, cs in zip(own, coupled, strict=True):
        spans = uncoupled_intervals(s, cs)
        if spans and regions:
            inside = [iv for r in regions for iv in _inside_intervals(s, r)]
            spans = _subtract(spans, inside)
        counted.append(spans)
    vias = [(v.position[0], v.position[1], v.size / 2.0) for v in pcb.vias_in_net(own_id)]
    totals = _attribute(own, counted, vias, terminals)
    return tuple((t.reference, totals[t.reference], t.region_source) for t in terminals)


def net_component_counts(pcb: PCB) -> dict[str, int]:
    """``{net name: number of components with a pad on the net}``."""
    refs: dict[str, set[str]] = {}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name:
                refs.setdefault(pad.net_name, set()).add(fp.reference)
    return {name: len(r) for name, r in refs.items()}


def measure_pair(
    pcb: PCB,
    p_id: int,
    n_id: int,
    p_name: str,
    n_name: str,
    components: dict[str, int] | None = None,
    gather_ignore: str | float = GATHER_IGNORE_DEFAULT,
) -> PairMeasure:
    """Measure one pair on ``pcb`` the way KiCad's ``diff_pair_uncoupled`` does.

    Also measures the kct-only per-terminal figures (Issue #6615) with the
    given ``gather_ignore`` region; they do not change the KiCad figure.
    """
    p_segs: list[Segment] = []
    n_segs: list[Segment] = []
    for net_id, bucket in ((p_id, p_segs), (n_id, n_segs)):
        for s in pcb.segments_in_net(net_id):
            bucket.append((s.start[0], s.start[1], s.end[0], s.end[1], s.layer, s.width))
    copper = _board_copper(pcb)
    comps = components if components is not None else net_component_counts(pcb)
    return PairMeasure(
        p_name=p_name,
        n_name=n_name,
        p_routed_mm=sum(_length(s) for s in p_segs),
        n_routed_mm=sum(_length(s) for s in n_segs),
        p_uncoupled_mm=measure_uncoupled(p_segs, n_segs, copper, True),
        n_uncoupled_mm=measure_uncoupled(n_segs, p_segs, copper, False),
        vias_p=sum(1 for _ in pcb.vias_in_net(p_id)),
        vias_n=sum(1 for _ in pcb.vias_in_net(n_id)),
        components_p=comps.get(p_name, 0),
        components_n=comps.get(n_name, 0),
        p_terminals=measure_per_terminal(
            pcb, p_segs, n_segs, p_id, p_name, gather_ignore, copper, True
        ),
        n_terminals=measure_per_terminal(
            pcb, n_segs, p_segs, n_id, n_name, gather_ignore, copper, False
        ),
    )


def uncoupled_limit(limits: PairThresholds, components: int) -> tuple[float | None, str | None]:
    """``(limit mm, how it was derived)`` for a pair, or ``(None, None)``.

    The class's explicit total wins; else the per-terminal allowance times
    the component count (at least one terminal).
    """
    if limits.max_uncoupled_total_mm is not None:
        return limits.max_uncoupled_total_mm, limits.provenance.get("max_uncoupled_total_mm")
    per = limits.max_uncoupled_per_terminal_mm
    if per is None:
        return None, None
    n = max(components, 1)
    src = limits.provenance.get("max_uncoupled_per_terminal_mm", "")
    return per * n, f"{per:g} mm x {n} component{'s' if n != 1 else ''} [{src}]"


def _region_label(source: str, gather_ignore: str | float) -> str:
    """How a terminal's gather-ignore region was built, for a finding."""
    if source == "none":
        return "terminal pads; gather_ignore none"
    radius = _gather_radius(gather_ignore) or 0.0
    grown = f" + {radius:g} mm" if radius > 0.0 else ""
    if source == "pad bbox":
        return f"pad bounding box + {PAD_BBOX_FALLBACK_MARGIN_MM:g} mm{grown}, no courtyard"
    return f"courtyard{grown}"


def _key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


class DiffPairUncoupledLengthRule(DRCRule):
    """Uncoupled length and via count of engaged differential pairs.

    Attributes:
        rule_id: ``"diffpair_uncoupled_length"``.
        limits: ``{(min_net_id, max_net_id): PairThresholds}`` -- the
            resolved limits and their provenance and severity.  A pair
            without a limit is still measured (reported as ``info`` when
            ``emit_info``).
        engaged_pairs: ``{(min_net_id, max_net_id)}``.  Empty or ``None``:
            the rule is a no-op.
    """

    rule_id = "diffpair_uncoupled_length"
    name = "Differential-Pair Uncoupled Length"
    description = (
        "Validates that engaged differential pairs stay beside each other: "
        "the larger of the two nets' uncoupled lengths, the uncoupled copper "
        "charged to each terminal outside its courtyard (kct-only), and each "
        "net's via count, within the protocol or class limit."
    )

    def __init__(
        self,
        engaged_pairs: set[tuple[int, int]] | None = None,
        limits: dict[tuple[int, int], PairThresholds] | None = None,
        emit_info: bool = False,
        kct_only: dict[tuple[int, int], str] | None = None,
    ) -> None:
        """Initialize the rule.

        Args:
            engaged_pairs: Pairs to validate.
            limits: Per-pair resolved limits.
            emit_info: Also emit an ``info`` finding for a passing pair
                carrying the measurement (``kct check --verbose``).
            kct_only: ``{pair key: reason}`` for pairs KiCad's native check
                cannot see (names it does not recognise); each gets one
                ``info`` finding saying the limit is kct-only.
        """
        self._engaged = {_key(a, b) for a, b in (engaged_pairs or set())}
        self._limits = {_key(a, b): t for (a, b), t in (limits or {}).items()}
        self._emit_info = emit_info
        self._kct_only = {_key(a, b): why for (a, b), why in (kct_only or {}).items()}

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,  # noqa: ARG002 (signature mirrors peer rules)
    ) -> DRCResults:
        results = DRCResults()
        if not self._engaged:
            return results
        names = {n.number: n.name for n in pcb.nets.values() if n.name}
        comps = net_component_counts(pcb)
        for a, b in sorted(self._engaged):
            na, nb = names.get(a), names.get(b)
            if not na or not nb:
                continue
            limits = self._limits.get((a, b))
            gather = getattr(limits, "gather_ignore", GATHER_IGNORE_DEFAULT)
            measure = measure_pair(pcb, a, b, na, nb, comps, gather_ignore=gather)
            if not measure.routed:
                continue  # unrouted pair: no copper, no uncoupled number
            results.rules_checked += 1
            results.rules_checked_by_rule[self.rule_id] = (
                results.rules_checked_by_rule.get(self.rule_id, 0) + 1
            )
            self._check_uncoupled(results, measure, limits, (a, b))
            self._check_per_terminal(results, measure, limits)
            self._check_vias(results, measure, limits)
        return results

    # -- per-terminal uncoupled length (kct-only, Issue #6615) -------------

    def _check_per_terminal(
        self, results: DRCResults, m: PairMeasure, limits: PairThresholds | None
    ) -> None:
        if limits is None or limits.max_uncoupled_per_terminal_mm is None:
            return
        worst = m.per_terminal_mm
        if worst is None:
            return  # no terminal on the board: nothing to charge copper to
        first, second = sorted((m.p_name, m.n_name))
        limit = limits.max_uncoupled_per_terminal_mm
        src = limits.provenance.get("max_uncoupled_per_terminal_mm", "")
        severity = getattr(limits, "uncoupled_per_terminal_severity", limits.uncoupled_severity)
        gather = getattr(limits, "gather_ignore", GATHER_IGNORE_DEFAULT)
        over = False
        for net, figures in ((m.p_name, m.p_terminals), (m.n_name, m.n_terminals)):
            for ref, mm, region in figures:
                if mm <= limit + UNCOUPLED_EPSILON_MM:
                    continue
                over = True
                results.add(
                    self._violation(
                        severity,
                        f"Differential pair {first}/{second}: net {net} "
                        f"{PER_TERMINAL_PHRASE} at {ref} {mm:.3f} mm (outside the "
                        f"{_region_label(region, gather)}) exceeds limit {limit:.3f} mm "
                        f"[{src}] ({KCT_ONLY_NOTE})",
                        m,
                        mm,
                        limit,
                        nets=(net,),
                    )
                )
        if not over and self._emit_info:
            results.add(
                self._violation(
                    "info",
                    f"Differential pair {first}/{second} worst {PER_TERMINAL_PHRASE} "
                    f"{worst:.3f} mm within limit {limit:.3f} mm [{src}] ({KCT_ONLY_NOTE})",
                    m,
                    worst,
                    limit,
                )
            )

    # -- uncoupled length --------------------------------------------------

    def _check_uncoupled(
        self,
        results: DRCResults,
        m: PairMeasure,
        limits: PairThresholds | None,
        key: tuple[int, int],
    ) -> None:
        first, second = sorted((m.p_name, m.n_name))
        why = self._kct_only.get(key)
        if why:
            results.add(
                self._violation(
                    "info",
                    f"Differential pair {first}/{second}: {why}; the uncoupled-length "
                    "limit is checked by kct only",
                    m,
                    m.uncoupled_mm,
                    None,
                )
            )
        limit, how = uncoupled_limit(limits, m.components) if limits is not None else (None, None)
        if limit is None:
            return
        detail = (
            f"uncoupled length {m.uncoupled_mm:.3f} mm "
            f"(P {m.p_uncoupled_mm:.3f}, N {m.n_uncoupled_mm:.3f})"
        )
        if m.uncoupled_mm > limit + UNCOUPLED_EPSILON_MM:
            assert limits is not None
            results.add(
                self._violation(
                    limits.uncoupled_severity,
                    f"Differential pair {first}/{second} {detail} exceeds limit "
                    f"{limit:.3f} mm = {how}",
                    m,
                    m.uncoupled_mm,
                    limit,
                )
            )
        elif self._emit_info:
            results.add(
                self._violation(
                    "info",
                    f"Differential pair {first}/{second} {detail} within limit "
                    f"{limit:.3f} mm = {how}",
                    m,
                    m.uncoupled_mm,
                    limit,
                )
            )

    # -- vias --------------------------------------------------------------

    def _check_vias(
        self, results: DRCResults, m: PairMeasure, limits: PairThresholds | None
    ) -> None:
        first, second = sorted((m.p_name, m.n_name))
        if limits is not None and limits.max_vias_per_net is not None:
            cap = limits.max_vias_per_net
            src = limits.provenance.get("max_vias_per_net", "")
            for name, count in ((m.p_name, m.vias_p), (m.n_name, m.vias_n)):
                if count > cap:
                    results.add(
                        self._violation(
                            limits.vias_severity,
                            f"Differential pair {first}/{second}: net {name} has "
                            f"{count} {VIA_OVER_LIMIT_PHRASE} {cap} [{src}]",
                            m,
                            float(count),
                            float(cap),
                            nets=(name,),
                        )
                    )
        if m.vias_p != m.vias_n:
            results.add(
                self._violation(
                    "warning",
                    f"Differential pair {first}/{second} has {VIA_UNEQUAL_PHRASE} "
                    f"({m.p_name}: {m.vias_p}, {m.n_name}: {m.vias_n})",
                    m,
                    float(abs(m.vias_p - m.vias_n)),
                    0.0,
                )
            )

    def _violation(
        self,
        severity: str,
        message: str,
        m: PairMeasure,
        actual: float,
        required: float | None,
        nets: tuple[str, ...] | None = None,
    ) -> DRCViolation:
        pair = tuple(sorted((m.p_name, m.n_name)))
        return DRCViolation(
            rule_id=self.rule_id,
            severity=severity,
            message=message,
            location=None,
            layer=None,
            actual_value=round(actual, 4),
            required_value=round(required, 4) if required is not None else None,
            items=pair if severity != "info" else (),
            nets=nets if nets is not None else (m.p_name, m.n_name),
        )
