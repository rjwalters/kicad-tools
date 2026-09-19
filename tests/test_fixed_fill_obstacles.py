"""Physical filled copper retains layer, holes and true distance semantics."""

import pytest
from shapely.geometry import LineString, Polygon

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles


@pytest.fixture(autouse=True)
def optional_native(request):
    params = getattr(getattr(request.node, "callspec", None), "params", {})
    if (
        params.get("native")
        or (
            params.get("strategy") == "mesh"
            and request.node.originalname == "test_real_valid_route_avoids_fixed_fill"
        )
    ) and not is_cpp_available():
        pytest.skip("Native routing backend is optional")


def model():
    return FixedFillObstacles(
        (
            FixedFill(
                source_net="BAD",
                source_net_id=1,
                layer=0,
                clearance=0.2,
                geometry=Polygon(
                    [(100, 100), (110, 100), (110, 110), (100, 110)],
                    holes=[[(103, 103), (107, 103), (107, 107), (103, 107)]],
                ),
            ),
        )
    )


def test_actual_fill_hole_layer_and_clearance():
    fills = model()
    assert not fills.segment_clear((101, 101), (102, 102), 0, 0.1, 0.1)
    assert fills.segment_clear((104, 104), (106, 106), 0, 0.1, 0.1)
    assert fills.segment_clear((101, 101), (102, 102), 1, 0.1, 0.1)
    assert not fills.segment_clear((103.29, 104), (103.29, 106), 0, 0.1, 0.1)
    assert fills.segment_clear((103.31, 104), (103.31, 106), 0, 0.1, 0.1)
    assert not fills.segment_clear((103.31, 104), (103.31, 106), 0, 0.1, 0.3)


def test_via_span_and_diagonal_crossing():
    fills = model()
    assert fills.via_clear((105, 105), (0, 1), 0.3, 0.2)
    assert not fills.via_clear((101, 101), (0, 1), 0.3, 0.2)
    assert fills.via_clear((101, 101), (1,), 0.3, 0.2)
    assert not fills.segment_clear((99, 105), (111, 105), 0, 0.1, 0.2)


@pytest.mark.skipif(not is_cpp_available(), reason="Native backend unavailable")
def test_native_true_geometry_parity():
    from kicad_tools.router.cpp_backend import router_cpp

    fills = model()
    native = router_cpp.Grid3D(200, 200, 2, 0.1, 95.0, 95.0)
    for layer, clearance, rings in fills.native_polygons():
        native.add_fixed_fill(layer, clearance, rings)
    for layer in (0, 1):
        for x in (101, 103.29, 103.2998, 103.29995, 103.3, 103.3002, 104, 107.01, 111):
            a, b = (x, 104), (x, 106)
            assert native.fixed_fill_clear(*a, *b, layer, 0.1, 0.3) == fills.segment_clear(
                a, b, layer, 0.1, 0.2
            ), (x, layer)


def write_board(tmp_path, zones):
    from tests.test_routing_placement_disposition import board_text

    path = tmp_path / "filled.kicad_pcb"
    path.write_text(board_text()[:-1] + zones + ")")
    return path


def zone(points, *, clearance=0.2, layer="F.Cu", uuid="fill"):
    pts = " ".join(f"(xy {x} {y})" for x, y in points)
    return f'''(zone (net 1) (net_name "BAD") (layer "{layer}") (uuid "{uuid}")
        (connect_pads (clearance {clearance})) (filled_areas_thickness no)
        (polygon (pts {pts})) (filled_polygon (layer "{layer}") (pts {pts})))'''


def load_board(path, *, native=False, strategy="grid", single_layer=False):
    from kicad_tools.placement.routing import analyze_routing_placement
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.layers import LayerDefinition, LayerStack, LayerType

    stack = (
        LayerStack([LayerDefinition("F.Cu", 0, LayerType.SIGNAL, True)]) if single_layer else None
    )
    return load_pcb_for_routing(
        str(path),
        placement_disposition=analyze_routing_placement(path),
        force_python=not native,
        strategy=strategy,
        layer_stack=stack,
    )


