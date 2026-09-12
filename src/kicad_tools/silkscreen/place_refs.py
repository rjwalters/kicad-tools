"""Silkscreen reference-designator placement solver (issue #5030).

``kct fix-silkscreen`` (baseline b8d5f595) repairs undersized silk stroke
widths and text heights, but has no way to *move* a reference designator
that collides with a pad, other silk, a neighboring component, or the
board edge -- so the only way to clear those DRC findings today is a
destructive one (hide, shrink, or delete the label). This module is the
missing placement solver: it moves (and, optionally, rotates) **visible**
reference text just far enough to clear real collisions while preserving
its visibility, text height, and stroke width exactly.

Design, in one pass:

* **Read geometry through the same helpers ``kct check``'s geometric
  silk DRC rules use** (:mod:`kicad_tools.validate.rules.silkscreen`,
  :mod:`kicad_tools.geometry.courtyard`). Text uses an oriented approximate
  envelope, not native glyph strokes; a clear plan is not a native DRC verdict.
* **Write back through the SAME shared S-expression tree** the schema
  parsed from (:attr:`~kicad_tools.schema.pcb.Footprint._sexp_node`),
  following the exact parse-mutate-save pattern already established by
  :mod:`kicad_tools.drc.repair_silkscreen` and
  :mod:`kicad_tools.silkscreen.generator`.  A dry run (:meth:`plan`
  alone, never calling :meth:`SilkRefPlacer.apply`) never touches the
  tree, so the source file is byte-for-byte unchanged.
* **Coordinate-only mutation**: only the reference text's own
  ``(at x y [angle])`` node is ever written.  Footprint position, pads,
  copper, and net bindings are never touched -- this cannot silently
  break routing or move a part.
* **Explicit failure reporting**: a reference that has no collision-free
  candidate within the search radius is left exactly where it was and
  reported as ``unplaceable`` (or ``under_component_fallback`` when its
  untouched position already sits on top of its own component's
  courtyard) -- never silently hidden, shrunk, or deleted to make a
  checker pass.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.sexp_file import save_pcb
from kicad_tools.geometry.courtyard import _courtyard_polygon, _fp_transform, _side_has_geometry
from kicad_tools.schema.pcb import PCB, _is_footprint_tag
from kicad_tools.sexp.parser import SExp, parse_file
from kicad_tools.validate.rules.silkscreen import (
    SILK_EDGE_CLEARANCE_MM,
    _iter_pad_apertures,
    _iter_via_apertures,
    _silk_side,
    _stroke_geometry,
    _text_bbox_geometry,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Footprint

# Default silk-to-obstacle clearance (mm).  This is deliberately a
# *dedicated* solver parameter, distinct from
# ``DesignRules.min_solder_mask_clearance_mm`` (used only to size pad mask
# apertures, matching ``validate.rules.silkscreen``) -- it is the
# "how far apart do labels need to be to stay legible" knob a fab-specific
# silkscreen policy tightens or loosens.  0.15mm matches the tightened
# value from the concrete Chorus v25 result this module targets.
DEFAULT_CLEARANCE_MM = 0.15

# Search geometry defaults.  A candidate ring step of 0.25mm across an 8mm
# radius (32 rings x 8 points = 256 candidates/reference, worst case) is
# comfortably fast for real boards while covering "just outside this
# 0402's courtyard" through "clear across a dense connector row".
DEFAULT_MAX_OFFSET_MM = 8.0
DEFAULT_STEP_MM = 0.25

# Floating-point tolerance, matching ``validate.rules.silkscreen``.
_EPSILON_MM = 1e-4


def _find_all_footprints(doc: SExp) -> list[SExp]:
    """Return every footprint node in *doc*, in document order.

    Mirrors ``repair_silkscreen._find_all_footprints`` / matches both the
    modern ``(footprint ...)`` spelling and the legacy pre-KiCad-6
    ``(module ...)`` spelling.
    """
    return [
        node for child in doc.children for node in child.iter_all() if _is_footprint_tag(node.name)
    ]


def _find_reference_node(footprint: Footprint) -> SExp | None:
    """Return the raw ``fp_text reference`` / ``property "Reference"`` node.

    Reads ``footprint._sexp_node`` -- the same back-reference
    :meth:`PCB._link_footprint_sexp_nodes` attaches for ``Pad``/``Footprint``
    write-through -- rather than re-parsing, so the returned node is the
    live node :meth:`SilkRefPlacer.save` will serialise.
    """
    node: SExp | None = footprint.__dict__.get("_sexp_node")
    if node is None:
        return None
    for child in node.children:
        if not child.is_atom and child.name == "fp_text" and child.children:
            atoms = child.get_atoms()
            if atoms and str(atoms[0]) == "reference" and len(atoms) >= 2:
                return child
    for child in node.children:
        if not child.is_atom and child.name == "property" and child.children:
            atoms = child.get_atoms()
            if atoms and str(atoms[0]) == "Reference" and len(atoms) >= 2:
                return child
    return None


def _read_text_angle(ref_node: SExp) -> float:
    """Return the third (angle) token of a text node's ``(at x y [angle])``."""
    at_node = ref_node.find("at")
    if at_node is None:
        return 0.0
    atoms = at_node.get_atoms()
    if len(atoms) >= 3:
        try:
            return float(atoms[2])
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _oriented_text_geometry(
    text: str,
    font_size: tuple[float, float],
    thickness: float,
    center: tuple[float, float],
    angle: float,
) -> Any:
    """Rotate the complete text envelope, retaining glyph dimensions.

    KiCad 10 parsePCB_TEXT stores an absolute board-frame angle (subtracting
    the footprint orientation before its position transform). Do not add the
    footprint angle again. KiCad positive angles turn toward negative board y.
    """
    from shapely.affinity import rotate  # type: ignore[import-untyped]

    geom = _text_bbox_geometry(text, font_size, thickness, center)
    return rotate(geom, -angle, origin=center) if geom is not None else None


