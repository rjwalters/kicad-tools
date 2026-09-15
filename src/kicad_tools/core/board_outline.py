"""Shared, layer-aware bounds of top-level PCB outline geometry."""

from __future__ import annotations

import math
from collections.abc import Iterator

from kicad_tools.core.outline_tessellation import (
    DEFAULT_MAX_ERROR_MM,
    tessellate_arc,
    tessellate_cubic,
)
from kicad_tools.sexp import SExp

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]

# Maximum chord (sagitta) deviation, in mm, allowed between a tessellated
# ``gr_circle`` Edge.Cuts boundary and the true circle it approximates.
#
# **This is a bound, not an error budget.** The chords of an inscribed
# polygon lie strictly *inside* the true circle, so replacing a circle with
# its tessellation shifts measured distances in opposite directions
# depending on which side of the boundary the copper is on:
#
# * outer boundary (copper *inside* the circle) -- the chord is nearer the
#   copper than the true arc, so a clearance check is conservative;
# * interior cutout (copper *outside* the circle, e.g. a round mounting
#   hole) -- the chord is *farther* from the copper than the true arc, so
#   a check against the raw chords would *under*-report the violation and
#   could hide a real one.
#
# ``board_outline_segments`` has no notion of outline topology, so it
# cannot pick a tessellation that is conservative for both roles (an
# inscribed polygon is safe only for outer boundaries; a circumscribed one
# only for cutouts). Instead it certifies the approximation error and hands
# it to consumers as ``OutlineSegments.max_error_mm`` -- a Hausdorff bound
# between the returned chain and the true outline (exactly ``0.0`` when
# every element is straight). Distance consumers subtract it and keepout
# consumers add it, which is conservative regardless of topology:
#
# * ``router/io.py`` ``validate_routes`` compares ``closest_dist -
#   half_width - max_error_mm`` against ``edge_clearance -
#   _CLEARANCE_EPSILON_MM``;
# * ``RoutingGrid.add_edge_keepout`` paints ``clearance + max_error_mm``.
#
# The floating-point comparison epsilon (``1e-4`` mm) stays a *comparison*
# tolerance and is never spent on tessellation error. Keeping the bound at
# 1e-5 mm (10 nm) means the extra conservatism it buys is three orders of
# magnitude below any manufacturable clearance, so it never turns a passing
# design into a reported violation in practice. See
# ``tests/test_routing_outline_bounds.py`` for the numerically-tested bound
# and the cutout regression.
_CIRCLE_TESSELLATION_MAX_ERROR_MM = 1e-5

# Hard floor/ceiling on the number of chord segments used to approximate a
# circle, regardless of radius. The floor keeps small circles visually and
# numerically round; the ceiling bounds worst-case memory/compute for
# pathological (very large or near-degenerate) radii.
#
# The ceiling is a *resource* limit, never a silent weakening of the error
# bound: ``circle_segment_count`` raises ``ValueError`` when the requested
# radius/``max_error`` pair would need more than ``_CIRCLE_MAX_SEGMENTS``
# chords, rather than returning an under-tessellated chain whose real
# sagitta exceeds the advertised bound.
#
# 25000 chords honour ``_CIRCLE_TESSELLATION_MAX_ERROR_MM`` up to a radius
# of ~1265 mm -- a 2.5 m diameter circle, comfortably beyond KiCad's own
# ~1 m usable design area and far beyond any real PCB outline.
_CIRCLE_MIN_SEGMENTS = 12
_CIRCLE_MAX_SEGMENTS = 25000


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


def circle_sagitta(radius: float, segments: int) -> float:
    """Exact worst-case chord-to-arc gap for an inscribed ``segments``-gon.

    Equals ``radius * (1 - cos(pi / segments))``, evaluated through the
    half-angle identity ``1 - cos(x) == 2 * sin(x / 2) ** 2`` so it stays
    accurate for the large segment counts (tiny angles) where the direct
    form loses every significant digit to cancellation.

    This is also the Hausdorff distance between the inscribed polygon and
    the circle, so ``|distance(Q, polygon) - distance(Q, circle)| <=
    circle_sagitta(...)`` for *any* point ``Q`` -- inside or outside.
    """
    if segments < 3:
        raise ValueError("circle_sagitta: segments must be at least 3")
    return 2.0 * radius * math.sin(math.pi / (2 * segments)) ** 2


