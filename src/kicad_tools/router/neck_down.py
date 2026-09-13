"""Preserve pad-local width tapers when emitting merged routing segments."""

from __future__ import annotations

import math


def taper_points(
    p0: tuple[float, float],
    p1: tuple[float, float],
    centers: list[tuple[float, float]],
    radius: float,
    resolution: float,
) -> list[tuple[float, float]]:
    """Split only the pad-local parts of a straight segment at grid resolution.

    Search reserves the net-class width. Emission may narrow that envelope
    near a fine-pitch pad, but must recover full width outside its taper disk.
    Circle intersections delimit that disk; sampling inside it represents the
    width profile without fragmenting the full-width corridor.
    """
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length2 = dx * dx + dy * dy
    if not centers or radius <= 0 or length2 == 0:
        return [p0, p1]
    length = math.sqrt(length2)
    cuts = {0.0, 1.0}
    for cx, cy in centers:
        ox, oy = p0[0] - cx, p0[1] - cy
        b = ox * dx + oy * dy
        discriminant = b * b - length2 * (ox * ox + oy * oy - radius * radius)
        if discriminant <= 0:
            continue
        root = math.sqrt(discriminant)
        lo = max(0.0, (-b - root) / length2)
        hi = min(1.0, (-b + root) / length2)
        if hi <= lo:
            continue
        count = max(1, math.ceil((hi - lo) * length / max(resolution, 1e-6)))
        cuts.update(lo + (hi - lo) * i / count for i in range(count + 1))
    return [(p0[0] + t * dx, p0[1] + t * dy) for t in sorted(cuts)]
