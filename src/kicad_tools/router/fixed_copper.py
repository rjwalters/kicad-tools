"""Layer-specific physical fill obstacles shared by routing engines.

AABB bounds only accelerate queries; copper distance is evaluated against the
actual repaired polygon, including interior rings. Source identities are kept
for diagnostics but never grant same-net reuse of placement-invalid copper.

The clearance predicates themselves are the Phase 1b exact-geometry kernel's
(Epic #5509 Phase 3f, consumer group 6): :mod:`.fixed_copper_kernel` projects
each fill's copper onto the kernel's shapes and owns the spatial index the
query path needs. The engines reach these predicates through
``FixedFillObstacles.segment_clear`` / ``via_clear`` and nowhere else --
``pathfinder._fixed_step_clear``, ``diffpair_routing``, ``pad_access`` and
``cpp_backend``'s route validation all delegate here rather than composing a
gap of their own, so they are switched by delegation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

from ..core.arcs import arc_sweep
from .fixed_copper_kernel import KernelFill, fixed_fill_clear, kernel_fill, polygon_rings

logger = logging.getLogger(__name__)


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
    # Issue #5863: True when this record stands in for a pad the router could
    # not carry as a routable Pad on an OTHERWISE ROUTABLE net (its net is
    # excluded instead of the board being refused). ``geometry`` may then be a
    # conservative OVER-approximation, and a copper layer outside the routing
    # stack is skipped rather than refused: a layer nothing routes on cannot
    # host a conflict, and refusing it would restore the #5863 whole-board
    # abort through the back door.
    degraded: bool = False


@dataclass(frozen=True)
class FixedFillObstacles:
    fills: tuple[FixedFill, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.fills)

    @cached_property
    def kernel_fills(self) -> tuple[KernelFill, ...]:
        """This copper as indexed kernel shapes, built once per obstacle set.

        One entry per multipolygon lobe, from exactly the rings
        :meth:`native_polygons` hands ``Grid3D::add_fixed_fill`` -- so the
        Python and native halves of consumer group 6 judge the same copper.
        Built lazily because a set with no query is common (every engine holds
        an empty :class:`FixedFillObstacles` when the board has no preserved
        copper) and the walk is proportional to the pour's vertex count.
        """
        return tuple(
            indexed
            for layer, clearance, rings in self.native_polygons()
            if (indexed := kernel_fill(layer, clearance, rings)) is not None
        )

    def segment_clear(
        self,
        a: tuple[float, float],
        b: tuple[float, float],
        layer: int,
        half: float,
        clearance: float,
    ) -> bool:
        """Is a candidate trace (or, for ``a == b``, a via) clear of this copper?

        The same loop ``Grid3D::validate_route`` runs natively: every fill on
        the query's layer, each judged by the clearance kernel through
        :func:`~kicad_tools.router.fixed_copper_kernel.fixed_fill_clear`.  A
        fill's own clearance floor can only raise the caller's requirement,
        never lower it.
        """
        if not self.fills:
            return True
        reach = half + clearance
        return all(
            fixed_fill_clear(fill, a[0], a[1], b[0], b[1], layer, half, reach)
            for fill in self.kernel_fills
        )

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
            for rings in polygon_rings(fill.geometry):
                yield (fill.layer, fill.clearance, rings)


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
    # shapely ships no stubs; the module's first import carries the ignore
    # (it used to sit in ``segment_clear``, which no longer needs shapely --
    # the clearance predicate is the kernel's now, Epic #5509 Phase 3f).
    from shapely.geometry import Polygon  # type: ignore[import-untyped]

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


def _refuse_bound(reference: str, pad_number: str, detail: str) -> ValueError:
    return ValueError(
        f"Cannot bound the copper of pad {reference}.{pad_number}: {detail}; "
        "routing cannot treat this pad as an obstacle without under-estimating "
        "its copper. Route this board with a router supporting its full copper "
        "geometry."
    )


def _stroke_half_width(node, reference: str, pad_number: str) -> float:
    """Half the primitive's pen width -- how far its stroke leaves the outline."""
    stroke = node.find_child("stroke")
    width_node = stroke.find_child("width") if stroke is not None else node.find_child("width")
    width = width_node.get_float(0) if width_node is not None else 0.0
    if width is None or not math.isfinite(width) or width < 0:
        raise _refuse_bound(reference, pad_number, f"unusable stroke width {width}")
    return width / 2.0


def _node_points(node) -> list[tuple[float, float]] | None:
    """Every point this primitive is defined by, or None if one is unreadable."""
    points: list[tuple[float, float]] = []
    for child in node.children:
        if child.is_atom:
            continue
        if child.name in ("xy", "start", "mid", "end", "center"):
            px, py = child.get_float(0), child.get_float(1)
            if px is None or py is None:
                return None
            points.append((px, py))
            continue
        nested = _node_points(child)
        if nested is None:
            return None
        points.extend(nested)
    return points


