"""Keepout rule-area honoring in the lattice engine (issue #4605).

Unit + pathfinder-level coverage of the spatial-constraint primitive:

1. geometry: polygon containment / copper-edge inflation predicates;
2. schema: ``(zone ... (keepout ...))`` rule areas parse into a first-class
   model (flags, layers, polygon, name) without disturbing pour zones, and
   round-trip through ``PCB.load`` -> ``save`` unchanged;
3. mask semantics: per-layer coverage, half-width inflation, the
   ``only`` / ``exempt`` net-id applicability filter, and the strict
   independence of the ``tracks`` and ``vias`` flags -- a
   ``copperpour not_allowed``-only area (the ``kct zones hv-keepout``
   output) never blocks routing;
4. pathfinder: a track-blocking wall reroutes / declines honestly (with a
   keepout-attributed reason), a same-layer wall is dipped under via B.Cu,
   an area governing only ANOTHER net leaves the route byte-identical to
   the no-keepout baseline, and a pad inside its own applicable keepout
   reports ``pad-escape-*-keepout`` (the #3900 never-misattribute lesson).
"""

from __future__ import annotations

from kicad_tools.router.lattice.geometry import (
    polygon_bbox,
    pt_in_polygon,
    seg_near_polygon,
)
from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.rules import DesignRules

Pt = tuple[float, float]

SQUARE: tuple[Pt, ...] = ((10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0))


def _pad(x: float, y: float, net: int, *, ref: str = "R1", layer: Layer = Layer.F_CU) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=f"N{net}",
        layer=layer,
        ref=ref,
        pin="1",
    )


def _area(
    polygon: tuple[Pt, ...] = SQUARE,
    layers: frozenset[int] = frozenset({0, 1}),
    *,
    blocks_tracks: bool = True,
    blocks_vias: bool = True,
    only: frozenset[int] | None = None,
    exempt: frozenset[int] = frozenset(),
    name: str = "",
) -> KeepoutArea:
    return KeepoutArea(
        polygon=polygon,
        layers=layers,
        blocks_tracks=blocks_tracks,
        blocks_vias=blocks_vias,
        only=only,
        exempt=exempt,
        name=name,
    )


# ---------------------------------------------------------------------------
# 1. Geometry predicates.
# ---------------------------------------------------------------------------


def test_pt_in_polygon_and_bbox() -> None:
    assert pt_in_polygon((15.0, 15.0), SQUARE)
    assert not pt_in_polygon((5.0, 15.0), SQUARE)
    assert not pt_in_polygon((25.0, 25.0), SQUARE)
    assert polygon_bbox(SQUARE) == (10.0, 10.0, 20.0, 20.0)


def test_seg_near_polygon_inflation_is_the_copper_edge_test() -> None:
    half = 0.5
    # Straddling segment: both endpoints outside, crosses the polygon.
    assert seg_near_polygon((5.0, 15.0), (25.0, 15.0), SQUARE, half)
    # Endpoint inside.
    assert seg_near_polygon((15.0, 15.0), (25.0, 15.0), SQUARE, half)
    # Outside, but the copper edge (centreline + half) grazes the boundary.
    assert seg_near_polygon((5.0, 9.7), (25.0, 9.7), SQUARE, half)
    # Outside with the copper edge clear of the boundary.
    assert not seg_near_polygon((5.0, 9.4), (25.0, 9.4), SQUARE, half)
    # Degenerate point probe.
    assert seg_near_polygon((15.0, 15.0), (15.0, 15.0), SQUARE, half)
    assert not seg_near_polygon((5.0, 5.0), (5.0, 5.0), SQUARE, half)


# ---------------------------------------------------------------------------
# 2. Schema parsing + round-trip.
# ---------------------------------------------------------------------------


def _rule_area_zone_text(
    *,
    name: str = "hv-bank-a",
    tracks: str = "not_allowed",
    vias: str = "not_allowed",
    copperpour: str = "allowed",
) -> str:
    return f"""(zone
  (net 0)
  (net_name "")
  (name "{name}")
  (layers "F.Cu" "B.Cu")
  (uuid "aaaaaaaa-0000-0000-0000-000000000001")
  (hatch edge 0.5)
  (keepout (tracks {tracks}) (vias {vias}) (pads allowed) (copperpour {copperpour}))
  (polygon (pts (xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20)))
)"""


