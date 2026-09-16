"""Preserve physical access at stranded Kelvin surface escape endpoints."""

from collections import defaultdict
from math import hypot

from .escape import EscapeRoute, EscapeRouter, PackageInfo
from .geometry import point_to_segment_distance, segment_to_segment_distance
from .kelvin import detect_kelvin_topology
from .pad_geometry import pad_point_distance
from .primitives import Pad


def _candidate_clear(
    router: EscapeRouter,
    candidate: EscapeRoute,
    siblings: list[EscapeRoute],
    pads: list[Pad],
    clearance: float,
    edge_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    edge_clearance: float,
) -> bool:
    via = candidate.via
    assert via is not None
    first_layer, last_layer = sorted(layer.value for layer in via.layers)
    if (
        candidate.escape_layer == candidate.pad.layer
        or via.layers[0] == via.layers[1]
        or not first_layer <= candidate.pad.layer.value <= last_layer
        or not first_layer <= candidate.escape_layer.value <= last_layer
    ):
        return False
    grid = router.grid
    if not grid.validate_via_clearance(via, via.net, clearance)[0]:
        return False
    if not grid.validate_via_to_via_clearance(via, via.net, clearance)[0]:
        return False
    # Same-net copper may merge, but its drilled holes must remain distinct.
    existing_vias = [v for route in grid.routes for v in route.vias]
    existing_vias.extend(escape.via for escape in siblings if escape.via is not None)
    drill_gap = max(router.rules.min_drill_clearance, router.rules.min_hole_to_hole)
    for other in existing_vias:
        if (
            hypot(via.x - other.x, via.y - other.y)
            < (via.drill + other.drill) / 2 + drill_gap - 1e-6
        ):
            return False
    # The lateral helper only receives this package's pads. Include every
    # physical land here, and prove ordinary-via clearance from own-net SMT.
    for pad in pads:
        if pad.net == via.net and pad.through_hole:
            continue
        if pad_point_distance(pad, via.x, via.y) < via.diameter / 2 + clearance - 1e-6:
            return False
    # Kelvin access must not join an existing same-net force/sense branch
    # before its shunt. Ordinary clearance validators deliberately allow that
    # electrical merge, so reject it explicitly here. The replaced escape is
    # the only same-pad geometry that may overlap the new stub.
    same_net_segments = [
        segment for route in grid.routes for segment in route.segments if segment.net == via.net
    ]
    same_net_vias = [other for route in grid.routes for other in route.vias if other.net == via.net]
    for sibling in siblings:
        if sibling.pad is candidate.pad:
            continue
        if sibling.pad.net == via.net:
            same_net_segments.extend(sibling.segments)
            if sibling.via is not None:
                same_net_vias.append(sibling.via)
    for segment in same_net_segments:
        if (
            point_to_segment_distance(via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2)
            < (via.diameter + segment.width) / 2 + clearance
        ):
            return False
    for segment in candidate.segments:
        for other_segment in same_net_segments:
            if (
                other_segment.layer == segment.layer
                and segment_to_segment_distance(
                    segment.x1,
                    segment.y1,
                    segment.x2,
                    segment.y2,
                    other_segment.x1,
                    other_segment.y1,
                    other_segment.x2,
                    other_segment.y2,
                )
                < (segment.width + other_segment.width) / 2 + clearance
            ):
                return False
        for other in same_net_vias:
            if (
                point_to_segment_distance(
                    other.x, other.y, segment.x1, segment.y1, segment.x2, segment.y2
                )
                < (segment.width + other.diameter) / 2 + clearance
            ):
                return False
        for other_pad in pads:
            if (
                other_pad is not candidate.pad
                and other_pad.net == via.net
                and (other_pad.layer == segment.layer or other_pad.through_hole)
                and router._segment_to_pad_edge_gap(segment, other_pad) < clearance
            ):
                return False
    for sibling in siblings:
        if sibling.pad.net == via.net:
            continue
        for segment in sibling.segments:
            # Conservatively treat the barrel as occupying every copper layer.
            if (
                point_to_segment_distance(
                    via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2
                )
                < (via.diameter + segment.width) / 2 + clearance - 1e-6
            ):
                return False
    for segment in candidate.segments:
        if not grid.validate_segment_clearance(segment, via.net, clearance)[0]:
            return False
        if router._in_pad_stub_conflicts(
            segment.x1,
            segment.y1,
            segment.x2,
            segment.y2,
            segment.width,
            segment.layer,
            via.net,
            clearance,
            siblings,
        ):
            return False
        for pad in pads:
            if pad.net == via.net or (pad.layer != segment.layer and not pad.through_hole):
                continue
            if router._segment_to_pad_edge_gap(segment, pad) < clearance - 1e-6:
                return False
    edge_margin = edge_clearance + float(getattr(edge_segments, "max_error_mm", 0.0))
    for (x1, y1), (x2, y2) in edge_segments:
        if point_to_segment_distance(via.x, via.y, x1, y1, x2, y2) < via.diameter / 2 + edge_margin:
            return False
        for segment in candidate.segments:
            if (
                segment_to_segment_distance(
                    segment.x1, segment.y1, segment.x2, segment.y2, x1, y1, x2, y2
                )
                < segment.width / 2 + edge_margin
            ):
                return False
    # Reject an out-of-bounds candidate; clamping would invalidate the geometry
    # just checked, potentially moving its via back onto an SMT land.
    if router.board_bounds is not None:
        left, bottom, right, top = router.board_bounds
        edge = router.edge_clearance or 0.0
        points = [(via.x, via.y, via.diameter / 2)]
        for segment in candidate.segments:
            points.extend(
                [
                    (segment.x1, segment.y1, segment.width / 2),
                    (segment.x2, segment.y2, segment.width / 2),
                ]
            )
        for x, y, radius in points:
            if not (
                left + edge + radius <= x <= right - edge - radius
                and bottom + edge + radius <= y <= top - edge - radius
            ):
                return False
    return True


