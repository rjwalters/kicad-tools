"""Diff-pair coupled routing on the lattice engine (issue #4270, epic #4267 P3).

**Fat-agent centerline + geometric offset emission.**  An engaged
differential pair is routed as ONE A* agent over the lattice -- the pair
*centerline* -- with every legality check inflated by ``pair_pitch / 2``
(``pair_pitch = trace_width + effective intra-pair clearance``, a
*geometric* quantity deliberately NOT bound to lattice rows: board-06's
0.275-0.30 mm pitches are finer than the 0.4 mm minimum adjacent-row
pitch, so the node-constrained "two adjacent lattice rows" primitive was
rejected at curation).  P and N are then emitted as perpendicular
``+/- pitch/2`` offsets of the centerline.

Octilinearity is preserved *by construction*: offsetting a polyline moves
each segment onto a parallel line and every interior vertex onto the exact
intersection of the two adjacent offset lines, so segment directions are
unchanged and the emitted legs pass the #3907 ``Segment`` 45-degree choke
untouched.  Copper is geometric -- :class:`CommittedCopper` and DRC do not
care that offset vertices sit off-node.

**The fat pair search is planar** (``allow_vias=False``): a pair that cannot
complete a coupled run on one layer DECLINES honestly, with a per-pair
reason, and never ships a short (#3906 discipline).  Issue #6444 adds one
retry for a run whose planar search is exhausted: fan-via terminal gateways
(:mod:`.gateway`) move the run to another layer -- each net fans out to one
through via per end on its pad layer, and the same planar search couples the
run between the via pairs on the gateway layer.  The coupled agent
itself never splits; issue #6373 added an opt-in (``route_netset(...,
uncoupled_fallback=True)``, always on from the autorouter) that then routes
the declined pair's main run as two ordinary single-ended legs, reported as
``uncoupled-fallback:<reason>`` -- a diff-pair warning, not an open net.

This module holds the pure-geometry / model-query halves of the feature so
:mod:`.pathfinder` only gains a thin fat-agent branch and
:mod:`.obstacles` stays untouched (issue #4271 threads per-class widths
through those files in parallel).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import kernel_adapter as ka
from .geometry import (
    Pt,
    Rect,
    dist,
    pt_in_rect,
    seg_rect_intersect,
    seg_seg_dist,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..primitives import Pad
    from .obstacles import CommittedCopper, LatticeKeepoutMask, LatticeObstacleModel

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Connection shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoupledConnection:
    """One engaged differential pair, routed as a single fat A* agent.

    The four pads are the pair's *main-run* endpoints (P/N at end A, P/N
    at end B), selected by proximity pairing -- extra same-net pads are
    routed afterwards as ordinary single-ended connections.

    Attributes:
        key: Opaque hashable negotiation key (owned by the caller).
        pair_name: Human-readable pair base name for reporting.
        pad_p_a / pad_n_a: Positive / negative pads at end A.
        pad_p_b / pad_n_b: Positive / negative pads at end B.
        net_class: The pair's ``NetClassRouting`` (trace width source).
        pitch: P/N centreline pitch in mm --
            ``trace_width + effective_intra_pair_clearance()``.
        anchor_a / anchor_b: Optional relocated virtual endpoint of the fat
            centerline at end A / B (issue #6378).  ``None`` (the default)
            keeps the P/N pad midpoint.  Set when the midpoint sits inside a
            foreign pad's keep-out -- e.g. a SOT-23-6 ESD part whose GND pad
            lies between its D+/D- pads -- so the centerline starts just off
            the part and ``_pair_attach`` doglegs each pad onto its leg.
    """

    key: object
    pair_name: str
    pad_p_a: Pad
    pad_n_a: Pad
    pad_p_b: Pad
    pad_n_b: Pad
    net_class: object
    pitch: float
    anchor_a: Pt | None = None
    anchor_b: Pt | None = None

    @property
    def mid_a(self) -> Pt:
        if self.anchor_a is not None:
            return self.anchor_a
        return ((self.pad_p_a.x + self.pad_n_a.x) / 2.0, (self.pad_p_a.y + self.pad_n_a.y) / 2.0)

    @property
    def mid_b(self) -> Pt:
        if self.anchor_b is not None:
            return self.anchor_b
        return ((self.pad_p_b.x + self.pad_n_b.x) / 2.0, (self.pad_p_b.y + self.pad_n_b.y) / 2.0)

    @property
    def length(self) -> float:
        return dist(self.mid_a, self.mid_b)


# ---------------------------------------------------------------------------
# Fat-agent legality (geometric, no second mask build -- lattice_builds == 1)
# ---------------------------------------------------------------------------


def pads_block_segment_grown(
    obstacles: LatticeObstacleModel,
    a: Pt,
    b: Pt,
    layer: int,
    nets: set[int],
    extra: float,
    exempt: frozenset[int] | None = None,
) -> bool:
    """True if segment ``a-b`` enters any foreign pad keep-out on ``layer``
    after growing the keep-out rects by ``extra``.

    This is the ``segment_blocked`` pattern from :mod:`.obstacles` with the
    fat-agent twists: the inflated pad rects are grown by the pair
    half-envelope, and "self" is either every net in ``nets`` (attach /
    re-verification mode, ``exempt is None``) or ONLY the pad indices in
    ``exempt`` (centerline mode).  The centerline mode is deliberately
    stricter: a non-endpoint pad of a pair net is a real obstacle for the
    fat agent, because the leg of the OPPOSITE polarity would otherwise be
    laid straight over it (the board-06 USB2 B-row lesson).  Purely a query
    against the static model -- no mask rebuild.
    """
    x0, x1 = min(a[0], b[0]), max(a[0], b[0])
    y0, y1 = min(a[1], b[1]), max(a[1], b[1])
    for idx in obstacles.pads_near(x0 - extra, y0 - extra, x1 + extra, y1 + extra):
        if exempt is not None:
            if idx in exempt:
                continue
        elif obstacles.pads[idx].net in nets:
            continue
        if layer not in obstacles.pad_layer_indices[idx]:
            continue
        r = obstacles.pad_rects[idx]
        grown: Rect = (r[0] - extra, r[1] - extra, r[2] + extra, r[3] + extra)
        if seg_rect_intersect(a, b, grown):
            return True
    return False


def pads_block_point_grown(
    obstacles: LatticeObstacleModel,
    point: Pt,
    layer: int,
    nets: set[int],
    extra: float,
    exempt: frozenset[int] | None = None,
) -> bool:
    """Point flavour of :func:`pads_block_segment_grown`."""
    for idx in obstacles.pads_near(
        point[0] - extra, point[1] - extra, point[0] + extra, point[1] + extra
    ):
        if exempt is not None:
            if idx in exempt:
                continue
        elif obstacles.pads[idx].net in nets:
            continue
        if layer not in obstacles.pad_layer_indices[idx]:
            continue
        r = obstacles.pad_rects[idx]
        grown: Rect = (r[0] - extra, r[1] - extra, r[2] + extra, r[3] + extra)
        if pt_in_rect(point, grown):
            return True
    return False


def committed_seg_clear_grown(
    committed: CommittedCopper,
    a: Pt,
    b: Pt,
    layer: int,
    nets: set[int],
    extra: float,
) -> bool:
    """``CommittedCopper.seg_clear`` with gaps grown by ``extra`` and the
    whole pair-net set treated as "self".

    Issue #4271: committed segments carry their TRUE per-class half-width
    and clearance, so the per-item gap is ``trace_half + stored_half +
    max(clearance, stored_clearance) + extra`` -- the fat envelope is
    spaced honestly even against oversize (e.g. 2.6 mm HV) copper.

    Issue #4602 (deliberate exclusion): the fat-envelope predicates do NOT
    apply the HV pairwise (``|delta V|``-derived) widening.  An engaged diff
    pair is a same-domain signal pair by construction; giving the whole
    grown envelope a multi-mm HV keep-out on top of ``pitch/2`` would make
    every coupled run near mapped copper decline outright, and no fleet
    board pairs an HV domain net into a diff pair.  The emitted legs remain
    covered end-to-end: ``_pair_attach``/re-verification go through
    ``CommittedCopper.seg_clear`` (pairwise-aware when a projection is
    installed) and the #4588 post-route audit gates the committed copper.
    """
    if not committed.fixed_fills.segment_clear(
        a,
        b,
        layer,
        committed.trace_half + extra,
        committed.clearance,
    ):
        return False
    pad = committed.trace_half + committed.clearance + extra + 0.5
    # Epic #5509 Phase 3d: the gaps come from the shared clearance kernel via
    # the id-space adapter.  ``extra`` stays a term of the REQUIREMENT (the
    # fat agent's pair half-envelope) rather than of the copper width, exactly
    # as before -- the centreline carries it, not the copper.  A fat agent
    # owns a *set* of net ids rather than one, so the self test stays the
    # caller's ``nets`` membership and no single-id ``LatticeProbe`` applies.
    centreline = ka.trace(a, b, committed.trace_half, layer)
    for c, d, cnet, hw, iclr in committed.copper[layer].query_seg(a, b, pad=pad):
        if cnet in nets:
            continue
        required = max(committed.clearance, iclr) + extra
        if not ka.satisfies(ka.gap(centreline, ka.trace(c, d, hw, layer)), required):
            return False
    # Issue #4597: honor the stored via's class clearance, mirroring the
    # ``max(clearance, iclr)`` the copper loop above already applies.
    for point, vnet, vclr in committed.vias:
        if vnet in nets:
            continue
        required = max(committed.clearance, vclr) + extra
        if not ka.satisfies(ka.gap(centreline, ka.site(point, committed.via_radius)), required):
            return False
    return True


def committed_point_clear_grown(
    committed: CommittedCopper,
    point: Pt,
    layer: int,
    nets: set[int],
    extra: float,
) -> bool:
    """``CommittedCopper.node_clear`` with gaps grown by ``extra``."""
    if not committed.fixed_fills.segment_clear(
        point,
        point,
        layer,
        committed.trace_half + extra,
        committed.clearance,
    ):
        return False
    pad = committed.trace_half + committed.clearance + extra + 0.5
    # Epic #5509 Phase 3d: kernel gaps, as in ``committed_seg_clear_grown``.
    site = ka.site(point, committed.trace_half)
    for c, d, cnet, hw, iclr in committed.copper[layer].query_seg(point, point, pad=pad):
        if cnet in nets:
            continue
        required = max(committed.clearance, iclr) + extra
        if not ka.satisfies(ka.gap(site, ka.trace(c, d, hw, layer)), required):
            return False
    # Issue #4597: honor the stored via's class clearance (see
    # ``committed_seg_clear_grown``).
    for vpt, vnet, vclr in committed.vias:
        if vnet in nets:
            continue
        required = max(committed.clearance, vclr) + extra
        if not ka.satisfies(ka.gap(site, ka.site(vpt, committed.via_radius)), required):
            return False
    return True


# ---------------------------------------------------------------------------
# Polyline offset (octilinear-preserving by construction)
# ---------------------------------------------------------------------------


def merge_collinear_points(points: list[Pt]) -> list[Pt]:
    """Drop zero-length steps and interior vertices on straight runs.

    Offsetting is joint-by-joint, so fusing collinear lattice steps first
    keeps the offset legs free of spurious interior vertices.
    """
    out: list[Pt] = []
    for p in points:
        if out and dist(out[-1], p) <= _EPS:
            continue
        if len(out) >= 2:
            ax, ay = out[-2]
            bx, by = out[-1]
            d1 = (bx - ax, by - ay)
            d2 = (p[0] - bx, p[1] - by)
            cross = d1[0] * d2[1] - d1[1] * d2[0]
            dot = d1[0] * d2[0] + d1[1] * d2[1]
            if abs(cross) <= _EPS and dot > 0.0:
                out[-1] = p
                continue
        out.append(p)
    return out


def offset_polyline(points: list[Pt], offset: float) -> list[Pt] | None:
    """Offset ``points`` perpendicular by ``offset`` (left of travel > 0).

    Every offset segment lies on the parallel line of a source segment and
    every interior vertex is the exact intersection of two of those offset
    lines, so **emitted directions are a subset of the source directions**
    -- an octilinear input yields an octilinear output (#3907 by
    construction).

    Inside miters at tight turns can reverse a short offset segment (the
    classic miter collapse on fine-lattice jogs).  Such segments are
    eliminated and the neighbouring offset lines re-intersected, so the
    offset leg simply cuts the corner one or more source segments short --
    emitted directions remain a SUBSET of the source directions and the
    caller's geometric re-verification (masks + committed copper +
    intra-pair floor) stays the safety authority for collapsed geometry.

    Returns ``None`` when the offset is irrecoverably degenerate: a
    zero-length source segment, parallel non-collinear offset lines that
    would have to be joined (U-turn), or a collapse that would move the
    anchored first/last vertex (the endpoints must stay at the exact
    ``point +/- offset`` positions the pad-attach doglegs expect).
    """
    if len(points) < 2:
        return None
    # One offset line per source segment: (anchor point on the line, dir).
    lines: list[tuple[Pt, Pt]] = []
    for a, b in zip(points, points[1:], strict=False):
        length = dist(a, b)
        if length <= _EPS:
            return None
        d = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
        n = (-d[1], d[0])
        lines.append(((a[0] + offset * n[0], a[1] + offset * n[1]), d))

    def isect(l1: tuple[Pt, Pt], l2: tuple[Pt, Pt]) -> Pt | None:
        (p1, d1), (p2, d2) = l1, l2
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(cross) <= 1e-12:
            # Parallel offset lines: joinable only when collinear.
            off = (p2[0] - p1[0]) * d1[1] - (p2[1] - p1[1]) * d1[0]
            return p2 if abs(off) <= _EPS else None
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / cross
        return (p1[0] + t * d1[0], p1[1] + t * d1[1])

    n_first = (-lines[0][1][1], lines[0][1][0])
    start: Pt = (points[0][0] + offset * n_first[0], points[0][1] + offset * n_first[1])
    n_last = (-lines[-1][1][1], lines[-1][1][0])
    end: Pt = (points[-1][0] + offset * n_last[0], points[-1][1] + offset * n_last[1])

    idxs = list(range(len(lines)))
    for _guard in range(len(lines) + 1):
        verts: list[Pt] = [start]
        broken = False
        for a_i, b_i in zip(idxs, idxs[1:], strict=False):
            j = isect(lines[a_i], lines[b_i])
            if j is None:
                return None
            verts.append(j)
        verts.append(end)

        # Find the first offset segment that no longer advances along its
        # source direction (inside miter collapsed it).
        reversed_at: int | None = None
        for k, line_i in enumerate(idxs):
            d = lines[line_i][1]
            adv = (verts[k + 1][0] - verts[k][0]) * d[0] + (verts[k + 1][1] - verts[k][1]) * d[1]
            if adv <= _EPS:
                reversed_at = k
                broken = True
                break
        if not broken:
            return verts
        if len(idxs) < 3:
            return None
        assert reversed_at is not None
        if reversed_at == 0:
            # First (anchored) segment reversed: its junction with the next
            # line sits behind the fixed start vertex -- drop the next line.
            del idxs[1]
        elif reversed_at == len(idxs) - 1:
            # Last (anchored) segment reversed: junction beyond the fixed
            # end vertex -- drop the previous line.
            del idxs[-2]
        else:
            del idxs[reversed_at]
    return None


# ---------------------------------------------------------------------------
# Polarity
# ---------------------------------------------------------------------------


def side_bit(direction: Pt, pad_p: Pad, pad_n: Pad) -> bool:
    """True when the P pad sits on the LEFT of ``direction`` relative to N.

    "Left" is the ``offset > 0`` side of :func:`offset_polyline`, so this
    bit says which offset leg the positive net should own at an endpoint
    whose local travel direction is ``direction``.
    """
    n = (-direction[1], direction[0])
    return (pad_p.x - pad_n.x) * n[0] + (pad_p.y - pad_n.y) * n[1] > 0.0


def assign_polarity(
    plus_leg: list[Pt],
    minus_leg: list[Pt],
    pad_p_a: Pad,
    pad_n_a: Pad,
    pad_p_b: Pad,
    pad_n_b: Pad,
) -> tuple[list[Pt], list[Pt]] | None:
    """Map the two offset legs to (P, N) by endpoint proximity.

    Both ends must agree; a disagreement means the pair would need a
    polarity twist (legs crossing), which a planar coupled run cannot do
    -- return ``None`` so the caller declines honestly.
    """

    def side(pp: Pad, pn: Pad, q_plus: Pt, q_minus: Pt) -> bool:
        direct = dist((pp.x, pp.y), q_plus) + dist((pn.x, pn.y), q_minus)
        swapped = dist((pp.x, pp.y), q_minus) + dist((pn.x, pn.y), q_plus)
        return direct <= swapped  # True: P owns the plus leg

    at_a = side(pad_p_a, pad_n_a, plus_leg[0], minus_leg[0])
    at_b = side(pad_p_b, pad_n_b, plus_leg[-1], minus_leg[-1])
    if at_a != at_b:
        return None
    return (plus_leg, minus_leg) if at_a else (minus_leg, plus_leg)


# ---------------------------------------------------------------------------
# Misc geometry
# ---------------------------------------------------------------------------


def seg_rect_dist(a: Pt, b: Pt, rect: Rect) -> float:
    """Minimum distance from segment ``a-b`` to axis-aligned ``rect``."""
    if seg_rect_intersect(a, b, rect):
        return 0.0
    x0, y0, x1, y1 = rect
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return min(
        seg_seg_dist(a, b, c1, c2)
        for c1, c2 in zip(corners, corners[1:] + corners[:1], strict=False)
    )


Couple = tuple[int, int]
"""A ``(p_index, n_index)`` P/N pad couple (indices into the input lists)."""


def _greedy_couples(pads_p: list[Pad], pads_n: list[Pad]) -> list[Couple]:
    """Couple each P pad with its nearest unclaimed N pad, nearest first."""
    cands = sorted(
        (dist((p.x, p.y), (n.x, n.y)), i, j)
        for i, p in enumerate(pads_p)
        for j, n in enumerate(pads_n)
    )
    couples: list[Couple] = []
    used_p: set[int] = set()
    used_n: set[int] = set()
    for _d, i, j in cands:
        if i in used_p or j in used_n:
            continue
        couples.append((i, j))
        used_p.add(i)
        used_n.add(j)
    return couples


def _couple_mid(pads_p: list[Pad], pads_n: list[Pad], c: Couple) -> Pt:
    p, n = pads_p[c[0]], pads_n[c[1]]
    return ((p.x + n.x) / 2.0, (p.y + n.y) / 2.0)


def _farthest_couples(
    pads_p: list[Pad], pads_n: list[Pad], couples: list[Couple]
) -> tuple[int, int]:
    """Indices into ``couples`` of the two couples whose midpoints are farthest apart."""
    best: tuple[float, int, int] | None = None
    for x in range(len(couples)):
        for y in range(x + 1, len(couples)):
            d = dist(
                _couple_mid(pads_p, pads_n, couples[x]), _couple_mid(pads_p, pads_n, couples[y])
            )
            if best is None or d > best[0]:
                best = (d, x, y)
    assert best is not None
    return best[1], best[2]


def choose_pair_endpoints(
    pads_p: list[Pad], pads_n: list[Pad]
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Pick the pair's main-run endpoints by proximity pairing.

    Greedily couples each P pad with its nearest unclaimed N pad, then
    returns the two couples whose midpoints are farthest apart as
    ``((ip_a, in_a), (ip_b, in_b))`` index tuples into the input lists.
    Returns ``None`` when fewer than two couples exist (degenerate pad
    topology -- the pair cannot form a coupled main run).

    These two couples are also the ENDS of :func:`choose_pair_chain`.
    """
    if not pads_p or not pads_n:
        return None
    couples = _greedy_couples(pads_p, pads_n)
    if len(couples) < 2:
        return None
    x, y = _farthest_couples(pads_p, pads_n, couples)
    return couples[x], couples[y]


