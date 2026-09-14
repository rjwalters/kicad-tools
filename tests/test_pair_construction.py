"""One ledger spends departures, landings and bodies for a pair (#5333)."""

import time
from types import SimpleNamespace

import pytest

from kicad_tools.router import pair_construction
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.match_group_length import MatchGroupTracker
from kicad_tools.router.pair_construction import ConstructionBudget, construct_pair_routes
from tests.test_coupled_layer_transition import fixture


def _departure(outward, layer):
    return SimpleNamespace(proposal=SimpleNamespace(outward=outward, layer=layer))


def _stub(monkeypatch, *, departures, landings, result=None, clock=None):
    """Replace every stage with a recording stub around the real ledger."""
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

    monkeypatch.setattr(pair_construction, "validated_departures", fake_validated)
    monkeypatch.setattr(pair_construction, "landing_proposals", fake_landings)
    monkeypatch.setattr(pair_construction, "complete_departures", fake_complete)
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
        "departure_reasons={} departure_rejections={}"
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
        "departure_rejections={'sym_blocked_p': 60, 'via_blocked_p': 9}"
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
