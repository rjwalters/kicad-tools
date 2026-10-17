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

Issue #6837 (epic #6441 slice 2c-i) moved the engine-free pair planning --
the chain builders (:func:`choose_pair_chain`, :func:`choose_pair_tap_chain`,
:class:`PairChain`) and the equivalent-pin tie planner
(:func:`plan_equivalent_pin_tie`, :class:`PlannedTie`) -- to
:mod:`kicad_tools.router.pairplan`.  They are re-exported here as deprecated
aliases so in-flight callers keep working; new code should import them from
``router.pairplan``.  The re-exports go away with P6 (#6448).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..pairplan.chain import (  # noqa: F401 -- deprecated re-exports (#6837)
    Couple,
    PairChain,
    _couple_mid,
    _farthest_couples,
    _greedy_couples,
    _pair_chain,
    choose_pair_chain,
    choose_pair_endpoints,
    choose_pair_tap_chain,
)
from ..pairplan.tie import (  # noqa: F401 -- deprecated re-exports (#6837)
    _TIE_WIDTH_STEP,
    MAX_TIE_STUB_MM,
    PlannedTie,
    _tie_pad_gap,
    plan_equivalent_pin_tie,
)
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


def trim_end_reversals(points: list[Pt]) -> list[Pt]:
    """Trim a collinear overshoot-and-return at either end of a polyline.

    The lattice search can reach a node beyond the goal anchor and append a
    goal stub that returns along the same line, leaving a 180-degree
    reversal on the last (or first) edge.  :func:`offset_polyline` cannot
    offset that, so replace the two reversing steps ``a -> b -> p`` with the
    single step ``a -> p`` (``p`` lies on segment ``a``-``b``).  When ``p``
    coincides with ``a`` the whole out-and-back spur is dropped.

    Only the first and last two steps are considered: a reversal in the
    middle of the path is a genuine defect and must still decline.  The
    caller re-verifies the trimmed centerline like any other.
    """
    pts = merge_collinear_points(points)

    def trim_tail(seq: list[Pt]) -> list[Pt]:
        while len(seq) >= 3:
            a, b, p = seq[-3], seq[-2], seq[-1]
            d1 = (b[0] - a[0], b[1] - a[1])
            d2 = (p[0] - b[0], p[1] - b[1])
            cross = d1[0] * d2[1] - d1[1] * d2[0]
            dot = d1[0] * d2[0] + d1[1] * d2[1]
            if abs(cross) > _EPS or dot >= 0.0 or dist(b, p) > dist(a, b) + _EPS:
                break
            # p is on segment a-b: drop the overshoot vertex b (and p too
            # when it lands back on a, so the spur vanishes entirely).
            seq = seq[:-2] if dist(a, p) <= _EPS else [*seq[:-2], p]
        return seq

    pts = trim_tail(pts)
    pts = trim_tail(pts[::-1])[::-1]
    return merge_collinear_points(pts)


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


def _seg_rect_entry(a: Pt, b: Pt, rect: Rect) -> float | None:
    """Smallest ``t`` in ``[0, 1]`` where ``a + t (b - a)`` is inside ``rect``."""
    x0, y0, x1, y1 = rect
    t0, t1 = 0.0, 1.0
    for p, q, lo, hi in ((a[0], b[0], x0, x1), (a[1], b[1], y0, y1)):
        d = q - p
        if abs(d) <= _EPS:
            if p < lo - _EPS or p > hi + _EPS:
                return None
            continue
        ta, tb = (lo - p) / d, (hi - p) / d
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return None
    return t0


#: Shapes whose copper the kernel's pad builder models exactly.
_CLIP_SHAPES = frozenset({"rect", "roundrect", "circle", "oval", "obround"})

#: How far inside the copper a clipped leg end must sit (mm).  A cut landing
#: on the outline itself (a box corner that happens to touch a circle) is
#: refused, so a float round-off can never turn it into an open circuit.
_MIN_END_DEPTH = 0.01


