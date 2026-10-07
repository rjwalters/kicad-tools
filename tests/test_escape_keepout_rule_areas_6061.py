"""Escape stubs and escape vias honour keepout rule areas (Issue #6061).

#6008 / #6062 put board keepout rule areas on the routing grid, but the
escape-stub generator places copper by world-coordinate geometry: it never
consulted the areas, so BGA / QFN fan-out stubs and dogbone vias landed
inside ``(tracks not_allowed)`` / ``(vias not_allowed)`` zones.  These tests
pin, on BOTH backends:

* without keepouts the fan-out of a 6x6 BGA and a QFN-32 reaches into three
  regions (so the keepout tests below are not vacuous);
* with an all-layer track+via area, a via-only area and an F.Cu-only track
  area over those regions, no escape via or stub copper enters any of them;
* a ``spatial_keepouts`` filter that exempts the package's nets lets the
  fan-out back in, one that names them keeps it out;
* the sub-grid escape pre-pass gets the areas installed and gates on them;
* KiCad agrees: ``kicad-cli pcb drc`` with the #6039 ``intersectsArea``
  rules reports no ``items_not_allowed`` (skipped without kicad-cli).

Fixtures (``_keepout``, ``BACKENDS``) are shared with the #6008 suite.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from kicad_tools.router.io import load_pcb_for_routing, merge_routes_into_pcb
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules
from tests.test_grid_keepout_rule_areas_6008 import BACKENDS, _keepout

ALL_LAYERS = '"F.Cu" "In1.Cu" "In2.Cu" "B.Cu"'

# BGA U1 centred at (110, 110): 6x6, 0.8 mm pitch -> pads span 108.0..112.0.
# QFN U2 centred at (128, 110): 8 pads/side, 0.5 mm pitch, pad rows at +-2.4.
#
# (name, polygon, layers, tracks, vias, uid)
AREAS: list[tuple[str, list[tuple[float, float]], str, str, str, int]] = [
    # East of the BGA, every layer, tracks and vias.
    (
        "bga_east",
        [(112.3, 104.0), (118.0, 104.0), (118.0, 116.0), (112.3, 116.0)],
        ALL_LAYERS,
        "not_allowed",
        "not_allowed",
        1,
    ),
    # South of the QFN, vias only.
    (
        "qfn_south_vias",
        [(124.0, 111.0), (132.0, 111.0), (132.0, 116.0), (124.0, 116.0)],
        ALL_LAYERS,
        "allowed",
        "not_allowed",
        2,
    ),
    # West of the QFN, F.Cu tracks only.
    (
        "qfn_west_ftracks",
        [(123.0, 106.0), (125.4, 106.0), (125.4, 114.0), (123.0, 114.0)],
        '"F.Cu"',
        "not_allowed",
        "allowed",
        3,
    ),
]


def _bga(cx: float, cy: float, n: int = 6, pitch: float = 0.8, net0: int = 1):
    pads, nets, k = [], [], net0
    for r in range(n):
        for c in range(n):
            x, y = (c - (n - 1) / 2) * pitch, (r - (n - 1) / 2) * pitch
            pads.append(
                f'(pad "{chr(65 + r)}{c + 1}" smd circle (at {x:.3f} {y:.3f}) (size 0.4 0.4) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (net {k} "/B{k}"))'
            )
            nets.append((k, f"/B{k}"))
            k += 1
    fp = f"""  (footprint "Package_BGA:BGA-36" (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000077") (at {cx} {cy})
    (property "Reference" "U1" (at 0 -4 0) (layer "F.SilkS"))
    {" ".join(pads)}
  )
"""
    return fp, nets


def _qfn(cx: float, cy: float, per: int = 8, pitch: float = 0.5, net0: int = 100):
    pads, nets, k = [], [], net0
    off, span = 2.4, (per - 1) / 2 * pitch
    for side in range(4):
        for i in range(per):
            t = -span + i * pitch
            if side == 0:
                x, y, w, h = -off, t, 0.8, 0.25
            elif side == 1:
                x, y, w, h = t, off, 0.25, 0.8
            elif side == 2:
                x, y, w, h = off, -t, 0.8, 0.25
            else:
                x, y, w, h = -t, -off, 0.25, 0.8
            pads.append(
                f'(pad "{len(nets) + 1}" smd rect (at {x:.3f} {y:.3f}) (size {w} {h}) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (net {k} "/Q{k}"))'
            )
            nets.append((k, f"/Q{k}"))
            k += 1
    fp = f"""  (footprint "Package_DFN_QFN:QFN-32" (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000078") (at {cx} {cy})
    (property "Reference" "U2" (at 0 -4 0) (layer "F.SilkS"))
    {" ".join(pads)}
  )
"""
    return fp, nets


def _board(with_areas: bool) -> str:
    bga_fp, bga_nets = _bga(110.0, 110.0)
    qfn_fp, qfn_nets = _qfn(128.0, 110.0)
    nets = '  (net 0 "")\n' + "".join(f'  (net {k} "{n}")\n' for k, n in bga_nets + qfn_nets)
    extra = (
        "".join(
            _keepout(name, poly, layers=layers, tracks=tracks, vias=vias, uid=uid)
            for name, poly, layers, tracks, vias, uid in AREAS
        )
        if with_areas
        else ""
    )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
{nets}  (gr_rect (start 100 100) (end 140 120)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{bga_fp}{qfn_fp}{extra})
"""


def _rules() -> DesignRules:
    return DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.4,
        via_clearance=0.1,
    )


def _load(tmp_path: Path, with_areas: bool, force_python: bool):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_board(with_areas))
    router, _ = load_pcb_for_routing(
        str(pcb),
        rules=_rules(),
        use_pcb_rules=False,
        validate_drc=False,
        force_python=force_python,
    )
    return router


