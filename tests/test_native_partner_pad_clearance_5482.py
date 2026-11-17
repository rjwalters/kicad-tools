"""Differential partner routing must retain authored static pad clearance."""

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


def context():
    rules = DesignRules(grid_resolution=0.05, trace_width=0.25, trace_clearance=0.15)
    grid = RoutingGrid(10, 10, rules=rules)
    grid.add_pad(Pad(5, 5, 0.3, 0.6, 2, "PARTNER", ref="J1", pin="A6"), pin_pitch=1)
    native = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(native, rules)
    pathfinder.set_routable_layers([0])
    return grid, native, pathfinder


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [0, 6])
def test_partner_pad_keeps_scalar_clearance(sharing, radius):
    _, native, pathfinder = context()
    # Board06's first rejected edge translated onto a compact fixture: its
    # 0.144258 mm copper gap is below the authored 0.150 mm pad floor.
    x, y = native._impl.world_to_grid(5.25, 5.55)
    assert pathfinder._impl.is_trace_blocked(x, y, 0, 1, sharing, radius, 2, 4)


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [0, 6])
def test_same_net_pad_and_legal_partner_distance_stay_passable(sharing, radius):
    _, native, pathfinder = context()
    x, y = native._impl.world_to_grid(5.25, 5.55)
    assert not pathfinder._impl.is_trace_blocked(x, y, 0, 2, sharing, radius, 1, 4)
    x, y = native._impl.world_to_grid(6, 6)
    assert not pathfinder._impl.is_trace_blocked(x, y, 0, 1, sharing, radius, 2, 4)


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [0, 6])
@pytest.mark.parametrize("static_flag", ["static_blocked", "pad_blocked", "is_obstacle"])
def test_dynamic_partner_copper_keeps_tighter_radius(sharing, radius, static_flag):
    rules = DesignRules(grid_resolution=0.05, trace_width=0.25, trace_clearance=0.15)
    native = CppGrid.from_routing_grid(RoutingGrid(10, 10, rules=rules))
    pathfinder = CppPathfinder(native, rules)
    cell = native._impl.at(100, 100, 0)
    cell.blocked = True
    cell.net = 2
    cell.usage_count = 0
    cell.static_blocked = False
    cell.pad_blocked = False
    cell.is_obstacle = False
    # Authored pad/keepout occupancy is not a routed partner trace.
    setattr(cell, static_flag, True)
    assert pathfinder._impl.is_trace_blocked(105, 100, 0, 1, sharing, radius, 2, 4)
    setattr(cell, static_flag, False)
    # Five cells from partner copper: outside the tighter radius four,
    # inside the scalar radius six. The dynamic exemption remains active.
    assert not pathfinder._impl.is_trace_blocked(105, 100, 0, 1, sharing, radius, 2, 4)
    assert pathfinder._impl.is_trace_blocked(105, 100, 0, 1, sharing, radius, -1, 0)


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_native_search_routes_around_partner_pad_with_valid_copper(method, sharing):
    _, native, pathfinder = context()
    result = getattr(pathfinder._impl, method)(
        3,
        5.55,
        0,
        7,
        5.55,
        0,
        1,
        start_layers=[0],
        end_layers=[0],
        negotiated_mode=sharing,
        trace_radius_cells=6,
        partner_net=2,
        intra_pair_radius_cells=4,
        emit_trace_width=0.25,
        max_search_iterations=20000,
    )
    assert result.success
    validation = native._impl.validate_route(
        result.segments,
        result.vias,
        1,
        [],
        0.15,
        0.2,
        0.25,
        partner_net=2,
        intra_pair_clearance=0.075,
    )
    assert validation.valid, (validation.violation_type, validation.min_clearance)


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [0, 6])
def test_board06_captured_partner_pad_edge_is_blocked(sharing, radius):
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.25,
        via_diameter=0.45,
        manufacturer="jlcpcb",
        min_trace_width=0.1016,
    )
    grid = RoutingGrid(6, 6, rules=rules, origin_x=110, origin_y=53)
    grid.add_pad(Pad(113, 55.5, 0.3, 0.6, 6, "USB2_D+", ref="J1", pin="A6"), pin_pitch=1)
    native = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(native, rules)
    # Board06 uses a 0.15mm board default and a 0.25mm USB net-class
    # trace (explicit radius six). Preserve both grid and route context.
    # Preserve the original world coordinates and float32 endpoint from the
    # rejected Board06 USB2_D- route. Translating changes raster rounding.
    x, y = native._impl.world_to_grid(113.25, 56.04999923706055)
    assert pathfinder._impl.is_trace_blocked(x, y, 0, 7, sharing, radius, 6, 4)
