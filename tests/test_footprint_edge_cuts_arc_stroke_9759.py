"""Stroke outline of arc-bearing ``fp_poly`` and rounded ``fp_rect`` (#9759).

The filled-region reader (``footprint_outline_closed_shapes``, #9692) reads
``(arc ...)`` pieces inside an ``fp_poly``'s ``(pts ...)`` and an
``fp_rect``'s ``(radius N)``; the stroke reader (``footprint_outline_graphics``
-> ``PCB.get_board_outline_segments`` / board geometry / boardview / router)
used to drop the arcs and emit square corners.  These tests pin that both
readers now describe the same boundary, for filled and ``(fill no)`` shapes,
under rotation and on a back-side footprint, and that the edge-clearance
rule's verdict near an arc or rounded corner follows the authored shape
(native parity for the same probes lives in
``tests/test_edge_cuts_fp_filled_native_parity.py``).
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from kicad_tools.core.board_outline import (
    board_outline_bounds,
    board_outline_segments,
    footprint_outline_closed_shapes,
    footprint_outline_graphics,
)
from kicad_tools.core.geometry import point_to_segment_distance
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.edge import EdgeClearanceRule

_STROKE = "(stroke (width 0.05) (type solid))"
_ARC_OUT = (
    "(xy -20 -15) (xy 20 -15) (xy 20 15) (arc (start 15 15) (mid 0 30) (end -15 15)) (xy -20 15)"
)
_ARC_IN = (
    "(xy -20 -15) (xy 20 -15) (xy 20 15) (arc (start 15 15) (mid 0 0) (end -15 15)) (xy -20 15)"
)


def _board(shape: str, at: str = "60 50 0", layer: str = "F.Cu", outer: bool = False) -> PCB:
    outer_rect = (
        f'(gr_rect (start 0 0) (end 100 100) {_STROKE} (fill no) (layer "Edge.Cuts"))'
        if outer
        else ""
    )
    return PCB(
        parse_string(
            f"""(kicad_pcb (version 20241229) (generator pcbnew)
                 (general (thickness 1.6))
                 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
                 (net 0 "") (net 1 "N1")
                 {outer_rect}
                 (footprint "CUT" (layer "{layer}") (at {at})
                   {shape})
               )"""
        )
    )


def _poly(pts: str, fill: str) -> str:
    return f'(fp_poly (pts {pts}) {_STROKE} (fill {fill}) (layer "Edge.Cuts"))'


def _rect(start: str, end: str, radius: float, fill: str) -> str:
    return (
        f"(fp_rect (start {start}) (end {end}) (radius {radius}) "
        f'{_STROKE} (fill {fill}) (layer "Edge.Cuts"))'
    )


def _min_distance(point, segments) -> float:
    return min(
        point_to_segment_distance(point[0], point[1], a[0], a[1], b[0], b[1]) for a, b in segments
    )


def _sheet_segments(pcb: PCB):
    """``get_board_outline_segments`` shifted back to sheet coordinates."""
    ox, oy = pcb.board_origin
    segments = pcb.get_board_outline_segments()
    return [((a[0] + ox, a[1] + oy), (b[0] + ox, b[1] + oy)) for a, b in segments], segments


# ------------------------------------------------------------ fp_poly arcs


@pytest.mark.parametrize("fill", ["yes", "no"])
def test_arc_fp_poly_emits_arc_and_closed_line_boundary(fill: str) -> None:
    pcb = _board(_poly(_ARC_OUT, fill))
    nodes = list(footprint_outline_graphics(pcb._sexp))
    # (-20,-15)->(20,-15)->(20,15)->(15,15) arc (-15,15)->(-20,15)->close.
    assert [n.tag for n in nodes] == ["gr_line"] * 3 + ["gr_arc"] + ["gr_line"] * 2
    arc = nodes[3]
    assert [arc.find_child(k).get_float(1) for k in ("start", "mid", "end")] == [65, 80, 65]
    # Closed: each element ends where the next starts.
    ends = []
    for n in nodes:
        start = (n.find_child("start").get_float(0), n.find_child("start").get_float(1))
        end = (n.find_child("end").get_float(0), n.find_child("end").get_float(1))
        ends.append((start, end))
    for (_, end), (start, _) in zip(ends, ends[1:] + ends[:1], strict=True):
        assert end == pytest.approx(start)


def test_arc_free_fp_poly_still_emits_one_gr_poly() -> None:
    pcb = _board(_poly("(xy -20 -15) (xy 20 -15) (xy 20 15) (xy -20 15)", "no"))
    assert [n.tag for n in footprint_outline_graphics(pcb._sexp)] == ["gr_poly"]


def test_arc_start_on_previous_vertex_adds_no_zero_length_edge() -> None:
    pts = "(xy -20 -15) (xy 20 -15) (arc (start 20 -15) (mid 30 0) (end 20 15)) (xy -20 15)"
    nodes = list(footprint_outline_graphics(_board(_poly(pts, "yes"))._sexp))
    assert [n.tag for n in nodes] == ["gr_line", "gr_arc", "gr_line", "gr_line"]


def test_leading_arc_piece_closes_back_to_its_start() -> None:
    pts = "(arc (start -20 15) (mid 0 -25) (end 20 15)) (xy 0 20)"
    nodes = list(footprint_outline_graphics(_board(_poly(pts, "no"))._sexp))
    assert [n.tag for n in nodes] == ["gr_arc", "gr_line", "gr_line"]
    last = nodes[-1].find_child("end")
    assert (last.get_float(0), last.get_float(1)) == pytest.approx((40.0, 65.0))


@pytest.mark.parametrize("pts", [_ARC_OUT, _ARC_IN], ids=["outward", "inward"])
def test_arc_fp_poly_segments_follow_the_authored_arc(pts: str) -> None:
    pcb = _board(_poly(pts, "no"), at="60 50 30")
    sheet, segments = _sheet_segments(pcb)
    assert segments.max_error_mm > 0.0  # the arc was tessellated, not dropped
    # The arc's authored midpoint (local (0, 30) or (0, 0)) is on the boundary.
    mid_local = (0.0, 30.0) if pts is _ARC_OUT else (0.0, 0.0)
    a = math.radians(30.0)
    mid = (
        60 + mid_local[0] * math.cos(a) + mid_local[1] * math.sin(a),
        50 - mid_local[0] * math.sin(a) + mid_local[1] * math.cos(a),
    )
    assert _min_distance(mid, sheet) <= 1e-6
    # The dropped-arc reading's straight edge along local y = 15 between the
    # arc ends is gone: its midpoint (local (0, 15)) is 15 mm from the arc.
    chord_mid = (60 + 15 * math.sin(a), 50 + 15 * math.cos(a))
    assert _min_distance(chord_mid, sheet) == pytest.approx(15.0, abs=1e-3)


def test_router_reader_and_bounds_see_the_arc_bulge() -> None:
    sexp = _board(_poly(_ARC_OUT, "yes"))._sexp
    segments = board_outline_segments(sexp)
    assert max(max(a[1], b[1]) for a, b in segments) == pytest.approx(80.0, abs=1e-6)
    assert segments.max_error_mm > 0.0
    bounds = board_outline_bounds(sexp)
    assert bounds is not None
    assert bounds[3] == pytest.approx(80.0, abs=1e-6)


# ------------------------------------------------------- rounded fp_rect


@pytest.mark.parametrize("fill", ["yes", "no"])
def test_rounded_fp_rect_emits_corner_arcs(fill: str) -> None:
    nodes = list(footprint_outline_graphics(_board(_rect("-20 -20", "20 20", 12, fill))._sexp))
    assert [n.tag for n in nodes] == ["gr_arc", "gr_line"] * 4
    # Top-left corner arc: centre (52, 42), r 12, mid on the diagonal.
    arc = nodes[0]
    mid = (arc.find_child("mid").get_float(0), arc.find_child("mid").get_float(1))
    assert math.dist(mid, (52.0, 42.0)) == pytest.approx(12.0)
    assert mid[0] == pytest.approx(52.0 - 12.0 / math.sqrt(2.0))


def test_square_fp_rect_is_unchanged() -> None:
    for shape in (
        '(fp_rect (start -20 -20) (end 20 20) (layer "Edge.Cuts"))',
        _rect("-20 -20", "20 20", 0, "no"),
    ):
        nodes = list(footprint_outline_graphics(_board(shape)._sexp))
        assert [n.tag for n in nodes] == ["gr_line"] * 4


def test_rounded_fp_rect_radius_clamps_to_half_the_shorter_side() -> None:
    # 40 x 20 rect, radius 50 -> clamped to 10: the short sides vanish.
    nodes = list(footprint_outline_graphics(_board(_rect("-20 -10", "20 10", 50, "no"))._sexp))
    assert [n.tag for n in nodes] == ["gr_arc", "gr_line", "gr_arc", "gr_arc", "gr_line", "gr_arc"]
    # A square with radius >= half its side becomes four arcs: a circle.
    nodes = list(footprint_outline_graphics(_board(_rect("-10 -10", "10 10", 10, "no"))._sexp))
    assert [n.tag for n in nodes] == ["gr_arc"] * 4
    sheet, _ = _sheet_segments(_board(_rect("-10 -10", "10 10", 10, "no")))
    for a, _b in sheet:
        assert math.dist(a, (60.0, 50.0)) == pytest.approx(10.0, abs=1e-4)


def test_rounded_fp_rect_board_geometry_area_and_boardview() -> None:
    from kicad_tools.boardview.model import _outline_polylines
    from kicad_tools.pcb.board_geometry import BoardGeometry

    pcb = _board(_rect("-20 -20", "20 20", 12, "no"))
    geometry = BoardGeometry.from_pcb(pcb)
    expected = 40.0 * 40.0 - (4.0 - math.pi) * 12.0**2
    assert geometry.polygon.area == pytest.approx(expected, rel=1e-3)
    polylines = _outline_polylines(pcb, (0.0, 0.0))
    # No polyline passes through the square corner the rect does not have.
    points = [p for line in polylines for p in line]
    ox, oy = pcb.board_origin
    corner = (40.0 - ox, 30.0 - oy)
    assert min(math.dist(p, corner) for p in points) > 4.0


# --------------------------------- stroke and filled readers agree (#9759)


@pytest.mark.parametrize(
    "shape",
    [
        _poly(_ARC_OUT, "yes"),
        _poly(_ARC_OUT, "no"),
        _poly(_ARC_IN, "yes"),
        _poly(_ARC_IN, "no"),
        _rect("-20 -15", "20 15", 6, "yes"),
        _rect("-20 -15", "20 15", 6, "no"),
    ],
    ids=["arc_out_filled", "arc_out_no", "arc_in_filled", "arc_in_no", "rrect_filled", "rrect_no"],
)
@pytest.mark.parametrize(
    "at,layer", [("60 50 30", "F.Cu"), ("60 50 -75", "B.Cu")], ids=["front_rot30", "back_rot-75"]
)
def test_stroke_boundary_matches_filled_ring(shape: str, at: str, layer: str) -> None:
    pcb = _board(shape, at=at, layer=layer)
    sheet, _ = _sheet_segments(pcb)
    (closed,) = list(footprint_outline_closed_shapes(pcb._sexp))
    ring = closed.ring
    # Every filled-ring vertex lies on the stroke boundary ...
    assert max(_min_distance(p, sheet) for p in ring) <= 1e-3
    # ... and every stroke vertex on the filled ring's boundary.
    ring_edges = list(zip(ring, ring[1:] + ring[:1], strict=True))
    assert max(_min_distance(a, ring_edges) for a, _b in sheet) <= 1e-3


# ----------------------------------------- edge-clearance verdicts (kct side)


def _flagged(pcb: PCB, probes: dict) -> set[str]:
    results = EdgeClearanceRule().check(
        pcb, SimpleNamespace(min_copper_to_edge_mm=0.3, min_hole_to_edge_mm=0.5)
    )
    flagged: set[str] = set()
    for v in results.errors:
        if v.rule_id != "edge_clearance_trace":
            continue
        x, y = v.location
        flagged |= {
            name
            for name, (a, b) in probes.items()
            if point_to_segment_distance(x, y, a[0], a[1], b[0], b[1]) <= 1.0
        }
    return flagged


def test_edge_clearance_near_rounded_corner_follows_the_radius() -> None:
    probes = {
        "near_round": ((43.0728, 33.7799), (43.7799, 33.0728)),
        "square_corner": ((40.25, 30.6), (40.6, 30.25)),
    }
    pcb = _top_level_tracks(_rect("-20 -20", "20 20", 12, "no"), probes)
    assert _flagged(pcb, probes) == {"near_round"}


def test_edge_clearance_near_arc_bulge_follows_the_arc() -> None:
    probes = {
        "near_bulge": ((59.0, 80.125), (61.0, 80.125)),
        # 0.3 mm below the dropped-arc reading's straight edge at y = 65.
        "old_chord": ((59.0, 65.3), (61.0, 65.3)),
    }
    pcb = _top_level_tracks(_poly(_ARC_OUT, "no"), probes)
    assert _flagged(pcb, probes) == {"near_bulge"}


def _top_level_tracks(shape: str, probes: dict) -> PCB:
    copper = "\n".join(
        f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.25) (layer "F.Cu") (net 1))'
        for a, b in probes.values()
    )
    return PCB(
        parse_string(
            f"""(kicad_pcb (version 20241229) (generator pcbnew)
                 (general (thickness 1.6))
                 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
                 (net 0 "") (net 1 "N1")
                 (gr_rect (start 0 0) (end 100 100) {_STROKE} (fill no) (layer "Edge.Cuts"))
                 (footprint "CUT" (layer "F.Cu") (at 60 50 0)
                   {shape})
                 {copper}
               )"""
        )
    )
