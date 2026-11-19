"""Footprint-level Edge.Cuts acts as board outline everywhere (issue #9593).

KiCad treats ``fp_line``/``fp_arc``/``fp_circle``/``fp_poly``/``fp_rect``/
``fp_curve`` graphics on Edge.Cuts inside a footprint as board outline (real
boards ship this way -- SparkFun's IoT RedBoard ESP32-P4 keeps its whole
outline in a locked footprint).  These tests pin, for every outline consumer:

- the shared reader (``core.board_outline``) re-emits footprint graphics in
  board coordinates (rotation, back-side mirror, arcs, multi-footprint);
- ``PCB.get_board_outline`` / ``get_board_outline_segments`` /
  ``board_size`` include them;
- ``kct``'s edge-clearance rule agrees with ``kicad-cli pcb drc`` (the pinned
  10.0.6 release, run with the pinned config tables) on the committed
  fixtures;
- the router's outline reader/keep-in, the placer's outline, silkscreen
  board material and DSN export all see the footprint outline.

Fixture A (``fp_outline_only``): the only outline is a footprint ``fp_rect``
(-20,-15)..(20,15) in a footprint at (60, 50) rotated 30 deg.  Rotated board
corners, from ``anchor + R(30 deg) . local`` (Y-down rotate, verified against
pcbnew 10.0.1 goldens in ``tests/fixtures/fp_zone_transform/``):

    (35.179492, 47.009619), (69.820508, 27.009619),
    (84.820508, 52.990381), (50.179492, 72.990381)

PROBE_IN runs inside the outline (clean); PROBE_CROSS crosses the rotated
right edge; PROBE_NEAR runs 0.15 mm inside the bottom edge (violates KiCad's
0.5 mm default edge clearance).

Fixture B (``board_outline_with_fp_cutout``): board-level ``gr_rect``
(0,0)..(80,60) plus a footprint cut-out drawn as four ``fp_line`` edges
making the hole (30,25)..(50,35).  PROBE_CLEAN stays in board material;
PROBE_THRU_CUTOUT crosses the cut-out.
"""

import json
import math
import re
import subprocess
from pathlib import Path

import pytest

from kicad_tools.core.board_outline import (
    board_outline_bounds,
    board_outline_segments,
    footprint_outline_graphics,
    outline_graphics,
)
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string

FIXTURES = Path(__file__).parent / "fixtures" / "footprint_edge_cuts"
FIXTURE_A = FIXTURES / "fp_outline_only.kicad_pcb"
FIXTURE_B = FIXTURES / "board_outline_with_fp_cutout.kicad_pcb"

COS30 = math.cos(math.radians(30.0))
SIN30 = math.sin(math.radians(30.0))
ANCHOR_A = (60.0, 50.0)


def _to_board_a(lx: float, ly: float) -> tuple[float, float]:
    """Forward footprint frame of fixture A: anchor + R(30 deg) . local."""
    return (ANCHOR_A[0] + lx * COS30 + ly * SIN30, ANCHOR_A[1] - lx * SIN30 + ly * COS30)


# Fixture A rotated corners, in fp_rect corner order.
A_CORNERS = [
    _to_board_a(x, y) for x, y in ((-20.0, -15.0), (20.0, -15.0), (20.0, 15.0), (-20.0, 15.0))
]
A_BOUNDS = (
    min(x for x, _ in A_CORNERS),
    min(y for _, y in A_CORNERS),
    max(x for x, _ in A_CORNERS),
    max(y for _, y in A_CORNERS),
)

FIXTURE_A_PROBES = {
    "PROBE_IN": ((60.0, 50.0), (65.0, 50.0)),
    "PROBE_CROSS": ((65.0, 45.0), (95.0, 50.0)),
    "PROBE_NEAR": ((39.584619, 44.639523), (65.565381, 29.639523)),
}
FIXTURE_B_PROBES = {
    "PROBE_CLEAN": ((10.0, 10.0), (20.0, 10.0)),
    "PROBE_THRU_CUTOUT": ((40.0, 30.0), (52.0, 30.0)),
}

KICAD_DEFAULT_EDGE_CLEARANCE_MM = 0.5