def _set_text_at(ref_node: SExp, x: float, y: float, angle: float) -> None:
    """Write ``(at x y [angle])`` on *ref_node* in place.

    Coordinate-only: no other child of *ref_node* (layer, effects, hide,
    text content) is touched.
    """
    at_node = ref_node.find("at")
    if at_node is None:
        raise ValueError("Reference text node has no (at ...) child")
    at_node.set_atom(0, round(x, 6))
    at_node.set_atom(1, round(y, 6))
    atoms = at_node.get_atoms()
    if len(atoms) >= 3:
        at_node.set_atom(2, round(angle, 6))
    elif angle != 0.0:
        at_node.add(round(angle, 6))


def _inverse_fp_transform(footprint: Footprint):
    """Return a ``(X, Y) -> (x, y)`` board->local transform for a footprint.

    Exact inverse of ``geometry.courtyard._fp_transform`` /
    ``validate.rules.silkscreen._fp_transform``.
    """
    fp_x, fp_y = footprint.position
    theta = math.radians(-footprint.rotation)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    def inverse(point: tuple[float, float]) -> tuple[float, float]:
        dx = point[0] - fp_x
        dy = point[1] - fp_y
        return (dx * cos_t + dy * sin_t, -dx * sin_t + dy * cos_t)

    return inverse


def _iter_static_silk_obstacles(pcb: PCB):
    """Yield ``(side, geom, label)`` for every silk element that never moves.

    Sibling of ``validate.rules.silkscreen._iter_silk_geometries`` with
    reference-designator text excluded -- those are the movable elements
    this module places, tracked separately as "dynamic" obstacles by the
    caller so each already-decided reference still blocks the next one.
    """
    for footprint in pcb.footprints:
        transform = _fp_transform(footprint)

        for fp_text in footprint.texts:
            if fp_text.text_type == "reference":
                continue
            side = _silk_side(fp_text.layer)
            if side is None or fp_text.hidden:
                continue
            center = transform(fp_text.position)
            geom = _oriented_text_geometry(
                fp_text.text, fp_text.font_size, fp_text.font_thickness, center, fp_text.rotation
            )
            if geom is None:
                continue
            yield side, geom, f"{footprint.reference} ({fp_text.text_type})"

        for graphic in footprint.graphics:
            side = _silk_side(graphic.layer)
            if side is None:
                continue
            geom = _stroke_geometry(graphic, transform)
            if geom is None:
                continue
            yield side, geom, f"{footprint.reference} (fp_{graphic.graphic_type})"

    for text in pcb.texts:
        side = _silk_side(text.layer)
        if side is None or text.hidden:
            continue
        geom = _oriented_text_geometry(
            text.text, text.font_size, text.font_thickness, text.position, text.rotation
        )
        if geom is None:
            continue
        yield side, geom, (text.text[:20] if text.text else "gr_text")

    for board_graphic in pcb.graphics:
        side = _silk_side(board_graphic.layer)
        if side is None:
            continue
        geom = _stroke_geometry(board_graphic, None)
        if geom is None:
            continue
        yield side, geom, f"gr_{board_graphic.graphic_type}"


