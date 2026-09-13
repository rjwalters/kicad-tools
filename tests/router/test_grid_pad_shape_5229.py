"""Physical square/circle witnesses for shape-aware grid acceptance."""

import math

import pytest

from kicad_tools.router import DesignRules
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via


def rules():
    return DesignRules(
        trace_width=0.02,
        trace_clearance=0.127,
        grid_resolution=0.05,
        via_diameter=0.2,
        via_drill=0.1,
        via_clearance=0.127,
    )


def witness(shape, angle=0):
    pad = Pad(10, 10, 2, 2, 1, "FOREIGN", ref="J1", pin="1", shape=shape, rotation=angle)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))

    def pos(x, y):
        return 10 + x * c + y * s, 10 - x * s + y * c

    seg = Segment(*pos(0.95, 0.95), *pos(1.5, 0.95), 0.02, Layer.F_CU, 2)
    via = Via(*pos(0.95, 0.95), 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 2)
    return pad, seg, via


@pytest.mark.parametrize(
    "shape,valid", [("rect", False), ("circle", True), ("oval", False), ("roundrect", False)]
)
@pytest.mark.parametrize("angle", [0, -45, 30, 90, 180, 270])
def test_python_shape_backstops(shape, valid, angle):
    pad, seg, via = witness(shape, angle)
    grid = RoutingGrid(20, 20, rules())
    grid.add_pad(pad)
    assert grid.validate_segment_clearance(seg, exclude_net=2)[0] is valid
    assert (grid.worst_segment_pad_deficit(seg, 2)[0] == 0) is valid
    assert (grid.worst_via_pad_deficit(via, 2)[0] == 0) is valid
    if shape == "circle":
        assert grid.validate_segment_clearance(seg, exclude_net=2)[1] == pytest.approx(
            math.hypot(0.95, 0.95) - 1 - 0.01
        )
    if not valid:
        assert grid.worst_segment_pad_deficit(seg, 2)[0] > 0.05
        assert grid.worst_via_pad_deficit(via, 2)[0] > 0.05


@pytest.mark.parametrize("shape,valid", [("rect", False), ("circle", True)])
@pytest.mark.parametrize("angle", [0, -45, 30, 90])
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("kind", ["segment", "via"])
def test_native_shape_backstops(shape, valid, angle, late, kind):
    if not is_cpp_available():
        pytest.skip("requires rebuilt native backend")
    from kicad_tools.router import router_cpp

    pad, seg, via = witness(shape, angle)
    grid = RoutingGrid(20, 20, rules())
    if not late:
        grid.add_pad(pad)
    cpp = CppGrid.from_routing_grid(grid)
    if late:
        grid.add_pad(pad)
    segments, vias = [], []
    if kind == "segment":
        item = router_cpp.Segment()
        item.x1, item.y1, item.x2, item.y2 = seg.x1, seg.y1, seg.x2, seg.y2
        item.width, item.layer, item.net = seg.width, 0, 2
        segments.append(item)
    else:
        item = router_cpp.Via()
        item.x, item.y, item.diameter, item.drill = via.x, via.y, via.diameter, via.drill
        item.layer_from, item.layer_to, item.net = 0, 1, 2
        vias.append(item)
    assert cpp._impl.validate_route(segments, vias, 2, [], 0.127, 0.127, 0.1).valid is valid


@pytest.mark.parametrize("shape,demoted", [("rect", True), ("circle", False)])
@pytest.mark.parametrize("force_python", [False, True])
@pytest.mark.parametrize("kind", ["segment", "via"])
def test_real_finalization(shape, demoted, force_python, kind):
    if not force_python and not is_cpp_available():
        pytest.skip("requires rebuilt native backend")
    pad, seg, via = witness(shape)
    router = Autorouter(20, 20, rules=rules(), force_python=force_python)
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": pad.x,
                "y": pad.y,
                "width": 2,
                "height": 2,
                "net": 1,
                "net_name": "FOREIGN",
                "shape": shape,
            }
        ],
    )
    route = Route(
        2,
        "SIGNAL",
        segments=[seg] if kind == "segment" else [],
        vias=[via] if kind == "via" else [],
    )
    router.routes.append(route)
    router.grid.mark_route(route)
    router.grid.mark_route_usage(route)
    net_routes = {2: [route]}
    assert router._demote_pad_clearance_violation_nets(net_routes) == ([2] if demoted else [])
    assert (route in router.routes) is not demoted
    assert router.rules.trace_clearance == 0.127


@pytest.mark.parametrize("shape", ["custom", "trapezoid", "unknown"])
def test_unsupported_shape_rejected(shape):
    with pytest.raises(ValueError, match="shape"):
        Pad(10, 10, 2, 2, 1, "FOREIGN", shape=shape)


@pytest.mark.parametrize("method", ["add_pad", "add_pad_vectorized"])
def test_rotated_search_encloses_copper_and_registers_backstop(method):
    pad = Pad(10, 10, 4, 1, 1, "FOREIGN", rotation=45, shape="rect")
    grid = RoutingGrid(20, 20, rules())
    getattr(grid, method)(pad)
    gx, gy = grid.world_to_grid(9, 11.2)  # strictly inside rotated physical copper
    assert grid._pad_blocked[0, gy, gx]
    assert pad in grid._pads
    seg = Segment(8, 11.2, 12, 11.2, 0.02, Layer.F_CU, 2)
    assert not grid.validate_segment_clearance(seg, exclude_net=2)[0]


@pytest.mark.parametrize("shape", ["rect", "circle"])
@pytest.mark.parametrize(
    "net,layer,through,valid",
    [
        (1, Layer.F_CU, False, False),
        (2, Layer.F_CU, False, True),
        (0, Layer.F_CU, False, False),
        (1, Layer.B_CU, False, True),
        (1, Layer.B_CU, True, False),
    ],
)
def test_shape_preserves_same_net_and_layer_policy(shape, net, layer, through, valid):
    grid = RoutingGrid(20, 20, rules())
    grid.add_pad(Pad(10, 10, 2, 2, net, "PAD", layer=layer, through_hole=through, shape=shape))
    seg = Segment(10, 10, 10.5, 10, 0.02, Layer.F_CU, 2)
    assert grid.validate_segment_clearance(seg, exclude_net=2)[0] is valid
    assert (grid.worst_segment_pad_deficit(seg, 2)[0] == 0) is valid
    via = Via(10, 10, 0.1, 0.2, (Layer.F_CU, Layer.F_CU), 2)
    assert (grid.worst_via_pad_deficit(via, 2)[0] == 0) is valid


@pytest.mark.parametrize("shape", ["rect", "circle"])
@pytest.mark.parametrize("offset,valid", [(0, True), (-0.001, False), (0.001, True)])
def test_exact_clearance_comparison_unchanged(shape, offset, valid):
    # Axis witness has exactly the same physical edge for circle and square.
    # Binary-exact dimensions avoid a floating equality assertion by accident.
    design = DesignRules(trace_width=0.25, trace_clearance=0.125, grid_resolution=0.05)
    grid = RoutingGrid(20, 20, design)
    grid.add_pad(Pad(10, 10, 2, 2, 1, "FOREIGN", shape=shape))
    seg = Segment(11.25 + offset, 10, 12 + offset, 10, 0.25, Layer.F_CU, 2)
    assert grid.validate_segment_clearance(seg, exclude_net=2)[0] is valid
    assert (grid.worst_segment_pad_deficit(seg, 2)[0] == 0) is valid