def _arc_extremes(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Points whose bounding box contains the whole start-mid-end arc.

    The three authored points alone do NOT bound an arc (it bulges outside
    their hull), so this adds the axis extremes of the circumscribed circle
    that the swept angle actually reaches. Collinear (degenerate) points
    describe a straight segment, which the authored points do bound -- so a
    degenerate arc degrades to them here rather than refusing the board the
    way the Edge.Cuts outline reader does (#5863).
    """
    start, mid, end = points
    sweep = arc_sweep(start, mid, end)
    if sweep is None:
        return points
    return [*points, *sweep.axis_extreme_points()]


def _primitive_bound(node, reference: str, pad_number: str):
    """A box that is guaranteed to contain one primitive's copper.

    Over-approximation is the whole point: a bound that misses copper would
    let the router place a trace where the board already has metal, which is
    strictly worse than the over-blocking an enclosing box causes.
    """
    from shapely.geometry import box

    half = _stroke_half_width(node, reference, pad_number)
    points = _node_points(node)
    if not points:
        raise _refuse_bound(reference, pad_number, f"primitive {node.name!r} has no usable points")
    if node.name == "gr_circle" and len(points) >= 2:
        (cx, cy), (ex, ey) = points[0], points[1]
        radius = math.hypot(ex - cx, ey - cy)
        points = [(cx - radius, cy - radius), (cx + radius, cy + radius)]
    elif node.name == "gr_arc":
        if len(points) != 3:
            raise _refuse_bound(reference, pad_number, "gr_arc needs start, mid and end")
        points = _arc_extremes(points)
    xs = [px for px, _ in points]
    ys = [py for _, py in points]
    if not all(math.isfinite(value) for value in (*xs, *ys)):
        raise _refuse_bound(reference, pad_number, f"primitive {node.name!r} has non-finite points")
    return box(min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)


def bounding_pad_copper(
    pad_block: str,
    *,
    reference: str,
    pad_number: str,
    x: float,
    y: float,
    rotation: float,
    source_net: str,
    source_net_id: int,
) -> FixedPadCopper | None:
    """Conservative bounding copper of a pad the router cannot route (#5863).

    Returns ``None`` when the pad carries no copper layer at all (a paste- or
    mask-only pad, e.g. the QFN thermal-paste patterns on half the pinned
    dataset-srj18 boards): there is no copper to preserve, so the pad is
    neither a target nor an obstacle.

    Otherwise the result's ``geometry`` is the union of per-primitive boxes
    (plus the anchor), each an over-approximation of that primitive's copper --
    never an under-approximation. A layer-specific padstack still refuses:
    its per-layer copper is not derivable from the common block, and a bound
    that silently assumed otherwise could under-estimate real metal.
    """
    from shapely.affinity import rotate, translate
    from shapely.geometry import box
    from shapely.ops import unary_union

    from kicad_tools.sexp import parse_string

    node = parse_string(pad_block)
    atoms = [str(atom) for atom in node.get_atoms()]
    shape = atoms[2] if node.name == "pad" and len(atoms) >= 3 else ""
    layers_node = node.find_child("layers")
    names = tuple(str(name) for name in (layers_node.get_atoms() if layers_node else ()))
    copper_layers = tuple(name for name in names if name.endswith(".Cu"))
    if not copper_layers:
        return None
    if node.find_child("padstack") is not None:
        raise _refuse_bound(reference, pad_number, "layer-specific padstack copper")
    size = node.find_child("size")
    width = size.get_float(0) if size is not None else None
    height = size.get_float(1) if size is not None else None
    if width is None or height is None or not (width > 0 and height > 0):
        raise _refuse_bound(
            reference, pad_number, f"missing or non-positive size ({width}, {height})"
        )
    if shape == "custom":
        options = node.find_child("options")
        anchor_node = options.find_child("anchor") if options is not None else None
        anchor = str(
            anchor_node.get_string(0) or "" if anchor_node is not None else "circle"
        ).lower()
        # A circle anchor is KiCad's size.x disc; bound both axes by the larger
        # nominal dimension so an unequal (malformed) size cannot clip it.
        half_x, half_y = (
            (width / 2.0, height / 2.0) if anchor == "rect" else (max(width, height) / 2.0,) * 2
        )
        shapes = [box(-half_x, -half_y, half_x, half_y)]
        primitives = node.find_child("primitives")
        for child in primitives.children if primitives is not None else []:
            if child.is_atom:
                continue
            shapes.append(_primitive_bound(child, reference, pad_number))
    elif shape == "trapezoid":
        # KiCad's trapezoid corners are the nominal rectangle's, each shifted
        # by half the rect_delta of the OTHER axis, so the copper reaches
        # |delta|/2 past the nominal box (pad.cpp, PAD_SHAPE::TRAPEZOID).
        delta = node.find_child("rect_delta")
        dx = (delta.get_float(0) if delta is not None else 0.0) or 0.0
        dy = (delta.get_float(1) if delta is not None else 0.0) or 0.0
        if not (math.isfinite(dx) and math.isfinite(dy)):
            raise _refuse_bound(reference, pad_number, f"non-finite rect_delta ({dx}, {dy})")
        half_x = width / 2.0 + abs(dy) / 2.0
        half_y = height / 2.0 + abs(dx) / 2.0
        shapes = [box(-half_x, -half_y, half_x, half_y)]
    else:
        raise _refuse_bound(reference, pad_number, f"unsupported pad shape {shape!r}")
    local = unary_union(shapes)
    if local.is_empty or local.geom_type not in ("Polygon", "MultiPolygon"):
        raise _refuse_bound(reference, pad_number, "bounds enclose no copper")
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
        degraded=True,
    )


def degraded_pad_copper(
    pad_block: str,
    *,
    reference: str,
    pad_number: str,
    x: float,
    y: float,
    rotation: float,
    source_net: str,
    source_net_id: int,
) -> FixedPadCopper | None:
    """Obstacle copper for a pad on a ROUTABLE net the router cannot route.

    Issue #5863: an unsupported pad shape used to refuse the whole board even
    when only one pad was affected. The pad now degrades instead -- exact
    copper when :func:`custom_pad_copper` can reproduce it, else the
    conservative bound of :func:`bounding_pad_copper`, and ``None`` when the
    pad has no copper at all. The caller excludes the pad's net from routing,
    so nothing downstream mistakes this copper for a routable terminal.
    """
    kwargs: dict[str, Any] = {
        "reference": reference,
        "pad_number": pad_number,
        "x": x,
        "y": y,
        "rotation": rotation,
        "source_net": source_net,
        "source_net_id": source_net_id,
    }
    try:
        exact = custom_pad_copper(pad_block, **kwargs)
    except ValueError:
        return bounding_pad_copper(pad_block, **kwargs)
    return replace(exact, degraded=True)


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
                if pad.degraded:
                    # Issue #5863: this pad's net is excluded from routing and
                    # nothing routes on a layer outside the stack, so copper
                    # there cannot conflict -- skip it instead of restoring the
                    # whole-board refusal this path exists to remove.
                    logger.warning(
                        "Pad %s.%s copper layer %r is outside the routing stack; "
                        "skipped as an obstacle (its net is excluded from routing)",
                        pad.reference,
                        pad.pad_number,
                        name,
                    )
                    continue
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

    from kicad_tools.schema.pcb import PCB, Segment, Via
    from kicad_tools.validate.rules.clearance import _collect_zone_fills

    from .layers import Layer

    pcb = PCB.load(pcb_path)
    ox, oy = pcb._board_origin
    result = []
    selected_layers = {layer.name: layer.index for layer in grid.layer_stack.layers}
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
        if layer_name not in selected_layers:
            continue
        layer = selected_layers[layer_name]
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
        if (
            name not in names
            or legacy is None
            or zone.filled_polygons
            or zone.layer not in selected_layers
        ):
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
                layer=selected_layers[zone.layer],
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
        if name not in names or arc.layer not in selected_layers:
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
                layer=selected_layers[arc.layer],
                clearance=clearance,
                geometry=translate(geometry, ox, oy),
                source_kind="arc",
                source_object_id=arc.uuid,
            )
        )
    # Placement-excluded tracks/vias have neutral ownership in existing_routes
    # so no eligible net may reuse their copper. Their class clearance must
    # survive separately: resolving the class from neutral net 0 loses it,
    # and several excluded nets may have different clearance requirements.
    # Retain source identity here so a later CLI sidecar can grow the gap.
    from shapely.geometry import Point

    fixed_items: list[Segment | Via] = [*pcb.segments, *pcb.vias]
    for item in fixed_items:
        name = item.net_name or (
            pcb.nets[item.net_number].name if item.net_number in pcb.nets else ""
        )
        net_class = net_class_map.get(name)
        if name not in names:
            continue
        clearance = max(grid.rules.trace_clearance, net_class.clearance if net_class else 0.0)
        if isinstance(item, Segment):
            geometry = LineString((item.start, item.end)).buffer(
                item.width / 2 * _ROUND_OUT, quad_segs=64
            )
            authored_layer = Layer.from_kicad_name(item.layer)
            indices = [
                layer.index
                for layer in grid.layer_stack.layers
                if layer.layer_enum == authored_layer
            ]
            kind = "segment"
        else:
            geometry = Point(item.position).buffer(item.size / 2 * _ROUND_OUT, quad_segs=64)
            # Via endpoints describe the physical span, independently of
            # which layers this routing trial selected. A through via still
            # blocks F.Cu during a front-only trial with no B.Cu grid index.
            lo, hi = sorted(Layer.from_kicad_name(layer).value for layer in item.layers)
            indices = [
                layer.index
                for layer in grid.layer_stack.layers
                if lo <= layer.layer_enum.value <= hi
            ]
            kind = "via"
        for layer in indices:
            result.append(
                FixedFill(
                    source_net=name,
                    source_net_id=item.net_number,
                    layer=layer,
                    clearance=clearance,
                    geometry=translate(geometry, ox, oy),
                    source_kind=kind,
                    source_object_id=item.uuid,
                )
            )
    return FixedFillObstacles(tuple(result))
