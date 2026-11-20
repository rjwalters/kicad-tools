"""Start points on a net's own kept copper (issue #9636).

``kct route --complete`` keeps the copper a board already carries and asks the
lattice engine only for the missing joins.  A join that leaves a pad which is
*already* connected -- a fine-pitch pin with a (narrower than class width)
escape track on it -- used to start from the pad centre again, at the net's
class width, and decline when that width cannot leave the pin even though a
track already does.

This module turns the kept routes of one net into **anchors**: the points of
that copper a new join may start from, with the width the copper already has
there.  :meth:`LatticePathfinder._escape_stubs` prefers them over re-escaping
the pad.  Pure geometry, no routing state.

Connectivity is physical (Judge review of #9636): a pad seeds the copper that
actually touches its own outline -- measured with the geometry kernel, so a
circular pad's bounding-box corners are air, not copper -- and a via hops
layers only over the span it is physically drilled through
(:attr:`~kicad_tools.router.primitives.Via.drilled_layers`), so a blind or
buried via never joins copper on layers it does not reach.  The segment graph
is built once per net, not once per pad.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..clearance_shapes import pad_kernel_params
from ..layers import Layer
from ..primitives import Pad, pad_half_extents
from . import kernel_adapter as ka

Pt = tuple[float, float]

# Endpoints closer than this are the same copper joint (mm).
_JOINT_TOL_MM = 0.01
# Copper closer than this edge to edge counts as touching (mm).  A numeric
# epsilon in the spirit of ``connectivity.EPS``, far below any real air gap
# (the #9636 review probe shipped 0.0107 mm of air and must stay open).
_CONTACT_TOL_MM = 1e-3
# Per-pad cap so a heavily routed island cannot multiply the stub scans.
MAX_ANCHORS_PER_PAD = 12


@dataclass(frozen=True)
class KeptAnchor:
    """A point on already-routed copper of the net a new link may start from."""

    layer: int
    point: Pt
    width: float


def pad_key(pad: Pad) -> tuple[str, str]:
    """Identity of a pad across object copies: ``(ref, pin)``."""
    return (pad.ref, pad.pin)


def _same(a: Pt, b: Pt) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= _JOINT_TOL_MM


def _mark(p: Pt) -> tuple[int, int]:
    return (round(p[0] / _JOINT_TOL_MM), round(p[1] / _JOINT_TOL_MM))


def _via_span(v: Any, layer_index: Callable[[Any], int | None]) -> tuple[int, int] | None:
    """The lattice-index span a via is physically drilled through, or ``None``.

    A micro, blind or buried via is drilled only through its declared span;
    every other via is a through hole (see
    :attr:`~kicad_tools.router.primitives.Via.drilled_layers`).  A via whose
    span maps to no routing-stack layer cannot join anything in-stack.
    """
    span = getattr(v, "drilled_layers", None) or getattr(v, "layers", None)
    if not span:
        span = (Layer.F_CU, Layer.B_CU)
    ids = [layer_index(lyr) for lyr in span[:2]]
    known = [i for i in ids if i is not None]
    if not known:
        return None
    return (min(known), max(known))


def kept_copper_anchors(
    pads: Iterable[Pad],
    routes: Sequence[Any],
    layer_index: Callable[[Any], int | None],
) -> dict[tuple[str, str], list[KeptAnchor]]:
    """Anchors on the copper already attached to each pad of ONE net.

    ``routes`` are the net's kept routes (``segments`` / ``vias``);
    ``layer_index`` maps a layer (segment layers and via span layers) to the
    lattice layer index (or ``None`` when the layer is outside the routing
    stack).  For each pad the copper reachable from it -- segments chained end
    to end, hopping layers at vias whose drilled span covers both sides -- is
    collected and every segment endpoint that is NOT on the pad's own copper
    becomes an anchor (an endpoint on another pad of the net is the far end of
    an existing join, and a legitimate place to start from).  A pad with no
    copper attached has no entry.

    A segment joins the pad's island only when its copper physically touches
    the pad's real outline on a layer the pad lives on (the geometry kernel
    measures circles as discs and ovals as stadiums), and a via joins two
    segments only when its drilled span covers both segment layers.
    """
    segs: list[tuple[int, Pt, Pt, float]] = []
    vias: list[tuple[Pt, int, int]] = []
    for route in routes:
        for s in getattr(route, "segments", ()) or ():
            layer = layer_index(s.layer)
            if layer is None:
                continue
            a, b = (float(s.x1), float(s.y1)), (float(s.x2), float(s.y2))
            if _same(a, b):
                continue
            segs.append((layer, a, b, float(s.width)))
        for v in getattr(route, "vias", ()) or ():
            span = _via_span(v, layer_index)
            if span is not None:
                vias.append(((float(v.x), float(v.y)), span[0], span[1]))
    if not segs:
        return {}

    # Segment connectivity, built once per net (not once per pad): union-find
    # over same-layer shared endpoints, then over vias at a shared endpoint
    # whose span covers every joined segment's layer.
    parent = list(range(len(segs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    by_endpoint: dict[tuple[int, int, int], list[int]] = {}
    ends_by_point: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for i, (layer, a, b, _w) in enumerate(segs):
        for p in (a, b):
            mark = (layer, *_mark(p))
            by_endpoint.setdefault(mark, []).append(i)
            ends_by_point.setdefault(_mark(p), []).append((layer, i))
    for ids in by_endpoint.values():
        for j in ids[1:]:
            union(ids[0], j)
    for xy, lo, hi in vias:
        hop = [i for layer, i in ends_by_point.get(_mark(xy), ()) if lo <= layer <= hi]
        for j in hop[1:]:
            union(hop[0], j)

    out: dict[tuple[str, str], list[KeptAnchor]] = {}
    for pad in pads:
        shape, rratio = pad_kernel_params(pad)
        outline = ka.pad_outline(shape, pad.width, pad.height, rratio, pad.rotation, (pad.x, pad.y))
        pad_layer = None if pad.through_hole else layer_index(pad.layer)
        roots: set[int] = set()
        for i, (layer, a, b, w) in enumerate(segs):
            if pad_layer is not None and layer != pad_layer:
                continue
            if ka.gap(ka.trace(a, b, w / 2.0), outline) <= _CONTACT_TOL_MM:
                roots.add(find(i))
        if not roots:
            continue
        anchors: list[KeptAnchor] = []
        marks: set[tuple[int, int, int]] = set()
        hw, hh = pad_half_extents(pad)
        for i in range(len(segs)):
            if find(i) not in roots:
                continue
            layer, a, b, w = segs[i]
            for p in (a, b):
                # Cheap AABB pre-filter, then the real outline: an endpoint on
                # the pad's own copper is where the pad already connects, not
                # a place to start from.
                if (
                    abs(p[0] - pad.x) <= hw + _JOINT_TOL_MM
                    and abs(p[1] - pad.y) <= hh + _JOINT_TOL_MM
                    and ka.gap(ka.site(p, 0.0), outline) <= _CONTACT_TOL_MM
                ):
                    continue
                mark = (layer, *_mark(p))
                if mark in marks:
                    continue
                marks.add(mark)
                anchors.append(KeptAnchor(layer, p, w))
        if anchors:
            anchors.sort(
                key=lambda k: (math.hypot(k.point[0] - pad.x, k.point[1] - pad.y), k.layer)
            )
            out[pad_key(pad)] = anchors[:MAX_ANCHORS_PER_PAD]
    return out
