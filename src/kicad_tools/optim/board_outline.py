"""Extract board outline from KiCad PCB sexp data.

Shared by :mod:`kicad_tools.optim.keepout` and
:mod:`kicad_tools.optim.placement` to avoid duplicating the Edge.Cuts
parsing logic.

Returns an :class:`~kicad_tools.optim.geometry.Polygon` suitable for the
physics-based placement optimizer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.optim.geometry import Polygon, Vector2D

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


def extract_board_outline(pcb: PCB) -> Polygon | None:
    """Extract board outline from Edge.Cuts layer.

    Handles ``gr_rect`` and ``gr_line`` elements.  For ``gr_line`` outlines,
    attempts to chain the lines into a closed polygon; falls back to bounding
    box if chaining fails.

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

    # Collect gr_line elements on Edge.Cuts
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