def circle_segment_count(
    radius: float,
    max_error: float = _CIRCLE_TESSELLATION_MAX_ERROR_MM,
) -> int:
    """Number of equal chords needed to keep sagitta error within ``max_error``.

    Uses the standard inscribed-regular-polygon sagitta bound: for ``n``
    equal chords on a circle of ``radius``, the maximum gap between a chord
    and the arc it spans is ``radius * (1 - cos(pi / n))`` (see
    ``circle_sagitta``). Solving for the smallest ``n`` that keeps this at
    or below ``max_error`` gives a tessellation whose Hausdorff distance to
    the true circle is bounded by ``max_error``.

    The bound is honoured or refused, never silently exceeded: if the
    requested ``radius``/``max_error`` pair needs more than
    ``_CIRCLE_MAX_SEGMENTS`` chords, this raises ``ValueError`` rather than
    clamping to the ceiling and returning a chain whose real sagitta is
    larger than advertised.

    Note that the returned polygon is *inscribed* -- it lies inside the
    circle -- so the bound alone does not make a clearance check safe.
    Consumers must additionally widen their comparison by the certified
    error (``OutlineSegments.max_error_mm``); see the module-level notes on
    ``_CIRCLE_TESSELLATION_MAX_ERROR_MM``.
    """
    if radius <= 0 or not math.isfinite(radius):
        raise ValueError("Malformed Edge.Cuts gr_circle: radius must be finite and positive")
    if max_error <= 0 or not math.isfinite(max_error):
        raise ValueError("circle_segment_count: max_error must be finite and positive")

    # sagitta(n) = 2 * radius * sin(pi / 2n)^2 <= max_error
    #   =>  sin(pi / 2n) <= sqrt(max_error / 2 radius)
    #   =>  n >= pi / (2 * asin(sqrt(max_error / 2 radius)))
    # Solving in this (asin of a small quantity) form rather than through
    # ``acos(1 - max_error / radius)`` avoids the catastrophic cancellation
    # that made the old formulation collapse to a zero half-angle -- and
    # then silently return the ceiling -- once ``max_error / radius``
    # rounded away entirely.
    sin_half = math.sqrt(max_error / (2.0 * radius))
    if sin_half >= 1.0:
        # max_error already covers the whole diameter: any polygon is
        # within the bound, so only the aesthetic floor governs.
        return _CIRCLE_MIN_SEGMENTS
    segments = max(_CIRCLE_MIN_SEGMENTS, math.ceil(math.pi / (2.0 * math.asin(sin_half))))
    # Re-verify against the exact sagitta: the closed form is solved in
    # floating point, so nudge up by the odd ulp rather than trusting it.
    while segments <= _CIRCLE_MAX_SEGMENTS and circle_sagitta(radius, segments) > max_error:
        segments += 1
    if segments > _CIRCLE_MAX_SEGMENTS:
        raise ValueError(
            f"Edge.Cuts gr_circle radius {radius}mm needs more than "
            f"{_CIRCLE_MAX_SEGMENTS} chords to stay within {max_error}mm of the "
            f"true circle; refusing to tessellate rather than silently exceed "
            f"the documented error bound"
        )
    return segments


def circle_tessellation_points(
    center: Point,
    radius: float,
    max_error: float = _CIRCLE_TESSELLATION_MAX_ERROR_MM,
) -> list[Point]:
    """Ordered, on-circle vertices of a closed chord chain approximating a circle.

    Raises ``ValueError`` when ``radius``/``max_error`` cannot be honoured
    within ``_CIRCLE_MAX_SEGMENTS`` chords (see ``circle_segment_count``).
    """
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


