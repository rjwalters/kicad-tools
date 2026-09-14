"""Shared, layer-aware bounds of top-level PCB outline geometry."""

from __future__ import annotations

import math
from collections.abc import Iterator

from kicad_tools.sexp import SExp

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]

# Maximum chord (sagitta) deviation, in mm, allowed between a tessellated
# ``gr_circle`` Edge.Cuts boundary and the true circle it approximates.
#
# ``board_outline_segments`` is the sole feed for router edge-keepout
# painting (``RoutingGrid.add_edge_keepout``, which rounds its clearance
# radius *up* by a whole extra grid cell -- ``int(clearance / resolution) +
# 1`` -- dwarfing this bound) and for the post-route edge-clearance check
# in ``router/io.py`` (``actual_clearance < edge_clearance -
# _CLEARANCE_EPSILON_MM``, whose own floating-point epsilon is ``1e-4`` mm).
# Keeping the tessellation error an order of magnitude below that existing
# ``1e-4`` mm epsilon means no clearance verdict that would pass against
# the tessellated chain can newly disagree with the verdict against the
# true circle -- the discrepancy is already inside slack the comparisons
# tolerate today, for both interior (outer-boundary) and exterior
# (cutout/hole) points, independent of which side of the chord the true
# arc falls on. See ``tests/test_routing_outline_bounds.py`` for the
# numerically-tested bound.
_CIRCLE_TESSELLATION_MAX_ERROR_MM = 1e-5

# Hard floor/ceiling on the number of chord segments used to approximate a
# circle, regardless of radius. The floor keeps small circles visually and
# numerically round; the ceiling bounds worst-case memory/compute for
# pathological (very large or near-degenerate) radii while still keeping
# the resulting error small in absolute terms. 20000 comfortably keeps the
# sagitta within ``_CIRCLE_TESSELLATION_MAX_ERROR_MM`` for radii up to
# ~500 mm (already far larger than any real PCB outline); beyond that the
# sagitta grows slowly (roughly proportional to radius at a fixed segment
# count) but stays microns, not millimeters.
_CIRCLE_MIN_SEGMENTS = 12
_CIRCLE_MAX_SEGMENTS = 20000


def _rotate_point(
    point: tuple[float, float], center: tuple[float, float], angle_deg: float
) -> tuple[float, float]:
    """Rotate ``point`` about ``center`` by ``angle_deg`` degrees (CCW-positive).

    Used to normalize pre-KiCad-6 legacy ``gr_arc`` encodings (center + signed
    sweep angle) into modern on-arc start/mid/end points.
    """
    theta = math.radians(angle_deg)
    dx, dy = point[0] - center[0], point[1] - center[1]
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return (
        center[0] + dx * cos_t - dy * sin_t,
        center[1] + dx * sin_t + dy * cos_t,
    )


def legacy_arc_points(point: Point, center: Point, angle_deg: float) -> tuple[Point, Point, Point]:
    """Normalize legacy center/endpoint/signed-sweep arcs without changing coordinates."""
    return (
        point,
        _rotate_point(point, center, angle_deg / 2.0),
        _rotate_point(point, center, angle_deg),
    )


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


def circle_segment_count(
    radius: float,
    max_error: float = _CIRCLE_TESSELLATION_MAX_ERROR_MM,
) -> int:
    """Number of equal chords needed to keep sagitta error within ``max_error``.

    Uses the standard inscribed-regular-polygon sagitta bound: for ``n``
    equal chords on a circle of ``radius``, the maximum gap between a chord
    and the arc it spans is ``radius * (1 - cos(pi / n))``. Solving for the
    smallest ``n`` that keeps this at or below ``max_error`` gives a
    tessellation whose Hausdorff distance to the true circle is bounded by
    ``max_error`` (every polygon point lies on or inside the circle, and
    every circle point lies within ``max_error`` of the polygon boundary).
    That two-sided bound is what lets ``|distance(Q, polygon) -
    distance(Q, circle)| <= max_error`` hold for *any* point ``Q``,
    independent of whether the circle is an outer board boundary or an
    interior cutout.
    """
    if radius <= 0 or not math.isfinite(radius):
        raise ValueError("Malformed Edge.Cuts gr_circle: radius must be finite and positive")
    if max_error <= 0 or not math.isfinite(max_error):
        raise ValueError("circle_segment_count: max_error must be finite and positive")
    # cos(pi / n) = 1 - max_error / radius; solve for the half-chord angle.
    ratio = max(-1.0, min(1.0, 1.0 - max_error / radius))
    half_angle = math.acos(ratio)
    if half_angle <= 0:
        # max_error alone already exceeds the diameter -- any n keeps the
        # bound; fall back to the resolution ceiling for stability.
        return _CIRCLE_MAX_SEGMENTS
    segments = math.ceil(math.pi / half_angle)
    return max(_CIRCLE_MIN_SEGMENTS, min(_CIRCLE_MAX_SEGMENTS, segments))


def circle_tessellation_points(
    center: Point,
    radius: float,
    max_error: float = _CIRCLE_TESSELLATION_MAX_ERROR_MM,
) -> list[Point]:
    """Ordered, on-circle vertices of a closed chord chain approximating a circle."""
    n = circle_segment_count(radius, max_error)
    cx, cy = center
    return [
        (cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def outline_graphics(root: SExp) -> Iterator[SExp]:
    """Yield only direct, explicitly layer-qualified board outline graphics."""
    for node in root.iter_children():
        layer = node.find_child("layer")
        if node.tag and node.tag.startswith("gr_") and layer and layer.get_string(0) == "Edge.Cuts":
            yield node


def board_outline_segments(root: SExp) -> list[tuple[Point, Point]]:
    """Exact straight edges for routing; reject unsupported curved boundaries.

    ``gr_circle`` is the one curved exception: it is tessellated into a
    closed chain of chords bounded by ``_CIRCLE_TESSELLATION_MAX_ERROR_MM``
    (see ``circle_segment_count``), rather than rejected outright. This
    covers both circular outer boundaries and circular interior cutouts
    (e.g. round mounting holes drawn on Edge.Cuts) -- the function has no
    notion of outline topology, so every ``gr_circle`` element is
    tessellated the same way regardless of its role. Other curved
    geometry (``gr_arc``, ``gr_curve``) has no such treatment here and
    remains rejected.
    """
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
        elif node.tag == "gr_circle":
            center, end = _point(node, "center"), _point(node, "end")
            radius = math.dist(center, end)
            if radius <= 0:
                raise ValueError("Malformed Edge.Cuts gr_circle: zero-radius circle has no boundary")
            ring = circle_tessellation_points(center, radius)
            segments.extend(zip(ring, ring[1:] + ring[:1], strict=True))
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
            start, end = _point(node, "start"), _point(node, "end")
            if node.find_child("mid") is not None:
                mid = _point(node, "mid")
            else:
                angle = node.find_child("angle")
                sweep = angle.get_float(0) if angle is not None else None
                if (
                    angle is None
                    or len(angle.children) != 1
                    or sweep is None
                    or not math.isfinite(sweep)
                ):
                    raise ValueError(
                        "Malformed Edge.Cuts gr_arc: missing mid or invalid legacy angle"
                    )
                start, mid, end = legacy_arc_points(end, start, sweep)
            points.extend(_arc_points(start, mid, end))
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
