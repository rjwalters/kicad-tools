"""Bounded geometric body selection for a native-validated departure."""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .body_planning import construct_pair_body
from .construction_validation import constructed_pair_geometry_issue
from .pair_completion import complete_pair_body

if TYPE_CHECKING:
    from .departure_planning import ValidatedDeparture
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter
    from .primitives import Pad, Route
    from .terminal_planning import PairLanding


@dataclass
class BodySearchBudget:
    deadline: float
    bodies_remaining: int
    bodies_used: int = 0


def complete_departure(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    departure: ValidatedDeparture,
    landing: PairLanding,
    budget: BodySearchBudget,
    *,
    board_thickness_mm: float,
    num_copper_layers: int,
    reserved_routes: tuple[Route, ...] = (),
) -> tuple[Route, Route] | None:
    """Try corner/turn/loop bodies under one caller-owned deadline and cap.

    The search contains no net-name or fixture-coordinate choices. Source
    and goal row orientation orders a fixed physical-size shape lattice.
    Each body is checked against actual copper and future reservations before
    both terminal approach orderings are tried. Acceptance still requires
    complete geometry, physical skew and the authored coupling threshold.
    """
    if time.monotonic() >= budget.deadline or budget.bodies_remaining <= 0:
        return None
    grid = finder.grid
    nc = finder.net_class_map.get(pads[0].net_name)
    if nc is None:
        return None
    across = departure.proposal.across
    goal_axis = (pads[3].x - pads[1].x, pads[3].y - pads[1].y)
    parallel = abs(goal_axis[0] * across[1] - goal_axis[1] * across[0]) < 1e-9
    resolution = grid.resolution
    depths = range(max(1, math.ceil(0.6 / resolution)), math.ceil(1.2 / resolution) + 1)
    retreats = list(
        range(
            max(1, math.ceil(0.5 / resolution)),
            math.ceil(4.6 / resolution) + 1,
            max(1, math.ceil(0.25 / resolution)),
        )
    )
    preferred = (1.5 if parallel else 3.0) / resolution
    retreats.sort(key=lambda cells: (abs(cells - preferred), cells))
    offsets = (None,) if parallel else (None, -1, 0)
    loops = (0.0, 4.0, 2 * math.ceil(4.0 / resolution) * resolution)
    reserved_routes = tuple(r for r in reserved_routes if r.net not in (pads[0].net, pads[2].net))
    for depth, retreat, offset, added in itertools.product(depths, retreats, offsets, loops):
        if time.monotonic() >= budget.deadline or budget.bodies_remaining <= 0:
            return None
        budget.bodies_remaining -= 1
        budget.bodies_used += 1
        body = construct_pair_body(
            finder,
            departure,
            pads,
            depth_cells=depth,
            retreat_cells=retreat,
            goal_offset_cells=offset,
            added_coupled_length=added,
        )
        if body is None:
            continue
        heads = tuple(
            router._virtual_pad_at(pad, *grid.grid_to_world(*point), departure.proposal.layer)
            for pad, point in zip((pads[1], pads[3]), (body.p_head, body.n_head), strict=True)
        )
        if (
            constructed_pair_geometry_issue(
                router,
                finder,
                body.p_route,
                body.n_route,
                (pads[0], heads[0], pads[2], heads[1]),
                intra_pair_clearance=nc.effective_intra_pair_clearance(),
                deadline=budget.deadline,
                reserved_routes=reserved_routes,
            )
            is not None
        ):
            continue
        for shortest in (False, True):
            if time.monotonic() >= budget.deadline:
                return None
            result = complete_pair_body(
                router,
                finder,
                pair,
                pads,
                body,
                deadline=budget.deadline,
                board_thickness_mm=board_thickness_mm,
                num_copper_layers=num_copper_layers,
                allowed_via_sites=landing.allowed_sites,
                prefer_shortest_approach=shortest,
                reserved_routes=reserved_routes,
            )
            if result is not None:
                return result
    return None


def complete_departures(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    departures: list[ValidatedDeparture],
    landing: PairLanding,
    budget: BodySearchBudget,
    *,
    board_thickness_mm: float,
    num_copper_layers: int,
    reserved_routes: tuple[Route, ...] = (),
    max_bodies_per_departure: int = 32,
) -> tuple[Route, Route] | None:
    """Share a body/time allowance across already validated layers and escapes.

    Each departure receives a bounded slice so an obstructed layer cannot
    consume every shape attempt before another layer is considered. Actual
    body charges are debited from the parent even if construction raises.
    Native departure validation has its own caller-owned iteration ledger.
    """
    if max_bodies_per_departure <= 0:
        return None
    for departure in departures:
        if time.monotonic() >= budget.deadline or budget.bodies_remaining <= 0:
            return None
        portion = BodySearchBudget(
            budget.deadline, min(max_bodies_per_departure, budget.bodies_remaining)
        )
        try:
            result = complete_departure(
                router,
                finder,
                pair,
                pads,
                departure,
                landing,
                portion,
                board_thickness_mm=board_thickness_mm,
                num_copper_layers=num_copper_layers,
                reserved_routes=reserved_routes,
            )
        finally:
            budget.bodies_remaining -= portion.bodies_used
            budget.bodies_used += portion.bodies_used
        if result is not None:
            return result
    return None
