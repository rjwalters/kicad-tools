"""Unit tests for kicad_tools.placement.cost.

Focused on per-net wirelength weighting (issue #2822).  The module ships
with broader cost coverage in test_optimize_placement_cmd.py and the
strategy/seed test files; this module isolates the new ``Net.weight`` knob
introduced for anchor-aware optimisation.
"""

from __future__ import annotations

import pytest

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    CostMode,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    compute_creepage_violation,
    compute_wirelength,
    evaluate_placement,
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


# ---------------------------------------------------------------------------
# HV creepage-keepout term (issue #4373)
# ---------------------------------------------------------------------------

# Two 2x2 mm footprints; edge-to-edge gap = |dx| - 2 when placed on the X axis.
_HV_SIZES = {"A": (2.0, 2.0), "B": (2.0, 2.0)}
# Reproduces the AC_NEUTRAL <-> REF_1V65 evidence pair: required 1.6 mm.
_REQ = {("mains", "signal"): 1.6}


def _hv_pair(dx: float) -> list[ComponentPlacement]:
    """A at origin, B at (dx, 0). Both 2x2 mm -> gap = dx - 2 for dx >= 2."""
    return [
        ComponentPlacement(reference="A", x=0.0, y=0.0),
        ComponentPlacement(reference="B", x=dx, y=0.0),
    ]


class TestComputeCreepageViolation:
    def test_same_domain_pair_is_zero(self) -> None:
        """Two refs in the same domain never incur a keepout shortfall."""
        placements = _hv_pair(2.35)  # gap 0.35 mm -- but same domain
        domains = {"A": "mains", "B": "mains"}
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_cross_domain_below_required_reports_shortfall(self) -> None:
        placements = _hv_pair(2.35)  # gap 0.35 mm, required 1.6 -> shortfall 1.25
        domains = {"A": "mains", "B": "signal"}
        got = compute_creepage_violation(placements, domains, _REQ, _HV_SIZES)
        assert got == pytest.approx(1.25)

    def test_cross_domain_at_or_above_required_is_zero(self) -> None:
        placements = _hv_pair(3.6)  # gap exactly 1.6 mm == required -> no shortfall
        domains = {"A": "mains", "B": "signal"}
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_untabulated_domain_pair_is_zero(self) -> None:
        """A cross-domain pair with no required-distance entry is unconstrained."""
        placements = _hv_pair(2.35)
        domains = {"A": "mains", "B": "other"}  # ("mains","other") absent from _REQ
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_exempt_pair_is_skipped(self) -> None:
        """Guarded sense taps: an exempt ref pair incurs no shortfall."""
        placements = _hv_pair(2.35)
        domains = {"A": "mains", "B": "signal"}
        exempt = {frozenset(("A", "B"))}
        got = compute_creepage_violation(placements, domains, _REQ, _HV_SIZES, exempt)
        assert got == 0.0

    def test_ref_absent_from_domains_is_skipped(self) -> None:
        placements = _hv_pair(2.35)
        domains = {"A": "mains"}  # B has no domain -> pair skipped
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_empty_inputs_are_zero(self) -> None:
        placements = _hv_pair(2.35)
        assert compute_creepage_violation(placements, {}, _REQ, _HV_SIZES) == 0.0
        assert compute_creepage_violation(placements, {"A": "mains"}, {}, _HV_SIZES) == 0.0


class TestEvaluatePlacementCreepageFeasibility:
    """The creepage term gates feasibility in evaluate_placement."""

    _BOARD = BoardOutline(min_x=-10.0, min_y=-10.0, max_x=10.0, max_y=10.0)
    _RULES = DesignRuleSet()

    def test_too_close_is_infeasible(self) -> None:
        placements = _hv_pair(2.35)  # gap 0.35 < 1.6
        domains = {"A": "mains", "B": "signal"}
        score = evaluate_placement(
            placements,
            nets=[],
            rules=self._RULES,
            board=self._BOARD,
            config=PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC),
            footprint_sizes=_HV_SIZES,
            ref_domains=domains,
            required_mm_by_domain_pair=_REQ,
        )
        assert score.breakdown.creepage == pytest.approx(1.25)
        assert score.is_feasible is False
        # Lexicographic: infeasible placements sit above the sentinel offset.
        assert score.total >= 1e12

    def test_separated_is_feasible(self) -> None:
        placements = _hv_pair(3.6)  # gap 1.6 == required
        domains = {"A": "mains", "B": "signal"}
        score = evaluate_placement(
            placements,
            nets=[],
            rules=self._RULES,
            board=self._BOARD,
            config=PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC),
            footprint_sizes=_HV_SIZES,
            ref_domains=domains,
            required_mm_by_domain_pair=_REQ,
        )
        assert score.breakdown.creepage == 0.0
        assert score.is_feasible is True

    def test_no_domain_input_is_byte_identical(self) -> None:
        """Absent a domain/voltage input the creepage term stays dormant."""
        placements = _hv_pair(2.35)
        common = {
            "nets": [],
            "rules": self._RULES,
            "board": self._BOARD,
            "config": PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC),
            "footprint_sizes": _HV_SIZES,
        }
        baseline = evaluate_placement(placements, **common)
        # Passing domains but no required table (and vice versa) must also no-op.
        with_domains_only = evaluate_placement(
            placements, ref_domains={"A": "mains", "B": "signal"}, **common
        )
        assert baseline.breakdown.creepage == 0.0
        assert with_domains_only.breakdown.creepage == 0.0
        assert with_domains_only.total == baseline.total
