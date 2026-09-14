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


def load_fixed_fills(pcb_path, names, grid, net_class_map) -> FixedFillObstacles:
    """Reuse the DRC's actual-fill topology and convert to sheet coordinates."""
    if not names:
        return FixedFillObstacles()
    from shapely.affinity import translate  # type: ignore[import-untyped]

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
    from shapely.ops import unary_union  # type: ignore[import-untyped]

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
