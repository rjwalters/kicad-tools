"""Placement-excluded custom pads keep their actual copper, never a box.

The fixture's custom pad owns two wall primitives that reach far beyond its
0.4x0.4 nominal size, separated by a real slot. A nominal-box substitution
would leave the direct path open; an enclosing-box substitution would seal
the slot. Only the true geometry routes the way these tests assert.
"""

import logging
import math
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from kicad_tools.router.cpp_backend import is_cpp_available

# Pad-local primitive extents (mm). The pad sits at board (108, 108) with a
# 0.4 x 0.4 rect anchor, so board coverage is x 107.8..108.2 with a slot at
# y 104.1..105.5 between the two walls.
UPPER_WALL = (-0.2, -8.0, 0.2, -3.9)
LOWER_WALL = (-0.2, -2.5, 0.2, 4.0)
SLOT = (104.1, 105.5)


@pytest.fixture(autouse=True)
def optional_native(request):
    params = getattr(getattr(request.node, "callspec", None), "params", {})
    if (params.get("native") or params.get("strategy") == "mesh") and not is_cpp_available():
        pytest.skip("Native routing backend is optional")


def poly(corners, *, width=0, fill="yes", tag="gr_poly"):
    x1, y1, x2, y2 = corners
    pts = f"(xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2})"
    return f"({tag} (pts {pts}) (width {width}) (fill {fill}))"


def custom_pad(
    *,
    number="9",
    net='(net 1 "GND")',
    at="0 0",
    size="0.4 0.4",
    layers='"F.Cu"',
    anchor="rect",
    primitives=None,
    extra="",
):
    shapes = (poly(UPPER_WALL), poly(LOWER_WALL)) if primitives is None else primitives
    body = f"(primitives {' '.join(shapes)})" if shapes else ""
    return (
        f'(pad "{number}" smd custom (at {at}) (size {size}) (layers {layers}) {net} '
        f"(options (clearance outline) (anchor {anchor})) {body} {extra})"
    )


def simple_pad(net, *, layers='"F.Cu"'):
    return f'(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers {layers}) {net})'


def footprint(ref, x, y, pad, *, rotation=0.0, layer="F.Cu"):
    return f'''(footprint "test" (layer "{layer}") (at {x} {y} {rotation})
      (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
      {pad})'''


