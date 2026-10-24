"""Trailing/leading collinear reversal trim before pair offsetting (#6958)."""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice.coupled import (
    merge_collinear_points,
    offset_polyline,
    trim_end_reversals,
)

P = 0.2


def test_trailing_overshoot_offsets_to_straight_legs():
    center = trim_end_reversals([(0, 0), (0, 2.6), (0, 2.2)])
    assert center == [(0, 0), (0, 2.2)]
    for sign in (+1, -1):
        leg = offset_polyline(center, sign * P)
        assert leg is not None
        assert len(leg) == 2
        assert leg[0][0] == pytest.approx(-sign * P)
        assert leg[-1] == pytest.approx((-sign * P, 2.2))


def test_untrimmed_reversal_is_degenerate():
    assert offset_polyline([(0, 0), (0, 2.6), (0, 2.2)], P) is None


def test_leading_overshoot_trimmed():
    assert trim_end_reversals([(0, 2.2), (0, 2.6), (0, 0)]) == [(0, 2.2), (0, 0)]


def test_out_and_back_spur_removed():
    pts = [(0, 0), (1, 0), (1.2, 0.2), (1, 0)]
    assert trim_end_reversals(pts) == [(0, 0), (1, 0)]


def test_reversal_only_leaves_two_points():
    assert trim_end_reversals([(0, 0), (0, 1), (0, 0.5)]) == [(0, 0), (0, 0.5)]


def test_mid_path_reversal_still_declines():
    center = trim_end_reversals([(0, 0), (0, 2), (0, 1), (1, 1), (2, 2)])
    assert center == [(0, 0), (0, 2), (0, 1), (1, 1), (2, 2)]
    assert offset_polyline(center, P) is None


def test_overshoot_past_start_trims_from_the_other_end():
    # a->b is shorter than b->p, so the tail is not an overshoot; the same
    # triple read from the other end is, and trims to the single step.
    assert trim_end_reversals([(0, 0), (0, 1), (0, -1)]) == [(0, 0), (0, -1)]


# ---------------------------------------------------------------------------
# Issue #7931: near-anchor jog-and-return / detour-and-return tails
# ---------------------------------------------------------------------------

# The three centerlines logged on board 03 (USB_D J1.A6->U2.3, hybrid trunk
# searches; builder note 2026-10-24T02:50Z).  Each ends with a sub-0.1 mm
# loop past the anchor (146.5, 66.7) that comes back to it.
_JOG_AND_RETURN = [
    (146.5, 63.3),
    (146.5, 66.7),
    (146.475, 66.725),
    (146.475, 67.075),
    (146.5, 67.1),
    (146.5, 66.7),
]
_DETOUR_PAST_ANCHOR = [
    (146.5, 63.3),
    (146.5, 66.9828),
    (146.4414, 67.0414),
    (146.5, 67.1),
    (146.5, 66.7),
]
_DETOUR_AT_ANCHOR = [
    (146.5, 63.3),
    (146.5, 66.7),
    (146.4414, 66.7586),
    (146.5, 66.8172),
    (146.5, 66.7),
]
# The raw search output those three were nudged from (post-#7932 main,
# board 03 seed 42): one diagonal lattice step out past the anchor and back,
# 0.2 mm sideways.  It offsets, but the two legs cross in the loop.
_LATTICE_DETOUR = [
    (146.5, 63.3),
    (146.5, 64.3),
    (146.5, 66.7),
    (146.3, 66.9),
    (146.5, 67.1),
    (146.5, 66.7),
]
_LOGGED = [_JOG_AND_RETURN, _DETOUR_PAST_ANCHOR, _DETOUR_AT_ANCHOR]
_IDS = ["jog-and-return", "detour-past-anchor", "detour-at-anchor"]

#: Board 03's USB_D class: 0.2 mm traces at a 0.35 mm pitch.  The finish
#: gate offsets the centerline by the half pitch, and the trim's loop bound
#: is the pair's copper half-width (half pitch + half trace width).
_HALF = 0.175
_BAND = 0.275


def _mirror(pts):
    """Reflect across the vertical line x = 146.5 (swaps left and right)."""
    return [(2 * 146.5 - x, y) for x, y in pts]


