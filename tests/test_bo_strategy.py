"""Tests for Bayesian Optimization placement strategy (Ax/BoTorch).

Tests cover:
- BayesianOptStrategy initialization and ask/tell loop
- LHS initial population generation
- Discrete variable handling (rotation, side)
- Batch suggestion (no duplicates within batch)
- Convergence detection on score plateau
- save_state / load_state round-trip
- Loaded strategy can continue optimization
- Error handling (use before init, mismatched lengths, etc.)

All tests are skipped if ax-platform/botorch are not installed.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import pytest

# Skip entire module if ax-platform is not installed
ax = pytest.importorskip("ax", reason="ax-platform not installed")

from kicad_tools.placement.bo_strategy import (  # noqa: E402
    BayesianOptStrategy,
    _latin_hypercube_sample,
)
from kicad_tools.placement.cost import BoardOutline  # noqa: E402
from kicad_tools.placement.strategy import (  # noqa: E402
    PlacementStrategy,
    StrategyConfig,
)
from kicad_tools.placement.vector import (  # noqa: E402
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
    within 3mm of each other.
    """
    n = vec.num_components
    total = 0.0
    positions: list[tuple[float, float]] = []

    for i in range(n):
        sl = vec.component_slice(i)
        x, y = float(sl[0]), float(sl[1])
        total += (x - 25.0) ** 2 + (y - 25.0) ** 2
        positions.append((x, y))

    for i in range(n):
        for j in range(i + 1, n):
            dx = positions[i][0] - positions[j][0]
            dy = positions[i][1] - positions[j][1]
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 3.0:
                total += 1000.0 * (3.0 - dist)

    return total


def _make_strategy_config(**kwargs) -> StrategyConfig:
    """Create a StrategyConfig with small initial population for fast tests."""
    defaults = {
        "seed": 42,
        "extra": {"batch_size": 4, "n_initial_factor": 2},
    }
    defaults.update(kwargs)
    return StrategyConfig(**defaults)


# ---------------------------------------------------------------------------
# Latin Hypercube Sampling Tests
# ---------------------------------------------------------------------------


class TestLatinHypercubeSampling:
    """Test the LHS helper function."""

    def test_correct_number_of_samples(self):
        """LHS produces the requested number of samples."""
        rng = np.random.default_rng(42)
        lower = np.array([0.0, 0.0])
        upper = np.array([10.0, 10.0])
        mask = np.array([False, False])
        samples = _latin_hypercube_sample(20, lower, upper, mask, rng)
        assert len(samples) == 20

    def test_samples_within_bounds(self):
        """All LHS samples are within [lower, upper]."""
        rng = np.random.default_rng(42)
        lower = np.array([1.0, 2.0, 0.0])
        upper = np.array([5.0, 8.0, 3.0])
        mask = np.array([False, False, True])
        samples = _latin_hypercube_sample(50, lower, upper, mask, rng)

        for s in samples:
            for d in range(3):
                assert s[d] >= lower[d] - 1e-9
                assert s[d] <= upper[d] + 1e-9

    def test_discrete_dimensions_are_integers(self):
        """Discrete dimensions in LHS produce integer values."""
        rng = np.random.default_rng(42)
        lower = np.array([0.0, 0.0])
        upper = np.array([10.0, 3.0])
        mask = np.array([False, True])  # second dim is discrete
        samples = _latin_hypercube_sample(30, lower, upper, mask, rng)

        for s in samples:
            assert s[1] == int(s[1]), f"Discrete dim not integer: {s[1]}"

    def test_stratification_property(self):
        """Each stratum should contain exactly one sample per dimension."""
        rng = np.random.default_rng(42)
        n = 10
        lower = np.zeros(2)
        upper = np.ones(2) * n
        mask = np.array([False, False])
        samples = _latin_hypercube_sample(n, lower, upper, mask, rng)

        # For each dimension, check that samples are spread across strata
        for d in range(2):
            values = sorted([s[d] for s in samples])
            # Each stratum [k, k+1) should have exactly one sample
            strata_counts = [0] * n
            for v in values:
                stratum = min(int(v), n - 1)
                strata_counts[stratum] += 1
            assert all(c == 1 for c in strata_counts), (
                f"Dim {d}: not all strata have exactly one sample: {strata_counts}"
            )


