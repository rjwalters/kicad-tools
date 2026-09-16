"""Track corridors do not inherit a via-only drill-spacing reservation."""

import math

import pytest
from shapely.geometry import Point, Polygon, box

from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles


def native_model(fills):
    from kicad_tools.router.cpp_backend import router_cpp

    assert router_cpp.BUILD_VERSION == 44
    native = router_cpp.Grid3D(200, 200, 4, 0.1, -5.0, -5.0)
    for layer, clearance, rings, via in fills.native_polygons(include_via_clearance=True):
        native.add_fixed_fill(layer, clearance, rings, -1 if via is None else via)
    return native


@pytest.mark.parametrize("native", [False, True])
def test_legal_track_corridor_retains_stronger_via_exclusion(native):
    fills = FixedFillObstacles(
        (
            FixedFill(
                "GND",
                1,
                0,
                0.2,
                Point(0, 0).buffer(0.25 / math.cos(math.pi / 256), quad_segs=64),
                via_clearance=0.4,
            ),
        )
    )
    cpp = native_model(fills) if native else None

    def track(y):
        return (
            cpp.fixed_fill_clear(-1, y, 1, y, 0, 0.075, 0.225)
            if cpp
            else fills.segment_clear((-1, y), (1, y), 0, 0.075, 0.15)
        )

    def via(x):
        return (
            cpp.fixed_fill_clear(x, 0, x, 0, 0, 0.225, 0.375, True)
            if cpp
            else fills.via_clear((x, 0), (0,), 0.225, 0.15)
        )

    assert not track(0.5)
    assert track(0.55) and track(0.7)
    assert not via(0.75) and not via(0.8)
    assert via(0.9)


@pytest.mark.parametrize("via_floor", [None, 0.05, 0.4])
def test_python_native_holes_layers_diagonals_and_boundary_parity(via_floor):
    shape = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], holes=[[(1, 1), (3, 1), (3, 3), (1, 3)]])
    fills = FixedFillObstacles((FixedFill("GND", 1, 1, 0.2, shape, via_clearance=via_floor),))
    cpp = native_model(fills)
    for layer in (0, 1, 2):
        for radius in (0.0, 0.075, 0.225):
            for x in (-0.7, -0.4, -0.3002, -0.2998, 0, 1.2998, 1.3002, 2, 3, 4.3, 4.7):
                point = (x, 2)
                for via in (False, True):
                    expected = (
                        fills.via_clear(point, (layer,), radius, 0.15)
                        if via
                        else fills.segment_clear(point, (x + 0.1, 2.1), layer, radius, 0.15)
                    )
                    end = point if via else (x + 0.1, 2.1)
                    assert (
                        cpp.fixed_fill_clear(*point, *end, layer, radius, radius + 0.15, via)
                        == expected
                    )


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf")])
def test_invalid_via_floor_refused_by_both_engines(bad):
    from kicad_tools.router.cpp_backend import router_cpp

    with pytest.raises(ValueError):
        FixedFill("GND", 1, 0, 0.2, box(0, 0, 1, 1), via_clearance=bad)
    cpp = router_cpp.Grid3D(20, 20, 2, 0.1, 0, 0)
    with pytest.raises(ValueError):
        cpp.add_fixed_fill(0, 0.2, [[(0, 0), (1, 0), (1, 1), (0, 1)]], bad)


def test_legacy_native_api_retains_uniform_floor():
    from kicad_tools.router.cpp_backend import router_cpp

    fills = FixedFillObstacles((FixedFill("GND", 1, 0, 0.4, box(0, 0, 1, 1)),))
    cpp = router_cpp.Grid3D(20, 20, 2, 0.1, 0, 0)
    for layer, clearance, rings in fills.native_polygons():
        cpp.add_fixed_fill(layer, clearance, rings)
    assert not cpp.fixed_fill_clear(1.3, 0.5, 1.3, 0.5, 0, 0.05, 0.1)
    assert not cpp.fixed_fill_clear(1.3, 0.5, 1.3, 0.5, 0, 0.05, 0.1, True)


@pytest.mark.parametrize(
    "hole_copper,diameter,drill,clearance", [(0.25, 0.5, 0.3, 0.2), (0.6, 0.8, 0.4, 0.1)]
)
def test_plane_access_derives_track_floor_from_actual_drill_geometry(
    tmp_path, hole_copper, diameter, drill, clearance
):
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.plane_access import PlaneAccessPolicy, PlaneAccessTarget
    from kicad_tools.zones.pour_escape import EscapeRules
    from tests.test_plane_access import board as board_fixture

    path = board_fixture.__wrapped__(tmp_path)
    rules = EscapeRules(
        hole_copper=hole_copper, diameter=diameter, drill=drill, clearance=clearance
    )
    router, _ = load_pcb_for_routing(
        str(path),
        skip_nets=["GND", "POWER"],
        force_python=True,
        plane_access_policy=PlaneAccessPolicy((PlaneAccessTarget("GND", "In1.Cu"),), rules),
    )
    annulus = (diameter - drill) / 2
    barrels = [f for f in router._plane_access_fills if f.source_kind == "plane_access_barrel"]
    assert barrels
    for f in barrels:
        assert f.clearance == max(clearance, hole_copper - annulus)
        assert f.via_clearance == max(f.clearance, hole_copper, rules.hole_gap - annulus)
    for f in router._plane_access_fills:
        if f.source_kind == "plane_access_stub":
            assert f.clearance == clearance and f.via_clearance == max(clearance, hole_copper)


@pytest.mark.parametrize("layer", [0, 1, 2, 3])
def test_native_route_validation_uses_via_floor_on_each_physical_layer(layer):
    from kicad_tools.router.cpp_backend import router_cpp

    fills = FixedFillObstacles(
        (FixedFill("GND", 1, layer, 0.2, box(-0.25, -0.25, 0.25, 0.25), via_clearance=0.4),)
    )
    cpp = native_model(fills)
    segment = router_cpp.Segment()
    segment.x1, segment.y1, segment.x2, segment.y2 = -1, 0.55, 1, 0.55
    segment.width, segment.layer, segment.net = 0.15, layer, 2
    assert cpp.validate_route([segment], [], 2, [], 0.15, 0.15, 0.5).valid
    via = router_cpp.Via()
    via.x, via.y, via.diameter, via.drill, via.net = 0.8, 0, 0.45, 0.25, 2
    via.layer_from, via.layer_to = 0, 3
    assert not cpp.validate_route([], [via], 2, [], 0.15, 0.15, 0.5).valid
    via.x = 0.9
    assert cpp.validate_route([], [via], 2, [], 0.15, 0.15, 0.5).valid
    if layer > 1:
        via.x, via.layer_to = 0.8, 1
        assert cpp.validate_route([], [via], 2, [], 0.15, 0.15, 0.5).valid