def recover_kelvin_escapes(
    router: EscapeRouter,
    package: PackageInfo,
    escapes: list[EscapeRoute],
    pads: list[Pad],
    *,
    edge_segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
    edge_clearance: float = 0.0,
) -> list[EscapeRoute]:
    """Try a legal inward transition when a Kelvin endpoint remains in its land.

    Recognition uses the original physical terminals, including the shunt root.
    Both searches retain the normal via process, hole census and bounded offset.
    Existing escapes form the complete sibling context before any replacement.
    A failed recovery leaves the original escape unchanged.
    """
    by_net: dict[int, list[Pad]] = defaultdict(list)
    for pad in pads:
        by_net[pad.net].append(pad)
    kelvin_nets = {
        net
        for net, terminals in by_net.items()
        if net and detect_kelvin_topology(terminals) is not None
    }
    result = list(escapes)
    for index, escape in enumerate(result):
        pad = escape.pad
        if (
            pad.net not in kelvin_nets
            or escape.via is not None
            or pad.through_hole
            or escape.escape_layer != pad.layer
            or pad_point_distance(pad, *escape.escape_point) > 1e-6
        ):
            continue
        inward = router._OPPOSITE_DIRECTIONS.get(escape.direction)
        if inward is None:
            continue
        clearance = max(
            router.rules.trace_clearance,
            router.rules.via_clearance,
            router.rules.get_clearance_for_component(package.ref, pin_pitch=package.pin_pitch),
        )
        width = router._get_trace_width_for_net(pad.net_name)

        def candidate_clear(candidate: EscapeRoute) -> bool:
            return _candidate_clear(
                router, candidate, result, pads, clearance, edge_segments or [], edge_clearance
            )

        outward = router._try_lateral_via_escape(
            pad,
            escape.direction,
            clearance,
            width,
            package=package,
            existing_escapes=result,
            candidate_validator=candidate_clear,
        )
        if outward is not None:
            continue
        candidate = router._try_lateral_via_escape(
            pad,
            inward,
            clearance,
            width,
            package=package,
            existing_escapes=result,
            candidate_validator=candidate_clear,
        )
        if candidate is not None:
            result[index] = candidate
    return result
