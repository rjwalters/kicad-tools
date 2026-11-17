"""Conservative ampacity of copper pours (Issue #7206, follow-up F3).

``AmpacityRule`` rates track segments.  A net whose declared current flows
through a copper pour -- only a pour, or a pour plus tracks -- also needs
the pour rated, or a necked pour would read as a pass.  This module
estimates it from the committed fill geometry.

Method
======

The pour is sized by its **narrowest neck along the current path between
the net's pads**.  The probe width on copper layer ``L`` is ``w_L``, the
IPC-2221 width the track check would require there for the declared current
(:func:`kicad_tools.physics.ampacity.width_for_current`, with the same
``outer_copper_oz`` / ``inner_copper_oz`` external/internal split that
:class:`~kicad_tools.validate.rules.ampacity.AmpacityRule` uses).

* **In the pour.** The net's zone fill on ``L`` is eroded by ``w_L / 2``.
  What survives holds every ``w_L`` disk that fits in the fill, so two parts
  of it are joined only through copper at least ``w_L`` wide.
* **Into a pad, via or track (its entry).** An item joins a surviving part
  only through a flat-faced probe set against one of its edges: ``w_L`` wide
  from the edge out to depth ``w_L / 2``, capped by the far half of a
  ``w_L`` disk.  The probe must lie in the fill plus the item's own copper,
  and its disk centre must lie in the surviving part.  So the copper must be
  ``w_L`` wide right at the item.  A thermal spoke narrower than ``w_L``
  fails however short its gap is.  Only copper in front of the item counts,
  so a pad smaller than ``w_L`` fed by copper at least ``w_L`` wide is not
  capped at the pad's own size.
* **Bridges.** Items that touch (a track on a pad, a via in a pad) join
  directly.  A track's own width is ``AmpacityRule``'s job.

The net passes when every pad is joined.  The reported neck comes from
bisecting the probe scale: the widest ``s * w_L`` that still joins every pad.

What "conservative" covers
==========================

The estimate never reads a pour wider than its copper:

* every pad must be reached at full width, including pads that carry little
  of the current, such as a decoupling capacitor on a thermal spoke;
* parallel paths are never added together.  Four 1.0 mm spokes in parallel
  count as 1.0 mm;
* an oblique approach to an item reads narrower than it is, never wider,
  because the probe's flat face sits square on the item's edge;
* the IPC-2221 trace formula is applied to the neck as if it were a long
  trace.  A short neck between two wide pours runs cooler than that.

It does **not** rate everything on the path.  **Via barrels are not rated**,
the same as for tracks: a pour that hands its current to another layer
through one small via passes here on the strength of the copper on both
sides.  Track widths are rated by ``AmpacityRule``, not here.

Fail closed
===========

When no defensible estimate exists the net is **unmeasured**, and
``kct check`` reports ``ampacity`` as a blocking
``unknown:pour not measurable (<net>: <reason>)``.  A pour is never assumed
to be wide.  The reasons:

* ``shapely unavailable``;
* ``zone fill absent``: a copper zone of the net has no committed fill;
* ``N copper pad(s); no current path to rate``: fewer than two copper pads;
* ``pad R.N has no copper outline``: a pad's copper could not be modelled;
* ``no fill copper on a board copper layer``: the zones' fills lie on no
  copper layer of the board;
* ``no copper weight for <layer>``: an internal layer on a stackup with
  ``inner_copper_oz <= 0``;
* ``pour copper does not join pad(s) ... to the rest of the net``: the net's
  copper, as modelled here, does not join all its pads even before any
  erosion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools.physics.ampacity import width_for_current

from .violations import DRCViolation

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

# Copper that meets within this distance (mm) touches; matches the 2.5 um
# edge-contact tolerance of :mod:`kicad_tools.validate.copper_contact`.
_TOUCH = 0.0025
# Smallest erosion (mm): "is the copper connected at all" probe.
_R0 = 1e-4
# A channel exactly at the required width passes (``>=``, like tracks).
_PASS_SLACK = 1e-4
# Bisection steps for the reported neck (scale resolution ~ 2**-12).
_BISECT_STEPS = 12
# Arc resolution of the erosion, and the tolerance for finding a probe's
# disk centre in it (a polygonized offset sits up to ~r * 3e-4 inside).
_ERODE_QUAD_SEGS = 32
_ERODE_SLACK = 1e-3
# Segments of the probe's far semicircle; drawn circumscribed, so the probe
# polygon covers the true probe (it can only fail where the true one fits).
_PROBE_ARC_SEGS = 32
# Depth (mm) in front of an item edge at which the copper facing it is read
# to place the probe, and how far along the edge that read extends.
_FACE_DEPTH = 2 * _TOUCH
_FACE_REACH = 50.0
_EXTERNAL_LAYERS = ("F.Cu", "B.Cu")


@dataclass
class PourAmpacityResult:
    """Outcome of rating every declared net that has pour copper."""

    violations: list[DRCViolation] = field(default_factory=list)
    unmeasured: dict[str, str] = field(default_factory=dict)
    necks_mm: dict[str, float] = field(default_factory=dict)


@dataclass
class _Node:
    """A copper item the pour feeds: a pad, a via or a track."""

    kind: str  # "pad" | "track" | "via"
    label: str
    layers: frozenset[str]
    outline: dict[str, Any]  # layer -> the item's real copper outline
    location: tuple[float, float] = (0.0, 0.0)


@dataclass
class _Face:
    """One edge of an item's outline that has fill copper in front of it.

    ``origin`` is the edge's start, ``lateral`` runs along it (length
    ``length``) and ``normal`` points away from the item.  ``offsets`` are
    the probe positions along the edge: the centre of each interval of fill
    copper facing it, clamped onto the edge, plus the edge's ends and middle.
    """

    node: int
    origin: tuple[float, float]
    lateral: tuple[float, float]
    normal: tuple[float, float]
    length: float
    offsets: list[float]


def _same_net(obj: Any, net_name: str, net_number: int) -> bool:
    name = getattr(obj, "net_name", "") or ""
    if name:
        return bool(name == net_name)
    number = getattr(obj, "net_number", 0) or 0
    return bool(net_number and number == net_number)


def _board_copper_layers(pcb: Any) -> list[str]:
    try:
        names = [layer.name for layer in pcb.copper_layers]
    except (AttributeError, TypeError):
        names = []
    return names or ["F.Cu", "B.Cu"]


def _span(layers: list[str], board_layers: list[str]) -> frozenset[str]:
    """Copper layers a via spans (through: all; blind/buried: the range)."""
    named = [layer for layer in layers if layer in board_layers]
    if any(layer in ("*.Cu", "F&B.Cu") for layer in layers) or len(named) < 2:
        return frozenset(named or board_layers)
    i, j = sorted(board_layers.index(n) for n in named[:2])
    return frozenset(board_layers[i : j + 1])


def _required_width(current_a: float, layer: str, rules: DesignRules) -> float | None:
    if layer in _EXTERNAL_LAYERS:
        oz = rules.outer_copper_oz
        kind = "external"
    else:
        oz = rules.inner_copper_oz
        kind = "internal"
    if oz is None or oz <= 0:
        return None
    return float(width_for_current(current_a, copper_weight_oz=oz, layer=kind))


def _parts(geom: Any) -> list[Any]:
    if geom is None or geom.is_empty:
        return []
    return [g for g in getattr(geom, "geoms", [geom]) if not g.is_empty and g.area > 0]


def _probe(face: _Face, offset: float, r: float) -> tuple[Any, tuple[float, float]]:
    """The flat-faced probe at ``offset`` along ``face``, and its disk centre.

    In the face's frame (depth along the normal): the rectangle
    ``[-r, r] x [0, r]`` with its flat side on the item's edge, capped by the
    far half of the disk of radius ``r`` centred at depth ``r``.  Its width
    is ``2r`` from the item's edge outwards, so copper narrower than that at
    the edge -- a thermal spoke, however short -- cannot hold it.
    """
    import math

    from shapely.geometry import Polygon  # type: ignore[import-untyped]

    (ox, oy), (tx, ty), (nx, ny) = face.origin, face.lateral, face.normal
    cx, cy = ox + tx * offset, oy + ty * offset

    def at(lat: float, depth: float) -> tuple[float, float]:
        return (cx + tx * lat + nx * depth, cy + ty * lat + ny * depth)

    k = _PROBE_ARC_SEGS
    rc = r / math.cos(math.pi / (2 * k))  # circumscribed arc
    pts = [at(-r, 0.0), at(r, 0.0), at(r, r)]
    for i in range(1, k):
        a = math.pi * i / k
        pts.append(at(rc * math.cos(a), r + rc * math.sin(a)))
    pts.append(at(-r, r))
    return Polygon(pts), at(0.0, r)


class _NetModel:
    """One net's copper: per-layer fill regions and the items they feed.

    The fill region holds the zone fills only.  Pads, vias and tracks are
    items with their real outlines.  At probe width ``w = 2r`` the fill is
    eroded by ``r``; what survives holds every ``w`` disk that fits in the
    fill.  An item joins a surviving part only through a flat-faced probe
    (:func:`_probe`) set against one of its edges: the probe must lie inside
    the fill plus the item's own copper, and its disk centre must lie in that
    part.  A probe that merely touches the item with the tip of a disk is
    never enough, so a spoke narrower than ``w`` fails however short it is,
    while a pad smaller than ``w`` fed by copper at least ``w`` wide is not
    capped at its own size.
    """

    def __init__(
        self,
        regions: dict[str, Any],
        nodes: list[_Node],
        faces: dict[str, list[_Face]],
        holders: dict[tuple[str, int], Any],
    ) -> None:
        self.regions = regions
        self.nodes = nodes
        self.faces = faces
        self.holders = holders  # (layer, node) -> prepared fill + item copper
        self.pads = [i for i, n in enumerate(nodes) if n.kind == "pad"]
        # Items touching each other directly (track-pad, via-pad,
        # track-track, overlapping pads ...) on a shared layer.  Their own
        # widths are the track rule's job; independent of the erosion.
        self.bridge_pairs: list[tuple[int, int]] = []
        for i, a in enumerate(nodes):
            for j in range(i + 1, len(nodes)):
                b = nodes[j]
                for layer in a.layers & b.layers:
                    if a.outline[layer].distance(b.outline[layer]) <= _TOUCH:
                        self.bridge_pairs.append((i, j))
                        break

    def groups(self, radii: dict[str, float]) -> list[set[int]]:
        """Pad-index groups joined by fill copper eroded by ``radii[layer]``."""
        from shapely.geometry import Point
        from shapely.prepared import prep  # type: ignore[import-untyped]

        parent = list(range(len(self.nodes)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for layer, region in self.regions.items():
            r = radii[layer]
            eroded = region.buffer(-r, quad_segs=_ERODE_QUAD_SEGS)
            parts = _parts(eroded)
            if not parts:
                continue
            tol = _TOUCH + _ERODE_SLACK * r
            near = [prep(p.buffer(tol)) for p in parts]
            base = len(parent)
            parent.extend(range(base, base + len(parts)))
            for face in self.faces.get(layer, []):
                holder = self.holders[(layer, face.node)]
                for offset in face.offsets:
                    probe, centre = _probe(face, offset, r)
                    if not holder.contains(probe):
                        continue
                    pt = Point(centre)
                    for k, part in enumerate(near):
                        if part.contains(pt):
                            union(face.node, base + k)
                            break

        for a, b in self.bridge_pairs:
            union(a, b)

        by_root: dict[int, set[int]] = {}
        for p in self.pads:
            by_root.setdefault(find(p), set()).add(p)
        return list(by_root.values())

    def joined(self, radii: dict[str, float]) -> bool:
        return len(self.groups(radii)) <= 1


def _faces(region: Any, nodes: list[_Node], layer: str) -> list[_Face]:
    """Every item edge on ``layer`` with fill copper in front of it."""
    from shapely.geometry import LineString, Point

    out: list[_Face] = []
    for i, node in enumerate(nodes):
        if layer not in node.layers:
            continue
        outline = node.outline[layer]
        if region.distance(outline) > _TOUCH:
            continue
        for poly in getattr(outline, "geoms", [outline]):
            ring = list(getattr(poly, "exterior", poly).coords)
            for (x0, y0), (x1, y1) in zip(ring, ring[1:], strict=False):
                length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
                if length < 1e-9:
                    continue
                t = ((x1 - x0) / length, (y1 - y0) / length)
                n = (t[1], -t[0])
                mx, my = (x0 + x1) / 2, (y0 + y1) / 2
                if outline.contains(Point(mx + n[0] * 1e-4, my + n[1] * 1e-4)):
                    n = (-n[0], -n[1])
                fx, fy = x0 + n[0] * _FACE_DEPTH, y0 + n[1] * _FACE_DEPTH
                line = LineString(
                    [
                        (fx - t[0] * _FACE_REACH, fy - t[1] * _FACE_REACH),
                        (fx + t[0] * (length + _FACE_REACH), fy + t[1] * (length + _FACE_REACH)),
                    ]
                )
                facing = region.intersection(line)
                offsets = {0.0, length / 2, length}
                hit = False
                for piece in getattr(facing, "geoms", [facing]):
                    coords = list(getattr(piece, "coords", []))
                    if len(coords) < 2:
                        continue
                    lats = [(x - x0) * t[0] + (y - y0) * t[1] for x, y in coords]
                    lo, hi = min(lats), max(lats)
                    if hi < -_TOUCH or lo > length + _TOUCH:
                        continue  # copper beside the edge, not facing it
                    hit = True
                    offsets.add(min(max((lo + hi) / 2, 0.0), length))
                if hit:
                    out.append(
                        _Face(
                            node=i,
                            origin=(x0, y0),
                            lateral=t,
                            normal=n,
                            length=length,
                            offsets=sorted(offsets),
                        )
                    )
    return out


def _build_model(
    pcb: Any, net_name: str, net_number: int, board_layers: list[str]
) -> tuple[_NetModel | None, str | None]:
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from .rules.clearance import _collect_zone_fills, _pad_polygon
    from .rules.thermal_asymmetry import pad_copper_layers

    zones = list(getattr(pcb, "zones", []) or [])
    fills: dict[str, list[Any]] = {}
    for layer, layer_fills in _collect_zone_fills(pcb).items():
        for fill in layer_fills:
            zone = zones[fill.source_zone_index] if fill.source_zone_index >= 0 else None
            if zone is None or getattr(zone, "keepout", None) is not None:
                continue
            if not _same_net(fill, net_name, net_number):
                continue
            polygon: Any = fill.polygon
            inflation = zone.fill_inflation()
            if inflation > 0:
                polygon = polygon.buffer(inflation)
            fills.setdefault(layer, []).append(polygon)

    nodes: list[_Node] = []
    for fp in getattr(pcb, "footprints", []) or []:
        for pad in getattr(fp, "pads", []) or []:
            if not _same_net(pad, net_name, net_number):
                continue
            layers = pad_copper_layers(pad, board_layers)
            if not layers:
                continue
            outline = _pad_polygon(pad, fp)
            if outline is None or outline.is_empty:
                return None, f"pad {fp.reference}.{pad.number} has no copper outline"
            c = outline.centroid
            nodes.append(
                _Node(
                    kind="pad",
                    label=f"{fp.reference}.{pad.number}",
                    layers=frozenset(layers),
                    outline=dict.fromkeys(layers, outline),
                    location=(round(c.x, 3), round(c.y, 3)),
                )
            )
    if len(nodes) < 2:
        return None, f"{len(nodes)} copper pad(s); no current path to rate"

    tracks = [s for s in getattr(pcb, "segments", []) or [] if _same_net(s, net_name, net_number)]
    tracks += [a for a in getattr(pcb, "arcs", []) or [] if _same_net(a, net_name, net_number)]
    for t in tracks:
        layer = getattr(t, "layer", "")
        if layer not in board_layers:
            continue
        width = max(float(getattr(t, "width", 0.0) or 0.0), 0.0)
        pts = [t.start, t.mid, t.end] if hasattr(t, "mid") else [t.start, t.end]
        line = LineString(pts) if pts[0] != pts[-1] else Point(pts[0])
        copper = line.buffer(width / 2.0) if width > 0 else line
        nodes.append(
            _Node(
                kind="track",
                label="track",
                layers=frozenset({layer}),
                outline={layer: copper},
            )
        )
    for v in getattr(pcb, "vias", []) or []:
        if not _same_net(v, net_name, net_number):
            continue
        span = _span(list(getattr(v, "layers", []) or []), board_layers)
        size = max(float(v.size), 0.0)
        disk = Point(v.position).buffer(size / 2.0)
        nodes.append(_Node(kind="via", label="via", layers=span, outline=dict.fromkeys(span, disk)))

    from shapely.prepared import prep

    regions = {layer: unary_union(polys) for layer, polys in fills.items()}
    faces = {layer: _faces(region, nodes, layer) for layer, region in regions.items()}
    holders = {(layer, f.node): None for layer, layer_faces in faces.items() for f in layer_faces}
    for layer, node in list(holders):
        holders[(layer, node)] = prep(unary_union([regions[layer], nodes[node].outline[layer]]))
    return _NetModel(regions, nodes, faces, holders), None


def _unfilled_zone(pcb: Any, net_name: str, net_number: int) -> bool:
    for zone in getattr(pcb, "zones", []) or []:
        if getattr(zone, "keepout", None) is not None:
            continue
        if not _same_net(zone, net_name, net_number):
            continue
        if not getattr(zone, "filled_polygons", None):
            return True
    return False


def poured_nets(pcb: Any, nets: list[str]) -> list[str]:
    """The nets in ``nets`` with at least one copper (non-keepout) zone."""
    poured = {
        getattr(z, "net_name", "")
        for z in getattr(pcb, "zones", []) or []
        if getattr(z, "net_name", "") and getattr(z, "keepout", None) is None
    }
    return [n for n in nets if n in poured]


def measure_pour_ampacity(
    pcb: PCB, specs: dict[str, float], design_rules: DesignRules
) -> PourAmpacityResult:
    """Rate the pour copper of every declared net that has a copper zone."""
    result = PourAmpacityResult()
    nets = poured_nets(pcb, sorted(specs))
    if not nets:
        return result

    from kicad_tools._shapely import has_shapely

    if not has_shapely():
        for net in nets:
            result.unmeasured[net] = "shapely unavailable"
        return result

    board_layers = _board_copper_layers(pcb)
    name_to_number = {
        getattr(n, "name", ""): getattr(n, "number", 0)
        for n in (getattr(pcb, "nets", {}) or {}).values()
    }
    for net in nets:
        current = specs[net]
        number = int(name_to_number.get(net, 0) or 0)
        if _unfilled_zone(pcb, net, number):
            result.unmeasured[net] = "zone fill absent"
            continue
        model, reason = _build_model(pcb, net, number, board_layers)
        if model is None:
            result.unmeasured[net] = reason or "unmeasurable"
            continue
        if not model.regions:
            result.unmeasured[net] = "no fill copper on a board copper layer"
            continue
        required: dict[str, float] = {}
        for layer in model.regions:
            width = _required_width(current, layer, design_rules)
            if width is None:
                required = {}
                result.unmeasured[net] = f"no copper weight for {layer}"
                break
            required[layer] = width
        if not required:
            continue

        def radii(scale: float, _req: dict[str, float] = required) -> dict[str, float]:
            return {layer: max(scale * w / 2.0 - _PASS_SLACK, _R0) for layer, w in _req.items()}

        if not model.joined(dict.fromkeys(model.regions, _R0)):
            groups = model.groups(dict.fromkeys(model.regions, _R0))
            labels = sorted(model.nodes[i].label for i in min(groups, key=len))
            result.unmeasured[net] = (
                f"pour copper does not join pad(s) {', '.join(labels)} to the rest of the net"
            )
            continue

        # Widest scale that still joins every pad: the pour's neck.
        lo, hi = 0.0, 1.0
        while model.joined(radii(hi)) and hi < 1024:
            lo, hi = hi, hi * 2
        for _ in range(_BISECT_STEPS):
            mid = (lo + hi) / 2
            if model.joined(radii(mid)):
                lo = mid
            else:
                hi = mid
        # Report the neck on an outer layer when the pour has one.
        ref_layer = sorted(required, key=lambda name: (name not in _EXTERNAL_LAYERS, name))[0]
        neck = lo * required[ref_layer]
        result.necks_mm[net] = round(neck, 4)
        if model.joined(radii(1.0)):
            continue

        groups = model.groups(radii(1.0))
        cut = min(groups, key=len)
        labels = sorted(model.nodes[i].label for i in cut)
        where = model.nodes[min(cut)].location
        layer_class = "external" if ref_layer in _EXTERNAL_LAYERS else "internal"
        oz = (
            design_rules.outer_copper_oz
            if layer_class == "external"
            else design_rules.inner_copper_oz
        )
        result.violations.append(
            DRCViolation(
                rule_id="ampacity",
                severity="error",
                message=(
                    f"Pour on {ref_layer} too narrow for {current:.1f}A: narrowest neck "
                    f"{neck:.3f}mm cuts off pad(s) {', '.join(labels)}, requires "
                    f"{required[ref_layer]:.3f}mm (IPC-2221, {oz}oz {layer_class})"
                ),
                location=where,
                layer=ref_layer,
                actual_value=round(neck, 4),
                required_value=required[ref_layer],
                items=(net,),
                nets=(net,),
            )
        )
    return result
