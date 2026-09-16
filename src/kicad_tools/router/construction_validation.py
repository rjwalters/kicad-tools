"""Geometry acceptance checks for uncommitted constructed differential pairs."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from shapely.geometry import LineString, MultiLineString, Point  # type: ignore[import-untyped]
from shapely.ops import polygonize  # type: ignore[import-untyped]

from .primitives import Pad, Route, Segment
from .quantize import is_45_aligned

if TYPE_CHECKING:
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter


def parallel_copper_overlap_issue(
    segments: list[Segment],
    *,
    protected_points: list[tuple[float, float]] | None = None,
    deadline: float = math.inf,
) -> str | None:
    """Reject parallel copper foldbacks before they bypass measured path length."""
    # Nonadjacent parallel runs must not overlap in copper; such a
    # foldback shortens the electrical path even if centerlines stay simple.
    from .optimizer.consolidate import consolidate_segments

    segments, _ = consolidate_segments(
        segments, protected_points=protected_points or (), tolerance=1e-9
    )
    for i, segment in enumerate(segments):
        if time.monotonic() >= deadline:
            return "deadline"
        ux, uy = segment.x2 - segment.x1, segment.y2 - segment.y1
        length = math.hypot(ux, uy)
        for other in segments[:i]:
            if segment.layer != other.layer or any(
                math.dist(a, b) < 1e-8
                for a in (segment.start, segment.end)
                for b in (other.start, other.end)
            ):
                continue
            vx, vy = other.x2 - other.x1, other.y2 - other.y1
            if abs(ux * vy - uy * vx) > 1e-9:
                continue
            first = sorted((x * ux + y * uy) / length for x, y in (segment.start, segment.end))
            second = sorted((x * ux + y * uy) / length for x, y in (other.start, other.end))
            if min(first[1], second[1]) - max(first[0], second[0]) < max(
                segment.width, other.width
            ):
                continue
            if (
                LineString([segment.start, segment.end]).distance(
                    LineString([other.start, other.end])
                )
                < (segment.width + other.width) / 2 - 1e-9
            ):
                return "self_overlap"
    return None


def route_topology_issue(route: Route, start: Pad, end: Pad, *, deadline: float) -> str | None:
    """Check the complete copper graph, including full-stack ordinary vias."""
    if time.monotonic() >= deadline:
        return "deadline"
    if not route.segments or route.net != start.net or route.net != end.net:
        return "endpoints"
    objects: list[tuple[set[int] | None, Any]] = []
    for segment in route.segments:
        if (
            segment.net != route.net
            or not all(math.isfinite(v) for v in (*segment.start, *segment.end, segment.width))
            or segment.width <= 0
            or math.dist(segment.start, segment.end) <= 1e-9
            or not is_45_aligned(segment.x2 - segment.x1, segment.y2 - segment.y1)
        ):
            return "segment_geometry"
        objects.append(
            (
                {segment.layer.value},
                LineString([segment.start, segment.end]).buffer(segment.width / 2),
            )
        )
    for via in route.vias:
        if (
            via.net != route.net
            or not all(math.isfinite(v) for v in (via.x, via.y, via.diameter, via.drill))
            or not 0 < via.drill <= via.diameter
        ):
            return "via_geometry"
        layers = (
            set(range(min(l.value for l in via.layers), max(l.value for l in via.layers) + 1))
            if via.is_micro
            else None
        )
        objects.append((layers, Point(via.x, via.y).buffer(via.diameter / 2)))
    for layer in {s.layer for s in route.segments}:
        if time.monotonic() >= deadline:
            return "deadline"
        lines = [LineString([s.start, s.end]) for s in route.segments if s.layer == layer]
        if not MultiLineString(lines).is_simple or any(p.area > 1e-9 for p in polygonize(lines)):
            return "self_intersection"
    overlap = parallel_copper_overlap_issue(
        route.segments, protected_points=[(v.x, v.y) for v in route.vias], deadline=deadline
    )
    if overlap is not None:
        return overlap
    # Every copper object must belong to the endpoint-connected component;
    # a disconnected island must not contribute to measured route length.
    root = len(objects)
    objects.extend(
        [({start.layer.value}, Point(start.x, start.y)), ({end.layer.value}, Point(end.x, end.y))]
    )
    seen, pending = {root}, [root]
    while pending:
        if time.monotonic() >= deadline:
            return "deadline"
        index = pending.pop()
        layers, shape = objects[index]
        for j, (other_layers, other) in enumerate(objects):
            if (
                j not in seen
                and (layers is None or other_layers is None or layers & other_layers)
                and shape.distance(other) <= 1e-9
            ):
                seen.add(j)
                pending.append(j)
    if len(seen) != len(objects):
        return "disconnected"
    return None


def constructed_pair_geometry_issue(
    router: DiffPairRouter,
    pathfinder: CoupledPathfinder,
    p_route: Route,
    n_route: Route,
    pads: tuple[Pad, Pad, Pad, Pad],
    *,
    intra_pair_clearance: float,
    deadline: float,
    reserved_routes: tuple[Route, ...] = (),
) -> str | None:
    """Validate geometry without committing it; length/coupling are caller gates.

    Known pad/live-route raster halos may be resolved with exact geometry.
    Unknown blocked cells fail closed. All existing routes, the uncommitted
    pair, planned reservations, and drilled holes participate in clearance checks.
    """
    from .diffpair_routing import _segment_to_segment_distance
    from .via_clearance import drill_hole_to_hole_clear

    if not math.isfinite(intra_pair_clearance) or intra_pair_clearance < 0:
        return "invalid_clearance"
    grid, rules = router.autorouter.grid, router.autorouter.rules
    pair = (p_route, n_route)
    if p_route.net == n_route.net:
        return "same_net"
    for route, start, end in ((p_route, pads[0], pads[1]), (n_route, pads[2], pads[3])):
        issue = route_topology_issue(route, start, end, deadline=deadline)
        if issue:
            return issue
    universe = list(
        {
            id(r): r
            for r in [
                *getattr(grid, "routes", []),
                *router.autorouter.routes,
                *reserved_routes,
                *pair,
            ]
        }.values()
    )
    live = {id(r) for r in getattr(grid, "routes", [])}
    available = {
        token
        for token, ref in getattr(grid, "_route_geometry_sources", {}).items()
        if (route := ref()) is not None and id(route) in live
    }
    pad_cells: set[tuple[int, int, int]] = getattr(grid, "_pad_geometry_cells", set())
    route_cells = getattr(grid, "_route_geometry_cells", {})

    def cell_known(layer: int, y: int, x: int) -> bool:
        if not (0 <= x < grid.cols and 0 <= y < grid.rows):
            return False
        cell = grid.cell_at(layer, y, x)
        key = (layer, y, x)
        if key in grid._reserved_for_nets and key not in grid._soft_reservations:
            return False
        if not (cell.blocked or cell.pad_blocked or cell.is_obstacle) or key in pad_cells:
            return True
        sources = route_cells.get(key)
        return bool(sources and sources <= available)

    with router._shadow_foreign_copper(*universe):
        for route in pair:
            if time.monotonic() >= deadline:
                return "deadline"
            if router._route_pad_violation(route)[0] > 1e-9:
                return "pad_clearance"
            if router._route_via_violation(route)[0] > 1e-9:
                return "via_clearance"
            for segment in route.segments:
                if time.monotonic() >= deadline:
                    return "deadline"
                x1, y1 = grid.world_to_grid(*segment.start)
                x2, y2 = grid.world_to_grid(*segment.end)
                steps = max(abs(x2 - x1), abs(y2 - y1))
                radius = math.ceil(segment.width / (2 * grid.resolution))
                layer = grid.layer_to_index(segment.layer.value)
                for i in range(steps + 1):
                    if i % 64 == 0 and time.monotonic() >= deadline:
                        return "deadline"
                    t = i / steps if steps else 0
                    x, y = round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t)
                    if any(
                        not cell_known(layer, yy, xx)
                        for yy in range(y - radius, y + radius + 1)
                        for xx in range(x - radius, x + radius + 1)
                    ):
                        return "unknown_blocker"
                for other in universe:
                    if other.net == route.net:
                        continue
                    clearance = (
                        intra_pair_clearance
                        if other.net in (p_route.net, n_route.net)
                        else rules.trace_clearance
                    )
                    if any(
                        s.layer == segment.layer
                        and _segment_to_segment_distance(
                            *segment.start, *segment.end, *s.start, *s.end
                        )
                        < (segment.width + s.width) / 2 + clearance - 1e-9
                        for s in other.segments
                    ):
                        return "trace_clearance"
            drills = router._collect_existing_drills()
            for via in route.vias:
                if time.monotonic() >= deadline:
                    return "deadline"
                x, y = grid.world_to_grid(via.x, via.y)
                radius = max(
                    pathfinder._via_extra_cells, math.ceil(via.diameter / (2 * grid.resolution))
                )
                layers = (
                    range(
                        grid.layer_to_index(min(via.layers, key=lambda l: l.value).value),
                        grid.layer_to_index(max(via.layers, key=lambda l: l.value).value) + 1,
                    )
                    if via.is_micro
                    else range(grid.num_layers)
                )
                if any(
                    not cell_known(layer, yy, xx)
                    for layer in layers
                    for yy in range(y - radius, y + radius + 1)
                    for xx in range(x - radius, x + radius + 1)
                ):
                    return "unknown_via_blocker"
                if (
                    grid.worst_via_pad_deficit(
                        via, exclude_net=route.net, clearance_floor=rules.via_clearance
                    )[0]
                    > 1e-9
                ):
                    return "via_pad_clearance"
                others = drills + [
                    (v.x, v.y, v.drill) for r in pair for v in r.vias if v is not via
                ]
                if not drill_hole_to_hole_clear(
                    via.x, via.y, via.drill, others, rules.min_hole_to_hole
                ):
                    return "drill_clearance"
    return "deadline" if time.monotonic() >= deadline else None
