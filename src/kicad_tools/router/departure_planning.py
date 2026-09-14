"""Bounded, native-validated departure proposals for geometric pair routing."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .diffpair_routing import CoupledPathfinder
    from .primitives import Pad, Route

JointStep = tuple[int, int, int, int, int, int]


@dataclass
class DepartureBudget:
    """The caller's shared search allowance, charged across every proposal."""

    deadline: float
    iterations_remaining: int
    iterations_used: int = 0


@dataclass(frozen=True)
class DepartureProposal:
    prefix: tuple[JointStep, ...]
    across: tuple[int, int]
    outward: tuple[int, int]
    layer: int
    bend_steps: int


@dataclass(frozen=True)
class ValidatedDeparture:
    proposal: DepartureProposal
    p_route: Route
    n_route: Route
    iterations: int


def departure_proposals(
    finder: CoupledPathfinder, pads: tuple[Pad, Pad, Pad, Pad]
) -> Iterator[DepartureProposal]:
    """Enumerate two escape directions/shapes on each other routable layer.

    Source pitch defines an axis-aligned local frame. Geometry never names a
    net or assumes a board origin; native validation decides which proposals
    can actually leave the pads without colliding with copper or their trail.
    """
    grid = finder.grid
    p_start, p_goal, n_start, n_goal = pads
    if p_start.layer != n_start.layer:
        return
    px, py = grid.world_to_grid(p_start.x, p_start.y)
    nx, ny = grid.world_to_grid(n_start.x, n_start.y)
    dx, dy = nx - px, ny - py
    if (dx == 0) == (dy == 0):
        return
    across = ((1 if dx > 0 else -1), 0) if dx else (0, (1 if dy > 0 else -1))
    directions = [(across[1], -across[0]), (-across[1], across[0])]
    travel = (
        p_goal.x + n_goal.x - p_start.x - n_start.x,
        p_goal.y + n_goal.y - p_start.y - n_start.y,
    )
    directions.sort(key=lambda d: -(d[0] * travel[0] + d[1] * travel[1]))
    start_layer = grid.layer_to_index(p_start.layer.value)
    # Keep the ordinary barrel outside its own pad. Foreign clearance is
    # deliberately not waived here; every native step still checks it.
    extent = max(p_start.height, n_start.height) if across[0] else max(p_start.width, n_start.width)
    escape = max(
        1, math.ceil(max(0.8, extent / 2 + finder.rules.via_diameter / 2) / grid.resolution)
    )
    width = max(finder._get_trace_width_for_net(p.net_name) for p in (p_start, n_start))
    bend = math.ceil(width / grid.resolution) + 1
    bends = (
        (0, bend)
        if bend < escape and abs(dx) + abs(dy) - bend >= finder.min_spacing_cells
        else (0,)
    )
    for layer in reversed(grid.get_routable_indices()):
        if layer == start_layer:
            continue
        for outward in directions:
            ox, oy = outward
            for bend_steps in bends:
                prefix = []
                if bend_steps:
                    prefix.extend(
                        (px, py, start_layer, nx - across[0] * j, ny - across[1] * j, start_layer)
                        for j in range(1, bend_steps + 1)
                    )
                    prefix.extend(
                        (
                            px + ox * j,
                            py + oy * j,
                            start_layer,
                            nx - across[0] * bend_steps + ox * j,
                            ny - across[1] * bend_steps + oy * j,
                            start_layer,
                        )
                        for j in range(1, bend_steps + 1)
                    )
                    prefix.extend(
                        (
                            px + ox * bend_steps,
                            py + oy * bend_steps,
                            start_layer,
                            nx - across[0] * (bend_steps - j) + ox * bend_steps,
                            ny - across[1] * (bend_steps - j) + oy * bend_steps,
                            start_layer,
                        )
                        for j in range(1, bend_steps + 1)
                    )
                prefix.extend(
                    (px + ox * j, py + oy * j, start_layer, nx + ox * j, ny + oy * j, start_layer)
                    for j in range(bend_steps + 1, escape + 1)
                )
                prefix.append(
                    (
                        px + ox * escape,
                        py + oy * escape,
                        layer,
                        nx + ox * escape,
                        ny + oy * escape,
                        layer,
                    )
                )
                yield DepartureProposal(tuple(prefix), across, outward, layer, bend_steps)


def validated_departures(
    finder: CoupledPathfinder,
    pads: tuple[Pad, Pad, Pad, Pad],
    budget: DepartureBudget,
) -> Iterator[ValidatedDeparture]:
    """Charge native attempts and yield only complete, verified prefixes.

    These are uncommitted route proposals. The caller must validate assembled
    geometry against the live board again before committing either half.
    """
    for proposal in departure_proposals(finder, pads):
        remaining_time = budget.deadline - time.monotonic()
        if remaining_time <= 0 or budget.iterations_remaining <= 0:
            return
        allowance = min(
            budget.iterations_remaining, len(proposal.prefix) + (6 if proposal.bend_steps else 4)
        )
        finder.route_coupled(
            *pads,
            departure_prefix=list(proposal.prefix),
            timeout_seconds=remaining_time,
            max_iterations_budget=allowance,
        )
        used = finder.last_iterations
        budget.iterations_used += used
        budget.iterations_remaining -= used
        if time.monotonic() >= budget.deadline or budget.iterations_remaining < 0:
            return
        if finder.last_rejections.get("departure_backend_unavailable"):
            return
        path = finder.last_validated_departure_path
        if (
            len(path) != len(proposal.prefix) + 1
            or tuple(tuple(step[:6]) for step in path[1:]) != proposal.prefix
        ):
            continue
        p_route, n_route = finder._reconstruct_coupled_routes_from_cpp_path(path, partial=True)
        if time.monotonic() >= budget.deadline:
            return
        yield ValidatedDeparture(proposal, p_route, n_route, used)
