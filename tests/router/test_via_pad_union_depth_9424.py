"""Via-vs-own-pad union depth (Issue #9424).

The same-net pad slit band (#8957 / #8994 / #9124) accepted a via annulus as
one copper union with its own SMD pad whenever it overlapped the pad's
axis-aligned bounding box.  Board 05's unattended route carried four
``physical_copper_gap`` errors from exactly that: three vias overlapping
only the square corner of a ``roundrect`` pad's box (air beside the rounded
copper) and one 5 um overlap on a flat face whose lens leaves a wedge the
checker grades.  The union now needs :func:`same_net_pad_union_depth_mm` of
overlap into the box's inscribed stadium.

Pinned here:

* the four board-05 seats: accepted by the old box model, graded by the
  checker, rejected now;
* an oracle sweep -- every seat the predicate accepts is clean under the
  fab-view checker itself, for several pad shapes, annuli and edges;
* the region form (``same_net_pad_gap_band_ring``) agrees with the point form;
* the C++ ``same_net_pad_gap_in_band`` mirror agrees with Python.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.cli.stitch_cmd import find_all_pad_bboxes
from kicad_tools.router.via_clearance import (
    same_net_pad_gap_acceptable,
    same_net_pad_gap_band_ring,
    same_net_pad_union_depth_mm,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap

EDGE = 0.15  # jlcpcb pad_spacing error edge on board 05's stackup
VIA = 0.6
R = VIA / 2


def _board_text(pw, ph, shape, rratio, vx, vy, via=VIA):
    extra = f"(roundrect_rratio {rratio})" if shape == "roundrect" else ""
    return f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "SIG")
      (footprint "t" (layer "F.Cu") (at 10 10)
        (pad "1" smd {shape} (at 0 0) (size {pw} {ph}) {extra}
          (layers "F.Cu") (net 1 "SIG") (uuid "pad1")))
      (via (at {vx:.4f} {vy:.4f}) (size {via}) (drill {via / 2})
        (layers "F.Cu" "B.Cu") (net 1) (uuid "via1"))
    )"""


def _bboxes(pw, ph, shape, rratio):
    sexp = parse_string(_board_text(pw, ph, shape, rratio, 0.0, 0.0))
    return [b[:4] for b in find_all_pad_bboxes(sexp) if b[4] == 1]


def _graded(pw, ph, shape, rratio, vx, vy, edge=EDGE, via=VIA):
    pcb = PCB(parse_string(_board_text(pw, ph, shape, rratio, vx, vy, via)))
    return [
        v
        for v in check_physical_copper_gap(pcb, edge).violations
        if v.rule_id == "physical_copper_gap"
    ]


def _old_box_model_accepts(x, y, half, bboxes, edge):
    """The pre-#9424 point predicate: any box overlap past -1e-9 is a union."""
    for b in bboxes:
        dx = max(0.0, b[0] - x, x - b[2])
        dy = max(0.0, b[1] - y, y - b[3])
        gap = math.hypot(dx, dy) - half
        if -1e-9 < gap < edge - 1e-9:
            return False
    return True


def test_union_depth_formula():
    # 0.6 mm annulus at 0.15 mm: c = 0.3 sin60 - 0.15 = 0.1098, d = 0.0208.
    assert same_net_pad_union_depth_mm(0.3, 0.15) == pytest.approx(0.0208 + 1e-3, abs=2e-4)
    # When the facing reach is inside the edge, only the slack remains: a
    # tangent seat is still never a union (#9124).
    assert same_net_pad_union_depth_mm(0.25, 0.25) == pytest.approx(1e-3)
    assert same_net_pad_union_depth_mm(0.25, 0.25) > 0


# Board 05 unattended route, seed 42, origin/main e56ea23dd: the via seat
# relative to its pad centre, the pad, and the checker's measured gap.
BOARD05_SEATS = [
    # (label, pad w, pad h, shape, rratio, dx, dy)
    ("U1.24 HALL_B corner", 1.475, 0.55, "roundrect", 0.25, 0.9525, -0.445),
    ("R5.2 RESET shallow face", 1.025, 1.4, "roundrect", 0.25, -0.0225, 0.995),
    ("R6.2 nFAULT corner", 1.025, 1.4, "roundrect", 0.25, -0.5625, -0.985),
    ("D1.1 VM corner", 2.5, 1.8, "roundrect", 0.25, 1.54, -0.755),
]


@pytest.mark.parametrize(("label", "pw", "ph", "shape", "rr", "dx", "dy"), BOARD05_SEATS)
def test_board05_seats_rejected(label, pw, ph, shape, rr, dx, dy):
    bboxes = _bboxes(pw, ph, shape, rr)
    x, y = 10.0 + dx, 10.0 + dy
    assert _old_box_model_accepts(x, y, R, bboxes, EDGE), f"{label}: old model accepted it"
    assert _graded(pw, ph, shape, rr, x, y), f"{label}: the checker grades the seat"
    assert not same_net_pad_gap_acceptable(x, y, VIA, bboxes, EDGE), label


PAD_SHAPES = [
    (1.025, 1.4, "roundrect", 0.25),
    (1.475, 0.55, "roundrect", 0.25),
    (1.2, 0.8, "rect", 0.0),
    (1.0, 1.0, "circle", 0.0),
    (1.5, 0.6, "oval", 0.0),
]


