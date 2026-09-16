"""Connect candidate copper to existing vias before clearance validation."""

from __future__ import annotations

import math
from collections.abc import Callable

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


def reuse_same_net_vias(
    route: Route,
    existing_routes: list[Route],
    clearance: float,
    *,
    can_reuse: Callable[[Via, list[Segment]], bool] | None = None,
) -> None:
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
        widths = {}
        ambiguous_contact = False
        for segment in route.segments:
            if any(
                math.hypot(x - via.x, y - via.y) < 1e-6 for x, y in (segment.start, segment.end)
            ):
                widths[segment.layer] = max(widths.get(segment.layer, 0), segment.width)
            else:
                # A barrel may contact the middle of a segment. Removing it
                # must not silently detach that conductor; this proposal only
                # knows how to reconnect explicit center endpoints.
                dx, dy = segment.x2 - segment.x1, segment.y2 - segment.y1
                length2 = dx * dx + dy * dy
                t = (
                    0
                    if not length2
                    else max(
                        0, min(1, ((via.x - segment.x1) * dx + (via.y - segment.y1) * dy) / length2)
                    )
                )
                distance = math.hypot(via.x - segment.x1 - t * dx, via.y - segment.y1 - t * dy)
                if distance <= (via.diameter + segment.width) / 2:
                    ambiguous_contact = True
        if (
            ambiguous_contact
            or not set(via.layers).issubset(widths)
            or any(not other_lo <= layer.value <= other_hi for layer in widths)
        ):
            retained.append(via)
            continue
        points = dogleg_points(via.x, via.y, other.x, other.y)
        stubs = []
        for layer, width in widths.items():
            for start, end in zip(points, points[1:], strict=False):
                if start == end:
                    continue
                stubs.append(
                    Segment(*start, *end, width, layer, net=route.net, net_name=route.net_name)
                )
        if can_reuse is not None and not can_reuse(other, stubs):
            retained.append(via)
            continue
        route.segments.extend(stubs)
    route.vias = retained


def reuse_marked_vias(route: Route, grid, clearance: float) -> None:
    """Propose reuse only for marked barrels reachable without crossing barriers.

    Route lists alone are not occupancy provenance. The mark ledger rejects
    stale/unmarked barrels; current cells retain Kelvin isolation and static
    obstacles. Full physical and partner clearance validation is still required
    after this function, including every added dogleg.
    """
    marked = {key for (key, _), count in grid._route_halo.marks.items() if count > 0}

    def allowed(via: Via, stubs: list[Segment]) -> bool:
        if grid._route_halo.via_key(via) not in marked:
            return False
        cells = grid._get_via_cells(via)
        if any(
            not (0 <= x < grid.cols and 0 <= y < grid.rows)
            or not grid.cell_at(layer, y, x).blocked
            or grid.cell_at(layer, y, x).net != route.net
            for x, y, layer in cells
        ):
            return False
        for stub in stubs:
            cells.update(grid._get_segment_cells(stub))
        for x, y, layer in cells:
            if not (0 <= x < grid.cols and 0 <= y < grid.rows):
                return False
            cell = grid.cell_at(layer, y, x)
            if cell.is_obstacle or cell.pad_blocked:
                return False
            if cell.blocked and cell.net != route.net:
                return False
            if grid._static_blocked is not None and grid._static_blocked[layer, y, x]:
                return False
            reserved = grid._reserved_for_nets.get((layer, y, x))
            if reserved is not None:
                return False
        return True

    reuse_same_net_vias(route, grid.routes, clearance, can_reuse=allowed)
