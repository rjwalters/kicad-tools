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
* **Coupled means exactly parallel.**  A P segment is coupled over the
  length its projection overlaps a same-layer N segment whose direction
  differs by no more than ~0.01 degrees (anti-parallel counts).  A 0.02
  degree skew already makes both traces uncoupled.  There is **no distance
  limit** unless a ``diff_pair_gap (max ..)`` constraint is present, and kct
  emits only ``(min ..)``.  Two parallel traces 10 mm apart are coupled.
* **Via barrels are not counted.**  A via is not track length: a pair whose
  vias sit side by side loses nothing, and offset vias cost only the
  track copper that no longer has a partner beside it.

:func:`measure_uncoupled` reproduces exactly that, so kct and kicad-cli agree
on pass/fail per pair; the known divergence (arcs are not measured, and
KiCad's number is known to misreport on meanders) is listed in
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_engagement import PairThresholds

#: Direction difference (degrees) up to which KiCad still calls two segments
#: parallel.  Measured on kicad-cli 10.0.6: 0.005 degrees couples, 0.02 does not.
KICAD_PARALLEL_TOLERANCE_DEG = 0.01
_SIN_TOL = math.sin(math.radians(KICAD_PARALLEL_TOLERANCE_DEG))

#: Float-comparison slack (mm) on a limit, as the sibling skew rule has.
UNCOUPLED_EPSILON_MM = 1e-6

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


Segment = tuple[float, float, float, float, str]  # x1, y1, x2, y2, layer
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


def _coupled_interval(own: Segment, other: Segment) -> tuple[float, float] | None:
    """Span of ``own`` (distance from its start) beside ``other``, or ``None``."""
    if own[4] != other[4]:
        return None
    ox, oy = own[2] - own[0], own[3] - own[1]
    tx, ty = other[2] - other[0], other[3] - other[1]
    lo, lt = math.hypot(ox, oy), math.hypot(tx, ty)
    if lo <= 0.0 or lt <= 0.0:
        return None
    # sin(angle) between the directions; the absolute slack absorbs the
    # 1 nm coordinate quantum on very short segments.
    if abs(ox * ty - oy * tx) > _SIN_TOL * lo * lt + 1e-6:
        return None
    ux, uy = ox / lo, oy / lo
    a = (other[0] - own[0]) * ux + (other[1] - own[1]) * uy
    b = (other[2] - own[0]) * ux + (other[3] - own[1]) * uy
    start, end = max(0.0, min(a, b)), min(lo, max(a, b))
    return (start, end) if end > start else None


def _coupled_length(own: list[Segment], partner: list[Segment]) -> float:
    """Length of ``own`` that runs beside ``partner`` copper (union per segment)."""
    total = 0.0
    for seg in own:
        spans = sorted(s for s in (_coupled_interval(seg, p) for p in partner) if s)
        covered = 0.0
        cursor = 0.0
        for start, end in spans:
            start = max(start, cursor)
            if end > start:
                covered += end - start
                cursor = end
        total += covered
    return total


def measure_uncoupled(own: list[Segment], partner: list[Segment]) -> float:
    """Uncoupled length (mm) of ``own`` against ``partner``: KiCad's per-net figure."""
    return max(0.0, sum(_length(s) for s in own) - _coupled_length(own, partner))


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


def uncoupled_intervals(own: Segment, partner: list[Segment]) -> list[Interval]:
    """Spans of ``own`` (distance from its start) beside no ``partner`` copper.

    The complement of :func:`_coupled_interval`'s union, so the spans of a
    segment always sum to its share of :func:`measure_uncoupled`.
    """
    coupled = [s for s in (_coupled_interval(own, p) for p in partner) if s]
    return _subtract([(0.0, _length(own))], coupled)


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
    counted = []
    for s in own:
        spans = uncoupled_intervals(s, partner)
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


def net_track_mm(pcb: PCB, net_id: int) -> float:
    """Straight-track copper (mm) on ``net_id`` -- the figure ``routed`` reads.

    A pair is measured (``PairMeasure.routed``) only when this is positive
    for both nets.  Issue #6820: ``routing_policy`` uses it to defer a pair's
    via cap to this rule only when this rule will actually evaluate the pair.
    """
    return sum(
        math.hypot(s.end[0] - s.start[0], s.end[1] - s.start[1])
        for s in pcb.segments_in_net(net_id)
    )


def pair_is_measured(pcb: PCB, p_id: int, n_id: int) -> bool:
    """True when :meth:`DiffPairUncoupledLengthRule.check` evaluates the pair."""
    return net_track_mm(pcb, p_id) > 0.0 and net_track_mm(pcb, n_id) > 0.0


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
            bucket.append((s.start[0], s.start[1], s.end[0], s.end[1], s.layer))
    comps = components if components is not None else net_component_counts(pcb)
    return PairMeasure(
        p_name=p_name,
        n_name=n_name,
        p_routed_mm=sum(_length(s) for s in p_segs),
        n_routed_mm=sum(_length(s) for s in n_segs),
        p_uncoupled_mm=measure_uncoupled(p_segs, n_segs),
        n_uncoupled_mm=measure_uncoupled(n_segs, p_segs),
        vias_p=sum(1 for _ in pcb.vias_in_net(p_id)),
        vias_n=sum(1 for _ in pcb.vias_in_net(n_id)),
        components_p=comps.get(p_name, 0),
        components_n=comps.get(n_name, 0),
        p_terminals=measure_per_terminal(pcb, p_segs, n_segs, p_id, p_name, gather_ignore),
        n_terminals=measure_per_terminal(pcb, n_segs, p_segs, n_id, n_name, gather_ignore),
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
