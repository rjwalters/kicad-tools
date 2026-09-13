"""A departure prefix constrains real expansion without bypassing its guards."""

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from tests.test_diffpair_coupled_endpoint_via_guard import _finder

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")


def _prefix():
    return [(10, y, 0, 22, y, 0) for y in range(9, 5, -1)] + [(10, 6, 3, 22, 6, 3)]


def _route(finder, prefix=None, budget=1000, corridor=None):
    return finder._get_cpp_coupled_impl().route(
        p_start_xy=(10, 10),
        n_start_xy=(22, 10),
        start_layer=0,
        p_goal_xy=(10, 10),
        n_goal_xy=(22, 10),
        end_layer=3,
        p_net=1,
        n_net=2,
        effective_target_spacing=12,
        effective_approach_radius=6,
        effective_departure_radius=14,
        routable_layers=list(finder.grid.get_routable_indices()),
        corridor_bitset=corridor or [],
        max_iterations_budget=budget,
        timeout_seconds=1.0,
        departure_prefix=prefix,
    )


def test_valid_departure_keeps_original_start_and_via_history():
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    prefix = _prefix()
    path, _ = _route(finder, prefix)
    assert path is not None
    assert path[0][:6] == (10, 10, 0, 22, 10, 0)
    assert [step[:6] for step in path[1 : 1 + len(prefix)]] == prefix
    assert path[len(prefix)][6] is True
    assert path[-1][:6] == (10, 10, 3, 22, 10, 3)


@pytest.mark.parametrize(
    "obstruction",
    ["jump", "self_trail", "partner_barrel", "foreign_barrel", "unsupported_pad", "corridor"],
)
def test_departure_cannot_bypass_normal_constraints(obstruction):
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    prefix = _prefix()
    corridor = None
    if obstruction == "jump":
        prefix = [prefix[2]]
    elif obstruction == "self_trail":
        prefix = [prefix[0], prefix[1], prefix[0]]
    elif obstruction == "partner_barrel":
        prefix += [(x, 6, 3, 22, 6, 3) for x in range(11, 19)]
    elif obstruction == "foreign_barrel":
        finder.grid._blocked[1, 6, 10] = True
        finder.grid._net[1, 6, 10] = 99
    elif obstruction == "unsupported_pad":
        prefix = [(10, 10, 3, 22, 10, 3)]
    else:
        corridor = [0] * (finder.grid.rows * finder.grid.cols)
    path, _ = _route(finder, prefix, corridor=corridor)
    assert path is None


def test_prefix_steps_consume_original_iteration_budget():
    path, diagnostics = _route(_finder(), _prefix(), budget=4)
    assert path is None
    assert diagnostics["iteration_limited"]
    assert diagnostics["iterations"] == 4


def test_empty_prefix_preserves_default_search():
    finder = _finder()
    assert _route(finder) == _route(finder, [])
