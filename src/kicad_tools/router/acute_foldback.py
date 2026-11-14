"""Acute two-segment fold-back detection and repair (Issue #8919).

A *fold-back* is a degree-2 trace vertex where the track overshoots and turns
back on itself, leaving an open acute wedge of etchant-trapping copper.  JLC's
DFM tool flagged exactly this shape on the #8840 calibration board (all three
flagged vertices were two-segment 45-degree fold-backs away from pads and
vias), and ``kct check``'s ``sharp_trace_corner`` rule gates it, so every
fold-back the router emits is a board-gate failure.

Why the trace optimizer does not already fix it
-----------------------------------------------

:func:`~kicad_tools.router.optimizer.algorithms.shorten_detour_pass` replaces a
detouring chain with a shorter 45-degree path, but it is a **no-op below three
segments** (``if len(segments) < 3: return list(segments)``).  A two-segment
fold-back is exactly the sub-three case, so no amount of budget or clearance
headroom lets that pass repair one -- the gap is structural, which is why the
repair lives here and is invoked from
:meth:`~kicad_tools.router.core.Autorouter.cleanup_artifacts` instead (the last
pass to touch router copper, on every emission path).

Detection parity
----------------

The detector matches ``sharp_trace_corner``'s semantics so the router does not
repair copper the gate never flags, nor leave copper it does:

* angle in ``[MIN_CORNER_ANGLE_DEG, MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG)``
  -- i.e. ``[1, 89)`` degrees, with the constants **imported** from
  :mod:`kicad_tools.explain.checks.acid_trap`, never re-declared here;
* the vertex is not buried under same-net pad/via copper
  (:meth:`CopperDisc.covers`, inscribed circles);
* the wedge is **open** -- ``min(|VA|, |VB|) > s0`` with
  ``s0 = (w1 + w2) / (4 sin(theta / 2))`` -- plus the thin-leg-inside-wide-leg
  exemption (:func:`wedge_is_open`).

Repair
------

For a vertex ``V`` with shorter leg ``V->A`` and longer leg ``V->B``, the
vertex is moved to a point ``P`` on ``V->B`` at distance ``t``, so the copper
becomes ``A->P`` plus the retained stub ``P->B``.  Because ``P`` lies *on* the
old long leg, the corner angle at ``B`` is unchanged by construction.  Two
values of ``t`` are offered as candidates:

``t = |VA| * cos(theta)``
    ``AP`` comes out perpendicular to the **long** leg, so the corner at ``P``
    is a right angle.  Since ``cos(theta) < 1`` and ``|VA| <= |VB|``, this
    always fits inside the long leg -- it is the candidate that rescues the
    ``|VB| < sqrt(2) * |VA|`` geometries.

``t = |VA| / cos(theta)``
    ``AP`` comes out perpendicular to the **short** leg (the construction in
    issue #8919's body), leaving a 135-degree corner at ``P``.  At 45 degrees
    this is ``|VA| * sqrt(2)``, so it needs ``|VB| >= sqrt(2) * |VA|`` and is
    **infeasible** on two of the three fold-backs the issue measured.

When neither perpendicular move survives validation, the whole two-segment
detour collapses to the shortest 1-2 segment 45-degree path between ``A`` and
``B`` (:func:`~kicad_tools.router.optimizer.algorithms._optimal_path`, the same
candidate generator ``shorten_detour_pass`` uses).

Every candidate must, before it is applied:

* keep each new segment on the ``{0, 45, 90, 135}`` angle set as serialized
  (:func:`~kicad_tools.router.quantize.is_45_aligned` on 4-decimal
  displacements) and longer than ``min_segment_length``;
* not grow the net's copper length;
* pass ``path_is_clear`` for every new segment -- a vetoed repair is
  **rejected and counted**, never forced;
* keep every same-net pad/via disc that sat on the replaced copper on the
  replacement copper;
* introduce no new gating corner anywhere in the rewritten neighbourhood
  (:func:`gating_corner_keys` is recomputed and must not gain a vertex).

The module is pure geometry over :class:`~kicad_tools.router.primitives.Segment`
with the collision model and the pad/via discs **injected**, which is what keeps
the regression suite cheap and lets ``kct check``'s rule share the predicate
later.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ..explain.checks.acid_trap import (
    ANGLE_TOLERANCE_DEG,
    MIN_CORNER_ANGLE_DEG,
    MIN_TRACE_ANGLE_DEG,
)
from .primitives import Layer, Segment
from .quantize import is_45_aligned

__all__ = [
    "CopperDisc",
    "FoldBack",
    "FoldBackRepair",
    "FoldBackResult",
    "JUNCTION_TOL",
    "MAX_GATING_ANGLE_DEG",
    "find_foldbacks",
    "gating_corner_keys",
    "plan_foldback_repairs",
    "repair_foldbacks",
    "wedge_is_open",
]

#: Vertex coincidence tolerance in mm.  Matches
#: :meth:`kicad_tools.explain.checks.acid_trap.AcidTrapCheck._find_junction`
#: so the router and the ``sharp_trace_corner`` rule agree on what "the same
#: vertex" means.
JUNCTION_TOL = 0.01

#: A corner at or above this angle (degrees) is not a gating acid trap.
#: Derived from the ``acid_trap`` constants rather than restated: 89.0 deg.
MAX_GATING_ANGLE_DEG = MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG

#: Shortest replacement segment the pass will emit (mm).  Below this a new
#: segment is serialization noise rather than copper.
MIN_REPAIR_SEGMENT_LENGTH = 0.005

#: Hard cap on repair rounds per net/layer group, so a pathological geometry
#: cannot spin the pass.
_MAX_ROUNDS = 64

#: Candidate preference order (issue #8919 acceptance criteria): the issue
#: body's perpendicular construction first, then the perpendicular move that
#: rescues the ``|VB| < sqrt(2) * |VA|`` geometries it cannot express, and only
#: then the detour collapse -- which rewrites both legs instead of nudging one
#: vertex.  Fixed rather than shortest-first so a run is deterministic and the
#: router's own shape survives wherever a local edit suffices.
_CONSTRUCTION_ORDER = (
    "perpendicular-to-short-leg",
    "perpendicular-to-long-leg",
    "detour-collapse",
    "detour-collapse-reversed",
)

#: ``path_is_clear(x1, y1, x2, y2, layer, width, exclude_net) -> bool`` --
#: the signature of
#: :meth:`kicad_tools.router.optimizer.collision.VectorCollisionChecker.path_is_clear`.
PathIsClear = Callable[[float, float, float, float, Layer, float, int], bool]


@dataclass(frozen=True)
class CopperDisc:
    """Same-net pad/via copper that can bury a trace vertex.

    Pads contribute their **inscribed** circle (half the smaller dimension),
    so a vertex counts as buried only when it is certainly under copper --
    the same conservative choice ``sharp_trace_corner`` makes.

    Attributes:
        x: Disc centre X in mm.
        y: Disc centre Y in mm.
        radius: Inscribed radius in mm.
        layers: Copper layers the pad/via copper exists on.  A disc listing
            both outer layers is a through pad/via and covers every copper
            layer of the stack, inner ones included.
        net: Router net id the copper belongs to.
    """

    x: float
    y: float
    radius: float
    layers: frozenset[Layer]
    net: int

    def covers_layer(self, layer: Layer) -> bool:
        """Whether this disc's copper exists on *layer*."""
        if layer in self.layers:
            return True
        return Layer.F_CU in self.layers and Layer.B_CU in self.layers

    def covers(self, layer: Layer, point: tuple[float, float]) -> bool:
        """Whether this disc's copper on *layer* contains *point*."""
        if not self.covers_layer(layer):
            return False
        return math.dist(point, (self.x, self.y)) <= self.radius