@dataclass(frozen=True)
class PairChain:
    """A diff pair's signal path as an ordered chain of couples (#6378).

    Attributes:
        segments: Coupled runs ``(couple_from, couple_to)`` in chain order,
            end A first.  Exactly one segment (the farthest-apart trunk)
            when the pair passes through no interior part.
        hops: ``(entry_couple, exit_couple)`` inside each interior part
            that has distinct entry and exit couples -- routed as two
            ordinary single-ended connections (P hop, N hop), never as a
            coupled attempt.
    """

    segments: list[tuple[Couple, Couple]]
    hops: list[tuple[Couple, Couple]]


def choose_pair_chain(pads_p: list[Pad], pads_n: list[Pad]) -> PairChain | None:
    """Order a pair's couples along its signal path (issue #6378).

    Couples are formed and the two ENDS chosen exactly as
    :func:`choose_pair_endpoints` does (greedy nearest P/N, then the
    farthest-apart couples).  Couples are then grouped by footprint (the
    ref of both pads).  A couple whose P and N sit on different refs (one
    test point per net, or the two nets running through two different
    parts) never forms a part: it stays off the chain and is routed as an
    extra, as before.  An *interior part* is a same-ref group holding neither end couple
    whose couple-midpoint centroid projects strictly between the ends
    (``0 < t < 1`` along the end-to-end axis).  Interior parts are visited
    in projection order; each is entered at its couple nearest the previous
    chain node and left at its couple nearest the next one (the next
    part's centroid, or the far end).  A part with a single couple (a
    termination resistor tapping the pair, board 06 LVDS) or whose entry
    and exit coincide is not a pass-through and stays on the trunk.

    Board 03 USB_D: J1.B6/B7 -> U2.3/U2.1 (segment), U2.3/U2.1 ->
    U2.4/U2.6 (hop through the pass-through ESD part), U2.4/U2.6 ->
    R4.1/R3.1 (segment) -- instead of a J1 -> R trunk that has to cross
    U2's own pads.  Couples on an end part other than the end couple (the
    second row of a USB-C receptacle) and parts outside the end span stay
    off the chain: the caller routes them as extras, as before.

    With no interior part the result is the single farthest-apart trunk,
    identical to :func:`choose_pair_endpoints`.  Returns ``None`` when
    fewer than two couples exist.
    """
    return _pair_chain(pads_p, pads_n, split_at_taps=False)


