"""Endpoint-preserving, bounded-error routing outline chains.

These helpers return geometry only. Callers MUST carry ``max_error`` into
clearance consumers as a Hausdorff allowance, not absorb it into a comparison
epsilon. A resource or floating-point limit refuses the outline instead of
silently relaxing its bound. Authored coordinates are never rewritten.
"""

from __future__ import annotations

import math
from fractions import Fraction

Point = tuple[float, float]
DEFAULT_MAX_ERROR_MM = 1e-4
MAX_SEGMENTS = 20_000
MAX_DEPTH = 32


def _budget(points: list[Point], max_error: float, max_segments: int) -> float:
    if not math.isfinite(max_error) or max_error <= 0:
        raise ValueError("Outline max_error must be finite and positive")
    if not isinstance(max_segments, int) or max_segments < 1:
        raise ValueError("Outline segment resource budget must be positive")
    if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points):
        raise ValueError("Malformed Edge.Cuts: nonfinite or invalid coordinates")
    # Conservative precision preflight, not the geometric certificate. Leave
    # headroom for radius/angle construction at large sheet offsets. Cubic
    # subdivision is checked exactly; rounded arc chords are certified again
    # by exact annulus containment after construction.
    rounding = 256 * max(math.ulp(v) for p in points for v in p)
    remaining = max_error - rounding
    if remaining <= 0 or not math.isfinite(rounding):
        raise ValueError("Cannot certify outline error at this coordinate precision")
    return remaining


RationalPoint = tuple[Fraction, Fraction]