@dataclass(frozen=True)
class FoldBack:
    """A gating two-segment acute fold-back.

    Attributes:
        net: Router net id.
        layer: Copper layer both legs run on.
        vertex: The fold-back vertex ``V``.
        angle_deg: Interior angle at ``V`` between the two legs.
        short: The shorter leg's segment.
        long: The longer leg's segment.
        a: Far end of the shorter leg (``A``).
        b: Far end of the longer leg (``B``).
    """

    net: int
    layer: Layer
    vertex: tuple[float, float]
    angle_deg: float
    short: Segment
    long: Segment
    a: tuple[float, float]
    b: tuple[float, float]

    @property
    def short_length(self) -> float:
        """``|VA|`` in mm."""
        return math.dist(self.vertex, self.a)

    @property
    def long_length(self) -> float:
        """``|VB|`` in mm."""
        return math.dist(self.vertex, self.b)


@dataclass(frozen=True)
class FoldBackRepair:
    """One accepted rewrite, in application order.

    ``removed`` segments are live objects to drop; ``added`` are their
    replacement.  A later repair in the same plan may remove a segment an
    earlier one added, so a consumer must apply the list **in order**.
    """

    net: int
    layer: Layer
    vertex: tuple[float, float]
    removed: tuple[Segment, ...]
    added: tuple[Segment, ...]
    construction: str