def _point_segment_distance(point, segment) -> float:
    (px, py), ((ax, ay), (bx, by)) = point, segment
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _kct_edge_probes(pcb_path: Path, probes: dict, clearance: float) -> set[str]:
    from kicad_tools.validate.rules.edge import EdgeClearanceRule

    pcb = PCB.load(pcb_path)
    ox, oy = pcb.board_origin
    rules = DesignRules(
        min_copper_to_edge_mm=clearance,
        min_trace_width_mm=0.2,
        min_clearance_mm=0.2,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.15,
    )
    results = EdgeClearanceRule().check(pcb, rules)
    named = set()
    for violation in results.violations:
        sheet = (violation.location[0] + ox, violation.location[1] + oy)
        best = min(probes, key=lambda name: _point_segment_distance(sheet, probes[name]))
        assert _point_segment_distance(sheet, probes[best]) < 1.0, (sheet, violation.message)
        named.add(best)
    return named


# ------------------------------------------------------------- shared reader


def test_fixture_a_bounds_are_the_rotated_rect() -> None:
    pcb = PCB.load(FIXTURE_A)
    assert pcb.outline_error == ""
    bounds = board_outline_bounds(pcb._sexp)
    assert bounds == pytest.approx(A_BOUNDS, abs=1e-6)
    assert pcb.board_size == pytest.approx(
        (A_BOUNDS[2] - A_BOUNDS[0], A_BOUNDS[3] - A_BOUNDS[1]), abs=1e-6
    )


def test_fixture_a_segments_form_the_closed_rotated_rect() -> None:
    from collections import Counter

    pcb = PCB.load(FIXTURE_A)
    segments = pcb.get_board_outline_segments()
    assert len(segments) == 4

    # get_board_outline_segments is board-relative; fixture A's origin is the
    # rotated outline's min corner.
    ox, oy = pcb.board_origin
    relative_corners = [(x - ox, y - oy) for x, y in A_CORNERS]

    def edge_key(p, q):
        return tuple(sorted((round(p[0], 6), round(p[1], 6), round(q[0], 6), round(q[1], 6))))

    expected = {edge_key(relative_corners[i], relative_corners[(i + 1) % 4]) for i in range(4)}
    assert {edge_key(s, e) for s, e in segments} == expected
    # Closed: each rotated corner is the endpoint of exactly two edges.
    endpoint_counts: Counter = Counter()
    for s, e in segments:
        endpoint_counts[(round(s[0], 6), round(s[1], 6))] += 1
        endpoint_counts[(round(e[0], 6), round(e[1], 6))] += 1
    assert set(endpoint_counts.values()) == {2}


def test_fixture_a_polygon_is_chained_board_relative() -> None:
    pcb = PCB.load(FIXTURE_A)
    polygon = pcb.get_board_outline()
    # Closed chain, board-relative (origin = outline min corner subtracted).
    assert len(polygon) >= 3
    assert polygon[0] == polygon[-1]
    ox, oy = pcb.board_origin
    expected = sorted((x - ox, y - oy) for x, y in A_CORNERS)
    actual = sorted(polygon[:-1])
    assert all(
        math.isclose(ax, ex, abs_tol=1e-6) and math.isclose(ay, ey, abs_tol=1e-6)
        for (ax, ay), (ex, ey) in zip(actual, expected, strict=True)
    )


def test_fixture_b_bounds_and_cutout_segments() -> None:
    pcb = PCB.load(FIXTURE_B)
    assert pcb.outline_error == ""
    assert board_outline_bounds(pcb._sexp) == (0.0, 0.0, 80.0, 60.0)
    segments = pcb.get_board_outline_segments()
    # 4 board-level rect edges + 4 footprint cut-out edges.
    assert len(segments) == 4 + 4
    endpoints = sorted(end for segment in segments for end in segment)
    cutout_corners = [(30.0, 25.0), (50.0, 25.0), (50.0, 35.0), (30.0, 35.0)]
    board_corners = [(0.0, 0.0), (80.0, 0.0), (80.0, 60.0), (0.0, 60.0)]
    expected = sorted([*cutout_corners, *cutout_corners, *board_corners, *board_corners])
    assert all(
        math.isclose(ax, ex, abs_tol=1e-9) and math.isclose(ay, ey, abs_tol=1e-9)
        for (ax, ay), (ex, ey) in zip(endpoints, expected, strict=True)
    )
    assert pcb.get_board_outline()  # the outer rect still chains


def test_outline_graphics_yields_footprint_graphics_without_double_counting() -> None:
    sexp = parse_string(FIXTURE_B.read_text())
    tags = [node.tag for node in outline_graphics(sexp)]
    # 1 board gr_rect + 4 synthesized gr_line from the footprint cut-out.
    assert tags == ["gr_rect", "gr_line", "gr_line", "gr_line", "gr_line"]
    fp_only = [node.tag for node in footprint_outline_graphics(sexp)]
    assert fp_only == ["gr_line", "gr_line", "gr_line", "gr_line"]