def _component_bbox(
    footprint: Footprint, side: str, Polygon: Any
) -> tuple[float, float, float, float]:
    """Return ``(min_x, min_y, max_x, max_y)`` for a footprint's body on *side*.

    Prefers the real courtyard polygon (accurate body outline); falls back
    to the bounding box of the footprint's pads, then to a small box
    around the footprint's anchor point when neither is available (rare --
    e.g. a footprint with no courtyard and no pads).
    """
    courtyard = _courtyard_polygon(footprint, side, Polygon)
    if courtyard is not None:
        min_x, min_y, max_x, max_y = courtyard.bounds
        return (min_x, min_y, max_x, max_y)

    transform = _fp_transform(footprint)
    xs: list[float] = []
    ys: list[float] = []
    for pad in footprint.pads:
        cx, cy = transform(pad.position)
        half_w, half_h = pad.size[0] / 2.0, pad.size[1] / 2.0
        xs.extend([cx - half_w, cx + half_w])
        ys.extend([cy - half_h, cy + half_h])
    if xs and ys:
        return (min(xs), min(ys), max(xs), max(ys))

    fx, fy = footprint.position
    return (fx - 1.0, fy - 1.0, fx + 1.0, fy + 1.0)


def _candidate_points(
    bbox: tuple[float, float, float, float],
    text_w: float,
    text_h: float,
    max_offset_mm: float,
    step_mm: float,
) -> list[tuple[float, float]]:
    """Return candidate centers on rings of increasing distance from *bbox*.

    Each ring contributes the midpoint of each of the 4 edges plus the 4
    corners of *bbox* expanded by the ring's offset, so the label is
    tested progressively farther from the component body without ever
    losing the "near its own component" property the caller sorts on.
    """
    min_x, min_y, max_x, max_y = bbox
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    half_w, half_h = text_w / 2.0, text_h / 2.0

    points: list[tuple[float, float]] = []
    for ring in range(math.floor(max_offset_mm / step_mm) + 1):
        offset = ring * step_mm
        top_y = min_y - offset - half_h
        bottom_y = max_y + offset + half_h
        left_x = min_x - offset - half_w
        right_x = max_x + offset + half_w
        points.extend(
            [
                (cx, top_y),
                (cx, bottom_y),
                (left_x, cy),
                (right_x, cy),
                (left_x, top_y),
                (right_x, top_y),
                (left_x, bottom_y),
                (right_x, bottom_y),
            ]
        )
    return points


def _board_material(pcb: PCB, Polygon: Any) -> tuple[list, Any | None, str]:
    """Resolve closed polygon contours without guessing missing board material.

    Reuse the schema's segment stitching and 0.01mm endpoint tolerance.
    Nested contours alternate material/cutout (including islands). Curved
    outlines are explicitly unsupported here: the schema's two-chord arc
    approximation is not sufficiently accurate to prove containment.
    """
    segments = pcb.get_board_outline_segments()
    ox, oy = pcb._board_origin
    for node in pcb._sexp.children:
        if node.is_atom:
            continue
        if _is_footprint_tag(node.name):
            for element in node.iter_all():
                element_layer = element.find("layer")
                if element_layer is not None and element_layer.get_string(0) == "Edge.Cuts":
                    return segments, None, "board outline unavailable: footprint-local Edge.Cuts"
        layer = node.find("layer")
        if layer is None or layer.get_string(0) != "Edge.Cuts":
            continue
        if node.name not in {"gr_line", "gr_rect", "gr_poly"}:
            return segments, None, f"board outline unavailable: unsupported {node.name}"
        if node.name in {"gr_line", "gr_rect"}:
            for endpoint in ("start", "end"):
                point = node.find(endpoint)
                if point is None or len(point.get_atoms()) != 2:
                    return segments, None, "board outline unavailable: malformed endpoint"
                for coordinate in (point.get_float(0), point.get_float(1)):
                    if coordinate is None or not math.isfinite(coordinate):
                        return segments, None, "board outline unavailable: malformed endpoint"
        if node.name == "gr_poly":
            pts = node.find("pts")
            vertices = []
            if pts is not None:
                for xy in pts.children:
                    if xy.is_atom or xy.name != "xy":
                        return segments, None, "board outline unavailable: malformed polygon"
                    x, y = xy.get_float(0), xy.get_float(1)
                    if x is None or y is None or not math.isfinite(x) or not math.isfinite(y):
                        return segments, None, "board outline unavailable: malformed polygon"
                    vertices.append((x - ox, y - oy))
            if len(vertices) > 1 and vertices[-1] == vertices[0]:
                vertices.pop()
            if len(vertices) < 3:
                return segments, None, "board outline unavailable: malformed polygon"
            segments.extend(zip(vertices, vertices[1:] + vertices[:1], strict=True))
    if not segments:
        return segments, None, "board outline unavailable: missing Edge.Cuts"
    if any(not math.isfinite(v) for segment in segments for point in segment for v in point):
        return segments, None, "board outline unavailable: non-finite coordinates"
    contours: list[Any] = []
    for indices in PCB._group_segments_by_connectivity(segments, 0.01):
        ring = PCB._chain_segment_indices(segments, indices, 0.01)
        if not ring:
            return segments, None, "board outline unavailable: open or branched contour"
        polygon = Polygon(ring)
        if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
            return segments, None, "board outline unavailable: invalid contour"
        for other in contours:
            # Separate islands and strictly nested cutouts are valid; touching
            # or crossing contours do not define an unambiguous material side.
            if polygon.boundary.intersects(other.boundary):
                return segments, None, "board outline unavailable: intersecting contours"
        contours.append(polygon)
    material = Polygon()
    for polygon in contours:
        material = material.symmetric_difference(polygon)
    return segments, material, ""