def _escape(router) -> list:
    dense = router.detect_dense_packages()
    assert {p.ref for p in dense} == {"U1", "U2"}, dense
    return router.generate_escape_routes(dense)


# -- exact geometry: copper vs an axis-aligned rectangle ---------------------


def _bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _point_rect_dist(x: float, y: float, rect) -> float:
    x0, y0, x1, y1 = rect
    dx = max(x0 - x, 0.0, x - x1)
    dy = max(y0 - y, 0.0, y - y1)
    return math.hypot(dx, dy)


def _segment_rect_dist(seg, rect) -> float:
    """Min distance from the segment's centreline to ``rect`` (0 = touches)."""
    n = max(2, math.ceil(math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1) / 0.005) + 1)
    return min(
        _point_rect_dist(
            seg.x1 + (seg.x2 - seg.x1) * i / (n - 1),
            seg.y1 + (seg.y2 - seg.y1) * i / (n - 1),
            rect,
        )
        for i in range(n)
    )


_LAYER_NAMES = {Layer.F_CU: '"F.Cu"', Layer.B_CU: '"B.Cu"'}


def _intrusions(routes, areas=AREAS) -> list[str]:
    """Every escape primitive whose copper enters an area that forbids it."""
    found: list[str] = []
    for name, poly, layers, tracks, vias, _uid in areas:
        rect = _bbox(poly)
        for route in routes:
            if vias == "not_allowed":
                for via in route.vias:
                    if _point_rect_dist(via.x, via.y, rect) < via.diameter / 2:
                        found.append(f"via {route.net_name} @({via.x:.3f},{via.y:.3f}) in {name}")
            if tracks == "not_allowed":
                for seg in route.segments:
                    layer_name = _LAYER_NAMES.get(seg.layer, f'"{seg.layer.kicad_name}"')
                    if layer_name not in layers:
                        continue
                    if _segment_rect_dist(seg, rect) < seg.width / 2:
                        found.append(f"track {route.net_name} {layer_name} in {name}")
    return found


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_python", BACKENDS)
def test_baseline_fanout_reaches_every_area_region(tmp_path: Path, force_python: bool) -> None:
    """Without keepouts the fan-out lands in all three regions -- otherwise
    the avoidance test below would pass vacuously."""
    routes = _escape(_load(tmp_path, with_areas=False, force_python=force_python))
    hit = {msg.rsplit(" in ", 1)[1] for msg in _intrusions(routes)}
    assert hit == {name for name, *_ in AREAS}, hit