def _entry_in_copper(entry: Pt, pad: Pad) -> bool:
    """True when ``entry`` lies inside ``pad``'s real copper (issue #6959).

    "Inside" means at least :data:`_MIN_END_DEPTH` in from the outline.

    The cut is computed on the pad's axis-aligned bounding box, which holds
    area with no copper for a circle, an oval or a rounded corner.  This test
    uses the clearance kernel's pad builder, which models the real outline:
    a rotated rectangle, a disc, a stadium, or a roundrect whose corner radius
    is ``roundrect_rratio * min(width, height)``.  One difference from
    :func:`~kicad_tools.router.clearance_shapes.pad_shape`: a roundrect with
    no file ratio is measured here at KiCad's largest ratio (0.5), not as its
    full rectangle.  The full rectangle is the safe choice for a clearance
    test and the unsafe one for a containment test, because it would accept
    a point in a corner that may hold no copper.  Shapes the builder does not
    model exactly (``custom``, ``trapezoid``, ...) never contain the point.
    """
    from ..clearance_kernel import KVia, copper_gap, make_pad
    from ..clearance_shapes import pad_kernel_params

    if pad.shape not in _CLIP_SHAPES:
        return False
    if pad.shape == "roundrect" and getattr(pad, "roundrect_rratio", None) is None:
        shape, rratio = "roundrect", 0.5
    else:
        shape, rratio = pad_kernel_params(pad)
    # Shrink the pad by the margin on every side and test plain membership
    # (the kernel's point distance is 0 anywhere inside, so it has no depth
    # to compare).  For a roundrect the shrunk pad keeps the same ratio, so
    # its corners come out slightly rounder than a true inset: it stays
    # inside the true inset, which is the safe direction.
    m = 2.0 * _MIN_END_DEPTH
    kpad = make_pad(shape, pad.width - m, pad.height - m, rratio, pad.rotation, pad.x, pad.y)
    return copper_gap(kpad, KVia(x=entry[0], y=entry[1], diameter=0.0)) <= 1e-9


def clip_to_pad_entry(points: list[Pt], pad: Pad, trace_w: float) -> list[Pt]:
    """Trim the pad-end tail of ``points`` where the track enters ``pad``.

    ``points`` ends at the pad (its last point is the pad position).  The
    polyline is cut at the first point, walking from the far end, that lies
    inside the pad rectangle inset by ``trace_w / 2`` along its travel axis --
    the track end is then inside the pad copper and the in-pad tail (and any dogleg
    within it) is dropped (issue #6959).  An axis no wider than the trace
    keeps the pad's full extent.  Returned unchanged when neither axis can
    inset (``vpad`` gateway pads have zero size), when the far end is
    already inside the pad, or when no entry exists.
    """
    from ..primitives import pad_half_extents

    # A non-cardinal residual rotation makes the AABB far larger than the
    # copper, so a cut on it can land off the pad (an open circuit): keep
    # the unclipped leg to the pad centre.
    if pad.rotation % 90.0 > _EPS and 90.0 - pad.rotation % 90.0 > _EPS:
        return points
    half_w, half_h = pad_half_extents(pad)
    inset = trace_w / 2.0
    # The track end is inset only along the segment's travel axis: a leg
    # running alongside the pad's edge region (inside the copper, outside the
    # all-round inset) must still be cut at the pad's far edge.
    ix = inset if half_w - inset > _EPS else 0.0
    iy = inset if half_h - inset > _EPS else 0.0
    if (ix == 0.0 and iy == 0.0) or len(points) < 2:
        return points
    x0, y0, x1, y1 = pad.x - half_w, pad.y - half_h, pad.x + half_w, pad.y + half_h
    if pt_in_rect(points[0], (x0 + ix, y0 + iy, x1 - ix, y1 - iy)):
        return points
    for i, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        if abs(b[0] - a[0]) >= abs(b[1] - a[1]):
            rect = (x0 + ix, y0, x1 - ix, y1)
        else:
            rect = (x0, y0 + iy, x1, y1 - iy)
        t = _seg_rect_entry(a, b, rect)
        if t is None:
            continue
        entry = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        if not _entry_in_copper(entry, pad):
            return points
        clipped = [*points[: i + 1], entry]
        if sum(dist(u, v) for u, v in zip(clipped, clipped[1:], strict=False)) <= _EPS:
            return points
        return clipped
    return points


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
    from ..pairplan.attach import anchor_shift_candidates, dogleg_attach_candidates

    mid = ((pad_p.x + pad_n.x) / 2.0, (pad_p.y + pad_n.y) / 2.0)
    if any(
        not pads_block_point_grown(obstacles, mid, layer, pair_nets, half_pitch, exempt=exempt)
        for layer in layers
    ):
        return None

    def seg_ok(pad: Pad, a: Pt, b: Pt, layer: int) -> bool:
        if dist(a, b) <= _EPS:
            return True
        if pads_block_segment_grown(obstacles, a, b, layer, pair_nets, 0.0):
            return False
        return not (keepouts and keepouts.segment_blocked(a, b, layer, pad.net, trace_half))

    def attach_clear(pad: Pad, target: Pt, layer: int) -> bool:
        for poly in dogleg_attach_candidates((pad.x, pad.y), target):
            if all(seg_ok(pad, a, b, layer) for a, b in zip(poly, poly[1:], strict=False)):
                return True
        return False

    for anchor, end_p, end_n in anchor_shift_candidates(
        pad_p, pad_n, toward, half_pitch=half_pitch, step=step, max_shift=max_shift
    ):
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
    from ..pairplan.attach import sandwich_via_site
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
        site = sandwich_via_site(pad, (nx, ny), via_radius, reach)
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
