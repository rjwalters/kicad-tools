"""Layer-specific physical fill obstacles shared by routing engines.

AABB bounds only accelerate queries; copper distance is evaluated against the
actual repaired polygon, including interior rings. Source identities are kept
for diagnostics but never grant same-net reuse of placement-invalid copper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FixedFill:
    source_net: str
    source_net_id: int
    layer: int
    clearance: float
    geometry: Any
    source_zone_id: str = ""
    source_kind: str = "zone"
    source_object_id: str = ""


@dataclass(frozen=True)
class FixedPadCopper:
    """Actual board-frame copper of one placement-excluded custom pad.

    ``geometry`` is the union of the authored anchor and supported primitives
    after the pad's own transform; ``layers`` holds the pad's copper layer
    names (``*.Cu`` kept verbatim for later stack expansion). The source pad
    block itself is never rewritten -- this record only feeds obstacles.
    """

    reference: str
    pad_number: str
    source_net: str
    source_net_id: int
    layers: tuple[str, ...]
    local_clearance: float
    geometry: Any


@dataclass(frozen=True)
class FixedFillObstacles:
    fills: tuple[FixedFill, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.fills)

    def segment_clear(
        self,
        a: tuple[float, float],
        b: tuple[float, float],
        layer: int,
        half: float,
        clearance: float,
    ) -> bool:
        if not self.fills:
            return True
        from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

        query = Point(a) if a == b else LineString((a, b))
        for fill in self.fills:
            if fill.layer != layer:
                continue
            required = half + max(clearance, fill.clearance)
            x0, y0, x1, y1 = fill.geometry.bounds
            if (
                max(a[0], b[0]) + required < x0
                or min(a[0], b[0]) - required > x1
                or max(a[1], b[1]) + required < y0
                or min(a[1], b[1]) - required > y1
            ):
                continue
            # Intersections remain forbidden even for a zero-width query.
            if query.intersects(fill.geometry) or query.distance(fill.geometry) < required - 1e-4:
                return False
        return True

    def via_clear(
        self,
        point: tuple[float, float],
        layers: tuple[int, ...],
        radius: float,
        clearance: float,
    ) -> bool:
        return all(self.segment_clear(point, point, layer, radius, clearance) for layer in layers)

    def native_polygons(self):
        """Simple polygons with holes; preserve every lobe of a multipolygon."""
        for fill in self.fills:
            geometries = (
                fill.geometry.geoms
                if fill.geometry.geom_type == "MultiPolygon"
                else (fill.geometry,)
            )
            for geometry in geometries:
                yield (
                    fill.layer,
                    fill.clearance,
                    [
                        list(geometry.exterior.coords),
                        *[list(ring.coords) for ring in geometry.interiors],
                    ],
                )


# Outward rounding for polygonal circle approximations: a tessellated disc
# is inscribed, so scale it by the sagitta bound to keep copper conservative.
_ROUND_OUT = 1.0 / math.cos(math.pi / 256)
# Forms whose actual copper this module reproduces exactly. Anything else is
# refused rather than approximated -- a nominal or enclosing box would either
# drop copper or seal a legal corridor.
_FILLED_TOKENS = {"yes", "true", "solid"}
_ANCHOR_SHAPES = {"rect", "circle"}


def _refuse(reference: str, pad_number: str, detail: str) -> ValueError:
    return ValueError(
        f"Cannot preserve placement-excluded custom pad {reference}.{pad_number}: "
        f"{detail}; only filled zero-width gr_poly primitives with a rect or circle "
        "anchor are represented as fixed copper. Route this board with a router "
        "supporting its full copper geometry."
    )


def _primitive_polygon(node, reference: str, pad_number: str):
    """Actual copper of one supported primitive, in the pad's local frame."""
    from shapely.geometry import Polygon

    if node.name != "gr_poly":
        raise _refuse(reference, pad_number, f"unsupported primitive {node.name!r}")
    stroke = node.find_child("stroke")
    width_node = stroke.find_child("width") if stroke is not None else node.find_child("width")
    width = width_node.get_float(0) if width_node is not None else 0.0
    if width is None or not math.isfinite(width) or width != 0:
        raise _refuse(reference, pad_number, f"unsupported gr_poly stroke width {width}")
    fill = node.find_child("fill")
    token = ""
    if fill is not None:
        nested = fill.find_child("type")
        token = str((nested or fill).get_string(0) or "").lower()
    if token not in _FILLED_TOKENS:
        raise _refuse(reference, pad_number, f"unfilled or unrecognized gr_poly fill {token!r}")
    pts = node.find_child("pts")
    points = [(xy.get_float(0), xy.get_float(1)) for xy in (pts.find_children("xy") if pts else [])]
    if len(points) < 3 or any(x is None or y is None for x, y in points):
        raise _refuse(reference, pad_number, "gr_poly needs at least three complete points")
    polygon = Polygon(points)
    if not polygon.is_valid:
        # GEOS repair is not KiCad topology: buffer(0) can discard an entire
        # bow-tie lobe and incorrectly clear a route through real copper.
        raise _refuse(reference, pad_number, "invalid or self-intersecting gr_poly")
    if polygon.is_empty or polygon.geom_type not in ("Polygon", "MultiPolygon"):
        raise _refuse(reference, pad_number, "gr_poly encloses no copper")
    return polygon