@dataclass
class FoldBackResult:
    """Outcome of a repair planning run.

    Attributes:
        repairs: Accepted rewrites, in application order.
        repaired: Number of gating fold-backs repaired.
        skipped: Number of gating fold-backs left in place because no
            candidate survived validation (clearance veto, anchor loss, or a
            rewrite that would have created a new acute corner).
    """

    repairs: list[FoldBackRepair] = field(default_factory=list)
    repaired: int = 0
    skipped: int = 0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _far_end(seg: Segment, junction: tuple[float, float]) -> tuple[float, float]:
    """The endpoint of *seg* that is not *junction*."""
    if abs(seg.x1 - junction[0]) < JUNCTION_TOL and abs(seg.y1 - junction[1]) < JUNCTION_TOL:
        return (seg.x2, seg.y2)
    return (seg.x1, seg.y1)


def _point_segment_dist(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Distance from *p* to the segment ``a``--``b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    len2 = dx * dx + dy * dy
    if len2 == 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _angle_at(
    seg1: Segment,
    seg2: Segment,
    junction: tuple[float, float],
) -> float | None:
    """Interior angle in degrees between *seg1* and *seg2* at *junction*."""
    p1, p2 = _far_end(seg1, junction), _far_end(seg2, junction)
    v1 = (p1[0] - junction[0], p1[1] - junction[1])
    v2 = (p2[0] - junction[0], p2[1] - junction[1])
    m1 = math.hypot(*v1)
    m2 = math.hypot(*v2)
    if m1 == 0.0 or m2 == 0.0:
        return None
    cos_a = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))


def wedge_is_open(
    seg1: Segment,
    seg2: Segment,
    junction: tuple[float, float],
    angle_deg: float,
) -> bool:
    """Whether the legs' copper outlines leave an unfilled acute notch.

    Judged from the copper outline rather than the centrelines: the facing
    edges of the two legs only separate at
    ``s0 = (w1 + w2) / (4 sin(theta / 2))`` along the legs, so a leg shorter
    than that (a width-filled hairpin) leaves solid copper, and a thin leg
    lying entirely inside a wider one adds no notch.  Mirrors
    ``sharp_trace_corner``'s ``_wedge_is_open`` exactly.
    """
    half = math.radians(angle_deg) / 2
    sin_half = math.sin(half)
    if sin_half <= 0.0:
        return False
    s0 = (seg1.width + seg2.width) / (4 * sin_half)
    p1, p2 = _far_end(seg1, junction), _far_end(seg2, junction)
    if min(math.dist(junction, p1), math.dist(junction, p2)) <= s0:
        return False
    for thin, wide in ((seg1, seg2), (seg2, seg1)):
        margin = wide.width / 2 - thin.width / 2
        if margin >= 0 and all(
            _point_segment_dist(pt, wide.start, wide.end) <= margin for pt in (thin.start, thin.end)
        ):
            return False
    return True


def _is_buried(
    discs: Sequence[CopperDisc],
    net: int,
    layer: Layer,
    point: tuple[float, float],
) -> bool:
    """Whether same-net pad/via copper on *layer* contains *point*."""
    return any(d.net == net and d.covers(layer, point) for d in discs)


# ---------------------------------------------------------------------------
# Vertex topology
# ---------------------------------------------------------------------------


