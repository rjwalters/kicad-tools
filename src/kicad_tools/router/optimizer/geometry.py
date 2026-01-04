"""Geometry helper functions for trace optimization."""

from __future__ import annotations

import math

from ..primitives import Segment


def segment_direction(seg: Segment, tolerance: float = 1e-4) -> float:
    """Calculate the direction of a segment in degrees (0-360).

    0° is positive X (right), 90° is positive Y (up),
    180° is negative X (left), 270° is negative Y (down).

    Args:
        seg: The segment to analyze.
        tolerance: Tolerance for zero-length detection.

    Returns:
        Direction in degrees (0-360).
    """
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1

    if abs(dx) < tolerance and abs(dy) < tolerance:
        return 0.0  # Zero-length segment

    angle = math.degrees(math.atan2(dy, dx))
    if angle < 0:
        angle += 360
    return angle


def is_connected(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if end of s1 connects to start of s2."""
    return abs(s1.x2 - s2.x1) < tolerance and abs(s1.y2 - s2.y1) < tolerance


def segments_touch(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if two segments share any endpoint (regardless of direction)."""
    # Check all four possible endpoint connections
    # s1.end -> s2.start
    if abs(s1.x2 - s2.x1) < tolerance and abs(s1.y2 - s2.y1) < tolerance:
        return True
    # s1.end -> s2.end
    if abs(s1.x2 - s2.x2) < tolerance and abs(s1.y2 - s2.y2) < tolerance:
        return True
    # s1.start -> s2.start
    if abs(s1.x1 - s2.x1) < tolerance and abs(s1.y1 - s2.y1) < tolerance:
        return True
    # s1.start -> s2.end
    if abs(s1.x1 - s2.x2) < tolerance and abs(s1.y1 - s2.y2) < tolerance:
        return True

    return False


def same_direction(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if two segments have the same direction."""
    dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
    dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

    # Handle zero-length segments
    len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
    len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

    if len1 < tolerance or len2 < tolerance:
        return True  # Zero-length segments are "same direction"

    # Normalize
    dx1, dy1 = dx1 / len1, dy1 / len1
    dx2, dy2 = dx2 / len2, dy2 / len2

    # Cross product should be ~0 for parallel
    cross = abs(dx1 * dy2 - dy1 * dx2)
    # Dot product should be positive (same direction, not opposite)
    dot = dx1 * dx2 + dy1 * dy2

    return cross < 0.01 and dot > 0


def angle_between(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> float:
    """Calculate angle between two segments in degrees (0-180)."""
    dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
    dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

    len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
    len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

    if len1 < tolerance or len2 < tolerance:
        return 0

    # Dot product
    dot = dx1 * dx2 + dy1 * dy2
    cos_angle = dot / (len1 * len2)
    cos_angle = max(-1, min(1, cos_angle))  # Clamp for numerical stability

    return math.degrees(math.acos(cos_angle))


def is_90_degree_corner(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if two segments form a 90-degree corner."""
    angle = angle_between(s1, s2, tolerance)
    return 80 < angle < 100  # Allow some tolerance


def is_zigzag(s1: Segment, s2: Segment, s3: Segment, tolerance: float = 1e-4) -> bool:
    """Check if s2 is a zigzag (backtrack) between s1 and s3."""
    # Calculate angles
    angle12 = angle_between(s1, s2, tolerance)

    # Zigzag: s2 goes roughly opposite to s1, then s3 continues roughly same as s1
    # This means angle12 is close to 180 degrees
    return abs(angle12 - 180) < 30


def segment_length(seg: Segment) -> float:
    """Calculate the length of a segment."""
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    return math.sqrt(dx * dx + dy * dy)


def shorten_segment_end(seg: Segment, amount: float, min_length: float = 0.05) -> Segment | None:
    """Shorten a segment from its end by the given amount."""
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    length = math.sqrt(dx * dx + dy * dy)

    if length <= amount + min_length:
        return None  # Can't shorten enough

    # New end point
    ratio = (length - amount) / length
    new_x2 = seg.x1 + dx * ratio
    new_y2 = seg.y1 + dy * ratio

    return Segment(
        x1=seg.x1,
        y1=seg.y1,
        x2=new_x2,
        y2=new_y2,
        width=seg.width,
        layer=seg.layer,
        net=seg.net,
        net_name=seg.net_name,
    )


def shorten_segment_start(seg: Segment, amount: float, min_length: float = 0.05) -> Segment | None:
    """Shorten a segment from its start by the given amount."""
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    length = math.sqrt(dx * dx + dy * dy)

    if length <= amount + min_length:
        return None  # Can't shorten enough

    # New start point
    ratio = amount / length
    new_x1 = seg.x1 + dx * ratio
    new_y1 = seg.y1 + dy * ratio

    return Segment(
        x1=new_x1,
        y1=new_y1,
        x2=seg.x2,
        y2=seg.y2,
        width=seg.width,
        layer=seg.layer,
        net=seg.net,
        net_name=seg.net_name,
    )


def count_corners(segments: list[Segment], tolerance: float = 1e-4) -> int:
    """Count number of corners (direction changes) in a segment list."""
    if len(segments) < 2:
        return 0

    corners = 0
    for i in range(len(segments) - 1):
        if not same_direction(segments[i], segments[i + 1], tolerance):
            corners += 1
    return corners


def total_length(segments: list[Segment]) -> float:
    """Calculate total length of segments."""
    total = 0.0
    for seg in segments:
        total += segment_length(seg)
    return total