# ---------------------------------------------------------------------------
# Initialization Tests
# ---------------------------------------------------------------------------


class TestBayesianOptInit:
    """Test BayesianOptStrategy initialization."""

    def test_is_placement_strategy_subclass(self):
        """BayesianOptStrategy implements PlacementStrategy."""
        assert issubclass(BayesianOptStrategy, PlacementStrategy)

    def test_initialize_returns_population(self):
        """initialize() returns a list of PlacementVectors."""
        strategy = BayesianOptStrategy()
        b = _make_bounds()
        config = _make_strategy_config()
        population = strategy.initialize(b, config)

        assert isinstance(population, list)
        assert len(population) > 0
        assert all(isinstance(v, PlacementVector) for v in population)

    def test_initial_population_size(self):
        """Initial population is n_initial_factor * ndim."""
        components = _make_components(3)
        b = _make_bounds(components=components)
        ndim = len(b.lower)

        strategy = BayesianOptStrategy()
        config = StrategyConfig(seed=42, extra={"batch_size": 4, "n_initial_factor": 3})
        population = strategy.initialize(b, config)

        assert len(population) == 3 * ndim

    def test_vectors_within_bounds(self):
        """All initial vectors should be within the specified bounds."""
        strategy = BayesianOptStrategy()
        b = _make_bounds()
        config = _make_strategy_config()
        population = strategy.initialize(b, config)

        for vec in population:
            for i in range(len(vec.data)):
                assert vec.data[i] >= b.lower[i] - 1e-6, (
                    f"dim {i}: {vec.data[i]} < lower bound {b.lower[i]}"
                )
                assert vec.data[i] <= b.upper[i] + 1e-6, (
                    f"dim {i}: {vec.data[i]} > upper bound {b.upper[i]}"
                )

    def test_discrete_variables_are_integers(self):
        """Rotation and side values should be integer-valued."""
        strategy = BayesianOptStrategy()
        b = _make_bounds()
        config = _make_strategy_config()
        population = strategy.initialize(b, config)

        n_components = len(_make_components())
        for vec in population:
            for i in range(n_components):
                sl = vec.component_slice(i)
                rot = float(sl[2])
                side = float(sl[3])
                assert rot == int(rot), f"Rotation {rot} is not an integer"
                assert 0 <= rot <= 3, f"Rotation {rot} out of range [0, 3]"
                assert side == int(side), f"Side {side} is not an integer"
                assert side in (0.0, 1.0), f"Side {side} not in {{0, 1}}"


# ---------------------------------------------------------------------------
# Ask/Tell Loop Tests
# ---------------------------------------------------------------------------


class TestBayesianOptAskTell:
    """Test the ask/tell optimization loop."""

    def test_suggest_returns_correct_count(self):
        """suggest(n) returns exactly n candidates."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        candidates = strategy.suggest(4)
        assert len(candidates) == 4

    def test_observe_updates_best(self):
        """observe() tracks the best solution."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        population = strategy.initialize(b, config)

        scores = [_simple_cost(v) for v in population]
        strategy.observe(population, scores)

        best_vec, best_score = strategy.best()
        assert best_score == min(scores)
        assert any(np.array_equal(best_vec.data, v.data) for v in population)

    def test_full_ask_tell_loop(self):
        """A complete ask/tell loop runs without errors."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        for _ in range(3):
            candidates = strategy.suggest(4)
            scores = [_simple_cost(v) for v in candidates]
            strategy.observe(candidates, scores)

        best_vec, best_score = strategy.best()
        assert best_score < float("inf")
        assert isinstance(best_vec, PlacementVector)

    def test_batch_no_duplicate_suggestions(self):
        """Batch suggestions should not contain duplicate vectors."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        candidates = strategy.suggest(8)
        # Check pairwise uniqueness
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                assert not np.array_equal(candidates[i].data, candidates[j].data), (
                    f"Duplicate candidates at indices {i} and {j}"
                )


# ---------------------------------------------------------------------------
# Convergence Tests
# ---------------------------------------------------------------------------


