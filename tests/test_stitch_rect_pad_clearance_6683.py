"""Issue #6683: stitch via-vs-pad checks must be shape-aware.

``find_all_pads`` models every pad as a circle of radius ``max(w, h) / 2``.
For a square pad that is the INSCRIBED circle, so a via near the pad's
corner was under-checked by up to ``(sqrt(2) - 1) x half-side``.  On board
07 that let the U4.E3/E5 stitch vias land 0.19 mm from the square U4.F3/F5
BGA pads (DRC ``clearance``).

These tests pin:

* the corner geometry from the issue (0.45 mm square pad, via r=0.225,
  clearance 0.2 at (0.5206, 0.5206): real gap 0.193 mm, circle-model gap
  0.286 mm);
* that the straight, dog-leg and extended-escape placers all reject a
  corner placement once given the shape-aware pad pool (and that the
  legacy circle pool alone does not -- so the tests discriminate);
* the round-pad negative control: circle pads stay exact, so round-pad BGA
  escapes are not newly over-constrained;
* leg checks (dog-leg / multi-leg) and the skip diagnostic.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    _check_dogleg_path_clearance,
    _check_multileg_path_clearance,
    _pad_conflict,
    _pad_copper_gap,
    calculate_dogleg_via_position,
    calculate_extended_escape_position,
    calculate_via_position,
    find_all_pad_bboxes,
    identify_nearest_obstacle,
)
from kicad_tools.sexp import parse_string

VIA_SIZE = 0.45
VIA_R = VIA_SIZE / 2
CLEARANCE = 0.2
PAD = 0.45  # square BGA land, mm


def _box(x: float, y: float, shape: str, size: float = PAD, net: int = 2):
    h = size / 2
    return (x - h, y - h, x + h, y + h, net, shape)


def _circle_of(bbox) -> tuple[float, float, float, int]:
    """The legacy ``find_all_pads`` record for the same pad."""
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2, max(w, h) / 2, bbox[4])


def _grid(pitch: float, shape: str):
    """3x3 BGA field: the stitched pad at the origin, 8 other-net neighbours."""
    bboxes = [
        _box(i * pitch, j * pitch, shape)
        for i in (-1, 0, 1)
        for j in (-1, 0, 1)
        if (i, j) != (0, 0)
    ]
    return bboxes, [_circle_of(b) for b in bboxes]


def _min_via_gap(pos, bboxes) -> float:
    """True copper-edge gap from a via at ``pos`` to the nearest pad."""
    return min(_pad_copper_gap(pos[0], pos[1], pos[0], pos[1], b) for b in bboxes) - VIA_R


STITCHED = PadInfo("U4", "E3", 1, "GND", 0.0, 0.0, "F.Cu", PAD, PAD)


# ---------------------------------------------------------------------------
# Geometry from the issue
# ---------------------------------------------------------------------------


def test_square_pad_corner_gap_matches_issue_numbers() -> None:
    sq = _box(0.0, 0.0, "rect")
    gap = _pad_copper_gap(0.5206, 0.5206, 0.5206, 0.5206, sq) - VIA_R
    assert gap == pytest.approx(0.193, abs=1e-3)

    # The old inscribed-circle model reports a passing gap at the same spot.
    cx, cy, r, _ = _circle_of(sq)
    circle_gap = math.hypot(0.5206 - cx, 0.5206 - cy) - r - VIA_R
    assert circle_gap == pytest.approx(0.286, abs=1e-3)

    required = VIA_R + CLEARANCE
    assert _pad_conflict(0.5206, 0.5206, 0.5206, 0.5206, required, [], [sq])
    # Legacy circle fallback (no bboxes) is the under-check being fixed.
    assert not _pad_conflict(0.5206, 0.5206, 0.5206, 0.5206, required, [_circle_of(sq)], None)


def test_round_pad_same_geometry_is_accepted() -> None:
    """Negative control: a circle pad is checked exactly, not as its box."""
    rnd = _box(0.0, 0.0, "circle")
    gap = _pad_copper_gap(0.5206, 0.5206, 0.5206, 0.5206, rnd) - VIA_R
    assert gap == pytest.approx(0.286, abs=1e-3)
    assert not _pad_conflict(0.5206, 0.5206, 0.5206, 0.5206, VIA_R + CLEARANCE, [], [rnd])


def test_oval_gap_is_never_below_either_bound() -> None:
    oval = _box(0.0, 0.0, "oval", size=1.0)
    # On the diagonal the stadium (here a circle) is farther than the box.
    gap = _pad_copper_gap(1.0, 1.0, 1.0, 1.0, oval)
    assert gap == pytest.approx(math.hypot(1.0, 1.0) - 0.5)


def test_legacy_five_tuple_is_treated_as_rect() -> None:
    assert _pad_copper_gap(1.0, 0.0, 1.0, 0.0, (-0.5, -0.5, 0.5, 0.5, 2)) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Every placement strategy, 1.0 mm-pitch BGA of 0.45 mm square lands
# ---------------------------------------------------------------------------

PLACERS = {
    "straight": lambda pads, bbs: calculate_via_position(
        STITCHED, 0.5, VIA_SIZE, [], CLEARANCE, other_net_pads=pads, other_net_pad_bboxes=bbs
    ),
    "dogleg": lambda pads, bbs: calculate_dogleg_via_position(
        STITCHED, 0.5, VIA_SIZE, [], CLEARANCE, other_net_pads=pads, other_net_pad_bboxes=bbs
    ),
    "extended": lambda pads, bbs: calculate_extended_escape_position(
        STITCHED, 0.5, VIA_SIZE, [], CLEARANCE, other_net_pads=pads, other_net_pad_bboxes=bbs
    ),
}


@pytest.mark.parametrize("strategy", sorted(PLACERS))
def test_square_pad_field_rejects_corner_vias(strategy: str) -> None:
    bboxes, circles = _grid(1.0, "rect")
    place = PLACERS[strategy]

    # Discriminating: the circle model alone places a via inside clearance
    # of a square pad's corner.
    legacy = place(circles, None)
    assert legacy is not None
    assert _min_via_gap(legacy, bboxes) < CLEARANCE

    # Shape-aware: whatever is returned clears every square land.
    fixed = place(circles, bboxes)
    if fixed is not None:
        assert _min_via_gap(fixed, bboxes) >= CLEARANCE - 1e-6


@pytest.mark.parametrize("strategy", sorted(PLACERS))
def test_round_pad_field_placement_unchanged(strategy: str) -> None:
    """Round-pad BGA escapes are not newly rejected (circle stays exact)."""
    bboxes, circles = _grid(1.0, "circle")
    place = PLACERS[strategy]
    legacy = place(circles, None)
    fixed = place(circles, bboxes)
    assert legacy is not None
    assert fixed == legacy


def test_straight_diagonal_via_kept_for_round_pads() -> None:
    bboxes, circles = _grid(1.0, "circle")
    pos = calculate_via_position(
        STITCHED, 0.5, VIA_SIZE, [], CLEARANCE, other_net_pads=circles, other_net_pad_bboxes=bboxes
    )
    assert pos is not None
    assert pos[0] == pytest.approx(pos[1])  # the diagonal interstitial site


# ---------------------------------------------------------------------------
# Leg checks
# ---------------------------------------------------------------------------

TRACE_HALF = 0.1
# Diagonal leg x + y = c whose edge passes 0.19 mm from the (0.225, 0.225)
# corner of a 0.45 mm square at the origin.
_C = 2 * (PAD / 2) + (0.19 + TRACE_HALF) * math.sqrt(2)
LEG = ((_C + 1.0, -1.0), (-1.0, _C + 1.0))


def test_leg_edge_distance_is_0_19_mm() -> None:
    sq = _box(0.0, 0.0, "rect")
    (sx, sy), (ex, ey) = LEG
    assert _pad_copper_gap(sx, sy, ex, ey, sq) - TRACE_HALF == pytest.approx(0.19)


@pytest.mark.parametrize("shape,blocked", [("rect", True), ("circle", False)])
def test_multileg_path_rejects_leg_near_rect_corner(shape: str, blocked: bool) -> None:
    pad = _box(0.0, 0.0, shape)
    (sx, sy), (ex, ey) = LEG
    clear = _check_multileg_path_clearance(
        [(sx, sy), (ex, ey)],
        TRACE_HALF,
        [],
        [],
        [_circle_of(pad)],
        CLEARANCE,
        other_net_pad_bboxes=[pad],
    )
    assert clear is (not blocked)
    # Circle model alone accepts the leg either way (the old under-check).
    assert _check_multileg_path_clearance(
        [(sx, sy), (ex, ey)], TRACE_HALF, [], [], [_circle_of(pad)], CLEARANCE
    )


def test_dogleg_path_rejects_leg_near_rect_corner() -> None:
    sq = _box(0.0, 0.0, "rect")
    (sx, sy), (ex, ey) = LEG
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    args = (sx, sy, mx, my, ex, ey, TRACE_HALF, [], [], [_circle_of(sq)], CLEARANCE)
    assert _check_dogleg_path_clearance(*args)
    assert not _check_dogleg_path_clearance(*args, other_net_pad_bboxes=[sq])


# ---------------------------------------------------------------------------
# Skip diagnostic
# ---------------------------------------------------------------------------


def test_identify_nearest_obstacle_reports_rect_aware_gap() -> None:
    sq = _box(0.0, 0.0, "rect")
    stitched = PadInfo("U4", "E3", 1, "GND", 0.5206, 0.5206, "F.Cu", 0.1, 0.1)
    legacy = identify_nearest_obstacle(
        stitched, VIA_SIZE, CLEARANCE, [], other_net_pads=[_circle_of(sq)]
    )
    fixed = identify_nearest_obstacle(
        stitched,
        VIA_SIZE,
        CLEARANCE,
        [],
        other_net_pads=[_circle_of(sq)],
        other_net_pad_bboxes=[sq],
    )
    assert "gap=0.29mm" in legacy.reason
    assert "gap=0.19mm" in fixed.reason
    assert fixed.obstacle_type == "pad"
    assert (fixed.obstacle_x, fixed.obstacle_y) == pytest.approx((0.0, 0.0))


# ---------------------------------------------------------------------------
# Pad model
# ---------------------------------------------------------------------------

_PCB = """(kicad_pcb
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "BGA" (layer "F.Cu") (at 10 10 45)
    (pad "A1" smd rect (at 0 0) (size 0.45 0.45) (layers "F.Cu") (net 1 "GND"))
    (pad "A2" smd circle (at 1 0) (size 0.45 0.45) (layers "F.Cu") (net 2 "SIG"))
  )
  (footprint "QFP" (layer "F.Cu") (at 20 20)
    (pad "1" smd roundrect (at 0 0 90) (size 1.5 0.3) (layers "F.Cu") (net 2 "SIG"))
  )
)"""


def test_find_all_pad_bboxes_appends_shape() -> None:
    bboxes = find_all_pad_bboxes(parse_string(_PCB))
    assert len(bboxes) == 3
    by_shape = {b[5]: b for b in bboxes}
    # ``[4]`` is still the net (``_sibling_pools`` indexes it).
    assert by_shape["rect"][4] == 1
    assert by_shape["circle"][4] == 2
    # A circle's box is its exact bounding square even at 45 degrees.
    c = by_shape["circle"]
    assert c[2] - c[0] == pytest.approx(0.45)
    assert c[3] - c[1] == pytest.approx(0.45)
    # A rect at 45 degrees keeps the conservative rotated AABB.
    r = by_shape["rect"]
    assert r[2] - r[0] == pytest.approx(0.45 * math.sqrt(2))
    # A pad-local 90 degree angle widens the box to cover both orientations,
    # so the long axis can never be modelled the wrong way round.
    q = by_shape["roundrect"]
    assert q[2] - q[0] == pytest.approx(1.5)
    assert q[3] - q[1] == pytest.approx(1.5)
