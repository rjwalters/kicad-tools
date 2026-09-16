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
import math
import os
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .body_planning import PairBody
from .body_search import BodySearchBudget, complete_departures
from .departure_planning import DepartureBudget, validated_departures
from .pair_completion import WidenBudget, complete_pair_body, qualify_constructed_pair
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

# Issue #5333 (TMDS_D1): the fraction of the construction window that is held
# back for :func:`_corridor_guided_departures` when a corridor is available.
#
# The two body-finding stages share ONE caller-supplied wall-clock window, and
# before this reserve the FIRST of them (:func:`_geometric_body_search`) could
# spend all of it.  Measured on real Board07 (seed 42, native ABI 31, the
# regression fixture, ``--differential-pairs``): TMDS_D1 reported
# ``landings=1 bodies=139 completions=146 completion_reasons={'no_tail': 277,
# ...} widen_spent=40 corridor_attempts=0`` -- the lattice burned the pair's
# whole 60 s window on full-lattice tail widening (its widen allowance is
# exhausted, 40/40) and the corridor-guided native search, the stage that is
# the ONLY reason MIPI_DAT0 resolves at all (``corridor_attempts=2
# corridor_iters=181755``), never ran even once.  That is starvation, not a
# verdict: the corridor stage has its own, entirely separate iteration ledger
# (``corridor_iterations_remaining``) that was fully unspent.
#
# This is a max-min split of an UNCHANGED window, not a bigger budget: the
# pair's deadline, native departure allowance, body-attempt allowance and
# corridor-iteration allowance are all untouched, and a lattice that finishes
# (or succeeds) early still leaves the whole remainder to the corridor stage.
# It mirrors the split the top-level coupled search already applies between
# its own corridor and open phases (#3473, ``per_pair_max_iterations // 2``).
# When no corridor is supplied -- every pre-#5333 caller, and every board that
# does not run the escape-aware path -- the lattice keeps the entire window
# exactly as before.
CORRIDOR_WALL_RESERVE_FRACTION: float = float(
    os.environ.get("KCT_CONSTRUCTION_CORRIDOR_WALL_RESERVE", "0.5")
)
if not math.isfinite(CORRIDOR_WALL_RESERVE_FRACTION):
    raise ValueError("KCT_CONSTRUCTION_CORRIDOR_WALL_RESERVE must be finite")