class TestBayesianOptConvergence:
    """Test convergence detection."""

    def test_not_converged_initially(self):
        """Optimizer should not be converged right after initialization."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        strategy.initialize(b, config)
        assert not strategy.converged

    def test_convergence_on_constant_score(self):
        """Optimizer should detect convergence when score stops improving."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = StrategyConfig(
            seed=42,
            convergence_window=5,
            convergence_threshold=1e-6,
            extra={"batch_size": 4, "n_initial_factor": 2},
        )
        pop = strategy.initialize(b, config)

        # Feed constant scores to trigger convergence
        for _ in range(10):
            scores = [100.0] * len(pop)
            strategy.observe(pop, scores)
            pop = strategy.suggest(len(pop))

        assert strategy.converged, "Should detect convergence on constant scores"


# ---------------------------------------------------------------------------
# Save/Load Tests
# ---------------------------------------------------------------------------


class TestBayesianOptSaveLoad:
    """Test state serialization and round-trip."""

    def test_save_and_load_round_trip(self):
        """save_state / load_state preserves key optimizer state."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        # Run a few generations
        for _ in range(2):
            candidates = strategy.suggest(4)
            scores = [_simple_cost(v) for v in candidates]
            strategy.observe(candidates, scores)

        original_best_vec, original_best_score = strategy.best()
        original_generation = strategy._generation

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "bo_state.json"
            strategy.save_state(state_path)

            assert state_path.exists()

            loaded = BayesianOptStrategy.load_state(state_path)

        loaded_best_vec, loaded_best_score = loaded.best()
        assert loaded_best_score == original_best_score
        assert np.array_equal(loaded_best_vec.data, original_best_vec.data)
        assert loaded._generation == original_generation

    def test_load_invalid_strategy_type(self):
        """load_state raises ValueError for wrong strategy type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "bad_state.json"
            state_path.write_text('{"strategy": "cmaes"}')

            with pytest.raises(ValueError, match="Expected strategy 'bayesian_opt'"):
                BayesianOptStrategy.load_state(state_path)

    def test_load_nonexistent_file(self):
        """load_state raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            BayesianOptStrategy.load_state("/nonexistent/path/state.json")

    def test_loaded_strategy_can_continue(self):
        """A loaded strategy can continue optimization."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            strategy.save_state(state_path)
            loaded = BayesianOptStrategy.load_state(state_path)

            # Continue optimization
            candidates = loaded.suggest(4)
            scores = [_simple_cost(v) for v in candidates]
            loaded.observe(candidates, scores)

            best_vec, best_score = loaded.best()
            assert best_score < float("inf")


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestBayesianOptErrorHandling:
    """Test error cases and edge conditions."""

    def test_suggest_before_initialize(self):
        """suggest() raises RuntimeError before initialize()."""
        strategy = BayesianOptStrategy()
        with pytest.raises(RuntimeError, match="Must call initialize"):
            strategy.suggest(5)

    def test_observe_before_initialize(self):
        """observe() raises RuntimeError before initialize()."""
        strategy = BayesianOptStrategy()
        with pytest.raises(RuntimeError, match="Must call initialize"):
            strategy.observe([], [])

    def test_best_before_observe(self):
        """best() raises RuntimeError before any observation."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        strategy.initialize(b, _make_strategy_config())
        with pytest.raises(RuntimeError, match="No observations yet"):
            strategy.best()

    def test_observe_mismatched_lengths(self):
        """observe() raises ValueError when lengths don't match."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        pop = strategy.initialize(b, config)

        with pytest.raises(ValueError, match="placements but.*scores"):
            strategy.observe(pop, [1.0, 2.0])

    def test_observe_without_matching_suggest(self):
        """observe() raises ValueError when placements don't match pending."""
        strategy = BayesianOptStrategy()
        b = _make_bounds(components=_make_components(3))
        config = _make_strategy_config()
        pop = strategy.initialize(b, config)
        scores = [_simple_cost(v) for v in pop]
        strategy.observe(pop, scores)

        # Now suggest 4 but try to observe 2
        strategy.suggest(4)
        short_pop = pop[:2]
        short_scores = [1.0, 2.0]
        with pytest.raises(ValueError, match="pending from last suggest"):
            strategy.observe(short_pop, short_scores)

    def test_save_before_initialize(self):
        """save_state() raises RuntimeError before initialize()."""
        strategy = BayesianOptStrategy()
        with pytest.raises(RuntimeError, match="Must call initialize"):
            strategy.save_state("/tmp/unused.json")