def test_same_net_zones_keep_local_clearance(tmp_path):
    path = write_board(
        tmp_path,
        zone([(103, 102), (104, 102), (104, 103), (103, 103)], clearance=0.2, uuid="low")
        + zone([(116, 102), (117, 102), (117, 103), (116, 103)], clearance=1.0, uuid="high"),
    )
    router, _ = load_board(path)
    fills = router.grid.fixed_fills
    assert fills.segment_clear((104.4, 102), (104.4, 103), 0, 0.1, 0.2)
    assert not fills.segment_clear((117.4, 102), (117.4, 103), 0, 0.1, 0.2)
    assert {fill.source_zone_id for fill in fills.fills if fill.source_kind == "zone"} == {
        "low",
        "high",
    }


@pytest.mark.parametrize(
    "native,strategy", [(False, "grid"), (True, "grid"), (False, "lattice"), (False, "mesh")]
)
def test_real_valid_route_avoids_fixed_fill(tmp_path, native, strategy):
    path = write_board(tmp_path, zone([(107, 106), (108, 106), (108, 108), (107, 108)]))
    original = path.read_bytes()
    router, nets = load_board(path, native=native, strategy=strategy, single_layer=True)
    routes = router.route_net(nets["GOOD"])
    assert routes, (native, strategy)
    assert any(route.segments for route in routes)
    # Independent source-space oracle, deliberately not the production predicate.
    copper = Polygon([(107, 106), (108, 106), (108, 108), (107, 108)])
    for route in routes:
        for seg in route.segments:
            assert seg.layer.value == 0, "single-layer fixture must detour"
            line = LineString(((seg.x1, seg.y1), (seg.x2, seg.y2)))
            assert line.distance(copper) >= seg.width / 2 + 0.2 - 1e-4
        assert not route.vias
    if native:
        from kicad_tools.router.cpp_backend import CppPathfinder

        assert isinstance(router.router, CppPathfinder)
        assert router.router._fallback_count == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize("native", [False, True])
def test_knockout_hole_is_routable_and_reset_keeps_fill(tmp_path, native):
    ring = [
        (101, 101),
        (119, 101),
        (119, 111),
        (101, 111),
        (101, 101),
        (103, 104),
        (103, 110),
        (112, 110),
        (112, 104),
        (103, 104),
        (101, 101),
    ]
    path = write_board(tmp_path, zone(ring))
    router, nets = load_board(path, native=native)
    assert router.route_net(nets["GOOD"])
    router._reset_for_new_trial()
    assert router.grid.fixed_fills
    assert not router.grid.fixed_fills.segment_clear((115, 105), (116, 105), 0, 0.1, 0.2)
    assert router.route_net(nets["GOOD"])


def test_legacy_segment_fill_and_old_polygon_stroke(tmp_path):
    legacy = """(zone (net 1) (net_name "BAD") (layer "F.Cu") (uuid "legacy")
      (min_thickness 0.4) (connect_pads (clearance 0.2))
      (polygon (pts (xy 103 101) (xy 112 101) (xy 112 106) (xy 103 106)))
      (fill_segments (pts (xy 104 104) (xy 110 104))))"""
    path = write_board(tmp_path, legacy)
    router, _ = load_board(path)
    fill = router.grid.fixed_fills.fills[0]
    assert fill.source_kind == "legacy_fill_segments"
    assert not router.grid.fixed_fills.segment_clear((105, 104.39), (109, 104.39), 0, 0, 0.2)
    assert router.grid.fixed_fills.segment_clear((105, 104.41), (109, 104.41), 0, 0, 0.2)
    assert router.grid.fixed_fills.segment_clear((105, 102), (109, 102), 0, 0.1, 0.2)
    old = zone([(104, 104), (110, 104), (110, 105), (104, 105)]).replace(
        "(filled_areas_thickness no)", "(min_thickness 0.4)"
    )
    path.write_text(path.read_text().replace(legacy, old))
    router, _ = load_board(path)
    assert not router.grid.fixed_fills.segment_clear((105, 103.61), (109, 103.61), 0, 0, 0.2)
    assert router.grid.fixed_fills.segment_clear((105, 103.59), (109, 103.59), 0, 0, 0.2)