@pytest.mark.parametrize("force_python", BACKENDS)
def test_escape_copper_avoids_keepout_rule_areas(tmp_path: Path, force_python: bool) -> None:
    router = _load(tmp_path, with_areas=True, force_python=force_python)
    routes = _escape(router)
    # The areas were installed by the escape entry point itself.
    assert len(router.grid._rule_area_keepouts) == 3
    assert routes, "the packages must still escape away from the areas"
    assert _intrusions(routes) == []
    # Tracks remain legal inside the via-only area (KiCad semantics).
    south = _bbox(AREAS[1][1])
    assert any(
        _segment_rect_dist(seg, south) < seg.width / 2 for r in routes for seg in r.segments
    ), "via-only area must not block escape tracks"


@pytest.mark.parametrize("force_python", BACKENDS)
@pytest.mark.parametrize(
    ("spatial_filter", "enters"),
    [({"only_classes": ["BGA"]}, False), ({"except_classes": ["BGA"]}, True)],
    ids=["only-bga", "except-bga"],
)
def test_escape_gate_respects_spatial_keepouts_filters(
    tmp_path: Path, force_python: bool, spatial_filter: dict, enters: bool
) -> None:
    from kicad_tools.router.rules import NetClassRouting

    router = _load(tmp_path, with_areas=True, force_python=force_python)
    bga_class = NetClassRouting(name="BGA")
    for k in range(1, 37):
        router.net_class_map[f"/B{k}"] = bga_class
    router._spatial_keepout_filters = {"bga_east": spatial_filter}
    routes = _escape(router)
    bga_hits = _intrusions(routes, AREAS[:1])
    assert bool(bga_hits) is enters, bga_hits
    # The unfiltered QFN areas still hold either way.
    assert _intrusions(routes, AREAS[1:]) == []


def test_grid_predicates_include_static_areas(tmp_path: Path) -> None:
    """The escape helpers see the all-nets (static-cell) areas the plain
    pathfinder predicates deliberately skip."""
    from kicad_tools.router.rule_area_grid import segment_hits_rule_area, via_hits_rule_area

    router = _load(tmp_path, with_areas=True, force_python=True)
    router._install_grid_rule_area_keepouts()
    grid = router.grid
    f_cu = grid.layer_to_index(Layer.F_CU.value)
    b_cu = grid.layer_to_index(Layer.B_CU.value)
    gx, gy = grid.world_to_grid(115.0, 110.0)  # inside bga_east
    assert not grid.rule_area_trace_blocked(gx, gy, f_cu, 1, 2)
    assert grid.rule_area_trace_blocked(gx, gy, f_cu, 1, 2, include_static=True)
    assert via_hits_rule_area(grid, 115.0, 110.0, 1, 0.4, 0.1)
    # Via-only area: blocks vias, not tracks.
    assert via_hits_rule_area(grid, 128.0, 114.0, 100, 0.4, 0.1)
    assert not segment_hits_rule_area(grid, 128.0, 112.0, 128.0, 115.0, f_cu, 100, 0.15, 0.1)
    # F.Cu-only track area: F.Cu blocked, B.Cu free.
    assert segment_hits_rule_area(grid, 123.5, 110.0, 124.5, 110.0, f_cu, 100, 0.15, 0.1)
    assert not segment_hits_rule_area(grid, 123.5, 110.0, 124.5, 110.0, b_cu, 100, 0.15, 0.1)
    # Far from every area: clear.
    assert not via_hits_rule_area(grid, 104.0, 104.0, 1, 0.4, 0.1)


