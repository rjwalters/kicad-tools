"""Route one KiCad-reported missing link against fixed copper (Issue #5785).

The oracle completion loop (:mod:`kicad_tools.router.oracle_completion`)
first tries to weld a stranded pour pad into its plane with a via.  When no
via site reaches the plane -- a fine-pitch IC pin whose only via sites sit in
the plane's clearance pullback, a pad on a layer the pour never covers -- the
link has to be *routed*: copper from one end of the link to the other, around
everything already on the board.

:func:`route_link` is that router.  It is deliberately small and
self-contained, because a link is short and the board is otherwise finished:

* a uniform multi-layer grid over the link's bounding box plus a margin,
  aligned on the link's first endpoint so a pad's own axis is a grid line;
* every other net's copper (pads, tracks, vias, arcs and unnetted copper
  pads), inflated by ``clearance + trace_width / 2``, blocks a cell; the
  board outline (inset by the edge clearance) bounds the grid;
* other nets' **zone fills** do not block -- KiCad refills a pour around new
  copper -- but cost extra per mm, so a link prefers not to carve a plane;
* a layer change is a through via, allowed only where the via clears foreign
  copper on every layer, every existing drill by the hole-to-hole rule, and
  every pad (no via-in-pad);
* the search starts on the cells inside endpoint A's copper and ends on any
  cell inside endpoint B's copper, so the new copper bonds to both;
* the result is simplified to straight runs and then **checked exactly**
  against the foreign copper with shapely -- grid quantisation can never leak
  a sub-clearance segment into the board.

Credit: KiCadRoutingTools routes oracle links with a dedicated plane-join
router (``py_router/plane_region_connector.py:route_plane_connection_wide``,
https://github.com/drandyhaas/KiCadRoutingTools, MIT).  This module follows the
same idea -- a small dedicated A* for a single missing link -- on our own PCB
model; no KRT code is copied.

All coordinates here are the :class:`~kicad_tools.schema.pcb.PCB` model's
board-relative millimetres.  :func:`append_link_route` adds the board origin
back when it writes the copper into the file.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "LinkRoute",
    "LinkRouteRules",
    "LinkTerminal",
    "append_link_route",
    "padless_components",
    "route_link",
]

#: Route endpoints land at least this far inside the terminal copper (mm).
_TERMINAL_INSET = 0.03

_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


@dataclass(frozen=True)
class LinkRouteRules:
    """Geometry and cost knobs for :func:`route_link` (all mm)."""

    trace_width: float = 0.2
    clearance: float = 0.2
    via_size: float = 0.6
    via_drill: float = 0.3
    hole_to_hole: float = 0.5
    edge_clearance: float = 0.5
    grid: float = 0.05
    margin: float = 4.0
    via_cost: float = 2.0
    foreign_fill_cost: float = 4.0
    max_cells: int = 1_500_000


@dataclass
class LinkTerminal:
    """One end of a link: the same-net copper the route must bond to.

    ``layers`` maps each copper layer name the item exists on to its
    board-relative shapely geometry there.
    """

    label: str
    point: tuple[float, float]
    layers: dict[str, Any]


@dataclass
class LinkRoute:
    """Copper that closes one link."""

    net_number: int
    net_name: str
    width: float
    via_size: float
    via_drill: float
    segments: list[tuple[float, float, float, float, str]] = field(default_factory=list)
    vias: list[tuple[float, float]] = field(default_factory=list)

    def capsules(self) -> list[tuple[float, float, float, float, float]]:
        """``(x1, y1, x2, y2, half_width)`` per segment and via (board-relative)."""
        caps = [(x1, y1, x2, y2, self.width / 2) for x1, y1, x2, y2, _layer in self.segments]
        caps.extend((x, y, x, y, self.via_size / 2) for x, y in self.vias)
        return caps


# ---------------------------------------------------------------------------
# Board copper model
# ---------------------------------------------------------------------------


def _copper_layer_names(pcb: PCB) -> list[str]:
    layers = pcb.copper_layers
    names = [getattr(layer, "name", str(layer)) for layer in layers]
    return names or ["F.Cu", "B.Cu"]


def _pad_layers(pad_layers: list[str], copper: list[str]) -> list[str]:
    out: list[str] = []
    for layer in copper:
        for spec in pad_layers:
            if spec == layer or spec == "*.Cu" or (spec == "F&B.Cu" and layer in ("F.Cu", "B.Cu")):
                out.append(layer)
                break
    return out


def _pad_geometry(fp: Any, pad: Any) -> Any:
    """Board-relative copper outline of a footprint pad (un-eroded)."""
    from shapely.geometry import LineString, Point, Polygon  # type: ignore[import-untyped]

    from kicad_tools.core.geometry import rotate_pad_offset

    ox, oy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
    cx, cy = fp.position[0] + ox, fp.position[1] + oy
    w, h = pad.size
    if w <= 0 or h <= 0:
        return Point(cx, cy)
    # A pad's (at x y angle) carries its ABSOLUTE board angle (issue #3902);
    # KiCad's negated-angle convention maps the local box into the board.
    a = math.radians(-(getattr(pad, "rotation", 0.0) or 0.0))
    ca, sa = math.cos(a), math.sin(a)

    def to_board(lx: float, ly: float) -> tuple[float, float]:
        return (cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)

    shape = (getattr(pad, "shape", "") or "").lower()
    if shape in ("circle", "oval"):
        radius = min(w, h) / 2
        half = abs(w - h) / 2
        if half <= 0:
            return Point(cx, cy).buffer(radius)
        ends = [(-half, 0.0), (half, 0.0)] if w >= h else [(0.0, -half), (0.0, half)]
        return LineString([to_board(*e) for e in ends]).buffer(radius)
    return Polygon(
        [
            to_board(-w / 2, -h / 2),
            to_board(w / 2, -h / 2),
            to_board(w / 2, h / 2),
            to_board(-w / 2, h / 2),
        ]
    )


@dataclass
class _Item:
    net: int
    layers: dict[str, Any]
    is_pad: bool = False


@dataclass
class _BoardModel:
    copper: list[str]
    items: list[_Item]
    drills: list[tuple[float, float, float]]
    pad_geoms: list[Any]
    fills: dict[str, list[tuple[int, Any]]]
    outline: Any


def _build_model(pcb: PCB) -> _BoardModel:
    from shapely.geometry import LineString, Point, Polygon

    copper = _copper_layer_names(pcb)
    items: list[_Item] = []
    drills: list[tuple[float, float, float]] = []
    pad_geoms: list[Any] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if (getattr(pad, "type", "") or "") == "np_thru_hole":
                drill = float(getattr(pad, "drill", 0.0) or 0.0)
                if drill > 0:
                    geom = _pad_geometry(fp, pad)
                    c = geom.centroid
                    drills.append((c.x, c.y, drill))
                continue
            layers = _pad_layers(list(getattr(pad, "layers", []) or []), copper)
            if not layers:
                continue
            geom = _pad_geometry(fp, pad)
            pad_geoms.append(geom)
            items.append(
                _Item(int(getattr(pad, "net_number", 0) or 0), dict.fromkeys(layers, geom), True)
            )
            drill = float(getattr(pad, "drill", 0.0) or 0.0)
            if drill > 0:
                c = geom.centroid
                drills.append((c.x, c.y, drill))
    for seg in pcb.segments:
        if seg.layer not in copper:
            continue
        line = LineString([seg.start, seg.end]) if seg.start != seg.end else Point(seg.start)
        items.append(_Item(seg.net_number, {seg.layer: line.buffer(seg.width / 2)}))
    for arc in getattr(pcb, "arcs", []) or []:
        layer = getattr(arc, "layer", None)
        pts = [
            p
            for p in (
                getattr(arc, "start", None),
                getattr(arc, "mid", None),
                getattr(arc, "end", None),
            )
            if p
        ]
        if layer in copper and len(pts) >= 2:
            width = float(getattr(arc, "width", 0.2) or 0.2)
            items.append(
                _Item(
                    int(getattr(arc, "net_number", 0) or 0),
                    {layer: LineString(pts).buffer(width / 2)},
                )
            )
    for via in pcb.vias:
        disk = Point(via.position).buffer((via.size or 0.0) / 2)
        items.append(_Item(via.net_number, dict.fromkeys(copper, disk)))
        if via.drill:
            drills.append((via.position[0], via.position[1], float(via.drill)))
    fills: dict[str, list[tuple[int, Any]]] = {}
    for zone in pcb.zones:
        polys = getattr(zone, "filled_polygons", None) or []
        layers = getattr(zone, "filled_polygon_layers", None) or []
        for idx, pts in enumerate(polys):
            if len(pts) < 3:
                continue
            layer = layers[idx] if idx < len(layers) else getattr(zone, "layer", None)
            if layer not in copper:
                continue
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                fills.setdefault(layer, []).append((zone.net_number, poly))
    outline_pts = pcb.get_board_outline()
    outline = Polygon(outline_pts) if len(outline_pts) >= 3 else None
    if outline is not None and not outline.is_valid:
        outline = outline.buffer(0)
    return _BoardModel(copper, items, drills, pad_geoms, fills, outline)


def terminal_for_pad(pcb: PCB, ref: str, pad_number: str) -> LinkTerminal | None:
    """The copper of footprint pad ``ref.pad_number`` as a :class:`LinkTerminal`."""
    copper = _copper_layer_names(pcb)
    fp = pcb.get_footprint(ref)
    if fp is None:
        return None
    for pad in fp.pads:
        if str(pad.number) == str(pad_number):
            layers = _pad_layers(list(getattr(pad, "layers", []) or []), copper)
            if not layers:
                return None
            geom = _pad_geometry(fp, pad)
            c = geom.centroid
            return LinkTerminal(f"{ref}.{pad_number}", (c.x, c.y), dict.fromkeys(layers, geom))
    return None


def terminal_at_copper(
    pcb: PCB, net_number: int, point: tuple[float, float], layer: str | None, kind: str
) -> LinkTerminal | None:
    """The same-net via or track KiCad anchored a link end at (board-relative)."""
    from shapely.geometry import LineString, Point

    copper = _copper_layer_names(pcb)
    probe = Point(point)
    if kind == "via":
        best = None
        for via in pcb.vias:
            if via.net_number != net_number:
                continue
            d = probe.distance(Point(via.position))
            if d <= max((via.size or 0) / 2, 0.05) and (best is None or d < best[0]):
                best = (d, via)
        if best is None:
            return None
        via = best[1]
        disk = Point(via.position).buffer((via.size or 0.0) / 2)
        return LinkTerminal("via", via.position, dict.fromkeys(copper, disk))
    if kind == "track":
        for seg in pcb.segments:
            if seg.net_number != net_number or (layer and seg.layer != layer):
                continue
            line = LineString([seg.start, seg.end]) if seg.start != seg.end else Point(seg.start)
            if line.distance(probe) <= seg.width / 2 + 1e-3:
                return LinkTerminal("track", point, {seg.layer: line.buffer(seg.width / 2)})
    return None


def net_components(model: _BoardModel, net_number: int) -> list[dict[str, Any]]:
    """Copper islands of one net, KiCad-style, as ``{layer: geometry}`` dicts.

    Nodes are the net's pads, tracks, vias and zone-fill islands; two nodes
    join when their copper intersects on a layer they share.  Sorted largest
    first by total copper area, so ``[0]`` is the main body of the net.
    """
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    nodes: list[dict[str, Any]] = [dict(it.layers) for it in model.items if it.net == net_number]
    for layer, fills in model.fills.items():
        nodes.extend({layer: poly} for net, poly in fills if net == net_number)
    parent = list(range(len(nodes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for layer in model.copper:
        idx = [i for i, n in enumerate(nodes) if layer in n]
        if len(idx) < 2:
            continue
        geoms = [nodes[i][layer] for i in idx]
        tree = STRtree(geoms)
        left, right = tree.query(geoms, predicate="intersects")
        for li_, ri_ in zip(left, right, strict=True):
            a, b = find(idx[int(li_)]), find(idx[int(ri_)])
            if a != b:
                parent[a] = b
    groups: dict[int, list[int]] = {}
    for i in range(len(nodes)):
        groups.setdefault(find(i), []).append(i)
    comps: list[dict[str, Any]] = []
    for members in groups.values():
        per_layer: dict[str, list[Any]] = {}
        for i in members:
            for layer, geom in nodes[i].items():
                per_layer.setdefault(layer, []).append(geom)
        comps.append({layer: unary_union(gs) for layer, gs in per_layer.items()})
    comps.sort(key=lambda c: -sum(g.area for g in c.values()))
    return comps


def padless_components(
    model: _BoardModel, net_number: int, comps: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Copper islands of ``net_number`` that contain no pad, largest first.

    KiCad reports a ``Zone <-> Zone`` unconnected item when a filled island
    that carries no pad of its own floats free of the rest of the net (the
    zone outline anchor says nothing about *which* island).  Those are the
    islands to bond: pour fragments - possibly with a dangling stub - that no
    pad or plane reaches.
    """
    pads = [it for it in model.items if it.net == net_number and getattr(it, "is_pad", False)]
    out = []
    if comps is None:
        comps = net_components(model, net_number)
    if len(comps) < 2:
        return []
    for comp in comps:
        if any(
            layer in comp and comp[layer].intersects(g)
            for pad in pads
            for layer, g in pad.layers.items()
        ):
            continue
        out.append(comp)
    return out


