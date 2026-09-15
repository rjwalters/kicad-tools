"""Placement-fixed copper retains authored classes despite neutral ownership."""

from types import SimpleNamespace

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar
from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.rules import NetClassRouting
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("name_only", [False, True])
def test_late_sidecar_keeps_distinct_fixed_clearances_and_neutral_ownership(tmp_path, name_only):
    path = tmp_path / "mixed.kicad_pcb"
    partner_net = '(net "PARTNER")' if name_only else "(net 3)"
    original = (
        board_text(name_only=name_only)[:-1]
        + f'(segment (start 113 109) (end 117 109) (width 0.2) (layer "F.Cu") {partner_net})'
        + f'(via (at 115 109) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") {partner_net}))'
    )
    path.write_text(original)
    disposition = analyze_routing_placement(path, coupled_groups=[("BAD", "PARTNER")])
    router, nets = load_pcb_for_routing(
        str(path), placement_disposition=disposition, force_python=True, strategy="lattice"
    )
    assert all(route.net == 0 and route.net_name == "" for route in router.existing_routes)
    # Both overrides become known only AFTER loading; neither net is routable.
    args = SimpleNamespace(
        _loaded_net_class_map={
            "BAD": NetClassRouting(name="HV", clearance=2.0),
            "PARTNER": NetClassRouting(name="MV", clearance=1.0),
        }
    )
    _apply_net_class_map_sidecar(router, args, quiet=True)
    assert router.net_class_map["BAD"].clearance == 2.0
    assert router.net_class_map["PARTNER"].clearance == 1.0
    fixed = router.grid.fixed_fills
    tracks = [fill for fill in fixed.fills if fill.source_kind in {"segment", "via"}]
    assert len(tracks) == 6  # Two tracks plus two through vias on both copper layers.
    assert {(fill.source_net, fill.clearance) for fill in tracks} == {
        ("BAD", 2.0),
        ("PARTNER", 1.0),
    }
    bad_segment = next(f for f in tracks if f.source_net == "BAD" and f.source_kind == "segment")
    bad_via = next(f for f in tracks if f.source_net == "BAD" and f.source_kind == "via")
    # Conservative, tightly bounded approximations of the actual authored copper.
    assert bad_segment.geometry.covers(LineString([(105, 103), (106, 103)]).buffer(0.1))
    assert bad_segment.geometry.bounds[0] >= 104.89999
    assert bad_via.geometry.covers(Point(106, 103).buffer(0.3))
    assert bad_via.geometry.bounds[0] >= 105.69997

    pf = router._ensure_lattice_pathfinder()
    committed = pf._fresh_committed()
    # Original net identity never grants reuse, even on the back-layer via.
    assert not committed.seg_clear((105.5, 104.3), (105.6, 104.3), 0, nets["BAD"])
    assert not committed.seg_clear((106, 104.3), (106.1, 104.3), 1, nets["BAD"])
    # The second neutral net has its OWN gap, not the maximum of all net-0 copper.
    assert committed.seg_clear((116, 107.7), (116.1, 107.7), 0, nets["GOOD"])
    assert not committed.seg_clear((116, 108.1), (116.1, 108.1), 0, nets["GOOD"])
    routes = router.route_net(nets["GOOD"])
    assert any(route.segments for route in routes)
    for route in routes:
        for segment in route.segments:
            assert fixed.segment_clear(
                (segment.x1, segment.y1),
                (segment.x2, segment.y2),
                router.grid.layer_to_index(segment.layer.value),
                segment.width / 2,
                router.rules.trace_clearance,
            )
    router._reset_for_new_trial()
    assert router.grid.fixed_fills == fixed
    assert path.read_text() == original


