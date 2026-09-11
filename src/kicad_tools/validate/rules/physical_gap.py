"""Opt-in physical gap check on the boundary of the per-layer copper union.

Electrical net membership never excludes a candidate. Joining primitive edges
are removed by union before measuring air between facing boundary edges.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..violations import DRCResults, DRCViolation
from .clearance import _pad_on_layer, _pad_polygon, _repair_fill_polygon

# Polygon approximation and comparison tolerance, in millimetres.
TOLERANCE = 0.001


@dataclass
class CopperSource:
    geometry: Any
    layer: str
    net: str
    identity: str


def _arc_geometry(node):
    """Flatten a modern start/mid/end track arc with <=0.1um chord error."""
    from shapely.geometry import LineString  # type: ignore[import-untyped]

    points = []
    for key in ("start", "mid", "end"):
        point = node.find_child(key)
        if point is None:
            raise ValueError("unsupported legacy or incomplete copper arc")
        points.append((point.get_float(0), point.get_float(1)))
    (x1, y1), (x2, y2), (x3, y3) = points
    determinant = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(determinant) < 1e-12:
        raise ValueError("degenerate copper arc")
    q1, q2, q3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    cx = (q1 * (y2 - y3) + q2 * (y3 - y1) + q3 * (y1 - y2)) / determinant
    cy = (q1 * (x3 - x2) + q2 * (x1 - x3) + q3 * (x2 - x1)) / determinant
    radius = math.hypot(x1 - cx, y1 - cy)
    angles = [math.atan2(y - cy, x - cx) for x, y in points]
    sweep = (angles[2] - angles[0]) % math.tau
    if (angles[1] - angles[0]) % math.tau > sweep:
        sweep -= math.tau
    step = 2 * math.acos(max(-1.0, 1 - 0.0001 / radius))
    count = max(2, math.ceil(abs(sweep) / max(step, 1e-6)))
    width = node.find_child("width")
    if width is None or width.get_float(0) <= 0:
        raise ValueError("missing copper arc width")
    return LineString(
        [
            (
                cx + radius * math.cos(angles[0] + sweep * i / count),
                cy + radius * math.sin(angles[0] + sweep * i / count),
            )
            for i in range(count + 1)
        ]
    ).buffer(width.get_float(0) / 2, quad_segs=64)


def _raw_geometry_issues(pcb):
    """Do not let tolerant schema recovery manufacture supported copper.

    An invalid source primitive makes this invocation incomplete before any
    recovered/defaulted geometry is used. Chamfer modifiers are native geometry
    outside this rule's current supported pad subset.
    """
    issues = []
    layers = {layer.name for layer in pcb.copper_layers}

    def numeric(node, tag, lengths, *, required=True, positive=False):
        fields = [c for c in node.children if c.name == tag]
        if not fields and not required:
            return
        if len(fields) != 1:
            issues.append(f"invalid {node.name} {tag}: missing or repeated field")
            return
        atoms = fields[0].children
        values = [fields[0].get_float(i) for i in range(len(atoms))]
        if len(values) not in lengths or any(
            not c.is_atom
            or isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or (positive and v <= 0)
            for c, v in zip(atoms, values, strict=True)
        ):
            issues.append(f"invalid {node.name} {tag}: finite numeric geometry required")

    for node in pcb._sexp.children:
        if node.name in {"segment", "arc"}:
            for tag in ("start", "end"):
                numeric(node, tag, {2})
            if node.name == "arc":
                numeric(node, "mid", {2})
            numeric(node, "width", {1}, positive=True)
            layer = node.find_child("layer")
            if layer is None or layer.get_string(0) not in layers:
                issues.append(f"unresolved {node.name} copper layer")
        elif node.name == "via":
            numeric(node, "at", {2})
            numeric(node, "size", {1}, positive=True)
            span = node.find_child("layers")
            if (
                span is None
                or len(span.children) != 2
                or any(c.value not in layers for c in span.children)
            ):
                issues.append("unresolved via layer span")
        elif node.name in {"footprint", "module"}:
            numeric(node, "at", {2, 3}, required=False)
            for pad in node.find_all("pad"):
                if pad.get_string(1) == "np_thru_hole":
                    continue
                numeric(pad, "at", {2, 3})
                numeric(pad, "size", {2}, positive=True)
                numeric(pad, "roundrect_rratio", {1}, required=False)
                ratio = pad.find_child("roundrect_rratio")
                if ratio is not None:
                    value = ratio.get_float(0)
                    if value is None or not 0 <= value <= 0.5:
                        issues.append("invalid pad roundrect ratio")
                if any(pad.find_child(tag) is not None for tag in ("chamfer", "chamfer_ratio")):
                    issues.append("unsupported pad chamfer geometry")
        elif node.name == "zone":
            for fill in node.find_all("filled_polygon"):
                pts = fill.find_child("pts")
                if pts is None:
                    issues.append("invalid filled polygon: missing points")
                else:
                    for xy in pts.children:
                        if xy.name == "xy":
                            values = [xy.get_float(i) for i in range(len(xy.children))]
                            if len(values) != 2 or any(
                                not isinstance(v, (int, float)) or not math.isfinite(v)
                                for v in values
                            ):
                                issues.append("invalid filled polygon coordinates")
    return issues


def _collect(pcb):
    from shapely.geometry import LineString, Point, Polygon

    sources: list[CopperSource] = []
    unsupported = _raw_geometry_issues(pcb)
    if unsupported:
        return sources, unsupported
    layer_names = [layer.name for layer in pcb.copper_layers]

    def net_name(number):
        return pcb.nets[number].name if number in pcb.nets else str(number)

    for index, segment in enumerate(pcb.segments):
        sources.append(
            CopperSource(
                LineString([segment.start, segment.end]).buffer(segment.width / 2, quad_segs=64),
                segment.layer,
                net_name(segment.net_number),
                segment.uuid or f"segment:{index}",
            )
        )
    for fp in pcb.footprints:
        if any(graphic.layer in layer_names for graphic in fp.graphics):
            unsupported.append(f"unsupported footprint copper graphic: {fp.reference}")
        for index, pad in enumerate(fp.pads):
            if pad.type == "np_thru_hole":
                continue
            if pad.shape not in {"circle", "rect", "oval", "obround", "roundrect"}:
                unsupported.append(f"unsupported pad shape: {fp.reference}:{pad.number}")
                continue
            geom = _pad_polygon(pad, fp)
            if geom is None:
                unsupported.append(f"degenerate pad: {fp.reference}:{pad.number}")
                continue
            for layer in layer_names:
                if _pad_on_layer(pad, layer):
                    sources.append(
                        CopperSource(
                            geom,
                            layer,
                            net_name(pad.net_number),
                            pad.uuid or f"{fp.reference}:pad:{index}",
                        )
                    )
    for index, via in enumerate(pcb.vias):
        # A barrel occupies every layer between the declared endpoints.
        if any(layer not in layer_names for layer in via.layers):
            unsupported.append(f"unresolved via layers: {via.uuid}")
            continue
        first, last = sorted(layer_names.index(layer) for layer in via.layers)
        for layer in layer_names[first : last + 1]:
            sources.append(
                CopperSource(
                    Point(via.position).buffer(via.size / 2, quad_segs=64),
                    layer,
                    net_name(via.net_number),
                    via.uuid or f"via:{index}",
                )
            )
    for index, zone in enumerate(pcb.zones):
        if not zone.filled_polygons and not zone.keepout:
            unsupported.append(f"unfilled zone: {zone.uuid or index}")
        for fill_index, points in enumerate(zone.filled_polygons):
            if len(points) >= 3:
                sources.append(
                    CopperSource(
                        _repair_fill_polygon(Polygon(points)),
                        zone.filled_polygon_layer(fill_index),
                        zone.net_name or net_name(zone.net_number),
                        zone.uuid or f"zone:{index}",
                    )
                )
    # Copper track arcs are not represented in PCB.segments; read only these
    # nodes from the source tree instead of silently dropping them.
    for index, node in enumerate(pcb._sexp.children):
        if node.name not in {"arc", "gr_line", "gr_arc", "gr_rect", "gr_poly", "gr_circle"}:
            continue
        layer_node = node.find_child("layer")
        layer = layer_node.get_string(0) if layer_node else ""
        if layer not in layer_names:
            continue
        if node.name != "arc":
            unsupported.append(f"unsupported copper graphic: {node.name}:{index}")
            continue
        try:
            geom = _arc_geometry(node)
        except (ValueError, TypeError) as exc:
            unsupported.append(str(exc))
            continue
        identifier = node.find_child("uuid")
        net = node.find_child("net")
        value = net.get_first_atom() if net else 0
        sources.append(
            CopperSource(
                geom,
                layer,
                net_name(value) if isinstance(value, int) else str(value),
                identifier.get_string(0) if identifier else f"arc:{index}",
            )
        )
    return sources, unsupported


def check_physical_copper_gap(pcb, minimum_mm: float) -> DRCResults:
    """Report open gaps; never reinterpret a copper join as zero clearance.

    Facing-edge screening excludes local corners. This is a slit detector,
    not a guarantee about acute notches, etching chemistry or stale zone fills.
    """
    from shapely import STRtree, union_all  # type: ignore[import-untyped]
    from shapely.geometry import LineString
    from shapely.geometry.polygon import orient  # type: ignore[import-untyped]
    from shapely.ops import nearest_points  # type: ignore[import-untyped]

    if not math.isfinite(minimum_mm) or minimum_mm <= 0:
        raise ValueError("Physical copper gap must be finite and positive")
    sources, unsupported = _collect(pcb)
    results = DRCResults(rules_checked=1)
    for detail in sorted(set(unsupported)):
        results.add(
            DRCViolation(
                "physical_copper_gap_incomplete",
                "error",
                f"Physical gap coverage incomplete: {detail}",
            )
        )
    for layer in sorted({source.layer for source in sources}):
        local = [s for s in sources if s.layer == layer and not s.geometry.is_empty]
        union = union_all([s.geometry for s in local])
        polygons = [union] if union.geom_type == "Polygon" else list(union.geoms)
        edges, normals = [], []
        for polygon in polygons:
            polygon = orient(polygon, sign=1)
            for ring in [polygon.exterior, *polygon.interiors]:
                coordinates = list(ring.coords)
                for a, b in zip(coordinates, coordinates[1:], strict=False):
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    length = math.hypot(dx, dy)
                    if length <= 1e-12:
                        continue
                    edges.append(LineString([a, b]))
                    normals.append((dy / length, -dx / length))  # outward into air
        tree = STRtree(edges)
        source_tree = STRtree([s.geometry for s in local])
        findings: dict[tuple[tuple[str, ...], tuple[str, ...]], DRCViolation] = {}
        for i, edge in enumerate(edges):
            for raw_j in tree.query(edge, predicate="dwithin", distance=minimum_mm):
                j = int(raw_j)
                if j <= i:
                    continue
                if sum(a * b for a, b in zip(normals[i], normals[j], strict=True)) > -0.64:
                    continue
                candidates = [nearest_points(edge, edges[j])]
                middle = edge.interpolate(0.5, normalized=True)
                candidates.append((middle, nearest_points(middle, edges[j])[1]))
                middle = edges[j].interpolate(0.5, normalized=True)
                candidates.append((nearest_points(edge, middle)[0], middle))
                for a, b in candidates:
                    distance = a.distance(b)
                    if distance <= TOLERANCE or distance >= minimum_mm - TOLERANCE:
                        continue
                    dx, dy = (b.x - a.x) / distance, (b.y - a.y) / distance
                    # Both boundaries must face the open gap, within 25 degrees.
                    if normals[i][0] * dx + normals[i][1] * dy < math.cos(math.radians(25)):
                        continue
                    if normals[j][0] * -dx + normals[j][1] * -dy < 0.5:
                        continue
                    connector = LineString([a, b])
                    # The open connector must contain no copper from a third
                    # primitive; union provenance alone is not electrical isolation.
                    if connector.intersection(union).length > 1e-8:
                        continue
                    owner_indices = [
                        [
                            int(k)
                            for k in source_tree.query(
                                point, predicate="dwithin", distance=TOLERANCE
                            )
                            if local[int(k)].geometry.boundary.distance(point) <= TOLERANCE
                        ]
                        for point in (a, b)
                    ]
                    # Local shoulders at a pad escape or joined track are not
                    # slits. Exclude only the neighbourhood of the actual join,
                    # never a whole connected component or an entire source pair.
                    if any(
                        left != right
                        and local[left].geometry.intersects(local[right].geometry)
                        and connector.distance(
                            local[left].geometry.intersection(local[right].geometry)
                        )
                        < minimum_mm
                        for left in owner_indices[0]
                        for right in owner_indices[1]
                    ):
                        continue
                    owners = []
                    for point in (a, b):
                        owners.append(
                            sorted(
                                (local[int(k)].identity, local[int(k)].net)
                                for k in source_tree.query(
                                    point, predicate="dwithin", distance=TOLERANCE
                                )
                                if local[int(k)].geometry.boundary.distance(point) <= TOLERANCE
                            )
                        )
                    ids = tuple(sorted({identity for side in owners for identity, _ in side}))
                    nets = tuple(sorted({net for side in owners for _, net in side}))
                    key = ids, nets
                    if key in findings and (findings[key].actual_value or 0) <= distance:
                        continue
                    findings[key] = DRCViolation(
                        "physical_copper_gap",
                        "error",
                        f"Physical copper gap {distance:.4f}mm < {minimum_mm:.4f}mm; "
                        f"closest boundaries ({a.x:.6f}, {a.y:.6f}) and ({b.x:.6f}, {b.y:.6f})",
                        location=((a.x + b.x) / 2, (a.y + b.y) / 2),
                        layer=layer,
                        actual_value=distance,
                        required_value=minimum_mm,
                        items=ids,
                        nets=nets,
                        closest_locations=((a.x, a.y), (b.x, b.y)),
                    )
        for key in sorted(findings):
            results.add(findings[key])
    return results