class _Vertices:
    """Tolerance-bucketed vertex canonicalizer (O(n) over segment endpoints)."""

    def __init__(self, tolerance: float = JUNCTION_TOL) -> None:
        self._tol = tolerance
        self._points: list[tuple[float, float]] = []
        self._cells: dict[tuple[int, int], list[int]] = {}

    def index(self, x: float, y: float) -> int:
        """Canonical index of ``(x, y)``, creating one if it is new."""
        cx = int(math.floor(x / self._tol))
        cy = int(math.floor(y / self._tol))
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for k in self._cells.get((cx + i, cy + j), ()):
                    px, py = self._points[k]
                    if math.hypot(x - px, y - py) <= self._tol:
                        return k
        k = len(self._points)
        self._points.append((x, y))
        self._cells.setdefault((cx, cy), []).append(k)
        return k

    def point(self, k: int) -> tuple[float, float]:
        """The representative position of canonical vertex *k*."""
        return self._points[k]


def _adjacency(
    segments: Sequence[Segment],
    tolerance: float = JUNCTION_TOL,
) -> tuple[_Vertices, dict[int, list[Segment]]]:
    """Canonical vertices plus the segments incident to each.

    A zero-length segment (both endpoints canonicalizing to one vertex) is
    dropped: it carries no direction, so it is not part of any corner.
    """
    verts = _Vertices(tolerance)
    incident: dict[int, list[Segment]] = {}
    for seg in segments:
        k1 = verts.index(seg.x1, seg.y1)
        k2 = verts.index(seg.x2, seg.y2)
        if k1 == k2:
            continue
        incident.setdefault(k1, []).append(seg)
        incident.setdefault(k2, []).append(seg)
    return verts, incident


def _group_by_net_layer(
    segments: Iterable[Segment],
) -> dict[tuple[int, Layer], list[Segment]]:
    """Segments bucketed by ``(net, layer)``; net-0 copper is excluded."""
    groups: dict[tuple[int, Layer], list[Segment]] = {}
    for seg in segments:
        if seg.net == 0:
            continue
        groups.setdefault((seg.net, seg.layer), []).append(seg)
    return groups


def _vertex_key(point: tuple[float, float]) -> tuple[float, float]:
    """Rounded vertex key for before/after comparison (2 dp, as #8848 uses)."""
    return (round(point[0], 2), round(point[1], 2))


def gating_corner_keys(
    segments: Sequence[Segment],
    discs: Sequence[CopperDisc],
    net: int,
    layer: Layer,
    tolerance: float = JUNCTION_TOL,
) -> set[tuple[float, float]]:
    """Vertex keys of every gating acute corner in one ``(net, layer)`` group.

    Unlike :func:`find_foldbacks` this does **not** restrict to degree-2
    vertices: a branch junction with an open acute wedge is reported by
    ``sharp_trace_corner`` too, so a repair that created one there would be
    trading one gate failure for another.  Used as the "no new acute corner"
    guard, not as a repair target list.
    """
    verts, incident = _adjacency(segments, tolerance)
    keys: set[tuple[float, float]] = set()
    for k, segs in incident.items():
        if len(segs) < 2:
            continue
        point = verts.point(k)
        if _is_buried(discs, net, layer, point):
            continue
        for i, s1 in enumerate(segs):
            for s2 in segs[i + 1 :]:
                angle = _angle_at(s1, s2, point)
                if angle is None or not (MIN_CORNER_ANGLE_DEG <= angle < MAX_GATING_ANGLE_DEG):
                    continue
                if wedge_is_open(s1, s2, point, angle):
                    keys.add(_vertex_key(point))
                    break
            else:
                continue
            break
    return keys