def test_board_level_outline_graphics_are_untouched() -> None:
    sexp = parse_string(
        """(kicad_pcb
             (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts"))
             (gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts"))
             (footprint "R" (layer "F.Cu") (at 5 5)
               (fp_line (start -1 -1) (end 1 -1) (layer "F.SilkS")))  ; silk only: ignored
           )"""
    )
    assert [node.tag for node in outline_graphics(sexp)] == ["gr_line", "gr_rect"]
    assert list(footprint_outline_graphics(sexp)) == []


def test_back_side_footprint_mirrors_y_before_rotating() -> None:
    sexp = parse_string(
        """(kicad_pcb
             (footprint "CUT" (layer "B.Cu") (at 100 100 90)
               (fp_line (start 1 2) (end 3 4) (layer "Edge.Cuts")))
           )"""
    )
    nodes = list(footprint_outline_graphics(sexp))
    assert [node.tag for node in nodes] == ["gr_line"]
    start = nodes[0].find_child("start")
    # Mirror y first (1,2) -> (1,-2), then R(90): x' = y, y' = -x -> (-2,-1).
    assert start.get_float(0) == pytest.approx(100 + -2.0)
    assert start.get_float(1) == pytest.approx(100 + -1.0)


def test_footprint_arc_transforms_start_mid_end() -> None:
    anchor = (10.0, 10.0)
    cos30 = math.cos(math.radians(30.0))
    sin30 = math.sin(math.radians(30.0))

    def to_board(lx, ly):
        return (anchor[0] + lx * cos30 + ly * sin30, anchor[1] - lx * sin30 + ly * cos30)

    sexp = parse_string(
        """(kicad_pcb
             (footprint "ARC" (layer "F.Cu") (at 10 10 30)
               (fp_arc (start -5 0) (mid 0 -5) (end 5 0) (layer "Edge.Cuts")))
           )"""
    )
    node = next(iter(footprint_outline_graphics(sexp)))
    assert node.tag == "gr_arc"
    points = [node.find_child(tag) for tag in ("start", "mid", "end")]
    expected = [to_board(-5.0, 0.0), to_board(0.0, -5.0), to_board(5.0, 0.0)]
    for child, want in zip(points, expected, strict=True):
        assert child.get_float(0) == pytest.approx(want[0], abs=1e-9)
        assert child.get_float(1) == pytest.approx(want[1], abs=1e-9)
    segments = board_outline_segments(sexp)
    assert segments.max_error_mm > 0.0  # the arc was tessellated, not flattened
    bounds = board_outline_bounds(sexp)
    assert bounds is not None and all(math.isfinite(v) for v in bounds)


def test_legacy_fp_arc_angle_form() -> None:
    # Legacy fp_arc: (start) = centre, (end) = first on-arc point, (angle) = sweep.
    # Normalized the same way as legacy gr_arc: on-arc point, its rotation by
    # angle/2 about the centre, then by the full angle (Y-down rotate).
    sexp = parse_string(
        """(kicad_pcb
             (footprint "ARC" (layer "F.Cu") (at 10 10 0)
               (fp_arc (start 0 0) (end 5 0) (angle 90) (layer "Edge.Cuts")))
           )"""
    )
    node = next(iter(footprint_outline_graphics(sexp)))
    # The on-arc point (5,0) at anchor (10,10) is exact; the sweep must be
    # identical to an equivalent board-level legacy gr_arc (same treatment,
    # not a re-derivation).
    board_sexp = parse_string(
        """(kicad_pcb
             (gr_arc (start 10 10) (end 15 10) (angle 90) (layer "Edge.Cuts"))
           )"""
    )
    board_node = next(n for n in outline_graphics(board_sexp) if n.tag == "gr_arc")

    from kicad_tools.core.board_outline import outline_node_points

    fp_points = outline_node_points(node)
    board_arc_points = outline_node_points(board_node)
    assert all(
        fx == pytest.approx(bx, abs=1e-12) and fy == pytest.approx(by, abs=1e-12)
        for (fx, fy), (bx, by) in zip(fp_points, board_arc_points, strict=True)
    )
    # The on-arc point (5,0) is placement-exact in the normalized chain.
    assert fp_points[0] == pytest.approx((15.0, 10.0))