def choose_pair_tap_chain(pads_p: list[Pad], pads_n: list[Pad]) -> PairChain | None:
    """The pair's chain split at its tap parts too (issue #6535).

    :func:`choose_pair_chain` keeps a tap -- a part holding a single couple,
    such as a termination resistor straddling P and N (board 06 LVDS R1..R4)
    -- on the single trunk.  When that trunk declines with ``pair-no-path``
    (the tap's own P/N pads straddle the straight corridor, and every pad is
    an obstacle to the fat agent), the caller may retry the pair as this
    chain: each tap becomes a chain node, so the pair runs IC -> tap -> IC
    and every segment ends *at* the tap's couple instead of having to get
    past it.  A tap contributes no hop (entry == exit); pass-through parts
    are chained exactly as :func:`choose_pair_chain` chains them.

    Returns ``None`` when the result would not differ from
    :func:`choose_pair_chain` (no tap part lies between the ends), so a
    caller never retries the same chain.
    """
    chain = _pair_chain(pads_p, pads_n, split_at_taps=True)
    if chain is None or chain == _pair_chain(pads_p, pads_n, split_at_taps=False):
        return None
    return chain


def _pair_chain(pads_p: list[Pad], pads_n: list[Pad], *, split_at_taps: bool) -> PairChain | None:
    """Shared body of :func:`choose_pair_chain` / :func:`choose_pair_tap_chain`."""
    if not pads_p or not pads_n:
        return None
    couples = _greedy_couples(pads_p, pads_n)
    if len(couples) < 2:
        return None
    ix, iy = _farthest_couples(pads_p, pads_n, couples)
    end_a, end_b = couples[ix], couples[iy]
    trunk = PairChain(segments=[(end_a, end_b)], hops=[])

    def group(c: Couple) -> object:
        ref_p, ref_n = pads_p[c[0]].ref, pads_n[c[1]].ref
        if ref_p and ref_p == ref_n:
            return ("ref", ref_p)
        return ("couple", c)

    g_a, g_b = group(end_a), group(end_b)
    if g_a == g_b:
        return trunk
    groups: dict[object, list[Couple]] = {}
    for c in couples:
        g = group(c)
        # Only a same-ref group is a part the pair passes through.  A couple
        # whose P and N pads sit on different refs (one test point per net,
        # or D+ and D- running through two different parts) is not a part:
        # it stays an extra, exactly as before #6378.
        if g in (g_a, g_b) or g[0] != "ref":  # type: ignore[index]
            continue
        groups.setdefault(g, []).append(c)
    # A tap (one couple on the part: a termination resistor straddling P and N)
    # has no distinct entry and exit -- the trunk passes it on a clear axis, so
    # it stays on the trunk.  Only a part with two or more couples is a
    # pass-through the signal enters on one side and leaves on the other.
    if not split_at_taps:
        groups = {g: m for g, m in groups.items() if len(m) >= 2}
    if not groups:
        return trunk

    m_a = _couple_mid(pads_p, pads_n, end_a)
    m_b = _couple_mid(pads_p, pads_n, end_b)
    ax, ay = m_b[0] - m_a[0], m_b[1] - m_a[1]
    len2 = ax * ax + ay * ay
    if len2 <= _EPS:
        return trunk
    interior: list[tuple[float, Pt, list[Couple]]] = []
    for members in groups.values():
        mids = [_couple_mid(pads_p, pads_n, c) for c in members]
        cx = sum(m[0] for m in mids) / len(mids)
        cy = sum(m[1] for m in mids) / len(mids)
        t = ((cx - m_a[0]) * ax + (cy - m_a[1]) * ay) / len2
        if 0.0 < t < 1.0:
            interior.append((t, (cx, cy), members))
    if not interior:
        return trunk
    interior.sort(key=lambda e: e[0])

    def nearest(members: list[Couple], to: Pt) -> Couple:
        return min(members, key=lambda c: (dist(_couple_mid(pads_p, pads_n, c), to), c))

    segments: list[tuple[Couple, Couple]] = []
    hops: list[tuple[Couple, Couple]] = []
    prev = end_a
    for k, (_t, _centroid, members) in enumerate(interior):
        nxt = interior[k + 1][1] if k + 1 < len(interior) else m_b
        entry = nearest(members, _couple_mid(pads_p, pads_n, prev))
        exit_ = nearest(members, nxt)
        if exit_ == entry:
            if not split_at_taps:
                continue  # no distinct exit: a tap, not a pass-through
            # Issue #6535: a tap is a chain node with no hop -- the segment
            # before it ends at its couple and the next one starts there.
            segments.append((prev, entry))
            prev = entry
            continue
        segments.append((prev, entry))
        hops.append((entry, exit_))
        prev = exit_
    if not segments:
        return trunk
    segments.append((prev, end_b))

    # Issue #6698: launch each end from the couple of its end PART that faces
    # the chain.  The farthest-apart pick keeps the far row of a dual-row
    # receptacle (board 03 J1.B6/B7), walled in behind the near row (A6/A7)
    # whose pads leave no room for a trace between them.  Re-pick, on the
    # same part only, the couple nearest the adjacent chain node; the other
    # row stays an extra (tied to it by the caller).  Hops are unchanged.
    def facing(end: Couple, g_end: object, to: Pt) -> Couple:
        if g_end[0] != "ref":  # type: ignore[index]
            return end  # P and N on different refs: not a part
        members = [c for c in couples if group(c) == g_end]
        if len(members) < 2:
            return end
        return min(
            members,
            key=lambda c: (dist(_couple_mid(pads_p, pads_n, c), to), c != end, c),
        )

    new_a = facing(end_a, g_a, _couple_mid(pads_p, pads_n, segments[0][1]))
    new_b = facing(end_b, g_b, _couple_mid(pads_p, pads_n, segments[-1][0]))
    segments[0] = (new_a, segments[0][1])
    segments[-1] = (segments[-1][0], new_b)
    return PairChain(segments=segments, hops=hops)