def test_bowtie_lobes_and_concave_notch(tmp_path):
    path = write_board(tmp_path, zone([(103, 104), (107, 108), (103, 108), (107, 104)]))
    router, _ = load_board(path)
    fills = router.grid.fixed_fills
    assert not fills.segment_clear((105, 104.5), (105, 104.5), 0, 0, 0)
    assert not fills.segment_clear((105, 107.5), (105, 107.5), 0, 0, 0)
    assert fills.segment_clear((103.2, 106), (103.2, 106), 0, 0, 0)
    if not is_cpp_available():
        return
    from kicad_tools.router.cpp_backend import CppGrid

    native = CppGrid.from_routing_grid(router.grid)._impl
    assert not native.fixed_fill_clear(105, 104.5, 105, 104.5, 0, 0, 0)
    assert not native.fixed_fill_clear(105, 107.5, 105, 107.5, 0, 0, 0)
    assert native.fixed_fill_clear(103.2, 106, 103.2, 106, 0, 0, 0)


@pytest.mark.parametrize("checkpoint", [False, True])
def test_export_reload_and_presave_validation(tmp_path, checkpoint):
    from kicad_tools.cli.route_cmd import _write_routed_pcb
    from kicad_tools.router.io import validate_routes
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    raw_zone = zone([(107, 106), (108, 106), (108, 108), (107, 108)])
    arc = '(arc (start 104 104) (mid 105 105) (end 106 104) (width 0.2) (layer "F.Cu") (net 1))'
    path = write_board(tmp_path, raw_zone + arc)
    router, nets = load_board(path)
    router.routes = [
        Route(
            net=nets["GOOD"],
            net_name="GOOD",
            segments=[Segment(106, 107, 109, 107, 0.2, Layer.F_CU, nets["GOOD"])],
        )
    ]
    failures = [v for v in validate_routes(router) if v.obstacle_type == "fixed_copper"]
    assert failures and failures[0].obstacle_net_name == "BAD"
    assert failures[0].distance < 0
    output = tmp_path / "output.kicad_pcb"
    _write_routed_pcb(path, output, router.placement_preserved_copper, is_checkpoint=checkpoint)
    text = output.read_text()
    assert text.count(raw_zone) == text.count(arc) == 1
    reloaded, _ = load_board(output)
    assert sorted(fill.source_kind for fill in reloaded.grid.fixed_fills.fills) == [
        "arc",
        "segment",
        "via",
        "via",
        "zone",
    ]
    assert reloaded.placement_preserved_zones == (raw_zone,)
    assert reloaded.placement_preserved_arcs == (arc,)


def _coupled_fill_route(native, *, legacy_dimensions=False, net_clearance_floors=None):
    from kicad_tools.router.diffpair_routing import CoupledPathfinder
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import Layer, LayerDefinition, LayerStack, LayerType
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.rules import DesignRules, NetClassRouting

    # Exact grid-aligned pitch avoids the large convergence search in the
    # general coupled-parity fixture. One layer forces a physical detour.
    rules = DesignRules(
        grid_resolution=0.2,
        trace_width=0.2,
        trace_clearance=0.2,
        net_clearance_floors=net_clearance_floors or {},
    )
    stack = LayerStack([LayerDefinition("F.Cu", 0, LayerType.SIGNAL, True)])
    grid = RoutingGrid(5, 5, rules, layer_stack=stack)
    pads = [
        Pad(x=x, y=y, width=0.4, height=0.4, net=net, net_name=name, layer=Layer.F_CU)
        for net, name, y in [(1, "D+", 2), (2, "D-", 3.2)]
        for x in [1, 4]
    ]
    copper = Polygon([(2, 0), (3, 0), (3, 1.5), (2, 1.5)])
    grid.install_fixed_fills(FixedFillObstacles((FixedFill("BAD", 9, 0, 0.2, copper),)))
    klass = NetClassRouting(name="wide", trace_width=0.61, clearance=0.31)
    classes = {"D+": klass, "D-": klass}
    pf = CoupledPathfinder(
        grid,
        rules,
        target_spacing_cells=6,
        min_spacing_cells=5,
        net_class_map={} if legacy_dimensions else classes,
    )
    if legacy_dimensions:
        # Counterfactual of the reviewed defect: global dimensions during
        # search, but the real per-class width during route emission.
        emit = pf._build_route_from_path

        def emit_wide(*args):
            saved = pf.net_class_map
            pf.net_class_map = classes
            try:
                return emit(*args)
            finally:
                pf.net_class_map = saved

        pf._build_route_from_path = emit_wide
    pf._use_cpp_coupled = native
    routes = pf.route_coupled(*pads, timeout_seconds=10, max_iterations_budget=2000)
    assert routes is not None
    if native:
        assert pf._cpp_coupled_impl is not None
        assert pf._use_cpp_coupled
    assert all(route.segments and not route.vias for route in routes)
    segments = [segment for route in routes for segment in route.segments]
    assert all(segment.layer == Layer.F_CU and segment.width == 0.61 for segment in segments)
    # Independent source-space oracle measures actual emitted copper.
    return min(
        LineString(((seg.x1, seg.y1), (seg.x2, seg.y2))).distance(copper) - seg.width / 2
        for seg in segments
    )


