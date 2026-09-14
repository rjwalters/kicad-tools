import time
from dataclasses import replace

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.departure_planning import (
    DepartureBudget,
    departure_proposals,
    validated_departures,
)
from tests.test_diffpair_coupled_departure_prefix import _public_pads
from tests.test_diffpair_coupled_endpoint_via_guard import _finder


@pytest.mark.parametrize("quarter_turns", [1, 2, 3])
def test_proposals_rotate_with_pad_geometry(quarter_turns):
    finder = _finder()
    pads = _public_pads(finder)
    proposals = list(departure_proposals(finder, pads))
    assert len(proposals) == 12

    def rotate(x, y):
        x, y = x - 20, y - 15
        for _ in range(quarter_turns):
            x, y = -y, x
        return x + 20, y + 15

    transformed = []
    for pad in pads:
        x, y = rotate(*finder.grid.world_to_grid(pad.x, pad.y))
        wx, wy = finder.grid.grid_to_world(x, y)
        transformed.append(
            replace(
                pad,
                x=wx,
                y=wy,
                width=pad.height if quarter_turns % 2 else pad.width,
                height=pad.width if quarter_turns % 2 else pad.height,
            )
        )
    expected = set()
    for proposal in proposals:
        expected.add(
            tuple(
                (*rotate(px, py), pl, *rotate(nx, ny), nl)
                for px, py, pl, nx, ny, nl in proposal.prefix
            )
        )
    assert {p.prefix for p in departure_proposals(finder, tuple(transformed))} == expected


def test_expired_budget_performs_no_native_attempt(monkeypatch):
    finder = _finder()
    monkeypatch.setattr(
        finder, "route_coupled", lambda *a, **kw: pytest.fail("expired native call")
    )
    budget = DepartureBudget(0, 100)
    assert list(validated_departures(finder, _public_pads(finder), budget)) == []
    assert budget.iterations_used == 0 and budget.iterations_remaining == 100


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_all_attempts_charge_one_iteration_budget(monkeypatch):
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    budget = DepartureBudget(time.monotonic() + 5, 64)
    original = finder.route_coupled
    charges = []

    def record(*args, **kwargs):
        assert 0 < kwargs["max_iterations_budget"] <= budget.iterations_remaining
        assert 0 < kwargs["timeout_seconds"] <= 5
        result = original(*args, **kwargs)
        charges.append(finder.last_iterations)
        return result

    monkeypatch.setattr(finder, "route_coupled", record)
    proposals = list(validated_departures(finder, _public_pads(finder), budget))
    assert proposals
    assert charges and budget.iterations_used == sum(charges)
    assert budget.iterations_remaining == 64 - sum(charges) >= 0
    for departure in proposals:
        for route in (departure.p_route, departure.n_route):
            assert route.segments and route.vias
        assert departure.iterations > 0