def board_text(*, pad=None, rotation=0.0, layer="F.Cu", nets=None):
    table = nets or {1: "GND", 2: "SIG"}
    parts = [
        # Off-board: the sole source of placement invalidity, exactly as
        # off-board SH1 makes GND invalid on the pinned real board.
        footprint("SH1", 125, 105, simple_pad('(net 1 "GND")')),
        footprint("U6", 108, 108, pad or custom_pad(), rotation=rotation, layer=layer),
        footprint("R1", 104, 108, simple_pad('(net 2 "SIG")')),
        footprint("R2", 114, 108, simple_pad('(net 2 "SIG")')),
        footprint("G1", 104, 102, simple_pad('(net 1 "GND")')),
        footprint("G2", 114, 102, simple_pad('(net 1 "GND")')),
    ]
    declared = "\n".join(f'(net {i} "{n}")' for i, n in table.items())
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) {declared}
      (gr_rect (start 100 100) (end 120 112) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
      {" ".join(parts)})"""


def write_board(tmp_path, **kwargs):
    path = tmp_path / "custom.kicad_pcb"
    path.write_text(board_text(**kwargs))
    return path


def load_board(path, *, native=False, strategy="grid", single_layer=True, netlist=None, **kwargs):
    from kicad_tools.placement.routing import analyze_routing_placement
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.layers import LayerDefinition, LayerStack, LayerType

    stack = (
        LayerStack([LayerDefinition("F.Cu", 0, LayerType.SIGNAL, True)]) if single_layer else None
    )
    return load_pcb_for_routing(
        str(path),
        netlist=netlist,
        placement_disposition=analyze_routing_placement(path, netlist=netlist),
        force_python=not native,
        strategy=strategy,
        layer_stack=stack,
        **kwargs,
    )


def oracle_copper():
    """Board-frame copper built independently of the production parser."""
    return unary_union(
        [
            box(107.8, 100.0, 108.2, 104.1),
            box(107.8, 105.5, 108.2, 112.0),
            box(107.8, 107.8, 108.2, 108.2),
        ]
    )


def test_actual_primitive_copper_replaces_the_nominal_box(tmp_path):
    router, nets = load_board(write_board(tmp_path))
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    fill = fills[0]
    assert fill.source_net == "GND"
    assert fill.source_net_id == nets["GND"]
    assert fill.source_object_id == "U6.9"
    assert fill.geometry.symmetric_difference(oracle_copper()).area < 1e-9
    # The excluded custom pad is copper, never a routing terminal.
    assert ("U6", "9") not in router.pads
    assert nets["GND"] not in router.nets
    # Copper far outside the nominal 0.4x0.4 box blocks; the slot does not.
    assert not router.grid.fixed_fills.segment_clear((107, 103), (109, 103), 0, 0.1, 0.2)
    assert not router.grid.fixed_fills.segment_clear((107, 110), (109, 110), 0, 0.1, 0.2)
    assert router.grid.fixed_fills.segment_clear((107, 104.8), (109, 104.8), 0, 0.1, 0.2)


@pytest.mark.parametrize(
    "native,strategy", [(False, "grid"), (True, "grid"), (False, "lattice"), (False, "mesh")]
)
def test_real_route_threads_the_slot_between_primitives(tmp_path, native, strategy):
    path = write_board(tmp_path)
    original = path.read_bytes()
    router, nets = load_board(path, native=native, strategy=strategy)
    routes = router.route_net(nets["SIG"])
    assert routes, (native, strategy)
    copper = oracle_copper()
    segments = [seg for route in routes for seg in route.segments]
    assert segments
    lines = []
    for seg in segments:
        line = LineString(((seg.x1, seg.y1), (seg.x2, seg.y2)))
        assert line.distance(copper) >= seg.width / 2 + 0.2 - 1e-4
        lines.append(line)
    # The direct y=108 path is walled off by copper the nominal box omits, so
    # the emitted trace must cross the wall column inside the real slot.
    crossing = unary_union(lines).intersection(LineString([(108, 100), (108, 112)]))
    assert not crossing.is_empty
    assert all(SLOT[0] < y < SLOT[1] for _, y in crossing.coords)
    if native:
        from kicad_tools.router.cpp_backend import CppPathfinder

        assert isinstance(router.router, CppPathfinder)
        assert router.router._fallback_count == 0
    assert path.read_bytes() == original


def test_back_layer_rotation_transform_matches_independent_oracle(tmp_path):
    from shapely.affinity import rotate, translate

    pad = custom_pad(
        at="0.7 -0.4 205",
        layers='"B.Cu" "B.Paste" "B.Mask"',
        primitives=(poly((-0.1, 0.7, 0.1, -0.8)),),
    )
    path = write_board(tmp_path, pad=pad, rotation=205, layer="B.Cu")
    from kicad_tools.router.layers import Layer

    router, _ = load_board(path, single_layer=False)
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    assert fills[0].layer == router.grid.layer_to_index(Layer.B_CU.value)

    # Independent transform: rotate the pad offset about the footprint origin
    # by KiCad's negated angle, then place local copper at the pad's absolute
    # orientation.
    theta = math.radians(-205)
    px = 108 + 0.7 * math.cos(theta) - (-0.4) * math.sin(theta)
    py = 108 + 0.7 * math.sin(theta) + (-0.4) * math.cos(theta)
    local = unary_union([box(-0.1, -0.8, 0.1, 0.7), box(-0.2, -0.2, 0.2, 0.2)])
    expected = translate(rotate(local, -205, origin=(0, 0)), px, py)
    assert fills[0].geometry.symmetric_difference(expected).area < 1e-9


def test_through_hole_custom_pad_covers_copper_layers_only(tmp_path):
    pad = custom_pad(
        layers='"*.Cu" "*.Mask"',
        primitives=(poly((-0.1, -0.1, 0.1, 0.1)),),
        extra="(drill 0.3)",
    )
    path = write_board(tmp_path, pad=pad.replace("smd custom", "thru_hole custom"))
    router, _ = load_board(path, single_layer=False)
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert {fill.layer for fill in fills} == {0, 1}
    assert all(fill.geometry.area == pytest.approx(0.16) for fill in fills)


def test_source_split_keeps_copper_without_same_net_reuse(tmp_path):
    path = write_board(tmp_path)
    original = path.read_bytes()
    mapping = {"SH1.1": "GNDX", "U6.9": "GNDX"}
    router, nets = load_board(path, netlist=mapping)
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    # Authored identity survives the effective-net split.
    assert [fill.source_net for fill in fills] == ["GND"]
    assert fills[0].source_net_id == nets["GND"]
    # GND is still routable through its on-board terminals, and must NOT be
    # allowed to reuse the excluded copper.
    assert nets["GND"] in router.nets
    assert len(router.nets[nets["GND"]]) == 2
    routes = router.route_net(nets["GND"])
    assert routes
    copper = oracle_copper()
    for route in routes:
        for seg in route.segments:
            line = LineString(((seg.x1, seg.y1), (seg.x2, seg.y2)))
            assert line.distance(copper) >= seg.width / 2 + 0.2 - 1e-4
    assert path.read_bytes() == original


def test_reset_and_fine_grid_retry_keep_the_wall(tmp_path):
    from types import SimpleNamespace

    path = write_board(tmp_path)
    router, nets = load_board(path)
    assert router.route_net(nets["SIG"])
    router._reset_for_new_trial()
    assert not router.grid.fixed_fills.segment_clear((107, 103), (109, 103), 0, 0.1, 0.2)
    assert router.route_net(nets["SIG"])

    sealed = custom_pad(primitives=(poly((-0.2, -8.0, 0.2, 4.0)),))
    router, nets = load_board(write_board(tmp_path, pad=sealed))
    router.routing_failures = []

    def coarse_failure(**kwargs):
        router.routing_failures = [SimpleNamespace(net=nets["SIG"], net_name="SIG")]

    router.route_all = coarse_failure
    routes = router.route_all_multi_resolution(use_negotiated=False, timeout=5)
    assert not [route for route in routes if route.net == nets["SIG"]]


@pytest.mark.parametrize("checkpoint", [False, True])
def test_export_reload_preserves_source_pad_bytes(tmp_path, checkpoint):
    from kicad_tools.cli.route_cmd import _write_routed_pcb

    path = write_board(tmp_path)
    router, nets = load_board(path)
    router.routes = router.route_net(nets["SIG"])
    output = tmp_path / "routed.kicad_pcb"
    _write_routed_pcb(path, output, router.placement_preserved_copper, is_checkpoint=checkpoint)
    text = output.read_text()
    assert text.count(custom_pad()) == 1
    reloaded, _ = load_board(output)
    fills = [f for f in reloaded.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    assert fills[0].geometry.symmetric_difference(oracle_copper()).area < 1e-9


@pytest.mark.parametrize(
    "pad,message",
    [
        (custom_pad(primitives=(poly(UPPER_WALL, tag="gr_circle"),)), "gr_circle"),
        (custom_pad(primitives=(poly(UPPER_WALL, fill="no"),)), "unfilled"),
        (custom_pad(primitives=(poly(UPPER_WALL, width=0.1),)), "stroke"),
        (custom_pad(anchor="trapezoid"), "anchor"),
        (custom_pad(layers='"F.Paste" "F.Mask"'), "copper layer"),
        (custom_pad(extra="(padstack (mode front_inner_back))"), "padstack"),
        (custom_pad(size="0 0"), "size"),
    ],
    ids=["primitive", "fill", "stroke", "anchor", "layers", "padstack", "size"],
)
def test_unsupported_fixed_geometry_is_refused(tmp_path, pad, message):
    path = write_board(tmp_path, pad=pad)
    original = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        load_board(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("reverse", [False, True])
def test_self_intersecting_primitive_refused_without_dropping_native_lobe(tmp_path, reverse):
    """KiCad 10.0.6 retains both bow-tie lobes (18.08 mm² with anchor).

    buffer(0) kept only one lobe (9.12 mm²) and incorrectly cleared the
    segment (107.8, 110)..(108.2, 110), inside native copper. Until native
    topology is represented, refuse this primitive before building obstacles.
    """
    points = [(-3, -3), (3, 3), (-3, 3), (3, -3)]
    if reverse:
        points.reverse()
    pts = " ".join(f"(xy {x} {y})" for x, y in points)
    primitive = f"(gr_poly (pts {pts}) (width 0) (fill yes))"
    path = write_board(tmp_path, pad=custom_pad(primitives=[primitive]))
    original = path.read_bytes()
    with pytest.raises(ValueError, match=r"U6\.9: invalid or self-intersecting gr_poly"):
        load_board(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("native", [False, True])
def test_valid_concave_and_disconnected_primitives_keep_physical_clearance(tmp_path, native):
    # One valid concave L and one disjoint rectangle: rejecting malformed
    # outlines must not reject valid concavity or flatten disconnected copper.
    primitive = (
        "(gr_poly (pts (xy -3 -3) (xy 3 -3) (xy 3 -2) "
        "(xy -2 -2) (xy -2 3) (xy -3 3)) (width 0) (fill yes))"
    )
    path = write_board(tmp_path, pad=custom_pad(primitives=[primitive, poly((2, 1, 3, 2))]))
    router, _ = load_board(path, native=native)
    obstacles = router.grid.fixed_fills
    assert not obstacles.segment_clear((106, 105.5), (110, 105.5), 0, 0.1, 0.127)
    assert not obstacles.segment_clear((110.2, 109.5), (110.8, 109.5), 0, 0.1, 0.127)
    assert obstacles.segment_clear((106.5, 109), (109, 109), 0, 0.1, 0.127)
    if native:
        from kicad_tools.router.cpp_backend import CppPathfinder

        assert isinstance(router.router, CppPathfinder)
        grid = router.grid._cpp_grid
        assert grid is not None
        grid = grid._impl
        assert not grid.fixed_fill_clear(106, 105.5, 110, 105.5, 0, 0.1, 0.127)
        assert not grid.fixed_fill_clear(110.2, 109.5, 110.8, 109.5, 0, 0.1, 0.127)
        assert grid.fixed_fill_clear(106.5, 109, 109, 109, 0, 0.1, 0.127)


@pytest.mark.parametrize("width", [-0.2, "nan", "inf", "-inf"])
def test_nonzero_or_nonfinite_primitive_stroke_is_refused(tmp_path, width):
    path = write_board(tmp_path, pad=custom_pad(primitives=(poly(UPPER_WALL, width=width),)))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="stroke width"):
        load_board(path)
    assert path.read_bytes() == original


def test_routable_custom_pad_degrades_instead_of_refusing_the_board(tmp_path, caplog):
    """Issue #5863: one custom pad costs its own net, not the whole board.

    Six of the sixteen pinned real boards never reached the router because a
    single custom pad on a routable net raised. The pad now keeps its ACTUAL
    copper as an obstacle (the slot stays open, the walls still block) and its
    net -- and only its net -- loses routing coverage, out loud.
    """
    path = write_board(tmp_path, pad=custom_pad(net='(net 2 "SIG")'))
    original = path.read_bytes()
    with caplog.at_level(logging.WARNING, logger="kicad_tools.router.io"):
        router, nets = load_board(path)
    assert ("U6", "9") not in router.pads
    assert nets["SIG"] not in router.nets
    assert router.unsupported_geometry_nets == frozenset({"SIG"})
    assert router.unsupported_geometry_pads == (("U6.9", "SIG", "fixed copper"),)
    assert "U6.9" in caplog.text and "SIG" in caplog.text
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    assert fills[0].source_net == "SIG"
    assert fills[0].source_net_id == nets["SIG"]
    assert fills[0].geometry.symmetric_difference(oracle_copper()).area < 1e-9
    assert not router.grid.fixed_fills.segment_clear((107, 103), (109, 103), 0, 0.1, 0.2)
    assert router.grid.fixed_fills.segment_clear((107, 104.8), (109, 104.8), 0, 0.1, 0.2)
    assert path.read_bytes() == original


def test_missing_disposition_degrades_too(tmp_path):
    """No placement disposition is not a reason to refuse the board (#5863)."""
    from kicad_tools.router.io import load_pcb_for_routing

    path = write_board(tmp_path)
    router, nets = load_pcb_for_routing(str(path), force_python=True)
    assert ("U6", "9") not in router.pads
    assert router.unsupported_geometry_nets == frozenset({"GND"})
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    assert fills[0].geometry.symmetric_difference(oracle_copper()).area < 1e-9


def test_paste_only_custom_pad_is_dropped_rather_than_refused(tmp_path):
    """A paste-only pad has no copper, so it is no target AND no obstacle.

    This is the first offending pad on half the affected pinned boards
    (H4/H3/MP4 thermal-paste patterns, issue #5863): refusing a board over
    copper that does not exist blocked routing for nothing.
    """
    pad = custom_pad(net="", layers='"F.Paste" "F.Mask"')
    router, nets = load_board(write_board(tmp_path, pad=pad))
    assert ("U6", "9") not in router.pads
    assert not [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert router.unsupported_geometry_nets == frozenset()
    assert router.unsupported_geometry_pads == (("U6.9", "", "no copper"),)
    # Nothing is excluded: SIG still routes, straight through where the paste
    # pattern lives, because there is no metal there.
    assert nets["SIG"] in router.nets
    assert router.grid.fixed_fills.segment_clear((107, 103), (109, 103), 0, 0.1, 0.2)
    assert router.route_net(nets["SIG"])


def test_unrepresentable_copper_falls_back_to_a_conservative_bound(tmp_path):
    """A primitive we cannot reproduce exactly is BOUNDED, never shrunk (#5863).

    A stroked ``gr_poly`` is real copper ``width / 2`` outside its outline.
    The fallback bound must contain all of it -- an under-estimate would let
    the router lay a trace where the board already has metal.
    """
    stroked = poly((-0.2, -3.0, 0.2, -1.5), width=0.3)
    path = write_board(tmp_path, pad=custom_pad(net='(net 2 "SIG")', primitives=(stroked,)))
    router, nets = load_board(path)
    assert ("U6", "9") not in router.pads
    assert nets["SIG"] not in router.nets
    assert router.unsupported_geometry_nets == frozenset({"SIG"})
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    # True copper: the stroked wall plus the 0.4x0.4 rect anchor.
    true_copper = unary_union(
        [box(107.8, 105.0, 108.2, 106.5).buffer(0.15), box(107.8, 107.8, 108.2, 108.2)]
    )
    assert fills[0].geometry.buffer(1e-9).covers(true_copper)
    # One box per primitive, not a single box swallowing the gap between the
    # wall and the anchor: conservative does not have to mean coarse.
    expected = unary_union([box(107.65, 104.85, 108.35, 106.65), box(107.8, 107.8, 108.2, 108.2)])
    assert fills[0].geometry.geom_type == "MultiPolygon"
    assert fills[0].geometry.symmetric_difference(expected).area < 1e-9
    assert not router.grid.fixed_fills.segment_clear((107, 105.7), (109, 105.7), 0, 0.1, 0.2)
    assert router.grid.fixed_fills.segment_clear((107, 107.2), (109, 107.2), 0, 0.1, 0.2)


# The 0.4x0.4 rect anchor of the fixture pad, in board coordinates. Every
# bounded custom pad keeps it alongside its per-primitive boxes.
ANCHOR_COPPER = box(107.8, 107.8, 108.2, 108.2)

# Arc primitives placed in the pad's local frame around centre (0, -3) with
# radius 1, i.e. board centre (108, 105): the pad sits at (108, 108) with no
# rotation. Each case is (primitive, board-frame arc bound, sweep in degrees
# measured in the same frame, probe segment, probe is blocked).
ARC_CASES = {
    # A 270 deg counter-clockwise sweep. The arc bulges OUTSIDE the hull of
    # its own three authored points on both axes -- it reaches local x -1 and
    # y -2 while start/mid/end only reach x -0.6 and y -2.2 -- so a bound
    # built from the authored points alone would leave real copper exposed.
    "ccw_270": (
        "(gr_arc (start 1 -3) (mid -0.6 -2.2) (end 0 -4) (stroke (width 0.2) (type default)))",
        box(106.9, 103.9, 109.1, 106.1),
        (0.0, 270.0),
        ((106.6, 105.0), (107.2, 105.0)),
        True,
    ),
    # The complementary 90 deg clockwise sweep between the SAME endpoints --
    # only `mid` says which of the two arcs KiCad drew. Its bound must stay
    # the quarter's box: mistaking the direction would inflate it to the
    # ccw_270 box above (and block the probe segment that must stay open).
    "cw_90": (
        "(gr_arc (start 1 -3) (mid 0.8 -3.6) (end 0 -4) (stroke (width 0.2) (type default)))",
        box(107.9, 103.9, 109.1, 105.1),
        (360.0, 270.0),
        ((106.6, 105.0), (107.2, 105.0)),
        False,
    ),
    # Collinear start/mid/end describe a straight segment, not an arc: there
    # is no circumcircle to sweep, and the authored points already bound it.
    "collinear": (
        "(gr_arc (start -1 -3) (mid 0 -3) (end 1 -3) (stroke (width 0.2) (type default)))",
        box(106.9, 104.9, 109.1, 105.1),
        None,
        ((106.6, 105.0), (107.2, 105.0)),
        True,
    ),
}


def arc_copper_oracle(centre, radius, start_deg, end_deg, half_width, *, steps=720):
    """Board-frame copper of a stroked arc, sampled from the analytic circle.

    Built without the production parser, and strictly inside the true stroked
    arc (the samples sit exactly on the arc and shapely's buffer inscribes),
    so ``covers`` on it is a sound containment check.
    """
    cx, cy = centre
    span = end_deg - start_deg
    points = [
        (
            cx + radius * math.cos(math.radians(start_deg + span * i / steps)),
            cy + radius * math.sin(math.radians(start_deg + span * i / steps)),
        )
        for i in range(steps + 1)
    ]
    return LineString(points).buffer(half_width)


@pytest.mark.parametrize("case", list(ARC_CASES), ids=list(ARC_CASES))
def test_arc_primitive_bound_contains_the_whole_swept_arc(tmp_path, case):
    """A degraded ``gr_arc`` is bounded by the arc it actually sweeps (#5863).

    The three authored points do NOT bound an arc -- it bulges outside their
    hull -- so the bound has to add the axis extremes of the circumscribed
    circle that the swept angle reaches, and only those: the sweep direction
    (which `mid` alone reveals) decides which of the four it reaches.
    """
    primitive, arc_bound, sweep, probe, probe_blocked = ARC_CASES[case]
    path = write_board(tmp_path, pad=custom_pad(net='(net 2 "SIG")', primitives=(primitive,)))
    router, nets = load_board(path)
    assert ("U6", "9") not in router.pads
    assert nets["SIG"] not in router.nets
    assert router.unsupported_geometry_nets == frozenset({"SIG"})
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    geometry = fills[0].geometry
    # Containment: every bit of real copper is inside the bound.
    true_arc = (
        LineString([(107.0, 105.0), (109.0, 105.0)]).buffer(0.1)
        if sweep is None
        else arc_copper_oracle((108.0, 105.0), 1.0, sweep[0], sweep[1], 0.1)
    )
    assert geometry.buffer(1e-9).covers(unary_union([true_arc, ANCHOR_COPPER]))
    # Tightness: the arc's own axis-aligned bound, not the whole circle's, and
    # one box per primitive rather than one box swallowing the anchor gap.
    expected = unary_union([arc_bound, ANCHOR_COPPER])
    assert geometry.geom_type == "MultiPolygon"
    assert geometry.symmetric_difference(expected).area < 1e-9
    # The same distinction, seen by the router: the probe crosses where the
    # ccw_270 arc has metal and the cw_90 arc does not.
    clear = router.grid.fixed_fills.segment_clear(probe[0], probe[1], 0, 0.1, 0.2)
    assert bool(clear) == (not probe_blocked)
    assert router.grid.fixed_fills.segment_clear((107, 107.2), (109, 107.2), 0, 0.1, 0.2)


def test_circle_primitive_bound_contains_the_whole_disc(tmp_path):
    """A degraded ``gr_circle`` is bounded by its radius, not by its points.

    ``center``/``end`` are two points on a disc that reaches ``radius`` in
    every direction; bounding their hull would expose three quarters of the
    copper. The bound is the disc's exact box, grown by half the stroke.
    """
    primitive = (
        "(gr_circle (center -0.5 -3) (end 0.5 -3) (stroke (width 0.2) (type default)) (fill yes))"
    )
    path = write_board(tmp_path, pad=custom_pad(net='(net 2 "SIG")', primitives=(primitive,)))
    router, nets = load_board(path)
    assert ("U6", "9") not in router.pads
    assert nets["SIG"] not in router.nets
    assert router.unsupported_geometry_nets == frozenset({"SIG"})
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert len(fills) == 1
    geometry = fills[0].geometry
    # True copper: the filled 1.0 disc at board (107.5, 105), stroked by 0.1.
    true_copper = unary_union([Point(107.5, 105.0).buffer(1.1, quad_segs=256), ANCHOR_COPPER])
    assert geometry.buffer(1e-9).covers(true_copper)
    expected = unary_union([box(106.4, 103.9, 108.6, 106.1), ANCHOR_COPPER])
    assert geometry.geom_type == "MultiPolygon"
    assert geometry.symmetric_difference(expected).area < 1e-9
    # The disc's far side is metal even though no authored point sits there.
    assert not router.grid.fixed_fills.segment_clear((106.0, 105.0), (106.6, 105.0), 0, 0.1, 0.2)
    assert router.grid.fixed_fills.segment_clear((107, 107.2), (109, 107.2), 0, 0.1, 0.2)


def test_arc_primitive_without_three_points_is_refused(tmp_path):
    """An arc missing ``mid`` has no derivable sweep, so it cannot be bounded.

    Without ``mid`` the swept direction is unknowable, so no box can be shown
    to contain the copper -- and an unbounded pad refuses the board rather
    than degrading, exactly like a per-layer padstack.
    """
    primitive = "(gr_arc (start 1 -3) (end 0 -4) (stroke (width 0.2) (type default)))"
    path = write_board(tmp_path, pad=custom_pad(net='(net 2 "SIG")', primitives=(primitive,)))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported routing geometry 'custom'") as excinfo:
        load_board(path)
    # The surfaced error stays the geometry diagnosis; the bound failure is
    # chained as its cause (io.py `raise pad_shape_error from exc`).
    assert "gr_arc needs start, mid and end" in str(excinfo.value.__cause__)
    assert path.read_bytes() == original


def test_degraded_copper_outside_the_routing_stack_is_skipped(tmp_path):
    """A layer nothing routes on cannot host a conflict (#5863).

    ``pad_fixed_fills`` refuses an out-of-stack layer for a placement-excluded
    pad (#5357) so real copper is never dropped silently. For a degraded pad
    the same refusal would restore the whole-board abort, so the layer is
    skipped with a warning instead -- its net is excluded either way.
    """
    pad = custom_pad(net='(net 2 "SIG")', layers='"In1.Cu"')
    router, nets = load_board(write_board(tmp_path, pad=pad), single_layer=False)
    assert ("U6", "9") not in router.pads
    assert router.unsupported_geometry_nets == frozenset({"SIG"})
    assert not [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]


def test_standalone_helpers_keep_rejecting_custom_pads():
    from kicad_tools.router.io import _pad_shape_from_block, _schema_pad_shape
    from kicad_tools.schema.pcb import Pad

    with pytest.raises(ValueError, match="Unsupported routing geometry 'custom'"):
        _pad_shape_from_block(custom_pad(), "U6", "9")
    pad = Pad(number="9", type="smd", shape="custom", position=(0, 0), size=(1, 1), layers=["F.Cu"])
    with pytest.raises(ValueError, match="Unsupported routing geometry 'custom'"):
        _schema_pad_shape(pad, "U6")


PINNED_BOARD = Path(".cache/kct-benchmarks/external/normalized/beagleconnect_freedom.kicad_pcb")
PINNED_SHA256 = "9747958c13a5c625ddd15df7afb3a5a100b6877c134e63c861a2a4c351312d9d"


@pytest.mark.slow
@pytest.mark.skipif(not PINNED_BOARD.exists(), reason="pinned benchmark board not fetched")
def test_pinned_board_custom_pad_is_fixed_copper():
    """Acceptance for the pinned BeagleConnect input (issue #5357).

    Reproduce with::

        uv run python benchmarks/external/fetch_boards.py --board beagleconnect_freedom
        uv run python benchmarks/external/normalize.py --board beagleconnect_freedom

    This is the actual loader acceptance gate: retain the full outline and
    unmodified placement identities. Outline prerequisites #5367/#5372 are
    integrated; the unchanged pinned board now passes this loader check.
    Full default routing and artifact/report acceptance remain #5348/#4946.
    """
    import hashlib
    import math

    from shapely.affinity import rotate, translate

    from kicad_tools.placement.routing import analyze_routing_placement
    from kicad_tools.router import io as router_io
    from kicad_tools.schema.pcb import PCB

    before = hashlib.sha256(PINNED_BOARD.read_bytes()).hexdigest()
    assert before == PINNED_SHA256
    disposition = analyze_routing_placement(PINNED_BOARD)
    assert disposition.invalid_references == frozenset({"SH1"})
    assert disposition.invalid_nets == frozenset({"GND"})

    router, nets = router_io.load_pcb_for_routing(
        str(PINNED_BOARD), placement_disposition=disposition
    )
    # U6.9 no longer refuses; it is copper, not a target.
    assert ("U6", "9") not in router.pads
    assert nets["GND"] not in router.nets
    fills = [f for f in router.grid.fixed_fills.fills if f.source_object_id == "U6.9"]
    assert len(fills) == 1
    assert fills[0].source_net == "GND"

    pcb = PCB.load(PINNED_BOARD)
    origin_x, origin_y = pcb._board_origin
    footprint_u6 = next(fp for fp in pcb.footprints if fp.reference == "U6")
    pad = next(p for p in footprint_u6.pads if p.number == "9")
    theta = math.radians(-footprint_u6.rotation)
    px = footprint_u6.position[0] + pad.position[0] * math.cos(theta)
    px -= pad.position[1] * math.sin(theta)
    py = footprint_u6.position[1] + pad.position[0] * math.sin(theta)
    py += pad.position[1] * math.cos(theta)
    nominal = box(-pad.size[0] / 2, -pad.size[1] / 2, pad.size[0] / 2, pad.size[1] / 2)
    authored = Polygon([(0, 0.8), (0.1, 0.8), (0.1, -0.8), (-0.1, -0.8), (-0.1, 0.7)])
    place = lambda shape: translate(  # noqa: E731
        rotate(shape, -pad.rotation, origin=(0, 0)), px + origin_x, py + origin_y
    )
    assert (
        fills[0].geometry.symmetric_difference(place(unary_union([nominal, authored]))).area < 1e-12
    )
    # Real copper the nominal box would have dropped.
    assert fills[0].geometry.difference(place(nominal)).area > 0.1
    assert hashlib.sha256(PINNED_BOARD.read_bytes()).hexdigest() == before


def test_concave_neighbour_space_survives_and_multipolygon_reaches_native(tmp_path):
    arm = poly((0.2, -2.5, 3.0, -2.1))
    path = write_board(
        tmp_path, pad=custom_pad(primitives=(poly(UPPER_WALL), poly(LOWER_WALL), arm))
    )
    router, _ = load_board(path)
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    notch = Polygon([(108.4, 106.0), (111.0, 106.0), (111.0, 107.0), (108.4, 107.0)])
    assert fills[0].geometry.intersection(notch).area == 0
    assert router.grid.fixed_fills.segment_clear((108.6, 106.5), (110.8, 106.5), 0, 0.1, 0.2)
    if not is_cpp_available():
        return
    from kicad_tools.router.cpp_backend import CppGrid

    native = CppGrid.from_routing_grid(router.grid)._impl
    assert not native.fixed_fill_clear(107, 103, 109, 103, 0, 0.1, 0.2)
    assert native.fixed_fill_clear(108.6, 106.5, 110.8, 106.5, 0, 0.1, 0.2)
