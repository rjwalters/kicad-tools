"""Bounded scheduling must give each departure direction a landing attempt."""

from types import SimpleNamespace as NS

from kicad_tools.router import body_search, pair_construction
from kicad_tools.router.pair_construction import ConstructionBudget


def run(monkeypatch, *, success=True, expire=False, empty_first=False):
    clock = [0.0]
    visits = []
    generated = []
    directions = ((0, -1), (0, 1))
    departures = [
        NS(proposal=NS(outward=d)) for d, count in zip(directions, (4, 2), strict=True) for _ in range(count)
    ]
    result = object()

    def landings(*args, outward, **kwargs):
        if empty_first and outward == directions[0]:
            return
        for index in range(4):
            generated.append((outward, index))
            if expire:
                clock[0] = 10.0
            yield NS(outward=outward, index=index)

    def complete(router, finder, pair, pads, departure, landing, budget, **kwargs):
        visits.append((landing.outward, landing.index, budget.bodies_remaining))
        budget.bodies_used += budget.bodies_remaining
        budget.bodies_remaining = 0
        return result if success and landing.outward == directions[1] else None

    monkeypatch.setattr(pair_construction, "landing_proposals", landings)
    monkeypatch.setattr(body_search, "complete_departure", complete)
    monkeypatch.setattr(pair_construction, "time", NS(monotonic=lambda: clock[0]))
    monkeypatch.setattr(body_search, "time", NS(monotonic=lambda: clock[0]))
    budget = ConstructionBudget(deadline=10.0, iterations_remaining=256, bodies_remaining=400)
    found = pair_construction._geometric_body_search(
        None,
        None,
        None,
        (),
        departures,
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
        reserved_routes=(),
        max_landings=4,
        max_bodies_per_departure=32,
    )
    return found, result, budget, visits, generated


def test_second_direction_gets_first_landing_before_first_retries(monkeypatch):
    found, result, budget, visits, generated = run(monkeypatch)
    assert found is result
    assert visits == [((0, -1), 0, 32)] * 4 + [((0, 1), 0, 32)]
    assert generated == [((0, -1), 0), ((0, 1), 0)]
    assert budget.bodies_used == 160 and budget.bodies_remaining == 240


def test_all_rejections_preserve_exact_total_cap(monkeypatch):
    found, _, budget, visits, generated = run(monkeypatch, success=False)
    assert found is None
    assert budget.bodies_used == 400 and budget.bodies_remaining == 0
    assert {v[0] for v in visits} == {(0, -1), (0, 1)}
    assert sum(v[2] for v in visits) == 400
    assert all(0 < v[2] <= 32 for v in visits)
    assert len(generated) <= 8


def test_deadline_during_landing_generation_prevents_body_attempt(monkeypatch):
    found, _, budget, visits, generated = run(monkeypatch, expire=True)
    assert found is None and visits == []
    assert budget.bodies_used == 0 and budget.bodies_remaining == 400
    assert generated == [((0, -1), 0)]


def test_empty_direction_does_not_hide_next_direction(monkeypatch):
    found, result, budget, visits, generated = run(monkeypatch, empty_first=True)
    assert found is result
    assert budget.bodies_used == 32
    assert generated == [((0, 1), 0)]


def test_exception_debits_attempts_before_propagating(monkeypatch):
    budget = ConstructionBudget(deadline=10.0, iterations_remaining=256, bodies_remaining=400)
    departures = [NS(proposal=NS(outward=(0, -1)))]
    monkeypatch.setattr(pair_construction, "time", NS(monotonic=lambda: 0.0))
    monkeypatch.setattr(body_search, "time", NS(monotonic=lambda: 0.0))
    monkeypatch.setattr(pair_construction, "landing_proposals", lambda *a, **k: iter((object(),)))

    def fail(*args, **kwargs):
        portion = args[6]
        portion.bodies_used = 7
        portion.bodies_remaining -= 7
        raise RuntimeError("body failed after seven attempts")

    monkeypatch.setattr(body_search, "complete_departure", fail)
    import pytest

    with pytest.raises(RuntimeError, match="seven attempts"):
        pair_construction._geometric_body_search(
            None,
            None,
            None,
            (),
            departures,
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
            reserved_routes=(),
            max_landings=4,
            max_bodies_per_departure=32,
        )
    assert budget.bodies_used == 7 and budget.bodies_remaining == 393