@pytest.mark.parametrize("native", [False, True])
def test_coupled_rails_respect_class_width_at_fixed_fill(native):
    assert _coupled_fill_route(native) >= 0.31 - 1e-4


@pytest.mark.parametrize("native", [False, True])
def test_coupled_fill_fixture_detects_global_rule_search(native):
    assert _coupled_fill_route(native, legacy_dimensions=True) < 0.31 - 1e-4


@pytest.mark.parametrize("native", [False, True])
def test_non_grid_width_fits_physical_neck(tmp_path, native):
    from kicad_tools.router.rules import NetClassRouting

    zones = zone([(107, 100), (108, 100), (108, 106.675), (107, 106.675)]) + zone(
        [(107, 107.325), (108, 107.325), (108, 112), (107, 112)], uuid="upper"
    )
    path = write_board(tmp_path, zones)
    router, nets = load_board(path, native=native, single_layer=True)
    klass = NetClassRouting(name="fractional", trace_width=0.23, clearance=0.2)
    router.net_class_map["GOOD"] = klass
    (router.router._net_class_map if native else router.router.net_class_map)["GOOD"] = klass
    result = router.route_net(nets["GOOD"])
    assert result
    for route in result:
        for seg in route.segments:
            assert seg.width == 0.23
            assert abs(seg.y1 - 107) < 0.01 and abs(seg.y2 - 107) < 0.01
    if native:
        assert router.router._fallback_count == 0


def test_fine_grid_retry_cannot_cross_fixed_wall(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = write_board(tmp_path, zone([(107, 100), (108, 100), (108, 112), (107, 112)]))
    router, nets = load_board(path, single_layer=True)

    def coarse_failure(**kwargs):
        router.routing_failures = [SimpleNamespace(net=nets["GOOD"], net_name="GOOD")]

    monkeypatch.setattr(router, "route_all", coarse_failure)
    routes = router.route_all_multi_resolution(use_negotiated=False, timeout=5)
    assert not [route for route in routes if route.net == nets["GOOD"]]


def test_lattice_coupled_rails_detour_fixed_fill():
    from tests.router.lattice.test_coupled_pairs import _pair_board

    pf, pc = _pair_board()
    copper = Polygon([(12, 4), (16, 4), (16, 6), (12, 6)])
    pf.fixed_fills = FixedFillObstacles((FixedFill("BAD", 9, 0, 0.2, copper),))
    routes, stats = pf.route_netset([], coupled=[pc])
    assert stats.routed == 1
    assert pf.pair_outcomes[pc.key] == "coupled"
    for route in routes.values():
        for seg in route.segments:
            if seg.layer.value == 0:
                assert (
                    LineString(((seg.x1, seg.y1), (seg.x2, seg.y2))).distance(copper)
                    >= seg.width / 2 + 0.2 - 1e-4
                )


@pytest.mark.parametrize("native", [False, True])
def test_mixed_zone_layers_and_four_layer_via_span(tmp_path, native):
    from kicad_tools.router.cpp_backend import CppPathfinder
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Via

    raw = zone([(107, 105), (109, 105), (109, 109), (107, 109)], layer="In2.Cu")
    raw = (
        raw[:-1]
        + '(filled_polygon (layer "In1.Cu") (pts (xy 114 105) (xy 116 105) (xy 116 109) (xy 114 109))))'
    )
    path = write_board(tmp_path, raw)
    text = path.read_text().replace(
        '(31 "B.Cu" signal)', '(1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)'
    )
    text = text.replace('(44 "Edge.Cuts" user))', '(44 "Edge.Cuts" user)\n)')
    path.write_text(text)
    router, nets = load_board(path, native=native)
    assert router.grid.num_layers == 4
    assert {fill.layer for fill in router.grid.fixed_fills.fills if fill.source_kind == "zone"} == {
        1,
        2,
    }
    assert {fill.layer for fill in router.grid.fixed_fills.fills if fill.source_kind == "via"} == {
        0,
        1,
        2,
        3,
    }
    for x in (108, 115):
        through = Via(
            x=x, y=107, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=nets["GOOD"]
        )
        assert not router.grid.validate_via_clearance(through, nets["GOOD"])[0]
        # F->In1 does not span the first fill on In2.
        blind = Via(
            x=x, y=107, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.IN1_CU), net=nets["GOOD"]
        )
        assert router.grid.validate_via_clearance(blind, nets["GOOD"])[0] == (x == 108)
        if native:
            assert isinstance(router.router, CppPathfinder)
            start, end = router.pads[("R1", "1")], router.pads[("R2", "1")]
            for via, valid in ((through, False), (blind, x == 108)):
                route = Route(net=nets["GOOD"], net_name="GOOD", vias=[via])
                assert (
                    router.router._validate_route_clearance(route, start, end, 3) is None
                ) == valid
            assert router.router._fallback_count == 0


