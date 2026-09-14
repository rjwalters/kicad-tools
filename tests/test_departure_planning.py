import math
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


def _tight_pitch_pads(finder, pitch_cells):
    """Source pads closer together than the mutual barrel pitch."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    return tuple(
        Pad(x=x, y=y, width=0.2, height=0.2, net=net, net_name=str(net), layer=layer)
        for net, gx in ((1, 10), (2, 10 + pitch_cells))
        for layer in (Layer.F_CU, Layer.B_CU)
        for x, y in [finder.grid.grid_to_world(gx, 10)]
    )


def test_tight_pitch_proposals_fan_out_to_the_mutual_barrel_pitch():
    """Issue #5333: a paired via needs the mutual barrel pitch, not pad pitch.

    Without the fan-out every one of these proposals ends in a via step the
    native search rejects as ``via_pair_pitch``, which is what confined
    fine-pitch escapes to their start layer.
    """
    finder = _finder()
    required = finder._minimum_via_pitch_cells()
    pads = _tight_pitch_pads(finder, 4)
    assert required > 4
    proposals = list(departure_proposals(finder, pads))
    assert proposals
    for proposal in proposals:
        px, py, pl, nx, ny, nl = proposal.prefix[-1]
        assert (pl, nl) == (proposal.layer, proposal.layer)
        assert math.hypot(px - nx, py - ny) + 1e-9 >= required
        # Every spread step is one ordinary single-cell, single-leg move.
        for before, after in zip(proposal.prefix[:-1], proposal.prefix[1:], strict=True):
            moved = [abs(after[i] - before[i]) + abs(after[i + 1] - before[i + 1]) for i in (0, 3)]
            assert max(moved) <= 1 or after[2] != before[2]


def test_wide_pitch_proposals_keep_their_original_shape():
    """Pads already at or beyond the barrel pitch get no extra steps."""
    finder = _finder()
    pads = _tight_pitch_pads(finder, int(math.ceil(finder._minimum_via_pitch_cells())))
    for proposal in departure_proposals(finder, pads):
        px, py, _, nx, ny, _ = proposal.prefix[-2]
        before_via = math.hypot(px - nx, py - ny)
        px, py, _, nx, ny, _ = proposal.prefix[-1]
        assert math.isclose(before_via, math.hypot(px - nx, py - ny))


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_native_validation_accepts_a_fanned_out_fine_pitch_escape():
    """The whole point of the fan-out: real, validated departures exist."""
    from tests.test_coupled_layer_transition import fixture

    _, finder, _, pads = fixture()
    assert finder.target_spacing_cells < finder._minimum_via_pitch_cells()
    budget = DepartureBudget(time.monotonic() + 60, 512)
    departures = list(validated_departures(finder, pads, budget))
    assert departures
    for departure in departures:
        assert departure.p_route.vias and departure.n_route.vias
        via = (departure.p_route.vias[-1], departure.n_route.vias[-1])
        distance = math.hypot(via[0].x - via[1].x, via[0].y - via[1].y)
        assert distance + 1e-9 >= max(
            finder.rules.via_diameter + finder.rules.via_clearance,
            finder.rules.via_drill + finder.rules.min_hole_to_hole,
        )
    assert 0 < budget.iterations_used <= 512
