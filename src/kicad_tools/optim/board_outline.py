"""Extract board outline from KiCad PCB sexp data.

Shared by :mod:`kicad_tools.optim.keepout` and
:mod:`kicad_tools.optim.placement` to avoid duplicating the Edge.Cuts
parsing logic.

Returns an :class:`~kicad_tools.optim.geometry.Polygon` suitable for the
physics-based placement optimizer.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.core.arcs import arc_sweep
from kicad_tools.optim.geometry import Polygon, Vector2D

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


def to_origin_relative(outline: Polygon | None, pcb: PCB) -> Polygon | None:
    """Translate a raw (absolute) Edge.Cuts outline into the origin-relative frame.

    The Edge.Cuts parsers in this module read raw sexp coordinates, which are
    absolute.  ``Footprint.position`` and :class:`BoardGeometry` are relative
    to ``pcb.board_origin`` (issue #7106), so every outline handed to the
    optimizer must have the origin subtracted.  Returns ``outline`` unchanged
    when it is ``None`` or the origin is ``(0, 0)``.
    """
    if outline is None:
        return None
    ox, oy = pcb.board_origin
    if not (ox or oy):
        return outline
    return outline.translate(Vector2D(-ox, -oy))


def extract_board_outline(pcb: PCB) -> Polygon | None:
    """Extract board outline from Edge.Cuts layer.

    Handles ``gr_rect``, ``gr_poly``, ``gr_line`` and ``gr_arc`` elements
    (issue #7148).  ``gr_line``/``gr_arc`` outlines are chained into a closed
    polygon (arcs are sampled into short segments); falls back to bounding
    box if chaining fails.  Coordinates are absolute (raw sexp); use
    :func:`to_origin_relative` for the optimizer frame.

    Args:
        pcb: Parsed :class:`~kicad_tools.schema.pcb.PCB` instance.

    Returns:
        A :class:`Polygon` representing the board outline, or ``None``
        if no Edge.Cuts geometry is found.
    """
    sexp = pcb._sexp

    # Look for gr_rect on Edge.Cuts (simple rectangular boards)
    for child in sexp.iter_children():
        if child.tag == "gr_rect":
            layer = child.find("layer")
            if layer and layer.get_string(0) == "Edge.Cuts":
                start = child.find("start")
                end = child.find("end")
                if start and end:
                    x1 = start.get_float(0) or 0.0
                    y1 = start.get_float(1) or 0.0
                    x2 = end.get_float(0) or 0.0
                    y2 = end.get_float(1) or 0.0
                    return Polygon(
                        vertices=[
                            Vector2D(x1, y1),
                            Vector2D(x2, y1),
                            Vector2D(x2, y2),
                            Vector2D(x1, y2),
                        ]
                    )

    # gr_poly on Edge.Cuts (explicit vertex list)
    for child in sexp.iter_children():
        if child.tag == "gr_poly":
            layer = child.find("layer")
            if layer and layer.get_string(0) == "Edge.Cuts":
                pts = child.find("pts")
                verts: list[Vector2D] = []
                if pts:
                    for xy in pts.find_all("xy"):
                        x, y = xy.get_float(0), xy.get_float(1)
                        if x is not None and y is not None:
                            verts.append(Vector2D(x, y))
                if len(verts) >= 3:
                    return Polygon(vertices=verts)

    # Collect gr_line / gr_arc elements on Edge.Cuts
    edge_lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for child in sexp.iter_children():
        if child.tag == "gr_line":
            layer = child.find("layer")
            if layer and layer.get_string(0) == "Edge.Cuts":
                start = child.find("start")
                end = child.find("end")
                if start and end:
                    x1 = start.get_float(0) or 0.0
                    y1 = start.get_float(1) or 0.0
                    x2 = end.get_float(0) or 0.0
                    y2 = end.get_float(1) or 0.0
                    edge_lines.append(((x1, y1), (x2, y2)))

    for child in sexp.iter_children():
        if child.tag == "gr_arc":
            layer = child.find("layer")
            if layer and layer.get_string(0) == "Edge.Cuts":
                a_start, a_mid, a_end = child.find("start"), child.find("mid"), child.find("end")
                if a_start and a_mid and a_end:
                    edge_lines.extend(
                        _arc_segments(
                            (a_start.get_float(0) or 0.0, a_start.get_float(1) or 0.0),
                            (a_mid.get_float(0) or 0.0, a_mid.get_float(1) or 0.0),
                            (a_end.get_float(0) or 0.0, a_end.get_float(1) or 0.0),
                        )
                    )

    if len(edge_lines) >= 3:
        # Try to chain the lines into a closed polygon
        vertices = _chain_lines_to_polygon(edge_lines)
        if vertices:
            return Polygon(vertices=[Vector2D(x, y) for x, y in vertices])

    if len(edge_lines) >= 4:
        # Fallback: return bounding box
        all_x = [p[0] for line in edge_lines for p in line]
        all_y = [p[1] for line in edge_lines for p in line]
        return Polygon(
            vertices=[
                Vector2D(min(all_x), min(all_y)),
                Vector2D(max(all_x), min(all_y)),
                Vector2D(max(all_x), max(all_y)),
                Vector2D(min(all_x), max(all_y)),
            ]
        )

    return None


def _arc_segments(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
    max_step_deg: float = 2.0,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Sample a start/mid/end arc into short chord segments.

    The first and last points are exactly *start* and *end* so the result
    chains with neighbouring ``gr_line`` segments.  A collinear arc degrades
    to a single straight segment.
    """
    sweep = arc_sweep(start, mid, end)
    if sweep is None:
        return [(start, end)]
    n = max(2, math.ceil(math.degrees(sweep.sweep) / max_step_deg))
    sign = 1.0 if sweep.counter_clockwise else -1.0
    pts = [start]
    for i in range(1, n):
        pts.append(sweep.point_at(sweep.start_angle + sign * sweep.sweep * i / n))
    pts.append(end)
    return list(zip(pts[:-1], pts[1:], strict=True))


def _chain_lines_to_polygon(
    edge_lines: list[tuple[tuple[float, float], tuple[float, float]]],
    tolerance: float = 0.01,
) -> list[tuple[float, float]] | None:
    """Chain line segments into an ordered list of polygon vertices.

    Starts from the first segment and greedily finds the next segment
    whose start matches the current endpoint (within *tolerance*).

    Returns:
        Ordered list of (x, y) vertices, or ``None`` if chaining fails.
    """
    if not edge_lines:
        return None

    remaining = list(edge_lines)
    start_pt, current_pt = remaining.pop(0)
    vertices = [start_pt, current_pt]

    while remaining:
        found = False
        for i, (p1, p2) in enumerate(remaining):
            if _pts_close(current_pt, p1, tolerance):
                current_pt = p2
                vertices.append(current_pt)
                remaining.pop(i)
                found = True
                break
            if _pts_close(current_pt, p2, tolerance):
                current_pt = p1
                vertices.append(current_pt)
                remaining.pop(i)
                found = True
                break
        if not found:
            break

    # Remove duplicate closing vertex if polygon is closed
    if len(vertices) >= 3 and _pts_close(vertices[0], vertices[-1], tolerance):
        vertices.pop()

    return vertices if len(vertices) >= 3 else None


def _pts_close(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    """Check if two points are within *tol* of each other."""
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol
