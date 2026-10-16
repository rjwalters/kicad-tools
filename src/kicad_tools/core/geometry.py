"""Consolidated vector geometry primitives shared across the codebase.

Provides efficient, pure-Python implementations of core geometric operations:
point-to-segment distance, segment-to-segment distance, and segment
intersection testing.

All functions operate on raw coordinate scalars (floats or ints) for maximum
performance on hot paths.  Module-specific wrappers that accept higher-level
types (tuples, dataclass objects) should delegate to these canonical
implementations.

This module was created by consolidating duplicate implementations that had
been independently copy-pasted across 7+ modules (router, validate, mcp, cli,
optim, reasoning).
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Point-to-segment distance
# ---------------------------------------------------------------------------


def point_to_segment_distance(
    px: float,
    py: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Calculate the minimum distance from a point to a line segment.

    Projects the point onto the infinite line through (x1,y1)-(x2,y2),
    clamps the projection parameter *t* to [0, 1], and returns the
    Euclidean distance to the closest point on the segment.

    Args:
        px, py: Point coordinates.
        x1, y1: Segment start coordinates.
        x2, y2: Segment end coordinates.

    Returns:
        Minimum distance from the point to the segment (>= 0).
    """
    dx = x2 - x1
    dy = y2 - y1
    seg_len_sq = dx * dx + dy * dy

    # Rebase everything to the segment's start point so the computation uses
    # only coordinate *differences*.  This makes the result exactly invariant
    # under a rigid translation of all three points: differences are unchanged
    # by translation, whereas the earlier ``cx = x1 + t*dx`` / ``px - cx`` form
    # mixed absolute magnitudes into the final subtraction, so its floating
    # point rounding drifted with the board's absolute position (issue #3714 --
    # page_fit shifted boards and tipped a knife-edge diffpair-continuity check).
    apx = px - x1
    apy = py - y1

    if seg_len_sq == 0:
        # Degenerate segment (a single point)
        return math.sqrt(apx * apx + apy * apy)

    # Projection parameter, clamped to segment
    t = max(0.0, min(1.0, (apx * dx + apy * dy) / seg_len_sq))

    # Vector from the closest point on the segment to the query point, built
    # entirely from translation-invariant differences.
    rx = apx - t * dx
    ry = apy - t * dy

    return math.sqrt(rx * rx + ry * ry)


# ---------------------------------------------------------------------------
# Segment intersection test
# ---------------------------------------------------------------------------


