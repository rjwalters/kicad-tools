"""One bounded ledger that turns pad geometry into a qualified coupled pair.

This is the orchestration layer over the already-tested construction stages:
native-validated departures (:mod:`departure_planning`), mutually clear goal
barrels (:mod:`terminal_planning`) and the bounded body/terminal search
(:mod:`body_search`).  It owns no geometry decisions of its own -- its whole
job is to spend ONE caller-supplied deadline, native-iteration allowance and
body-attempt allowance across those stages so that no single stage can starve
the others, and to never hand back anything the stages did not fully qualify.

Nothing here commits occupancy, mutates the caller's routes or renews a
budget: the returned pair is still an uncommitted proposal that has passed the
assembled-geometry, physical-skew and authored-coupling gates.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .body_search import BodySearchBudget, complete_departures
from .departure_planning import DepartureBudget, validated_departures
from .terminal_planning import landing_proposals

if TYPE_CHECKING:
    from .departure_planning import ValidatedDeparture
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter
    from .primitives import Pad, Route


@dataclass
class ConstructionBudget:
    """The pair's entire construction allowance, charged by every stage.

    ``deadline`` is an absolute :func:`time.monotonic` instant owned by the
    caller -- construction never extends it.  ``iterations_remaining`` is the
    native validation allowance for departure proposals (each proposal costs
    only its own prefix length plus a small constant); ``bodies_remaining``
    caps the geometric shape attempts.  Both are debited in place so a caller
    can audit the true spend after the fact.
    """

    deadline: float
    iterations_remaining: int
    bodies_remaining: int
    iterations_used: int = 0
    bodies_used: int = 0


def construct_pair_routes(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    budget: ConstructionBudget,
    *,
    board_thickness_mm: float,
    num_copper_layers: int,
    reserved_routes: tuple[Route, ...] = (),
    max_departures: int = 6,
    max_landings: int = 4,
    max_bodies_per_departure: int = 32,
) -> tuple[Route, Route] | None:
    """Construct and qualify one coupled pair, or return ``None``.

    Departures are validated once and then shared by every landing plan that
    leaves the pads in the same direction, so the native allowance is spent at
    most once per escape shape.  Each landing plan receives a bounded slice of
    the remaining body attempts; a hopeless goal-side barrel choice therefore
    cannot consume the shape lattice of the next one.  Actual charges are
    debited from the parent ledger even when a stage raises.
    """
    if (
        time.monotonic() >= budget.deadline
        or budget.iterations_remaining <= 0
        or budget.bodies_remaining <= 0
        or max_departures <= 0
        or max_landings <= 0
    ):
        return None
    departures = _validated_departures(finder, pads, budget, max_departures)
    if not departures:
        return None
    for outward, group in _by_escape_direction(departures):
        if time.monotonic() >= budget.deadline or budget.bodies_remaining <= 0:
            return None
        landings = landing_proposals(
            router, finder, pads, outward=outward, deadline=budget.deadline
        )
        for landing in itertools.islice(landings, max_landings):
            if time.monotonic() >= budget.deadline or budget.bodies_remaining <= 0:
                return None
            portion = BodySearchBudget(
                budget.deadline,
                min(max_bodies_per_departure * len(group), budget.bodies_remaining),
            )
            try:
                result = complete_departures(
                    router,
                    finder,
                    pair,
                    pads,
                    group,
                    landing,
                    portion,
                    board_thickness_mm=board_thickness_mm,
                    num_copper_layers=num_copper_layers,
                    reserved_routes=reserved_routes,
                    max_bodies_per_departure=max_bodies_per_departure,
                )
            finally:
                budget.bodies_remaining -= portion.bodies_used
                budget.bodies_used += portion.bodies_used
            if result is not None:
                return result
    return None


def _validated_departures(
    finder: CoupledPathfinder,
    pads: tuple[Pad, Pad, Pad, Pad],
    budget: ConstructionBudget,
    limit: int,
) -> list[ValidatedDeparture]:
    """Spend the native allowance once, charging it even on an exception."""
    portion = DepartureBudget(budget.deadline, budget.iterations_remaining)
    try:
        return list(itertools.islice(validated_departures(finder, pads, portion), limit))
    finally:
        budget.iterations_remaining -= portion.iterations_used
        budget.iterations_used += portion.iterations_used


def _by_escape_direction(
    departures: list[ValidatedDeparture],
) -> list[tuple[tuple[int, int], list[ValidatedDeparture]]]:
    """Group by escape direction, keeping the proposal order of first use."""
    grouped: dict[tuple[int, int], list[ValidatedDeparture]] = {}
    for departure in departures:
        grouped.setdefault(departure.proposal.outward, []).append(departure)
    return list(grouped.items())
