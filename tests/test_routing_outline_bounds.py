"""One coordinate contract for routing, planning and schema (#4978)."""

import math

import pytest

from kicad_tools.core.board_outline import board_outline_bounds
from kicad_tools.router import DesignRules
from kicad_tools.router.io import (
    _extract_edge_segments,
    extract_board_dimensions,
    extract_board_origin,
    load_pcb_for_routing,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string


def _board(outline, origin=(68.5, 55)):
    x, y = origin
    return f"""(kicad_pcb (version 20241229) (generator "test")
    (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "SIGNAL")
    (gr_rect (start 1 2) (end 900 900) (layer "F.SilkS"))
    {outline}
    (footprint "R" (layer "F.Cu") (at {x + 10} {y + 10})
      (property "Reference" "R1" (at 0 -2) (layer "F.SilkS"))
      (fp_rect (start -900 -900) (end 900 900) (layer "F.SilkS"))
      (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))
      (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))))"""


@pytest.mark.parametrize("origin", [(68.5, 55), (-68.5, -55)])
@pytest.mark.parametrize("kind", ["lines", "rect", "reversed"])
def test_same_outline_across_consumers(tmp_path, origin, kind):
    x, y = origin
    corners = [(x, y), (x + 160, y), (x + 160, y + 100), (x, y + 100)]
    if kind == "lines":
        outline = "\n".join(
            f'(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))'
            for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
        )
    else:
        a, b = (corners[2], corners[0]) if kind == "reversed" else (corners[0], corners[2])
        outline = f'(gr_rect (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))'
    text = _board(outline, origin)
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    assert extract_board_dimensions("  \n" + text) == (160, 100)
    assert extract_board_origin(path) == origin
    edges = _extract_edge_segments(text)
    assert len(edges) == 4
    assert all(px in (x, x + 160) and py in (y, y + 100) for edge in edges for px, py in edge)
    pcb = PCB.load(path)
    assert pcb.board_origin == origin
    assert pcb.board_size == (160, 100)
    assert pcb.footprints[0].position == (10, 10)
    assert pcb.get_pad_position("R1", "1") == (9, 10)
    saved = tmp_path / "roundtrip.kicad_pcb"
    pcb.save(saved)
    reread = PCB.load(saved)
    assert reread.board_origin == origin
    assert reread.get_pad_position("R1", "1") == (9, 10)
    router, _ = load_pcb_for_routing(
        path, rules=DesignRules(grid_resolution=0.5, trace_clearance=0.5), validate_drc=False
    )
    assert (router.grid.width, router.grid.height) == (160, 100)
    # Router pads retain sheet-absolute coordinates; the grid carries the origin.
    pads = list(router.pads.values())
    assert sorted((p.x, p.y) for p in pads) == [(x + 9, y + 10), (x + 11, y + 10)]
    assert (router.grid.origin_x, router.grid.origin_y) == origin


@pytest.mark.parametrize("outline", ["", '(gr_rect (start 0 0) (end 10 10) (layer "F.SilkS"))'])
def test_missing_outline_is_explicit(tmp_path, outline):
    text = _board(outline)
    assert extract_board_dimensions(text) is None
    assert extract_board_origin(text) is None
    path = tmp_path / "missing.kicad_pcb"
    path.write_text(text)
    with pytest.raises(ValueError, match="missing supported Edge.Cuts"):
        load_pcb_for_routing(path, validate_drc=False)


@pytest.mark.parametrize(
    "outline",
    [
        '(gr_rect (start 0 0) (layer "Edge.Cuts"))',
        '(gr_text "unsupported" (at 1 1) (layer "Edge.Cuts"))',
        '(gr_arc (start 0 0) (mid 1 1) (end 2 2) (layer "Edge.Cuts"))',
    ],
)
def test_malformed_or_unsupported_outline_is_explicit(outline):
    with pytest.raises(ValueError, match="Edge.Cuts"):
        extract_board_dimensions(_board(outline))


def test_circle_uses_radius_not_center_end_bbox():
    root = parse_string('(kicad_pcb (gr_circle (center -4 8) (end -1 12) (layer "Edge.Cuts")))')
    assert board_outline_bounds(root) == (-9, 3, 1, 13)


@pytest.mark.parametrize("reverse", [False, True])
def test_arc_includes_extrema_between_control_points(reverse):
    points = [(10 * math.cos(a), 10 * math.sin(a)) for a in (math.pi / 4, math.pi, 7 * math.pi / 4)]
    if reverse:
        points.reverse()
    coords = " ".join(
        f"({tag} {p[0]} {p[1]})" for tag, p in zip(("start", "mid", "end"), points, strict=True)
    )
    root = parse_string(f'(kicad_pcb (gr_arc {coords} (layer "Edge.Cuts")))')
    assert board_outline_bounds(root) == pytest.approx((-10, -10, math.sqrt(50), 10))


def test_cubic_bounds_use_curve_extrema_not_control_polygon():
    root = parse_string(
        '(kicad_pcb (gr_curve (pts (xy 0 0) (xy 0 10) (xy 10 10) (xy 10 0)) (layer "Edge.Cuts")))'
    )
    assert board_outline_bounds(root) == (0, 0, 10, 7.5)


@pytest.mark.parametrize(
    "outline",
    [
        '(gr_circle (center 10 10) (end 15 10) (layer "Edge.Cuts"))',
        '(gr_arc (start 0 0) (mid 5 5) (end 10 0) (layer "Edge.Cuts"))',
        '(gr_arc (start 0 0) (end 10 0) (angle -90) (layer "Edge.Cuts"))',
        '(gr_curve (pts (xy 0 0) (xy 0 10) (xy 10 10) (xy 10 0)) (layer "Edge.Cuts"))',
    ],
)
def test_routing_rejects_unsupported_curved_edge_obstacles(tmp_path, outline):
    path = tmp_path / "curved.kicad_pcb"
    path.write_text(_board(outline))
    with pytest.raises(ValueError, match="Unsupported routing Edge.Cuts"):
        load_pcb_for_routing(path, validate_drc=False)


@pytest.mark.parametrize("sweep, expected", [(270, (-10, -10, 10, 10)), (-90, (0, -10, 10, 0))])
def test_legacy_arc_bounds_preserve_signed_sweep(sweep, expected):
    root = parse_string(
        f'(kicad_pcb (gr_arc (start 0 0) (end 10 0) (angle {sweep}) (layer "Edge.Cuts")))'
    )
    assert board_outline_bounds(root) == pytest.approx(expected)


@pytest.mark.parametrize("angle", ["", "(angle nan)", "(angle 90 180)", "(angle 0)"])
def test_invalid_legacy_arc_is_rejected(angle):
    root = parse_string(f'(kicad_pcb (gr_arc (start 0 0) (end 10 0) {angle} (layer "Edge.Cuts")))')
    with pytest.raises(ValueError, match="Malformed Edge.Cuts"):
        board_outline_bounds(root)
