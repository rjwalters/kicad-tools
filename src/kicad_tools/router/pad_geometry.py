"""Geometry of router pads' rotated rectangular envelopes.

Router Pad does not retain shape/corner radius. These predicates describe its
rectangle, not roundrect/oval/custom copper. Bounds alone are for broad phase.
"""

from __future__ import annotations

import math

from .geometry import segment_to_segment_distance
from .primitives import Pad


def pad_local_point(pad: Pad, x: float, y: float) -> tuple[float, float]:
    """Transform a board point into the residual-angle pad frame.

    KiCad applies clockwise (negative mathematical) board rotations. Inverting
    that transform uses the positive residual angle after IO's cardinal swap.
    """
    x, y = x - pad.x, y - pad.y
    if pad.rotation == 0:
        return x, y
    angle = math.radians(pad.rotation)
    c, s = math.cos(angle), math.sin(angle)
    return c * x - s * y, s * x + c * y


def pad_point_distance(pad: Pad, x: float, y: float) -> float:
    """Unsigned distance to the rectangular envelope (zero inside/on copper)."""
    x, y = pad_local_point(pad, x, y)
    return math.hypot(max(abs(x) - pad.width / 2, 0), max(abs(y) - pad.height / 2, 0))


def pad_contains_point(pad: Pad, x: float, y: float) -> bool:
    """Membership in the rotated rectangle, not its enclosing board AABB."""
    x, y = pad_local_point(pad, x, y)
    return abs(x) <= pad.width / 2 and abs(y) <= pad.height / 2


def pad_segment_distance(pad: Pad, x1: float, y1: float, x2: float, y2: float) -> float:
    """Exact unsigned minimum centerline-to-rectangle distance."""
    x1, y1 = pad_local_point(pad, x1, y1)
    x2, y2 = pad_local_point(pad, x2, y2)
    w, h = pad.width / 2, pad.height / 2
    if (-w <= x1 <= w and -h <= y1 <= h) or (-w <= x2 <= w and -h <= y2 <= h):
        return 0.0
    corners = [(-w, -h), (w, -h), (w, h), (-w, h)]
    return min(
        segment_to_segment_distance(x1, y1, x2, y2, *a, *b)
        for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
    )


def pad_directional_extent(pad: Pad, dx: float, dy: float) -> float:
    """Support extent along a board direction (unit vector gives millimeters)."""
    # Translate-free inverse rotation of the direction, then rectangle support.
    lx, ly = pad_local_point(pad, pad.x + dx, pad.y + dy)
    return abs(lx) * pad.width / 2 + abs(ly) * pad.height / 2
