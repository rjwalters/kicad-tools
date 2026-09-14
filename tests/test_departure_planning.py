import math
import time
from collections import Counter
from dataclasses import replace

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.departure_planning import (
    DIRECTION_AWAY_FROM_GOAL,
    DIRECTION_TOWARD_GOAL,
    PREFIX_CONSTRAINT_REJECTION,
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


def _foreign_wall(finder, cells, layer=0, net=99):
    """Real foreign copper, blocking exactly the given start-layer cells."""
    for x, y in cells:
        finder.grid._blocked[layer, y, x] = True
        finder.grid._net[layer, y, x] = net


def test_default_tallies_are_never_shared_between_budgets():
    """A mutable default would merge two pairs' departure diagnostics."""
    first = DepartureBudget(1, 1)
    second = DepartureBudget(1, 1)
    first.reasons["stalled_at_step_0_of_9"] += 1
    first.native_rejections["asym_blocked_p"] += 1
    first.direction_seen[DIRECTION_TOWARD_GOAL] += 1
    first.direction_validated[DIRECTION_AWAY_FROM_GOAL] += 1
    assert second.reasons == Counter() and second.native_rejections == Counter()
    assert second.direction_seen == Counter() and second.direction_validated == Counter()


def test_start_layer_mismatch_names_itself_instead_of_yielding_nothing():
    """#5333: ``departures=0`` must separate "never offered" from "refused"."""
    finder = _finder()
    p_start, p_goal, n_start, n_goal = _public_pads(finder)
    reasons: Counter[str] = Counter()
    pads = (p_start, p_goal, replace(n_start, layer=n_goal.layer), n_goal)
    assert list(departure_proposals(finder, pads, reasons)) == []
    assert reasons == {"start_layers_differ": 1}


@pytest.mark.parametrize("offset", [(0, 0), (12, 4)])
def test_a_non_axis_aligned_pad_pair_names_itself(offset):
    """Coincident or diagonal starts define no across/outward frame."""
    finder = _finder()
    p_start, p_goal, n_start, n_goal = _public_pads(finder)
    gx, gy = finder.grid.world_to_grid(p_start.x, p_start.y)
    wx, wy = finder.grid.grid_to_world(gx + offset[0], gy + offset[1])
    reasons: Counter[str] = Counter()
    pads = (p_start, p_goal, replace(n_start, x=wx, y=wy), n_goal)
    assert list(departure_proposals(finder, pads, reasons)) == []
    assert reasons == {"start_pads_not_axis_aligned": 1}


def test_a_stack_with_no_other_routable_layer_names_itself(monkeypatch):
    """Every proposal ends on a paired via, so one layer offers none."""
    finder = _finder()
    monkeypatch.setattr(finder.grid, "get_routable_indices", lambda: [0])
    reasons: Counter[str] = Counter()
    assert list(departure_proposals(finder, _public_pads(finder), reasons)) == []
    assert reasons == {"no_target_layer": 1}


def test_a_structurally_sound_enumeration_records_no_reason():
    finder = _finder()
    reasons: Counter[str] = Counter()
    assert len(list(departure_proposals(finder, _public_pads(finder), reasons))) == 12
    assert reasons == Counter()


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_a_blocked_escape_names_the_required_step_it_never_got_past():
    """#5333, MIPI_DAT1's signature: zero departures with a real spend.

    Foreign copper laid one cell beyond both pads on the start layer makes
    every straight escape illegal at its FIRST step, while the bend variants
    travel three legal steps before meeting the same wall.  Without this
    tally both collapse into ``departures=0``, which cannot say whether the
    escape never left the pad row or died on its paired via.
    """
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    _foreign_wall(finder, [(10, 9), (22, 9), (10, 11), (22, 11)])
    budget = DepartureBudget(time.monotonic() + 60, 512)
    assert list(validated_departures(finder, _public_pads(finder), budget)) == []
    assert budget.proposals_seen == 12 and budget.proposals_validated == 0
    assert budget.reasons == {"stalled_at_step_0_of_9": 6, "stalled_at_step_3_of_15": 6}
    assert budget.iterations_used > 0
    # The guard histogram survives, minus the tautological prefix token.
    assert budget.native_rejections["sym_blocked_p"] > 0
    assert PREFIX_CONSTRAINT_REJECTION not in budget.native_rejections


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_an_unblocked_escape_stalls_on_its_via_step_not_its_first():
    """The same tally on an open board points at a different step entirely."""
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    budget = DepartureBudget(time.monotonic() + 60, 512)
    departures = list(validated_departures(finder, _public_pads(finder), budget))
    assert departures and budget.proposals_validated == len(departures)
    assert budget.proposals_seen == 12
    # Every refusal here is on the LAST required step -- the paired via.
    for reason, count in budget.reasons.items():
        reached, _, steps = reason.removeprefix("stalled_at_step_").partition("_of_")
        assert int(reached) == int(steps) - 1, (reason, count)
    assert budget.native_rejections["via_blocked_p"] > 0


def test_proposals_label_the_goal_preferred_direction_first():
    """``departure_proposals`` sorts toward-goal first; the label must agree."""
    finder = _finder()
    proposals = list(departure_proposals(finder, _public_pads(finder)))
    outward_by_direction = {p.direction: set() for p in proposals}
    for p in proposals:
        outward_by_direction[p.direction].add(p.outward)
    assert set(outward_by_direction) == {DIRECTION_TOWARD_GOAL, DIRECTION_AWAY_FROM_GOAL}
    # Each label names exactly one outward vector -- the frame never mixes
    # directions under one label.
    assert all(len(v) == 1 for v in outward_by_direction.values())
    assert (
        outward_by_direction[DIRECTION_TOWARD_GOAL]
        != outward_by_direction[DIRECTION_AWAY_FROM_GOAL]
    )


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_one_blocked_direction_is_named_not_averaged_away():
    """#5333: a sibling pair's signature -- one direction clean, one dead.

    Foreign copper exactly at the toward-goal via sites (only) reproduces
    what MIPI_CLK / MIPI_DAT0 / TMDS_D0-D2 actually measured on board 07:
    every proposal in ONE direction validates, every proposal in the OTHER
    does not.  The un-split ``proposals_validated`` count alone cannot tell
    this apart from every proposal failing for unrelated reasons.
    """
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    _foreign_wall(finder, [(10, 2), (22, 2)])
    budget = DepartureBudget(time.monotonic() + 60, 512)
    departures = list(validated_departures(finder, _public_pads(finder), budget))
    assert departures and all(d.proposal.direction == DIRECTION_AWAY_FROM_GOAL for d in departures)
    assert budget.direction_seen == {DIRECTION_TOWARD_GOAL: 6, DIRECTION_AWAY_FROM_GOAL: 6}
    assert budget.direction_validated == {DIRECTION_AWAY_FROM_GOAL: 6}


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_both_blocked_directions_are_distinguishable_from_one():
    """#5333: MIPI_DAT1's actual signature -- BOTH directions dead.

    Re-measured on board 07 seed 42 (native build 31): fanning the escape
    depth out to 8x the base distance with 4x the native allowance still
    stalled every proposal, at every depth, on both escape directions --
    ruling out "one via site is occupied" as the explanation.  This control
    reproduces that signature in miniature and asserts the ledger states it
    directly rather than requiring a manual replay to notice.
    """
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    _foreign_wall(finder, [(10, 2), (22, 2), (10, 18), (22, 18)])
    budget = DepartureBudget(time.monotonic() + 60, 512)
    assert list(validated_departures(finder, _public_pads(finder), budget)) == []
    assert budget.direction_seen == {DIRECTION_TOWARD_GOAL: 6, DIRECTION_AWAY_FROM_GOAL: 6}
    assert budget.direction_validated == Counter()


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_a_starved_allowance_is_not_reported_as_a_blocked_escape():
    """An unfinished proof is not evidence the escape is illegal."""
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    budget = DepartureBudget(time.monotonic() + 60, 4)
    assert list(validated_departures(finder, _public_pads(finder), budget)) == []
    assert budget.proposals_seen >= 1
    assert budget.reasons == {
        "iteration_limited_at_step_2_of_9": 1,
        "allowance_spent_before_attempt": 1,
    }
    # None of these is a ``stalled_at_step_*`` claim: the escapes may well be
    # legal, the search simply was not allowed to finish proving it.
    assert not any(r.startswith("stalled_at_step_") for r in budget.reasons)


def test_an_expired_deadline_is_charged_to_the_allowance_not_the_geometry(monkeypatch):
    finder = _finder()
    monkeypatch.setattr(
        finder, "route_coupled", lambda *a, **kw: pytest.fail("expired native call")
    )
    budget = DepartureBudget(0, 100)
    assert list(validated_departures(finder, _public_pads(finder), budget)) == []
    assert budget.proposals_seen == 1
    assert budget.reasons == {"allowance_spent_before_attempt": 1}


def test_the_departure_summary_reports_most_frequent_first():
    budget = DepartureBudget(1, 1)
    budget.proposals_seen = 12
    budget.proposals_validated = 2
    budget.reasons.update({"stalled_at_step_0_of_9": 4, "stalled_at_step_3_of_15": 6})
    budget.native_rejections.update({"sym_blocked_p": 3, "via_blocked_p": 40})
    assert budget.stage_summary() == (
        "proposals=12 validated=2 "
        "departure_reasons={'stalled_at_step_3_of_15': 6, 'stalled_at_step_0_of_9': 4} "
        "departure_rejections={'via_blocked_p': 40, 'sym_blocked_p': 3} "
        "departure_directions={}"
    )