def _inside_chord_capsule(
    point: RationalPoint, start: RationalPoint, end: RationalPoint, error_squared: Fraction
) -> bool:
    """Exact finite-segment distance comparison, without division or square roots."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    px, py = point[0] - start[0], point[1] - start[1]
    length_squared = dx * dx + dy * dy
    projection = px * dx + py * dy
    if projection <= 0:
        return px * px + py * py <= error_squared
    if projection >= length_squared:
        qx, qy = point[0] - end[0], point[1] - end[1]
        return qx * qx + qy * qy <= error_squared
    cross = px * dy - py * dx
    return cross * cross <= error_squared * length_squared


def _mid(a: RationalPoint, b: RationalPoint) -> RationalPoint:
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2


def tessellate_cubic(
    points: list[Point],
    *,
    max_error: float = DEFAULT_MAX_ERROR_MM,
    max_segments: int = MAX_SEGMENTS,
    max_depth: int = MAX_DEPTH,
) -> list[Point]:
    """Adaptive de Casteljau subdivision with a two-sided geometric bound.

    A chord's closed epsilon-neighborhood (a capsule) is convex. If all four
    control points lie inside it, their Bezier curve does too. Conversely the
    continuous projection of that curve onto the chord covers the chord from
    its first endpoint to its last, so every chord point is within epsilon of
    the curve. Thus the maximum control-point distance to the *finite chord*
    bounds Hausdorff error in both directions. Using the infinite chord line
    would incorrectly flatten collinear curves that overshoot/backtrack.
    """
    if len(points) != 4:
        raise ValueError("Malformed Edge.Cuts gr_curve: expected four cubic control points")
    if not isinstance(max_depth, int) or not 0 <= max_depth <= MAX_DEPTH:
        raise ValueError(f"Outline subdivision depth budget must be in [0, {MAX_DEPTH}]")
    _budget(points, max_error, max_segments)
    # All subdivision and acceptance arithmetic is exact on parsed binary
    # floats. Convex combinations stay inside the input coordinate ranges;
    # converting each output coordinate to float costs <= one input-scale
    # ulp. Two ulps bound the Euclidean endpoint displacement, and therefore
    # the entire output chord displacement. Subtract this allowance exactly.
    rounding = 2 * max(math.ulp(v) for p in points for v in p)
    error_squared = (Fraction(max_error) - Fraction(rounding)) ** 2
    output = [points[0]]
    rational = [(Fraction(x), Fraction(y)) for x, y in points]
    stack = [(rational, 0)]
    while stack:
        control, depth = stack.pop()
        p0, p1, p2, p3 = control
        if _inside_chord_capsule(p1, p0, p3, error_squared) and _inside_chord_capsule(
            p2, p0, p3, error_squared
        ):
            if len(output) > max_segments:
                raise ValueError("Outline subdivision exceeds segment resource budget")
            output.append((float(p3[0]), float(p3[1])))
            continue
        if depth >= max_depth or len(output) + len(stack) >= max_segments:
            raise ValueError("Cannot certify cubic within subdivision resource budget")
        a, b, c = _mid(p0, p1), _mid(p1, p2), _mid(p2, p3)
        d, e = _mid(a, b), _mid(b, c)
        f = _mid(d, e)
        stack.append(([f, e, c, p3], depth + 1))
        stack.append(([p0, a, d, f], depth + 1))
    return output


def _arc_center(start: Point, mid: Point, end: Point) -> tuple[RationalPoint, int]:
    # Exact rational arithmetic on the parsed binary floats avoids a near-
    # collinear circumcenter cancellation or a flipped major/minor sweep.
    x0, y0 = map(Fraction, start)
    bx, by = Fraction(mid[0]) - x0, Fraction(mid[1]) - y0
    cx, cy = Fraction(end[0]) - x0, Fraction(end[1]) - y0
    det = 2 * (bx * cy - by * cx)
    if not det:
        raise ValueError("Malformed Edge.Cuts gr_arc: collinear/coincident arc points")
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    center = x0 + (cy * b2 - by * c2) / det, y0 + (bx * c2 - cx * b2) / det
    return center, 1 if det > 0 else -1


def _certify_arc_chords(
    points: list[Point], center: RationalPoint, radius: float, direction: int, max_error: float
) -> None:
    """Certify the actual rounded chords by exact annulus containment.

    Radial projection maps a short, correctly oriented chord onto its circle
    arc. If every chord point is in the radius +/- error annulus, this maps
    both sets within error. Exact squared-distance comparisons check the
    nearest point and both endpoints (the farthest point is an endpoint).
    Thus trig/center rounding cannot silently invalidate the sagitta budget.
    """
    relative = [(Fraction(x) - center[0], Fraction(y) - center[1]) for x, y in points]
    radius_squared = relative[0][0] ** 2 + relative[0][1] ** 2
    exponent = math.frexp(radius)[1]
    scaled = radius_squared / Fraction(2) ** (2 * exponent)
    rounded_radius = math.ldexp(math.sqrt(float(scaled)), exponent)
    lower, upper = rounded_radius, rounded_radius
    # Enclose the exact irrational radius, checking the enclosure rather than
    # trusting a libm rounding claim. A failed numerical enclosure refuses.
    for _ in range(16):
        if Fraction(lower) ** 2 <= radius_squared <= Fraction(upper) ** 2:
            break
        lower = math.nextafter(lower, 0.0)
        upper = math.nextafter(upper, math.inf)
    else:
        raise ValueError("Cannot certify arc radius enclosure")
    inner_squared = max(Fraction(0), Fraction(upper) - Fraction(max_error)) ** 2
    outer_squared = (Fraction(lower) + Fraction(max_error)) ** 2
    for a, b in zip(relative[:-1], relative[1:], strict=True):
        a2, b2 = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2
        dx, dy = b[0] - a[0], b[1] - a[1]
        length_squared = dx * dx + dy * dy
        cross = a[0] * b[1] - a[1] * b[0]
        if direction * cross <= 0 or not length_squared:
            raise ValueError("Cannot certify arc chord orientation")
        projection = -(a[0] * dx + a[1] * dy)
        nearest_squared = (
            a2
            if projection <= 0
            else b2
            if projection >= length_squared
            else cross * cross / length_squared
        )
        if nearest_squared < inner_squared or max(a2, b2) > outer_squared:
            raise ValueError("Cannot certify rounded arc chord error")


def tessellate_arc(
    start: Point,
    mid: Point,
    end: Point,
    *,
    max_error: float = DEFAULT_MAX_ERROR_MM,
    max_segments: int = MAX_SEGMENTS,
) -> list[Point]:
    """Circular start/mid/end arc, preserving authored sweep and all three points.

    Split at the authored midpoint. For each subarc the chord sagitta is
    ``2*r*sin(abs(sweep)/(4*n))**2``. Its stable inverse uses asin/sqrt rather
    than subtracting nearly equal numbers in acos(1-error/r). Exact determinant
    sign selects orientation, including major arcs and wraparound.
    """
    _budget([start, mid, end], max_error, max_segments)
    exact_center, direction = _arc_center(start, mid, end)
    try:
        center = float(exact_center[0]), float(exact_center[1])
    except OverflowError as exc:
        raise ValueError("Cannot certify unrepresentable arc center") from exc
    radius = math.dist(start, center)
    available = _budget([start, mid, end, center, (radius, radius)], max_error, max_segments)
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("Malformed Edge.Cuts gr_arc: invalid radius")
    half_angle = math.asin(math.sqrt(min(0.25, available / (2 * radius))))
    if half_angle <= 0:
        raise ValueError("Cannot certify arc error at this radius")
    result = [start]
    for first, last in ((start, mid), (mid, end)):
        a = math.atan2(first[1] - center[1], first[0] - center[0])
        b = math.atan2(last[1] - center[1], last[0] - center[0])
        span = (direction * (b - a)) % math.tau
        if span == 0:
            raise ValueError("Cannot certify numerically unresolved arc sweep")
        count = max(1, math.ceil(span / (4 * half_angle)))
        if len(result) - 1 + count > max_segments:
            raise ValueError("Cannot certify arc within segment resource budget")
        for i in range(1, count):
            angle = a + direction * span * i / count
            result.append(
                (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))
            )
        result.append(last)
    _certify_arc_chords(result, exact_center, radius, direction, max_error)
    return result