def test_name_only_fill_keeps_source_identity_after_effective_split(tmp_path):
    from kicad_tools.placement.routing import analyze_routing_placement
    from kicad_tools.router.io import load_pcb_for_routing
    from tests.test_routing_placement_disposition import board_text

    path = tmp_path / "names.kicad_pcb"
    raw = zone([(107, 106), (108, 106), (108, 108), (107, 108)]).replace("(net 1)", '(net "BAD")')
    path.write_text(board_text(name_only=True)[:-1] + raw + ")")
    mapping = {"X1.1": "NEW", "X2.1": "NEW"}
    disposition = analyze_routing_placement(path, netlist=mapping)
    router, nets = load_pcb_for_routing(
        str(path), netlist=mapping, placement_disposition=disposition, force_python=True
    )
    assert nets["BAD"] in router.nets
    assert router.grid.fixed_fills.fills
    assert router.grid.fixed_fills.fills[0].source_net == "BAD"
    assert not router.grid.fixed_fills.segment_clear((107.5, 107), (107.5, 107), 0, 0.1, 0.2)
    assert router.placement_preserved_zones == (raw,)


@pytest.mark.parametrize("strategy", ["mesh", "lattice"])
def test_engine_via_uses_physical_radius_and_via_gap(strategy):
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.mesh.pathfinder import MeshPathfinder
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules(trace_clearance=0.2, via_clearance=0.35, via_diameter=0.6)
    outline = [(-2, -2), (3, -2), (3, 3), (-2, 3)]
    cls = MeshPathfinder if strategy == "mesh" else LatticePathfinder
    pf = cls(outline, [], rules, layer_stack=LayerStack.two_layer())
    pf.fixed_fills = FixedFillObstacles(
        (FixedFill("BAD", 1, 0, 0.3, Polygon([(1, 0), (2, 0), (2, 1), (1, 1)])),)
    )

    def clear(point):
        if strategy == "mesh":
            # Incoming envelope includes a .1 class gap; physical body remains .3.
            return pf._via_allowed_at(point, 2, 0.4, (0, 1), {})
        return pf._fresh_committed().via_clear(point, 2)

    assert not clear((0.45, 0.5))  # .55 gap < .3 body + .35 via clearance
    assert not clear((0.37, 0.5))  # .63 would clear the trace gap, but not via gap
    assert clear((0.34, 0.5))


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("strict_net", [1, 9])
def test_coupled_search_retains_authored_fill_floor(native, strict_net):
    # The unchanged .31 mm class yields a .335 mm gap. An authored .35 mm
    # floor must alter the route, with the same iteration/time budget.
    assert _coupled_fill_route(native) < 0.35
    assert _coupled_fill_route(native, net_clearance_floors={strict_net: 0.35}) >= 0.35 - 1e-4