def segments_intersect(
    ax1: float,
    ay1: float,
    ax2: float,
    ay2: float,
    bx1: float,
    by1: float,
    bx2: float,
    by2: float,
) -> bool:
    """Test whether two line segments properly intersect.

    Uses the standard cross-product orientation test.  Two segments
    intersect if and only if each segment straddles the line containing
    the other.

    Shared endpoints and collinear overlap are **not** counted as
    intersections (consistent with the pathfinder convention).

    Args:
        ax1, ay1, ax2, ay2: Endpoints of segment A.
        bx1, by1, bx2, by2: Endpoints of segment B.

    Returns:
        True if the segments share a proper interior point.
    """

    # Cross products inlined (OP x OQ): avoids rebuilding a closure and four
    # calls per invocation on the router's clearance hot path (Issue #5617).
    bdx = bx2 - bx1
    bdy = by2 - by1
    adx = ax2 - ax1
    ady = ay2 - ay1
    d1 = bdx * (ay1 - by1) - bdy * (ax1 - bx1)
    d2 = bdx * (ay2 - by1) - bdy * (ax2 - bx1)
    d3 = adx * (by1 - ay1) - ady * (bx1 - ax1)
    d4 = adx * (by2 - ay1) - ady * (bx2 - ax1)

    # Proper intersection: each segment straddles the line of the other
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and (
        (d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# Segment-to-segment distance
# ---------------------------------------------------------------------------


def segment_to_segment_distance(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    x3: float,
    y3: float,
    x4: float,
    y4: float,
) -> float:
    """Calculate the minimum distance between two line segments.

    First checks for proper intersection (returns 0 immediately),
    then falls back to checking all four endpoint-to-segment distances.

    Args:
        x1, y1, x2, y2: Endpoints of segment A.
        x3, y3, x4, y4: Endpoints of segment B.

    Returns:
        Minimum distance between the two segments (>= 0).
    """
    # Fully inlined (Issue #5617): the intersection test and the four
    # point-to-segment distances are expanded in place with the exact same
    # arithmetic as segments_intersect()/point_to_segment_distance(), saving
    # five calls per invocation.  The t clamp mirrors
    # ``max(0.0, min(1.0, v))`` (including NaN and -0.0 behaviour).
    adx = x2 - x1
    ady = y2 - y1
    bdx = x4 - x3
    bdy = y4 - y3

    c1 = bdx * (y1 - y3) - bdy * (x1 - x3)
    c2 = bdx * (y2 - y3) - bdy * (x2 - x3)
    if (c1 > 0 and c2 < 0) or (c1 < 0 and c2 > 0):
        c3 = adx * (y3 - y1) - ady * (x3 - x1)
        c4 = adx * (y4 - y1) - ady * (x4 - x1)
        if (c3 > 0 and c4 < 0) or (c3 < 0 and c4 > 0):
            return 0.0

    sqrt = math.sqrt
    b_len_sq = bdx * bdx + bdy * bdy
    a_len_sq = adx * adx + ady * ady

    # d1: (x1,y1) -> segment B ; d2: (x2,y2) -> segment B
    apx = x1 - x3
    apy = y1 - y3
    if b_len_sq == 0:
        d1 = sqrt(apx * apx + apy * apy)
    else:
        t = (apx * bdx + apy * bdy) / b_len_sq
        t = t if t < 1.0 else 1.0
        if not t > 0.0:
            t = 0.0
        rx = apx - t * bdx
        ry = apy - t * bdy
        d1 = sqrt(rx * rx + ry * ry)
    apx = x2 - x3
    apy = y2 - y3
    if b_len_sq == 0:
        d2 = sqrt(apx * apx + apy * apy)
    else:
        t = (apx * bdx + apy * bdy) / b_len_sq
        t = t if t < 1.0 else 1.0
        if not t > 0.0:
            t = 0.0
        rx = apx - t * bdx
        ry = apy - t * bdy
        d2 = sqrt(rx * rx + ry * ry)

    # d3: (x3,y3) -> segment A ; d4: (x4,y4) -> segment A
    apx = x3 - x1
    apy = y3 - y1
    if a_len_sq == 0:
        d3 = sqrt(apx * apx + apy * apy)
    else:
        t = (apx * adx + apy * ady) / a_len_sq
        t = t if t < 1.0 else 1.0
        if not t > 0.0:
            t = 0.0
        rx = apx - t * adx
        ry = apy - t * ady
        d3 = sqrt(rx * rx + ry * ry)
    apx = x4 - x1
    apy = y4 - y1
    if a_len_sq == 0:
        d4 = sqrt(apx * apx + apy * apy)
    else:
        t = (apx * adx + apy * ady) / a_len_sq
        t = t if t < 1.0 else 1.0
        if not t > 0.0:
            t = 0.0
        rx = apx - t * adx
        ry = apy - t * ady
        d4 = sqrt(rx * rx + ry * ry)

    return min(d1, d2, d3, d4)


# ---------------------------------------------------------------------------
# Segment clearance (edge-to-edge, accounting for trace widths)
# ---------------------------------------------------------------------------


def segment_clearance(
    ax1: float,
    ay1: float,
    ax2: float,
    ay2: float,
    width_a: float,
    bx1: float,
    by1: float,
    bx2: float,
    by2: float,
    width_b: float,
) -> float:
    """Calculate edge-to-edge clearance between two trace segments.

    The clearance is the center-to-center distance minus the sum of the
    half-widths of both traces.  A negative value indicates overlap.

    Args:
        ax1, ay1, ax2, ay2: Endpoints of segment A.
        width_a: Trace width of segment A.
        bx1, by1, bx2, by2: Endpoints of segment B.
        width_b: Trace width of segment B.

    Returns:
        Edge-to-edge clearance (negative means overlap).
    """
    center_dist = segment_to_segment_distance(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2)
    return center_dist - width_a / 2 - width_b / 2


# ---------------------------------------------------------------------------
# Footprint pad rotation (KiCad convention)
# ---------------------------------------------------------------------------


def rotate_pad_offset(
    local_x: float,
    local_y: float,
    rotation_deg: float,
) -> tuple[float, float]:
    """Rotate a pad's footprint-local offset into board-frame offset.

    This is the canonical forward local->world rotation for pad/footprint
    geometry. It MUST match KiCad's own ``pcbnew`` transform.

    KiCad applies the footprint orientation as a **negated** angle relative
    to the standard counter-clockwise math convention (verified directly
    against pcbnew 10.0.1, issue #3739)::

        rotated_x =  lx*cos(theta) + ly*sin(theta)
        rotated_y = -lx*sin(theta) + ly*cos(theta)

    which is equivalent to evaluating the standard CCW matrix at
    ``-rotation_deg``. For a footprint at (100,100) with a pad at local
    offset (2,0), this yields the pcbnew-verified world positions:

        ===  ====================
        deg  world (mm)
        ===  ====================
          0  (102, 100)
         90  (100,  98)
        180  ( 98, 100)
        270  (100, 102)
        ===  ====================

    The earlier standard-CCW form (PR #738) produced the mirror-image
    positions at 90/270 degrees (0/180 agree under both signs -- the
    "test trap" that hid the bug). To recover a board-frame offset back to
    footprint-local coordinates (the *inverse* transform), call this with
    ``-rotation_deg``.

    Args:
        local_x: Pad x offset in footprint-local frame.
        local_y: Pad y offset in footprint-local frame.
        rotation_deg: Footprint orientation in degrees.

    Returns:
        Tuple ``(rotated_x, rotated_y)`` board-frame offset (not yet
        translated by the footprint origin).
    """
    angle_rad = math.radians(-rotation_deg)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    rotated_x = local_x * cos_a - local_y * sin_a
    rotated_y = local_x * sin_a + local_y * cos_a
    return rotated_x, rotated_y
