"""Authored pad clearance must not widen an already inflated raster halo twice."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("sharing", [False, True])
def test_authored_pad_corridor_remains_reachable(sharing, monkeypatch):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.05,
        net_clearance_floors={1: 0.2, 2: 0.2, 3: 0.2},
    )
    grid = RoutingGrid(10, 10, rules)
    for net, y in ((2, 4.5), (1, 5.0), (3, 5.5)):
        grid.add_pad(Pad(5, y, 1.475, 0.3, net, str(net), ref="U1", pin=str(net)))
    router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    monkeypatch.setattr(router, "_try_python_fallback", lambda *args, **kwargs: None)
    start = Pad(4.3, 5, 0.14, 0.14, 1, "signal", ref="U1", escape_terminal=True)
    end = Pad(7, 5, 0.1, 0.1, 1, "signal")
    # This straight escape has 0.25 mm copper clearance to adjacent pads,
    # exceeding the mandatory 0.20 mm floor. Confirm the witness independently
    # of search before asserting reachability.
    witness = Route(1, "signal", segments=[Segment(4.3, 5, 7, 5, 0.2, Layer.F_CU, 1)])
    assert router._validate_route_clearance(witness, start, end, 5) is None
    result = router.route(start, end, negotiated_mode=sharing)
    assert result is not None, router.get_last_failure_info()
    assert router._validate_route_clearance(result, start, end, 5) is None


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("floor", [0.251, 0.3])
def test_pad_corridor_witness_preserves_stricter_authored_floor(floor):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.05,
        net_clearance_floors={1: floor, 2: floor, 3: floor},
    )
    grid = RoutingGrid(10, 10, rules)
    for net, y in ((2, 4.5), (1, 5.0), (3, 5.5)):
        grid.add_pad(Pad(5, y, 1.475, 0.3, net, str(net), ref="U1", pin=str(net)))
    router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    start = Pad(4.3, 5, 0.14, 0.14, 1, "signal", ref="U1", escape_terminal=True)
    end = Pad(7, 5, 0.1, 0.1, 1, "signal")
    witness = Route(1, "signal", segments=[Segment(4.3, 5, 7, 5, 0.2, Layer.F_CU, 1)])
    assert router._validate_route_clearance(witness, start, end, 5) is not None


@pytest.mark.skipif(not is_cpp_available(), reason="native build required")
@pytest.mark.parametrize("sharing", [False, True])
def test_pad_approach_cannot_bypass_authored_floor(sharing, monkeypatch):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.05,
        net_clearance_floors={1: 0.2, 2: 0.2, 3: 0.2},
    )
    grid = RoutingGrid(10, 10, rules)
    for net, y in ((2, 4.5), (1, 5.0), (3, 5.5)):
        grid.add_pad(Pad(5, y, 1.475, 0.3, net, str(net), ref="U1", pin=str(net)))
    router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    monkeypatch.setattr(router, "_try_python_fallback", lambda *args, **kwargs: None)
    start = Pad(4.3, 5, 0.14, 0.14, 1, "signal", ref="U1", escape_terminal=True)
    end = Pad(7, 5.2, 0.1, 0.1, 1, "signal")
    original = router._validate_route_clearance
    rejected = []

    def validate(*args, **kwargs):
        result = original(*args, **kwargs)
        if result is not None:
            rejected.append(result)
        return result

    monkeypatch.setattr(router, "_validate_route_clearance", validate)
    result = router.route(start, end, negotiated_mode=sharing)
    assert result is not None, router.get_last_failure_info()
    assert rejected == [], "search admitted an authored pad clearance violation"
