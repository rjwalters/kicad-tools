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
    compute_domain_cohesion,
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


# ---------------------------------------------------------------------------
# Same-domain clustering / cohesion term (issue #4373 Phase 1)
# ---------------------------------------------------------------------------


class TestComputeDomainCohesion:
    def test_empty_ref_domains_is_zero(self) -> None:
        placements = _hv_pair(10.0)
        assert compute_domain_cohesion(placements, {}) == 0.0
        assert compute_domain_cohesion(placements, None) == 0.0

    def test_single_domain_single_member_is_zero(self) -> None:
        """A domain with one member has zero spread."""
        placements = _hv_pair(10.0)
        assert compute_domain_cohesion(placements, {"A": "mains"}) == 0.0

    def test_all_singleton_domains_is_zero(self) -> None:
        """Every domain a singleton -> nothing to cluster."""
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "signal"}
        assert compute_domain_cohesion(placements, domains) == 0.0

    def test_monotonic_in_spread(self) -> None:
        """Two refs in one domain 10 mm apart cost more than 1 mm apart."""
        domains = {"A": "mains", "B": "mains"}
        far = compute_domain_cohesion(_hv_pair(10.0), domains)
        near = compute_domain_cohesion(_hv_pair(1.0), domains)
        assert far > near
        # Each member sits at half the separation from the centroid:
        # 2 members * (dx / 2) = dx.
        assert far == pytest.approx(10.0)
        assert near == pytest.approx(1.0)

    def test_domainless_refs_contribute_zero(self) -> None:
        """A ref absent from ref_domains does not join any cluster."""
        placements = [
            ComponentPlacement(reference="A", x=0.0, y=0.0),
            ComponentPlacement(reference="B", x=10.0, y=0.0),
            ComponentPlacement(reference="C", x=100.0, y=0.0),
        ]
        # C is domain-less; only A,B (same domain) cluster.
        domains = {"A": "mains", "B": "mains"}
        assert compute_domain_cohesion(placements, domains) == pytest.approx(10.0)

    def test_footprint_sizes_ignored(self) -> None:
        """footprint_sizes is accepted for parity but does not change the value."""
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        with_sizes = compute_domain_cohesion(placements, domains, _HV_SIZES)
        without = compute_domain_cohesion(placements, domains)
        assert with_sizes == without


class TestEvaluatePlacementCohesion:
    """Cohesion is a soft term: it scores but never gates feasibility."""

    _BOARD = BoardOutline(min_x=-100.0, min_y=-100.0, max_x=100.0, max_y=100.0)
    _RULES = DesignRuleSet()

    def _common(self, config: PlacementCostConfig) -> dict:
        return {
            "nets": [],
            "rules": self._RULES,
            "board": self._BOARD,
            "config": config,
            "footprint_sizes": _HV_SIZES,
        }

    def test_cohesion_populated_when_ref_domains_supplied(self) -> None:
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        score = evaluate_placement(
            placements,
            ref_domains=domains,
            **self._common(PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC)),
        )
        assert score.breakdown.cohesion == pytest.approx(10.0)

    def test_high_cohesion_keeps_feasible(self) -> None:
        """A large spread does NOT flip is_feasible (soft-term proof)."""
        placements = _hv_pair(10.0)  # 8 mm gap -> no overlap/drc/creepage
        domains = {"A": "mains", "B": "mains"}  # same domain -> no keepout
        score = evaluate_placement(
            placements,
            ref_domains=domains,
            required_mm_by_domain_pair=_REQ,
            **self._common(PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC)),
        )
        assert score.breakdown.cohesion == pytest.approx(10.0)
        assert score.breakdown.overlap == 0.0
        assert score.breakdown.drc == 0.0
        assert score.breakdown.creepage == 0.0
        assert score.is_feasible is True
        # Feasible: total sits below the infeasibility sentinel.
        assert score.total < 1e12

    def test_cohesion_enters_feasible_branch_only(self) -> None:
        """Feasible total includes the weighted cohesion term."""
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        config = PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC, cohesion_weight=2.0)
        score = evaluate_placement(placements, ref_domains=domains, **self._common(config))
        assert score.is_feasible is True
        # wirelength=0 (no nets), area = bbox area of the two 2x2 footprints.
        expected = config.area_weight * score.breakdown.area + 2.0 * score.breakdown.cohesion
        assert score.total == pytest.approx(expected)

    def test_cohesion_absent_from_infeasible_branch(self) -> None:
        """An infeasible (overlapping) placement's score excludes cohesion."""
        placements = _hv_pair(0.5)  # overlapping 2x2 footprints -> infeasible
        domains = {"A": "mains", "B": "mains"}
        config = PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC, cohesion_weight=1e9)
        score = evaluate_placement(placements, ref_domains=domains, **self._common(config))
        assert score.is_feasible is False
        # Infeasible offset branch = OFFSET + overlap/drc/boundary/creepage only.
        # A huge cohesion_weight must NOT leak into the total.
        expected = 1e12 + (
            config.overlap_weight * score.breakdown.overlap
            + config.drc_weight * score.breakdown.drc
            + config.boundary_weight * score.breakdown.boundary
            + config.creepage_weight * score.breakdown.creepage
        )
        assert score.total == pytest.approx(expected)

    def test_weighted_sum_includes_cohesion(self) -> None:
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        config = PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, cohesion_weight=3.0)
        score = evaluate_placement(placements, ref_domains=domains, **self._common(config))
        # Isolate the cohesion contribution against a zero-weight baseline.
        baseline = evaluate_placement(
            placements,
            ref_domains=domains,
            **self._common(PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, cohesion_weight=0.0)),
        )
        assert score.total - baseline.total == pytest.approx(3.0 * score.breakdown.cohesion)
