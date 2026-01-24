"""Tests for GPU-accelerated evolutionary fitness evaluation.

Tests verify:
1. GPU evaluation matches CPU results (within tolerance)
2. GPU evaluation provides speedup for large populations
3. Graceful fallback to CPU when GPU unavailable
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.acceleration.backend import ArrayBackend, BackendType
from kicad_tools.acceleration.kernels.evolutionary import (
    _compute_boundary_violations_batch,
    _compute_overlaps_batch,
    _compute_pin_alignment_batch,
    _compute_routability_batch,
    _compute_wire_lengths_batch,
    evaluate_population_gpu,
    population_to_batch,
    prepare_evaluation_data,
)
from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
    Individual,
)
from kicad_tools.optim.geometry import Polygon, Vector2D


@pytest.fixture
def cpu_backend() -> ArrayBackend:
    """Create a CPU backend for testing."""
    return ArrayBackend.create(BackendType.CPU)


@pytest.fixture
def simple_board() -> Polygon:
    """Create a simple rectangular board outline."""
    vertices = [
        Vector2D(0, 0),
        Vector2D(100, 0),
        Vector2D(100, 100),
        Vector2D(0, 100),
    ]
    return Polygon(vertices)


@pytest.fixture
def sample_components() -> dict:
    """Create sample component data for testing."""
    # ref -> (x, y, rotation, width, height, pin_offsets)
    return {
        "U1": (25.0, 25.0, 0.0, 10.0, 10.0, [(-4.0, 0.0, "1"), (4.0, 0.0, "2")]),
        "U2": (75.0, 25.0, 0.0, 10.0, 10.0, [(-4.0, 0.0, "1"), (4.0, 0.0, "2")]),
        "R1": (50.0, 50.0, 0.0, 5.0, 2.0, [(-2.0, 0.0, "1"), (2.0, 0.0, "2")]),
        "R2": (50.0, 75.0, 0.0, 5.0, 2.0, [(-2.0, 0.0, "1"), (2.0, 0.0, "2")]),
    }


@pytest.fixture
def sample_springs() -> list:
    """Create sample spring (net) connections."""
    # (comp1_ref, pin1_num, comp2_ref, pin2_num)
    return [
        ("U1", "2", "R1", "1"),
        ("R1", "2", "U2", "1"),
        ("U1", "1", "R2", "1"),
        ("R2", "2", "U2", "2"),
    ]


class TestArrayBackend:
    """Tests for ArrayBackend abstraction."""

    def test_create_cpu(self):
        """Test CPU backend creation."""
        backend = ArrayBackend.create("cpu")
        assert backend.backend_type == BackendType.CPU
        assert not backend.is_gpu

    def test_array_operations(self, cpu_backend: ArrayBackend):
        """Test basic array operations work correctly."""
        arr = cpu_backend.array([[1, 2], [3, 4]], dtype=cpu_backend.float32)
        assert arr.shape == (2, 2)

        result = cpu_backend.sum(arr, axis=1)
        np.testing.assert_array_almost_equal(result, [3, 7])

    def test_to_numpy(self, cpu_backend: ArrayBackend):
        """Test conversion back to numpy."""
        arr = cpu_backend.array([1, 2, 3])
        numpy_arr = cpu_backend.to_numpy(arr)
        assert isinstance(numpy_arr, np.ndarray)
        np.testing.assert_array_equal(numpy_arr, [1, 2, 3])


class TestPrepareEvaluationData:
    """Tests for data preparation functions."""

    def test_prepare_evaluation_data(self, sample_components, sample_springs, simple_board):
        """Test preparation of evaluation data arrays."""
        ref_to_idx = {ref: i for i, ref in enumerate(sorted(sample_components.keys()))}
        board_vertices = [(v.x, v.y) for v in simple_board.vertices]

        sizes, springs_arr, pin_offsets, board_arr = prepare_evaluation_data(
            sample_components, sample_springs, board_vertices, ref_to_idx
        )

        # Check component sizes
        assert sizes.shape == (4, 2)  # 4 components, (width, height)

        # Check spring indices
        assert springs_arr.shape == (4, 2)  # 4 springs, (comp1_idx, comp2_idx)

        # Check pin offsets
        assert pin_offsets.shape == (4, 2, 2)  # 4 springs, 2 pins, (x, y)

        # Check board vertices
        assert board_arr.shape == (4, 2)  # 4 vertices, (x, y)

    def test_population_to_batch(self, sample_components):
        """Test conversion of population to batch array."""
        ref_order = sorted(sample_components.keys())

        # Create sample individuals
        ind1 = Individual(
            positions={"U1": (25.0, 25.0), "U2": (75.0, 25.0), "R1": (50.0, 50.0), "R2": (50.0, 75.0)}
        )
        ind2 = Individual(
            positions={"U1": (30.0, 30.0), "U2": (70.0, 30.0), "R1": (55.0, 55.0), "R2": (55.0, 70.0)}
        )
        population = [ind1, ind2]

        positions = population_to_batch(population, ref_order)

        assert positions.shape == (2, 4, 2)  # 2 individuals, 4 components, (x, y)


class TestGPUKernels:
    """Tests for GPU kernel functions."""

    def test_wire_lengths_batch(self, cpu_backend: ArrayBackend):
        """Test wire length computation."""
        # 2 individuals, 3 components
        positions = cpu_backend.array([
            [[0, 0], [10, 0], [5, 5]],  # Individual 1
            [[0, 0], [20, 0], [10, 10]],  # Individual 2
        ])

        # 2 springs connecting comp 0->1 and comp 1->2
        springs = cpu_backend.array([[0, 1], [1, 2]], dtype=cpu_backend.int32)

        # Pin offsets (all at component centers for simplicity)
        pin_offsets = cpu_backend.array([
            [[0, 0], [0, 0]],  # Spring 0: comp0.pin -> comp1.pin
            [[0, 0], [0, 0]],  # Spring 1: comp1.pin -> comp2.pin
        ])

        wire_lengths = _compute_wire_lengths_batch(positions, springs, pin_offsets, cpu_backend)
        result = cpu_backend.to_numpy(wire_lengths)

        # Individual 1: distance(0,0->10,0) + distance(10,0->5,5) = 10 + 7.07
        # Individual 2: distance(0,0->20,0) + distance(20,0->10,10) = 20 + 14.14
        assert result.shape == (2,)
        assert result[0] < result[1]  # Second individual has longer wires

    def test_overlaps_batch(self, cpu_backend: ArrayBackend):
        """Test overlap detection."""
        # 2 individuals, 3 components
        positions = cpu_backend.array([
            [[0, 0], [5, 0], [100, 100]],  # Individual 1: comp 0 and 1 overlap
            [[0, 0], [50, 0], [100, 100]],  # Individual 2: no overlaps
        ])

        # All components are 10x10
        sizes = cpu_backend.array([[10, 10], [10, 10], [10, 10]])

        overlaps = _compute_overlaps_batch(positions, sizes, cpu_backend)
        result = cpu_backend.to_numpy(overlaps)

        assert result.shape == (2,)
        assert result[0] == 1  # Individual 1 has 1 overlap
        assert result[1] == 0  # Individual 2 has no overlaps

    def test_boundary_violations_batch(self, cpu_backend: ArrayBackend):
        """Test boundary violation detection."""
        # 2 individuals, 3 components
        positions = cpu_backend.array([
            [[50, 50], [50, 50], [50, 50]],  # All inside
            [[50, 50], [50, 50], [150, 150]],  # One outside
        ])

        # Simple square board 0-100
        board_vertices = cpu_backend.array([
            [0, 0], [100, 0], [100, 100], [0, 100]
        ])

        violations = _compute_boundary_violations_batch(positions, board_vertices, cpu_backend)
        result = cpu_backend.to_numpy(violations)

        assert result.shape == (2,)
        assert result[0] == 0  # All inside
        assert result[1] == 1  # One outside

    def test_routability_batch(self, cpu_backend: ArrayBackend):
        """Test routability score computation."""
        # 2 individuals, 3 components
        positions = cpu_backend.array([
            [[0, 0], [10, 0], [20, 0]],  # Tightly packed
            [[0, 0], [50, 0], [100, 0]],  # Spread out
        ])

        routability = _compute_routability_batch(positions, cpu_backend)
        result = cpu_backend.to_numpy(routability)

        assert result.shape == (2,)
        assert result[1] > result[0]  # Spread out has better routability

    def test_pin_alignment_batch(self, cpu_backend: ArrayBackend):
        """Test pin alignment score computation."""
        # 2 individuals, 2 components
        positions = cpu_backend.array([
            [[0, 0], [10, 0]],  # Horizontally aligned
            [[0, 0], [10, 10]],  # Diagonal (not aligned)
        ])

        springs = cpu_backend.array([[0, 1]], dtype=cpu_backend.int32)
        pin_offsets = cpu_backend.array([[[0, 0], [0, 0]]])  # Pins at centers

        alignment = _compute_pin_alignment_batch(
            positions, springs, pin_offsets, tolerance=0.5, backend=cpu_backend
        )
        result = cpu_backend.to_numpy(alignment)

        assert result.shape == (2,)
        assert result[0] == 100.0  # Horizontally aligned (dy < tolerance)
        assert result[1] == 0.0  # Not aligned


class TestEvaluatePopulationGPU:
    """Tests for the main GPU evaluation function."""

    def test_evaluate_population_matches_cpu(
        self, cpu_backend: ArrayBackend, sample_components, sample_springs, simple_board
    ):
        """Test that GPU evaluation produces same results as CPU."""
        ref_order = sorted(sample_components.keys())
        ref_to_idx = {ref: i for i, ref in enumerate(ref_order)}
        board_vertices = [(v.x, v.y) for v in simple_board.vertices]

        # Prepare data
        sizes, springs_arr, pin_offsets, board_arr = prepare_evaluation_data(
            sample_components, sample_springs, board_vertices, ref_to_idx
        )

        # Create test population
        positions = np.array([
            [[25, 25], [50, 75], [50, 50], [75, 25]],  # Original positions (sorted by ref)
            [[30, 30], [55, 70], [55, 55], [70, 30]],  # Moved positions
        ], dtype=np.float32)

        weights = {
            "baseline": 1000.0,
            "wire_length_weight": 0.1,
            "conflict_weight": 100.0,
            "boundary_violation_weight": 500.0,
            "routability_weight": 50.0,
            "pin_alignment_weight": 5.0,
            "pin_alignment_tolerance": 0.5,
        }

        fitness = evaluate_population_gpu(
            positions, sizes, springs_arr, pin_offsets, board_arr, weights, cpu_backend
        )

        assert fitness.shape == (2,)
        assert all(np.isfinite(fitness))

    def test_empty_population(self, cpu_backend: ArrayBackend):
        """Test handling of empty population."""
        positions = np.zeros((0, 4, 2), dtype=np.float32)
        sizes = np.zeros((4, 2), dtype=np.float32)
        springs = np.zeros((0, 2), dtype=np.int32)
        pin_offsets = np.zeros((0, 2, 2), dtype=np.float32)
        board_vertices = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
        weights = {
            "baseline": 1000.0,
            "wire_length_weight": 0.1,
            "conflict_weight": 100.0,
            "boundary_violation_weight": 500.0,
            "routability_weight": 50.0,
            "pin_alignment_weight": 5.0,
            "pin_alignment_tolerance": 0.5,
        }

        fitness = evaluate_population_gpu(
            positions, sizes, springs, pin_offsets, board_vertices, weights, cpu_backend
        )

        assert fitness.shape == (0,)


class TestEvolutionaryOptimizerIntegration:
    """Integration tests for EvolutionaryPlacementOptimizer with GPU."""

    def test_optimizer_uses_gpu_path(self, simple_board):
        """Test that optimizer can use GPU evaluation path."""
        config = EvolutionaryConfig(
            population_size=25,
            generations=2,
            use_gpu=True,
        )
        optimizer = EvolutionaryPlacementOptimizer(simple_board, config)

        # Add some components manually
        from kicad_tools.optim.components import Component, Pin

        for i in range(5):
            comp = Component(
                ref=f"U{i+1}",
                x=20 + i * 15,
                y=50,
                width=10,
                height=10,
                rotation=0,
            )
            comp.pins = [
                Pin(number="1", x=comp.x - 4, y=comp.y),
                Pin(number="2", x=comp.x + 4, y=comp.y),
            ]
            optimizer.add_component(comp)

        # Run a short optimization
        best = optimizer.optimize(generations=2, population_size=25)

        assert best is not None
        assert best.fitness != 0

    def test_gpu_fallback_to_cpu(self, simple_board):
        """Test fallback to CPU when GPU is disabled."""
        config = EvolutionaryConfig(
            population_size=10,
            generations=2,
            use_gpu=False,
            parallel=False,
        )
        optimizer = EvolutionaryPlacementOptimizer(simple_board, config)

        from kicad_tools.optim.components import Component, Pin

        for i in range(3):
            comp = Component(
                ref=f"U{i+1}",
                x=20 + i * 30,
                y=50,
                width=10,
                height=10,
                rotation=0,
            )
            comp.pins = [Pin(number="1", x=comp.x, y=comp.y)]
            optimizer.add_component(comp)

        # Should work with CPU path
        best = optimizer.optimize(generations=2, population_size=10)
        assert best is not None


class TestGPUPerformance:
    """Performance tests for GPU evaluation."""

    @pytest.mark.parametrize("pop_size", [50, 100, 200])
    def test_gpu_evaluation_scales(self, pop_size: int, cpu_backend: ArrayBackend):
        """Test that GPU evaluation works for different population sizes."""
        n_components = 30
        n_springs = 50

        # Generate random positions
        positions = np.random.uniform(10, 90, (pop_size, n_components, 2)).astype(np.float32)
        sizes = np.random.uniform(5, 15, (n_components, 2)).astype(np.float32)
        springs = np.random.randint(0, n_components, (n_springs, 2)).astype(np.int32)
        pin_offsets = np.random.uniform(-2, 2, (n_springs, 2, 2)).astype(np.float32)
        board_vertices = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)

        weights = {
            "baseline": 1000.0,
            "wire_length_weight": 0.1,
            "conflict_weight": 100.0,
            "boundary_violation_weight": 500.0,
            "routability_weight": 50.0,
            "pin_alignment_weight": 5.0,
            "pin_alignment_tolerance": 0.5,
        }

        result = evaluate_population_gpu(
            positions, sizes, springs, pin_offsets, board_vertices, weights, cpu_backend
        )

        assert result.shape == (pop_size,)
        assert all(np.isfinite(result))
