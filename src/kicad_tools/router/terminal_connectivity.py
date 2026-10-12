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
            for layer in copper_layers_in_span(via.layers[0], via.layers[1]):
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
