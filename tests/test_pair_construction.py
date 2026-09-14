"""One ledger spends departures, landings and bodies for a pair (#5333)."""

import time
from types import SimpleNamespace

import pytest

from kicad_tools.router import pair_construction
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.match_group_length import MatchGroupTracker
from kicad_tools.router.pair_construction import ConstructionBudget, construct_pair_routes
from tests.test_coupled_layer_transition import fixture


def _departure(outward, layer, prefix=()):
    return SimpleNamespace(proposal=SimpleNamespace(outward=outward, layer=layer, prefix=prefix))


def _stub(monkeypatch, *, departures, landings, result=None, clock=None, qualify=None):
    """Replace every stage with a recording stub around the real ledger.

    ``qualify``, if supplied, replaces ``qualify_constructed_pair`` -- the
    authored skew/coupling/post-tune-geometry gate ``_corridor_guided_
    departures`` runs every native result through (#5333).  Defaults to an
    identity pass-through so tests that are only exercising ORCHESTRATION
    (which stage ran, how the ledger was spent) are unaffected by the gate;
    tests that specifically exercise the gate's reject-and-retry behavior
    supply their own.
    """
    calls = {"departures": [], "landings": [], "bodies": []}

    def fake_validated(finder, pads, budget):
        calls["departures"].append(budget.iterations_remaining)
        budget.iterations_used += 3
        budget.iterations_remaining -= 3
        yield from departures

    def fake_landings(router, finder, pads, *, outward, deadline):
        calls["landings"].append(outward)
        yield from landings

    def fake_complete(router, finder, pair, pads, group, landing, budget, **kwargs):
        calls["bodies"].append((len(group), budget.bodies_remaining))
        budget.bodies_used = budget.bodies_remaining
        budget.bodies_remaining = 0
        return result

    def fake_qualify(router, finder, pair, pads, candidate, **kwargs):
        return candidate

    monkeypatch.setattr(pair_construction, "validated_departures", fake_validated)
    monkeypatch.setattr(pair_construction, "landing_proposals", fake_landings)
    monkeypatch.setattr(pair_construction, "complete_departures", fake_complete)
    monkeypatch.setattr(pair_construction, "qualify_constructed_pair", qualify or fake_qualify)
    if clock is not None:
        monkeypatch.setattr(pair_construction, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return calls


def _run(budget, **kwargs):
    return construct_pair_routes(
        None,
        None,
        None,
        None,
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        **kwargs,
    )


def test_departures_are_validated_once_and_shared_per_escape_direction(monkeypatch):
    departures = [_departure((1, 0), 3), _departure((1, 0), 2), _departure((-1, 0), 3)]
    calls = _stub(monkeypatch, departures=departures, landings=["a", "b"], clock=[0])
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=100)
    assert _run(budget, max_landings=2, max_bodies_per_departure=8) is None
    # One native pass only, then two escape directions x two landings.
    assert calls["departures"] == [64]
    assert calls["landings"] == [(1, 0), (-1, 0)]
    # Each landing gets its own slice: departures-in-group x per-departure cap,
    # never the whole remaining allowance, so one hopeless goal-side barrel
    # choice cannot consume the lattice of the next.
    assert calls["bodies"] == [(2, 16), (2, 16), (1, 8), (1, 8)]
    assert budget.iterations_used == 3 and budget.iterations_remaining == 61
    assert budget.bodies_used == 48 and budget.bodies_remaining == 52


def test_first_complete_pair_stops_the_search(monkeypatch):
    routes = ("p", "n")
    calls = _stub(
        monkeypatch,
        departures=[_departure((1, 0), 3)],
        landings=["a", "b"],
        result=routes,
        clock=[0],
    )
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=50)
    assert _run(budget) is routes
    assert len(calls["bodies"]) == 1


@pytest.mark.parametrize(
    "budget",
    [
        ConstructionBudget(deadline=0, iterations_remaining=64, bodies_remaining=50),
        ConstructionBudget(deadline=1, iterations_remaining=0, bodies_remaining=50),
        ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=0),
    ],
)
def test_an_exhausted_ledger_attempts_nothing(monkeypatch, budget):
    calls = _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=["a"], clock=[0])
    assert _run(budget) is None
    assert calls == {"departures": [], "landings": [], "bodies": []}


