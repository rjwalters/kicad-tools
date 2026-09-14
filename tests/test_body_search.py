from types import SimpleNamespace

from kicad_tools.router import body_search
from kicad_tools.router.body_search import BodySearchBudget, complete_departure


def fixture():
    finder = SimpleNamespace(grid=SimpleNamespace(resolution=0.127), net_class_map={"P": object()})
    pads = tuple(
        SimpleNamespace(x=x, y=y, net=net, net_name=name)
        for net, name, points in ((1, "P", ((0, 0), (20, 0))), (2, "N", ((1, 0), (20, 1))))
        for x, y in points
    )
    departure = SimpleNamespace(proposal=SimpleNamespace(across=(1, 0)))
    return finder, pads, departure


def run(finder, pads, departure, budget):
    return complete_departure(
        None,
        finder,
        None,
        pads,
        departure,
        None,
        budget,
        board_thickness_mm=1.6,
        num_copper_layers=4,
    )


def test_body_attempt_cap_is_shared_across_departures(monkeypatch):
    finder, pads, departure = fixture()
    calls = []

    def rejected(*args, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(body_search, "construct_pair_body", rejected)
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = BodySearchBudget(deadline=1, bodies_remaining=3)
    assert run(finder, pads, departure, budget) is None
    assert budget.bodies_used == 3 and budget.bodies_remaining == 0 and len(calls) == 3
    assert run(finder, pads, departure, budget) is None
    assert budget.bodies_used == 3 and len(calls) == 3


def test_shared_deadline_stops_before_next_body(monkeypatch):
    finder, pads, departure = fixture()
    clock = [0]
    calls = []

    def expire(*args, **kwargs):
        calls.append(True)
        clock[0] = 1
        return None

    monkeypatch.setattr(body_search, "construct_pair_body", expire)
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    budget = BodySearchBudget(deadline=1, bodies_remaining=100)
    assert run(finder, pads, departure, budget) is None
    assert len(calls) == 1 and budget.bodies_used == 1 and budget.bodies_remaining == 99
    assert run(finder, pads, departure, budget) is None
    assert len(calls) == 1


def test_layer_slices_do_not_reset_the_parent_allowance(monkeypatch):
    finder, pads, departure = fixture()
    calls = []

    def consume(router, finder, pair, pads, departure, landing, budget, **kwargs):
        calls.append((budget.deadline, budget.bodies_remaining))
        budget.bodies_used = budget.bodies_remaining
        budget.bodies_remaining = 0
        return None

    monkeypatch.setattr(body_search, "complete_departure", consume)
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = BodySearchBudget(1, 9)
    assert (
        body_search.complete_departures(
            None,
            finder,
            None,
            pads,
            [departure] * 4,
            None,
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
            max_bodies_per_departure=4,
        )
        is None
    )
    assert calls == [(1, 4), (1, 4), (1, 1)]
    assert budget.bodies_used == 9 and budget.bodies_remaining == 0


def test_failed_constructor_still_debits_attempts(monkeypatch):
    import pytest

    finder, pads, departure = fixture()

    def fail(router, finder, pair, pads, departure, landing, budget, **kwargs):
        budget.bodies_used = 2
        budget.bodies_remaining -= 2
        raise RuntimeError("construction failed")

    monkeypatch.setattr(body_search, "complete_departure", fail)
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = BodySearchBudget(1, 9)
    with pytest.raises(RuntimeError, match="construction failed"):
        body_search.complete_departures(
            None,
            finder,
            None,
            pads,
            [departure],
            None,
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )
    assert budget.bodies_used == 2 and budget.bodies_remaining == 7
