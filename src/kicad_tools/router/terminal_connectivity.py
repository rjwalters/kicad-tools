"""Layer-aware copper components for physical routing terminals."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import shapely  # type: ignore[import-untyped]
from shapely import affinity
from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
from shapely.strtree import STRtree  # type: ignore[import-untyped]

from kicad_tools.core.types import copper_layers_in_span

from .connectivity import _UnionFind
from .kelvin_obstacles import _pad_outline
from .layers import Layer

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .primitives import Pad, Route, Segment, Via


def _contact_outline(pad: Pad):
    """Copper a conductor may touch to connect ``pad``, never larger than the pad.

    Rounded rectangles lack corner-radius metadata on :class:`Pad`, and
    ``_pad_outline(contact=True)`` models them as the inscribed *circle* --
    safe for Kelvin contact selection, but far too small for connectivity:
    board 03's U1.3 (0.55 x 1.475 mm) is fed by a trace ending 0.51 mm from
    its centre, inside the copper yet outside that circle (Issue #6371).
    KiCad caps ``roundrect_rratio`` at 0.5, and every such roundrect contains
    the stadium inscribed in its bounding box (the box minus the radius-r
    corners contains the box minus radius-min(w,h)/2 corners), so the stadium
    is still a lower bound on the real copper.
    """
    if pad.shape != "roundrect":
        return _pad_outline(pad, contact=True)
    w, h = pad.width, pad.height
    radius = min(w, h) / 2
    delta = abs(w - h) / 2
    if delta <= 0:
        shape = Point(0, 0).buffer(radius)
    else:
        ends = [(-delta, 0), (delta, 0)] if w >= h else [(0, -delta), (0, delta)]
        shape = LineString(ends).buffer(radius)
    # Same rotation convention as ``_pad_outline``.
    return affinity.translate(affinity.rotate(shape, -pad.rotation), pad.x, pad.y)


def physical_pad_components(pads: list[Pad], routes: list[Route]) -> list[int]:
    """Return copper-component IDs, preserving a node for each physical pad.

    Contacts require intersecting copper on a shared layer. PTH pads and vias
    bridge only the layers their barrels span. Rounded rectangles use the
    inscribed stadium (see :func:`_contact_outline`) because Pad lacks
    corner-radius metadata; a missing corner contact must not become a false
    completion.
    """
    return physical_copper_components(
        pads,
        (segment for route in routes for segment in route.segments),
        (via for route in routes for via in route.vias),
    )


def physical_copper_components(
    pads: list[Pad], segments: Iterable[Segment], vias: Iterable[Via]
) -> list[int]:
    """Layer-aware pad components from loose segments and vias (Issue #6371).

    Same contact model as :func:`physical_pad_components`, for callers that
    hold copper outside :class:`Route` objects -- e.g. the post-save verifier
    re-parsing ``(segment ...)`` / ``(via ...)`` blocks from the written file.
    A pad counts as connected only when same-net copper on a layer the pad
    occupies actually intersects its outline; XY proximity alone never does.
    """
    # Layer sets as bitmasks so the contact filter below is vectorised.
    all_layers_mask = 0
    for layer in Layer:
        all_layers_mask |= 1 << layer.value
    shapes: list = [_contact_outline(pad) for pad in pads]
    masks = [all_layers_mask if pad.through_hole else 1 << pad.layer.value for pad in pads]

    # Bulk shapely-2 construction: one vectorised buffer per copper kind
    # instead of one Python-level call per conductor (a board-05 net set is
    # ~8k conductors; the per-object path cost seconds per verification).
    seg_list = list(segments)
    if seg_list:
        coords = np.array([[(seg.x1, seg.y1), (seg.x2, seg.y2)] for seg in seg_list], dtype=float)
        # A zero-width segment would buffer to an empty polygon and silently
        # drop out of the contact graph; keep it as a hairline instead.
        radii = np.array([max(seg.width, 1e-6) / 2 for seg in seg_list], dtype=float)
        shapes.extend(shapely.buffer(shapely.linestrings(coords), radii))
        masks.extend(1 << seg.layer.value for seg in seg_list)
    via_list = list(vias)
    if via_list:
        centres = np.array([(via.x, via.y) for via in via_list], dtype=float)
        radii = np.array([max(via.diameter, 1e-6) / 2 for via in via_list], dtype=float)
        shapes.extend(shapely.buffer(shapely.points(centres), radii))
        for via in via_list:
            mask = 0
            span_lo, span_hi = getattr(via, "drilled_layers", via.layers)
            for layer in copper_layers_in_span(span_lo, span_hi):
                mask |= 1 << layer.value
            masks.append(mask)

    uf = _UnionFind()
    # Explicitly configured internal jumpers have the same terminal key, even
    # when their physical copper is disjoint. Distinct land IDs never alias.
    terminal_nodes: dict[tuple[str, str], int] = {}
    for i, pad in enumerate(pads):
        if pad.component_key and pad.pin:
            previous = terminal_nodes.setdefault(pad.key, i)
            uf.union(previous, i)
    geoms = np.array(shapes, dtype=object)
    left, right = STRtree(geoms).query(geoms, predicate="intersects")
    mask_arr = np.array(masks, dtype=np.int64)
    keep = (left < right) & ((mask_arr[left] & mask_arr[right]) != 0)
    for i, j in zip(left[keep].tolist(), right[keep].tolist(), strict=True):
        uf.union(i, j)
    return [uf.find(i) for i in range(len(pads))]


def _via_span_mask(via: Via) -> int:
    """Bitmask of every copper layer a via's barrel physically reaches.

    Uses :attr:`Via.drilled_layers` (issue #6485) when present: an ordinary
    via is drilled ``F.Cu``-``B.Cu`` whatever logical pair it records, so its
    annular ring exists on every layer in between.  Reading the wider span
    can only find *more* bonded layers, never fewer.
    """
    first, last = getattr(via, "drilled_layers", via.layers)
    mask = 0
    for layer in copper_layers_in_span(first, last):
        mask |= 1 << layer.value
    return mask


def _net_copper_shapes(
    pads: list[Pad], segments: list[Segment], vias: list[Via]
) -> tuple[list, list[int]]:
    """Contact shapes and layer masks for pads, then segments, then vias."""
    all_layers_mask = 0
    for layer in Layer:
        all_layers_mask |= 1 << layer.value
    shapes: list = [_contact_outline(pad) for pad in pads]
    masks = [all_layers_mask if pad.through_hole else 1 << pad.layer.value for pad in pads]
    for seg in segments:
        shapes.append(
            LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)]).buffer(max(seg.width, 1e-6) / 2)
        )
        masks.append(1 << seg.layer.value)
    for via in vias:
        shapes.append(Point(via.x, via.y).buffer(max(via.diameter, 1e-6) / 2))
        masks.append(_via_span_mask(via))
    return shapes, masks


def copper_component_count(pads: list[Pad], segments: list[Segment], vias: list[Via]) -> int:
    """Number of layer-aware copper components over one net's copper (#6596).

    Every pad, segment and via is a node; two nodes join when their copper
    intersects on a shared layer (the :func:`physical_copper_components`
    contact model).  Unlike that function this counts *all* components,
    including islands without a pad, so a cleanup step can prove that
    deleting a via split nothing: the count after must not exceed the count
    before.
    """
    shapes, masks = _net_copper_shapes(pads, segments, vias)
    if not shapes:
        return 0
    uf = _UnionFind()
    terminal_nodes: dict[tuple[str, str], int] = {}
    for i, pad in enumerate(pads):
        if pad.component_key and pad.pin:
            previous = terminal_nodes.setdefault(pad.key, i)
            uf.union(previous, i)
    geoms = np.array(shapes, dtype=object)
    left, right = STRtree(geoms).query(geoms, predicate="intersects")
    mask_arr = np.array(masks, dtype=np.int64)
    keep = (left < right) & ((mask_arr[left] & mask_arr[right]) != 0)
    for i, j in zip(left[keep].tolist(), right[keep].tolist(), strict=True):
        uf.union(i, j)
    return len({uf.find(i) for i in range(len(shapes))})


def single_layer_via_indices(
    pads: list[Pad], segments: list[Segment], vias: list[Via]
) -> list[int]:
    """Indices into ``vias`` whose same-net copper lies on fewer than two layers.

    Issue #6596: such a via is a redundant junction -- KiCad reports it as
    ``via_dangling`` ("connected on only one layer").  A layer counts as
    bonded when a same-net segment or another via overlaps the via's pad on
    a layer both span.  A via overlapping any same-net pad is never returned:
    the pad test uses the pad's circumscribed circle (an upper bound on its
    copper), so an in-pad or pad-adjacent via is always left alone.

    The caller supplies one net's copper only and must still prove removal
    keeps the net connected (:func:`copper_component_count`); this function
    only nominates candidates.
    """
    if not vias:
        return []
    masks = [1 << s.layer.value for s in segments] + [_via_span_mask(v) for v in vias]
    shapes: list = [
        LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(max(s.width, 1e-6) / 2) for s in segments
    ]
    via_shapes = [Point(v.x, v.y).buffer(max(v.diameter, 1e-6) / 2) for v in vias]
    shapes.extend(via_shapes)
    first_via = len(segments)
    pad_bounds = [
        Point(p.x, p.y).buffer(max(float(np.hypot(p.width, p.height)) / 2, 1e-6)) for p in pads
    ]
    pad_tree = STRtree(np.array(pad_bounds, dtype=object)) if pad_bounds else None
    tree = STRtree(np.array(shapes, dtype=object))

    result: list[int] = []
    for k, via_shape in enumerate(via_shapes):
        if pad_tree is not None and len(pad_tree.query(via_shape, predicate="intersects")):
            continue
        own = first_via + k
        own_mask = masks[own]
        bonded = 0
        for j in tree.query(via_shape, predicate="intersects").tolist():
            if j != own:
                bonded |= masks[j] & own_mask
        if bonded.bit_count() < 2:
            result.append(k)
    return result