def find_foldbacks(
    segments: Iterable[Segment],
    discs: Sequence[CopperDisc] = (),
    tolerance: float = JUNCTION_TOL,
) -> list[FoldBack]:
    """Gating two-segment acute fold-backs in *segments*.

    A fold-back qualifies when the vertex has degree exactly 2, both legs are
    same-net and same-layer, the interior angle is in ``[1, 89)`` degrees, the
    vertex is not buried under same-net pad/via copper, and the wedge is open
    (see :func:`wedge_is_open`).  These are precisely ``sharp_trace_corner``'s
    conditions narrowed to the two-segment case this pass repairs.

    Args:
        segments: Router copper to scan.  Net-0 copper is ignored.
        discs: Same-net pad/via copper that can bury a vertex.
        tolerance: Vertex coincidence tolerance in mm.

    Returns:
        Fold-backs ordered by net, layer name and vertex position, so a run is
        deterministic.
    """
    found: list[FoldBack] = []
    for (net, layer), group in sorted(
        _group_by_net_layer(segments).items(),
        key=lambda kv: (kv[0][0], kv[0][1].kicad_name),
    ):
        verts, incident = _adjacency(group, tolerance)
        for k in sorted(incident, key=lambda i: _vertex_key(verts.point(i))):
            segs = incident[k]
            if len(segs) != 2:
                continue
            vertex = verts.point(k)
            s1, s2 = segs
            angle = _angle_at(s1, s2, vertex)
            if angle is None or not (MIN_CORNER_ANGLE_DEG <= angle < MAX_GATING_ANGLE_DEG):
                continue
            if _is_buried(discs, net, layer, vertex):
                continue
            if not wedge_is_open(s1, s2, vertex, angle):
                continue
            a, b = _far_end(s1, vertex), _far_end(s2, vertex)
            if math.dist(vertex, a) <= math.dist(vertex, b):
                short, long_, far_a, far_b = s1, s2, a, b
            else:
                short, long_, far_a, far_b = s2, s1, b, a
            found.append(
                FoldBack(
                    net=net,
                    layer=layer,
                    vertex=vertex,
                    angle_deg=angle,
                    short=short,
                    long=long_,
                    a=far_a,
                    b=far_b,
                )
            )
    return found


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


def _retarget(
    template: Segment,
    p: tuple[float, float],
    q: tuple[float, float],
) -> Segment:
    """Copy of *template* (width, layer, net) running from *p* to *q*."""
    return Segment(
        x1=p[0],
        y1=p[1],
        x2=q[0],
        y2=q[1],
        width=template.width,
        layer=template.layer,
        net=template.net,
        net_name=template.net_name,
    )


def _round4(point: tuple[float, float]) -> tuple[float, float]:
    """*point* snapped to the 0.1 um serialization quantum."""
    return (round(point[0], 4), round(point[1], 4))


def _perpendicular_candidates(
    fb: FoldBack,
) -> list[tuple[str, list[Segment]]]:
    """Vertex-move candidates: ``A->P`` plus the retained stub ``P->B``.

    ``P`` sits on the long leg at ``t`` from ``V``, so the corner angle at
    ``B`` is untouched by construction.  ``t = |VA| cos(theta)`` makes ``AP``
    perpendicular to the long leg (a right angle at ``P``, always inside the
    long leg); ``t = |VA| / cos(theta)`` makes it perpendicular to the short
    leg (135 degrees at ``P``, the issue body's construction, feasible only
    when ``|VB| >= |VA| / cos(theta)``).
    """
    v, a, b = fb.vertex, fb.a, fb.b
    la, lb = fb.short_length, fb.long_length
    if la <= 0.0 or lb <= 0.0:
        return []
    ux, uy = (a[0] - v[0]) / la, (a[1] - v[1]) / la
    wx, wy = (b[0] - v[0]) / lb, (b[1] - v[1]) / lb
    cos_theta = ux * wx + uy * wy
    if cos_theta <= 1e-9:
        # A right or obtuse vertex is not a fold-back; nothing to construct.
        return []

    out: list[tuple[str, list[Segment]]] = []
    for name, t in (
        ("perpendicular-to-long-leg", la * cos_theta),
        ("perpendicular-to-short-leg", la / cos_theta),
    ):
        if t <= MIN_REPAIR_SEGMENT_LENGTH or t > lb - MIN_REPAIR_SEGMENT_LENGTH:
            continue
        p = _round4((v[0] + t * wx, v[1] + t * wy))
        out.append(
            (
                name,
                [_retarget(fb.short, _round4(a), p), _retarget(fb.long, p, _round4(b))],
            )
        )
    return out


def _collapse_candidates(fb: FoldBack) -> list[tuple[str, list[Segment]]]:
    """Detour-collapse candidates: the 1-2 segment 45-degree path ``A``->``B``.

    Both legs are replaced wholesale.  Reuses
    :func:`~kicad_tools.router.optimizer.algorithms._optimal_path` -- the same
    candidate generator ``shorten_detour_pass`` uses -- rather than growing a
    second 45-degree path builder, and offers the reversed traversal too since
    the two differ when ``A`` and ``B`` are not 45-degree aligned.
    """
    from .optimizer.algorithms import _optimal_path, _reverse_path
    from .optimizer.config import OptimizationConfig

    cfg = OptimizationConfig()
    a, b = _round4(fb.a), _round4(fb.b)
    forward = _optimal_path(a, b, fb.long, cfg)
    if not forward:
        return []
    out: list[tuple[str, list[Segment]]] = [("detour-collapse", forward)]
    backward = _reverse_path(_optimal_path(b, a, fb.long, cfg))
    if len(forward) > 1 and len(backward) > 1:
        out.append(("detour-collapse-reversed", backward))
    return out


