"""Issue #6499: a coupled search must reach an SMD goal pad on the pad's layer.

Both coupled searches (Python ``route_coupled`` and C++
``CoupledPathfinder::route``) used to accept a joint state as the goal when
each rail's x/y matched its goal pad, ignoring the layer.  The returned route
then ended on whatever layer the path was on -- an open on an SMD pad.
Through-hole goal pads stay layer-agnostic.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

BACKENDS = [
    pytest.param("cpp", id="cpp", marks=pytest.mark.skipif(
        not is_cpp_available(), reason="C++ backend not available")),
    pytest.param("python", id="python"),
]  # fmt: skip


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.3,
        via_clearance=0.05,
        grid_resolution=0.1,
        min_hole_to_hole=0.5,
    )


def _run(monkeypatch, backend: str, *, four_layer: bool, through_hole: bool = False):
    """Pair crossing a wall at x=3.0 that blocks every layer but one."""
    monkeypatch.setenv("KCT_COUPLED_CPP", "1" if backend == "cpp" else "0")
    rules = _rules()
    if four_layer:
        stack = LayerStack.four_layer_all_signal()
        walled = [Layer.F_CU, Layer.B_CU, Layer.IN2_CU]
    else:
        stack = LayerStack.two_layer()
        walled = [Layer.F_CU]
    grid = RoutingGrid(width=6.0, height=3.0, rules=rules, layer_stack=stack)
    grid.add_keepout(3.0, 0.0, 3.0, 3.0, layers=walled)

    def pad(x: float, y: float, net: int, name: str, ref: str, goal: bool) -> Pad:
        th = through_hole and goal
        return Pad(
            x=x, y=y, width=0.2, height=0.2, net=net, net_name=name, layer=Layer.F_CU,
            ref=ref, pin="1", through_hole=th, drill=0.1 if th else 0.0,
        )  # fmt: skip

    p_start, p_end = pad(1.0, 1.2, 1, "D_P", "A", False), pad(5.0, 1.2, 1, "D_P", "B", True)
    n_start, n_end = pad(1.0, 1.8, 2, "D_N", "C", False), pad(5.0, 1.8, 2, "D_N", "D", True)
    for item in (p_start, p_end, n_start, n_end):
        grid.add_pad(item)
    pf = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=6, min_spacing_cells=6)
    pf._use_cpp_coupled = backend == "cpp"
    result = pf.route_coupled(p_start, p_end, n_start, n_end, max_iterations_budget=200000)
    assert pf.last_coupled_backend == backend
    pads = {1: [p_start, p_end], 2: [n_start, n_end]}
    return pf, result, pads


def _assert_connected(result, pads) -> None:
    conn = validate_net_connectivity(list(result), pads)
    assert conn[1]["connected"] and conn[2]["connected"], conn


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("four_layer", [False, True], ids=["two-layer", "four-layer"])
def test_smd_goal_never_returns_an_open_pair(monkeypatch, backend, four_layer):
    pf, result, pads = _run(monkeypatch, backend, four_layer=four_layer)
    if result is not None:
        _assert_connected(result, pads)


@pytest.mark.parametrize("four_layer", [False, True], ids=["two-layer", "four-layer"])
@pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not available")
def test_backends_agree_on_smd_goal(monkeypatch, four_layer):
    cpp_pf, cpp_result, _ = _run(monkeypatch, "cpp", four_layer=four_layer)
    py_pf, py_result, _ = _run(monkeypatch, "python", four_layer=four_layer)
    assert (cpp_result is None) == (py_result is None)
    if cpp_result is not None:
        for c, p in zip(cpp_result, py_result, strict=True):
            assert [(v.x, v.y) for v in c.vias] == [(v.x, v.y) for v in p.vias]
    assert cpp_pf.last_iterations == py_pf.last_iterations


@pytest.mark.parametrize("backend", BACKENDS)
def test_through_hole_goal_stays_layer_agnostic(monkeypatch, backend):
    """Through-hole goal pads are reachable on any layer: the route still lands."""
    pf, result, pads = _run(monkeypatch, backend, four_layer=False, through_hole=True)
    assert result is not None, "a through-hole goal must still route"
    _assert_connected(result, pads)
    # ... and it is accepted on the layer it arrived on (B.Cu), not forced up.
    assert result[0].segments[-1].layer == Layer.B_CU
