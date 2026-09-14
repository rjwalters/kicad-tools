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
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .body_search import BodySearchBudget, complete_departures
from .departure_planning import DepartureBudget, validated_departures
from .pair_completion import WidenBudget, qualify_constructed_pair
from .terminal_planning import landing_proposals

if TYPE_CHECKING:
    from .departure_planning import ValidatedDeparture
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter
    from .primitives import Pad, Route

# Issue #5333: opt-in diagnostic for ``_corridor_guided_departures`` -- when
# set, print the FULL native rejection histogram for every corridor-guided
# attempt (not just the summarized ``_corridor_stall_reason`` token) so a
# stuck pair's dominant blocker is visible without a separate replay.  Off by
# default; matches this module's existing convention for measurement-only
# knobs (mirrors ``diffpair_routing._SHADOW_DEBUG`` / ``_CROSSTAIL_CENSUS``).
# Diagnostic only -- never changes which route ships.
_CORRIDOR_DEBUG: bool = os.environ.get("KCT_CORRIDOR_CONSTRUCTION_DEBUG", "0") == "1"


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

    ``corridor_iterations_remaining`` is a SEPARATE native allowance (default
    0, meaning the stage never runs unless a caller explicitly spends it) for
    :func:`_corridor_guided_departures` -- a native, corridor-bounded search
    seeded past each validated departure escape, tried only after the
    geometric shape lattice has exhausted every direction it found (#5333,
    MIPI_DAT0/TMDS_D2: the fixed synthetic depth/retreat/offset/loop lattice
    never samples the corridor the single-ended guide already proved
    reachable).  It never shares budget with ``iterations_remaining``
    (departure-prefix validation) or ``bodies_remaining`` (the shape
    lattice), so spending it can never starve either existing stage.
    ``corridor_attempts`` / ``corridor_reasons`` are the matching diagnostic
    tally, never an input.
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
    corridor_iterations_remaining: int = 0
    corridor_iterations_used: int = 0
    corridor_attempts: int = 0
    corridor_reasons: Counter[str] = field(default_factory=Counter)

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
        corridor_reasons = dict(
            sorted(self.corridor_reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        )
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
            f"corridor_attempts={self.corridor_attempts} "
            f"corridor_iters={self.corridor_iterations_used} "
            f"corridor_reasons={corridor_reasons} "
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
    corridor: frozenset[tuple[int, int]] | None = None,
) -> tuple[Route, Route] | None:
    """Construct and qualify one coupled pair, or return ``None``.

    Departures are validated once and then shared by every landing plan that
    leaves the pads in the same direction, so the native allowance is spent at
    most once per escape shape.  Each landing plan receives a bounded slice of
    the remaining body attempts; a hopeless goal-side barrel choice therefore
    cannot consume the shape lattice of the next one.  Actual charges are
    debited from the parent ledger even when a stage raises.

    ``corridor``, if supplied, is tried as a LAST resort (#5333) -- a native,
    corridor-bounded joint search seeded past each already-validated
    departure escape -- only once the fixed geometric shape lattice has been
    tried for every escape direction and still returned nothing.  A pair that
    already succeeds through the geometric lattice never reaches it, so
    supplying a corridor cannot change the outcome for a pair that already
    routes.
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
    result = _geometric_body_search(
        router,
        finder,
        pair,
        pads,
        departures,
        budget,
        board_thickness_mm=board_thickness_mm,
        num_copper_layers=num_copper_layers,
        reserved_routes=reserved_routes,
        max_landings=max_landings,
        max_bodies_per_departure=max_bodies_per_departure,
    )
    if result is not None:
        return result
    if corridor is not None:
        return _corridor_guided_departures(
            router,
            finder,
            pair,
            pads,
            departures,
            budget,
            corridor,
            board_thickness_mm=board_thickness_mm,
            num_copper_layers=num_copper_layers,
            reserved_routes=reserved_routes,
        )
    return None


def _geometric_body_search(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    departures: list[ValidatedDeparture],
    budget: ConstructionBudget,
    *,
    board_thickness_mm: float,
    num_copper_layers: int,
    reserved_routes: tuple[Route, ...],
    max_landings: int,
    max_bodies_per_departure: int,
) -> tuple[Route, Route] | None:
    """The pre-existing fixed synthetic depth/retreat/offset/loop lattice."""
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


def _corridor_guided_departures(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    departures: list[ValidatedDeparture],
    budget: ConstructionBudget,
    corridor: frozenset[tuple[int, int]],
    *,
    board_thickness_mm: float,
    num_copper_layers: int,
    reserved_routes: tuple[Route, ...],
) -> tuple[Route, Route] | None:
    """Ask the real native search to finish the route, corridor-bounded.

    The geometric shape lattice (:func:`_geometric_body_search`) only ever
    tries a FIXED synthetic depth/retreat/offset/loop shape past a validated
    departure; on MIPI_DAT0/TMDS_D2 (#5333) every combination of that shape
    collides with the same foreign-pad halo -- the lattice is shape-
    invariant, not budget-starved, so more attempts of the same shapes
    cannot help.  The single-ended guide corridor is independently proven
    reachable (``guide_ok=True``) but was never itself searched for a body.

    This tries the ordinary native joint A* (the SAME search the top-level
    corridor attempt already ran, unmodified) but seeded past each already
    native-validated departure escape via ``departure_prefix`` -- so it never
    re-explores the near-pad joint-state combinatorics ("joint-A*-plateau")
    that stalled the pair's own earlier, unconstrained corridor attempt. Only
    the escape's FIRST required steps are forced; every step after that is
    an ordinary corridor-bounded native move subject to every existing
    copper/clearance/trail/via guard, so a route this returns is exactly as
    legal as one the top-level corridor search would have produced on its
    own -- no guard is relaxed or bypassed.

    Charged against ``budget.corridor_iterations_remaining``, a ledger
    entirely separate from the departure-validation and body-lattice
    allowances, so spending it can never starve either existing stage.  One
    attempt is made per validated departure (bounded by the caller's
    ``max_departures``), stopping as soon as one succeeds or the deadline /
    allowance is exhausted.

    A route the native search finds is only "exactly as legal" as the
    top-level corridor search's own output at the copper/clearance/trail/via
    guard level -- it is NOT run through :func:`pair_completion.
    complete_pair_body`'s tail-completion loop, so it never gets that loop's
    ``tune_diff_pair_skew`` + authored skew-tolerance + coupled-continuity
    checks for free.  Unlike the geometric shape lattice's deliberately
    symmetric moves, a departure-prefix-seeded joint search is not
    guaranteed to keep the two legs length-matched by construction.  So
    every native result is run through :func:`pair_completion.
    qualify_constructed_pair` -- the SAME authored-skew/coupled-continuity/
    post-tune-geometry gate :func:`complete_pair_body` applies to every
    geometric-lattice candidate -- before it can be returned; a route that
    fails that gate is discarded and the next validated departure is tried
    instead of the pair being reported as qualified.

    Measured on real Board07 (seed 42, native ABI 31): an EARLIER, now-
    corrected version of this function returned the raw native result
    directly, without any qualification step at all.  For MIPI_DAT0, that
    raw route reached a full, geometrically legal coupled route in ~10s
    (122,839 native iterations) and was reported as ``coupled-ok`` -- but
    its actual P/N physical length differed by 1.75mm against the net
    class's 0.05mm authored skew tolerance, 35x over, silently downgraded
    by an unrelated downstream serpentine-repair path to a mere
    ``LengthMismatchWarning`` instead of being rejected. That was a real
    quality-gate bypass, not a resolved pair: :func:`complete_pair_body`'s
    geometric-lattice candidates are run through ``tune_diff_pair_skew`` and
    a hard skew/coupling check before they can qualify, and the corridor
    path was not.

    With ``qualify_constructed_pair`` now applied (this function, current
    form), re-measuring on the same board/seed shows MIPI_DAT0 does NOT
    reliably resolve after all: its first validated departure's
    corridor-guided native result is found (fast, well inside budget) but
    rejected by the authored coupled-continuity gate
    (``corridor_reasons={'coupling_threshold': 1, ...}``, reproduced
    identically across repeated runs at fixed seed), and its remaining
    departures exhaust the 150,000-iteration allowance without a second
    candidate. TMDS_D2 was already unresolved before this fix (still
    shape-invariant at the geometric-lattice stage, and the corridor search
    itself exhausts its own frontier before reaching a candidate to
    qualify) and remains so. TMDS_D1 is unaffected either way (still
    site-scarce at the terminal stage). So this session's net effect on
    reach is zero -- the pre-existing 3/7 baseline (DQS, MIPI_CLK, TMDS_D0)
    is unchanged -- but it closes a real correctness gap: a raw corridor
    result reaching the goal quickly is evidence the SHAPE is reachable, not
    evidence the resulting PAIR is qualified, and this function no longer
    conflates the two. The corridor-guided native search finds legal copper
    fast; it has no bias toward a length-matched path, so passing its
    qualification gate is evidently rarer than reaching the goal at all --
    a genuinely different (and, on this evidence, harder) problem than
    "find *a* legal route," which is the next named gap for whichever
    session picks this up: either bias the native search itself toward
    symmetric/length-matched paths, or spend enough corridor-iteration
    budget across enough departures to raise the odds of landing on one
    that also happens to qualify.
    """
    # ``qualify_constructed_pair`` derives the net class (and, from it, the
    # intra-pair clearance and skew/coupling tolerances) itself, so a pair
    # with no configured net class or invalid board thickness is rejected
    # there -- this function does not need its own copy of that check.
    reserved_routes = tuple(r for r in reserved_routes if r.net not in (pads[0].net, pads[2].net))
    for departure in departures:
        if time.monotonic() >= budget.deadline or budget.corridor_iterations_remaining <= 0:
            return None
        remaining_time = budget.deadline - time.monotonic()
        allowance = budget.corridor_iterations_remaining
        result = finder.route_coupled(
            *pads,
            departure_prefix=list(departure.proposal.prefix),
            timeout_seconds=remaining_time,
            max_iterations_budget=allowance,
            corridor=corridor,
        )
        used = finder.last_iterations
        budget.corridor_iterations_used += used
        budget.corridor_iterations_remaining -= used
        budget.corridor_attempts += 1
        if result is not None:
            qualified = qualify_constructed_pair(
                router,
                finder,
                pair,
                pads,
                result,
                board_thickness_mm=board_thickness_mm,
                num_copper_layers=num_copper_layers,
                deadline=budget.deadline,
                reserved_routes=reserved_routes,
                reasons=budget.corridor_reasons,
            )
            if qualified is not None:
                return qualified
            if time.monotonic() >= budget.deadline:
                return None
            continue
        budget.corridor_reasons[_corridor_stall_reason(finder)] += 1
        if _CORRIDOR_DEBUG:
            print(f"    [corridor-construction-debug] {dict(finder.last_rejections)}", flush=True)
    return None


def _corridor_stall_reason(finder: CoupledPathfinder) -> str:
    """Name why one corridor-guided native attempt did not reach the goal."""
    progress = finder.last_best_progress
    progress_label = "inf" if progress is None or progress == float("inf") else int(progress)
    if finder.last_iteration_limited:
        return f"iteration_limited_progress_{progress_label}"
    if finder.last_timeout_exceeded:
        return f"deadline_progress_{progress_label}"
    return f"exhausted_progress_{progress_label}"


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
