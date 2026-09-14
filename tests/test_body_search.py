import itertools
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


def test_preference_order_keeps_the_best_combination_first():
    order = body_search.preference_ordered("ab", (0, 1), (None, -1))
    assert order[0] == ("a", 0, None)


def test_preference_order_spans_every_axis_before_exhausting_one():
    """The defect a lexicographic product hides (#5333)."""
    axes = (range(6), range(17), range(3), range(3))
    lexicographic = list(itertools.product(*axes))[:32]
    ranked = body_search.preference_ordered(*axes)[:32]
    # A truncated product never varies its outermost axis at all.
    assert {combo[0] for combo in lexicographic} == {0}
    # The ranked prefix samples every axis.
    for index in range(4):
        assert len({combo[index] for combo in ranked}) > 1


def test_preference_order_is_a_deterministic_permutation():
    axes = (range(3), range(4), range(2))
    ranked = body_search.preference_ordered(*axes)
    assert ranked == body_search.preference_ordered(*axes)
    assert sorted(ranked) == sorted(itertools.product(*axes))
    # Non-decreasing total rank is what makes the truncated prefix meaningful.
    sums = [sum(combo) for combo in ranked]
    assert sums == sorted(sums)


def test_a_constructor_that_never_builds_reports_zero_built(monkeypatch):
    """``bodies_used`` alone cannot name the stage that failed (#5333)."""
    finder, pads, departure = fixture()
    monkeypatch.setattr(body_search, "construct_pair_body", lambda *a, **k: None)
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = BodySearchBudget(deadline=1, bodies_remaining=4)
    assert run(finder, pads, departure, budget) is None
    assert budget.bodies_used == 4
    assert budget.bodies_built == 0
    assert budget.bodies_geometry_rejected == 0
    assert budget.completions_tried == 0


def test_built_geometry_rejected_against_copper_is_counted_separately(monkeypatch):
    """A body that exists but collides is a different defect from no body."""
    finder, pads, departure = fixture()
    body = SimpleNamespace(p_head=(0, 0), n_head=(0, 1), p_route=None, n_route=None)
    router = SimpleNamespace(_virtual_pad_at=lambda *a, **k: None)
    finder.grid = SimpleNamespace(resolution=0.127, grid_to_world=lambda *a: (0.0, 0.0))
    finder.net_class_map = {"P": SimpleNamespace(effective_intra_pair_clearance=lambda: 0.15)}
    departure.proposal = SimpleNamespace(across=(1, 0), layer=0)

    monkeypatch.setattr(body_search, "construct_pair_body", lambda *a, **k: body)
    monkeypatch.setattr(
        body_search, "constructed_pair_geometry_issue", lambda *a, **k: "trace_clearance"
    )
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = BodySearchBudget(deadline=1, bodies_remaining=3)
    assert (
        complete_departure(
            router,
            finder,
            None,
            pads,
            departure,
            None,
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )
        is None
    )
    assert budget.bodies_used == 3
    assert budget.bodies_built == 3
    assert budget.bodies_geometry_rejected == 3
    # The reason token is what separates "this shape collides" from "every
    # shape fails the same check".
    assert budget.geometry_reasons == {"trace_clearance": 3}
    # Terminal completion is never reached when the body itself collides.
    assert budget.completions_tried == 0


def test_clear_geometry_reaches_terminal_completion(monkeypatch):
    """Both approach orderings are tried and both are counted."""
    finder, pads, departure = fixture()
    body = SimpleNamespace(p_head=(0, 0), n_head=(0, 1), p_route=None, n_route=None)
    router = SimpleNamespace(_virtual_pad_at=lambda *a, **k: None)
    finder.grid = SimpleNamespace(resolution=0.127, grid_to_world=lambda *a: (0.0, 0.0))
    finder.net_class_map = {"P": SimpleNamespace(effective_intra_pair_clearance=lambda: 0.15)}
    departure.proposal = SimpleNamespace(across=(1, 0), layer=0)

    monkeypatch.setattr(body_search, "construct_pair_body", lambda *a, **k: body)
    monkeypatch.setattr(body_search, "constructed_pair_geometry_issue", lambda *a, **k: None)
    monkeypatch.setattr(body_search, "complete_pair_body", lambda *a, **k: None)
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = BodySearchBudget(deadline=1, bodies_remaining=2)
    assert (
        complete_departure(
            router,
            finder,
            None,
            pads,
            departure,
            SimpleNamespace(allowed_sites=()),
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )
        is None
    )
    assert budget.bodies_built == 2
    assert budget.bodies_geometry_rejected == 0
    assert budget.completions_tried == 4


def test_stage_counters_propagate_to_the_parent_allowance(monkeypatch):
    finder, pads, departure = fixture()

    def consume(router, finder, pair, pads, departure, landing, budget, **kwargs):
        budget.bodies_used = budget.bodies_remaining
        budget.bodies_remaining = 0
        budget.bodies_built += 2
        budget.bodies_geometry_rejected += 1
        budget.completions_tried += 3
        budget.geometry_reasons["via_clearance"] += 1
        return None

    monkeypatch.setattr(body_search, "complete_departure", consume)
    monkeypatch.setattr(body_search, "time", SimpleNamespace(monotonic=lambda: 0))
    budget = BodySearchBudget(1, 8)
    assert (
        body_search.complete_departures(
            None,
            finder,
            None,
            pads,
            [departure] * 2,
            None,
            budget,
            board_thickness_mm=1.6,
            num_copper_layers=4,
            max_bodies_per_departure=4,
        )
        is None
    )
    assert budget.bodies_built == 4
    assert budget.bodies_geometry_rejected == 2
    assert budget.completions_tried == 6
    assert budget.geometry_reasons == {"via_clearance": 2}


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
