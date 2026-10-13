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

**v1 pair agents are planar** (``allow_vias=False``): a pair that cannot
complete a coupled run on one layer DECLINES honestly, with a per-pair
reason, and never ships a short (#3906 discipline).  The coupled agent
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
    ref of both pads; a couple whose P and N sit on different refs is its
    own group).  An *interior part* is a group holding neither end couple
    whose couple-midpoint centroid projects strictly between the ends
    (``0 < t < 1`` along the end-to-end axis).  Interior parts are visited
    in projection order; each is entered at its couple nearest the previous
    chain node and left at its couple nearest the next one (the next
    part's centroid, or the far end).

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
        if g in (g_a, g_b):
            continue
        groups.setdefault(g, []).append(c)
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
        segments.append((prev, entry))
        if exit_ != entry:
            hops.append((entry, exit_))
        prev = exit_
    segments.append((prev, end_b))
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