def _candidate_clear(
    geom: Any,
    *,
    apertures: list[tuple[Any, str]],
    silk_obstacles: list[tuple[Any, str]],
    courtyards: list[tuple[Any, str]],
    outline: Any | None,
    board_material: Any | None,
    outline_error: str,
    clearance_mm: float,
    edge_clearance_mm: float,
) -> tuple[bool, str]:
    """Return ``(True, "")`` if *geom* clears every obstacle, else ``(False, why)``.

    Pad apertures and other silk are checked against *geom* expanded by
    ``clearance_mm`` (a real spacing requirement, not bare non-overlap).
    Courtyards are a hard keep-out -- landing on top of ANY component body
    (its own or a neighbor's) is never acceptable regardless of clearance.
    Board edge uses true distance, matching
    ``validate.rules.silkscreen.check_silk_edge_clearance``.
    """
    if outline_error:
        return False, outline_error
    if board_material is None or not board_material.covers(geom):
        return False, "outside board material or inside a cutout"
    expanded = geom.buffer(clearance_mm) if clearance_mm > 0 else geom
    for ap_geom, label in apertures:
        if expanded.intersects(ap_geom):
            return False, f"pad aperture {label}"
    for silk_geom, label in silk_obstacles:
        if expanded.intersects(silk_geom):
            return False, f"silkscreen {label}"
    for body_geom, label in courtyards:
        if geom.intersects(body_geom):
            return False, f"component body {label}"
    if outline is not None:
        distance = geom.distance(outline)
        if distance < edge_clearance_mm - _EPSILON_MM:
            return False, "board edge"
    return True, ""


@dataclass
class RefPlacement:
    """The plan (or outcome) for a single reference designator."""

    footprint_ref: str
    layer: str
    old_position: tuple[float, float]  # board-relative mm
    new_position: tuple[float, float]  # board-relative mm
    old_rotation: float
    new_rotation: float
    #: "unchanged" (already clear, not moved) | "moved" | "unplaceable" |
    #: "under_component_fallback" (best-effort position still sits on its
    #: own component's courtyard -- reported, never silently accepted).
    status: str
    reason: str = ""
    #: Footprint-local coordinates to write into the ``(at ...)`` node.
    #: Populated only for ``status == "moved"``.
    new_local_position: tuple[float, float] | None = None

    @property
    def moved(self) -> bool:
        return self.status == "moved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "footprint_ref": self.footprint_ref,
            "layer": self.layer,
            "old_position_mm": list(self.old_position),
            "new_position_mm": list(self.new_position),
            "old_rotation_deg": self.old_rotation,
            "new_rotation_deg": self.new_rotation,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class PlaceSilkRefsResult:
    """Aggregate result of a reference-placement plan."""

    clearance_mm: float
    placements: list[RefPlacement] = field(default_factory=list)

    @property
    def moved(self) -> list[RefPlacement]:
        return [p for p in self.placements if p.status == "moved"]

    @property
    def unchanged(self) -> list[RefPlacement]:
        return [p for p in self.placements if p.status == "unchanged"]

    @property
    def unplaceable(self) -> list[RefPlacement]:
        return [p for p in self.placements if p.status == "unplaceable"]

    @property
    def under_component_fallback(self) -> list[RefPlacement]:
        return [p for p in self.placements if p.status == "under_component_fallback"]

    @property
    def total_moved(self) -> int:
        return len(self.moved)

    @property
    def total_unplaceable(self) -> int:
        return len(self.unplaceable) + len(self.under_component_fallback)