def test_rule_area_parses_flags_layers_polygon_and_name() -> None:
    from kicad_tools.schema.pcb import Zone
    from kicad_tools.sexp.parser import parse_string

    zone = Zone.from_sexp(parse_string(_rule_area_zone_text()))
    assert zone.keepout is not None
    assert zone.keepout.tracks_allowed is False
    assert zone.keepout.vias_allowed is False
    assert zone.keepout.pads_allowed is True
    assert zone.keepout.copperpour_allowed is True
    assert zone.name == "hv-bank-a"
    assert zone.layers == ["F.Cu", "B.Cu"]
    assert zone.polygon == [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)]


def test_pour_zone_parsing_is_unchanged_and_not_a_rule_area() -> None:
    from kicad_tools.schema.pcb import Zone
    from kicad_tools.sexp.parser import parse_string

    text = """(zone
  (net 1)
  (net_name "GND")
  (layer "F.Cu")
  (uuid "bbbbbbbb-0000-0000-0000-000000000002")
  (hatch edge 0.5)
  (connect_pads (clearance 0.2))
  (min_thickness 0.25)
  (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
  (polygon (pts (xy 0 0) (xy 30 0) (xy 30 30) (xy 0 30)))
)"""
    zone = Zone.from_sexp(parse_string(text))
    assert zone.keepout is None
    assert zone.net_name == "GND"
    assert zone.layer == "F.Cu"


def test_hv_keepout_pour_void_output_is_routing_neutral() -> None:
    """``kct zones hv-keepout`` emits ``copperpour not_allowed`` ONLY -- the
    parsed flags must show tracks/vias allowed so the router never blocks."""
    from kicad_tools.schema.pcb import Zone
    from kicad_tools.sexp.builders import keepout_node
    from kicad_tools.sexp.parser import parse_string, serialize_sexp

    node = keepout_node(
        [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)],
        ["F.Cu"],
        no_tracks=False,
        no_vias=False,
        no_pour=True,
    )
    zone = Zone.from_sexp(parse_string(serialize_sexp(node)))
    assert zone.keepout is not None
    assert zone.keepout.tracks_allowed is True
    assert zone.keepout.vias_allowed is True
    assert zone.keepout.copperpour_allowed is False
    # The lattice mask drops it entirely.
    mask = LatticeKeepoutMask(
        [
            _area(
                tuple(zone.polygon),
                frozenset({0}),
                blocks_tracks=not zone.keepout.tracks_allowed,
                blocks_vias=not zone.keepout.vias_allowed,
            )
        ]
    )
    assert not mask
    assert not mask.segment_blocked((2.0, 2.0), (3.0, 3.0), 0, 1, 0.5)
    assert not mask.via_blocked((2.0, 2.0), 1, 0.3)


def test_keepout_node_emits_optional_name() -> None:
    from kicad_tools.schema.pcb import Zone
    from kicad_tools.sexp.builders import keepout_node
    from kicad_tools.sexp.parser import parse_string, serialize_sexp

    node = keepout_node([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)], ["F.Cu"], name="bank-a")
    zone = Zone.from_sexp(parse_string(serialize_sexp(node)))
    assert zone.name == "bank-a"


