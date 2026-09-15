"""Uncommitted pair-loop geometry with equal added lengths.

Callers must check board clearance, connectivity, and pair/group quality before
committing these candidates. This module does not change routing occupancy.
"""

from __future__ import annotations

import math
from dataclasses import replace

from .primitives import Segment

MAX_COORDINATED_LOOPS = 3


def coordinated_pair_loop(
    p_host: Segment,
    n_host: Segment,
    *,
    added_length: float,
    window_start: float,
    window_end: float,
    num_loops: int = 1,
) -> tuple[list[Segment], list[Segment]] | None:
    """Propose a U-loop on parallel hosts, maintaining their separation.

    Window distances are measured along P from its first endpoint. N's
    turns are inset by the host separation, so both paths gain exactly
    ``added_length`` even though the inner horizontal leg is shorter.
    Up to three shallow loops may share the window, with equal total added
    length and space between neighboring bends. This is one proposed insertion.
    Either N orientation is supported. Endpoints and segment metadata stay
    intact; the input hosts are never modified. No board clearance is implied.
    """
    if (
        isinstance(num_loops, bool)
        or not isinstance(num_loops, int)
        or not 1 <= num_loops <= MAX_COORDINATED_LOOPS
    ):
        return None
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
    gap = spacing + max(p_host.width, n_host.width)
    loop_width = (window_end - window_start - (num_loops - 1) * gap) / num_loops
    inner_start, inner_end = window_start + spacing, window_start + loop_width - spacing
    if not (0 < window_start < window_end < length):
        return None
    if inner_end - inner_start <= max(p_host.width, n_host.width):
        return None
    if not (min(n_start, n_end) < inner_start < window_end - spacing < max(n_start, n_end)):
        return None
    amplitude = added_length / (2 * num_loops)

    def point(along: float, normal: float) -> tuple[float, float]:
        return (p_host.x1 + along * ux + normal * vx, p_host.y1 + along * uy + normal * vy)

    n_first, n_last = (n_host.start, n_host.end) if n_start < n_end else (n_host.end, n_host.start)
    p_points = [p_host.start]
    n_points = [n_first]
    for i in range(num_loops):
        start = window_start + i * (loop_width + gap)
        end = start + loop_width
        p_points.extend(
            [point(start, 0), point(start, amplitude), point(end, amplitude), point(end, 0)]
        )
        n_points.extend(
            [
                point(start + spacing, -spacing),
                point(start + spacing, amplitude - spacing),
                point(end - spacing, amplitude - spacing),
                point(end - spacing, -spacing),
            ]
        )
    p_points.append(p_host.end)
    n_points.append(n_last)
    if n_start > n_end:
        n_points.reverse()

    def segments(host: Segment, points: list[tuple[float, float]]) -> list[Segment]:
        return [
            replace(host, x1=a[0], y1=a[1], x2=b[0], y2=b[1])
            for a, b in zip(points[:-1], points[1:], strict=True)
        ]

    return segments(p_host, p_points), segments(n_host, n_points)