class OutlineSegments(list[tuple[Point, Point]]):
    """A straight-edge outline chain plus its certified approximation error.

    Behaves exactly like the ``list`` of ``((x1, y1), (x2, y2))`` tuples it
    replaces -- every existing consumer keeps working unchanged -- while
    carrying ``max_error_mm``: a Hausdorff bound between this chain and the
    true board outline it stands for.

    ``max_error_mm`` is ``0.0`` when every Edge.Cuts element was already
    straight (``gr_line``/``gr_rect``/``gr_poly``), and the largest
    per-element tessellation sagitta otherwise. It is a *geometry* bound and
    must never be conflated with a consumer's floating-point comparison
    epsilon.

    Consumers that measure distances to the outline must widen their
    comparison by this value in the conservative direction -- subtract it
    from a measured clearance, add it to a painted keepout -- because the
    chain may sit on either side of the true outline. Read it defensively
    (``getattr(segments, "max_error_mm", 0.0)``) so a plain ``list`` from an
    older caller, or a slice of this one, still type-checks; a plain list
    means "no certified curve error", which is only true for exact
    straight-edge geometry.
    """

    max_error_mm: float = 0.0

    def __init__(
        self,
        segments: list[tuple[Point, Point]] | None = None,
        max_error_mm: float = 0.0,
    ) -> None:
        super().__init__(segments or [])
        self.max_error_mm = max_error_mm


def _routing_curved_chain(node: SExp) -> list[Point]:
    """Preserve an authored cubic/arc path; never close unrelated graphics.

    Legacy arcs use the same signed-angle normalization as bounds. Zero or
    full/multiple turns cannot be represented by three distinct arc points,
    so routing explicitly refuses these rather than guessing their topology.
    Bounds behavior remains unchanged.
    """
    if node.tag == "gr_curve":
        pts = node.find_child("pts")
        chain = [_coordinate(xy, "gr_curve xy") for xy in pts.find_children("xy")] if pts else []
        return tessellate_cubic(chain)
    if node.tag != "gr_arc":
        raise ValueError(f"Unsupported curved routing Edge.Cuts geometry: {node.tag}")
    start, end = _point(node, "start"), _point(node, "end")
    if node.find_child("mid") is not None:
        mid = _point(node, "mid")
    else:
        angle = node.find_child("angle")
        sweep = angle.get_float(0) if angle is not None else None
        if angle is None or len(angle.children) != 1 or sweep is None or not math.isfinite(sweep):
            raise ValueError("Malformed Edge.Cuts gr_arc: missing mid or invalid legacy angle")
        if not 0 < abs(sweep) < 360:
            raise ValueError(
                "Unsupported routing Edge.Cuts legacy arc: require 0 < abs(sweep) < 360"
            )
        start, mid, end = legacy_arc_points(end, start, sweep)
    return tessellate_arc(start, mid, end)


def board_outline_segments(root: SExp) -> OutlineSegments:
    """Endpoint-preserving outline chains with a certified Hausdorff allowance.

    Straight elements remain exact. Cubic Beziers and modern/legacy arcs use
    certified adaptive geometry; circles use their independently bounded ring.
    Every authored graphic remains a separate component, with no guessed
    closure or replacement by bounds. Consumers must retain max_error_mm and
    account for it conservatively in distance checks and grid keepouts.
    """
    segments = OutlineSegments()
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
        elif node.tag in ("gr_curve", "gr_arc"):
            chain = _routing_curved_chain(node)
            segments.extend(zip(chain[:-1], chain[1:], strict=True))
            segments.max_error_mm = max(segments.max_error_mm, DEFAULT_MAX_ERROR_MM)
        elif node.tag == "gr_circle":
            center, end = _point(node, "center"), _point(node, "end")
            radius = math.dist(center, end)
            if radius <= 0:
                raise ValueError(
                    "Malformed Edge.Cuts gr_circle: zero-radius circle has no boundary"
                )
            ring = circle_tessellation_points(center, radius)
            segments.extend(zip(ring, ring[1:] + ring[:1], strict=True))
            segments.max_error_mm = max(segments.max_error_mm, circle_sagitta(radius, len(ring)))
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