def custom_pad_copper(
    pad_block: str,
    *,
    reference: str,
    pad_number: str,
    x: float,
    y: float,
    rotation: float,
    source_net: str,
    source_net_id: int,
) -> FixedPadCopper:
    """Transformed copper of a custom pad, read from its canonical source block.

    ``x``/``y`` are the pad's board position and ``rotation`` its ABSOLUTE
    board-frame angle (KiCad folds the footprint orientation into the pad's
    own ``(at ...)`` -- issue #3902), so the anchor and primitives are rotated
    by the negated angle exactly once, matching KiCad's forward transform and
    ``validate.rules.clearance._pad_polygon``. Nominal ``(size ...)`` is only
    the anchor: primitives routinely reach past it, which is why this reads
    the source S-expression instead of the schema's nominal dimensions.
    """
    from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
    from shapely.geometry import Point, box
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from kicad_tools.sexp import parse_string

    node = parse_string(pad_block)
    atoms = [str(atom) for atom in node.get_atoms()]
    if node.name != "pad" or len(atoms) < 3 or atoms[2] != "custom":
        raise _refuse(reference, pad_number, f"shape {(atoms[2:3] or [''])[0]!r} is not custom")
    if node.find_child("padstack") is not None:
        raise _refuse(reference, pad_number, "layer-specific padstack copper")
    size = node.find_child("size")
    width = size.get_float(0) if size is not None else None
    height = size.get_float(1) if size is not None else None
    if not width or not height or width <= 0 or height <= 0:
        raise _refuse(reference, pad_number, f"missing or non-positive size ({width}, {height})")
    layers_node = node.find_child("layers")
    names = tuple(str(name) for name in (layers_node.get_atoms() if layers_node else ()))
    copper_layers = tuple(name for name in names if name.endswith(".Cu"))
    if not copper_layers:
        raise _refuse(reference, pad_number, f"no copper layer in {list(names)}")
    options = node.find_child("options")
    anchor_node = options.find_child("anchor") if options is not None else None
    anchor = str(anchor_node.get_string(0) or "" if anchor_node is not None else "circle").lower()
    if anchor not in _ANCHOR_SHAPES:
        raise _refuse(reference, pad_number, f"unsupported anchor {anchor!r}")
    if anchor == "rect":
        shapes = [box(-width / 2, -height / 2, width / 2, height / 2)]
    else:
        if abs(width - height) > 1e-9:
            raise _refuse(reference, pad_number, f"circle anchor with unequal size {anchor!r}")
        shapes = [Point(0, 0).buffer(width / 2 * _ROUND_OUT, quad_segs=64)]
    primitives = node.find_child("primitives")
    for child in primitives.children if primitives is not None else []:
        if child.is_atom:
            continue
        shapes.append(_primitive_polygon(child, reference, pad_number))
    local = unary_union(shapes)
    if local.is_empty or local.geom_type not in ("Polygon", "MultiPolygon"):
        raise _refuse(reference, pad_number, "primitives enclose no copper")
    clearance_node = node.find_child("clearance")
    local_clearance = clearance_node.get_float(0) if clearance_node is not None else None
    return FixedPadCopper(
        reference=reference,
        pad_number=pad_number,
        source_net=source_net,
        source_net_id=source_net_id,
        layers=copper_layers,
        local_clearance=local_clearance or 0.0,
        geometry=translate(rotate(local, -rotation, origin=(0, 0)), x, y),
    )


def pad_fixed_fills(pads, grid, net_class_map) -> tuple[FixedFill, ...]:
    """One physical obstacle per copper layer the excluded pad actually covers.

    Copper-layer expansion never includes paste/mask layers, and a named layer
    outside the routing stack refuses rather than silently dropping copper.
    """
    from .layers import Layer

    fills = []
    for pad in pads:
        net_class = net_class_map.get(pad.source_net)
        clearance = max(
            grid.rules.trace_clearance,
            pad.local_clearance,
            net_class.clearance if net_class is not None else 0.0,
        )
        indices: set[int] = set()
        for name in pad.layers:
            if name == "*.Cu":
                indices.update(range(grid.num_layers))
                continue
            try:
                indices.add(grid.layer_to_index(Layer.from_kicad_name(name).value))
            except Exception as exc:
                raise _refuse(
                    pad.reference, pad.pad_number, f"copper layer {name!r} is not in the stack"
                ) from exc
        for index in sorted(indices):
            fills.append(
                FixedFill(
                    source_net=pad.source_net,
                    source_net_id=pad.source_net_id,
                    layer=index,
                    clearance=clearance,
                    geometry=pad.geometry,
                    source_kind="pad",
                    source_object_id=f"{pad.reference}.{pad.pad_number}",
                )
            )
    return tuple(fills)