def test_multiple_footprints_form_one_closed_outline() -> None:
    sexp = parse_string(
        """(kicad_pcb
             (footprint "P1" (layer "F.Cu") (at 0 0)
               (fp_line (start 0 0) (end 10 0) (layer "Edge.Cuts"))
               (fp_line (start 10 0) (end 10 10) (layer "Edge.Cuts")))
             (footprint "P2" (layer "F.Cu") (at 0 0)
               (fp_line (start 10 10) (end 0 10) (layer "Edge.Cuts"))
               (fp_line (start 0 10) (end 0 0) (layer "Edge.Cuts")))
           )"""
    )
    segments = board_outline_segments(sexp)
    assert len(segments) == 4
    assert board_outline_bounds(sexp) == (0.0, 0.0, 10.0, 10.0)


def test_rotated_fp_rect_emits_four_lines_not_a_rect() -> None:
    sexp = parse_string(
        """(kicad_pcb
             (footprint "O" (layer "F.Cu") (at 50 50 45)
               (fp_rect (start -10 -10) (end 10 10) (layer "Edge.Cuts")))
           )"""
    )
    nodes = list(footprint_outline_graphics(sexp))
    assert [node.tag for node in nodes] == ["gr_line"] * 4
    sqrt2 = math.sqrt(2.0)
    # 45 deg rotation puts the rect corners at (50 +/- 10*sqrt2, 50) and
    # (50, 50 +/- 10*sqrt2); each line start is one of those four corners.
    expected_corners = {
        (50.0 + 10 * sqrt2, 50.0),
        (50.0 - 10 * sqrt2, 50.0),
        (50.0, 50.0 + 10 * sqrt2),
        (50.0, 50.0 - 10 * sqrt2),
    }
    actual_starts = {
        (round(n.find_child("start").get_float(0), 6), round(n.find_child("start").get_float(1), 6))
        for n in nodes
    }
    assert actual_starts == {(round(x, 6), round(y, 6)) for x, y in expected_corners}
    segments = board_outline_segments(sexp)
    assert len(segments) == 4


# ------------------------------------------------------- kct check vs kicad-cli


def _probe_net(violation: dict) -> str:
    for item in violation.get("items", []):
        match = re.search(r"\[([^\]]+)\]", item.get("description", ""))
        if match:
            return match.group(1)
    return ""


