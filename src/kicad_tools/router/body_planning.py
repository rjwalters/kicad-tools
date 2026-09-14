"""Uncommitted pair bodies expressed in a validated departure's local frame."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordinated_tuning import coordinated_pair_loop
from .layers import Layer
from .primitives import Route, Segment

if TYPE_CHECKING:
    from .departure_planning import ValidatedDeparture
    from .diffpair_routing import CoupledPathfinder
    from .primitives import Pad


@dataclass(frozen=True)
class PairBody:
    p_route: Route
    n_route: Route
    p_head: tuple[int, int]
    n_head: tuple[int, int]


def construct_pair_body(
    finder: CoupledPathfinder,
    departure: ValidatedDeparture,
    pads: tuple[Pad, Pad, Pad, Pad],
    *,
    depth_cells: int,
    retreat_cells: int,
    goal_offset_cells: int | None = None,
    added_coupled_length: float = 0.0,
) -> PairBody | None:
    """Extend a prefix with a corner or staggered turn toward the goal.

    This function proposes geometry only. It does not certify keepouts,
    clearance, connectivity, coupling or skew; the assembled-pair validator
    and length/coupling gates remain mandatory before committing anything.
    ``goal_offset_cells`` selects a final turn parallel to the escape axis;
    ``None`` leaves the two heads on the long coupled run.
    """
    if (
        depth_cells < 0
        or retreat_cells <= 0
        or not math.isfinite(added_coupled_length)
        or added_coupled_length < 0
    ):
        return None
    grid = finder.grid
    proposal = departure.proposal
    if not proposal.prefix:
        return None
    root = grid.world_to_grid(pads[0].x, pads[0].y)
    goal = grid.world_to_grid(pads[1].x, pads[1].y)
    across, outward = proposal.across, proposal.outward
    if (
        sum(v * v for v in across) != 1
        or sum(v * v for v in outward) != 1
        or sum(a * b for a, b in zip(across, outward, strict=True)) != 0
    ):
        return None
    sign = 1 if sum((g - r) * a for g, r, a in zip(goal, root, across, strict=True)) >= 0 else -1
    forward = (sign * across[0], sign * across[1])

    def local(point):
        dx, dy = point[0] - root[0], point[1] - root[1]
        return dx * forward[0] + dy * forward[1], dx * outward[0] + dy * outward[1]

    def global_point(point):
        x, y = point
        return root[0] + x * forward[0] + y * outward[0], root[1] + x * forward[1] + y * outward[1]

    px, py, pl, nx, ny, nl = proposal.prefix[-1]
    if pl != nl or pl != proposal.layer:
        return None
    p_head, n_head = local((px, py)), local((nx, ny))
    if p_head[1] != n_head[1]:
        return None
    nc = finder.net_class_map.get(pads[0].net_name) if finder.net_class_map else None
    clearance = nc.effective_intra_pair_clearance() if nc else finder.rules.trace_clearance
    width = max(finder._get_trace_width_for_net(p.net_name) for p in (pads[0], pads[2]))
    spacing = max(
        finder.min_spacing_cells,
        math.ceil((width + max(clearance, finder.rules.trace_clearance)) / grid.resolution),
    )
    gx, gy = local(goal)
    x = gx - retreat_cells
    y = p_head[1] + depth_cells
    if x <= max(p_head[0], n_head[0]):
        return None
    p_points = [p_head, (p_head[0], y + spacing), (x, y + spacing)]
    n_points = [n_head, (n_head[0], y), (x, y)]
    if goal_offset_cells is not None:
        p_points[-1] = (x + spacing, y + spacing)
        p_points.append((x + spacing, gy + goal_offset_cells))
        n_points.append((x, gy + goal_offset_cells))
    routes = copy.deepcopy([departure.p_route, departure.n_route])
    hosts = []
    layer = Layer(grid.index_to_layer(proposal.layer))
    for route, points in zip(routes, (p_points, n_points), strict=True):
        host = None
        for index, (a, b) in enumerate(zip(points[:-1], points[1:], strict=True)):
            if a == b:
                continue
            start, end = grid.grid_to_world(*global_point(a)), grid.grid_to_world(*global_point(b))
            segment = Segment(
                x1=start[0],
                y1=start[1],
                x2=end[0],
                y2=end[1],
                width=finder._get_trace_width_for_net(route.net_name),
                layer=layer,
                net=route.net,
                net_name=route.net_name,
            )
            if index == 1:
                host = len(route.segments)
            route.segments.append(segment)
        hosts.append(host)
    if added_coupled_length:
        if any(host is None for host in hosts):
            return None
        p_host, n_host = (route.segments[host] for route, host in zip(routes, hosts, strict=True))
        span = min(math.dist(p_host.start, p_host.end), math.dist(n_host.start, n_host.end))
        window = min(4.0, span / 3)
        loops = coordinated_pair_loop(
            p_host,
            n_host,
            added_length=added_coupled_length,
            window_start=window,
            window_end=2 * window,
        )
        if loops is None:
            return None
        for route, host, segments in zip(routes, hosts, loops, strict=True):
            route.segments[host : host + 1] = segments
    return PairBody(routes[0], routes[1], global_point(p_points[-1]), global_point(n_points[-1]))