def _variants():
    for pts, name in zip([*_LOGGED, _LATTICE_DETOUR], [*_IDS, "lattice-detour"], strict=True):
        yield pytest.param(pts, False, id=f"{name}-tail")
        yield pytest.param(_mirror(pts), False, id=f"{name}-mirrored-tail")
        yield pytest.param(pts[::-1], True, id=f"{name}-head")
        yield pytest.param(_mirror(pts)[::-1], True, id=f"{name}-mirrored-head")


def _directions(pts):
    out = set()
    for a, b in zip(pts, pts[1:], strict=False):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length > 1e-9:
            out.add((round((b[0] - a[0]) / length, 6), round((b[1] - a[1]) / length, 6)))
    return out


@pytest.mark.parametrize("pts", _LOGGED, ids=_IDS)
def test_logged_tail_loops_decline_without_the_new_trim(pts):
    # The pre-#7931 behaviour (no lateral bound): unchanged, and the right
    # side of the offset is degenerate, which declined the whole candidate.
    assert trim_end_reversals(pts) == merge_collinear_points(pts)
    assert offset_polyline(trim_end_reversals(pts), -_HALF) is None


@pytest.mark.parametrize(("pts", "at_head"), list(_variants()))
def test_tail_loop_trimmed_offsets_on_both_sides(pts, at_head):
    center = trim_end_reversals(pts, _BAND)
    # The anchored end vertices are kept exactly.
    assert center[0] == pts[0]
    assert center[-1] == pts[-1]
    # The loop is gone: a single straight step along the searched edge.
    assert len(center) == 2
    # Octilinear stays octilinear: no direction the search did not take.
    assert _directions(center) <= _directions(pts)
    for sign in (+1, -1):
        for off in (0.1, _HALF, 0.2):
            leg = offset_polyline(center, sign * off)
            assert leg is not None, (sign, off)


def test_tail_loop_keeps_the_body_before_the_loop():
    pts = [(140.0, 63.3), (146.5, 63.3), *_JOG_AND_RETURN[1:]]
    center = trim_end_reversals(pts, _BAND)
    assert center == [(140.0, 63.3), (146.5, 63.3), (146.5, 66.7)]
    assert offset_polyline(center, _HALF) is not None
    assert offset_polyline(center, -_HALF) is not None


def test_mid_path_reversal_still_declines_with_a_lateral_bound():
    center = trim_end_reversals([(0, 0), (0, 2), (0, 1), (1, 1), (2, 2)], P)
    assert center == [(0, 0), (0, 2), (0, 1), (1, 1), (2, 2)]
    assert offset_polyline(center, P) is None


def test_mid_path_jog_loop_is_not_trimmed():
    # The same jog-and-return as the logged tail, but followed by more path:
    # it is not at an end, so it is a genuine defect and must still decline.
    pts = [*_JOG_AND_RETURN, (146.5, 66.2), (146.0, 65.7)]
    center = trim_end_reversals(pts, _BAND)
    assert center == merge_collinear_points(pts)


def test_wide_out_and_back_tail_still_declines():
    # The logged jog-and-return shape, but swinging 0.3 mm sideways -- wider
    # than the pair's copper band: a routing decision, not a lattice artefact.
    # It is left alone, and its right-hand offset still declines.
    pts = [(146.5, 63.3), (146.5, 66.7), (146.2, 67.0), (146.2, 67.4), (146.5, 67.7), (146.5, 66.7)]
    center = trim_end_reversals(pts, _BAND)
    assert center == pts
    assert offset_polyline(center, -_HALF) is None


def test_loop_back_onto_the_earlier_edge_from_either_end():
    # Read from its last vertex this tail returns BEHIND the earlier edge's
    # start, so the tail pass leaves it; read from the first vertex the
    # same loop sits on a searched edge, and that end trims it.  Both
    # anchored vertices survive and the result is one searched direction.
    pts = [(0.0, 1.0), (0.0, 2.0), (-0.05, 2.05), (0.0, 2.1), (0.0, 0.5)]
    assert trim_end_reversals(pts, _BAND) == [(0.0, 1.0), (0.0, 0.5)]