def _svg_escape(text: str) -> str:
    """Minimal XML-escaping for SVG text content and attribute values."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _render_svg(
    pcb: PCB,
    result: PlaceSilkRefsResult,
    context: dict[str, Any],
    output_path: Path,
) -> Path:
    """Build and write the SVG review artifact described by :meth:`SilkRefPlacer.render_svg`."""

    margin_mm = 3.0
    xs: list[float] = []
    ys: list[float] = []

    def _bounds_of(x0: float, y0: float, x1: float, y1: float) -> None:
        xs.extend([x0, x1])
        ys.extend([y0, y1])

    outline_segments = context["outline_segments"]
    for start, end in outline_segments:
        _bounds_of(start[0], start[1], end[0], end[1])

    for side_geoms in context["courtyards"].values():
        for geom, _label in side_geoms:
            b = geom.bounds
            _bounds_of(*b)
    for side_geoms in context["apertures"].values():
        for geom, _label in side_geoms:
            b = geom.bounds
            _bounds_of(*b)
    for placement in result.placements:
        for x, y in (placement.old_position, placement.new_position):
            _bounds_of(x - 2.0, y - 2.0, x + 2.0, y + 2.0)

    if not xs or not ys:
        xs, ys = [0.0, 10.0], [0.0, 10.0]

    min_x, max_x = min(xs) - margin_mm, max(xs) + margin_mm
    min_y, max_y = min(ys) - margin_mm, max(ys) + margin_mm
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    # SVG y-axis grows downward; board Y in KiCad also grows downward, so no
    # flip is needed -- this matches how KiCad itself renders the board.
    scale = 10.0  # px per mm, purely cosmetic

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{min_x * scale:.2f} {min_y * scale:.2f} '
        f'{width * scale:.2f} {height * scale:.2f}" '
        f'width="{width * scale:.0f}" height="{height * scale:.0f}">',
        f'<rect x="{min_x * scale:.2f}" y="{min_y * scale:.2f}" '
        f'width="{width * scale:.2f}" height="{height * scale:.2f}" fill="white"/>',
        "<!-- kct place-silk-refs review artifact (issue #5030) -->",
        "<!-- legend: green=unchanged, blue(+dashed old)=moved, "
        "red=unplaceable/under_component_fallback -->",
    ]

    def _poly_points(geom: Any) -> str:
        coords = list(geom.exterior.coords) if hasattr(geom, "exterior") else list(geom.coords)
        return " ".join(f"{x * scale:.2f},{y * scale:.2f}" for x, y in coords)

    # Board outline.
    for start, end in outline_segments:
        parts.append(
            f'<line x1="{start[0] * scale:.2f}" y1="{start[1] * scale:.2f}" '
            f'x2="{end[0] * scale:.2f}" y2="{end[1] * scale:.2f}" '
            f'stroke="black" stroke-width="1"/>'
        )

    # Courtyards (component bodies -- hard keep-out context).
    for side_geoms in context["courtyards"].values():
        for geom, label in side_geoms:
            try:
                pts = _poly_points(geom)
            except Exception:
                continue
            parts.append(
                f'<polygon points="{pts}" fill="none" stroke="#999999" '
                f'stroke-width="0.5" stroke-dasharray="2,2">'
                f"<title>{_svg_escape(label)} courtyard</title></polygon>"
            )

    # Pad / via apertures.
    for side_geoms in context["apertures"].values():
        for geom, label in side_geoms:
            b = geom.bounds
            parts.append(
                f'<rect x="{b[0] * scale:.2f}" y="{b[1] * scale:.2f}" '
                f'width="{(b[2] - b[0]) * scale:.2f}" height="{(b[3] - b[1]) * scale:.2f}" '
                f'fill="#ffcc66" fill-opacity="0.5">'
                f"<title>{_svg_escape(label)}</title></rect>"
            )

    # Static obstacles use the same oriented polygons as collision detection.
    for side_geoms in context["static_silk"].values():
        for geom, label in side_geoms:
            for polygon in getattr(geom, "geoms", [geom]):
                if not hasattr(polygon, "exterior"):
                    continue
                points = " ".join(
                    f"{x * scale:.2f},{y * scale:.2f}" for x, y in polygon.exterior.coords
                )
                parts.append(
                    f'<polygon points="{points}" fill="#666666" fill-opacity="0.4">'
                    f"<title>{_svg_escape(label)}</title></polygon>"
                )

    # Reference placements: the actual review content.
    ref_texts_by_ref = {
        fp.reference: next((t for t in fp.texts if t.text_type == "reference"), None)
        for fp in pcb.footprints
    }
    for placement in sorted(result.placements, key=lambda p: p.footprint_ref):
        ref_text = ref_texts_by_ref.get(placement.footprint_ref)
        font_size = ref_text.font_size if ref_text is not None else (1.0, 1.0)
        font_thickness = ref_text.font_thickness if ref_text is not None else 0.15
        text = ref_text.text if ref_text is not None else placement.footprint_ref

        if placement.status == "moved":
            old_geom = _oriented_text_geometry(
                text, font_size, font_thickness, placement.old_position, placement.old_rotation
            )
            if old_geom is not None:
                points = " ".join(
                    f"{x * scale:.2f},{y * scale:.2f}" for x, y in old_geom.exterior.coords
                )
                parts.append(
                    f'<polygon points="{points}" '
                    f'fill="none" stroke="#999999" stroke-width="0.5" stroke-dasharray="1,1"/>'
                )
            ox, oy = placement.old_position
            nx, ny = placement.new_position
            parts.append(
                f'<line x1="{ox * scale:.2f}" y1="{oy * scale:.2f}" '
                f'x2="{nx * scale:.2f}" y2="{ny * scale:.2f}" '
                f'stroke="#3366cc" stroke-width="0.3" stroke-dasharray="1,1"/>'
            )
            color = "#3366cc"
        elif placement.status == "unchanged":
            color = "#2e8b57"
        else:  # unplaceable / under_component_fallback
            color = "#cc3333"

        geom = _oriented_text_geometry(
            text, font_size, font_thickness, placement.new_position, placement.new_rotation
        )
        if geom is not None:
            points = " ".join(f"{x * scale:.2f},{y * scale:.2f}" for x, y in geom.exterior.coords)
            parts.append(
                f'<polygon points="{points}" '
                f'fill="{color}" fill-opacity="0.35" stroke="{color}" stroke-width="0.4">'
                f"<title>{_svg_escape(placement.footprint_ref)}: {_svg_escape(placement.status)}"
                f"{' -- ' + _svg_escape(placement.reason) if placement.reason else ''}"
                f"</title></polygon>"
            )
        nx, ny = placement.new_position
        parts.append(
            f'<text x="{nx * scale:.2f}" y="{ny * scale:.2f}" font-size="{max(font_size[1] * scale * 0.6, 4):.1f}" '
            f'transform="rotate({-placement.new_rotation:g} {nx * scale:.2f} {ny * scale:.2f})" '
            f'fill="{color}" text-anchor="middle" dominant-baseline="middle">'
            f"{_svg_escape(placement.footprint_ref)}</text>"
        )

    parts.append("</svg>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")
    return output_path


class SilkRefPlacer:
    """Plan and apply readable, collision-free reference-designator placement.

    Usage::

        placer = SilkRefPlacer(Path("board.kicad_pcb"))
        result = placer.plan(clearance_mm=0.15)
        # ... inspect result.placements ...
        placer.apply(result)   # mutates the in-memory tree only
        placer.save()          # write to disk
    """

    def __init__(self, pcb_path: str | Path) -> None:
        self.path = Path(pcb_path)
        self.doc: SExp = parse_file(self.path)
        self.pcb: PCB = PCB(self.doc, self.path)
        # Populated by plan(): footprint reference -> live (fp_text
        # reference)/(property "Reference") node, for apply().
        self._ref_nodes: dict[str, SExp] = {}
        # Populated by plan(): a snapshot of the obstacle geometry used to
        # compute the plan, reused (read-only) by render_svg() so the review
        # artifact draws exactly what the solver saw -- never a re-derived
        # (and potentially inconsistent) second pass over the tree.
        self._render_context: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        *,
        clearance_mm: float = DEFAULT_CLEARANCE_MM,
        edge_clearance_mm: float = SILK_EDGE_CLEARANCE_MM,
        mask_clearance_mm: float = 0.05,
        max_offset_mm: float = DEFAULT_MAX_OFFSET_MM,
        step_mm: float = DEFAULT_STEP_MM,
        allow_rotate: bool = False,
    ) -> PlaceSilkRefsResult:
        """Compute a per-reference move plan without mutating the tree.

        Args:
            clearance_mm: Required spacing between a reference's text box
                and any pad aperture or other silk element.
            edge_clearance_mm: Required spacing to the board outline.
            mask_clearance_mm: Solder-mask margin used to size pad/via
                apertures (matches ``DesignRules.min_solder_mask_clearance_mm``).
            max_offset_mm: How far from the component body to search.
            step_mm: Search ring spacing.
            allow_rotate: If True, also try each candidate with the text
                rotated 90 degrees about its anchor when the
                original orientation does not fit.

        Returns:
            A :class:`PlaceSilkRefsResult` with one :class:`RefPlacement`
            per visible reference designator.  Nothing is written to the
            S-expression tree -- call :meth:`apply` to do that.
        """
        values = {
            "clearance_mm": clearance_mm,
            "edge_clearance_mm": edge_clearance_mm,
            "mask_clearance_mm": mask_clearance_mm,
            "max_offset_mm": max_offset_mm,
        }
        for name, value in values.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not math.isfinite(step_mm) or step_mm <= 0:
            raise ValueError("step_mm must be finite and positive")
        if max_offset_mm / step_mm >= 4096:
            raise ValueError("search exceeds 4096 rings; increase step_mm or reduce max_offset_mm")

        require_shapely("silk reference placement")
        from shapely.geometry import MultiLineString, Polygon  # type: ignore[import-untyped]

        pcb = self.pcb
        # Reference strings identify both move targets and own-component
        # obstacle exclusions. Hidden references also participate in those
        # exclusions, so require uniqueness across the whole footprint set.
        seen_refs: set[str] = set()
        for footprint in pcb.footprints:
            if footprint.reference in seen_refs:
                raise ValueError(
                    f"Duplicate footprint reference {footprint.reference!r}; "
                    "assign unique references before placing silkscreen"
                )
            seen_refs.add(footprint.reference)
        result = PlaceSilkRefsResult(clearance_mm=clearance_mm)
        self._ref_nodes = {}

        # --- Static obstacles (never move) ---
        apertures: dict[str, list[tuple[Any, str]]] = {"F": [], "B": []}
        for aperture_side, geom, label in _iter_pad_apertures(pcb, mask_clearance_mm):
            apertures[aperture_side].append((geom, label))
        for aperture_side, geom, label in _iter_via_apertures(pcb, mask_clearance_mm):
            apertures[aperture_side].append((geom, label))

        static_silk: dict[str, list[tuple[Any, str]]] = {"F": [], "B": []}
        for static_side, geom, label in _iter_static_silk_obstacles(pcb):
            static_silk[static_side].append((geom, label))

        courtyards: dict[str, list[tuple[Any, str]]] = {"F": [], "B": []}
        own_courtyard: dict[tuple[int, str], Any] = {}
        for footprint in pcb.footprints:
            for crtyd_side in ("F", "B"):
                if not _side_has_geometry(footprint, crtyd_side):
                    continue
                poly = _courtyard_polygon(footprint, crtyd_side, Polygon)
                if poly is not None:
                    courtyards[crtyd_side].append((poly, footprint.reference))
                    own_courtyard[(id(footprint), crtyd_side)] = poly

        outline_segments, board_material, outline_error = _board_material(pcb, Polygon)
        outline = (
            MultiLineString([[start, end] for start, end in outline_segments])
            if outline_segments and not outline_error
            else None
        )

        self._render_context = {
            "apertures": apertures,
            "static_silk": static_silk,
            "courtyards": courtyards,
            "outline_segments": outline_segments,
        }

        # --- Movable elements: every visible reference ---
        entries = []
        dynamic: dict[str, list[tuple[Any, str]]] = {"F": [], "B": []}
        for footprint in pcb.footprints:
            ref_text = next((t for t in footprint.texts if t.text_type == "reference"), None)
            if ref_text is None or ref_text.hidden:
                continue
            side = _silk_side(ref_text.layer)
            if side is None:
                continue
            ref_node = _find_reference_node(footprint)
            if ref_node is None:
                continue

            transform = _fp_transform(footprint)
            original_center = transform(ref_text.position)
            geom0 = _oriented_text_geometry(
                ref_text.text,
                ref_text.font_size,
                ref_text.font_thickness,
                original_center,
                _read_text_angle(ref_node),
            )
            if geom0 is None:
                continue

            self._ref_nodes[footprint.reference] = ref_node
            entries.append(
                {
                    "footprint": footprint,
                    "ref_text": ref_text,
                    "side": side,
                    "inverse": _inverse_fp_transform(footprint),
                    "original_center": original_center,
                    "old_rotation": _read_text_angle(ref_node),
                    "geom0": geom0,
                }
            )
            dynamic[side].append((geom0, footprint.reference))

        # Deterministic processing order.
        entries.sort(key=lambda e: e["footprint"].reference)

        for entry in entries:
            footprint = entry["footprint"]
            ref = footprint.reference
            side = entry["side"]
            ref_text = entry["ref_text"]
            geom0 = entry["geom0"]
            original_center = entry["original_center"]
            old_rotation = entry["old_rotation"]

            pool = dynamic[side]
            with contextlib.suppress(ValueError):
                pool.remove((geom0, ref))

            combined_apertures = apertures.get(side, [])
            combined_silk = static_silk[side] + pool
            combined_courtyards = courtyards.get(side, [])

            def clears(geom: Any) -> tuple[bool, str]:
                return _candidate_clear(
                    geom,
                    apertures=combined_apertures,
                    silk_obstacles=combined_silk,
                    courtyards=combined_courtyards,
                    outline=outline,
                    board_material=board_material,
                    outline_error=outline_error,
                    clearance_mm=clearance_mm,
                    edge_clearance_mm=edge_clearance_mm,
                )

            bbox = _component_bbox(footprint, side, Polygon)
            candidates = _candidate_points(
                bbox, ref_text.font_size[0], ref_text.font_size[1], max_offset_mm, step_mm
            )
            candidates.sort(
                key=lambda p: math.hypot(p[0] - original_center[0], p[1] - original_center[1])
            )
            ordered = [original_center, *candidates]

            chosen_point: tuple[float, float] | None = None
            chosen_rotation = old_rotation
            chosen_geom = None
            last_reason = ""
            for point in ordered:
                geom = _oriented_text_geometry(
                    ref_text.text, ref_text.font_size, ref_text.font_thickness, point, old_rotation
                )
                ok, why = clears(geom)
                if ok:
                    chosen_point, chosen_geom = point, geom
                    break
                last_reason = why

                if allow_rotate:
                    geom_r = _oriented_text_geometry(
                        ref_text.text,
                        ref_text.font_size,
                        ref_text.font_thickness,
                        point,
                        old_rotation + 90.0,
                    )
                    ok_r, why_r = clears(geom_r)
                    if ok_r:
                        chosen_point, chosen_geom = point, geom_r
                        chosen_rotation = old_rotation + 90.0
                        break
                    last_reason = why_r

            if chosen_point is None:
                # No candidate cleared every constraint -- leave untouched
                # and report explicitly (no destructive fallback).
                dynamic[side].append((geom0, ref))
                own_poly = own_courtyard.get((id(footprint), side))
                fallback = own_poly is not None and geom0.intersects(own_poly)
                status = "under_component_fallback" if fallback else "unplaceable"
                result.placements.append(
                    RefPlacement(
                        footprint_ref=ref,
                        layer=ref_text.layer,
                        old_position=original_center,
                        new_position=original_center,
                        old_rotation=old_rotation,
                        new_rotation=old_rotation,
                        status=status,
                        reason=last_reason or "no collision-free candidate within search radius",
                    )
                )
                continue

            dynamic[side].append((chosen_geom, ref))

            moved = (
                abs(chosen_point[0] - original_center[0]) > _EPSILON_MM
                or abs(chosen_point[1] - original_center[1]) > _EPSILON_MM
                or chosen_rotation != old_rotation
            )
            if moved:
                local = entry["inverse"](chosen_point)
                result.placements.append(
                    RefPlacement(
                        footprint_ref=ref,
                        layer=ref_text.layer,
                        old_position=original_center,
                        new_position=chosen_point,
                        old_rotation=old_rotation,
                        new_rotation=chosen_rotation,
                        status="moved",
                        new_local_position=local,
                    )
                )
            else:
                result.placements.append(
                    RefPlacement(
                        footprint_ref=ref,
                        layer=ref_text.layer,
                        old_position=original_center,
                        new_position=original_center,
                        old_rotation=old_rotation,
                        new_rotation=old_rotation,
                        status="unchanged",
                    )
                )

        return result

    def apply(self, result: PlaceSilkRefsResult) -> int:
        """Write every ``moved`` placement's new ``(x, y, [angle])``.

        Only the reference text's own ``at`` node is mutated -- footprint
        position, pads, copper, and net bindings are never touched.

        Returns:
            The number of references actually mutated.
        """
        applied = 0
        for placement in result.moved:
            ref_node = self._ref_nodes.get(placement.footprint_ref)
            if ref_node is None or placement.new_local_position is None:
                continue
            lx, ly = placement.new_local_position
            _set_text_at(ref_node, lx, ly, placement.new_rotation)
            applied += 1
        return applied

    def save(self, output_path: str | Path | None = None) -> None:
        """Write the (possibly modified) SExp tree to disk."""
        save_pcb(self.doc, Path(output_path) if output_path else self.path)

    def render_svg(self, result: PlaceSilkRefsResult, output_path: str | Path) -> Path:
        """Render a visual review artifact for *result* to an SVG file.

        This is the "actual rendered review artifact" the acceptance
        criteria call for -- DRC passing does not, by itself, prove the
        placement is *readable*.  The rendering shows, per reference:

        * green solid box -- already clear, never moved (``unchanged``);
        * blue solid box (with a dashed gray box at the old position and a
          connecting line) -- moved to clear a collision (``moved``);
        * red solid box -- could not be placed cleanly and was left exactly
          where it was (``unplaceable`` / ``under_component_fallback``).

        Board outline, pad apertures, and component courtyards are drawn as
        context so a reviewer can see *why* each reference landed where it
        did.  Requires :meth:`plan` to have been called first (uses the
        obstacle snapshot it records).
        """
        if self._render_context is None:
            raise RuntimeError("render_svg() requires plan() to be called first")
        return _render_svg(self.pcb, result, self._render_context, Path(output_path))
