"""Physical negative-KiCad-angle controls for grid acceptance, not AABB oracles."""

import math

import pytest

from kicad_tools.router import DesignRules
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import _resolve_pad_dims_and_rotation
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment, Via

native = pytest.mark.skipif(
    not is_cpp_available(), reason="native backend unavailable; run kct build-native"
)

ANGLES = [-45, -30, 0, 30, 45, 89.5, 90, 90.5, 180, 269.5, 270]


def physical(angle, u, v):
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return 10 + u * c + v * s, 10 - u * s + v * c


def setup(angle):
    w, h, rotation = _resolve_pad_dims_and_rotation(angle, 4, 1)
    pad = Pad(10, 10, w, h, 2, "FOREIGN", ref="U2", rotation=rotation)
    rules = DesignRules(trace_width=0.1, trace_clearance=0.127, grid_resolution=0.2)
    grid = RoutingGrid(20, 20, rules)
    grid.add_pad(pad)
    return grid, pad


@pytest.mark.parametrize("angle", ANGLES)
@pytest.mark.parametrize("v,valid", [(0, False), (0.6, False), (0.8, True)])
def test_python_backstops(angle, v, valid):
    grid, pad = setup(angle)
    a, b = physical(angle, 1.2, v), physical(angle, 1.6, v)
    seg = Segment(*a, *b, 0.1, Layer.F_CU, 1)
    via = Via(*physical(angle, 1.5, v), 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1)
    assert (grid.worst_segment_pad_deficit(seg, 1)[0] == 0) is valid
    assert (grid.worst_via_pad_deficit(via, 1)[0] == 0) is valid
    assert grid.validate_segment_clearance(seg, exclude_net=1)[0] is valid
    if not valid:
        assert grid.worst_via_pad_deficit(via, 1)[1] == (pad.x, pad.y)


@pytest.mark.parametrize("angle", ANGLES)
@pytest.mark.parametrize("v,valid", [(0, False), (0.6, False), (0.8, True)])
@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("late", [False, True])
@native
def test_compiled_acceptance(angle, v, valid, kind, late):
    from kicad_tools.router import router_cpp
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    assert is_cpp_available(), "Build native backend to run these regression controls"
    grid, pad = setup(angle)
    if late:
        grid = RoutingGrid(20, 20, grid.rules)
    cpp = CppGrid.from_routing_grid(grid)
    if late:
        grid.add_pad(pad)
    segments, vias = [], []
    if kind == "segment":
        item = router_cpp.Segment()
        item.x1, item.y1 = physical(angle, 1.2, v)
        item.x2, item.y2 = physical(angle, 1.6, v)
        item.width, item.layer, item.net = 0.1, 0, 1
        segments.append(item)
    else:
        item = router_cpp.Via()
        item.x, item.y = physical(angle, 1.5, v)
        item.diameter, item.drill = 0.2, 0.1
        item.layer_from, item.layer_to, item.net = 0, 1, 1
        vias.append(item)
    result = cpp._impl.validate_route(segments, vias, 1, [], 0.127, 0.127, 0.1)
    assert result.valid is valid


@pytest.mark.parametrize("kind", ["segment", "via"])
@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("v,demoted", [(0, True), (0.8, False)])
def test_actual_finalization(kind, force_python, v, demoted):
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.primitives import Route

    rules = DesignRules(trace_width=0.1, trace_clearance=0.127, grid_resolution=0.05)
    if not force_python and not is_cpp_available():
        pytest.skip("native backend unavailable")
    router = Autorouter(20, 20, rules=rules, force_python=force_python)
    _, pad = setup(45)
    router.add_component(
        "U2",
        [
            {
                "number": "1",
                "x": pad.x,
                "y": pad.y,
                "width": pad.width,
                "height": pad.height,
                "rotation": pad.rotation,
                "net": 2,
                "net_name": "FOREIGN",
            }
        ],
    )
    route = Route(1, "SIGNAL")
    if kind == "segment":
        route.segments.append(
            Segment(*physical(45, 1.2, v), *physical(45, 1.6, v), 0.1, Layer.F_CU, 1)
        )
    else:
        route.vias.append(Via(*physical(45, 1.5, v), 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1))
    router.routes.append(route)
    router.grid.mark_route(route)
    router.grid.mark_route_usage(route)
    net_routes = {1: [route]}
    assert router._demote_pad_clearance_violation_nets(net_routes) == ([1] if demoted else [])
    assert (route in router.routes) is not demoted
    assert bool(net_routes[1]) is not demoted
    assert rules.trace_clearance == 0.127
    assert router.grid.resolution == 0.05


