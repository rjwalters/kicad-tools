"""Preserve pad-local width tapers when emitting merged routing segments."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad
    from .rules import DesignRules


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


def taper_width_fn(
    rules: DesignRules,
    start_pad: Pad,
    end_pad: Pad,
    start_pitch: float | None,
    end_pitch: float | None,
    base_width: float,
) -> tuple[Callable[[float, float, float, float], float], bool, bool]:
    """Build the per-piece Issue #1018 taper-width function.

    Both route converters (``PathFinder._convert_path_to_route`` and
    ``CppPathfinder._convert_result_to_route``) call the SAME closure
    (#9851), so a piece's width is bit-identical whichever backend
    converted the path: the minimum over both pads of
    ``rules.get_neck_down_width(min endpoint distance, pitch,
    base_width=base_width)``, gated per pad by
    ``rules.should_apply_neck_down``.  Issue #3313: ``base_width`` is the
    caller's net-class corridor width so the taper interpolates down from
    the (possibly impedance-driven) per-net width, not the global default.

    Returns ``(width_fn, start_needs_neckdown, end_needs_neckdown)``; the
    flags also gate the taper-disk ``centers`` the caller builds.
    """
    start_needs_neckdown = rules.should_apply_neck_down(start_pad.ref, start_pitch)
    end_needs_neckdown = rules.should_apply_neck_down(end_pad.ref, end_pitch)

    def _width(x1: float, y1: float, x2: float, y2: float) -> float:
        if not start_needs_neckdown and not end_needs_neckdown:
            return base_width
        min_width = base_width
        if start_needs_neckdown:
            min_dist_start = min(
                math.sqrt((x1 - start_pad.x) ** 2 + (y1 - start_pad.y) ** 2),
                math.sqrt((x2 - start_pad.x) ** 2 + (y2 - start_pad.y) ** 2),
            )
            min_width = min(
                min_width,
                rules.get_neck_down_width(min_dist_start, start_pitch, base_width=base_width),
            )
        if end_needs_neckdown:
            min_dist_end = min(
                math.sqrt((x1 - end_pad.x) ** 2 + (y1 - end_pad.y) ** 2),
                math.sqrt((x2 - end_pad.x) ** 2 + (y2 - end_pad.y) ** 2),
            )
            min_width = min(
                min_width,
                rules.get_neck_down_width(min_dist_end, end_pitch, base_width=base_width),
            )
        return min_width

    return _width, start_needs_neckdown, end_needs_neckdown


def tapered_run_pieces(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    centers: list[tuple[float, float]],
    radius: float,
    resolution: float,
    width_fn: Callable[[float, float, float, float], float],
    base_width: float,
    own_net_bboxes: list[tuple[float, float, float, float]] | None,
    edge_mm: float,
) -> list[tuple[tuple[float, float], tuple[float, float], float]]:
    """Split one straight run into dogleg-aware tapered pieces with widths.

    The exact pipeline the two route converters used to duplicate (#9851):
    dogleg an off-angle run (#3532), cut each leg inside the taper disks
    at grid resolution (:func:`taper_points`, #1018), then width each
    piece through ``width_fn`` and hold it out of the own-net slit band
    (:func:`band_safe_width`, #9019).  Running both backends through this
    one function makes them emit identical segment bytes for the same
    path.  Returns ``[((sx, sy), (ex, ey), width), ...]``; callers skip
    zero-length pieces at emission.
    """
    from .quantize import dogleg_points

    dogleg = dogleg_points(x1, y1, x2, y2)
    points = [dogleg[0]]
    for a, b in zip(dogleg, dogleg[1:], strict=False):
        points.extend(taper_points(a, b, centers, radius, resolution)[1:])
    pieces: list[tuple[tuple[float, float], tuple[float, float], float]] = []
    for a, b in zip(points, points[1:], strict=False):
        width = width_fn(a[0], a[1], b[0], b[1])
        if own_net_bboxes:
            width = band_safe_width(a, b, width, base_width, own_net_bboxes, edge_mm)
        pieces.append((a, b, width))
    return pieces