def test_the_ledger_tallies_which_stage_spent_the_allowance(monkeypatch):
    """#5333: a failed pair has to name its failing stage without a re-run."""

    def fake_complete(router, finder, pair, pads, group, landing, budget, **kwargs):
        budget.bodies_used = budget.bodies_remaining
        budget.bodies_remaining = 0
        budget.bodies_built += 2
        budget.bodies_geometry_rejected += 1
        budget.completions_tried += 3
        budget.geometry_reasons["trace_clearance"] += 1
        return None

    _stub(monkeypatch, departures=[_departure((1, 0), 3), _departure((-1, 0), 3)], landings=["a"])
    monkeypatch.setattr(pair_construction, "complete_departures", fake_complete)
    monkeypatch.setattr(pair_construction, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    assert _run(budget, max_landings=1, max_bodies_per_departure=8) is None
    assert budget.departures_found == 2
    assert budget.landings_found == 2
    assert budget.bodies_built == 4
    assert budget.bodies_geometry_rejected == 2
    assert budget.completions_tried == 6
    assert budget.geometry_reasons == {"trace_clearance": 2}
    assert budget.stage_summary() == (
        "proposals=0 departures=2 landings=2 bodies=16 built=4 geom_rejected=2 completions=6 "
        "geom_reasons={'trace_clearance': 2} completion_reasons={} "
        "departure_reasons={} departure_rejections={} departure_directions={} "
        "corridor_attempts=0 corridor_iters=0 corridor_reasons={} widen_spent=0"
    )


def test_direction_tallies_propagate_from_the_departure_stage(monkeypatch):
    """#5333: MIPI_DAT1 re-measurement -- the split must reach the parent ledger.

    Sibling pairs on board 07 (MIPI_CLK, MIPI_DAT0, TMDS_D0-D2) each clear
    every proposal in ONE escape direction and none in the other; MIPI_DAT1
    clears neither.  ``ConstructionBudget.stage_summary()`` has to state that
    split directly (``departure_directions={...}``), not just the pooled
    ``departures_found`` count that cannot tell the two failure modes apart.
    """
    from kicad_tools.router.departure_planning import (
        DIRECTION_AWAY_FROM_GOAL,
        DIRECTION_TOWARD_GOAL,
    )

    def fake_validated(finder, pads, portion):
        portion.proposals_seen = 12
        portion.direction_seen[DIRECTION_TOWARD_GOAL] = 6
        portion.direction_seen[DIRECTION_AWAY_FROM_GOAL] = 6
        portion.direction_validated[DIRECTION_AWAY_FROM_GOAL] = 6
        portion.iterations_used = 40
        yield from [_departure((1, 0), 3)] * 6

    monkeypatch.setattr(pair_construction, "validated_departures", fake_validated)
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    result = pair_construction._validated_departures(None, None, budget, limit=6)
    assert len(result) == 6
    assert budget.departure_direction_seen == {
        DIRECTION_TOWARD_GOAL: 6,
        DIRECTION_AWAY_FROM_GOAL: 6,
    }
    assert budget.departure_direction_validated == {DIRECTION_AWAY_FROM_GOAL: 6}
    assert "departure_directions={'away_from_goal': '6/6', 'toward_goal': '0/6'}" in (
        budget.stage_summary()
    )


def test_geometry_reasons_are_reported_most_frequent_first():
    budget = ConstructionBudget(deadline=1, iterations_remaining=1, bodies_remaining=1)
    budget.geometry_reasons.update(
        {"via_clearance": 3, "trace_clearance": 40, "pad_clearance": 3, "disconnected": 7}
    )
    assert (
        "geom_reasons={'trace_clearance': 40, 'disconnected': 7, "
        "'pad_clearance': 3, 'via_clearance': 3} completion_reasons={}"
    ) in budget.stage_summary()


def test_completion_reasons_are_reported_most_frequent_first():
    budget = ConstructionBudget(deadline=1, iterations_remaining=1, bodies_remaining=1)
    budget.completion_reasons.update(
        {"no_tail": 3, "skew_tolerance": 40, "coupling_threshold": 3, "post_tune_pad_clearance": 7}
    )
    assert (
        "completion_reasons={'skew_tolerance': 40, 'post_tune_pad_clearance': 7, "
        "'coupling_threshold': 3, 'no_tail': 3}"
    ) in budget.stage_summary()


def test_departure_reasons_are_reported_most_frequent_first():
    budget = ConstructionBudget(deadline=1, iterations_remaining=1, bodies_remaining=1)
    budget.departure_reasons.update({"stalled_at_step_0_of_9": 6, "stalled_at_step_3_of_15": 12})
    budget.departure_rejections.update({"sym_blocked_p": 60, "via_blocked_p": 9})
    assert budget.stage_summary().endswith(
        "departure_reasons={'stalled_at_step_3_of_15': 12, 'stalled_at_step_0_of_9': 6} "
        "departure_rejections={'sym_blocked_p': 60, 'via_blocked_p': 9} "
        "departure_directions={} "
        "corridor_attempts=0 corridor_iters=0 corridor_reasons={} widen_spent=0"
    )


def test_departure_reasons_propagate_from_the_native_stage(monkeypatch):
    """#5333: ``departures=0`` is the only stage tally with no split at all.

    MIPI_DAT1 on board 07 spends a real native allowance and validates
    nothing; that is compatible with an enumerator that offered no shape, an
    escape refused on its first step, and one refused only on its closing
    via.  The parent ledger has to carry the departure stage's own tally for
    those to be distinguishable after the fact.
    """

    def fake_validated(finder, pads, budget):
        budget.iterations_used += 30
        budget.iterations_remaining -= 30
        budget.proposals_seen += 12
        budget.reasons.update({"stalled_at_step_0_of_9": 6, "stalled_at_step_3_of_15": 6})
        budget.native_rejections.update({"sym_blocked_p": 60})
        return iter(())

    monkeypatch.setattr(pair_construction, "validated_departures", fake_validated)
    monkeypatch.setattr(pair_construction, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    assert _run(budget) is None
    assert budget.departures_found == 0 and budget.departure_proposals_seen == 12
    assert budget.departure_reasons == {
        "stalled_at_step_0_of_9": 6,
        "stalled_at_step_3_of_15": 6,
    }
    assert budget.departure_rejections == {"sym_blocked_p": 60}
    assert budget.iterations_used == 30


def test_an_unoffered_escape_is_distinguishable_from_a_refused_one(monkeypatch):
    """Both report ``departures=0``; only the proposal count separates them."""

    def never_offered(finder, pads, budget):
        budget.reasons["start_pads_not_axis_aligned"] += 1
        return iter(())

    monkeypatch.setattr(pair_construction, "validated_departures", never_offered)
    monkeypatch.setattr(pair_construction, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    assert _run(budget) is None
    assert budget.departure_proposals_seen == 0
    assert budget.departure_reasons == {"start_pads_not_axis_aligned": 1}
    assert budget.iterations_used == 0


def test_completion_reasons_propagate_from_the_body_stage(monkeypatch):
    """#5333: a body that geometrically clears still has to name why it
    never qualifies -- no legal tail, missed skew, missed coupling, or a
    post-tune collision are different defects with different fixes."""

    def fake_complete(router, finder, pair, pads, group, landing, budget, **kwargs):
        budget.bodies_used = budget.bodies_remaining
        budget.bodies_remaining = 0
        budget.completions_tried += 2
        budget.completion_reasons["skew_tolerance"] += 2
        return None

    _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=["a"], clock=[0])
    monkeypatch.setattr(pair_construction, "complete_departures", fake_complete)
    monkeypatch.setattr(pair_construction, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    assert _run(budget, max_landings=1, max_bodies_per_departure=8) is None
    assert budget.completion_reasons == {"skew_tolerance": 2}


def test_no_validated_departure_is_distinguishable_from_no_landing(monkeypatch):
    """Both spend zero bodies; only the tally separates them."""
    _stub(monkeypatch, departures=[], landings=["a"], clock=[0])
    starved = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    assert _run(starved) is None
    assert starved.departures_found == 0 and starved.landings_found == 0
    assert starved.bodies_used == 0

    _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=[], clock=[0])
    landless = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    assert _run(landless) is None
    assert landless.departures_found == 1 and landless.landings_found == 0
    assert landless.bodies_used == 0


def test_native_charges_survive_a_raising_stage(monkeypatch):
    def explode(finder, pads, budget):
        budget.iterations_used += 7
        budget.iterations_remaining -= 7
        raise RuntimeError("native validation failed")

    monkeypatch.setattr(pair_construction, "validated_departures", explode)
    monkeypatch.setattr(pair_construction, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=50)
    with pytest.raises(RuntimeError, match="native validation failed"):
        _run(budget)
    assert budget.iterations_used == 7 and budget.iterations_remaining == 57


def test_body_charges_survive_a_raising_stage(monkeypatch):
    def explode(router, finder, pair, pads, group, landing, budget, **kwargs):
        budget.bodies_used = 5
        budget.bodies_remaining -= 5
        raise RuntimeError("body search failed")

    _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=["a"], clock=[0])
    monkeypatch.setattr(pair_construction, "complete_departures", explode)
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=50)
    with pytest.raises(RuntimeError, match="body search failed"):
        _run(budget)
    assert budget.bodies_used == 5 and budget.bodies_remaining == 45


