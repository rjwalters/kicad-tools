"""Authored via minima survive raster bounds and cached placement answers."""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("strict_net", [1, 2])
@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("kind", ["pad", "trace", "via", "fill"])
def test_late_authored_floor_rejects_cached_via(strict_net, sharing, kind):
    rules = DesignRules(
        grid_resolution=0.1,
        trace_clearance=0.1,
        via_clearance=0.1,
        via_diameter=0.2,
        via_drill=0.1,
        min_hole_to_hole=0.1,
    )
    grid = RoutingGrid(10, 10, rules)
    if kind == "pad":
        grid.add_pad(Pad(5, 5, 0.2, 0.2, 2, "foreign"))
    elif kind == "trace":
        grid.mark_route(Route(2, "foreign", segments=[Segment(4, 5, 6, 5, 0.2, Layer.F_CU, 2)]))
    elif kind == "fill":
        from shapely.geometry import box

        from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles

        grid.fixed_fills = FixedFillObstacles(
            (FixedFill("foreign", 2, 0, 0.1, box(4.9, 4.9, 5.1, 5.1)),)
        )
    else:
        grid.mark_route(
            Route(2, "foreign", vias=[Via(5, 5, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 2)])
        )
    router = Router(grid, rules)
    gx, gy = grid.world_to_grid(5, 6)
    assert router._check_via_placement_cached(gx, gy, 1, sharing)
    rules.net_clearance_floors = {strict_net: 0.9, 3: 4.0}
    assert not router._check_via_placement_cached(gx, gy, 1, sharing)
    assert router._is_via_blocked(gx, gy, 0, 1, sharing)
    rules.net_clearance_floors = {strict_net: 0.7, 3: 4.0}
    assert router._check_via_placement_cached(gx, gy, 1, sharing)


def test_through_via_checks_authored_pad_on_skipped_plane_layer():
    rules = DesignRules(
        grid_resolution=0.1,
        trace_clearance=0.1,
        via_clearance=0.1,
        via_diameter=0.2,
        via_drill=0.1,
        net_clearance_floors={2: 0.9},
    )
    grid = RoutingGrid(10, 10, rules, layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig())
    grid.add_pad(Pad(5, 5, 0.2, 0.2, 2, "foreign", layer=Layer.IN1_CU))
    router = Router(grid, rules)
    gx, gy = grid.world_to_grid(5, 6)
    assert not router._check_via_placement_cached(gx, gy, 1)


def test_authored_pad_gate_uses_emitted_netclass_via_diameter():
    from kicad_tools.router.rules import NetClassRouting

    rules = DesignRules(grid_resolution=0.1, via_diameter=0.2, net_clearance_floors={2: 0.7})
    grid = RoutingGrid(10, 10, rules)
    grid.add_pad(Pad(5, 5, 0.2, 0.2, 2, "foreign"))
    router = Router(grid, rules, net_class_map={"wide": NetClassRouting("wide", via_size=0.6)})
    router.set_net_name_to_id({"wide": 1, "foreign": 2})
    gx, gy = grid.world_to_grid(5, 6)
    assert not router._authored_via_clear(gx, gy, 1)
    assert router._authored_via_clear(gx, gy + 2, 1)


@pytest.mark.parametrize("kind", ["pad", "track"])
@pytest.mark.parametrize("strict_net", [1, 2])
def test_supplemental_foreign_geometry_retains_authored_identity(kind, strict_net):
    rules = DesignRules(
        grid_resolution=0.1, trace_clearance=0.1, via_clearance=0.1, via_diameter=0.2, via_drill=0.1
    )
    grid = RoutingGrid(10, 10, rules)
    router = Router(grid, rules)
    if kind == "pad":
        router.set_via_foreign_context(foreign_pads=[Pad(5, 5, 0.2, 0.2, 2, "foreign")])
    else:
        router.set_via_foreign_context(foreign_tracks=[Segment(4, 5, 6, 5, 0.2, Layer.F_CU, 2)])
    gx, gy = grid.world_to_grid(5, 6)
    assert router._check_via_placement_cached(gx, gy, 1)
    rules.net_clearance_floors = {strict_net: 0.9, 3: 4.0}
    assert not router._check_via_placement_cached(gx, gy, 1)