# Issue #5333 follow-up (2026-09-15 session): the reserve above is correct
# and unit-verified (``test_a_failing_lattice_cannot_spend_the_whole_window_
# before_the_corridor_runs``) for what it controls -- how ``construct_pair_
# routes``'s OWN two internal stages split whatever window they are handed.
# Re-measuring TMDS_D1 live on real Board07 (seed 42, native ABI 31,
# ``test_tmds_d1_corridor_stage_is_invoked_in_a_live_search``, derived 60s
# per-pair wall) still shows ``corridor_attempts=0``, but the shape is
# DIFFERENT from the original b4c764ef measurement quoted above -- this is
# NOT the same code path re-starving:
#
#   [coupled-construction] success=False native_iters=220 proposals=15
#   departures=6 landings=1 bodies=2 built=2 geom_rejected=1 completions=2
#   completion_reasons={'no_tail': 3, 'deadline': 1}
#   departure_directions={'away_from_goal': '6/7', 'toward_goal': '0/8'}
#   corridor_attempts=0 corridor_iters=0 corridor_reasons={} widen_spent=4
#
# ``departures=6`` (native-validated, NOT the ``exact_geometry_departure``
# fallback -- that fallback unconditionally sets ``corridor=None``, a
# SEPARATE, previously-untested code path confirmed reachable this session,
# see ``test_corridor_is_disabled_when_departures_come_from_the_geometric_
# fallback`` in ``tests/test_pair_construction.py``; it is NOT what fired
# here). ``landings=1`` and a single ``deadline`` completion reason means
# the lattice stage hit its OWN sub-deadline after barely a single landing
# -- i.e. ``construct_pair_routes`` was handed only a sliver of wall-clock
# time in the first place, not that the reserve failed to split it fairly.
#
# That sliver is a consequence of what happens BEFORE ``construct_pair_
# routes`` is ever called: ``DiffPairRouter._route_pair`` (diffpair_
# routing.py) runs a corridor-guided ``pathfinder.route_coupled`` probe
# (up to ~half of ``per_pair_timeout``), then, if that fails, an "open"
# fallback ``pathfinder.route_coupled`` with NO corridor that is deliberately
# sized to spend the REST of ``per_pair_timeout`` (``remaining_budget =
# per_pair_timeout - elapsed``) before construction gets a turn at all --
# and ``construction_deadline`` is the SAME absolute instant those two
# stages were already targeting, not a fresh window.  On a fast/idle host
# the open fallback can exit well before its own deadline (native iteration
# budget exhausted first) and leave construction a real window; on a loaded
# host it runs closer to the wall, and construction inherits whatever is
# left -- which can be small enough that ONE landing consumes it before the
# already-fair 50/50 split ever gets to apply.  This is the SAME class of
# host-speed dependence already named for the two wall-clock cutoffs this
# issue's instructions forbid changing without a full-recipe cost study
# (#3089/#3321 per-pair, #3439 aggregate) -- it is not a new, independently
# fixable defect in this module, and no budget, deadline or allowance here
# was changed to make this note.  Next full-recipe budget-reconciliation
# session: the corridor-probe / open-fallback / construction split is a
# THIRD, currently sequential ("whatever's left") consumer of the same
# ``per_pair_timeout``, so any up-front reallocation across those three
# needs to preserve their combined total the way #5333's own scope requires.
#
# Issue #5333 follow-up (2026-09-15, second same-day session): the note
# above was written from SECONDARY signals (``widen_spent``, ``landings``,
# ``completion_reasons``) because ``diffpair_routing.py`` did not record the
# corridor-probe / open-fallback / construction split's actual wall-clock
# spend, only the pair's TOTAL elapsed time.  This session added that
# missing per-stage instrumentation (``corridor_search_s`` / ``open_
# fallback_s`` / ``construction_entry_window_s`` on the ``[coupled-timing]``
# line, plus regression coverage in
# ``test_coupled_timing_reports_per_stage_wall_clock`` /
# ``test_coupled_timing_reports_na_for_a_stage_that_never_ran`` in
# ``tests/test_diffpair_coupled_instrumentation_4459.py``) and re-ran the
# SAME committed fixture (seed 42, native ABI 31, full recipe search/timeout
# values, macOS/arm64, idle host) that produced the note above. Direct
# measurement now CONTRADICTS the window-starvation theory as TMDS_D1's
# universal cause -- on this run construction was NOT starved:
#
#   [coupled-construction] success=False native_iters=220 proposals=15
#   departures=6 landings=1 bodies=19 built=19 geom_rejected=7 completions=18
#   completion_reasons={'no_tail': 24, 'coupling_threshold': 15,
#   'self_overlap': 10, 'deadline': 2}
#   corridor_attempts=6 corridor_iters=529184 widen_spent=25
#   [coupled-timing] ... corridor_search_s=0.43s open_fallback_s=0.29s
#   construction_entry_window_s=51.89s
#
# Construction was handed 51.89 of the pair's ~53.5s total window (the
# corridor probe and open fallback together spent only 0.72s) -- the
# opposite of the "one landing exhausts a sliver" shape quoted above -- and
# still failed: 19 candidate bodies built, 18 terminal completions
# attempted, EVERY one rejected on a geometry/quality gate (``no_tail``,
# ``coupling_threshold``, ``self_overlap``), only exhausting the ample
# window after that (``deadline: 2``). So TMDS_D1 has (at least) TWO
# distinct failure shapes depending on host speed/load, not one:
# window-starved construction (prior note, this session's other measurement
# reproduced it too on a busier moment) AND ample-window construction that
# still cannot pass its own completion gates. A full-recipe budget
# reallocation (the lever named above) can only ever address the FIRST
# shape; it would not by itself fix TMDS_D1 on a host exhibiting the
# second. The next session should treat these as two separate defects: (1)
# the already-diagnosed upstream window-entry starvation, gated on the
# full-recipe budget study #5333 requires, and (2) why 18/18 completion
# attempts fail ``no_tail``/``coupling_threshold``/``self_overlap`` even with
# an ample window -- likely a ``pair_construction.py`` body/tail-generation
# quality question, unexplored this session. No budget, deadline, allowance
# or construction/completion logic was changed to record this measurement.


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
    supplying a corridor may cut short a lattice search that would otherwise
    succeed later in the full window; the reserved time lets the corridor
    stage try a different construction instead.

    When a corridor IS supplied, the lattice is additionally bounded by a
    SUB-deadline -- see :data:`CORRIDOR_WALL_RESERVE_FRACTION` -- so that a
    lattice which is already failing cannot spend the whole window and starve
    the corridor stage of its turn (#5333, TMDS_D1).  Nothing about the pair's
    own deadline or any of the three allowances changes; only the point at
    which the FIRST of two body-finding stages must hand over.
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
    if not departures and time.monotonic() < budget.deadline:
        from .geometric_departure import geometric_departures

        departures = list(
            itertools.islice(
                geometric_departures(
                    router,
                    finder,
                    pads,
                    deadline=budget.deadline,
                    reserved_routes=reserved_routes,
                ),
                max_departures,
            )
        )
        # These proposals have exact physical validation, not a validated
        # native prefix. Their bodies use the existing geometric constructor.
        # The native corridor would reject the same conservative halo again.
        if departures:
            corridor = None
            budget.departure_reasons["exact_geometry_departure"] += len(departures)
    budget.departures_found = len(departures)
    if not departures:
        return None
    # Keep one existing body allowance per native departure for completion
    # of its saved corridor path. This partitions the original cap; it never
    # grants fresh body attempts after a failed shape lattice.
    reserve = (
        min(len(departures), budget.bodies_remaining)
        if corridor is not None and budget.corridor_iterations_remaining > 0
        else 0
    )
    budget.bodies_remaining -= reserve
    try:
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
            deadline=_lattice_deadline(budget, corridor),
        )
    finally:
        budget.bodies_remaining += reserve
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
    deadline: float | None = None,
) -> tuple[Route, Route] | None:
    """The pre-existing fixed synthetic depth/retreat/offset/loop lattice.

    ``deadline`` bounds THIS stage only and defaults to the pair's own
    ``budget.deadline``.  A caller that still has a later stage to run (the
    corridor-guided native search) passes an earlier sub-deadline so this
    stage cannot consume the whole window -- see
    :data:`CORRIDOR_WALL_RESERVE_FRACTION`.  It is only ever earlier than
    ``budget.deadline``, never later, so this can never extend a budget.
    """
    if deadline is None or deadline > budget.deadline:
        deadline = budget.deadline
    # Offer each direction its first landing before revisiting an obstructed
    # direction. Four departures can otherwise spend the entire 400-body
    # allowance across four landings before another direction gets a turn.
    # Landing generators remain lazy; all original caps and ledger debits apply.
    pending = deque(
        (
            group,
            iter(
                itertools.islice(
                    landing_proposals(router, finder, pads, outward=outward, deadline=deadline),
                    max_landings,
                )
            ),
        )
        for outward, group in _by_escape_direction(departures)
    )
    while pending:
        if time.monotonic() >= deadline or budget.bodies_remaining <= 0:
            return None
        group, landings = pending.popleft()
        try:
            landing = next(landings)
        except StopIteration:
            continue
        budget.landings_found += 1
        if time.monotonic() >= deadline or budget.bodies_remaining <= 0:
            return None
        portion = BodySearchBudget(
            deadline,
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
        pending.append((group, landings))
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

    Each attempt's OWN ``max_iterations_budget`` is a fair (max-min) share of
    whatever remains -- ``corridor_iterations_remaining // departures still
    to try`` -- not the full remaining allowance (#5333, MIPI_DAT0/TMDS_D1/
    TMDS_D2 re-measurement).  Measured on real Board07 (seed 42, native ABI
    31) with the prior "each attempt gets the full remaining allowance"
    policy: MIPI_DAT0 (6 validated departures) reached only 2
    ``corridor_attempts`` before the ledger emptied, and TMDS_D1/TMDS_D2/
    MIPI_DAT1 each reached exactly 1 -- the first (or first two) departures
    consumed the *entire* 150,000-iteration allowance on their own search,
    leaving every other validated escape direction/shape completely
    untried.  A departure that converges well under its share still lets its
    UNUSED iterations roll forward (the divisor shrinks by one and the
    remaining pool is re-split on every subsequent attempt), so a fast
    departure costs the later ones nothing -- this is strictly a
    starvation fix, not a smaller aggregate search: the total ledger spent
    across all departures cannot exceed ``budget.corridor_iterations_
    remaining`` either way.

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
    departures = [d for d in departures if getattr(d, "native_validated", True)]
    departures_remaining = len(departures)
    for departure in departures:
        if time.monotonic() >= budget.deadline or budget.corridor_iterations_remaining <= 0:
            return None
        remaining_time = budget.deadline - time.monotonic()
        # Max-min fair share of whatever is left, re-split on every attempt
        # so a departure that converges under its share leaves the surplus
        # for the ones still to come -- see the docstring's Board07
        # measurement for why "give every attempt the full remaining
        # allowance" starved every departure but the first one or two.
        allowance = max(1, budget.corridor_iterations_remaining // departures_remaining)
        departures_remaining -= 1
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
        # A failed joint search can already have a physical body underneath
        # the destination pads but lack the terminal layer returns. Complete
        # that actual native geometry with the existing two-tail constructor.
        # Give each remaining departure a fair share of the same deadline.
        completion_deadline = time.monotonic() + max(
            0.0, (budget.deadline - time.monotonic()) / (departures_remaining + 1)
        )
        completed = _complete_corridor_partial(
            router,
            finder,
            pair,
            pads,
            budget,
            deadline=min(budget.deadline, completion_deadline),
            board_thickness_mm=board_thickness_mm,
            num_copper_layers=num_copper_layers,
            reserved_routes=reserved_routes,
        )
        if completed is not None:
            return completed
        if _CORRIDOR_DEBUG:
            print(f"    [corridor-construction-debug] {dict(finder.last_rejections)}", flush=True)
    return None


def _complete_corridor_partial(
    router,
    finder,
    pair,
    pads,
    budget,
    *,
    deadline,
    board_thickness_mm,
    num_copper_layers,
    reserved_routes=(),
) -> tuple[Route, Route] | None:
    """Complete only this attempt's saved native body, charging shared ledgers."""
    deadline = min(deadline, budget.deadline)
    path = getattr(finder, "last_best_cpp_path", ())
    if not path or budget.bodies_remaining <= 0 or time.monotonic() >= deadline:
        return None
    if getattr(finder, "_cpp_reconstruct_pads", None) != pads:
        return None
    grid = finder.grid
    root = (
        *grid.world_to_grid(pads[0].x, pads[0].y),
        grid.layer_to_index(pads[0].layer.value),
        *grid.world_to_grid(pads[2].x, pads[2].y),
        grid.layer_to_index(pads[2].layer.value),
    )
    if tuple(path[0][:6]) != root:
        return None
    # Charge before reconstruction/completion, including exceptions and
    # rejected candidates. Never recycle a failed attempt's body allowance.
    budget.bodies_remaining -= 1
    budget.bodies_used += 1
    routes = finder._reconstruct_coupled_routes_from_cpp_path(path, partial=True)
    end = path[-1]
    if any(
        not route.segments or grid.layer_to_index(route.segments[-1].layer.value) != end[offset + 2]
        for route, offset in zip(routes, (0, 3), strict=True)
    ):
        budget.corridor_reasons["partial_head_layer"] += 1
        return None
    budget.bodies_built += 1
    budget.completions_tried += 1
    result = complete_pair_body(
        router,
        finder,
        pair,
        pads,
        PairBody(routes[0], routes[1], tuple(end[:2]), tuple(end[3:5])),
        deadline=min(deadline, budget.deadline),
        board_thickness_mm=board_thickness_mm,
        num_copper_layers=num_copper_layers,
        reserved_routes=reserved_routes,
        reasons=budget.completion_reasons,
        widen_budget=budget.widen_budget,
    )
    budget.corridor_reasons["partial_completed" if result else "partial_rejected"] += 1
    return result


def _lattice_deadline(
    budget: ConstructionBudget, corridor: frozenset[tuple[int, int]] | None
) -> float:
    """The instant :func:`_geometric_body_search` must hand over by.

    Returns ``budget.deadline`` unchanged unless a corridor-guided stage can
    actually run afterwards -- i.e. a corridor was supplied AND its own
    (separate) iteration allowance is nonzero.  Otherwise the lattice is the
    last stage there is, and holding time back from it would waste the window
    rather than share it.

    The reserve is taken from the time REMAINING now, after departure
    validation has already been charged, so the lattice always gets a real
    share of whatever the earlier stages left rather than a share of a window
    that is already gone.
    """
    if corridor is None or budget.corridor_iterations_remaining <= 0:
        return budget.deadline
    fraction = min(max(CORRIDOR_WALL_RESERVE_FRACTION, 0.0), 1.0)
    if fraction <= 0.0:
        return budget.deadline
    now = time.monotonic()
    remaining = budget.deadline - now
    if remaining <= 0:
        return budget.deadline
    return now + remaining * (1.0 - fraction)


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
