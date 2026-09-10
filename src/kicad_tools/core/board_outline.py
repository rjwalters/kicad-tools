"""Shared, layer-aware bounds of top-level PCB outline geometry."""

from __future__ import annotations

import math
from collections.abc import Iterator

from kicad_tools.sexp import SExp

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]


def _coordinate(child: SExp, context: str) -> Point:
    if len(child.children) != 2:
        raise ValueError(f"Malformed Edge.Cuts {context}: expected two coordinates")
    x, y = child.get_float(0), child.get_float(1)
    if x is None or y is None or not math.isfinite(x) or not math.isfinite(y):
        raise ValueError(f"Malformed Edge.Cuts {context}: invalid coordinate")
    return x, y


def _point(node: SExp, tag: str) -> Point:
    child = node.find_child(tag)
    if child is None:
        raise ValueError(f"Malformed Edge.Cuts {node.tag}: missing {tag} coordinate")
    return _coordinate(child, f"{node.tag} {tag}")


def _arc_points(start: Point, mid: Point, end: Point) -> list[Point]:
    # Translate first to avoid cancellation for boards far from sheet origin.
    bx, by = mid[0] - start[0], mid[1] - start[1]
    cx, cy = end[0] - start[0], end[1] - start[1]
    det = 2 * (bx * cy - by * cx)
    if abs(det) < 1e-12:
        raise ValueError("Malformed Edge.Cuts gr_arc: collinear arc points")
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    ox = start[0] + (cy * b2 - by * c2) / det
    oy = start[1] + (bx * c2 - cx * b2) / det
    radius = math.hypot(start[0] - ox, start[1] - oy)
    a, m, b = (math.atan2(y - oy, x - ox) for x, y in (start, mid, end))
    span = (b - a) % math.tau
    ccw = (m - a) % math.tau <= span
    points = [start, mid, end]
    for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        on_arc = (
            ((angle - a) % math.tau <= span + 1e-12)
            if ccw
            else ((a - angle) % math.tau <= (a - b) % math.tau + 1e-12)
        )
        if on_arc:
            points.append((ox + radius * math.cos(angle), oy + radius * math.sin(angle)))
    return points


def _curve_points(points: list[Point]) -> list[Point]:
    if len(points) != 4:
        raise ValueError("Malformed Edge.Cuts gr_curve: expected four cubic control points")
    extrema = {0.0, 1.0}
    for axis in (0, 1):
        p0, p1, p2, p3 = (p[axis] for p in points)
        a, b, c = -p0 + 3 * p1 - 3 * p2 + p3, 2 * (p0 - 2 * p1 + p2), p1 - p0
        if abs(a) < 1e-12:
            roots = [-c / b] if abs(b) >= 1e-12 else []
        else:
            discriminant = b * b - 4 * a * c
            roots = (
                [(-b + sign * math.sqrt(discriminant)) / (2 * a) for sign in (-1, 1)]
                if discriminant >= 0
                else []
            )
        extrema.update(t for t in roots if 0 < t < 1)
    return [
        (
            sum(
                w * p[0]
                for w, p in zip(
                    ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t**3),
                    points,
                    strict=True,
                )
            ),
            sum(
                w * p[1]
                for w, p in zip(
                    ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t**3),
                    points,
                    strict=True,
                )
            ),
        )
        for t in extrema
    ]


def outline_graphics(root: SExp) -> Iterator[SExp]:
    """Yield only direct, explicitly layer-qualified board outline graphics."""
    for node in root.iter_children():
        layer = node.find_child("layer")
        if node.tag and node.tag.startswith("gr_") and layer and layer.get_string(0) == "Edge.Cuts":
            yield node


def board_outline_segments(root: SExp) -> list[tuple[Point, Point]]:
    """Exact straight edges for routing; reject unsupported curved boundaries."""
    segments: list[tuple[Point, Point]] = []
    for node in outline_graphics(root):
        if node.tag == "gr_line":
            segments.append((_point(node, "start"), _point(node, "end")))
        elif node.tag == "gr_rect":
            a, b = _point(node, "start"), _point(node, "end")
            corners = [a, (b[0], a[1]), b, (a[0], b[1])]
            segments.extend(zip(corners, corners[1:] + corners[:1], strict=True))
        elif node.tag == "gr_poly":
            pts = node.find_child("pts")
            chain = [_coordinate(xy, "gr_poly xy") for xy in pts.find_children("xy")] if pts else []
            if len(chain) < 3:
                raise ValueError("Malformed Edge.Cuts gr_poly: missing points")
            segments.extend(zip(chain, chain[1:] + chain[:1], strict=True))
        else:
            raise ValueError(
                f"Unsupported routing Edge.Cuts geometry: {node.tag}; straight edges are required"
            )
    return segments


def board_outline_bounds(root: SExp) -> Bounds | None:
    """Return sheet-absolute bounds, ignoring non-outline and nested graphics.

    Bounds describe the geometry, excluding stroke width. Missing geometry
    returns None; malformed or unsupported Edge.Cuts graphics raise ValueError.
    This does not validate outline closure or topology.
    """
    points: list[Point] = []
    for node in outline_graphics(root):
        if node.tag in ("gr_rect", "gr_line"):
            points.extend((_point(node, "start"), _point(node, "end")))
        elif node.tag == "gr_arc":
            points.extend(
                _arc_points(_point(node, "start"), _point(node, "mid"), _point(node, "end"))
            )
        elif node.tag == "gr_circle":
            center, end = _point(node, "center"), _point(node, "end")
            radius = math.dist(center, end)
            points.extend(
                ((center[0] - radius, center[1] - radius), (center[0] + radius, center[1] + radius))
            )
        elif node.tag in ("gr_poly", "gr_curve"):
            pts = node.find_child("pts")
            chain: list[Point] = []
            if pts is not None:
                for xy in pts.find_children("xy"):
                    chain.append(_coordinate(xy, f"{node.tag} xy"))
            if len(chain) < 3:
                raise ValueError(f"Malformed Edge.Cuts {node.tag}: missing points")
            points.extend(_curve_points(chain) if node.tag == "gr_curve" else chain)
        else:
            raise ValueError(f"Unsupported Edge.Cuts geometry: {node.tag}")
    if not points:
        return None
    xs, ys = zip(*points, strict=True)
    return min(xs), min(ys), max(xs), max(ys)