class _FakeFinder:
    """Stub finder for the corridor-guided fallback (#5333) -- no native call."""

    def __init__(
        self,
        result,
        *,
        iterations=5,
        best_progress=0.0,
        iteration_limited=False,
        timeout_exceeded=False,
    ):
        self.result = result
        self.calls = []
        self.last_iterations = 0
        self.last_best_progress = best_progress
        self.last_iteration_limited = iteration_limited
        self.last_timeout_exceeded = timeout_exceeded
        self._iterations = iterations

    def route_coupled(
        self, *pads, departure_prefix, timeout_seconds, max_iterations_budget, corridor
    ):
        self.calls.append((departure_prefix, corridor, max_iterations_budget))
        self.last_iterations = self._iterations
        return self.result


def test_corridor_fallback_runs_only_after_the_lattice_exhausts(monkeypatch):
    """#5333: MIPI_DAT0/TMDS_D2's next step -- a native, corridor-bounded
    search seeded past a validated departure, tried once the fixed shape
    lattice comes back empty for every escape direction."""
    departure = _departure((1, 0), 3)
    _stub(monkeypatch, departures=[departure], landings=[], clock=[0])
    finder = _FakeFinder(result=("p", "n"))
    budget = ConstructionBudget(
        deadline=1,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=100,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    assert result == ("p", "n")
    assert len(finder.calls) == 1
    assert finder.calls[0][0] == list(departure.proposal.prefix)
    assert budget.corridor_attempts == 1
    assert budget.corridor_iterations_used == 5
    assert budget.corridor_iterations_remaining == 95


def test_corridor_fallback_rejects_an_unqualified_native_result_and_tries_the_next_departure(
    monkeypatch,
):
    """#5333 regression: a raw native corridor result is NOT length-matched
    by construction -- measured on real Board07's MIPI_DAT0, an unqualified
    corridor result was legal copper but 1.75mm out of skew against a
    0.05mm authored tolerance. ``_corridor_guided_departures`` must run
    every native result through the same authored skew/coupling gate
    ``complete_pair_body`` applies (``qualify_constructed_pair``), reject
    anything that fails it, and try the NEXT validated departure instead of
    returning the unqualified pair."""
    departures = [_departure((1, 0), 3), _departure((1, 0), 2)]

    def reject_first_accept_second(router, finder, pair, pads, candidate, reasons=None, **kwargs):
        if len(finder.calls) == 1:
            if reasons is not None:
                reasons["skew_tolerance"] += 1
            return None
        return candidate

    _stub(
        monkeypatch,
        departures=departures,
        landings=[],
        clock=[0],
        qualify=reject_first_accept_second,
    )
    finder = _FakeFinder(result=("p", "n"))
    budget = ConstructionBudget(
        deadline=1,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=100,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    assert result == ("p", "n")
    assert len(finder.calls) == 2
    assert budget.corridor_attempts == 2
    assert budget.corridor_reasons == {"skew_tolerance": 1}


def test_corridor_fallback_never_returns_an_unqualified_pair(monkeypatch):
    """If NO validated departure's native result ever qualifies, the
    corridor stage must return ``None`` -- never a raw, unqualified native
    route -- and the failure reason must come from the qualification gate,
    not be misreported as a native search stall."""

    def always_reject(router, finder, pair, pads, candidate, reasons=None, **kwargs):
        if reasons is not None:
            reasons["coupling_threshold"] += 1
        return None

    _stub(
        monkeypatch,
        departures=[_departure((1, 0), 3)],
        landings=[],
        clock=[0],
        qualify=always_reject,
    )
    finder = _FakeFinder(result=("p", "n"))
    budget = ConstructionBudget(
        deadline=1,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=100,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    assert result is None
    assert budget.corridor_attempts == 1
    assert budget.corridor_reasons == {"coupling_threshold": 1}


def test_corridor_fallback_is_not_reached_when_the_lattice_already_succeeds(monkeypatch):
    """A pair that already routes through the geometric lattice must never
    change outcome just because a corridor was supplied (no regression risk
    for DQS / MIPI_CLK / TMDS_D0)."""
    routes = ("p", "n")
    _stub(
        monkeypatch,
        departures=[_departure((1, 0), 3)],
        landings=["a"],
        result=routes,
        clock=[0],
    )
    finder = _FakeFinder(result=("should", "not-be-used"))
    budget = ConstructionBudget(
        deadline=1,
        iterations_remaining=64,
        bodies_remaining=50,
        corridor_iterations_remaining=100,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    assert result == routes
    assert finder.calls == []
    assert budget.corridor_attempts == 0


def test_corridor_fallback_is_skipped_without_a_corridor(monkeypatch):
    _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=[], clock=[0])
    finder = _FakeFinder(result=("p", "n"))
    budget = ConstructionBudget(
        deadline=1,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=100,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
    )
    assert result is None
    assert finder.calls == []


def test_corridor_fallback_is_skipped_when_its_own_allowance_is_zero(monkeypatch):
    """Default ``corridor_iterations_remaining=0`` matches every pre-existing
    test in this file -- the new stage is inert unless a caller explicitly
    spends a separate allowance on it."""
    _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=[], clock=[0])
    finder = _FakeFinder(result=("p", "n"))
    budget = ConstructionBudget(deadline=1, iterations_remaining=64, bodies_remaining=16)
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    assert result is None
    assert finder.calls == []


def test_corridor_fallback_names_why_each_attempt_failed(monkeypatch):
    _stub(
        monkeypatch,
        departures=[_departure((1, 0), 3), _departure((1, 0), 2)],
        landings=[],
        clock=[0],
    )
    finder = _FakeFinder(result=None, iterations=10, best_progress=4, iteration_limited=True)
    budget = ConstructionBudget(
        deadline=1,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=15,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    assert result is None
    assert len(finder.calls) == 2
    assert budget.corridor_reasons == {"iteration_limited_progress_4": 2}


def test_corridor_fallback_splits_its_allowance_fairly_across_departures(monkeypatch):
    """#5333 (MIPI_DAT0/TMDS_D1/TMDS_D2 re-measurement): on real Board07,
    handing every attempt the FULL remaining ``corridor_iterations_
    remaining`` let the first (or first two) validated departures consume
    the entire 150,000-iteration allowance by themselves -- MIPI_DAT0 (6
    validated departures) only ever reached ``corridor_attempts=2``, and
    TMDS_D1/TMDS_D2/MIPI_DAT1 each reached exactly 1, leaving every other
    natively-validated escape direction completely untried.  Each attempt
    must instead get a max-min FAIR share -- ``remaining // departures
    still to try`` -- so a starved-out tail of validated departures
    actually gets a turn."""
    departures = [_departure((1, 0), i) for i in range(3)]
    _stub(monkeypatch, departures=departures, landings=[], clock=[0])
    # Always fails to converge (result=None) and reports using only 5
    # iterations regardless of its offered share -- isolates the ALLOWANCE
    # each attempt is offered (what this fix changes) from how much it
    # actually spends (unaffected: the stub always "spends" 5).
    finder = _FakeFinder(result=None, iterations=5, best_progress=4, iteration_limited=True)
    budget = ConstructionBudget(
        deadline=1,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=90,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    assert result is None
    assert len(finder.calls) == 3
    offered_allowances = [call[2] for call in finder.calls]
    # NOT [90, 90, 90] (the pre-fix "full remaining every time" policy) and
    # NOT front-loaded onto the first attempt alone: the first attempt gets
    # only its fair 1/3 share (30, not the full 90), and because it only
    # "spends" 5 of that, the surplus rolls forward -- each later attempt's
    # share grows as the pool is re-split across fewer remaining departures.
    assert offered_allowances == [30, 42, 80]
    assert all(share < 90 for share in offered_allowances[:-1]), (
        "an early attempt must never be offered the full remaining pool "
        "when later validated departures still need a turn"
    )
    assert budget.corridor_attempts == 3
    assert budget.corridor_iterations_used == 15
    assert budget.corridor_iterations_remaining == 75


def _clock_burning_lattice(monkeypatch, clock):
    """Stub a lattice body attempt that spends every second it is allowed.

    It reads the deadline it was actually HANDED (the per-landing
    ``BodySearchBudget``'s) and runs the clock right up to it -- exactly what
    ``complete_departures``' real widen/tail search does on a structurally
    blocked landing, and the behavior that makes the reserve observable.
    """

    def fake_complete(router, finder, pair, pads, group, landing, budget, **kwargs):
        clock[0] = budget.deadline
        budget.bodies_used = 1
        budget.bodies_remaining -= 1
        return None

    monkeypatch.setattr(pair_construction, "complete_departures", fake_complete)


def test_a_failing_lattice_cannot_spend_the_whole_window_before_the_corridor_runs(monkeypatch):
    """#5333 (TMDS_D1): the corridor stage must get a turn, not just a ledger.

    Measured on real Board07 (seed 42, native ABI 31, regression fixture,
    ``--differential-pairs``), TMDS_D1 reported ``landings=1 bodies=139
    completions=146 completion_reasons={'no_tail': 277, ...} widen_spent=40
    corridor_attempts=0``: the geometric lattice spent the pair's ENTIRE 60 s
    window on full-lattice tail widening and the corridor-guided native
    search -- the stage that is the only reason MIPI_DAT0 resolves at all --
    never ran once, with its whole separate iteration allowance unspent.

    Against the OLD implementation the lattice was handed the pair's own
    deadline, so it burns the clock to 1.0 and the corridor stage bails on
    its first deadline check (``corridor_attempts=0``, result ``None``).
    """
    clock = [0.0]
    _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=["a"], clock=clock)
    _clock_burning_lattice(monkeypatch, clock)
    finder = _FakeFinder(result=("p", "n"))
    budget = ConstructionBudget(
        deadline=1.0,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=100,
    )
    result = construct_pair_routes(
        None,
        finder,
        None,
        (1, 2, 3, 4),
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        corridor=frozenset({(0, 0)}),
    )
    # The lattice is cut off at the reserve boundary (0.5 of a 1.0 window),
    # so the clock stands at 0.5 -- still inside the pair's own deadline --
    # when the corridor stage is offered its turn, and it resolves the pair.
    assert clock[0] == 0.5
    assert result == ("p", "n")
    assert budget.corridor_attempts == 1
    assert finder.calls and finder.calls[0][2] == 100


def test_without_a_corridor_the_lattice_still_owns_the_entire_window(monkeypatch):
    """No later stage exists, so holding time back would only waste it.

    Same clock-burning lattice as the test above, but with no corridor: the
    lattice is handed the pair's own deadline exactly as before #5333's
    reserve, so its first landing spends the clock all the way to 1.0.
    """
    clock = [0.0]
    departures = [_departure((1, 0), 3), _departure((-1, 0), 3)]
    _stub(monkeypatch, departures=departures, landings=["a"], clock=clock)
    _clock_burning_lattice(monkeypatch, clock)
    budget = ConstructionBudget(deadline=1.0, iterations_remaining=64, bodies_remaining=16)
    assert (
        construct_pair_routes(
            None,
            None,
            None,
            (1, 2, 3, 4),
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )
        is None
    )
    assert clock[0] == 1.0


def test_the_reserve_never_applies_when_the_corridor_allowance_is_zero(monkeypatch):
    """A corridor with no iteration allowance cannot run, so it gets no reserve."""
    clock = [0.0]
    departures = [_departure((1, 0), 3), _departure((-1, 0), 3)]
    _stub(monkeypatch, departures=departures, landings=["a"], clock=clock)
    _clock_burning_lattice(monkeypatch, clock)
    finder = _FakeFinder(result=("p", "n"))
    budget = ConstructionBudget(deadline=1.0, iterations_remaining=64, bodies_remaining=16)
    assert (
        construct_pair_routes(
            None,
            finder,
            None,
            (1, 2, 3, 4),
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
            corridor=frozenset({(0, 0)}),
        )
        is None
    )
    assert clock[0] == 1.0
    assert finder.calls == []


def test_the_lattice_sub_deadline_can_never_exceed_the_pair_deadline(monkeypatch):
    """A reserve fraction of zero (or a bad override) must not extend a budget."""
    monkeypatch.setattr(pair_construction, "CORRIDOR_WALL_RESERVE_FRACTION", 0.0)
    clock = [0.0]
    _stub(monkeypatch, departures=[_departure((1, 0), 3)], landings=["a"], clock=clock)
    budget = ConstructionBudget(
        deadline=1.0,
        iterations_remaining=64,
        bodies_remaining=16,
        corridor_iterations_remaining=100,
    )
    assert pair_construction._lattice_deadline(budget, frozenset({(0, 0)})) == 1.0
    monkeypatch.setattr(pair_construction, "CORRIDOR_WALL_RESERVE_FRACTION", 5.0)
    # Clamped to the whole window, and still never later than the deadline.
    assert pair_construction._lattice_deadline(budget, frozenset({(0, 0)})) == 0.0
    assert pair_construction._lattice_deadline(budget, None) == 1.0


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_constructs_a_qualified_pair_where_the_joint_search_cannot():
    """End-to-end: the barrier control from #5333's root-cause measurement.

    The joint search is confined to F.Cu and therefore cannot cross the
    barrier at any budget; construction fans out, changes layer and returns,
    and the assembled pair satisfies the authored skew and coupling gates.
    """
    auto, finder, pair, pads = fixture(barrier=True)
    router = auto._diffpair
    assert finder.route_coupled(*pads, timeout_seconds=60.0, max_iterations_budget=20000) is None

    before = (list(auto.routes), list(auto.grid.routes))
    budget = ConstructionBudget(time.monotonic() + 120, 256, 400)
    result = construct_pair_routes(
        router,
        finder,
        pair,
        pads,
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
    )
    assert result is not None
    p, n = result
    nc = finder.net_class_map["P"]
    lengths = [
        MatchGroupTracker._measure_route_total(r, 1.6, 4, blind_buried_supported=False)
        for r in (p, n)
    ]
    assert abs(lengths[0] - lengths[1]) <= nc.skew_tolerance_mm
    assert (
        min(
            router._tail_coupled_fraction(p, n.segments),
            router._tail_coupled_fraction(n, p.segments),
        )
        >= nc.coupled_continuity_threshold
    )
    # A real layer change, on both halves, with balanced barrels.
    for route in (p, n):
        assert len({segment.layer for segment in route.segments}) > 1
    assert len(p.vias) == len(n.vias) >= 2
    # Construction commits nothing: the caller decides.
    assert (auto.routes, auto.grid.routes) == before
    assert budget.iterations_used <= 256 and budget.bodies_used <= 400