# ---------------------------------------------------------------------------
# Validation and planning
# ---------------------------------------------------------------------------


def _segment_length(seg: Segment) -> float:
    return math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)


def _candidate_is_well_formed(
    candidate: Sequence[Segment],
    replaced: Sequence[Segment],
    min_segment_length: float,
) -> bool:
    """Shape checks that need no collision model or net context."""
    if not candidate:
        return False
    for seg in candidate:
        if _segment_length(seg) <= min_segment_length:
            return False
        if not is_45_aligned(
            round(seg.x2, 4) - round(seg.x1, 4), round(seg.y2, 4) - round(seg.y1, 4)
        ):
            return False
    old_len = sum(_segment_length(s) for s in replaced)
    new_len = sum(_segment_length(s) for s in candidate)
    return new_len <= old_len + JUNCTION_TOL


def _anchors_preserved(
    candidate: Sequence[Segment],
    replaced: Sequence[Segment],
    discs: Sequence[CopperDisc],
    net: int,
    layer: Layer,
) -> bool:
    """Whether every same-net pad/via on the old copper is still on the new.

    A pad sitting mid-leg (a daisy chain, or the escape via the long leg was
    reaching) must stay on the copper, exactly as ``shorten_detour_pass``
    requires of its whole-chain rewrites.
    """
    reach = max(s.width for s in replaced) / 2 + JUNCTION_TOL
    for disc in discs:
        if disc.net != net or not disc.covers_layer(layer):
            continue
        centre = (disc.x, disc.y)
        on_old = any(_point_segment_dist(centre, s.start, s.end) <= reach for s in replaced)
        if not on_old:
            continue
        if not any(_point_segment_dist(centre, s.start, s.end) <= reach for s in candidate):
            return False
    return True


def _plan_group(
    net: int,
    layer: Layer,
    group: list[Segment],
    discs: Sequence[CopperDisc],
    path_is_clear: PathIsClear,
    is_mutable: Callable[[Segment], bool] | None,
    tolerance: float,
    min_segment_length: float,
) -> tuple[list[FoldBackRepair], int, int]:
    """Plan repairs for one ``(net, layer)`` group.

    Returns ``(repairs, repaired, skipped)``.  Re-detects after each accepted
    rewrite so a repair that shares a leg with another fold-back is judged
    against the copper as it then stands.
    """
    working = list(group)
    repairs: list[FoldBackRepair] = []
    repaired = 0
    unrepairable: set[tuple[float, float]] = set()

    for _ in range(_MAX_ROUNDS):
        before = gating_corner_keys(working, discs, net, layer, tolerance)
        candidates_fb = [
            fb
            for fb in find_foldbacks(working, discs, tolerance)
            if _vertex_key(fb.vertex) not in unrepairable
        ]
        if not candidates_fb:
            break
        fb = candidates_fb[0]
        key = _vertex_key(fb.vertex)
        replaced = [fb.short, fb.long]
        if is_mutable is not None and not all(is_mutable(s) for s in replaced):
            # A leg is copper the router may not rewrite (``--preserve-existing``
            # or placement-preserved).  The gate still flags the vertex, so it
            # is reported as unrepairable rather than silently ignored.
            unrepairable.add(key)
            continue
        others = [s for s in working if s is not fb.short and s is not fb.long]

        accepted: tuple[str, list[Segment]] | None = None
        # Fixed preference order, not shortest-first: a perpendicular vertex
        # move is a local edit that keeps the router's shape and leaves the
        # corner at ``B`` untouched, so it is tried before the collapse
        # rewrites both legs wholesale.  Deterministic by construction.
        proposals = sorted(
            _perpendicular_candidates(fb) + _collapse_candidates(fb),
            key=lambda nc: _CONSTRUCTION_ORDER.index(nc[0]),
        )
        for name, cand in proposals:
            if not _candidate_is_well_formed(cand, replaced, min_segment_length):
                continue
            if not all(
                path_is_clear(s.x1, s.y1, s.x2, s.y2, s.layer, s.width, s.net) for s in cand
            ):
                continue
            if not _anchors_preserved(cand, replaced, discs, net, layer):
                continue
            after = gating_corner_keys(others + cand, discs, net, layer, tolerance)
            if key in after or not after <= before:
                continue
            accepted = (name, cand)
            break

        if accepted is None:
            unrepairable.add(key)
            continue

        name, cand = accepted
        repairs.append(
            FoldBackRepair(
                net=net,
                layer=layer,
                vertex=fb.vertex,
                removed=tuple(replaced),
                added=tuple(cand),
                construction=name,
            )
        )
        working = others + cand
        repaired += 1

    return repairs, repaired, len(unrepairable)


