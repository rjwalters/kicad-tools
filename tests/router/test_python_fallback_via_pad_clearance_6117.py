"""Issue #6117: the pure-Python route validator must check vias against pads.

On board 03 the C++ validator rejected a BTN1 via 0.125 mm from BTN2's
pad ``U1.19`` (0.15 mm required).  The pure-Python A* fallback then
committed the same via, because ``Router._validate_route_clearance``
checked vias against segments, vias, drills and holes but never against
pads.  These tests pin the via-vs-foreign-pad quadrant on the Python
validator.  They put the foreign pad on the SAME component as the routed
net's own pad, which is the board-03 geometry, and check that the verdict
matches the C++ ``Grid3D::validate_route`` via-pad branch.
"""

from dataclasses import replace

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Via
from kicad_tools.router.rules import DesignRules

VIA_DIAMETER = 0.6
FOREIGN_Y = 5.0
PAD_HALF_HEIGHT = 0.15


def _fixture():
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_diameter=VIA_DIAMETER,
        via_drill=0.3,
    )
    grid = RoutingGrid(width=10, height=10, rules=rules)
    # U1.1 is the routed net's own pad; U1.2 is a FOREIGN-net neighbour on
    # the same component (board 03: BTN1's U1.18 next to BTN2's U1.19).
    start = Pad(
        x=5,
        y=4.5,
        width=1.475,
        height=0.3,
        net=1,
        net_name="BTN1",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
    )
    foreign = replace(start, y=FOREIGN_Y, net=2, net_name="BTN2", pin="2")
    end = replace(start, x=8, y=8, ref="J1", pin="1", width=0.6, height=0.6)
    for pad in (start, foreign, end):
        grid.add_pad(pad)
    return rules, grid, start, end


def _via_at_gap(gap: float) -> Via:
    """A BTN1 via directly below U1.2, ``gap`` mm edge-to-edge from its copper."""
    y = FOREIGN_Y + PAD_HALF_HEIGHT + VIA_DIAMETER / 2 + gap
    return Via(
        x=5,
        y=y,
        drill=0.3,
        diameter=VIA_DIAMETER,
        layers=(Layer.F_CU, Layer.B_CU),
        net=1,
        net_name="BTN1",
    )


def _python_accepts(router: Router, via: Via, start: Pad, end: Pad) -> bool:
    route = Route(net=1, net_name="BTN1", vias=[via])
    # Same exclusion set ``Router._reconstruct_route`` builds (#1764).
    return router._validate_route_clearance(
        route,
        1,
        component_pitches=router.component_pitches,
        exclude_refs={start.component_key, end.component_key},
    )


def _cpp_accepts(grid: RoutingGrid, rules: DesignRules, via: Via, start: Pad, end: Pad) -> bool:
    backend = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    route = Route(net=1, net_name="BTN1", vias=[via])
    return backend._validate_route_clearance(route, start, end, 2) is None


def test_python_validator_rejects_subclearance_via_next_to_same_component_foreign_pad():
    """The board-03 shape: a 0.125 mm via-pad gap against 0.15 mm required."""
    rules, grid, start, end = _fixture()
    via = _via_at_gap(0.125)
    deficit, loc = grid.worst_via_pad_deficit(via, 1, exclude_refs={"U1", "J1"})
    assert deficit == pytest.approx(0.025)
    assert loc == (5, FOREIGN_Y)

    router = Router(grid, rules)
    assert _python_accepts(router, via, start, end) is False


def test_python_validator_accepts_via_at_full_clearance():
    """Control: the new check does not over-reject a via that clears the pad."""
    rules, grid, start, end = _fixture()
    router = Router(grid, rules)
    assert _python_accepts(router, _via_at_gap(0.2), start, end) is True


@pytest.mark.parametrize("gap", [0.125, 0.2])
@pytest.mark.parametrize("relaxed", [False, True])
def test_python_and_cpp_via_pad_verdicts_agree(gap, relaxed):
    """Python and C++ give the same via verdict, including under the #2452
    corridor relief.  A relaxed ref is "skip" mode in both validators, so
    a positive-gap via is exempt in both."""
    if not is_cpp_available():
        pytest.skip("C++ backend unavailable")
    rules, grid, start, end = _fixture()
    if relaxed:
        grid._relaxed_clearance_refs.add("U1")
    via = _via_at_gap(gap)
    router = Router(grid, rules)
    py_ok = _python_accepts(router, via, start, end)
    cpp_ok = _cpp_accepts(grid, rules, via, start, end)
    assert py_ok == cpp_ok
    assert py_ok is (gap >= 0.15 or relaxed)
