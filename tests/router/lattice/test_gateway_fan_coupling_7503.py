"""Gateway fan legs must not couple at the wrong gap (issue #7503).

Board 06rf: the USB3 TX1/RX2 gateways at J1 fanned both nets diagonally from
pads 1.0 mm apart, so the two 45 degree legs ran 0.707 mm apart centre to
centre (0.432 mm edge gap) -- about 112 ohm against the 90 ohm target, which
the impedance rule reports as real coupled copper.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.pairplan import gateway as gw
from kicad_tools.validate.rules import impedance

HALF = 0.1375  # 0.275 mm USB3 trace
PITCH = 0.45  # 0.175 mm gap


def test_mirrored_predicate_matches_the_impedance_rule() -> None:
    assert gw.FAN_COUPLING_WINDOW_MM == impedance.DEFAULT_COUPLING_WINDOW_MM
    assert gw.FAN_PARALLEL_SIN_TOLERANCE == impedance.ImpedanceRule._PARALLEL_SIN_TOLERANCE


def test_parallel_diagonal_fans_from_a_one_mm_row_are_rejected() -> None:
    # The 06rf TX1 J1 fans, verbatim.
    fan_p = [(10.5, 8.0), (9.6515, 7.1515)]
    fan_n = [(11.5, 8.0), (10.6515, 7.1515)]
    assert gw.fans_couple_off_pitch(fan_p, fan_n, half=HALF, pitch=PITCH)
    assert gw.fans_couple_off_pitch(fan_n, fan_p, half=HALF, pitch=PITCH)


def test_dogleg_fan_with_a_parallel_diagonal_piece_is_rejected() -> None:
    # The 06rf RX2 J1 fans: N's diagonal plus a short axis tail.
    fan_p = [(19.5, 8.0), (20.349, 7.151), (20.414, 7.151)]
    fan_n = [(18.5, 8.0), (19.849, 6.651), (19.849, 6.586)]
    assert gw.fans_couple_off_pitch(fan_p, fan_n, half=HALF, pitch=PITCH)


def test_straight_fans_off_a_wide_row_are_outside_the_window() -> None:
    # 1.0 mm apart: a 0.725 mm edge gap is a breakout, not a coupled run.
    fan_p = [(10.5, 8.0), (10.5, 7.0)]
    fan_n = [(11.5, 8.0), (11.5, 7.0)]
    assert not gw.fans_couple_off_pitch(fan_p, fan_n, half=HALF, pitch=PITCH)


def test_fans_at_the_pair_pitch_are_kept() -> None:
    fan_p = [(0.0, 0.0), (0.0, -1.0)]
    fan_n = [(PITCH, 0.0), (PITCH, -1.0)]
    assert not gw.fans_couple_off_pitch(fan_p, fan_n, half=HALF, pitch=PITCH)


def test_diverging_fans_are_kept() -> None:
    d = 1.0 / math.sqrt(2.0)
    fan_p = [(0.0, 0.0), (-d, -d)]
    fan_n = [(1.0, 0.0), (1.0 + d, -d)]
    assert not gw.fans_couple_off_pitch(fan_p, fan_n, half=HALF, pitch=PITCH)


def test_parallel_fans_that_do_not_overlap_are_kept() -> None:
    fan_p = [(0.0, 0.0), (0.0, -1.0)]
    fan_n = [(0.6, -1.5), (0.6, -2.5)]
    assert not gw.fans_couple_off_pitch(fan_p, fan_n, half=HALF, pitch=PITCH)


@pytest.mark.parametrize("centre", [0.30, 0.55, 0.70, 0.77])
def test_any_off_pitch_gap_inside_the_window_is_rejected(centre: float) -> None:
    fan_p = [(0.0, 0.0), (0.0, -1.0)]
    fan_n = [(centre, 0.0), (centre, -1.0)]
    assert gw.fans_couple_off_pitch(fan_p, fan_n, half=HALF, pitch=PITCH)
