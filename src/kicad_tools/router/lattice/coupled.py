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
reason -- it never splits into uncoupled legs and never ships a short
(#3906 discipline).

This module holds the pure-geometry / model-query halves of the feature so
:mod:`.pathfinder` only gains a thin fat-agent branch and
:mod:`.obstacles` stays untouched (issue #4271 threads per-class widths
through those files in parallel).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .geometry import (
    Pt,
    Rect,
    dist,
    pt_in_rect,
    seg_pt_dist,
    seg_rect_intersect,
    seg_seg_dist,
)

if TYPE_CHECKING:
    from ..primitives import Pad
    from .obstacles import CommittedCopper, LatticeObstacleModel

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
    """

    key: object
    pair_name: str
    pad_p_a: Pad
    pad_n_a: Pad
    pad_p_b: Pad
    pad_n_b: Pad
    net_class: object
    pitch: float

    @property
    def mid_a(self) -> Pt:
        return ((self.pad_p_a.x + self.pad_n_a.x) / 2.0, (self.pad_p_a.y + self.pad_n_a.y) / 2.0)

    @property
    def mid_b(self) -> Pt:
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
    whole pair-net set treated as "self"."""
    gap = committed.copper_gap + extra
    for c, d, cnet, _hw in committed.copper[layer].query_seg(a, b, pad=gap):
        if cnet not in nets and seg_seg_dist(a, b, c, d) < gap - _EPS:
            return False
    vgap = committed.via_copper_gap + extra
    for point, vnet in committed.vias:
        if vnet not in nets and seg_pt_dist(a, b, point) < vgap - _EPS:
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
    gap = committed.copper_gap + extra
    for c, d, cnet, _hw in committed.copper[layer].query_seg(point, point, pad=gap):
        if cnet not in nets and seg_pt_dist(c, d, point) < gap - _EPS:
            return False
    vgap = committed.via_copper_gap + extra
    for vpt, vnet in committed.vias:
        if vnet not in nets and dist(point, vpt) < vgap - _EPS:
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


def choose_pair_endpoints(
    pads_p: list[Pad], pads_n: list[Pad]
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Pick the pair's main-run endpoints by proximity pairing.

    Greedily couples each P pad with its nearest unclaimed N pad, then
    returns the two couples whose midpoints are farthest apart as
    ``((ip_a, in_a), (ip_b, in_b))`` index tuples into the input lists.
    Returns ``None`` when fewer than two couples exist (degenerate pad
    topology -- the pair cannot form a coupled main run).
    """
    if not pads_p or not pads_n:
        return None
    cands = sorted(
        (dist((p.x, p.y), (n.x, n.y)), i, j)
        for i, p in enumerate(pads_p)
        for j, n in enumerate(pads_n)
    )
    couples: list[tuple[int, int]] = []
    used_p: set[int] = set()
    used_n: set[int] = set()
    for _d, i, j in cands:
        if i in used_p or j in used_n:
            continue
        couples.append((i, j))
        used_p.add(i)
        used_n.add(j)
    if len(couples) < 2:
        return None

    def mid(c: tuple[int, int]) -> Pt:
        p, n = pads_p[c[0]], pads_n[c[1]]
        return ((p.x + n.x) / 2.0, (p.y + n.y) / 2.0)

    best: tuple[float, int, int] | None = None
    for x in range(len(couples)):
        for y in range(x + 1, len(couples)):
            d = dist(mid(couples[x]), mid(couples[y]))
            if best is None or d > best[0]:
                best = (d, x, y)
    assert best is not None
    return couples[best[1]], couples[best[2]]