@pytest.mark.parametrize("net", [0, 1, 2])
@pytest.mark.parametrize(
    "through,layer,span,valid",
    [
        (False, Layer.F_CU, (1, 0), False),
        (False, Layer.B_CU, (0, 0), True),
        (True, Layer.B_CU, (0, 0), False),
    ],
)
@native
def test_layer_span_and_same_net(net, through, layer, span, valid):
    from kicad_tools.router import router_cpp
    from kicad_tools.router.cpp_backend import CppGrid

    grid, pad = setup(30)
    pad.net, pad.through_hole, pad.layer = net, through, layer
    cpp = CppGrid.from_routing_grid(grid)
    x, y = physical(30, 1.5, 0)
    via = router_cpp.Via()
    via.x, via.y, via.diameter, via.drill = x, y, 0.2, 0.1
    via.layer_from, via.layer_to, via.net = *span, 1
    result = cpp._impl.validate_route([], [via], 1, [], 0.127, 0.4, 0.1)
    expected = valid or net == 1
    assert result.valid is expected
    py_via = Via(x, y, 0.1, 0.2, tuple([Layer.F_CU, Layer.B_CU][i] for i in span), 1)
    assert (grid.worst_via_pad_deficit(py_via, 1)[0] == 0) is expected
    if not expected:
        assert result.violation_type == 8
        assert (result.violation_x, result.violation_y) == (10, 10)


@pytest.mark.parametrize("mode", ["standard", "relaxed", "override", "strict", "plane"])
@pytest.mark.parametrize("net", [0, 2])
@pytest.mark.parametrize("v", [0, 0.65, 0.8])
@native
def test_via_carveout_and_component_floor(mode, net, v):
    from kicad_tools.router import router_cpp
    from kicad_tools.router.cpp_backend import CppGrid
    from kicad_tools.router.grid import _sync_pad_via_policies

    grid, pad = setup(30)
    pad.net = net
    if mode == "override":
        grid.rules.component_clearances[pad.ref] = 0.04
    if mode in ("relaxed", "strict", "plane"):
        grid._relaxed_clearance_refs.add(pad.ref)
    if mode == "strict":
        grid.rules.strict_pad_clearance = True
    if mode == "plane":
        pad.net_name = "GND"
    cpp = CppGrid.from_routing_grid(grid)
    _sync_pad_via_policies(grid, cpp)
    x, y = physical(30, 1.5, v)
    via = router_cpp.Via()
    via.x, via.y, via.diameter, via.drill = x, y, 0.2, 0.1
    via.layer_from, via.layer_to, via.net = 0, 1, 1
    result = cpp._impl.validate_route(
        [], [via], 1, [router_cpp.fnv1a_hash(pad.ref)], 0.127, 0.4, 0.1
    )
    py_via = Via(x, y, 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1)
    deficit, _ = grid.worst_via_pad_deficit(py_via, 1, exclude_refs={pad.ref})
    assert result.valid is (deficit == 0)
    expected = v == 0.8 or (v == 0.65 and mode in ("relaxed", "override"))
    assert result.valid is expected


@native
def test_late_pad_policy_refresh_reuses_pitch_cache(monkeypatch):
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
    from kicad_tools.router.primitives import Route

    grid, pad = setup(30)
    # This cache test exercises the explicit legacy pitch-based policy.
    # Default routing requires an authored clearance override (#5004).
    grid.rules.legacy_fine_pitch_carveout = True
    cpp = CppGrid.from_routing_grid(grid)
    calls = []
    original = grid.compute_component_pitches

    def counted():
        calls.append(True)
        return original()

    monkeypatch.setattr(grid, "compute_component_pitches", counted)
    # Completing a fine-pitch component changes the ORIGINAL pad's policy.
    # Late construction must not recompute all-pairs pitches for each pad.
    for offset in (0.5, 1):
        grid.add_pad(Pad(10, 10 + offset, 0.1, 0.1, 1, "SIGNAL", ref=pad.ref))
    assert calls == []
    pf = CppPathfinder(cpp, grid.rules)
    route = Route(1, "SIGNAL")
    route.vias.append(Via(*physical(30, 1.5, 0.65), 0.1, 0.2, (Layer.F_CU, Layer.B_CU), 1))
    own = Pad(5, 5, 0.1, 0.1, 1, "SIGNAL", ref=pad.ref)
    assert pf._validate_route_clearance(route, own, own, 1) is None
    # The public wrapper has other consumers of pitch; count only the shared
    # grid cache computation and require no additional call on repeated checks.
    count = len(calls)
    assert count >= 1
    assert pf._validate_route_clearance(route, own, own, 1) is None
    assert len(calls) == count
    # Policy-only changes are detected without rebuilding geometry/pitches.
    grid.rules.strict_pad_clearance = True
    assert pf._validate_route_clearance(route, own, own, 1) == (10, 10)
    assert len(calls) == count


@native
def test_segment_only_acceptance_does_not_refresh_via_policy(monkeypatch):
    import kicad_tools.router.grid as grid_module
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
    from kicad_tools.router.primitives import Route

    grid, _ = setup(30)
    cpp = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp, grid.rules)

    def forbidden(*args):
        pytest.fail("segment-only acceptance recomputed via policies")

    monkeypatch.setattr(grid_module, "_sync_pad_via_policies", forbidden)
    route = Route(1, "SIGNAL")
    route.segments.append(Segment(2, 2, 3, 2, 0.1, Layer.F_CU, 1))
    own = Pad(2, 2, 0.1, 0.1, 1, "SIGNAL")
    assert pf._validate_route_clearance(route, own, own, 1) is None
