"""Trailing/leading collinear reversal trim before pair offsetting (#6958)."""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.coupled import offset_polyline, trim_end_reversals

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
