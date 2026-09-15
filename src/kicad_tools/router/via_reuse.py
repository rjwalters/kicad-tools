"""Connect candidate copper to existing vias before clearance validation."""

from __future__ import annotations

import math

from .layers import Layer
from .primitives import Route, Segment, Via
from .quantize import dogleg_points


def _physical_span(via: Via) -> tuple[int, int]:
    # Ordinary vias serialize as through-hole vias; only microvias retain
    # partial spans. Match the copper KiCad materializes without rewriting
    # the existing route's declared layer endpoints.
    if not via.is_micro:
        return Layer.F_CU.value, Layer.B_CU.value
    first, last = sorted(layer.value for layer in via.layers)
    return first, last


def reuse_same_net_vias(route: Route, existing_routes: list[Route], clearance: float) -> None:
    """Replace nearby candidate vias with stubs to an existing same-net barrel.

    Only the candidate is changed. Existing spans and copper stay intact;
    callers must validate the complete candidate before committing it.
    """
    existing = [v for r in existing_routes if r.net == route.net for v in r.vias]
    retained = []
    for via in route.vias:
        if via.in_pad or via.is_micro:
            retained.append(via)
            continue
        lo, hi = sorted(layer.value for layer in via.layers)
        eligible = []
        for other in existing:
            other_lo, other_hi = _physical_span(other)
            if other.net != route.net or other_lo > lo or other_hi < hi:
                continue
            distance = math.hypot(via.x - other.x, via.y - other.y)
            if distance < (via.diameter + other.diameter) / 2 + clearance:
                eligible.append((distance, other))
        if not eligible:
            retained.append(via)
            continue
        _, other = min(eligible, key=lambda item: item[0])
        other_lo, other_hi = _physical_span(other)
        widths: dict[Layer, float] = {}
        for segment in route.segments:
            if any(
                math.hypot(x - via.x, y - via.y) < 1e-6 for x, y in (segment.start, segment.end)
            ):
                widths[segment.layer] = max(widths.get(segment.layer, 0), segment.width)
        if not widths or any(not other_lo <= layer.value <= other_hi for layer in widths):
            retained.append(via)
            continue
        points = dogleg_points(via.x, via.y, other.x, other.y)
        for layer, width in widths.items():
            for start, end in zip(points, points[1:], strict=False):
                if start == end:
                    continue
                route.segments.append(
                    Segment(*start, *end, width, layer, net=route.net, net_name=route.net_name)
                )
    route.vias = retained
