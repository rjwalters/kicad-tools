"""Cached native via policies follow explicit legacy carve-out changes."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("foreign_net", [0, 2])
def test_cached_native_via_policy_tracks_legacy_toggle(foreign_net):
    if not is_cpp_available():
        pytest.skip("native backend unavailable; requires current native build")
    rules = DesignRules(trace_width=0.1, trace_clearance=0.15, grid_resolution=0.05)
    grid = RoutingGrid(10, 10, rules)
    foreign = Pad(5, 5, 0.3, 0.3, foreign_net, "FOREIGN", ref="U1", pin="1")
    own = Pad(5, 5.5, 0.3, 0.3, 1, "SIGNAL", ref="U1", pin="2")
    for pad in (foreign, own):
        grid.add_pad(pad)
    cpp = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(cpp, rules)
    via = Via(5.35, 5, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1)
    route = Route(1, "SIGNAL", vias=[via])
    # Real copper gap is0.10mm, below the authored0.15mm floor. Default
    # acceptance rejects it, while the explicit legacy option permits it.
    deficit, _ = grid.worst_via_pad_deficit(via, 1, exclude_refs={"U1"})
    assert deficit == pytest.approx(0.05)
    pitches = None
    for legacy in (False, True, False, True):
        rules.legacy_fine_pitch_carveout = legacy
        native_valid = pathfinder._validate_route_clearance(route, own, own, 1) is None
        assert native_valid is legacy
        deficit, _ = grid.worst_via_pad_deficit(via, 1, exclude_refs={"U1"})
        assert (deficit == 0) is legacy
        if pitches is None:
            pitches = grid._component_pitch_cache
        else:
            assert grid._component_pitch_cache is pitches
    # Strict mode still wins over explicit legacy opt-in.
    rules.strict_pad_clearance = True
    assert pathfinder._validate_route_clearance(route, own, own, 1) is not None
