#!/usr/bin/env python3
"""Physical pad-to-pad path length for board 07's matched nets (Issue #9118).

``validate.py`` sums every copper segment on a net.  That is wrong for a net
whose copper carries a same-net loop the signal never travels (#9117, #9118).
This module measures what the signal actually does: the **shortest path
through the net's copper** between its two pads.

Model
    * A track is a polyline of straight legs; its centreline carries current.
    * Two pieces of same-net copper are connected where their *copper*
      overlaps (track body, via annulus, pad shape), not only where their
      centrelines meet.  A track that runs through a pad therefore connects to
      that pad wherever it overlaps it.
    * A pad is entered at the point of its copper closest to the track's
      centreline, and the pad is "reached" at its centre.
    * Vias join copper on different layers at zero planar length.  Via barrel
      length is a constant per net on this board and is not added.

The result is a planar length in mm.  It is a *lower bound* on the electrical
path and, unlike the summed-copper figure, cannot be inflated by a loop.

Usage::

    uv run python boards/07-matchgroup-test/real_design/physical_path.py \\
        [board.kicad_pcb]
"""

from __future__ import annotations

import heapq
import math
import sys
from pathlib import Path

from shapely.affinity import rotate
from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent
DEFAULT_BOARD = ROOT / "reviewed-routing/sdram_demo.kicad_pcb"

#: Copper that overlaps or lies within this distance (mm) is one conductor.
TOUCH_MM = 1e-4

BYTE0 = ("DQ0", "DQ1", "DQ2", "DQ3", "DQ4", "DQ5", "DQ6", "DQ7", "LDQM")
BYTE1 = ("DQ8", "DQ9", "DQ10", "DQ11", "DQ12", "DQ13", "DQ14", "DQ15", "UDQM")


def _pad_shapes(board, net: str):
    """``[(label, centre, shape)]`` for every F.Cu/B.Cu pad on ``net``."""
    pads = []
    for fp in board.footprints:
        fx, fy = fp.position
        for pad in fp.pads:
            if pad.net_name != net:
                continue
            rot = math.radians(-fp.rotation)  # board Y is down
            px, py = pad.position
            cx = fx + px * math.cos(rot) - py * math.sin(rot)
            cy = fy + px * math.sin(rot) + py * math.cos(rot)
            w, h = pad.size
            shape = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            # Pad angles already include the footprint rotation.
            shape = rotate(shape, -pad.rotation, origin=(cx, cy))
            layers = {layer for layer in pad.layers if layer.endswith(".Cu")}
            if "*.Cu" in pad.layers:
                layers = {"*"}
            pads.append((f"{fp.reference}.{pad.number}", (cx, cy), shape, layers))
    return pads


def net_copper(board, net: str):
    """All copper of ``net`` as ``(kind, key, layer-set, shape, centre)``."""
    items = []
    for seg in board.segments:
        if seg.net_name != net:
            continue
        line = LineString([seg.start, seg.end])
        items.append(("seg", (seg.start, seg.end), {seg.layer}, line.buffer(seg.width / 2), line))
    for via in board.vias:
        if via.net_name != net:
            continue
        items.append(
            (
                "via",
                via.position,
                {"*"},
                Point(via.position).buffer(via.size / 2),
                Point(via.position),
            )
        )
    for label, centre, shape, layers in _pad_shapes(board, net):
        items.append(("pad", label, layers, shape, Point(centre)))
    return items


def _share_layer(a: set[str], b: set[str]) -> bool:
    return "*" in a or "*" in b or bool(a & b)


