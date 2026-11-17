"""Unit tests for the pin-geometry slack-budget estimator (Issue #4085).

The estimator predicts the length skew a diff pair (or N-member match
group) accumulates purely from pin placement, BEFORE routing.  These
tests pin the pure-geometry contract against hand-constructed fixtures
with known expected spans -- no grid, no routing state.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.slack_budget import (
    _manhattan_span,
    estimate_group_skew_budget,
    estimate_pair_skew_budget,
)


class TestManhattanSpan:
    def test_two_pin_horizontal(self):
        # Two pins 10mm apart in x only -> span 10.
        assert _manhattan_span([(0.0, 0.0), (10.0, 0.0)]) == pytest.approx(10.0)

    def test_two_pin_diagonal(self):
        # dx=3, dy=4 -> Manhattan span 7 (not the 5mm Euclidean distance).
        assert _manhattan_span([(0.0, 0.0), (3.0, 4.0)]) == pytest.approx(7.0)

    def test_bounding_box_span_over_multiple_pins(self):
        # Four pins; bbox is x in [0,5], y in [0,2] -> span 7.
        pins = [(0.0, 0.0), (5.0, 1.0), (2.0, 2.0), (1.0, 0.5)]
        assert _manhattan_span(pins) == pytest.approx(7.0)

    def test_single_pin_is_zero(self):
        assert _manhattan_span([(1.0, 1.0)]) == 0.0

    def test_empty_is_zero(self):
        assert _manhattan_span([]) == 0.0

    def test_span_is_order_independent(self):
        a = [(0.0, 0.0), (5.0, 1.0), (2.0, 2.0)]
        b = [(2.0, 2.0), (0.0, 0.0), (5.0, 1.0)]
        assert _manhattan_span(a) == pytest.approx(_manhattan_span(b))


class TestPairSkewBudget:
    def test_matched_legs_zero_budget(self):
        # Two legs with identical spans -> zero estimated skew.
        leg_a = [(0.0, 0.0), (10.0, 0.0)]
        leg_b = [(0.0, 5.0), (10.0, 5.0)]
        assert estimate_pair_skew_budget(leg_a, leg_b) == pytest.approx(0.0)

    def test_known_skew(self):
        # Leg A span 10, leg B span 3 -> budget 7.
        leg_a = [(0.0, 0.0), (10.0, 0.0)]
        leg_b = [(0.0, 0.0), (3.0, 0.0)]
        assert estimate_pair_skew_budget(leg_a, leg_b) == pytest.approx(7.0)

    def test_symmetric(self):
        leg_a = [(0.0, 0.0), (12.0, 0.0)]
        leg_b = [(0.0, 0.0), (2.0, 3.0)]  # span 5
        forward = estimate_pair_skew_budget(leg_a, leg_b)
        backward = estimate_pair_skew_budget(leg_b, leg_a)
        assert forward == pytest.approx(backward)
        assert forward == pytest.approx(7.0)

    def test_always_non_negative(self):
        leg_a = [(0.0, 0.0), (1.0, 0.0)]
        leg_b = [(0.0, 0.0), (99.0, 0.0)]
        assert estimate_pair_skew_budget(leg_a, leg_b) >= 0.0

    def test_degenerate_single_pin_leg_is_zero_span(self):
        # A leg with a single pin has zero span; budget is the other leg's
        # full span.
        leg_a = [(0.0, 0.0), (8.0, 0.0)]  # span 8
        leg_b = [(0.0, 0.0)]  # span 0
        assert estimate_pair_skew_budget(leg_a, leg_b) == pytest.approx(8.0)


class TestGroupSkewBudget:
    def test_spread_of_three_legs(self):
        # Spans: 10, 4, 7 -> spread 10 - 4 = 6.
        legs = [
            [(0.0, 0.0), (10.0, 0.0)],
            [(0.0, 0.0), (4.0, 0.0)],
            [(0.0, 0.0), (7.0, 0.0)],
        ]
        assert estimate_group_skew_budget(legs) == pytest.approx(6.0)

    def test_single_member_is_zero(self):
        assert estimate_group_skew_budget([[(0.0, 0.0), (5.0, 0.0)]]) == 0.0

    def test_empty_is_zero(self):
        assert estimate_group_skew_budget([]) == 0.0

    def test_matched_group_zero(self):
        legs = [
            [(0.0, 0.0), (6.0, 0.0)],
            [(0.0, 2.0), (6.0, 2.0)],
            [(0.0, 4.0), (6.0, 4.0)],
        ]
        assert estimate_group_skew_budget(legs) == pytest.approx(0.0)