def relocate_pair_anchor(
    obstacles: LatticeObstacleModel,
    pad_p: Pad,
    pad_n: Pad,
    toward: Pt,
    *,
    half_pitch: float,
    layers: tuple[int, ...],
    exempt: frozenset[int],
    pair_nets: set[int],
    keepouts: LatticeKeepoutMask | None = None,
    trace_half: float = 0.0,
    step: float = 0.05,
    max_shift: float = 4.0,
) -> Pt | None:
    """Relocated fat-centerline endpoint for a blocked couple (issue #6378).

    Returns ``None`` when the P/N midpoint is already a legal fat-agent
    point on some layer in ``layers`` (the common case -- nothing changes).
    Otherwise the midpoint lies inside a foreign pad's keep-out grown by
    ``half_pitch`` (board 03 U2: the GND/VBUS pad between the ESD part's
    D+/D- pads), so no fat stub can ever leave it (``pair-escape-*``).  The
    anchor then slides perpendicular to the P-N axis, on the side facing
    ``toward`` (the segment's other end), in ``step`` increments until, on
    one layer:

    * the anchor clears every non-``exempt`` pad grown by ``half_pitch``;
    * each nominal leg end (``anchor +/- half_pitch`` along the P-N axis)
      is reachable from its own pad by an axis dogleg that clears every
      foreign pad at single-trace clearance (the static half of the
      ``_pair_attach`` gate -- the real attach is still re-verified);
    * with ``keepouts`` (the pathfinder's rule-area mask, which also holds
      the #6401 pour-pad via reservations), the fat envelope at the anchor
      (``half_pitch + trace_half``) and both doglegs (``trace_half``) stay
      out of every track-blocking area.  Board 03 reserves U2.5's VBUS via
      site straight outward from the part, exactly where the centerline
      would otherwise start; the emitted legs would then fail
      ``pair-leg-keepout``.

    The shift distance is therefore computed from the actual pad geometry,
    never hard-coded.  Returns ``None`` too when nothing clears within
    ``max_shift`` (the caller keeps the midpoint and the pair declines or
    falls back exactly as before).
    """
    from ..quantize import dogleg_points

    mid = ((pad_p.x + pad_n.x) / 2.0, (pad_p.y + pad_n.y) / 2.0)
    if any(
        not pads_block_point_grown(obstacles, mid, layer, pair_nets, half_pitch, exempt=exempt)
        for layer in layers
    ):
        return None
    ux, uy = pad_p.x - pad_n.x, pad_p.y - pad_n.y
    axis_len = (ux * ux + uy * uy) ** 0.5
    if axis_len <= _EPS:
        return None
    ux, uy = ux / axis_len, uy / axis_len
    nx, ny = -uy, ux  # perpendicular to the P-N axis
    if (toward[0] - mid[0]) * nx + (toward[1] - mid[1]) * ny < 0:
        nx, ny = -nx, -ny

    def seg_ok(pad: Pad, a: Pt, b: Pt, layer: int) -> bool:
        if dist(a, b) <= _EPS:
            return True
        if pads_block_segment_grown(obstacles, a, b, layer, pair_nets, 0.0):
            return False
        return not (keepouts and keepouts.segment_blocked(a, b, layer, pad.net, trace_half))

    def attach_clear(pad: Pad, target: Pt, layer: int) -> bool:
        for axis_first in (False, True):
            poly = dogleg_points(pad.x, pad.y, target[0], target[1], axis_first=axis_first)
            if all(seg_ok(pad, a, b, layer) for a, b in zip(poly, poly[1:], strict=False)):
                return True
        return False

    steps = int(max_shift / step + 0.5)
    for k in range(1, steps + 1):
        shift = k * step
        anchor = (mid[0] + nx * shift, mid[1] + ny * shift)
        end_p = (anchor[0] + ux * half_pitch, anchor[1] + uy * half_pitch)
        end_n = (anchor[0] - ux * half_pitch, anchor[1] - uy * half_pitch)
        for layer in layers:
            if pads_block_point_grown(
                obstacles, anchor, layer, pair_nets, half_pitch, exempt=exempt
            ):
                continue
            if keepouts and any(
                keepouts.segment_blocked(anchor, anchor, layer, net, half_pitch + trace_half)
                for net in pair_nets
            ):
                continue
            if attach_clear(pad_p, end_p, layer) and attach_clear(pad_n, end_n, layer):
                return anchor
    return None


