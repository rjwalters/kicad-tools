"""Candidate pair vias must clear each other before grid publication."""

import pytest

from kicad_tools.router.diffpair_routing import CoupledPathfinder, CoupledState, GridPos
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules


def _neighbors(dx, dy, *, allow_swap=False, endpoints=False, **rule_overrides):
    parameters = {
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "via_clearance": 0.2,
        "min_hole_to_hole": 0.5,
    }
    parameters.update(rule_overrides)
    rules = DesignRules(**parameters)
    grid = RoutingGrid(width=12, height=12, rules=rules, resolution_override=0.127)
    finder = CoupledPathfinder(grid, rules, target_spacing_cells=3, min_spacing_cells=3)
    finder.allow_swap_via = allow_swap
    state = CoupledState(GridPos(30, 30, 0), GridPos(30 + dx, 30 + dy, 0), (1, 0))
    kwargs = {"p_start": state.p_pos, "n_start": state.n_pos} if endpoints else {}
    return finder, finder._get_coupled_neighbors(state, 1, 2, **kwargs)


@pytest.mark.parametrize("allow_swap", [False, True])
@pytest.mark.parametrize("endpoints", [False, True])
def test_overlapping_candidate_vias_rejected_even_at_endpoints(allow_swap, endpoints):
    # Captured Board07 witness: sqrt(.254**2 + .381**2) = .457905 mm.
    # Two .6 mm barrels overlap; copper and hole rules require .8 mm pitch.
    finder, neighbors = _neighbors(2, 3, allow_swap=allow_swap, endpoints=endpoints)
    assert not any(is_via for _, _, is_via in neighbors)
    assert finder.last_rejections["via_pair_pitch"] > 0
    assert neighbors  # This guard only rejects the unsafe layer transitions.


@pytest.mark.parametrize("allow_swap", [False, True])
def test_clear_candidate_vias_permit_layer_change(allow_swap):
    # .889 mm clears both the .8 mm copper and hole pitch requirements.
    _, neighbors = _neighbors(7, 0, allow_swap=allow_swap)
    assert any(is_via for _, _, is_via in neighbors)


@pytest.mark.parametrize("dx,allowed", [(7, False), (9, True)])
def test_hole_clearance_can_determine_candidate_via_pitch(dx, allowed):
    # Copper needs .6 mm; .3 mm holes plus .7 mm edge clearance need 1 mm.
    finder, neighbors = _neighbors(
        dx, 0, via_diameter=0.4, via_drill=0.3, via_clearance=0.2, min_hole_to_hole=0.7
    )
    assert finder._minimum_via_pitch_cells() == pytest.approx(1.0 / 0.127)
    assert any(is_via for _, _, is_via in neighbors) is allowed


@pytest.mark.parametrize(
    "dx,dy,rule_overrides,allowed",
    [
        (2, 3, {}, False),
        (7, 0, {}, True),
        (7, 0, {"via_diameter": 0.4, "min_hole_to_hole": 0.7}, False),
        (9, 0, {"via_diameter": 0.4, "min_hole_to_hole": 0.7}, True),
    ],
)
def test_native_layer_transition_obeys_mutual_via_pitch(dx, dy, rule_overrides, allowed):
    from kicad_tools.router.cpp_backend import CppCoupledPathfinder, CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("requires the matching native router backend")
    finder, _ = _neighbors(dx, dy, **rule_overrides)
    grid = finder.grid
    impl = CppCoupledPathfinder(
        CppGrid.from_routing_grid(grid),
        finder.rules,
        target_spacing_cells=3,
        min_spacing_cells=3,
        trace_half_width_cells=finder._trace_half_width_cells,
        via_extra_cells=finder._via_extra_cells,
        via_drill_cells=2,
        spacing_penalty_factor=finder.spacing_penalty_factor,
        heuristic_weight=finder.heuristic_weight,
    )
    # Only the endpoint cells are in the corridor: the pair cannot fan out
    # to evade a too-small pitch. XY matches already, but goal layer does not.
    corridor = [0] * (grid.cols * grid.rows)
    for x, y in [(30, 30), (30 + dx, 30 + dy)]:
        corridor[y * grid.cols + x] = 1
    path, diagnostics = impl.route(
        p_start_xy=(30, 30),
        n_start_xy=(30 + dx, 30 + dy),
        start_layer=0,
        p_goal_xy=(30, 30),
        n_goal_xy=(30 + dx, 30 + dy),
        end_layer=1,
        p_net=1,
        n_net=2,
        effective_target_spacing=3,
        effective_approach_radius=6,
        effective_departure_radius=6,
        routable_layers=[0, 1],
        corridor_bitset=corridor,
        max_iterations_budget=100,
        timeout_seconds=1.0,
    )
    assert (path is not None) is allowed
    if allowed:
        assert path[-1][2] == path[-1][5] == 1
        assert path[-1][6]
    else:
        assert diagnostics["rejections"]["via_pair_pitch"] > 0
