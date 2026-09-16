"""The coupled search must be able to reach a layer change at all (#5333).

A coupled via move places BOTH barrels at the pair's CURRENT separation, so it
is legal only once that separation has reached the mutual barrel pitch
(``max(via_diameter + via_clearance, via_drill + min_hole_to_hole)``).  Every
planar move -- symmetric and asymmetric alike -- pins the separation to the
coupled target +-1 outside the endpoint relaxation radii, so before #5333 a
pair whose coupled target is narrower than that barrel pitch (every fine-pitch
pair) could not reach a via anywhere: the search stayed on its start layer for
its entire budget and reported the landing-stall / plateau taxonomy instead.

These are behaviour controls on an otherwise EMPTY four-layer board, so no
congestion, corridor or foreign-copper effect can explain the outcome.
"""

from types import SimpleNamespace

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")

# 0.6 mm pad pitch against a 0.8 mm mutual barrel pitch: the fan-out the pair
# needs before a paired via exceeds both its pad pitch and its coupled target.
_START_PITCH_MM = 0.6
_GOAL_OFFSET_MM = 1.5
_GOAL_TRAVEL_MM = 8.0


def fixture(*, barrier: bool = False):
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.15,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        manufacturer="jlcpcb",
    )
    nc = NetClassRouting(
        name="pair",
        trace_width=0.15,
        clearance=0.15,
        skew_tolerance_mm=0.05,
        coupled_continuity_threshold=0.85,
        coupled_routing=True,
    )
    auto = Autorouter(
        width=16,
        height=16,
        rules=rules,
        net_class_map={"P": nc, "N": nc},
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    finder = CoupledPathfinder(
        auto.grid,
        rules,
        target_spacing_cells=4,
        min_spacing_cells=3,
        net_class_map={"P": nc, "N": nc},
    )
    auto.net_class_map = finder.net_class_map
    pads = tuple(
        Pad(x=x, y=y, width=0.3, height=0.3, layer=Layer.F_CU, net=net, net_name=name)
        for net, name, x, y in (
            (1, "P", 2.0, 2.0),
            (1, "P", 2.0 + _GOAL_OFFSET_MM, 2.0 + _GOAL_TRAVEL_MM),
            (2, "N", 2.0, 2.0 + _START_PITCH_MM),
            (2, "N", 2.0 + _GOAL_OFFSET_MM, 2.0 + _START_PITCH_MM + _GOAL_TRAVEL_MM),
        )
    )
    for pad in pads:
        auto.grid.add_pad(pad)
    if barrier:
        # A single F.Cu trace spanning the board between source and goal. Any
        # route now REQUIRES a layer change; the inner layers stay empty.
        wall = Route(
            net=99,
            net_name="BARRIER",
            segments=[
                Segment(
                    x1=0.2,
                    y1=6.0,
                    x2=15.8,
                    y2=6.0,
                    width=0.4,
                    layer=Layer.F_CU,
                    net=99,
                    net_name="BARRIER",
                )
            ],
        )
        auto.grid.mark_route(wall)
        auto.routes.append(wall)
    pair = SimpleNamespace(
        name="PAIR",
        positive=SimpleNamespace(net_id=1, net_name="P"),
        negative=SimpleNamespace(net_id=2, net_name="N"),
    )
    return auto, finder, pair, pads


def test_fan_out_lets_a_fine_pitch_pair_land_on_an_empty_board():
    """Before #5333 this exhausted 20,000 iterations without a route."""
    _, finder, _, pads = fixture()
    assert finder.target_spacing_cells < finder._minimum_via_pitch_cells()
    assert finder.route_coupled(*pads, timeout_seconds=60.0, max_iterations_budget=20000)
    assert not finder.last_timeout_exceeded


def test_endpoint_relaxation_admits_the_mutual_barrel_pitch():
    """The relaxed band must reach the barrel pitch, not just the pad pitch.

    The radii are locals of ``route_coupled``; observe them through the native
    call it makes, which is the value the joint search actually enforces.
    """
    _, finder, _, pads = fixture()
    seen = {}
    original = finder._try_cpp_route_coupled

    def record(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    finder._try_cpp_route_coupled = record
    finder.route_coupled(*pads, timeout_seconds=30.0, max_iterations_budget=2000)
    required = finder._minimum_via_pitch_cells() - finder.target_spacing_cells
    for key in ("effective_approach_radius", "effective_departure_radius"):
        assert seen[key] >= required, (key, seen[key], required)


def test_joint_search_cannot_cross_a_barrier_that_requires_a_via():
    """Layer confinement, measured: the reachable space excludes the route.

    This is the board-07 failure in miniature -- and it is not a budget
    problem: 10x the iterations reports the same layer-confined outcome.
    """
    for budget in (20000, 200000):
        _, finder, _, pads = fixture(barrier=True)
        assert (
            finder.route_coupled(*pads, timeout_seconds=120.0, max_iterations_budget=budget) is None
        )
        path = finder.last_best_cpp_path or []
        assert path, "expected a saved partial path"
        start_layer = finder.grid.layer_to_index(pads[0].layer.value)
        assert {step[2] for step in path} | {step[5] for step in path} == {start_layer}
        assert finder.last_rejections["via_pair_pitch"] > 0
