"""Tests for the multi-fidelity placement evaluation pipeline.

Covers:
- All four fidelity levels produce valid scores
- Higher fidelity includes lower-fidelity checks
- Parameter validation (missing required args at each level)
- Cost model returns correct relative costs
- Fidelity 0 runs fast (< 1 ms on small boards)
- DefaultFidelitySelector budget-based selection
- make_fixed_fidelity_evaluator closure
- make_adaptive_evaluator state tracking
- PlacedComponent and ComponentPlacement interoperability at fidelity 0
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    CostMode,
    Net,
    PlacementCostConfig,
)
from kicad_tools.placement.multi_fidelity import (
    FIDELITY_COST,
    DefaultFidelitySelector,
    FidelityConfig,
    FidelityLevel,
    FidelityResult,
    RoutabilityResult,
    evaluate_placement_multifidelity,
    make_adaptive_evaluator,
    make_fixed_fidelity_evaluator,
)
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    TransformedPad,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_board() -> BoardOutline:
    """A 50x50 mm board."""
    return BoardOutline(min_x=0.0, min_y=0.0, max_x=50.0, max_y=50.0)


@pytest.fixture
def simple_placements() -> list[ComponentPlacement]:
    """Two well-separated components."""
    return [
        ComponentPlacement(reference="R1", x=10.0, y=10.0),
        ComponentPlacement(reference="R2", x=40.0, y=40.0),
    ]


@pytest.fixture
def overlapping_placements() -> list[ComponentPlacement]:
    """Two overlapping components (same position)."""
    return [
        ComponentPlacement(reference="R1", x=25.0, y=25.0),
        ComponentPlacement(reference="R2", x=25.0, y=25.0),
    ]


@pytest.fixture
def simple_nets() -> list[Net]:
    """One net connecting R1 pin 1 to R2 pin 1."""
    return [
        Net(name="N1", pins=[("R1", "1"), ("R2", "1")]),
    ]


@pytest.fixture
def component_defs() -> list[ComponentDef]:
    """Component definitions for R1 and R2 with pads."""
    return [
        ComponentDef(
            reference="R1",
            pads=(
                PadDef(name="1", local_x=-0.5, local_y=0.0, size_x=0.4, size_y=0.4),
                PadDef(name="2", local_x=0.5, local_y=0.0, size_x=0.4, size_y=0.4),
            ),
            width=2.0,
            height=1.0,
        ),
        ComponentDef(
            reference="R2",
            pads=(
                PadDef(name="1", local_x=-0.5, local_y=0.0, size_x=0.4, size_y=0.4),
                PadDef(name="2", local_x=0.5, local_y=0.0, size_x=0.4, size_y=0.4),
            ),
            width=2.0,
            height=1.0,
        ),
    ]


@pytest.fixture
def placed_components() -> list[PlacedComponent]:
    """PlacedComponent objects for R1 and R2 (well-separated)."""
    return [
        PlacedComponent(
            reference="R1",
            x=10.0,
            y=10.0,
            rotation=0.0,
            side=0,
            pads=(
                TransformedPad(name="1", x=9.5, y=10.0, size_x=0.4, size_y=0.4),
                TransformedPad(name="2", x=10.5, y=10.0, size_x=0.4, size_y=0.4),
            ),
        ),
        PlacedComponent(
            reference="R2",
            x=40.0,
            y=40.0,
            rotation=0.0,
            side=0,
            pads=(
                TransformedPad(name="1", x=39.5, y=40.0, size_x=0.4, size_y=0.4),
                TransformedPad(name="2", x=40.5, y=40.0, size_x=0.4, size_y=0.4),
            ),
        ),
    ]


@pytest.fixture
def close_placed_components() -> list[PlacedComponent]:
    """PlacedComponent objects very close together (DRC violation expected)."""
    return [
        PlacedComponent(
            reference="R1",
            x=10.0,
            y=10.0,
            rotation=0.0,
            side=0,
            pads=(
                TransformedPad(name="1", x=9.5, y=10.0, size_x=0.4, size_y=0.4),
                TransformedPad(name="2", x=10.5, y=10.0, size_x=0.4, size_y=0.4),
            ),
        ),
        PlacedComponent(
            reference="R2",
            x=11.0,  # Very close to R1
            y=10.0,
            rotation=0.0,
            side=0,
            pads=(
                TransformedPad(name="1", x=10.5, y=10.0, size_x=0.4, size_y=0.4),
                TransformedPad(name="2", x=11.5, y=10.0, size_x=0.4, size_y=0.4),
            ),
        ),
    ]


@pytest.fixture
def design_rules():
    """Design rules for DRC checking."""
    from kicad_tools.router.rules import DesignRules

    return DesignRules(trace_clearance=0.2)


# ---------------------------------------------------------------------------
# FidelityLevel enum tests
# ---------------------------------------------------------------------------


class TestFidelityLevel:
    """Tests for the FidelityLevel enum."""

    def test_values(self):
        assert FidelityLevel.HPWL == 0
        assert FidelityLevel.DRC == 1
        assert FidelityLevel.GLOBAL_ROUTE == 2
        assert FidelityLevel.FULL_ROUTE == 3

    def test_ordering(self):
        assert FidelityLevel.HPWL < FidelityLevel.DRC
        assert FidelityLevel.DRC < FidelityLevel.GLOBAL_ROUTE
        assert FidelityLevel.GLOBAL_ROUTE < FidelityLevel.FULL_ROUTE

    def test_can_construct_from_int(self):
        assert FidelityLevel(0) == FidelityLevel.HPWL
        assert FidelityLevel(3) == FidelityLevel.FULL_ROUTE


class TestFidelityCost:
    """Tests for the FIDELITY_COST mapping."""

    def test_all_levels_have_costs(self):
        for level in FidelityLevel:
            assert level in FIDELITY_COST

    def test_cost_model_values(self):
        assert FIDELITY_COST[FidelityLevel.HPWL] == 1
        assert FIDELITY_COST[FidelityLevel.DRC] == 10
        assert FIDELITY_COST[FidelityLevel.GLOBAL_ROUTE] == 100
        assert FIDELITY_COST[FidelityLevel.FULL_ROUTE] == 1000

    def test_costs_monotonically_increasing(self):
        levels = sorted(FidelityLevel)
        costs = [FIDELITY_COST[l] for l in levels]
        for i in range(1, len(costs)):
            assert costs[i] > costs[i - 1]


# ---------------------------------------------------------------------------
# Fidelity 0 (HPWL) tests
# ---------------------------------------------------------------------------


class TestFidelity0:
    """Tests for fidelity 0 evaluation (HPWL + overlap + boundary)."""

    def test_basic_feasible_placement(self, simple_placements, simple_nets, simple_board):
        result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )

        assert result.fidelity == FidelityLevel.HPWL
        assert result.cost == 1
        assert result.score.total > 0  # Wirelength contributes
        assert result.score.is_feasible is True
        assert result.score.breakdown.overlap == 0.0
        assert result.score.breakdown.boundary == 0.0
        assert result.score.breakdown.drc == 0.0  # Not computed at fidelity 0
        assert result.score.breakdown.wirelength > 0.0
        assert result.wall_time_ms >= 0.0
        assert result.drc_result is None
        assert result.routability is None

    def test_overlapping_placement(self, overlapping_placements, simple_nets, simple_board):
        result = evaluate_placement_multifidelity(
            placements=overlapping_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=0,  # Test integer fidelity
        )

        assert result.score.is_feasible is False
        assert result.score.breakdown.overlap > 0.0

    def test_boundary_violation(self, simple_nets, simple_board):
        placements = [
            ComponentPlacement(reference="R1", x=-1.0, y=25.0),
            ComponentPlacement(reference="R2", x=25.0, y=25.0),
        ]

        result = evaluate_placement_multifidelity(
            placements=placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )

        assert result.score.is_feasible is False
        assert result.score.breakdown.boundary > 0.0

    def test_empty_nets(self, simple_placements, simple_board):
        result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=[],
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )

        assert result.score.breakdown.wirelength == 0.0

    def test_accepts_placed_component(self, placed_components, simple_nets, simple_board):
        """Fidelity 0 should accept PlacedComponent objects too."""
        result = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )

        assert result.fidelity == FidelityLevel.HPWL
        assert result.score.total > 0

    def test_fast_evaluation(self, simple_placements, simple_nets, simple_board):
        """Fidelity 0 should complete quickly (< 5 ms for 2 components)."""
        t0 = time.perf_counter()
        for _ in range(100):
            evaluate_placement_multifidelity(
                placements=simple_placements,
                nets=simple_nets,
                board=simple_board,
                fidelity=FidelityLevel.HPWL,
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        avg_ms = elapsed_ms / 100

        # Average should be well under 1 ms for 2 components
        assert avg_ms < 5.0, f"Fidelity 0 took {avg_ms:.2f} ms avg (expected < 5 ms)"

    def test_no_args_needed(self, simple_placements, simple_nets, simple_board):
        """Fidelity 0 does not require component_defs or design_rules."""
        result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )
        assert result.score is not None


# ---------------------------------------------------------------------------
# Fidelity 1 (DRC) tests
# ---------------------------------------------------------------------------


class TestFidelity1:
    """Tests for fidelity 1 evaluation (+ DRC checking)."""

    def test_requires_component_defs(
        self, placed_components, simple_nets, simple_board, design_rules
    ):
        with pytest.raises(ValueError, match="component_defs is required"):
            evaluate_placement_multifidelity(
                placements=placed_components,
                nets=simple_nets,
                board=simple_board,
                fidelity=FidelityLevel.DRC,
                design_rules=design_rules,
            )

    def test_requires_design_rules(
        self, placed_components, simple_nets, simple_board, component_defs
    ):
        with pytest.raises(ValueError, match="design_rules is required"):
            evaluate_placement_multifidelity(
                placements=placed_components,
                nets=simple_nets,
                board=simple_board,
                fidelity=FidelityLevel.DRC,
                component_defs=component_defs,
            )

    def test_requires_placed_component_type(
        self, simple_placements, simple_nets, simple_board, component_defs, design_rules
    ):
        """Fidelity >= 1 requires PlacedComponent, not ComponentPlacement."""
        with pytest.raises(ValueError, match="PlacedComponent"):
            evaluate_placement_multifidelity(
                placements=simple_placements,
                nets=simple_nets,
                board=simple_board,
                fidelity=FidelityLevel.DRC,
                component_defs=component_defs,
                design_rules=design_rules,
            )

    def test_well_separated_no_drc_violations(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        result = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.DRC,
            component_defs=component_defs,
            design_rules=design_rules,
        )

        assert result.fidelity == FidelityLevel.DRC
        assert result.cost == 10
        assert result.score.is_feasible is True
        assert result.drc_result is not None
        assert result.drc_result.violation_count == 0
        assert result.score.breakdown.drc == 0.0

    def test_close_components_drc_violations(
        self,
        close_placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        result = evaluate_placement_multifidelity(
            placements=close_placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.DRC,
            component_defs=component_defs,
            design_rules=design_rules,
        )

        assert result.fidelity == FidelityLevel.DRC
        assert result.drc_result is not None
        # Close components should trigger courtyard or pad violations
        assert result.drc_result.violation_count > 0
        assert result.score.breakdown.drc > 0.0
        assert result.score.is_feasible is False

    def test_includes_fidelity_0_components(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """Fidelity 1 should still compute wirelength, overlap, boundary."""
        result = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.DRC,
            component_defs=component_defs,
            design_rules=design_rules,
        )

        # Wirelength should be computed (components are 30mm apart)
        assert result.score.breakdown.wirelength > 0.0
        # Area should be computed
        assert result.score.breakdown.area > 0.0


# ---------------------------------------------------------------------------
# Fidelity 2 (Global Route) validation tests
# ---------------------------------------------------------------------------


class TestFidelity2Validation:
    """Tests for fidelity 2 parameter validation."""

    def test_requires_global_router(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        with pytest.raises(ValueError, match="global_router is required"):
            evaluate_placement_multifidelity(
                placements=placed_components,
                nets=simple_nets,
                board=simple_board,
                fidelity=FidelityLevel.GLOBAL_ROUTE,
                component_defs=component_defs,
                design_rules=design_rules,
            )

    def test_with_mock_global_router(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """Fidelity 2 with a mock global router that succeeds."""
        mock_router = MagicMock()
        mock_result = MagicMock()
        mock_result.failed_nets = []
        mock_router.route_all.return_value = mock_result

        result = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.GLOBAL_ROUTE,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=mock_router,
        )

        assert result.fidelity == FidelityLevel.GLOBAL_ROUTE
        assert result.cost == 100
        assert result.routability is not None
        assert result.routability.routability_ratio == 1.0
        assert result.routability.routed_nets == 1
        assert result.routability.failed_nets == 0

    def test_with_failing_global_router(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """Fidelity 2 with a global router that fails some nets."""
        mock_router = MagicMock()
        mock_result = MagicMock()
        mock_result.failed_nets = [0]  # One net failed
        mock_router.route_all.return_value = mock_result

        result = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.GLOBAL_ROUTE,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=mock_router,
        )

        assert result.routability is not None
        assert result.routability.routability_ratio == 0.0
        assert result.routability.failed_nets == 1
        assert result.score.is_feasible is False

    def test_global_router_exception_handled(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """If global router raises, routability should be zero."""
        mock_router = MagicMock()
        mock_router.route_all.side_effect = RuntimeError("routing failed")

        result = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.GLOBAL_ROUTE,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=mock_router,
        )

        assert result.routability is not None
        assert result.routability.routability_ratio == 0.0
        assert result.score.is_feasible is False


# ---------------------------------------------------------------------------
# Fidelity 3 (Full Route) validation tests
# ---------------------------------------------------------------------------


class TestFidelity3Validation:
    """Tests for fidelity 3 parameter validation."""

    def test_requires_orchestrator(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        mock_router = MagicMock()
        mock_result = MagicMock()
        mock_result.failed_nets = []
        mock_router.route_all.return_value = mock_result

        with pytest.raises(ValueError, match="orchestrator is required"):
            evaluate_placement_multifidelity(
                placements=placed_components,
                nets=simple_nets,
                board=simple_board,
                fidelity=FidelityLevel.FULL_ROUTE,
                component_defs=component_defs,
                design_rules=design_rules,
                global_router=mock_router,
            )

    def test_with_mock_orchestrator(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """Fidelity 3 with mock router and orchestrator."""
        mock_router = MagicMock()
        mock_gr_result = MagicMock()
        mock_gr_result.failed_nets = []
        mock_router.route_all.return_value = mock_gr_result

        mock_orchestrator = MagicMock()
        mock_route_result = MagicMock()
        mock_route_result.success = True
        mock_orchestrator.route_net.return_value = mock_route_result

        result = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.FULL_ROUTE,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=mock_router,
            orchestrator=mock_orchestrator,
        )

        assert result.fidelity == FidelityLevel.FULL_ROUTE
        assert result.cost == 1000
        assert result.routability is not None
        assert result.routability.routability_ratio == 1.0
        assert result.score.is_feasible is True

    def test_orchestrator_partial_failure(
        self,
        component_defs,
        simple_board,
        design_rules,
    ):
        """Fidelity 3 where orchestrator fails some nets."""
        nets = [
            Net(name="N1", pins=[("R1", "1"), ("R2", "1")]),
            Net(name="N2", pins=[("R1", "2"), ("R2", "2")]),
        ]

        placed = [
            PlacedComponent(
                reference="R1",
                x=10.0,
                y=10.0,
                rotation=0.0,
                side=0,
                pads=(
                    TransformedPad(name="1", x=9.5, y=10.0, size_x=0.4, size_y=0.4),
                    TransformedPad(name="2", x=10.5, y=10.0, size_x=0.4, size_y=0.4),
                ),
            ),
            PlacedComponent(
                reference="R2",
                x=40.0,
                y=40.0,
                rotation=0.0,
                side=0,
                pads=(
                    TransformedPad(name="1", x=39.5, y=40.0, size_x=0.4, size_y=0.4),
                    TransformedPad(name="2", x=40.5, y=40.0, size_x=0.4, size_y=0.4),
                ),
            ),
        ]

        mock_router = MagicMock()
        mock_gr_result = MagicMock()
        mock_gr_result.failed_nets = []
        mock_router.route_all.return_value = mock_gr_result

        mock_orchestrator = MagicMock()
        # First net succeeds, second fails
        success_result = MagicMock()
        success_result.success = True
        fail_result = MagicMock()
        fail_result.success = False
        mock_orchestrator.route_net.side_effect = [success_result, fail_result]

        result = evaluate_placement_multifidelity(
            placements=placed,
            nets=nets,
            board=simple_board,
            fidelity=FidelityLevel.FULL_ROUTE,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=mock_router,
            orchestrator=mock_orchestrator,
        )

        assert result.routability is not None
        assert result.routability.routed_nets == 1
        assert result.routability.failed_nets == 1
        assert result.routability.routability_ratio == 0.5
        assert result.score.is_feasible is False


# ---------------------------------------------------------------------------
# Score monotonicity tests
# ---------------------------------------------------------------------------


class TestScoreMonotonicity:
    """Higher fidelity scores are strictly more informative."""

    def test_fidelity_0_and_1_agree_when_no_drc_violations(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """When no DRC violations exist, fidelity 0 and 1 should produce
        similar base scores (wirelength + overlap + boundary + area)."""
        r0 = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )
        r1 = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.DRC,
            component_defs=component_defs,
            design_rules=design_rules,
        )

        # Both should be feasible
        assert r0.score.is_feasible is True
        assert r1.score.is_feasible is True

        # Wirelength should be similar (both use component centers)
        assert abs(r0.score.breakdown.wirelength - r1.score.breakdown.wirelength) < 0.01

    def test_drc_violations_increase_score(
        self,
        close_placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """Fidelity 1 (with DRC violations) should produce a higher
        score than fidelity 0 for the same placement."""
        r0 = evaluate_placement_multifidelity(
            placements=close_placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )
        r1 = evaluate_placement_multifidelity(
            placements=close_placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.DRC,
            component_defs=component_defs,
            design_rules=design_rules,
        )

        # Fidelity 1 detects DRC violations, so its score is higher (worse)
        assert r1.score.total >= r0.score.total

    def test_routability_penalty_increases_score(
        self,
        placed_components,
        component_defs,
        simple_nets,
        simple_board,
        design_rules,
    ):
        """Failed routing at fidelity 2 should increase the score."""
        # Fidelity 1 baseline
        r1 = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.DRC,
            component_defs=component_defs,
            design_rules=design_rules,
        )

        # Fidelity 2 with failing router
        mock_router = MagicMock()
        mock_result = MagicMock()
        mock_result.failed_nets = [0]  # All nets fail
        mock_router.route_all.return_value = mock_result

        r2 = evaluate_placement_multifidelity(
            placements=placed_components,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.GLOBAL_ROUTE,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=mock_router,
        )

        # Failed routing should increase score
        assert r2.score.total > r1.score.total


# ---------------------------------------------------------------------------
# Configuration tests
# ---------------------------------------------------------------------------


class TestFidelityConfig:
    """Tests for FidelityConfig."""

    def test_defaults(self):
        config = FidelityConfig()
        assert config.cost_config == PlacementCostConfig()
        assert config.drc_violation_weight == 1e4
        assert config.routability_weight == 1e3
        assert config.footprint_sizes is None

    def test_custom_config(self, simple_placements, simple_nets, simple_board):
        config = FidelityConfig(
            cost_config=PlacementCostConfig(wirelength_weight=2.0),
            drc_violation_weight=5e3,
        )

        result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
            config=config,
        )

        # Custom wirelength weight should affect total
        default_result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )

        # With doubled wirelength weight, total should be higher
        assert result.score.total > default_result.score.total

    def test_lexicographic_mode(self, simple_placements, simple_nets, simple_board):
        config = FidelityConfig(
            cost_config=PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC),
        )

        result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
            config=config,
        )

        assert result.score.is_feasible is True
        assert result.score.total > 0


# ---------------------------------------------------------------------------
# DefaultFidelitySelector tests
# ---------------------------------------------------------------------------


class TestDefaultFidelitySelector:
    """Tests for the default budget-based fidelity selector."""

    def test_high_budget_selects_hpwl(self):
        """Budget above all thresholds should select the cheapest level."""
        selector = DefaultFidelitySelector()
        fidelity = selector.select_fidelity(
            iteration=0,
            current_best=None,
            budget_remaining=0.9,
        )
        assert fidelity == FidelityLevel.HPWL

    def test_budget_at_075_selects_hpwl(self):
        """Budget at 0.75 should select HPWL (matching the threshold)."""
        selector = DefaultFidelitySelector()
        fidelity = selector.select_fidelity(
            iteration=0,
            current_best=None,
            budget_remaining=0.75,
        )
        assert fidelity == FidelityLevel.HPWL

    def test_medium_budget_selects_drc(self):
        """Budget between 0.20 and 0.50 should select DRC."""
        selector = DefaultFidelitySelector()
        fidelity = selector.select_fidelity(
            iteration=50,
            current_best=None,
            budget_remaining=0.4,
        )
        assert fidelity == FidelityLevel.DRC

    def test_low_budget_selects_global_route(self):
        """Budget between 0.05 and 0.20 should select GLOBAL_ROUTE."""
        selector = DefaultFidelitySelector()
        fidelity = selector.select_fidelity(
            iteration=100,
            current_best=None,
            budget_remaining=0.15,
        )
        assert fidelity == FidelityLevel.GLOBAL_ROUTE

    def test_very_low_budget_selects_full_route(self):
        """Budget at or below 0.05 should select FULL_ROUTE."""
        selector = DefaultFidelitySelector()
        fidelity = selector.select_fidelity(
            iteration=200,
            current_best=None,
            budget_remaining=0.03,
        )
        assert fidelity == FidelityLevel.FULL_ROUTE

    def test_custom_thresholds(self):
        selector = DefaultFidelitySelector(
            thresholds={
                0.5: FidelityLevel.HPWL,
                0.1: FidelityLevel.DRC,
            }
        )
        # Above 0.5 -> HPWL (cheapest, above all thresholds)
        assert selector.select_fidelity(0, None, 0.8) == FidelityLevel.HPWL
        # At 0.5 -> HPWL (matches threshold)
        assert selector.select_fidelity(0, None, 0.5) == FidelityLevel.HPWL
        # Between 0.1 and 0.5 -> HPWL
        assert selector.select_fidelity(0, None, 0.3) == FidelityLevel.HPWL
        # At or below 0.1 -> DRC
        assert selector.select_fidelity(0, None, 0.05) == FidelityLevel.DRC


# ---------------------------------------------------------------------------
# Factory function tests
# ---------------------------------------------------------------------------


class TestMakeFixedFidelityEvaluator:
    """Tests for make_fixed_fidelity_evaluator."""

    def test_closure_captures_parameters(self, simple_nets, simple_board):
        evaluator = make_fixed_fidelity_evaluator(
            fidelity=FidelityLevel.HPWL,
            nets=simple_nets,
            board=simple_board,
        )

        placements = [
            ComponentPlacement(reference="R1", x=10.0, y=10.0),
            ComponentPlacement(reference="R2", x=40.0, y=40.0),
        ]

        result = evaluator(placements)
        assert isinstance(result, FidelityResult)
        assert result.fidelity == FidelityLevel.HPWL
        assert result.score.total > 0

    def test_callable_returns_consistent_results(
        self, simple_placements, simple_nets, simple_board
    ):
        evaluator = make_fixed_fidelity_evaluator(
            fidelity=FidelityLevel.HPWL,
            nets=simple_nets,
            board=simple_board,
        )

        r1 = evaluator(simple_placements)
        r2 = evaluator(simple_placements)

        assert r1.score.total == r2.score.total


class TestMakeAdaptiveEvaluator:
    """Tests for make_adaptive_evaluator."""

    def test_budget_tracking(self, simple_nets, simple_board):
        selector = DefaultFidelitySelector()
        evaluator = make_adaptive_evaluator(
            selector=selector,
            nets=simple_nets,
            board=simple_board,
            total_budget=100.0,
        )

        placements = [
            ComponentPlacement(reference="R1", x=10.0, y=10.0),
            ComponentPlacement(reference="R2", x=40.0, y=40.0),
        ]

        # Early calls should use fidelity 0
        result = evaluator(placements, iteration=0)
        assert result.fidelity == FidelityLevel.HPWL

    def test_tracks_best_score(self, simple_nets, simple_board):
        selector = DefaultFidelitySelector()
        evaluator = make_adaptive_evaluator(
            selector=selector,
            nets=simple_nets,
            board=simple_board,
            total_budget=1000.0,
        )

        placements = [
            ComponentPlacement(reference="R1", x=10.0, y=10.0),
            ComponentPlacement(reference="R2", x=40.0, y=40.0),
        ]

        # Multiple calls should succeed
        r1 = evaluator(placements, iteration=0)
        r2 = evaluator(placements, iteration=1)

        # Both should return valid results
        assert r1.score.total > 0
        assert r2.score.total > 0


# ---------------------------------------------------------------------------
# RoutabilityResult tests
# ---------------------------------------------------------------------------


class TestRoutabilityResult:
    """Tests for the RoutabilityResult dataclass."""

    def test_defaults(self):
        r = RoutabilityResult()
        assert r.routed_nets == 0
        assert r.failed_nets == 0
        assert r.routability_ratio == 1.0
        assert r.congestion_score == 0.0

    def test_custom_values(self):
        r = RoutabilityResult(
            routed_nets=8,
            failed_nets=2,
            routability_ratio=0.8,
            congestion_score=0.3,
        )
        assert r.routed_nets == 8
        assert r.failed_nets == 2
        assert r.routability_ratio == 0.8


# ---------------------------------------------------------------------------
# FidelityResult tests
# ---------------------------------------------------------------------------


class TestFidelityResult:
    """Tests for the FidelityResult dataclass."""

    def test_wall_time_positive(self, simple_placements, simple_nets, simple_board):
        result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )
        assert result.wall_time_ms >= 0.0

    def test_cost_matches_fidelity(self, simple_placements, simple_nets, simple_board):
        result = evaluate_placement_multifidelity(
            placements=simple_placements,
            nets=simple_nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )
        assert result.cost == FIDELITY_COST[FidelityLevel.HPWL]


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_empty_placements_fidelity_0(self, simple_board):
        result = evaluate_placement_multifidelity(
            placements=[],
            nets=[],
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )
        assert result.score.total == 0.0
        assert result.score.is_feasible is True

    def test_invalid_fidelity_level(self, simple_placements, simple_nets, simple_board):
        with pytest.raises(ValueError):
            evaluate_placement_multifidelity(
                placements=simple_placements,
                nets=simple_nets,
                board=simple_board,
                fidelity=5,  # Invalid
            )

    def test_single_component(self, simple_board):
        placements = [ComponentPlacement(reference="R1", x=25.0, y=25.0)]
        nets = []

        result = evaluate_placement_multifidelity(
            placements=placements,
            nets=nets,
            board=simple_board,
            fidelity=FidelityLevel.HPWL,
        )

        assert result.score.is_feasible is True
        assert result.score.breakdown.overlap == 0.0
