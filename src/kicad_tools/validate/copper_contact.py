"""One copper-contact model for every connectivity consumer (Issue #6452).

Before this module four places decided "does this copper touch that copper"
with four different rules: ``NetStatusAnalyzer``'s per-zone fill
union-find, ``ConnectivityValidator.validate()``'s pad-centre matching (nets
without fills), its item graph plus a nearest-fill heuristic (nets with
fills), and the label-free LVS partition.  They disagreed with each other and
with ``kicad-cli pcb drc`` (Issues #6452, #6453, #6470).

:func:`build_copper_contact_graph` is the single replacement.  Every piece of
copper is a node:

* each **pad** occurrence (exact outline, :func:`pad_copper_outline`);
* each **via** (its physical annulus, bridging every layer it spans);
* each **track** segment or arc (its swept capsule on one layer);
* each **fill** (one hole-aware ``filled_polygon``).

Two nodes bond only where their copper really meets on a shared copper layer:

==============  ==============================================================
pair            bond when
==============  ==============================================================
pad - pad       outlines overlap by positive area (an exposed pad over its
                thermal-via pads, Issue #6435); disjoint same-number pads stay
                separate (Issue #5151)
pad - via       annulus overlaps the outline by positive area (#5133)
pad - track     the track centreline enters the outline, or its copper
                penetrates the outline by more than 1 um (#4678, #5060)
pad - fill      outline overlaps the solid by positive area (#6453)
via - via       annuli overlap by positive area
via - track     the track copper penetrates the via disk by more than 1 um
via - fill      annulus intersects the solid (#5382)
track - track   same-layer swept copper touches (#5060)
track - fill    track copper intersects the solid
fill - fill     different zones: solids intersect; the same zone: only when
                the stroked-fill encoding makes them continuous (#5362)
==============  ==============================================================

A pad/via centre within ``POSITION_TOLERANCE`` (0.01 mm) of another centre
or of a track endpoint on a shared layer also bonds, as do the endpoints of a
width-less track, so degenerate zero-size fixtures keep working.

The graph is built either label-free (every item on the board -- the LVS
partition, which must see shorts) or scoped to one net (every item carrying
that net -- net-status and ``validate()``).  Fills of *different* zones are
nodes of one graph, so a via joining two zones' fills is a bridge (#6452).

Copper fragments that reach no pad are their own components.  KiCad's
ratsnest counts a detached track or via as an unconnected item -- and a fill
island once a track or via sits on it -- so
:meth:`CopperContactGraph.open_count` includes them.  A component made
*only* of fill copper (a lone fill island that nothing touches) is the one
exception: ``kicad-cli pcb drc`` does not report it, so it is left out of
the count (:meth:`CopperContactGraph.fill_only_components`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools.geometry.copper import (
    CONTACT_AREA_EPSILON,
    pad_board_center,
    pad_copper_outline,
    pad_outline_is_exact,
    segment_copper_polygon,
    segments_copper_touch,
)

if TYPE_CHECKING:
    from kicad_tools.validate.connectivity import ConnectivityValidator

# Endpoint/centre coincidence radius (mm); mirrors ``POSITION_TOLERANCE``.
_TOLERANCE = 0.01
# Strictly positive contact depth (mm) a track needs into a pad or via, so
# copper separated by a real clearance moat never fuses (steps 2a2/2a3).
_PENETRATION = 1e-3

PAD = "pad"
VIA = "via"
TRACK = "track"
FILL = "fill"
_KIND_ORDER = {PAD: 0, VIA: 1, TRACK: 2, FILL: 3}


@dataclass
class CopperItem:
    """One piece of copper in the contact graph.

    ``key`` identifies the source object: ``(footprint_index, pad_index)``
    for a pad, the via index, ``("segment", i)`` / ``("arc", i)`` for a
    track, and ``(zone_index, fill_index)`` for a fill.
    """

    kind: str
    key: Any
    net: int
    layers: frozenset[str]
    geom: Any
    center: tuple[float, float] | None = None
    exact: bool = True
    # Track centreline geometry and half width; via disk/annulus.
    line: Any = None
    half_width: float = 0.0
    radius: float = 0.0
    annulus: Any = None
    endpoints: tuple[tuple[float, float], ...] = ()
    straight: bool = True
    zone: int = -1


@dataclass
class CopperContactGraph:
    """Items plus their connected-component labels."""

    items: list[CopperItem]
    component: list[int] = field(default_factory=list)

    def components(self) -> list[list[int]]:
        """Item indices per component, ordered by first item."""
        groups: dict[int, list[int]] = {}
        for index, root in enumerate(self.component):
            groups.setdefault(root, []).append(index)
        return list(groups.values())

    def _kinds(self, members: list[int]) -> set[str]:
        return {self.items[i].kind for i in members}

    def pad_components(self) -> list[list[CopperItem]]:
        """Pad items grouped by component (components without pads dropped)."""
        out: list[list[CopperItem]] = []
        for members in self.components():
            pads = [self.items[i] for i in members if self.items[i].kind == PAD]
            if pads:
                out.append(pads)
        return out

    def fill_bonded_pads(self) -> set[Any]:
        """Keys of pads whose component contains fill copper (Issue #6315)."""
        bonded: set[Any] = set()
        for members in self.components():
            if FILL in self._kinds(members):
                bonded.update(self.items[i].key for i in members if self.items[i].kind == PAD)
        return bonded

    def terminal_components(self) -> list[list[int]]:
        """Components holding a pad or a fill solid."""
        return [m for m in self.components() if self._kinds(m) & {PAD, FILL}]

    def orphan_components(self) -> list[list[int]]:
        """Track/via-only fragments that reach no pad and no fill."""
        return [m for m in self.components() if not self._kinds(m) & {PAD, FILL}]

    def is_fill_only(self, members: list[int]) -> bool:
        """True for a component made solely of fill copper (no pad/via/track)."""
        return self._kinds(members) == {FILL}

    def fill_only_components(self) -> list[list[int]]:
        """Lone fill islands: components whose every item is fill copper.

        ``kicad-cli pcb drc`` does not report these as unconnected items; a
        fill island only becomes one once a track or via sits on it.
        """
        return [m for m in self.components() if self.is_fill_only(m)]

    def counted_components(self) -> list[list[int]]:
        """Components KiCad's ratsnest counts: everything but lone fill islands."""
        return [m for m in self.components() if not self.is_fill_only(m)]

    def open_count(self) -> int:
        """Remaining ratsnest connections (KiCad parity).

        Counted components - 1, where a counted component holds at least one
        pad, via or track.  Lone fill islands are excluded.
        """
        return max(len(self.counted_components()) - 1, 0)


def _expand_copper_layers(cv: ConnectivityValidator, layers: list[str]) -> frozenset[str]:
    """Concrete copper layers of a pad layer list (``*.Cu`` and ``F&B.Cu`` aware)."""
    out: set[str] = set()
    for layer in layers:
        if layer == "F&B.Cu":
            out.update(("F.Cu", "B.Cu"))
        elif layer == "*.Cu":
            out.update(cv._copper_layer_order())
        elif layer.endswith(".Cu"):
            out.add(layer)
    return frozenset(out)


def _fill_layer(zone: Any, index: int) -> str:
    """Layer of ``zone.filled_polygons[index]`` (duck-typed zones fall back)."""
    getter = getattr(zone, "filled_polygon_layer", None)
    return str(getter(index)) if getter is not None else str(zone.layer)


def _close(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 < _TOLERANCE * _TOLERANCE


def _area_contact(a: Any, b: Any) -> bool:
    if not a.intersects(b):
        return False
    return bool(a.intersection(b).area > CONTACT_AREA_EPSILON)


def _collect_items(cv: ConnectivityValidator, net_number: int | None) -> list[CopperItem]:
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

    pcb = cv.pcb
    items: list[CopperItem] = []

    def wanted(net: int) -> bool:
        return net_number is None or net == net_number

    for fi, fp in enumerate(pcb.footprints):
        if not fp.reference or fp.reference.startswith("#"):
            continue
        for pi, pad in enumerate(fp.pads):
            if not wanted(pad.net_number):
                continue
            if net_number is None and (pad.number is None or pad.number == ""):
                continue
            geom = pad_copper_outline(pad, fp.position, fp.rotation)
            if geom is None:
                continue
            items.append(
                CopperItem(
                    kind=PAD,
                    key=(fi, pi),
                    net=pad.net_number,
                    layers=_expand_copper_layers(cv, pad.layers),
                    geom=geom,
                    center=pad_board_center(pad, fp.position, fp.rotation),
                    exact=pad_outline_is_exact(pad),
                )
            )

    for index, via in enumerate(pcb.vias):
        if not wanted(via.net_number):
            continue
        radius = max(getattr(via, "size", 0.0) or 0.0, 0.0) / 2.0
        if radius > 0:
            disk = Point(via.position).buffer(radius, quad_segs=64)
            annulus = cv._physical_via_annulus(via)
        else:
            disk = annulus = Point(via.position)
        items.append(
            CopperItem(
                kind=VIA,
                key=index,
                net=via.net_number,
                layers=cv._via_bridged_layers(via.layers),
                geom=disk,
                center=via.position,
                radius=radius,
                annulus=annulus,
            )
        )

    for index, seg in enumerate(pcb.segments):
        if not wanted(seg.net_number):
            continue
        poly = segment_copper_polygon(seg.start, seg.end, seg.width)
        if poly is None:
            continue
        line = Point(seg.start) if seg.start == seg.end else LineString([seg.start, seg.end])
        items.append(
            CopperItem(
                kind=TRACK,
                key=("segment", index),
                net=seg.net_number,
                layers=frozenset({seg.layer}),
                geom=poly,
                line=line,
                half_width=max(seg.width or 0.0, 0.0) / 2.0,
                endpoints=(seg.start, seg.end),
            )
        )

    # Duck-typed boards (tests, adapters) may carry no arcs attribute.
    for index, arc in enumerate(getattr(pcb, "arcs", None) or ()):
        if not wanted(arc.net_number):
            continue
        # Same tessellation bound as NetStatusAnalyzer._segment_poly.
        error = min(0.00001, arc.width / 1000)
        radius = arc.width / 2
        step = 4 * math.asin(math.sqrt(min(error / (2 * radius), 0.5)))
        quad_segs = max(16, math.ceil(math.pi / (2 * step)))
        line = LineString(arc.centerline_points(error))
        items.append(
            CopperItem(
                kind=TRACK,
                key=("arc", index),
                net=arc.net_number,
                layers=frozenset({arc.layer}),
                geom=line.buffer(radius, quad_segs=quad_segs),
                line=line,
                half_width=radius,
                endpoints=(arc.start, arc.end),
                straight=False,
            )
        )

    for zone_index, zone in enumerate(pcb.zones):
        if not wanted(zone.net_number):
            continue
        for fill_index, points in enumerate(zone.filled_polygons):
            region = cv._fill_solid_region(points)
            if region is None:
                continue
            layer = _fill_layer(zone, fill_index)
            # One stored ``filled_polygon`` is one KiCad connectivity item,
            # even when repairing its fractured contour yields several
            # solids: KiCad keeps them joined through the fracture bridges
            # (fresh lattice board 03 reports 0 unconnected with three such
            # pieces on its B.Cu GND fill).
            items.append(
                CopperItem(
                    kind=FILL,
                    key=(zone_index, fill_index),
                    net=zone.net_number,
                    layers=frozenset({layer}),
                    geom=region,
                    zone=zone_index,
                )
            )
    return items


def _track_reaches(track: CopperItem, target: Any, extra: float) -> bool:
    """Track centreline within ``half_width + extra - 1 um`` of ``target``.

    ``extra`` is 0 for a pad outline (the track must reach into the copper)
    and the via radius when ``target`` is the via centre point.
    """
    dist = track.line.distance(target)
    if dist == 0.0 and extra == 0.0:
        return True
    return bool(dist < track.half_width + extra - _PENETRATION)


def _bonded(a: CopperItem, b: CopperItem) -> bool:
    """Decide contact between two items already known to share a layer."""
    if _KIND_ORDER[a.kind] > _KIND_ORDER[b.kind]:
        a, b = b, a
    kinds = (a.kind, b.kind)
    if kinds == (PAD, PAD):
        if a.center is not None and b.center is not None and _close(a.center, b.center):
            return True
        if a.exact and b.exact:
            return _area_contact(a.geom, b.geom)
        return bool(a.geom.intersects(b.geom))
    if kinds == (PAD, VIA):
        if a.center is not None and b.center is not None and _close(a.center, b.center):
            return True
        if a.exact:
            return _area_contact(a.geom, b.annulus)
        from shapely.geometry import Point

        return bool(a.geom.intersects(Point(b.center)))
    if kinds == (PAD, TRACK):
        if a.center is not None and any(_close(a.center, p) for p in b.endpoints):
            return True
        return _track_reaches(b, a.geom, 0.0)
    if kinds == (PAD, FILL):
        if a.exact:
            return _area_contact(a.geom, b.geom)
        return bool(a.geom.intersects(b.geom))
    if kinds == (VIA, VIA):
        if a.center is not None and b.center is not None and _close(a.center, b.center):
            return True
        return _area_contact(a.annulus, b.annulus)
    if kinds == (VIA, TRACK):
        if a.center is not None and any(_close(a.center, p) for p in b.endpoints):
            return True
        if a.radius <= 0:
            return False
        from shapely.geometry import Point

        return _track_reaches(b, Point(a.center), a.radius)
    if kinds == (VIA, FILL):
        return bool(a.annulus.intersects(b.geom))
    if kinds == (TRACK, TRACK):
        # Endpoint tolerance only rescues width-less (degenerate) fixtures:
        # real copper 0.009 mm apart is not contact (Issue #4176).
        degenerate = a.half_width <= 0 or b.half_width <= 0
        if degenerate and any(_close(p, q) for p in a.endpoints for q in b.endpoints):
            return True
        if a.straight and b.straight:
            (a0, a1), (b0, b1) = a.endpoints, b.endpoints
            return segments_copper_touch(a0, a1, 2 * a.half_width, b0, b1, 2 * b.half_width)
        return bool(a.geom.intersects(b.geom))
    if kinds == (TRACK, FILL):
        return bool(a.geom.intersects(b.geom))
    # FILL - FILL of different zones (same-zone pairs are decided by the
    # stroke-adjacency pass, Issue #5362).
    if a.zone == b.zone:
        return False
    return bool(a.geom.intersects(b.geom))


def build_copper_contact_graph(
    cv: ConnectivityValidator,
    net_number: int | None = None,
) -> CopperContactGraph:
    """Build the copper-contact graph for one net, or label-free for the board.

    Args:
        cv: Validator supplying the board and its layer/fill helpers.
        net_number: Restrict the graph to items carrying this net.  ``None``
            includes every item on the board and never consults a net label
            (the independent-LVS partition, Issue #3742).

    Requires shapely (callers keep their own core-only fallbacks).
    """
    from shapely.strtree import STRtree  # type: ignore[import-untyped]

    items = _collect_items(cv, net_number)
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        lr, rr = find(left), find(right)
        if lr != rr:
            parent[rr] = lr

    if len(items) > 1:
        tree = STRtree([item.geom for item in items])
        left, right = tree.query(
            [item.geom for item in items], predicate="dwithin", distance=_TOLERANCE * 1.01
        )
        for a, b in zip(left.tolist(), right.tolist(), strict=True):
            if a >= b:
                continue
            ia, ib = items[a], items[b]
            if not ia.layers & ib.layers:
                continue
            if find(a) == find(b):
                continue
            if _bonded(ia, ib):
                union(a, b)

    # Same-zone fill fragments: continuous iff their REAL copper meets, which
    # depends on the fill encoding (Issue #5362): ``fill_inflation`` is
    # ``min_thickness / 2`` on a stroked fill and zero on a solid one.
    zone_fills: dict[int, list[int]] = {}
    for index, item in enumerate(items):
        if item.kind == FILL:
            zone_fills.setdefault(item.zone, []).append(index)
    for zone_index, members in zone_fills.items():
        zone = cv.pcb.zones[zone_index]
        inflation = zone.fill_inflation() if hasattr(zone, "fill_inflation") else 0.0
        if inflation <= 0.0 or len(members) < 2:
            continue
        geoms = [items[i].geom for i in members]
        layers = [next(iter(items[i].layers)) for i in members]
        for a_pos, b_pos in cv._adjacent_fill_pairs(geoms, layers, 2.0 * inflation):
            union(members[a_pos], members[b_pos])

    return CopperContactGraph(items=items, component=[find(i) for i in range(len(items))])


def pad_items_by_key(graph: CopperContactGraph) -> dict[Any, CopperItem]:
    """Map ``(footprint_index, pad_index)`` -> pad item."""
    return {item.key: item for item in graph.items if item.kind == PAD}


def describe_item(cv: ConnectivityValidator, item: CopperItem) -> tuple[str, str]:
    """Stable id and human description for a relationship endpoint."""
    if item.kind == PAD:
        fi, pi = item.key
        fp = cv.pcb.footprints[fi]
        pad = fp.pads[pi]
        name = f"{fp.reference}.{pad.number}"
        return f"pad:{name}", f"Pad {name}"
    if item.kind == VIA:
        return f"via:{item.key}", f"Via {item.key}"
    if item.kind == TRACK:
        kind, index = item.key
        label = "Segment" if kind == "segment" else "Arc"
        return f"{kind}:{index}", f"{label} {index}"
    zone_index, fill_index = item.key
    layer = next(iter(item.layers))
    item_id = f"zone[{zone_index}].fill[{fill_index}]@{layer}"
    return item_id, f"Zone island {item_id}"


__all__ = [
    "CopperContactGraph",
    "CopperItem",
    "build_copper_contact_graph",
    "describe_item",
    "pad_items_by_key",
]