def load_fixed_fills(pcb_path, names, grid, net_class_map) -> FixedFillObstacles:
    """Reuse the DRC's actual-fill topology and convert to sheet coordinates."""
    if not names:
        return FixedFillObstacles()
    from shapely.affinity import translate

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.clearance import _collect_zone_fills

    from .layers import Layer

    pcb = PCB.load(pcb_path)
    ox, oy = pcb._board_origin
    result = []
    zones = list(pcb.zones)
    source_zones = list(pcb._sexp.find_all("zone"))
    version_node = pcb._sexp.find("version")
    version = version_node.get_int(0) if version_node else 0

    def stroke_width(index):
        node = source_zones[index].find("filled_areas_thickness")
        # KiCad's PCB_IO_KICAD_SEXPR_PARSER: pre-20250210 fills carry
        # min-thickness edge strokes unless explicitly disabled.
        stroked = (version or 0) < 20250210 and not (node and node.get_string(0) == "no")
        return zones[index].min_thickness / 2 if stroked else 0.0

    for layer_name, fills in _collect_zone_fills(pcb).items():
        layer = grid.layer_to_index(Layer.from_kicad_name(layer_name).value)
        for fill in fills:
            if fill.net_name not in names:
                continue
            net_class = net_class_map.get(fill.net_name)
            clearance = max(
                grid.rules.trace_clearance,
                fill.source_clearance,
                net_class.clearance if net_class is not None else 0.0,
            )
            geometry: Any = fill.polygon
            stroke = stroke_width(fill.source_zone_index)
            if stroke > 0:
                geometry = geometry.buffer(stroke / math.cos(math.pi / 256), quad_segs=64)
            result.append(
                FixedFill(
                    source_net=fill.net_name,
                    source_net_id=fill.net_number,
                    layer=layer,
                    clearance=clearance,
                    geometry=translate(geometry, ox, oy),
                    source_zone_id=fill.source_zone_id,
                )
            )
    # The upstream parser prefers polygon fills when both legacy encodings
    # exist; segment-only fills are round strokes of min_thickness.
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    for index, zone in enumerate(zones):
        name = zone.net_name or (
            pcb.nets[zone.net_number].name if zone.net_number in pcb.nets else ""
        )
        legacy = source_zones[index].find("fill_segments")
        if name not in names or legacy is None or zone.filled_polygons:
            continue
        if zone.min_thickness <= 0:
            raise ValueError("Legacy fill_segments requires positive min_thickness")
        strokes = []
        for pts in legacy.find_all("pts"):
            points = [(xy.get_float(0), xy.get_float(1)) for xy in pts.find_all("xy")]
            if len(points) != 2 or any(x is None or y is None for x, y in points):
                raise ValueError("Malformed legacy fill_segments copper")
            strokes.append(
                LineString(points).buffer(
                    zone.min_thickness / 2 / math.cos(math.pi / 256), quad_segs=64
                )
            )
        if not strokes:
            continue
        klass = net_class_map.get(name)
        result.append(
            FixedFill(
                source_net=name,
                source_net_id=zone.net_number,
                layer=grid.layer_to_index(Layer.from_kicad_name(zone.layer).value),
                clearance=max(
                    grid.rules.trace_clearance, zone.clearance, klass.clearance if klass else 0
                ),
                geometry=unary_union(strokes),
                source_zone_id=zone.uuid,
                source_kind="legacy_fill_segments",
            )
        )
    # Preserve authored arcs exactly in the output while using bounded-error
    # physical copper for search. Inflate by the centerline sagitta bound so
    # tessellation cannot cut away the outer face of the true circular stroke.
    from shapely.geometry import LineString

    for arc in pcb.arcs:
        name = arc.net_name or (pcb.nets[arc.net_number].name if arc.net_number in pcb.nets else "")
        if name not in names:
            continue
        net_class = net_class_map.get(name)
        clearance = max(grid.rules.trace_clearance, net_class.clearance if net_class else 0.0)
        error = 0.00001
        geometry = LineString(arc.centerline_points(max_error_mm=error)).buffer(
            (arc.width / 2 + error) / math.cos(math.pi / 256),
            quad_segs=64,
        )
        result.append(
            FixedFill(
                source_net=name,
                source_net_id=arc.net_number,
                layer=grid.layer_to_index(Layer.from_kicad_name(arc.layer).value),
                clearance=clearance,
                geometry=translate(geometry, ox, oy),
                source_kind="arc",
                source_object_id=arc.uuid,
            )
        )
    return FixedFillObstacles(tuple(result))
