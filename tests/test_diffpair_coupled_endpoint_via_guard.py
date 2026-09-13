"""Endpoint vias may overlap their own pad, never foreign copper on any layer."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder, CoupledState, GridPos
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

_P = (10, 10)
_N = (22, 10)


def _finder():
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=4,
        height=3,
        rules=rules,
        resolution_override=0.1,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    # Represent actual own-net endpoint pad metal. Its drill-cell exception
    # must remain available even though pad_blocked is set.
    for (x, y), net in [(_P, 1), (_N, 2)]:
        grid._blocked[0, y, x] = True
        grid._pad_blocked[0, y, x] = True
        grid._net[0, y, x] = net
    return CoupledPathfinder(grid, rules, target_spacing_cells=12, min_spacing_cells=3)


def _has_endpoint_transition(finder, backend):
    if backend in {"python", "python_swap"}:
        finder.allow_swap_via = backend == "python_swap"
        p = GridPos(*_P, 0)
        n = GridPos(*_N, 0)
        neighbors = finder._get_coupled_neighbors(
            CoupledState(p, n, (0, 0)),
            p_net=1,
            n_net=2,
            p_start=p,
            n_start=n,
            p_goal=GridPos(*_P, 3),
            n_goal=GridPos(*_N, 3),
        )
        expected_p, expected_n = (_N, _P) if finder.allow_swap_via else (_P, _N)
        return any(
            via
            and state.p_pos == GridPos(*expected_p, 3)
            and state.n_pos == GridPos(*expected_n, 3)
            for state, _, via in neighbors
        )

    if not is_cpp_available():
        pytest.skip("requires the matching native router backend")
    # Two isolated corridor cells disallow every planar move. Reaching the
    # same XY goals on B.Cu therefore requires the endpoint via transition;
    # the search cannot escape the blocker by choosing another via site.
    grid = finder.grid
    corridor = bytearray(grid.rows * grid.cols)
    for x, y in [_P, _N]:
        corridor[y * grid.cols + x] = 1
    path, _ = finder._get_cpp_coupled_impl().route(
        p_start_xy=_P,
        n_start_xy=_N,
        start_layer=0,
        p_goal_xy=_P,
        n_goal_xy=_N,
        end_layer=3,
        p_net=1,
        n_net=2,
        effective_target_spacing=12,
        effective_approach_radius=0,
        effective_departure_radius=0,
        routable_layers=list(grid.get_routable_indices()),
        corridor_bitset=list(corridor),
        max_iterations_budget=128,
        timeout_seconds=1.0,
    )
    return bool(path)


@pytest.mark.parametrize("backend", ["python", "python_swap", "cpp"])
def test_endpoint_via_allows_only_its_own_pad_exception(backend):
    finder = _finder()
    assert _has_endpoint_transition(finder, backend)
    # Outside the endpoint exception the same pad still prohibits drilling.
    assert finder._is_via_blocked(*_P, 1)
    assert finder._is_via_blocked(*_N, 2)


@pytest.mark.parametrize("backend", ["python", "python_swap", "cpp"])
@pytest.mark.parametrize("layer", range(4))
@pytest.mark.parametrize("leg", ["P", "N"])
@pytest.mark.parametrize("blocker", ["foreign_envelope", "foreign_drill_pad"])
def test_endpoint_via_rejects_foreign_copper_across_full_barrel(backend, layer, leg, blocker):
    finder = _finder()
    x, y = _P if leg == "P" else _N
    if blocker == "foreign_envelope":
        # The head itself stays clear; foreign copper lies only within the
        # additional via envelope. Destination trace checks alone miss it.
        x += finder._via_extra_cells
        finder.grid._blocked[layer, y, x] = True
    else:
        # Isolate pad-metal protection from the blocked-cell predicate.
        # Foreign drill-pad metal must be rejected even without a halo bit.
        finder.grid._blocked[layer, y, x] = False
        finder.grid._pad_blocked[layer, y, x] = True
    finder.grid._net[layer, y, x] = 99

    assert not _has_endpoint_transition(finder, backend)


@pytest.mark.parametrize("layer", range(4))
def test_cpp_grid_preserves_unblocked_foreign_pad_metadata(layer):
    if not is_cpp_available():
        pytest.skip("requires the matching native router backend")
    grid = _finder().grid
    x, y = _P
    # Same-component carve-outs can release blocked while retaining the
    # outer pad-metal band. Its drill prohibition and owner must survive.
    grid._blocked[layer, y, x] = False
    grid._pad_blocked[layer, y, x] = True
    grid._net[layer, y, x] = 99

    native_grid = CppGrid.from_routing_grid(grid)
    cell = native_grid._impl.at(x, y, layer)

    assert not cell.blocked
    assert cell.pad_blocked
    assert cell.net == 99
