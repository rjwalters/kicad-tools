"""Layer-aware copper components for physical routing terminals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
from shapely.strtree import STRtree  # type: ignore[import-untyped]

from .connectivity import _UnionFind
from .kelvin_obstacles import _pad_outline
from .layers import Layer

if TYPE_CHECKING:
    from .primitives import Pad, Route


def physical_pad_components(pads: list[Pad], routes: list[Route]) -> list[int]:
    """Return copper-component IDs, preserving a node for each physical pad.

    Contacts require intersecting copper on a shared layer. PTH pads and vias
    bridge only the layers their barrels span. Rounded rectangles use the
    existing conservative inner contact geometry because Pad lacks corner-radius
    metadata; a missing corner contact must not become a false completion.
    """
    shapes = [_pad_outline(pad, contact=True) for pad in pads]
    layers = [set(Layer) if pad.through_hole else {pad.layer} for pad in pads]
    for route in routes:
        for segment in route.segments:
            line = LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)])
            shapes.append(line.buffer(segment.width / 2))
            layers.append({segment.layer})
        for via in route.vias:
            shapes.append(Point(via.x, via.y).buffer(via.diameter / 2))
            first, last = sorted(layer.value for layer in via.layers)
            layers.append({layer for layer in Layer if first <= layer.value <= last})

    uf = _UnionFind()
    # Explicitly configured internal jumpers have the same terminal key, even
    # when their physical copper is disjoint. Distinct land IDs never alias.
    terminal_nodes: dict[tuple[str, str], int] = {}
    for i, pad in enumerate(pads):
        if pad.component_key and pad.pin:
            previous = terminal_nodes.setdefault(pad.key, i)
            uf.union(previous, i)
    tree = STRtree(shapes)
    for i, shape in enumerate(shapes):
        for raw_j in tree.query(shape, predicate="intersects"):
            j = int(raw_j)
            if j > i and layers[i] & layers[j]:
                uf.union(i, j)
    return [uf.find(i) for i in range(len(pads))]
