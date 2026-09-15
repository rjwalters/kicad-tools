"""A coupled landing must clear a barrel created earlier in the same search."""

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("use_cpp", [False, True], ids=["python", "cpp"])
@pytest.mark.parametrize("goal_y,success", [(14, False), (17, True)])
def test_landing_clears_partner_source_barrel(use_cpp, goal_y, success):
    if use_cpp and not is_cpp_available():
        pytest.skip("requires the matching native router backend")
    rules = DesignRules(
        trace_width=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(width=4, height=3, rules=rules, resolution_override=0.1)
    # Confine departure to the two own-net pad envelopes. B.Cu is empty;
    # the source vias must clear foreign copper even at endpoint cells.
    grid._blocked[0, :, :] = True
    grid._is_obstacle[0, :, :] = True

    def pad(x, y, layer, net):
        wx, wy = grid.grid_to_world(x, y)
        return Pad(x=wx, y=wy, width=0.2, height=0.2, net=net, net_name=f"D{net}", layer=layer)

    finder = CoupledPathfinder(
        grid, rules, target_spacing_cells=10, min_spacing_cells=2, heuristic_weight=1.5
    )
    # The inflated via envelope must be legal, rather than depending on
    # the old endpoint exception that bypassed foreign-copper checks.
    radius = finder._via_extra_cells
    for x, net in ((10, 1), (20, 2)):
        grid._net[0, 10 - radius : 11 + radius, x - radius : x + radius + 1] = net
    finder._use_cpp_coupled = use_cpp
    result = finder.route_coupled(
        pad(10, 10, Layer.F_CU, 1),
        pad(20, goal_y, Layer.B_CU, 1),
        pad(20, 10, Layer.F_CU, 2),
        pad(30, goal_y, Layer.B_CU, 2),
        max_iterations_budget=20000,
    )
    # The P landing is .4mm or .7mm from N's earlier barrel. Its .6mm
    # clearance requirement still applies on B.Cu and at the goal endpoint.
    assert (result is not None) is success
    if result is not None:
        for route in result:
            assert route.vias
            assert route.segments[-1].layer == Layer.B_CU
