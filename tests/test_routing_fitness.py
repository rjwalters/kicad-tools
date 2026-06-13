"""Tests for routing-based fitness evaluation in the evolutionary optimizer.

Verifies that the EvolutionaryPlacementOptimizer can use a RoutingEvaluator
to replace the spacing-based routability proxy with actual routing completion
rate, and that the spacing proxy remains as the default fallback.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kicad_tools.optim import Component, Pin, Polygon, Spring
from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
    Individual,
    RoutingEvaluator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_optimizer(
    routing_evaluator=None,
    config=None,
):
    """Create a small optimizer with two components and one spring."""
    board = Polygon.rectangle(50, 50, 100, 80)
    config = config or EvolutionaryConfig()
    opt = EvolutionaryPlacementOptimizer(board, config, routing_evaluator=routing_evaluator)

    comp1 = Component(
        ref="U1",
        x=30.0,
        y=40.0,
        width=5.0,
        height=5.0,
        pins=[Pin(number="1", x=30.0, y=40.0, net=1, net_name="NET1")],
    )
    comp2 = Component(
        ref="R1",
        x=70.0,
        y=60.0,
        width=3.0,
        height=2.0,
        pins=[Pin(number="1", x=70.0, y=60.0, net=1, net_name="NET1")],
    )
    opt.add_component(comp1)
    opt.add_component(comp2)

    spring = Spring(
        comp1_ref="U1",
        pin1_num="1",
        comp2_ref="R1",
        pin2_num="1",
        stiffness=1.0,
        net=1,
        net_name="NET1",
    )
    opt.add_spring(spring)

    return opt


class _MockRoutingEvaluator:
    """Simple mock that records calls and returns a fixed completion rate."""

    def __init__(self, completion_rate: float = 0.85):
        self.completion_rate = completion_rate
        self.calls: list[tuple[dict, dict]] = []

    def evaluate_routability(
        self,
        positions: dict[str, tuple[float, float]],
        rotations: dict[str, float],
    ) -> float:
        self.calls.append((dict(positions), dict(rotations)))
        return self.completion_rate


# ---------------------------------------------------------------------------
# Tests: RoutingEvaluator integration
# ---------------------------------------------------------------------------


class TestRoutingEvaluatorFallback:
    """Verify spacing proxy is used when no routing evaluator is provided."""

    def test_default_uses_spacing_proxy(self):
        opt = _make_optimizer(routing_evaluator=None)
        score = opt._estimate_routability()
        # The spacing proxy returns avg_spacing * 5.0 clamped to [0, 100].
        # Two components at (30,40) and (70,60): dist ~ 44.7, * 5 = 223 -> clamped to 100
        assert score == pytest.approx(100.0)

    def test_spacing_proxy_with_close_components(self):
        opt = _make_optimizer(routing_evaluator=None)
        # Move components very close together
        opt.components[0].x = 50.0
        opt.components[0].y = 50.0
        opt.components[1].x = 52.0
        opt.components[1].y = 50.0
        score = opt._estimate_routability()
        # dist = 2.0, * 5.0 = 10.0
        assert score == pytest.approx(10.0)


class TestRoutingEvaluatorUsed:
    """Verify routing evaluator is called when configured."""

    def test_evaluator_called_during_estimate(self):
        evaluator = _MockRoutingEvaluator(completion_rate=0.75)
        opt = _make_optimizer(routing_evaluator=evaluator)
        score = opt._estimate_routability()
        assert len(evaluator.calls) == 1
        assert score == pytest.approx(75.0)  # 0.75 * 100

    def test_evaluator_receives_current_positions(self):
        evaluator = _MockRoutingEvaluator(completion_rate=1.0)
        opt = _make_optimizer(routing_evaluator=evaluator)

        # Move components to known positions
        opt.components[0].x = 25.0
        opt.components[0].y = 35.0
        opt.components[1].x = 65.0
        opt.components[1].y = 55.0

        opt._estimate_routability()
        positions, rotations = evaluator.calls[0]
        assert positions["U1"] == (25.0, 35.0)
        assert positions["R1"] == (65.0, 55.0)

    def test_evaluator_zero_completion(self):
        evaluator = _MockRoutingEvaluator(completion_rate=0.0)
        opt = _make_optimizer(routing_evaluator=evaluator)
        score = opt._estimate_routability()
        assert score == pytest.approx(0.0)

    def test_evaluator_full_completion(self):
        evaluator = _MockRoutingEvaluator(completion_rate=1.0)
        opt = _make_optimizer(routing_evaluator=evaluator)
        score = opt._estimate_routability()
        assert score == pytest.approx(100.0)

    def test_evaluator_clamped_above_one(self):
        evaluator = _MockRoutingEvaluator(completion_rate=1.5)
        opt = _make_optimizer(routing_evaluator=evaluator)
        score = opt._estimate_routability()
        assert score == pytest.approx(100.0)

    def test_evaluator_clamped_below_zero(self):
        evaluator = _MockRoutingEvaluator(completion_rate=-0.1)
        opt = _make_optimizer(routing_evaluator=evaluator)
        score = opt._estimate_routability()
        assert score == pytest.approx(0.0)


class TestRoutingEvaluatorFitnessIntegration:
    """Verify fitness function correctly incorporates routing completion."""

    def test_fitness_uses_routing_evaluator(self):
        evaluator = _MockRoutingEvaluator(completion_rate=0.5)
        opt = _make_optimizer(routing_evaluator=evaluator)
        ind = opt._individual_from_current()
        fitness = opt._evaluate_fitness(ind)
        # Evaluator should have been called
        assert len(evaluator.calls) >= 1
        assert fitness != 0.0

    def test_higher_completion_yields_higher_fitness(self):
        """Better routing completion should produce higher fitness."""
        config = EvolutionaryConfig(
            routability_weight=50.0,
            wire_length_weight=0.0,  # Disable wire length to isolate routability
        )

        eval_low = _MockRoutingEvaluator(completion_rate=0.2)
        opt_low = _make_optimizer(routing_evaluator=eval_low, config=config)
        ind = opt_low._individual_from_current()
        fitness_low = opt_low._evaluate_fitness(ind)

        eval_high = _MockRoutingEvaluator(completion_rate=0.9)
        opt_high = _make_optimizer(routing_evaluator=eval_high, config=config)
        ind = opt_high._individual_from_current()
        fitness_high = opt_high._evaluate_fitness(ind)

        assert fitness_high > fitness_low

    def test_fitness_restores_positions_after_evaluation(self):
        """Verify components return to original positions after evaluation."""
        evaluator = _MockRoutingEvaluator(completion_rate=0.5)
        opt = _make_optimizer(routing_evaluator=evaluator)
        original_x = opt.components[0].x
        original_y = opt.components[0].y

        ind = Individual(
            positions={"U1": (99.0, 99.0), "R1": (60.0, 60.0)},
            rotations={"U1": 90.0, "R1": 0.0},
        )
        opt._evaluate_fitness(ind)

        assert opt.components[0].x == original_x
        assert opt.components[0].y == original_y


class TestRoutingEvaluatorErrorHandling:
    """Verify graceful fallback when routing evaluator raises."""

    def test_evaluator_exception_falls_back_to_spacing(self):
        evaluator = MagicMock(spec=RoutingEvaluator)
        evaluator.evaluate_routability.side_effect = RuntimeError("routing failed")
        opt = _make_optimizer(routing_evaluator=evaluator)

        # Should not raise -- falls back to spacing proxy
        score = opt._estimate_routability()
        # The spacing proxy returns a score (we just check it's reasonable)
        assert 0.0 <= score <= 100.0
        assert evaluator.evaluate_routability.called


class TestPopulationEvaluationWithRouting:
    """Verify population evaluation respects routing evaluator."""

    def test_sequential_evaluation_uses_routing(self):
        evaluator = _MockRoutingEvaluator(completion_rate=0.6)
        config = EvolutionaryConfig(parallel=False)
        opt = _make_optimizer(routing_evaluator=evaluator, config=config)
        population = opt._initialize_population(5)
        opt._evaluate_population(population)

        # Each individual should have been evaluated
        assert all(ind.fitness != 0.0 for ind in population)
        # The evaluator should have been called once per individual
        assert len(evaluator.calls) == 5

    def test_parallel_evaluation_uses_threads(self):
        """When routing evaluator is active, parallel uses ThreadPoolExecutor."""
        evaluator = _MockRoutingEvaluator(completion_rate=0.8)
        config = EvolutionaryConfig(parallel=True, max_workers=2)
        opt = _make_optimizer(routing_evaluator=evaluator, config=config)
        population = opt._initialize_population(10)
        opt._evaluate_population(population)

        # All individuals should be evaluated
        assert all(ind.fitness != 0.0 for ind in population)
        # The evaluator should have been called for each individual
        assert len(evaluator.calls) == 10


class TestOptimizeWithRouting:
    """Integration: full optimization with routing evaluator."""

    def test_optimize_with_routing_evaluator(self):
        evaluator = _MockRoutingEvaluator(completion_rate=0.9)
        config = EvolutionaryConfig(
            population_size=8,
            generations=3,
            parallel=False,
        )
        opt = _make_optimizer(routing_evaluator=evaluator, config=config)
        best = opt.optimize(generations=3, population_size=8)
        assert isinstance(best, Individual)
        assert best.fitness != 0.0
        # Evaluator should have been called many times
        assert len(evaluator.calls) > 0


class TestEdgeCases:
    """Edge cases for routing evaluator."""

    def test_zero_components(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        evaluator = _MockRoutingEvaluator(completion_rate=0.5)
        opt = EvolutionaryPlacementOptimizer(board, routing_evaluator=evaluator)
        score = opt._estimate_routability()
        # 0 components => 100.0 (short-circuit before evaluator)
        assert score == pytest.approx(100.0)
        assert len(evaluator.calls) == 0

    def test_one_component(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        evaluator = _MockRoutingEvaluator(completion_rate=0.5)
        opt = EvolutionaryPlacementOptimizer(board, routing_evaluator=evaluator)
        comp = Component(ref="U1", x=50.0, y=50.0, width=5.0, height=5.0)
        opt.add_component(comp)
        score = opt._estimate_routability()
        # 1 component => 100.0 (short-circuit before evaluator)
        assert score == pytest.approx(100.0)
        assert len(evaluator.calls) == 0


class TestBackwardCompatibility:
    """Ensure existing tests pass without modification when no evaluator."""

    def test_existing_fitness_unchanged(self):
        """Without routing evaluator, fitness should match original behavior."""
        opt = _make_optimizer(routing_evaluator=None)
        ind = opt._individual_from_current()
        fitness = opt._evaluate_fitness(ind)
        # Just verify it runs and produces a reasonable value
        assert isinstance(fitness, float)
        assert fitness != 0.0

    def test_factory_methods_accept_routing_evaluator(self):
        """Verify from_placement_optimizer accepts routing_evaluator."""
        from kicad_tools.optim import PlacementOptimizer

        board = Polygon.rectangle(50, 50, 100, 80)
        physics_opt = PlacementOptimizer(board)
        comp = Component(ref="U1", x=50.0, y=50.0, width=5.0, height=5.0)
        physics_opt.add_component(comp)

        evaluator = _MockRoutingEvaluator(completion_rate=0.7)
        evo_opt = EvolutionaryPlacementOptimizer.from_placement_optimizer(
            physics_opt, routing_evaluator=evaluator
        )
        assert evo_opt.routing_evaluator is evaluator