def test_subgrid_prepass_installs_and_honours_areas(tmp_path: Path) -> None:
    """``prepare_subgrid_escapes`` (the ``route_with_subgrid`` pre-pass)
    installs the areas before generating stubs, and no stub enters one."""
    router = _load(tmp_path, with_areas=True, force_python=True)
    assert router.grid._rule_area_keepouts is None
    result = router.prepare_subgrid_escapes()
    assert router.grid._rule_area_keepouts is not None
    assert len(router.grid._rule_area_keepouts) == 3
    routes = router._subgrid.get_escape_routes(result)
    assert _intrusions(routes) == []


def test_subgrid_leg_gate_rejects_keepout_candidate(tmp_path: Path) -> None:
    """A sub-grid candidate leg into a track keepout is rejected and the next
    candidate is taken."""
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.subgrid import SubGridPad

    router = _load(tmp_path, with_areas=True, force_python=True)
    router._install_grid_rule_area_keepouts()
    sub = router._subgrid
    pad = Pad(
        x=122.4,
        y=110.02,
        width=0.2,
        height=0.2,
        net=100,
        net_name="/Q100",
        layer=Layer.F_CU,
        ref="TP1",
        pin="1",
    )
    gx, gy = router.grid.world_to_grid(pad.x, pad.y)
    snap_x, snap_y = router.grid.grid_to_world(gx, gy)
    sgp = SubGridPad(
        pad=pad,
        grid_x=gx,
        grid_y=gy,
        offset_x=pad.x - snap_x,
        offset_y=pad.y - snap_y,
        snap_x=snap_x,
        snap_y=snap_y,
    )
    into_area = (0.0, *router.grid.world_to_grid(123.2, 110.0), 123.2, 110.0)
    away = (1.0, *router.grid.world_to_grid(122.0, 110.0), 122.0, 110.0)
    esc = sub._try_candidates_with_clearance(sgp, [into_area, away], 0.15, Layer.F_CU, {})
    assert esc is not None
    assert esc.snap_point == (122.0, 110.0)


# ---------------------------------------------------------------------------
# KiCad cross-check: the #6039 ``intersectsArea`` DRU rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_python", BACKENDS)
def test_kicad_drc_intersects_area_finds_nothing(tmp_path: Path, force_python: bool) -> None:
    from kicad_tools.cli.runner import find_kicad_cli

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        pytest.skip("kicad-cli not installed")

    router = _load(tmp_path, with_areas=True, force_python=force_python)
    routes = _escape(router)
    assert routes

    out = tmp_path / "routed.kicad_pcb"
    out.write_text(merge_routes_into_pcb(_board(True), router.to_sexp(skip_cleanup=True)))
    rules = ["(version 1)"]
    for name, _poly, _layers, tracks, vias, _uid in AREAS:
        kinds = " ".join(k for k, v in (("track", tracks), ("via", vias)) if v == "not_allowed")
        rules.append(
            f'(rule "ko_{name}" (condition "A.intersectsArea(\'{name}\')") '
            f"(constraint disallow {kinds}))"
        )
    (tmp_path / "routed.kicad_dru").write_text("\n".join(rules) + "\n")
    (tmp_path / "routed.kicad_pro").write_text("{}")
    report = tmp_path / "drc.json"
    subprocess.run(
        [str(kicad_cli), "pcb", "drc", "--severity-all", "--format", "json", "-o", str(report)]
        + [str(out)],
        capture_output=True,
        check=False,
        timeout=300,
    )
    data = json.loads(report.read_text())
    not_allowed = [v for v in data.get("violations", []) if v["type"] == "items_not_allowed"]
    assert not_allowed == [], [
        (v["description"], [i["description"] for i in v["items"]]) for v in not_allowed
    ]