@pytest.mark.parametrize(("via", "edge"), [(0.6, 0.15), (0.5, 0.1016), (0.8, 0.15)])
@pytest.mark.parametrize(("pw", "ph", "shape", "rr"), PAD_SHAPES)
def test_accepted_seats_are_clean_under_the_checker(pw, ph, shape, rr, via, edge):
    """Oracle sweep: the predicate never accepts a seat the checker grades."""
    half = via / 2
    bboxes = _bboxes(pw, ph, shape, rr)
    accepted = 0
    for angle_deg in (0, 20, 35, 45, 55, 70, 90):
        a = math.radians(angle_deg)
        ux, uy = math.cos(a), math.sin(a)
        to_box = min(pw / 2 / ux if ux > 1e-9 else 1e9, ph / 2 / uy if uy > 1e-9 else 1e9)
        for k in range(30):
            d = to_box + half - 0.1 + k * 0.01
            x, y = 10.0 + d * ux, 10.0 + d * uy
            if not same_net_pad_gap_acceptable(x, y, via, bboxes, edge):
                continue
            accepted += 1
            found = _graded(pw, ph, shape, rr, x, y, edge, via)
            assert not found, (shape, pw, ph, angle_deg, round(d, 4), found[0].actual_value)
    assert accepted > 20


@pytest.mark.parametrize(("pw", "ph", "shape", "rr"), PAD_SHAPES)
def test_ring_matches_point_predicate(pw, ph, shape, rr):
    from shapely.geometry import Point

    bboxes = _bboxes(pw, ph, shape, rr)
    ring = same_net_pad_gap_band_ring(bboxes[0], VIA, EDGE)
    for ix in range(-40, 41, 3):
        for iy in range(-40, 41, 3):
            p = Point(10.0 + ix * 0.033, 10.0 + iy * 0.033)
            if ring.boundary.distance(p) < 1e-4:
                continue
            ok = same_net_pad_gap_acceptable(p.x, p.y, VIA, bboxes, EDGE)
            assert ok is (not ring.contains(p)), (p.x, p.y)


# ---------------------------------------------------------------------------
# C++ mirror parity
# ---------------------------------------------------------------------------

RES = 0.05
COLS = ROWS = 400
NET = 7


def _cpp_pathfinder(edge_mm, pads):
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(COLS, ROWS, 2, RES, 0.0, 0.0)
    for x, y, width, height, rotation, is_circular in pads:
        grid.add_pad(x, y, width, height, NET, 0, 0, 0.0, False, rotation, is_circular)
    rules = router_cpp.DesignRules()
    rules.trace_width = 0.2
    rules.trace_clearance = 0.15
    rules.via_clearance = 0.15
    rules.via_drill = 0.3
    rules.via_diameter = VIA
    rules.allow_smd_vias = True
    rules.same_net_pad_gap_band_edge_mm = edge_mm
    return router_cpp.Pathfinder(grid, rules, False)


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize(
    ("pw", "ph", "shape", "circular"),
    [
        (1.025, 1.4, "roundrect", False),
        (1.475, 0.55, "roundrect", False),
        (1.0, 1.0, "circle", True),
    ],
)
def test_cpp_mirror_matches_python(pw, ph, shape, circular):
    cx = cy = 10.0
    pads = [(cx, cy, pw, ph, 0.0, circular)]
    bboxes = _bboxes(pw, ph, shape, 0.25)
    pf_on = _cpp_pathfinder(EDGE, pads)
    pf_off = _cpp_pathfinder(0.0, pads)
    depth = same_net_pad_union_depth_mm(R, EDGE)
    compared = flipped_vs_old = 0
    for gx in range(int((cx - 1.6) / RES), int((cx + 1.6) / RES) + 1):
        for gy in range(int((cy - 1.6) / RES), int((cy + 1.6) / RES) + 1):
            x, y = gx * RES, gy * RES
            if pf_off.is_via_blocked(gx, gy, NET, True):
                continue
            b = bboxes[0]
            gap = math.hypot(max(0.0, b[0] - x, x - b[2]), max(0.0, b[1] - y, y - b[3])) - R
            corner = min(b[2] - b[0], b[3] - b[1]) / 2
            st = (
                math.hypot(
                    max(0.0, b[0] + corner - x, x - (b[2] - corner)),
                    max(0.0, b[1] + corner - y, y - (b[3] - corner)),
                )
                - corner
                - R
            )
            # float32 pad edges on the C++ side: skip seats within 1e-4 of a band edge.
            if abs(gap - EDGE) < 1e-4 or abs(st + depth) < 1e-4:
                continue
            py_ok = same_net_pad_gap_acceptable(x, y, VIA, bboxes, EDGE)
            assert pf_on.is_via_blocked(gx, gy, NET, True) is (not py_ok), (x, y, gap, st)
            compared += 1
            if py_ok != _old_box_model_accepts(x, y, R, bboxes, EDGE):
                flipped_vs_old += 1
    assert compared > 500
    if shape == "roundrect":
        # The sweep must exercise the new union rule, not only the old band.
        assert flipped_vs_old > 0
