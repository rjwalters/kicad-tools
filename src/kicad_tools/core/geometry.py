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

    def _cross(ox: float, oy: float, px: float, py: float, qx: float, qy: float) -> float:
        """Sign of cross product (OP x OQ)."""
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = _cross(bx1, by1, bx2, by2, ax1, ay1)
    d2 = _cross(bx1, by1, bx2, by2, ax2, ay2)
    d3 = _cross(ax1, ay1, ax2, ay2, bx1, by1)
    d4 = _cross(ax1, ay1, ax2, ay2, bx2, by2)

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
    # If segments properly intersect, distance is zero
    if segments_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
        return 0.0

    # Otherwise, the minimum distance is the smallest of the four
    # endpoint-to-segment distances
    d1 = point_to_segment_distance(x1, y1, x3, y3, x4, y4)
    d2 = point_to_segment_distance(x2, y2, x3, y3, x4, y4)
    d3 = point_to_segment_distance(x3, y3, x1, y1, x2, y2)
    d4 = point_to_segment_distance(x4, y4, x1, y1, x2, y2)

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
