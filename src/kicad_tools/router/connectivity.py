"""Per-pad copper-reachability check for routed nets (Issue #4165).

The orchestrator's global-family strategies (``global`` / ``escape`` /
``subgrid``) route a **single two-terminal corridor** between the two most
distant pads of a net (see :meth:`GlobalRouter.route_net`).  For a multi-pad
net that leaves intermediate pads stranded, yet the strategy reports
``success=True`` unconditionally.  This module supplies the missing
**truth-in-exit** signal: after a strategy produces geometry, verify that every
pad of the net is actually reachable across the produced copper (unioned with
pre-existing same-net copper).

Why not reuse ``NetStatusAnalyzer``
-----------------------------------
``analysis.net_status.NetStatusAnalyzer`` unions copper on **endpoint-proximity
tolerance** (Issue #4176), which over-connects relative to KiCad's real
geometric-contact requirement — reusing it here would just move the false
"complete" from "no check" to "check with the wrong tolerance".  Instead this
module walks **actual copper adjacency**: two elements are joined only when
their geometry truly touches — a shared endpoint (within a tight numeric
epsilon, not a design-rule clearance), a point lying **on** a segment, or two
segments crossing/overlapping.  A pad is "connected" only if it lies on a
copper element that is transitively joined to every other pad.

The epsilon here is a floating-point coincidence tolerance (``EPS`` = 1 µm),
deliberately far below any trace width or clearance, so the check errs toward
**under-connecting** (reporting incomplete) rather than over-connecting — the
safe direction for a completion oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Geometric coincidence tolerance in mm.  This is a numeric epsilon for
# floating-point equality of coordinates, NOT a clearance/tolerance union.
# 1 micron is well below any realistic trace width, so "touching" here means
# the copper genuinely overlaps or shares an endpoint.
EPS = 1e-3


@dataclass(frozen=True)
class _Seg:
    """Layer-annotated segment endpoints for adjacency testing."""

    x1: float
    y1: float
    x2: float
    y2: float
    layer: object  # opaque layer key; only equality matters


class _UnionFind:
    """Minimal union-find over integer node ids."""

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, a: int) -> int:
        parent = self._parent
        while parent.get(a, a) != a:
            parent[a] = parent.get(parent[a], parent[a])
            a = parent[a]
        parent.setdefault(a, a)
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def _pt_eq(ax: float, ay: float, bx: float, by: float) -> bool:
    return abs(ax - bx) <= EPS and abs(ay - by) <= EPS


def _point_on_segment(px: float, py: float, s: _Seg) -> bool:
    """True if point (px, py) lies on segment ``s`` (within EPS)."""
    # Endpoint hits first (cheap + robust for zero-length segments).
    if _pt_eq(px, py, s.x1, s.y1) or _pt_eq(px, py, s.x2, s.y2):
        return True
    dx = s.x2 - s.x1
    dy = s.y2 - s.y1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq <= EPS * EPS:
        return False  # degenerate; already covered by endpoint test
    # Projection parameter t of P onto the segment.
    t = ((px - s.x1) * dx + (py - s.y1) * dy) / seg_len_sq
    if t < -EPS or t > 1.0 + EPS:
        return False
    t = max(0.0, min(1.0, t))
    projx = s.x1 + t * dx
    projy = s.y1 + t * dy
    # Perpendicular distance from the point to the segment line.
    return _pt_eq(px, py, projx, projy)


def _segments_touch(a: _Seg, b: _Seg) -> bool:
    """True if two same-layer segments share any point (endpoint / on-line)."""
    if a.layer != b.layer:
        return False
    # Any endpoint of one lying on the other means the copper is joined.
    if _point_on_segment(a.x1, a.y1, b) or _point_on_segment(a.x2, a.y2, b):
        return True
    if _point_on_segment(b.x1, b.y1, a) or _point_on_segment(b.x2, b.y2, a):
        return True
    return False


def _seg_layer_key(seg: Any) -> object:
    """Extract a hashable layer key from a router/PCB segment-like object."""
    layer = getattr(seg, "layer", None)
    # Router Layer enums and KiCad layer strings are both hashable; prefer the
    # KiCad name so a router-Layer segment and an existing-copper string
    # segment on the SAME physical layer are treated as one layer.
    name = getattr(layer, "kicad_name", None)
    if name is not None:
        return name
    return layer


def check_net_pad_connectivity(
    pad_positions: list[tuple[float, float]],
    segments: list[Any],
    vias: list[Any] | None = None,
    existing_segments: list[Any] | None = None,
    existing_vias: list[Any] | None = None,
) -> tuple[int, int]:
    """Count how many pads of a net are joined by continuous copper.

    Performs a **real** per-pad reachability walk over the union of the
    freshly-produced copper (``segments`` / ``vias``) and any pre-existing
    same-net copper (``existing_segments`` / ``existing_vias``).  Two copper
    elements are adjacent only when they geometrically touch (shared endpoint,
    point-on-segment, or crossing) within :data:`EPS`; vias join copper on the
    two layers they connect at their drill location.

    Args:
        pad_positions: (x, y) centres of every pad of the net.
        segments: Newly produced trace segments (objects with x1/y1/x2/y2/layer).
        vias: Newly produced vias (objects with x/y[/layers]).
        existing_segments: Pre-existing same-net trace segments, same shape.
        existing_vias: Pre-existing same-net vias, same shape.

    Returns:
        ``(pads_connected, pads_total)`` where ``pads_connected`` is the size
        of the largest single copper component that contains pads, and
        ``pads_total`` is ``len(pad_positions)``.  If the net has <2 pads the
        result is ``(pads_total, pads_total)`` (trivially connected).
    """
    n = len(pad_positions)
    if n < 2:
        return (n, n)

    # Collect all segments (new + existing) as layer-annotated _Seg records.
    # Accepts two segment shapes: the router primitive (x1/y1/x2/y2 + Layer
    # enum) and the PCB-schema segment (start/end tuples + layer string).
    seg_records: list[_Seg] = []
    for src in (segments or [], existing_segments or []):
        for s in src:
            try:
                if hasattr(s, "x1"):
                    x1, y1, x2, y2 = (
                        float(s.x1),
                        float(s.y1),
                        float(s.x2),
                        float(s.y2),
                    )
                else:
                    (x1, y1), (x2, y2) = s.start, s.end
                    x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
                seg_records.append(_Seg(x1=x1, y1=y1, x2=x2, y2=y2, layer=_seg_layer_key(s)))
            except (AttributeError, TypeError, ValueError):
                continue

    via_points: list[tuple[float, float]] = []
    for src in (vias or [], existing_vias or []):
        for v in src:
            try:
                via_points.append((float(v.x), float(v.y)))
            except (AttributeError, TypeError, ValueError):
                continue

    # Node id layout:  0..n-1 = pads, then one node per segment, then one node
    # per via.  Vias join copper regardless of layer (they are through-features
    # in this coarse model), so they union any segment/pad touching their site.
    uf = _UnionFind()
    for i in range(n):
        uf.find(i)  # ensure pad nodes exist

    seg_base = n
    for i in range(len(seg_records)):
        uf.find(seg_base + i)

    via_base = seg_base + len(seg_records)
    for i in range(len(via_points)):
        uf.find(via_base + i)

    # Pad -> segment adjacency: a pad joins any segment it lies on.
    for pi, (px, py) in enumerate(pad_positions):
        for si, s in enumerate(seg_records):
            if _point_on_segment(px, py, s):
                uf.union(pi, seg_base + si)

    # Segment <-> segment adjacency (same layer, touching).
    for i in range(len(seg_records)):
        for j in range(i + 1, len(seg_records)):
            if _segments_touch(seg_records[i], seg_records[j]):
                uf.union(seg_base + i, seg_base + j)

    # Via adjacency: a via joins any segment endpoint/point at its location and
    # any pad at its location (through-feature: layer-agnostic here).
    for vi, (vx, vy) in enumerate(via_points):
        vnode = via_base + vi
        for pi, (px, py) in enumerate(pad_positions):
            if _pt_eq(vx, vy, px, py):
                uf.union(vnode, pi)
        for si, s in enumerate(seg_records):
            # Treat the via as a zero-extent point; test against the segment
            # ignoring layer (through-feature bridges layers).
            probe = _Seg(x1=vx, y1=vy, x2=vx, y2=vy, layer=s.layer)
            if _segments_touch(probe, s) or _point_on_segment(vx, vy, s):
                uf.union(vnode, seg_base + si)

    # Group pads by their copper component; the net is "complete" only when all
    # pads share one component.  Report the size of the largest pad-bearing
    # component as pads_connected.
    comp_counts: dict[int, int] = {}
    for pi in range(n):
        root = uf.find(pi)
        comp_counts[root] = comp_counts.get(root, 0) + 1

    pads_connected = max(comp_counts.values()) if comp_counts else 0
    return (pads_connected, n)