def plan_foldback_repairs(
    segments: Iterable[Segment],
    *,
    discs: Sequence[CopperDisc] = (),
    path_is_clear: PathIsClear | None,
    is_mutable: Callable[[Segment], bool] | None = None,
    tolerance: float = JUNCTION_TOL,
    min_segment_length: float = MIN_REPAIR_SEGMENT_LENGTH,
) -> FoldBackResult:
    """Plan fold-back repairs over *segments* without mutating anything.

    Like ``shorten_detour_pass``, a whole-corner rewrite is only safe with a
    collision model, so the pass is a **no-op** when *path_is_clear* is
    ``None``.

    Args:
        segments: Copper to consider.  Net-0 copper is ignored.  Pass fixed
            copper (``--preserve-existing``, placement-preserved) in here too
            alongside *is_mutable*: it has to be visible so a vertex where
            router copper meets it is seen at its true degree, and so the
            "no new acute corner" guard judges the whole neighbourhood.
        discs: Same-net pad/via copper -- used both to exempt buried vertices
            and as the anchors a rewrite must keep covering.
        path_is_clear: Collision-check callback with
            :data:`PathIsClear`'s signature (``None`` = no-op).
        is_mutable: Predicate naming the segments the caller may rewrite.
            ``None`` means every segment is rewritable.  A fold-back with an
            immutable leg is counted in ``skipped``, since the gate still
            flags it.
        tolerance: Vertex coincidence tolerance in mm.
        min_segment_length: Shortest replacement segment to emit, in mm.

    Returns:
        A :class:`FoldBackResult` whose ``repairs`` must be applied in order.
    """
    result = FoldBackResult()
    if path_is_clear is None:
        return result
    for (net, layer), group in sorted(
        _group_by_net_layer(segments).items(),
        key=lambda kv: (kv[0][0], kv[0][1].kicad_name),
    ):
        repairs, repaired, skipped = _plan_group(
            net,
            layer,
            group,
            discs,
            path_is_clear,
            is_mutable,
            tolerance,
            min_segment_length,
        )
        result.repairs.extend(repairs)
        result.repaired += repaired
        result.skipped += skipped
    return result


def repair_foldbacks(
    segments: Iterable[Segment],
    *,
    discs: Sequence[CopperDisc] = (),
    path_is_clear: PathIsClear | None,
    is_mutable: Callable[[Segment], bool] | None = None,
    tolerance: float = JUNCTION_TOL,
    min_segment_length: float = MIN_REPAIR_SEGMENT_LENGTH,
) -> tuple[list[Segment], FoldBackResult]:
    """Apply :func:`plan_foldback_repairs` to a flat segment list.

    The convenience wrapper the regression suite uses; the router applies the
    plan itself so each replacement lands on the right
    :class:`~kicad_tools.router.primitives.Route`.

    Returns:
        ``(segments, result)`` -- the rewritten copper (untouched segments are
        the same objects) and the planning outcome.
    """
    out = list(segments)
    result = plan_foldback_repairs(
        out,
        discs=discs,
        path_is_clear=path_is_clear,
        is_mutable=is_mutable,
        tolerance=tolerance,
        min_segment_length=min_segment_length,
    )
    for repair in result.repairs:
        dropped = {id(s) for s in repair.removed}
        out = [s for s in out if id(s) not in dropped]
        out.extend(repair.added)
    return out, result
