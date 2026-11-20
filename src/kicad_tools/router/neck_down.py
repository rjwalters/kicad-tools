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


def band_safe_width(
    a: tuple[float, float],
    b: tuple[float, float],
    width: float,
    base_width: float,
    own_net_bboxes: list[tuple[float, float, float, float]] | None,
    edge_mm: float,
) -> float:
    """Keep a tapered sub-segment out of the own-net pad slit band (#9019).

    The #1018 taper only ever NARROWS copper, and narrowing a leg that the
    search certified at ``base_width`` (touching its own pad, or clear of the
    fab ``pad_spacing`` detection edge) opens an air gap between the leg's
    edge and the pad: a leg that unioned with the pad corner at 0.2 mm lands
    in the ``(0, edge_mm)`` band at 0.1 mm -- the staircase of fab-view
    ``physical_copper_gap`` slits hugging a fine-pitch pad's corner.  When
    ``width`` leaves the segment in the band but ``base_width`` does not,
    keep ``base_width``; otherwise the taper width stands.

    Pure and backend-agnostic: the Python grid router and the C++ backend's
    route conversion both call it, so the two emit identical widths.
    ``own_net_bboxes`` are the segment's own-net, own-layer SMD pad boxes;
    ``edge_mm <= 0`` (stackup unresolved) or no pads leaves ``width`` as is.
    """
    if edge_mm <= 0 or not own_net_bboxes or width >= base_width:
        return width
    from .via_clearance import same_net_segment_pad_gap_acceptable

    if same_net_segment_pad_gap_acceptable(a, b, width / 2, own_net_bboxes, edge_mm):
        return width
    if same_net_segment_pad_gap_acceptable(a, b, base_width / 2, own_net_bboxes, edge_mm):
        return base_width
    return width