@pytest.mark.parametrize(
    ("via_layers", "selected", "occupied"),
    [
        (("F.Cu", "B.Cu"), ["F.Cu"], {0}),
        (("F.Cu", "B.Cu"), ["In1.Cu"], {0}),
        (("F.Cu", "In2.Cu"), ["F.Cu", "In1.Cu", "B.Cu"], {0, 1}),
        (("In1.Cu", "In3.Cu"), ["F.Cu", "In2.Cu", "B.Cu"], {1}),
        (("In1.Cu", "In2.Cu"), ["F.Cu", "B.Cu"], set()),
    ],
)
def test_fixed_via_physical_span_intersects_selected_layers(
    tmp_path, via_layers, selected, occupied
):
    from kicad_tools.router.fixed_copper import load_fixed_fills
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import LayerDefinition, LayerStack, LayerType
    from kicad_tools.router.rules import DesignRules

    path = tmp_path / "mixed.kicad_pcb"
    text = board_text().replace(
        '(layers "F.Cu" "B.Cu")', f'(layers "{via_layers[0]}" "{via_layers[1]}")'
    )
    path.write_text(text)
    stack = LayerStack(
        [LayerDefinition(name, i, LayerType.SIGNAL) for i, name in enumerate(selected)]
    )
    grid = RoutingGrid(20, 12, DesignRules(), origin_x=100, origin_y=100, layer_stack=stack)
    fills = load_fixed_fills(path, {"BAD"}, grid, {})
    via_fills = [fill for fill in fills.fills if fill.source_kind == "via"]
    assert {fill.layer for fill in via_fills} == occupied
    segment_fills = [fill for fill in fills.fills if fill.source_kind == "segment"]
    assert {fill.layer for fill in segment_fills} == (
        {selected.index("F.Cu")} if "F.Cu" in selected else set()
    )
    for fill in via_fills:
        assert fill.geometry.contains(Point(106, 103))
    assert path.read_text() == text


@pytest.mark.parametrize("selected", ["F.Cu", "B.Cu"])
def test_fixed_zone_arc_and_legacy_fill_only_occupy_selected_layer(tmp_path, selected):
    from kicad_tools.router.fixed_copper import load_fixed_fills
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import LayerDefinition, LayerStack, LayerType
    from kicad_tools.router.rules import DesignRules

    path = tmp_path / "mixed.kicad_pcb"
    copper = """
      (zone (net 1) (net_name "BAD") (layer "B.Cu")
        (min_thickness 0.2) (connect_pads (clearance 0.2))
        (polygon (pts (xy 108 102) (xy 111 102) (xy 111 105) (xy 108 105)))
        (filled_polygon (layer "B.Cu")
          (pts (xy 108 102) (xy 111 102) (xy 111 105) (xy 108 105))))
      (zone (net 1) (net_name "BAD") (layer "B.Cu")
        (min_thickness 0.2) (connect_pads (clearance 0.2))
        (polygon (pts (xy 112 102) (xy 115 102) (xy 115 105) (xy 112 105)))
        (fill_segments (pts (xy 112 103) (xy 115 103))))
      (arc (start 105 103) (mid 106 104) (end 107 103)
        (width 0.2) (layer "B.Cu") (net 1))"""
    text = board_text()[:-1] + copper + ")"
    path.write_text(text)
    grid = RoutingGrid(
        20,
        12,
        DesignRules(),
        origin_x=100,
        origin_y=100,
        layer_stack=LayerStack([LayerDefinition(selected, 0, LayerType.SIGNAL)]),
    )
    fixed = load_fixed_fills(path, {"BAD"}, grid, {})
    assert {fill.layer for fill in fixed.fills} == {0}
    assert {fill.source_kind for fill in fixed.fills} == (
        {"segment", "via"} if selected == "F.Cu" else {"zone", "legacy_fill_segments", "arc", "via"}
    )
    if selected == "B.Cu":
        for point in [(109, 103), (113, 103), (106, 104)]:
            assert not fixed.segment_clear(point, point, 0, 0.1, 0.2)
    assert path.read_text() == text