def component_terminals(
    comp: dict[str, Any], others: list[dict[str, Any]], label: str
) -> tuple[LinkTerminal, LinkTerminal] | None:
    """Terminals joining ``comp`` to the union of ``others`` at their closest approach."""
    from shapely.ops import nearest_points, unary_union

    if not others:
        return None
    rest: dict[str, list[Any]] = {}
    for other in others:
        for layer, geom in other.items():
            rest.setdefault(layer, []).append(geom)
    rest_u = {layer: unary_union(gs) for layer, gs in rest.items()}
    a_all = unary_union(list(comp.values()))
    b_all = unary_union(list(rest_u.values()))
    pa, pb = nearest_points(a_all, b_all)
    return (
        LinkTerminal(f"{label}:island", (pa.x, pa.y), comp),
        LinkTerminal(f"{label}:rest", (pb.x, pb.y), rest_u),
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def route_link(
    pcb: PCB,
    net_number: int,
    net_name: str,
    a: LinkTerminal,
    b: LinkTerminal,
    rules: LinkRouteRules,
    *,
    model: _BoardModel | None = None,
) -> LinkRoute | None:
    """Route copper from terminal ``a`` to terminal ``b`` on net ``net_number``.

    Returns ``None`` when no clearance-legal route exists inside the search
    window (or the window would exceed ``rules.max_cells``).
    """
    import numpy as np
    import shapely
    from shapely.geometry import LineString, Point, box
    from shapely.ops import unary_union

    model = model or _build_model(pcb)
    copper = model.copper
    g = rules.grid
    half_w = rules.trace_width / 2
    track_keepout = rules.clearance + half_w + 0.005
    via_keepout = rules.clearance + rules.via_size / 2 + 0.005

    (ax, ay), (bx, by) = a.point, b.point
    x0, x1 = min(ax, bx) - rules.margin, max(ax, bx) + rules.margin
    y0, y1 = min(ay, by) - rules.margin, max(ay, by) + rules.margin
    if model.outline is not None:
        ob = model.outline.bounds
        x0, y0, x1, y1 = max(x0, ob[0]), max(y0, ob[1]), min(x1, ob[2]), min(y1, ob[3])
    # Align the grid on A's anchor (snapped to 0.01 mm, the precision the PCB
    # writer keeps) so the written copper is exactly the copper checked here.
    gx0, gy0 = round(ax, 2), round(ay, 2)
    ix0 = math.floor((x0 - gx0) / g)
    iy0 = math.floor((y0 - gy0) / g)
    nx = int(math.ceil((x1 - gx0) / g)) - ix0 + 1
    ny = int(math.ceil((y1 - gy0) / g)) - iy0 + 1
    nl = len(copper)
    if nx <= 1 or ny <= 1 or nx * ny * nl > rules.max_cells:
        return None
    xs = np.round(gx0 + (ix0 + np.arange(nx)) * g, 2)
    ys = np.round(gy0 + (iy0 + np.arange(ny)) * g, 2)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    flat_x, flat_y = gx.ravel(), gy.ravel()
    window = box(xs[0] - 1.0, ys[0] - 1.0, xs[-1] + 1.0, ys[-1] + 1.0)

    def mask_of(geom: Any) -> Any:
        if geom is None or geom.is_empty:
            return np.zeros(nx * ny, dtype=bool)
        shapely.prepare(geom)
        return shapely.intersects_xy(geom, flat_x, flat_y).reshape(nx, ny)

    foreign_by_layer: dict[str, Any] = {}
    blocked = np.zeros((nl, nx, ny), dtype=bool)
    fill_cost = np.ones((nl, nx, ny), dtype=float)
    inside = None
    if model.outline is not None:
        inside = mask_of(model.outline.buffer(-(rules.edge_clearance + half_w)))
    for li, layer in enumerate(copper):
        geoms = [
            it.layers[layer]
            for it in model.items
            if it.net != net_number and layer in it.layers and it.layers[layer].intersects(window)
        ]
        foreign = unary_union(geoms) if geoms else None
        foreign_by_layer[layer] = foreign
        if foreign is not None:
            blocked[li] = mask_of(foreign.buffer(track_keepout))
        if inside is not None:
            blocked[li] |= ~inside
        fills = [
            poly
            for net, poly in model.fills.get(layer, [])
            if net != net_number and poly.intersects(window)
        ]
        if fills:
            fill_cost[li][mask_of(unary_union(fills))] = rules.foreign_fill_cost

    # Via legality: clear of foreign copper on every layer, of every pad
    # (no via-in-pad), of every drill by the hole-to-hole rule.
    via_bad = np.zeros((nx, ny), dtype=bool)
    for foreign in foreign_by_layer.values():
        if foreign is not None:
            via_bad |= mask_of(foreign.buffer(via_keepout))
    pads_here = [p for p in model.pad_geoms if p.intersects(window)]
    if pads_here:
        via_bad |= mask_of(unary_union(pads_here).buffer(rules.via_size / 2 + 0.05))
    drill_disks = [
        Point(dx, dy).buffer(dd / 2 + rules.via_drill / 2 + rules.hole_to_hole)
        for dx, dy, dd in model.drills
        if window.distance(Point(dx, dy)) < 2.0
    ]
    if drill_disks:
        via_bad |= mask_of(unary_union(drill_disks))
    if inside is not None:
        via_bad |= ~mask_of(model.outline.buffer(-(rules.edge_clearance + rules.via_size / 2)))

    def terminal_mask(t: LinkTerminal) -> Any:
        m = np.zeros((nl, nx, ny), dtype=bool)
        for li, layer in enumerate(copper):
            if layer in t.layers:
                geom = t.layers[layer]
                # Keep endpoints strictly inside the copper they bond to (KiCad
                # joins a track to a pad by its END POINT, not by overlap).
                inset = geom.buffer(-_TERMINAL_INSET)
                m[li] = mask_of(geom if inset.is_empty else inset) & ~blocked[li]
        return m

    # Inner layers that carry a pour are plane layers: a link may drop a via
    # into one (that is how a stranded pad reaches its plane) but never runs
    # a track along it.
    plane_layer = [
        li
        for li, layer in enumerate(copper)
        if layer not in ("F.Cu", "B.Cu") and model.fills.get(layer)
    ]
    is_plane = [li in plane_layer for li in range(nl)]

    start = terminal_mask(a)
    goal = terminal_mask(b)
    if not start.any() or not goal.any():
        return None

    # A* over (layer, i, j).
    inf = float("inf")
    dist = np.full((nl, nx, ny), inf)
    parent: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    heap: list[tuple[float, float, int, int, int]] = []

    def h(i: int, j: int) -> float:
        return math.hypot(xs[i] - bx, ys[j] - by)

    for li, i, j in zip(*np.nonzero(start), strict=True):
        dist[li, i, j] = 0.0
        heapq.heappush(heap, (h(i, j), 0.0, int(li), int(i), int(j)))

    found: tuple[int, int, int] | None = None
    while heap:
        _f, d, li, i, j = heapq.heappop(heap)
        if d > dist[li, i, j]:
            continue
        if goal[li, i, j]:
            found = (li, i, j)
            break
        for di, dj in [] if is_plane[li] else _DIRS:
            ni, nj = i + di, j + dj
            if not (0 <= ni < nx and 0 <= nj < ny) or blocked[li, ni, nj]:
                continue
            step = g * (math.sqrt(2.0) if di and dj else 1.0)
            # Diagonal moves must not cut a blocked corner.
            if di and dj and (blocked[li, i + di, j] or blocked[li, i, j + dj]):
                continue
            nd = d + step * 0.5 * (fill_cost[li, i, j] + fill_cost[li, ni, nj])
            if nd < dist[li, ni, nj]:
                dist[li, ni, nj] = nd
                parent[(li, ni, nj)] = (li, i, j)
                heapq.heappush(heap, (nd + h(ni, nj), nd, li, ni, nj))
        if not via_bad[i, j]:
            for nli in range(nl):
                if nli == li or blocked[nli, i, j]:
                    continue
                nd = d + rules.via_cost
                if nd < dist[nli, i, j]:
                    dist[nli, i, j] = nd
                    parent[(nli, i, j)] = (li, i, j)
                    heapq.heappush(heap, (nd + h(i, j), nd, nli, i, j))
    if found is None:
        return None
    if dist[found] == 0.0:
        return None  # the two terminals already touch on the grid: nothing to add

    path = [found]
    while path[-1] in parent:
        path.append(parent[path[-1]])
    path.reverse()

    route = LinkRoute(
        net_number=net_number,
        net_name=net_name,
        width=rules.trace_width,
        via_size=rules.via_size,
        via_drill=rules.via_drill,
    )
    # Split into per-layer runs; a layer change is a via at that cell.
    runs: list[tuple[int, list[tuple[int, int]]]] = []
    for li, i, j in path:
        if runs and runs[-1][0] == li:
            runs[-1][1].append((i, j))
        else:
            if runs:
                route.vias.append((float(xs[i]), float(ys[j])))
            runs.append((li, [(i, j)]))
    legal_area = (
        model.outline.buffer(-(rules.edge_clearance + half_w))
        if model.outline is not None
        else None
    )
    for li, cells in runs:
        pts = _simplify(cells)
        xy = [(float(xs[i]), float(ys[j])) for i, j in pts]
        xy = _shortcut(xy, foreign_by_layer.get(copper[li]), rules.clearance + half_w, legal_area)
        for (x_a, y_a), (x_b, y_b) in zip(xy, xy[1:], strict=False):
            route.segments.append((x_a, y_a, x_b, y_b, copper[li]))

    # Exact check: grid quantisation must never leak a sub-clearance segment.
    for x_a, y_a, x_b, y_b, layer in route.segments:
        foreign = foreign_by_layer.get(layer)
        if foreign is None:
            continue
        line = LineString([(x_a, y_a), (x_b, y_b)])
        if line.distance(foreign) < rules.clearance + half_w - 1e-4:
            return None
    for vx, vy in route.vias:
        p = Point(vx, vy)
        for foreign in foreign_by_layer.values():
            if (
                foreign is not None
                and p.distance(foreign) < rules.clearance + rules.via_size / 2 - 1e-4
            ):
                return None
    return route


def _octilinear(p: tuple[float, float], q: tuple[float, float]) -> bool:
    dx, dy = abs(q[0] - p[0]), abs(q[1] - p[1])
    return dx < 1e-6 or dy < 1e-6 or abs(dx - dy) < 1e-6


def _shortcut(
    pts: list[tuple[float, float]], foreign: Any, keepout: float, legal_area: Any
) -> list[tuple[float, float]]:
    """Merge staircase corners: jump to the farthest vertex reachable by one
    legal octilinear segment.  Endpoints (and so terminal contact) are kept.

    The A* grid breaks cost ties arbitrarily, so a diagonal run often comes
    out as a zig-zag of grid-step segments.  Each candidate shortcut is
    re-checked exactly against the foreign copper and the board outline.
    """
    from shapely.geometry import LineString

    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    k = 0
    while k < len(pts) - 1:
        nxt = k + 1
        for m in range(len(pts) - 1, k + 1, -1):
            if not _octilinear(pts[k], pts[m]):
                continue
            line = LineString([pts[k], pts[m]])
            if foreign is not None and line.distance(foreign) < keepout - 1e-4:
                continue
            if legal_area is not None and not legal_area.covers(line):
                continue
            nxt = m
            break
        out.append(pts[nxt])
        k = nxt
    return out


def _simplify(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep only the cells where the move direction changes."""
    if len(cells) <= 2:
        return list(cells) if len(cells) == 2 else [cells[0], cells[0]]
    out = [cells[0]]
    for prev, cur, nxt in zip(cells, cells[1:], cells[2:], strict=False):
        if (cur[0] - prev[0], cur[1] - prev[1]) != (nxt[0] - cur[0], nxt[1] - cur[1]):
            out.append(cur)
    out.append(cells[-1])
    return out


def append_link_route(pcb_path: Path, route: LinkRoute, origin: tuple[float, float]) -> None:
    """Write ``route``'s segments and vias into the board file.

    ``origin`` is the PCB model's ``board_origin``: the route is in
    board-relative coordinates and the file is absolute.
    """
    from kicad_tools.cli.stitch_cmd import _stitch_uuid
    from kicad_tools.core.sexp_file import load_pcb, save_pcb
    from kicad_tools.sexp.builders import segment_node, via_node

    ox, oy = origin
    doc = load_pcb(pcb_path)
    for x_a, y_a, x_b, y_b, layer in route.segments:
        if (x_a, y_a) == (x_b, y_b):
            continue
        doc.append(
            segment_node(
                start_x=x_a + ox,
                start_y=y_a + oy,
                end_x=x_b + ox,
                end_y=y_b + oy,
                width=route.width,
                layer=layer,
                net=route.net_number,
                uuid_str=_stitch_uuid("oracle-seg", x_a, y_a, x_b, y_b, layer, route.net_number),
            )
        )
    for vx, vy in route.vias:
        doc.append(
            via_node(
                x=vx + ox,
                y=vy + oy,
                size=route.via_size,
                drill=route.via_drill,
                layers=("F.Cu", "B.Cu"),
                net=route.net_number,
                uuid_str=_stitch_uuid("oracle-via", vx, vy, route.net_number),
            )
        )
    save_pcb(doc, pcb_path)