def _board_with_rule_area(tmp_path, zone_text: str):
    board = f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_rect (start 0 0) (end 30 30)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{zone_text}
)"""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(board)
    return path


def test_rule_area_round_trips_through_pcb_load_save(tmp_path) -> None:
    from kicad_tools.schema.pcb import PCB

    path = _board_with_rule_area(tmp_path, _rule_area_zone_text())
    pcb = PCB.load(path)
    assert len(pcb.rule_areas) == 1
    area = pcb.rule_areas[0]
    assert area.keepout is not None and area.keepout.tracks_allowed is False

    out = tmp_path / "roundtrip.kicad_pcb"
    pcb.save(out)
    saved = out.read_text()
    assert "(keepout" in saved
    assert "(tracks not_allowed)" in saved
    # And it re-parses to the same rule area.
    pcb2 = PCB.load(out)
    assert len(pcb2.rule_areas) == 1
    assert pcb2.rule_areas[0].polygon == area.polygon


# ---------------------------------------------------------------------------
# 2b. Layer-spec resolution at the projection level (#4672).
# ---------------------------------------------------------------------------


def _projection_mask(tmp_path, zone_layers: str, stack: LayerStack):
    """Project one track-blocking rule area with the given layer spec through
    ``Autorouter._lattice_keepout_projection`` against ``stack``."""
    from kicad_tools.router.core import Autorouter

    zone_text = f"""(zone
  (net 0)
  (net_name "")
  (name "inner-guard")
  (layers {zone_layers})
  (uuid "aaaaaaaa-0000-0000-0000-000000000009")
  (hatch edge 0.5)
  (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed))
  (polygon (pts (xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20)))
)"""
    path = _board_with_rule_area(tmp_path, zone_text)
    router = Autorouter(30, 30, strategy="lattice", layer_stack=stack)
    router._pairwise_attach_zone_pcb_path = str(path)
    return router._lattice_keepout_projection()


def test_inner_wildcard_resolves_to_inner_layers_on_4_layer_stack(tmp_path) -> None:
    mask = _projection_mask(tmp_path, '"*.In.Cu"', LayerStack.four_layer_all_signal())
    assert mask is not None and len(mask.areas) == 1
    assert mask.areas[0].layers == frozenset({1, 2})


def test_inner_wildcard_resolves_to_inner_layers_on_6_layer_stack(tmp_path) -> None:
    mask = _projection_mask(tmp_path, '"*.In.Cu"', LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig())
    assert mask is not None and len(mask.areas) == 1
    assert mask.areas[0].layers == frozenset({1, 2, 3, 4})


def test_inner_wildcard_is_empty_on_2_layer_stack(tmp_path) -> None:
    # No inner layers exist: the wildcard legitimately resolves to the empty
    # set, so an area declared ONLY on ``*.In.Cu`` never constrains routing.
    mask = _projection_mask(tmp_path, '"*.In.Cu"', LayerStack.two_layer())
    assert mask is None


def test_inner_wildcard_mixed_with_named_layer_keeps_the_named_layer(tmp_path) -> None:
    mask = _projection_mask(tmp_path, '"F.Cu" "*.In.Cu"', LayerStack.two_layer())
    assert mask is not None and len(mask.areas) == 1
    assert mask.areas[0].layers == frozenset({0})


# ---------------------------------------------------------------------------
# 3. Mask semantics.
# ---------------------------------------------------------------------------


def test_applies_to_filter_semantics() -> None:
    all_nets = _area()
    assert all_nets.applies_to(1) and all_nets.applies_to(2)

    only = _area(only=frozenset({1}))
    assert only.applies_to(1)
    assert not only.applies_to(2)

    exempt = _area(exempt=frozenset({1}))
    assert not exempt.applies_to(1)
    assert exempt.applies_to(2)


def test_mask_layer_subset_and_half_inflation() -> None:
    mask = LatticeKeepoutMask([_area(layers=frozenset({0}))])
    seg = ((5.0, 15.0), (25.0, 15.0))
    assert mask.segment_blocked(*seg, 0, 1, 0.1)
    # Other layers stay routable.
    assert not mask.segment_blocked(*seg, 1, 1, 0.1)
    # Copper-edge inflation: a fat trace grazing the boundary is blocked, a
    # thin one at the same centreline is not.
    graze = ((5.0, 9.5), (25.0, 9.5))
    assert mask.segment_blocked(*graze, 0, 1, 0.8)
    assert not mask.segment_blocked(*graze, 0, 1, 0.2)


def test_mask_track_and_via_flags_are_independent() -> None:
    tracks_only = LatticeKeepoutMask([_area(blocks_tracks=True, blocks_vias=False)])
    assert tracks_only.segment_blocked((15.0, 15.0), (15.0, 15.0), 0, 1, 0.1)
    assert not tracks_only.via_blocked((15.0, 15.0), 1, 0.3)

    vias_only = LatticeKeepoutMask([_area(blocks_tracks=False, blocks_vias=True)])
    assert not vias_only.segment_blocked((15.0, 15.0), (15.0, 15.0), 0, 1, 0.1)
    assert vias_only.via_blocked((15.0, 15.0), 1, 0.3)
    # The via check is layer-agnostic (through-barrel), including an area on
    # a strict subset of layers.
    subset = LatticeKeepoutMask([_area(layers=frozenset({1}), blocks_tracks=False)])
    assert subset.via_blocked((15.0, 15.0), 1, 0.3)


def test_mask_respects_net_filter() -> None:
    mask = LatticeKeepoutMask([_area(exempt=frozenset({7}))])
    seg = ((5.0, 15.0), (25.0, 15.0))
    assert mask.segment_blocked(*seg, 0, 1, 0.1)
    assert not mask.segment_blocked(*seg, 0, 7, 0.1)
    assert mask.via_blocked((15.0, 15.0), 1, 0.3)
    assert not mask.via_blocked((15.0, 15.0), 7, 0.3)


# ---------------------------------------------------------------------------
# 4. Pathfinder integration.
# ---------------------------------------------------------------------------

OUTLINE: list[Pt] = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]


def _pathfinder() -> LatticePathfinder:
    pads = [
        _pad(5.0, 10.0, 1, ref="R1"),
        _pad(25.0, 10.0, 1, ref="R2"),
    ]
    return LatticePathfinder(OUTLINE, pads, DesignRules(), LayerStack.two_layer())


def _route_one(pf: LatticePathfinder) -> tuple[dict, object]:
    conns = [((1, 0), pf.pads[0], pf.pads[1], None)]
    return pf.route_netset(conns, max_iterations=4)


def _copper(routes: dict) -> list[tuple]:
    out = []
    for route in routes.values():
        assert isinstance(route, Route)
        for seg in route.segments:
            out.append((seg.layer, seg.x1, seg.y1, seg.x2, seg.y2, seg.width))
        for via in route.vias:
            out.append(("via", via.x, via.y))
    return sorted(out, key=repr)


# Full-height wall through the board middle, both layers.
WALL: tuple[Pt, ...] = ((14.0, 0.0), (16.0, 0.0), (16.0, 20.0), (14.0, 20.0))


def test_wall_on_both_layers_declines_with_keepout_reason() -> None:
    pf = _pathfinder()
    pf.set_keepouts(LatticeKeepoutMask([_area(WALL)]))
    routes, stats = _route_one(pf)
    assert stats.routed == 0
    assert pf.failure_reasons[(1, 0)] == "no-path-keepout-constrained"


def test_wall_on_top_layer_dips_under_via_bcu() -> None:
    pf = _pathfinder()
    pf.set_keepouts(LatticeKeepoutMask([_area(WALL, layers=frozenset({0}), blocks_vias=False)]))
    routes, stats = _route_one(pf)
    assert stats.routed == 1
    copper = _copper(routes)
    vias = [c for c in copper if c[0] == "via"]
    assert vias, "expected a layer dip under the F.Cu wall"
    # No F.Cu copper edge inside the wall.
    half = DesignRules().trace_width / 2.0
    for item in copper:
        if item[0] == Layer.F_CU:
            _l, x1, y1, x2, y2, _w = item
            assert not seg_near_polygon((x1, y1), (x2, y2), WALL, half), (
                f"F.Cu segment {item} enters the keepout"
            )


def test_area_governing_only_another_net_is_byte_identical_to_baseline() -> None:
    baseline_routes, baseline_stats = _route_one(_pathfinder())
    assert baseline_stats.routed == 1

    pf = _pathfinder()
    pf.set_keepouts(LatticeKeepoutMask([_area(WALL, only=frozenset({99}))]))
    routes, stats = _route_one(pf)
    assert stats.routed == 1
    assert _copper(routes) == _copper(baseline_routes)


def test_set_keepouts_none_resets_and_matches_baseline() -> None:
    baseline_routes, _stats = _route_one(_pathfinder())

    pf = _pathfinder()
    pf.set_keepouts(LatticeKeepoutMask([_area(WALL)]))
    pf.set_keepouts(None)
    routes, stats = _route_one(pf)
    assert stats.routed == 1
    assert _copper(routes) == _copper(baseline_routes)
    # An EMPTY mask is normalised to None too (dormant fast path).
    pf2 = _pathfinder()
    pf2.set_keepouts(LatticeKeepoutMask([]))
    assert pf2._keepouts is None


def test_detour_keeps_all_copper_out_of_a_partial_wall() -> None:
    # Wall spans y in [0, 15] on both layers -- a detour over the top exists.
    partial: tuple[Pt, ...] = ((14.0, 0.0), (16.0, 0.0), (16.0, 15.0), (14.0, 15.0))
    pf = _pathfinder()
    pf.set_keepouts(LatticeKeepoutMask([_area(partial)]))
    routes, stats = _route_one(pf)
    assert stats.routed == 1
    for item in _copper(routes):
        if item[0] == "via":
            assert not seg_near_polygon(
                (item[1], item[2]), (item[1], item[2]), partial, DesignRules().via_diameter / 2.0
            )
        else:
            _l, x1, y1, x2, y2, w = item
            assert not seg_near_polygon((x1, y1), (x2, y2), partial, w / 2.0), (
                f"segment {item} enters the keepout"
            )


def test_pad_inside_own_keepout_reports_keepout_escape_reason() -> None:
    pocket: tuple[Pt, ...] = ((2.0, 7.0), (8.0, 7.0), (8.0, 13.0), (2.0, 13.0))
    pf = _pathfinder()
    pf.set_keepouts(LatticeKeepoutMask([_area(pocket)]))
    _routes, stats = _route_one(pf)
    assert stats.routed == 0
    assert pf.failure_reasons[(1, 0)] == "pad-escape-start-keepout"