def sandwiched_pad_escape_sites(
    obstacles: LatticeObstacleModel,
    pad_p: Pad,
    pad_n: Pad,
    toward: Pt,
    *,
    pair_nets: set[int],
    routed_nets: set[int] | frozenset[int],
    via_radius: float,
    clearance_for: Callable[[int], float],
) -> list[tuple[Pad, Pt]]:
    """Via sites for routed pads sealed in by a coupled pair's start (#6697).

    When a pair segment starts at two pads of one part that straddle a
    foreign pad (board 03 U2: VBUS U2.5 between D+ U2.4 and D- U2.6), the
    P/N midpoint lies on that pad and :func:`relocate_pair_anchor` slides
    the fat centerline's endpoint just past its outer edge.  The pair start
    then caps the pad's only outward exit; its sides are the pair pads, and
    its inner end faces the part body.  A pour net used to get a #6401 via
    site there, which pushed the anchor further out.  A *routed* net (board
    03 VBUS since #6599) has none, so it was stranded.

    Returns ``(pad, site)`` for each single-layer pad of a ``routed_nets`` net
    (not a pair net) whose copper contains the P/N midpoint: ``site`` is a
    via centre just off the pad's outer edge on the side facing ``toward``
    (the same side the anchor slides to).  A site is kept only when that via
    clears every other-net pad (any layer: the barrel spans the stack) by
    ``clearance_for(net)``.
    """
    mid = ((pad_p.x + pad_n.x) / 2.0, (pad_p.y + pad_n.y) / 2.0)
    ux, uy = pad_p.x - pad_n.x, pad_p.y - pad_n.y
    axis_len = math.hypot(ux, uy)
    if axis_len <= _EPS:
        return []
    nx, ny = -uy / axis_len, ux / axis_len
    if (toward[0] - mid[0]) * nx + (toward[1] - mid[1]) * ny < 0:
        nx, ny = -nx, -ny
    from ..primitives import pad_half_extents

    def box_gap(other: Pad, pt: Pt) -> float:
        hw, hh = pad_half_extents(other)
        return math.hypot(max(abs(pt[0] - other.x) - hw, 0.0), max(abs(pt[1] - other.y) - hh, 0.0))

    out: list[tuple[Pad, Pt]] = []
    for idx in obstacles.pads_near(mid[0], mid[1], mid[0], mid[1]):
        pad = obstacles.pads[idx]
        if not pad.net or pad.net in pair_nets or pad.net not in routed_nets:
            continue
        if len(obstacles.pad_layer_indices[idx]) != 1:
            continue  # a through-hole pad already has its own barrel
        if box_gap(pad, mid) > _EPS:
            continue  # the midpoint is not on this pad's copper
        hw, hh = pad_half_extents(pad)
        # Support distance of the pad box along the outward normal.
        reach = abs(nx) * hw + abs(ny) * hh
        site = (pad.x + nx * (reach + via_radius), pad.y + ny * (reach + via_radius))
        need = via_radius + clearance_for(pad.net)
        span = need + 3.0
        if any(
            obstacles.pads[j].net != pad.net and box_gap(obstacles.pads[j], site) < need - 1e-6
            for j in obstacles.pads_near(
                site[0] - span, site[1] - span, site[0] + span, site[1] + span
            )
        ):
            continue
        out.append((pad, site))
    return out


# ---------------------------------------------------------------------------
# Equivalent-pin ties (issue #6698)
# ---------------------------------------------------------------------------

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
    copper :func:`.obstacles._pad_waiver_polygon` builds for the audit; a
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

    from .obstacles import _pad_waiver_polygon

    polygon = _pad_waiver_polygon(pad.x, pad.y, pad.width, pad.height, pad.shape, pad.rotation)
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