def path_length(board, net: str) -> tuple[float, tuple[str, str]]:
    """Shortest planar path (mm) between the net's two pads, and their names.

    Raises ``ValueError`` if the net does not have exactly two pads or its
    copper does not connect them.
    """
    items = net_copper(board, net)
    pads = [i for i in items if i[0] == "pad"]
    if len(pads) != 2:
        raise ValueError(f"{net}: expected 2 pads, found {len(pads)}")

    # Graph nodes: segment endpoints, via centres, pad centres, and the
    # contact points where two pieces of copper overlap.
    nodes: dict[tuple[int, int], int] = {}
    coords: list[tuple[float, float]] = []

    def node(p) -> int:
        key = (round(p[0] * 1e4), round(p[1] * 1e4))
        if key not in nodes:
            nodes[key] = len(coords)
            coords.append((p[0], p[1]))
        return nodes[key]

    # Per piece, the nodes that lie on it (it is a conductor between them).
    on_piece: list[list[int]] = [[] for _ in items]
    layer_of = [i[2] for i in items]
    for idx, (kind, key, layers, shape, geom) in enumerate(items):
        if kind == "seg":
            on_piece[idx] += [node(key[0]), node(key[1])]
        else:
            on_piece[idx].append(node(geom.coords[0]))

    hops: list[tuple[int, int, float]] = []
    tree = STRtree([i[3] for i in items])
    for a, item_a in enumerate(items):
        for b in tree.query(item_a[3]):
            b = int(b)
            if b <= a or not _share_layer(layer_of[a], layer_of[b]):
                continue
            if item_a[3].distance(items[b][3]) > TOUCH_MM:
                continue
            # Contact point: on the track centreline nearest the other copper.
            inter = item_a[3].intersection(items[b][3])
            if inter.is_empty:
                continue
            ca = item_a[4]
            cb = items[b][4]
            # A point on a's centreline and one on b's, closest to each other
            # *within the overlap region*, joined by a zero-cost hop only when
            # both centrelines reach the overlap.
            pa = ca.interpolate(ca.project(inter.centroid)) if ca.geom_type == "LineString" else ca
            pb = cb.interpolate(cb.project(inter.centroid)) if cb.geom_type == "LineString" else cb
            na = node((pa.x, pa.y))
            nb = node((pb.x, pb.y))
            on_piece[a].append(na)
            on_piece[b].append(nb)
            if na != nb:
                # The two centreline points sit inside shared copper.
                hops.append((na, nb, math.dist(coords[na], coords[nb])))
    # Build adjacency.
    adj: dict[int, list[tuple[int, float]]] = {}

    def link(u: int, v: int, w: float) -> None:
        adj.setdefault(u, []).append((v, w))
        adj.setdefault(v, []).append((u, w))

    for idx, (kind, key, layers, shape, geom) in enumerate(items):
        pts = sorted(set(on_piece[idx]))
        if kind == "seg":
            line = geom
            ordered = sorted(pts, key=lambda n: line.project(Point(coords[n])))
            for u, v in zip(ordered, ordered[1:], strict=False):
                link(u, v, math.dist(coords[u], coords[v]))
        else:
            # A via or pad is one conductor: everything on it is joined, with
            # the straight-line planar distance inside the copper.
            for i, u in enumerate(pts):
                for v in pts[i + 1 :]:
                    link(u, v, math.dist(coords[u], coords[v]))
    for u, v, w in hops:
        link(u, v, w)

    src = node(pads[0][4].coords[0])
    dst = node(pads[1][4].coords[0])
    dist = {src: 0.0}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == dst:
            return d, (pads[0][1], pads[1][1])
        if d > dist.get(u, math.inf):
            continue
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    raise ValueError(f"{net}: pads {pads[0][1]} and {pads[1][1]} are not connected")


def group_lengths(board, nets=BYTE0) -> dict[str, float]:
    return {net: path_length(board, net)[0] for net in nets}


def spread(lengths: dict[str, float]) -> float:
    return max(lengths.values()) - min(lengths.values())


def main() -> int:
    from kicad_tools.schema.pcb import PCB

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BOARD
    board = PCB.load(str(path))
    for name, nets in (("SDRAM_BYTE0", BYTE0), ("SDRAM_BYTE1", BYTE1)):
        lengths = group_lengths(board, nets)
        for net, value in lengths.items():
            print(f"{net:6s} {value:8.3f} mm")
        print(f"{name} physical pad-to-pad spread: {spread(lengths):.3f} mm\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
