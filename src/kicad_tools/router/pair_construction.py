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
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .body_search import BodySearchBudget, complete_departures
from .departure_planning import DepartureBudget, validated_departures
from .pair_completion import WidenBudget
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

    The trailing counters are a diagnostic tally, never an input: they record
    which stage consumed the allowance so a failed construction can be
    classified without re-running it.  ``departures_found`` is the number of
    natively validated escapes, ``landings_found`` the number of mutually
    clear goal-barrel plans offered across those escape directions, and the
    three body counters split ``bodies_used`` into geometry that was built,
    geometry rejected against committed/reserved copper, and terminal
    completions attempted.  ``completion_reasons`` further splits those
    attempted completions -- a pair that reaches ``geom_rejected=0`` with a
    nonzero ``completions`` count still needs this to tell "no legal
    layer-return tail exists" apart from "every tail passes the geometry
    gate but misses the authored skew or coupling threshold" (#5333).

    ``departure_proposals_seen`` / ``departure_reasons`` /
    ``departure_rejections`` are the same split one stage EARLIER, for the
    only stage that previously had none: a pair reporting
    ``departures_found=0`` spent its native allowance proving something, and
    these say what.  ``departure_proposals_seen == 0`` means the geometry
    enumerator itself declined (its reason token is in ``departure_reasons``);
    otherwise every offered shape carries a token naming the required step it
    never got past, plus the native guard histogram from that attempt.

    ``departure_direction_seen`` / ``departure_direction_validated`` further
    split those two counts by :data:`departure_planning.DIRECTION_TOWARD_GOAL`
    / :data:`departure_planning.DIRECTION_AWAY_FROM_GOAL` (#5333, MIPI_DAT1
    re-measurement): every sibling pair that resolves exactly half its
    proposals resolves ALL of one direction and NONE of the other, while a
    pair failing in both directions is stuck a different way that fanning
    the escape depth does not reach.
    """

    deadline: float
    iterations_remaining: int
    bodies_remaining: int
    iterations_used: int = 0
    bodies_used: int = 0
    departure_proposals_seen: int = 0
    departures_found: int = 0
    landings_found: int = 0
    bodies_built: int = 0
    bodies_geometry_rejected: int = 0
    completions_tried: int = 0
    geometry_reasons: Counter[str] = field(default_factory=Counter)
    completion_reasons: Counter[str] = field(default_factory=Counter)
    departure_reasons: Counter[str] = field(default_factory=Counter)
    departure_rejections: Counter[str] = field(default_factory=Counter)
    departure_direction_seen: Counter[str] = field(default_factory=Counter)
    departure_direction_validated: Counter[str] = field(default_factory=Counter)
    widen_budget: WidenBudget = field(default_factory=WidenBudget)

    def stage_summary(self) -> str:
        """One-line tally of where this pair's construction allowance went."""
        reasons = dict(sorted(self.geometry_reasons.items(), key=lambda kv: (-kv[1], kv[0])))
        completion_reasons = dict(
            sorted(self.completion_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        departure_reasons = dict(
            sorted(self.departure_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        departure_rejections = dict(
            sorted(self.departure_rejections.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        departure_directions = {
            direction: f"{self.departure_direction_validated[direction]}/{seen}"
            for direction, seen in sorted(self.departure_direction_seen.items())
        }
        return (
            f"proposals={self.departure_proposals_seen} "
            f"departures={self.departures_found} "
            f"landings={self.landings_found} "
            f"bodies={self.bodies_used} "
            f"built={self.bodies_built} "
            f"geom_rejected={self.bodies_geometry_rejected} "
            f"completions={self.completions_tried} "
            f"geom_reasons={reasons} "
            f"completion_reasons={completion_reasons} "
            f"departure_reasons={departure_reasons} "
            f"departure_rejections={departure_rejections} "
            f"departure_directions={departure_directions} "
            f"widen_spent={self.widen_budget.spent}"
        )


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
    budget.departures_found = len(departures)
    if not departures:
        return None
    for outward, group in _by_escape_direction(departures):
        if time.monotonic() >= budget.deadline or budget.bodies_remaining <= 0:
            return None
        landings = landing_proposals(
            router, finder, pads, outward=outward, deadline=budget.deadline
        )
        for landing in itertools.islice(landings, max_landings):
            budget.landings_found += 1
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
                    widen_budget=budget.widen_budget,
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
        budget.departure_proposals_seen += portion.proposals_seen
        budget.departure_reasons.update(portion.reasons)
        budget.departure_rejections.update(portion.native_rejections)
        budget.departure_direction_seen.update(portion.direction_seen)
        budget.departure_direction_validated.update(portion.direction_validated)


def _by_escape_direction(
    departures: list[ValidatedDeparture],
) -> list[tuple[tuple[int, int], list[ValidatedDeparture]]]:
    """Group by escape direction, keeping the proposal order of first use."""
    grouped: dict[tuple[int, int], list[ValidatedDeparture]] = {}
    for departure in departures:
        grouped.setdefault(departure.proposal.outward, []).append(departure)
    return list(grouped.items())
