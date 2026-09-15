"""Object-specific factory spacing, separate from generic electrical clearance.

Native DRC remains authoritative for rendered text and graphics unsupported by
our geometry model. These checks never substitute a blanket same-net clearance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad


def _hole_geometry(pad: Pad, footprint: Footprint):
    """Actual round/slotted drill including its offset and absolute pad angle."""
    from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

    from .clearance import _transform_pad_position

    width = height = pad.drill
    offset_x = offset_y = 0.0
    node = pad._sexp_node.find("drill") if pad._sexp_node is not None else None
    if node is not None:
        if node.get_string(0) == "oval":
            width, height = node.get_float(1) or 0, node.get_float(2) or 0
        offset = node.find("offset")
        if offset is not None:
            offset_x, offset_y = offset.get_float(0) or 0, offset.get_float(1) or 0
    if width <= 0 or height <= 0:
        return None
    radius = min(width, height) / 2
    half_line = abs(width - height) / 2
    ends = (
        ((-half_line, 0), (half_line, 0)) if width >= height else ((0, -half_line), (0, half_line))
    )
    geom = (LineString(ends) if half_line else Point(0, 0)).buffer(radius, quad_segs=64)
    geom = rotate(translate(geom, offset_x, offset_y), -pad.rotation, origin=(0, 0))
    return translate(geom, *_transform_pad_position(pad, footprint))


def check_pth_hole_clearance(pcb: PCB, rules: DesignRules) -> DRCResults:
    """Different-net PTH-hole to track (outer) or all copper (inner)."""
    results = DRCResults()
    if rules.min_pth_hole_to_track_mm is None and rules.min_inner_pth_hole_to_copper_mm is None:
        return results
    require_shapely("PTH hole clearance")
    import shapely  # type: ignore[import-untyped]
    from shapely import STRtree
    from shapely.geometry import LineString

    from .clearance import (
        ClearanceRule,
        _collect_zone_fills,
        _element_to_shapely_geom,
        _pad_on_layer,
        _transform_pad_position,
    )

    holes = [
        (fp, pad, _hole_geometry(pad, fp))
        for fp in pcb.footprints
        for pad in fp.pads
        if pad.type == "thru_hole"
    ]
    fills = (
        _collect_zone_fills(pcb, include_unassigned=True)
        if rules.min_inner_pth_hole_to_copper_mm is not None
        else {}
    )
    for layer in pcb.copper_layers:
        inner = layer.name not in ("F.Cu", "B.Cu")
        inner_minimum = rules.min_inner_pth_hole_to_copper_mm if inner else None
        minimum = max(rules.min_pth_hole_to_track_mm or 0, inner_minimum or 0)
        if not minimum:
            continue
        copper = []
        for elem in ClearanceRule()._collect_elements(pcb, layer.name):
            if elem.element_type == "segment":
                x1, y1, x2, y2, width = elem.geometry
                geom = LineString(((x1, y1), (x2, y2))).buffer(width / 2, quad_segs=64)
            elif inner_minimum is not None:
                geom = _element_to_shapely_geom(elem)
            else:
                continue
            if geom is not None:
                copper.append((elem.net_number, geom, elem.reference, elem.source_pad))
        if inner_minimum is not None:
            copper.extend(
                (fill.net_number, fill.polygon, f"Zone [{fill.net_name}]", None)
                for fill in fills.get(layer.name, [])
            )
        if not copper:
            continue
        tree = STRtree([geom for _, geom, _, _ in copper])
        for fp, pad, hole in holes:
            if hole is None or not _pad_on_layer(pad, layer.name):
                continue
            minx, miny, maxx, maxy = hole.bounds
            query_box = shapely.box(minx - minimum, miny - minimum, maxx + minimum, maxy + minimum)
            for idx in tree.query(query_box):
                net, geom, reference, source_pad = copper[int(idx)]
                # A hole is part of its own pad even without an assigned net.
                # Other net-0 objects have no proven electrical relationship.
                if source_pad is pad or (net != 0 and net == pad.net_number):
                    continue
                distance = hole.distance(geom)
                if distance + DRC_TOLERANCE < minimum:
                    results.add(
                        DRCViolation(
                            rule_id="pth_hole_clearance",
                            severity="error",
                            message=f"PTH hole to copper clearance {distance:.4f}mm < {minimum}mm",
                            location=_transform_pad_position(pad, fp),
                            layer=layer.name,
                            actual_value=distance,
                            required_value=minimum,
                            items=(f"{fp.reference}-{pad.number} hole", reference),
                        )
                    )
    results.rules_checked = 1
    return results


def check_silk_pad_clearance(pcb: PCB, rules: DesignRules) -> DRCResults:
    """Exact modeled silk strokes versus pad copper/apertures on the same side.

    Text bounding boxes cannot establish a manufacturing error: native DRC
    checks the rendered glyphs. Existing text advisory checks remain available.
    """
    results = DRCResults()
    minimum = rules.min_silk_to_pad_clearance_mm
    if minimum is None:
        return results
    require_shapely("silk pad clearance")
    import shapely
    from shapely import STRtree

    from .clearance import _pad_polygon
    from .silkscreen import _fp_transform, _silk_side, _stroke_geometry

    strokes: list[tuple[Any, Any, str, tuple[float, float]]] = []
    for fp in pcb.footprints:
        for graphic in fp.graphics:
            strokes.append((graphic, _fp_transform(fp), fp.reference, fp.position))
    strokes.extend((graphic, None, "board", graphic.start) for graphic in pcb.graphics)
    apertures = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.type not in ("smd", "thru_hole", "connect"):
                continue
            margin = pad.solder_mask_margin
            if margin is None:
                margin = pcb.setup.pad_to_mask_clearance if pcb.setup is not None else 0
            copper = _pad_polygon(pad, fp)
            for side in ("F", "B"):
                if "*.Cu" not in pad.layers and f"{side}.Cu" not in pad.layers:
                    continue
                exposed = "*.Mask" in pad.layers or f"{side}.Mask" in pad.layers
                geom = copper.buffer(max(margin or 0, 0)) if exposed else copper
                apertures.append((side, geom, f"{fp.reference}-{pad.number}"))
    apertures_by_side: dict[str, list[tuple[Any, str]]] = {"F": [], "B": []}
    for pad_side, aperture, pad_reference in apertures:
        if not aperture.is_empty:
            apertures_by_side[pad_side].append((aperture, pad_reference))
    trees: dict[str, Any] = {
        side: STRtree([geom for geom, _ in entries])
        for side, entries in apertures_by_side.items()
        if entries
    }

    for graphic, transform, reference, location in strokes:
        silk_side = _silk_side(graphic.layer)
        if silk_side is None or silk_side not in trees:
            continue
        geom = _stroke_geometry(graphic, transform)
        if geom is None:
            continue
        side_entries = apertures_by_side[silk_side]
        minx, miny, maxx, maxy = geom.bounds
        query_box = shapely.box(minx - minimum, miny - minimum, maxx + minimum, maxy + minimum)
        for idx in trees[silk_side].query(query_box):
            aperture, pad_reference = side_entries[int(idx)]
            distance = geom.distance(aperture)
            if distance + DRC_TOLERANCE < minimum:
                results.add(
                    DRCViolation(
                        rule_id="silk_pad_clearance",
                        severity="error",
                        message=f"Silk to pad clearance {distance:.4f}mm < {minimum}mm",
                        location=location,
                        layer=graphic.layer,
                        actual_value=distance,
                        required_value=minimum,
                        items=(f"{reference} {graphic.graphic_type}", pad_reference),
                    )
                )
    results.rules_checked = 1
    return results
