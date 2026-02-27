"""Tests for PlacementStrategy ABC and CMA-ES optimizer implementation.

Tests cover:
- PlacementStrategy ABC cannot be instantiated
- CMAESStrategy initialization and ask/tell loop
- Discrete variable handling (rotation, side)
- Optimizer improves score over random placement
- Convergence detection on score plateau
- save_state / load_state round-trip
- Error handling (use before init, mismatched lengths, etc.)
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.placement.cmaes_strategy import CMAESStrategy, _auto_population_size
from kicad_tools.placement.cost import BoardOutline
from kicad_tools.placement.strategy import PlacementStrategy, StrategyConfig
from kicad_tools.placement.vector import (
    ComponentDef,
    PlacementBounds,
    PlacementVector,
    bounds,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_board() -> BoardOutline:
    """Create a 50x50mm board centered at (25, 25)."""
    return BoardOutline(min_x=0.0, min_y=0.0, max_x=50.0, max_y=50.0)


def _make_components(n: int = 5) -> list[ComponentDef]:
    """Create n simple 2x2mm components."""
    return [ComponentDef(reference=f"U{i + 1}", width=2.0, height=2.0) for i in range(n)]


def _make_bounds(
    board: BoardOutline | None = None,
    components: list[ComponentDef] | None = None,
) -> PlacementBounds:
    """Build placement bounds for the given board and components."""
    if board is None:
        board = _make_board()
    if components is None:
        components = _make_components()
    return bounds(board, components)


def _simple_cost(vec: PlacementVector) -> float:
    """Trivial cost: sum of squared distances from board center (25, 25).

    Penalizes overlap by adding a large cost when any two components are
    within 3mm of each other. This gives the optimizer a clear gradient
    toward spreading components while centering them.
    """
    n = vec.num_components
    total = 0.0
    positions: list[tuple[float, float]] = []

    for i in range(n):
        sl = vec.component_slice(i)
        x, y = float(sl[0]), float(sl[1])
        # Distance from center
        total += (x - 25.0) ** 2 + (y - 25.0) ** 2
        positions.append((x, y))

    # Overlap penalty
    for i in range(n):
        for j in range(i + 1, n):
            dx = positions[i][0] - positions[j][0]
            dy = positions[i][1] - positions[j][1]
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 3.0:
                total += 1000.0 * (3.0 - dist)

    return total


# ---------------------------------------------------------------------------
# ABC Tests
# ---------------------------------------------------------------------------


class TestPlacementStrategyABC:
    """Test that PlacementStrategy ABC enforces the interface."""

    def test_cannot_instantiate_abc(self):
        """PlacementStrategy is abstract and cannot be instantiated."""
        with pytest.raises(TypeError):
            PlacementStrategy()  # type: ignore[abstract]

    def test_subclass_must_implement_all_methods(self):
        """A subclass that does not implement all abstract methods cannot
        be instantiated."""

        class IncompleteStrategy(PlacementStrategy):
            def initialize(self, bounds, config):
                return []

            # Missing: suggest, observe, best, converged, save_state, load_state

        with pytest.raises(TypeError):
            IncompleteStrategy()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# CMAESStrategy Tests
# ---------------------------------------------------------------------------


class TestCMAESStrategyInit:
    """Test CMAESStrategy initialization."""

    def test_initialize_returns_population(self):
        """initialize() returns a list of PlacementVectors."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42)
        population = strategy.initialize(b, config)

        assert isinstance(population, list)
        assert len(population) > 0
        assert all(isinstance(v, PlacementVector) for v in population)

    def test_population_size_auto_scaled(self):
        """Default population size follows 4 + floor(3 * ln(n))."""
        components = _make_components(5)
        b = _make_bounds(components=components)
        ndim = len(b.lower)

        strategy = CMAESStrategy()
        population = strategy.initialize(b, StrategyConfig(seed=42))

        expected = _auto_population_size(ndim)
        assert len(population) == expected

    def test_population_size_user_override(self):
        """User can override population size via extra config."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 10})
        population = strategy.initialize(b, config)

        assert len(population) == 10

    def test_vectors_within_bounds(self):
        """All initial vectors should be within the specified bounds."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42)
        population = strategy.initialize(b, config)

        for vec in population:
            # Continuous dimensions should be within bounds
            for i in range(len(vec.data)):
                assert vec.data[i] >= b.lower[i] - 1e-6, (
                    f"dim {i}: {vec.data[i]} < lower bound {b.lower[i]}"
                )
                assert vec.data[i] <= b.upper[i] + 1e-6, (
                    f"dim {i}: {vec.data[i]} > upper bound {b.upper[i]}"
                )

    def test_discrete_variables_are_integers(self):
        """Rotation and side values should be integer-valued."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42)
        population = strategy.initialize(b, config)

        n_components = len(_make_components())
        for vec in population:
            for i in range(n_components):
                sl = vec.component_slice(i)
                rot = float(sl[2])
                side = float(sl[3])
                # Rotation should be an integer 0-3
                assert rot == int(rot), f"Rotation {rot} is not an integer"
                assert 0 <= rot <= 3, f"Rotation {rot} out of range [0, 3]"
                # Side should be 0 or 1
                assert side == int(side), f"Side {side} is not an integer"
                assert side in (0.0, 1.0), f"Side {side} not in {{0, 1}}"


class TestCMAESStrategyAskTell:
    """Test the ask/tell optimization loop."""

    def test_suggest_returns_correct_count(self):
        """suggest(n) returns exactly n candidates."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 8})
        strategy.initialize(b, config)

        candidates = strategy.suggest(8)
        assert len(candidates) == 8

    def test_observe_updates_best(self):
        """observe() tracks the best solution."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 6})
        population = strategy.initialize(b, config)

        scores = [_simple_cost(v) for v in population]
        strategy.observe(population, scores)

        best_vec, best_score = strategy.best()
        assert best_score == min(scores)
        # The best vector should be one of the population members
        assert any(np.array_equal(best_vec.data, v.data) for v in population)

    def test_full_ask_tell_loop(self):
        """A complete ask/tell loop runs without errors."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, max_iterations=10, extra={"population_size": 6})
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        for _ in range(5):
            candidates = strategy.suggest(6)
            scores = [_simple_cost(v) for v in candidates]
            strategy.observe(candidates, scores)

        best_vec, best_score = strategy.best()
        assert best_score < float("inf")
        assert isinstance(best_vec, PlacementVector)

    def test_optimizer_improves_over_generations(self):
        """Score should improve (decrease) over multiple generations."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 10})
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        initial_best = strategy.best()[1]

        # Run 30 generations
        for _ in range(30):
            candidates = strategy.suggest(10)
            scores = [_simple_cost(v) for v in candidates]
            strategy.observe(candidates, scores)

        final_best = strategy.best()[1]
        assert final_best <= initial_best, (
            f"Score did not improve: initial={initial_best}, final={final_best}"
        )


class TestCMAESConvergence:
    """Test convergence detection."""

    def test_not_converged_initially(self):
        """Optimizer should not be converged right after initialization."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42)
        strategy.initialize(b, config)
        assert not strategy.converged

    def test_convergence_on_constant_score(self):
        """Optimizer should detect convergence when score stops improving."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(
            seed=42,
            convergence_window=5,
            convergence_threshold=1e-6,
            extra={"population_size": 6},
        )
        pop = strategy.initialize(b, config)

        # Feed constant scores to trigger convergence
        for _ in range(10):
            scores = [100.0] * len(pop)
            strategy.observe(pop, scores)
            # Re-suggest to keep the loop going
            pop = strategy.suggest(6)

        assert strategy.converged, "Should detect convergence on constant scores"


class TestCMAESSaveLoad:
    """Test state serialization and round-trip."""

    def test_save_and_load_round_trip(self):
        """save_state / load_state preserves key optimizer state."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 6})
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        # Run a few generations
        for _ in range(3):
            candidates = strategy.suggest(6)
            scores = [_simple_cost(v) for v in candidates]
            strategy.observe(candidates, scores)

        original_best_vec, original_best_score = strategy.best()
        original_generation = strategy._generation

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "cmaes_state.json"
            strategy.save_state(state_path)

            assert state_path.exists()

            loaded = CMAESStrategy.load_state(state_path)

        # Verify restored state
        loaded_best_vec, loaded_best_score = loaded.best()
        assert loaded_best_score == original_best_score
        assert np.array_equal(loaded_best_vec.data, original_best_vec.data)
        assert loaded._generation == original_generation

    def test_load_invalid_strategy_type(self):
        """load_state raises ValueError for wrong strategy type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "bad_state.json"
            state_path.write_text('{"strategy": "simulated_annealing"}')

            with pytest.raises(ValueError, match="Expected strategy 'cmaes'"):
                CMAESStrategy.load_state(state_path)

    def test_load_nonexistent_file(self):
        """load_state raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            CMAESStrategy.load_state("/nonexistent/path/state.json")

    def test_loaded_strategy_can_continue(self):
        """A loaded strategy can continue optimization."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 6})
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            strategy.save_state(state_path)
            loaded = CMAESStrategy.load_state(state_path)

            # Continue optimization with loaded strategy
            candidates = loaded.suggest(6)
            scores = [_simple_cost(v) for v in candidates]
            loaded.observe(candidates, scores)

            best_vec, best_score = loaded.best()
            assert best_score < float("inf")


class TestCMAESErrorHandling:
    """Test error cases and edge conditions."""

    def test_suggest_before_initialize(self):
        """suggest() raises RuntimeError before initialize()."""
        strategy = CMAESStrategy()
        with pytest.raises(RuntimeError, match="Must call initialize"):
            strategy.suggest(5)

    def test_observe_before_initialize(self):
        """observe() raises RuntimeError before initialize()."""
        strategy = CMAESStrategy()
        with pytest.raises(RuntimeError, match="Must call initialize"):
            strategy.observe([], [])

    def test_best_before_observe(self):
        """best() raises RuntimeError before any observation."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        strategy.initialize(b, StrategyConfig(seed=42))
        with pytest.raises(RuntimeError, match="No observations yet"):
            strategy.best()

    def test_observe_mismatched_lengths(self):
        """observe() raises ValueError when lengths don't match."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 6})
        pop = strategy.initialize(b, config)

        with pytest.raises(ValueError, match="placements but.*scores"):
            strategy.observe(pop, [1.0, 2.0])  # Wrong number of scores

    def test_observe_without_matching_suggest(self):
        """observe() raises ValueError when placements don't match pending."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(seed=42, extra={"population_size": 6})
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        # Now suggest 6 but try to observe 3
        strategy.suggest(6)
        short_pop = pop[:3]
        short_scores = [1.0, 2.0, 3.0]
        with pytest.raises(ValueError, match="pending from last suggest"):
            strategy.observe(short_pop, short_scores)

    def test_save_before_initialize(self):
        """save_state() raises RuntimeError before initialize()."""
        strategy = CMAESStrategy()
        with pytest.raises(RuntimeError, match="Must call initialize"):
            strategy.save_state("/tmp/unused.json")


class TestAutoPopulationSize:
    """Test the auto-scaling population size formula."""

    def test_minimum_population(self):
        """Population size should be at least 4."""
        assert _auto_population_size(1) >= 4
        assert _auto_population_size(0) >= 4

    def test_scales_with_dimensionality(self):
        """Population size should increase with dimensionality."""
        small = _auto_population_size(4)
        medium = _auto_population_size(20)
        large = _auto_population_size(100)

        assert small <= medium <= large

    def test_formula_matches(self):
        """Verify the formula: 4 + floor(3 * ln(n))."""
        for ndim in [5, 10, 20, 50, 100]:
            expected = 4 + int(math.floor(3 * math.log(ndim)))
            assert _auto_population_size(ndim) == expected
