"""Uncommitted pair-loop geometry with equal added lengths.

Callers must check board clearance, connectivity, and pair/group quality before
committing these candidates. This module does not change routing occupancy.
"""

from __future__ import annotations

import math
from dataclasses import replace

from .primitives import Segment


def coordinated_pair_loop(
    p_host: Segment,
    n_host: Segment,
    *,
    added_length: float,
    window_start: float,
    window_end: float,
) -> tuple[list[Segment], list[Segment]] | None:
    """Propose a U-loop on parallel hosts, maintaining their separation.

    Window distances are measured along P from its first endpoint. N's
    turns are inset by the host separation, so both paths gain exactly
    ``added_length`` even though the inner horizontal leg is shorter.
    Either N orientation is supported. Endpoints and segment metadata stay
    intact; the input hosts are never modified. No board clearance is implied.
    """
    values = (
        p_host.x1,
        p_host.y1,
        p_host.x2,
        p_host.y2,
        n_host.x1,
        n_host.y1,
        n_host.x2,
        n_host.y2,
        p_host.width,
        n_host.width,
        added_length,
        window_start,
        window_end,
    )
    if not all(math.isfinite(v) for v in values):
        return None
    if p_host.layer != n_host.layer or min(p_host.width, n_host.width, added_length) <= 0:
        return None
    dx, dy = p_host.x2 - p_host.x1, p_host.y2 - p_host.y1
    length = math.hypot(dx, dy)
    ndx, ndy = n_host.x2 - n_host.x1, n_host.y2 - n_host.y1
    n_length = math.hypot(ndx, ndy)
    if min(length, n_length) <= 1e-9:
        return None
    ux, uy = dx / length, dy / length
    if abs(ndx * uy - ndy * ux) > 1e-9 * n_length:
        return None
    vx, vy = -uy, ux
    offset = (n_host.x1 - p_host.x1) * vx + (n_host.y1 - p_host.y1) * vy
    if abs(offset) <= (p_host.width + n_host.width) / 2:
        return None
    # Point the loop away from N; N follows inside the same bend.
    if offset > 0:
        vx, vy = -vx, -vy
    spacing = abs(offset)
    n_start = (n_host.x1 - p_host.x1) * ux + (n_host.y1 - p_host.y1) * uy
    n_end = (n_host.x2 - p_host.x1) * ux + (n_host.y2 - p_host.y1) * uy
    inner_start, inner_end = window_start + spacing, window_end - spacing
    if not (0 < window_start < window_end < length):
        return None
    if inner_end - inner_start <= max(p_host.width, n_host.width):
        return None
    if not (min(n_start, n_end) < inner_start < inner_end < max(n_start, n_end)):
        return None
    amplitude = added_length / 2

    def point(along: float, normal: float) -> tuple[float, float]:
        return (p_host.x1 + along * ux + normal * vx, p_host.y1 + along * uy + normal * vy)

    p_points = [
        p_host.start,
        point(window_start, 0),
        point(window_start, amplitude),
        point(window_end, amplitude),
        point(window_end, 0),
        p_host.end,
    ]
    n_first, n_last = (n_host.start, n_host.end) if n_start < n_end else (n_host.end, n_host.start)
    n_points = [
        n_first,
        point(inner_start, -spacing),
        point(inner_start, amplitude - spacing),
        point(inner_end, amplitude - spacing),
        point(inner_end, -spacing),
        n_last,
    ]
    if n_start > n_end:
        n_points.reverse()

    def segments(host: Segment, points: list[tuple[float, float]]) -> list[Segment]:
        return [
            replace(host, x1=a[0], y1=a[1], x2=b[0], y2=b[1])
            for a, b in zip(points[:-1], points[1:], strict=True)
        ]

    return segments(p_host, p_points), segments(n_host, n_points)
