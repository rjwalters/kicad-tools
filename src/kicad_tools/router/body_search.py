"""Bounded geometric body selection for a native-validated departure."""

from __future__ import annotations

import itertools
import math
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
    """One body-attempt allowance plus the stage tally of how it was spent.

    ``bodies_used`` is the charged allowance; the three counters below split
    that charge into the stage that consumed it, so a caller can tell an
    exhausted shape lattice (``bodies_built`` high, ``completions_tried``
    high) apart from a body constructor that never produced geometry at all
    (``bodies_built`` zero) or one whose geometry always collided with
    committed/reserved copper (``bodies_geometry_rejected`` high).  The
    counters are diagnostic only: nothing reads them to make a decision.

    ``geometry_reasons`` histograms the short reason token
    :func:`constructed_pair_geometry_issue` returns, which is what separates
    "this shape happens to collide" from "every shape fails the same check".

    ``completion_reasons`` is the SAME kind of histogram for the terminal
    stage (:func:`complete_pair_body`): a body can pass the pre-tuning
    geometry gate every time and still never qualify, and ``completions_tried``
    alone cannot say whether that is because no legal layer-return tail
    exists (``no_tail``), physical skew/coupling missed the authored
    threshold, or tuning itself reintroduced a collision.
    """

    deadline: float
    bodies_remaining: int
    bodies_used: int = 0
    bodies_built: int = 0
    bodies_geometry_rejected: int = 0
    completions_tried: int = 0
    geometry_reasons: Counter[str] = field(default_factory=Counter)
    completion_reasons: Counter[str] = field(default_factory=Counter)


def preference_ordered(*axes: Iterable[Any]) -> list[tuple[Any, ...]]:
    """Visit a shape lattice in ascending TOTAL preference rank.

    Each axis is supplied most-preferred value first.  ``itertools.product``
    is lexicographic, so a truncated prefix of it never varies the outermost
    axis: with the per-departure attempt cap (32) roughly 3% of this lattice
    (~900 combinations on a 0.127 mm grid), every single attempt shared one
    escape depth and the first two retreats, and no amount of allowance
    spent on a blocked pair ever reached a second escape depth.

    Ordering by the sum of per-axis ranks keeps the all-most-preferred
    combination first (so a pair that already succeeds on its first attempt
    still does), then spends each following attempt on a combination that
    differs from it as little as possible while spanning every axis.  Ties
    break on the rank tuple, so the order is total and deterministic.

    Measured on board 07 (seed 42, ``--differential-pairs``): the three pairs
    that construct successfully still do so at the SAME attempt counts (2, 1)
    and the same wall cost, and TMDS_D1 reaches the terminal stage on more of
    its bodies (262 -> 212 rejected of 400).  This is a coverage fix, not on
    its own a completion fix: MIPI_DAT0 and TMDS_D2 still reject all 400
    bodies, which is what shows their rejection is shape-INVARIANT and has to
    be diagnosed from the reason histogram rather than by sampling more
    shapes.
    """
    indexed = [list(enumerate(axis)) for axis in axes]
    ranked = sorted(
        itertools.product(*indexed),
        key=lambda combo: (sum(rank for rank, _ in combo), tuple(rank for rank, _ in combo)),
    )
    return [tuple(value for _, value in combo) for combo in ranked]


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
    for depth, retreat, offset, added in preference_ordered(depths, retreats, offsets, loops):
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
        budget.bodies_built += 1
        heads = tuple(
            router._virtual_pad_at(pad, *grid.grid_to_world(*point), departure.proposal.layer)
            for pad, point in zip((pads[1], pads[3]), (body.p_head, body.n_head), strict=True)
        )
        issue = constructed_pair_geometry_issue(
            router,
            finder,
            body.p_route,
            body.n_route,
            (pads[0], heads[0], pads[2], heads[1]),
            intra_pair_clearance=nc.effective_intra_pair_clearance(),
            deadline=budget.deadline,
            reserved_routes=reserved_routes,
        )
        if issue is not None:
            budget.bodies_geometry_rejected += 1
            budget.geometry_reasons[issue] += 1
            continue
        for shortest in (False, True):
            if time.monotonic() >= budget.deadline:
                return None
            budget.completions_tried += 1
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
                reasons=budget.completion_reasons,
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
            budget.bodies_built += portion.bodies_built
            budget.bodies_geometry_rejected += portion.bodies_geometry_rejected
            budget.completions_tried += portion.completions_tried
            budget.geometry_reasons.update(portion.geometry_reasons)
            budget.completion_reasons.update(portion.completion_reasons)
        if result is not None:
            return result
    return None