def _kicad_cli_edge_probes(pcb_path: Path, tmp_path: Path) -> set[str]:
    from kicad_tools.cli.kicad_toolchain import (
        kicad_cli_env,
        scratch_config_home,
    )
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    report = tmp_path / "drc.json"
    # Run on a copy: kicad-cli writes a .kicad_prl next to the board.
    board_copy = tmp_path / pcb_path.name
    board_copy.write_bytes(pcb_path.read_bytes())
    with scratch_config_home("kct-fp-edge-") as config_home:
        proc = subprocess.run(
            [
                str(cli),
                "pcb",
                "drc",
                "--format",
                "json",
                "--exit-code-violations",
                "-o",
                str(report),
                str(board_copy),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=kicad_cli_env(config_home),
        )
    assert proc.returncode in (0, 5), proc.stderr  # 5 = violations found
    data = json.loads(report.read_text())
    return {_probe_net(v) for v in data["violations"] if v["type"] == "copper_edge_clearance"}


@pytest.mark.parametrize(
    "fixture, probes, kicad_violating, kct_clean",
    [
        (FIXTURE_A, FIXTURE_A_PROBES, {"PROBE_CROSS", "PROBE_NEAR"}, {"PROBE_IN"}),
        (FIXTURE_B, FIXTURE_B_PROBES, {"PROBE_THRU_CUTOUT"}, {"PROBE_CLEAN"}),
    ],
)
def test_edge_verdicts_match_kicad_cli(
    fixture, probes, kicad_violating, kct_clean, tmp_path
) -> None:
    """Parity on the pinned image release: same violating/clean verdicts.

    kicad-cli applies its board-setup default edge clearance (0.5 mm on the
    pinned 10.0.6 release); kct gets the same value explicitly.
    """
    cli_probes = _kicad_cli_edge_probes(fixture, tmp_path)
    assert cli_probes == kicad_violating
    kct_probes = _kct_edge_probes(fixture, probes, KICAD_DEFAULT_EDGE_CLEARANCE_MM)
    assert kct_probes == kicad_violating
    assert kct_clean.isdisjoint(kct_probes)


# ------------------------------------------------------- consumers see it too


def test_router_outline_reader_sees_footprint_outline() -> None:
    from kicad_tools.router.io import _extract_edge_segments, _read_outline_bounds

    text = FIXTURE_A.read_text()
    segments = _extract_edge_segments(text)
    assert len(segments) == 4
    assert segments.max_error_mm == 0.0
    assert _read_outline_bounds(text) == pytest.approx(A_BOUNDS, abs=1e-6)


def test_router_grid_keepin_respects_footprint_outline() -> None:
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.io import _extract_edge_segments
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.rules import DesignRules as RouterRules

    text = FIXTURE_A.read_text()
    segments = _extract_edge_segments(text)
    margin = 10.0
    width = A_BOUNDS[2] - A_BOUNDS[0] + 2 * margin
    height = A_BOUNDS[3] - A_BOUNDS[1] + 2 * margin
    grid = RoutingGrid(
        width, height, RouterRules(), origin_x=A_BOUNDS[0] - margin, origin_y=A_BOUNDS[1] - margin
    )
    blocked_cells = grid.add_edge_keepout(segments, 0.5)
    assert blocked_cells > 0
    layer = Layer.F_CU
    # Board centre: routable.
    gx, gy = grid.world_to_grid(*ANCHOR_A)
    assert not grid.is_blocked(gx, gy, layer)
    # 0.25 mm inside the bottom edge: inside the outline but inside the
    # keepout corridor.
    near = _to_board_a(0.0, -14.75)
    gx, gy = grid.world_to_grid(*near)
    assert grid.is_blocked(gx, gy, layer)
    # Just outside the rotated right edge: inside the keepout corridor of the
    # footprint-defined outline, so a route may not go there either.
    outside = _to_board_a(20.5, 0.0)
    gx, gy = grid.world_to_grid(*outside)
    assert grid.is_blocked(gx, gy, layer)


def test_placer_outline_is_board_relative_and_exact() -> None:
    from kicad_tools.placement.geometry import extract_board_outline

    pcb = PCB.load(FIXTURE_A)
    outline = extract_board_outline(pcb, warn=False)
    assert (outline.min_x, outline.min_y, outline.max_x, outline.max_y) == pytest.approx(
        (0.0, 0.0, A_BOUNDS[2] - A_BOUNDS[0], A_BOUNDS[3] - A_BOUNDS[1]), abs=1e-6
    )


def test_board_geometry_polygonizes_footprint_outline() -> None:
    pytest.importorskip("shapely")
    from kicad_tools.pcb.board_geometry import BoardGeometry

    a = BoardGeometry.from_pcb(PCB.load(FIXTURE_A))
    # The rotated rect keeps its true area (40 x 30 mm).
    assert a.polygon.area == pytest.approx(1200.0, abs=1e-6)
    b = BoardGeometry.from_pcb(PCB.load(FIXTURE_B))
    # Board area minus the footprint cut-out, as an interior ring.
    assert b.polygon.area == pytest.approx(80.0 * 60.0 - 20.0 * 10.0, abs=1e-6)
    assert len(b.polygon.interiors) == 1


def test_silkscreen_accepts_footprint_outline() -> None:
    pytest.importorskip("shapely")
    from shapely.geometry import Polygon

    from kicad_tools.silkscreen.place_refs import _board_material, _SilkscreenPCB

    material, poly, error = _board_material(_SilkscreenPCB.load(FIXTURE_A), Polygon)
    assert error == ""
    assert material
    material, poly, error = _board_material(_SilkscreenPCB.load(FIXTURE_B), Polygon)
    assert error == ""
    assert poly is not None and len(poly.interiors) == 1


def test_dsn_export_no_longer_omits_footprint_outline() -> None:
    from kicad_tools.export.dsn import KiCadToDSNExporter

    exporter = KiCadToDSNExporter(FIXTURE_A)
    exporter.export()
    assert len(exporter._board_outline) == 4
    exporter = KiCadToDSNExporter(FIXTURE_B)
    exporter.export()
    assert len(exporter._board_outline) == 4
    assert len(exporter._outline_cutouts) == 1


def test_boardview_renders_footprint_outline_polylines() -> None:
    from kicad_tools.boardview.model import _outline_polylines

    pcb = PCB.load(FIXTURE_A)
    polylines = _outline_polylines(pcb, (0.0, 0.0))
    assert polylines  # the footprint outline renders
    xs = [x for line in polylines for x, _ in line]
    assert min(xs) == pytest.approx(A_BOUNDS[0], abs=1e-3)
    assert max(xs) == pytest.approx(A_BOUNDS[2], abs=1e-3)
