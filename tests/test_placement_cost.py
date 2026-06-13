"""Unit tests for kicad_tools.placement.cost.

Focused on per-net wirelength weighting (issue #2822).  The module ships
with broader cost coverage in test_optimize_placement_cmd.py and the
strategy/seed test files; this module isolates the new ``Net.weight`` knob
introduced for anchor-aware optimisation.
"""

from __future__ import annotations

import pytest

from kicad_tools.placement.cost import (
    ComponentPlacement,
    Net,
    compute_wirelength,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _placements() -> list[ComponentPlacement]:
    """Three components arranged in an L-shape.

    Layout:

        A (0, 0)   B (10, 0)
                          |
                          C (10, 5)

    HPWL of net {A, B, C} is (10-0) + (5-0) = 15 mm.
    HPWL of net {A, B} is 10 mm; HPWL of net {B, C} is 5 mm.
    """
    return [
        ComponentPlacement(reference="A", x=0.0, y=0.0),
        ComponentPlacement(reference="B", x=10.0, y=0.0),
        ComponentPlacement(reference="C", x=10.0, y=5.0),
    ]


# ---------------------------------------------------------------------------
# Default-weight regression coverage
# ---------------------------------------------------------------------------


class TestComputeWirelengthDefaultWeight:
    """Net constructed without an explicit weight must behave as before."""

    def test_compute_wirelength_default_weight_unchanged(self) -> None:
        # Sanity: explicit weight=1.0 and no weight kwarg agree.
        placements = _placements()
        net_default = Net(name="N", pins=[("A", "1"), ("B", "1"), ("C", "1")])
        net_explicit = Net(
            name="N",
            pins=[("A", "1"), ("B", "1"), ("C", "1")],
            weight=1.0,
        )
        assert net_default.weight == 1.0
        assert compute_wirelength(placements, [net_default]) == compute_wirelength(
            placements, [net_explicit]
        )

    def test_compute_wirelength_matches_pre_weight_formula(self) -> None:
        """Aggregate equals the historical sum of HPWLs (pre-#2822)."""
        placements = _placements()
        nets = [
            Net(name="N1", pins=[("A", "1"), ("B", "1")]),  # HPWL = 10
            Net(name="N2", pins=[("B", "1"), ("C", "1")]),  # HPWL =  5
        ]
        assert compute_wirelength(placements, nets) == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Per-net weighting
# ---------------------------------------------------------------------------


class TestComputeWirelengthHonorsPerNetWeight:
    """compute_wirelength must scale each net's HPWL by ``net.weight``."""

    def test_weight_three_triples_contribution(self) -> None:
        placements = _placements()
        net = Net(
            name="HEAVY",
            pins=[("A", "1"), ("B", "1")],
            weight=3.0,
        )
        # HPWL = 10, weight = 3.0 -> 30.
        assert compute_wirelength(placements, [net]) == pytest.approx(30.0)

    def test_mixed_weights_sum_correctly(self) -> None:
        placements = _placements()
        nets = [
            Net(name="A_B", pins=[("A", "1"), ("B", "1")], weight=2.0),  # 10*2 = 20
            Net(name="B_C", pins=[("B", "1"), ("C", "1")], weight=0.5),  #  5*0.5 = 2.5
        ]
        assert compute_wirelength(placements, nets) == pytest.approx(22.5)

    def test_weight_scales_linearly(self) -> None:
        """Doubling the weight doubles the contribution."""
        placements = _placements()
        net1 = Net(name="N", pins=[("A", "1"), ("C", "1")], weight=1.0)
        net2 = Net(name="N", pins=[("A", "1"), ("C", "1")], weight=2.0)
        single = compute_wirelength(placements, [net1])
        doubled = compute_wirelength(placements, [net2])
        assert doubled == pytest.approx(2.0 * single)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestComputeWirelengthZeroWeight:
    """Weight of 0 must drop the net from the sum entirely."""

    def test_zero_weight_excludes_net(self) -> None:
        placements = _placements()
        kept = Net(name="KEEP", pins=[("A", "1"), ("B", "1")])  # HPWL=10
        dropped = Net(
            name="DROP",
            pins=[("B", "1"), ("C", "1")],  # would add 5 if weight==1
            weight=0.0,
        )
        assert compute_wirelength(placements, [kept, dropped]) == pytest.approx(10.0)

    def test_zero_weight_alone_yields_zero(self) -> None:
        placements = _placements()
        net = Net(name="ZERO", pins=[("A", "1"), ("B", "1"), ("C", "1")], weight=0.0)
        assert compute_wirelength(placements, [net]) == 0.0
