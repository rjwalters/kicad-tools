"""Tests for GPU configuration and signal integrity acceleration.

Tests the GPU backend abstraction, should_use_gpu decision logic,
and GPU-accelerated signal integrity kernels.
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.acceleration import (
    GpuConfig,
    GpuThresholds,
    get_backend,
    should_use_gpu,
)
from kicad_tools.acceleration.config import NumpyBackend, clear_backend_cache
from kicad_tools.acceleration.kernels.signal_integrity import (
    calculate_crosstalk_matrix,
    calculate_impedance_batch,
    calculate_next_fext_batch,
    calculate_pairwise_distances,
    classify_crosstalk_risk,
    estimate_parallel_lengths,
)
from kicad_tools.performance import PerformanceConfig


class TestGpuConfig:
    """Tests for GPU configuration."""

    def test_default_thresholds(self) -> None:
        """Test default GPU thresholds are reasonable."""
        thresholds = GpuThresholds()

        assert thresholds.min_grid_cells == 100_000
        assert thresholds.min_components == 50
        assert thresholds.min_population == 20
        assert thresholds.min_trace_pairs == 100

    def test_gpu_config_defaults(self) -> None:
        """Test GpuConfig defaults."""
        config = GpuConfig()

        # Backend should be auto-detected (could be cuda, metal, or cpu)
        assert config.backend in ("cuda", "metal", "cpu")
        assert config.device_id == 0
        assert config.memory_limit_mb == 0
        assert config.enabled is True

    def test_cpu_backend_explicit(self) -> None:
        """Test forcing CPU backend."""
        config = GpuConfig(backend="cpu")

        assert config.backend == "cpu"


class TestShouldUseGpu:
    """Tests for should_use_gpu decision logic."""

    def test_gpu_disabled(self) -> None:
        """Test that GPU is not used when disabled."""
        config = PerformanceConfig()
        config.gpu.enabled = False

        assert should_use_gpu(config, 1_000_000, "grid") is False
        assert should_use_gpu(config, 1000, "signal_integrity") is False

    def test_cpu_backend(self) -> None:
        """Test that GPU is not used when backend is CPU."""
        config = PerformanceConfig()
        config.gpu.backend = "cpu"
        config.gpu.enabled = True

        assert should_use_gpu(config, 1_000_000, "grid") is False

    def test_below_threshold(self) -> None:
        """Test that GPU is not used below threshold."""
        config = PerformanceConfig()
        config.gpu.backend = "cuda"  # Pretend we have CUDA
        config.gpu.enabled = True

        # Below grid threshold (100,000)
        assert should_use_gpu(config, 50_000, "grid") is False

        # Below signal_integrity threshold (100)
        assert should_use_gpu(config, 50, "signal_integrity") is False

    def test_above_threshold(self) -> None:
        """Test that GPU is used above threshold."""
        config = PerformanceConfig()
        config.gpu.backend = "cuda"  # Pretend we have CUDA
        config.gpu.enabled = True

        # Above grid threshold
        assert should_use_gpu(config, 200_000, "grid") is True

        # Above signal_integrity threshold
        assert should_use_gpu(config, 200, "signal_integrity") is True

    def test_signal_integrity_threshold(self) -> None:
        """Test signal integrity threshold specifically."""
        config = PerformanceConfig()
        config.gpu.backend = "cuda"
        config.gpu.enabled = True

        # Exactly at threshold
        assert should_use_gpu(config, 100, "signal_integrity") is True

        # Just below threshold
        assert should_use_gpu(config, 99, "signal_integrity") is False


class TestArrayBackend:
    """Tests for array backend operations."""

    def setup_method(self) -> None:
        """Clear backend cache before each test."""
        clear_backend_cache()

    def test_numpy_backend_basic(self) -> None:
        """Test NumPy backend basic operations."""
        backend = NumpyBackend()

        # Array creation
        arr = backend.array([1, 2, 3, 4])
        assert isinstance(arr, np.ndarray)
        assert arr.shape == (4,)

        # zeros/ones
        zeros = backend.zeros((3, 3))
        assert zeros.shape == (3, 3)
        assert np.all(zeros == 0)

        ones = backend.ones((2, 4))
        assert ones.shape == (2, 4)
        assert np.all(ones == 1)

    def test_numpy_backend_math(self) -> None:
        """Test NumPy backend math operations."""
        backend = NumpyBackend()

        arr = backend.array([1.0, 4.0, 9.0, 16.0])

        # sqrt
        sqrt_arr = backend.sqrt(arr)
        np.testing.assert_array_almost_equal(sqrt_arr, [1.0, 2.0, 3.0, 4.0])

        # log
        log_arr = backend.log(arr)
        expected_log = np.log([1.0, 4.0, 9.0, 16.0])
        np.testing.assert_array_almost_equal(log_arr, expected_log)

        # exp
        exp_arr = backend.exp(backend.array([0.0, 1.0]))
        np.testing.assert_array_almost_equal(exp_arr, [1.0, np.e])

    def test_numpy_backend_aggregations(self) -> None:
        """Test NumPy backend aggregation operations."""
        backend = NumpyBackend()

        arr = backend.array([[1, 2, 3], [4, 5, 6]])

        # sum
        assert backend.sum(arr) == 21
        np.testing.assert_array_equal(backend.sum(arr, axis=0), [5, 7, 9])
        np.testing.assert_array_equal(backend.sum(arr, axis=1), [6, 15])

        # max/min
        assert backend.max(arr) == 6
        assert backend.min(arr) == 1

    def test_numpy_backend_fill_diagonal(self) -> None:
        """Test fill_diagonal operation."""
        backend = NumpyBackend()

        arr = backend.ones((3, 3))
        result = backend.fill_diagonal(arr, 0.0)

        expected = np.array([[0, 1, 1], [1, 0, 1], [1, 1, 0]], dtype=float)
        np.testing.assert_array_equal(result, expected)

    def test_get_backend_default_cpu(self) -> None:
        """Test get_backend returns CPU backend without config."""
        clear_backend_cache()
        backend = get_backend(None)

        assert isinstance(backend, NumpyBackend)


class TestCrosstalkMatrix:
    """Tests for crosstalk matrix calculation."""

    def test_zero_separation(self) -> None:
        """Test that very small separation gives high coupling."""
        backend = NumpyBackend()

        separations = np.array([[0.0, 0.05], [0.05, 0.0]])
        lengths = np.array([[0.0, 10.0], [10.0, 0.0]])

        coupling = calculate_crosstalk_matrix(separations, lengths, backend)

        # Diagonal should be zero (no self-coupling)
        assert coupling[0, 0] == 0.0
        assert coupling[1, 1] == 0.0

        # Off-diagonal should have coupling (capped at 1.0)
        assert 0 < coupling[0, 1] <= 1.0
        assert 0 < coupling[1, 0] <= 1.0

    def test_large_separation(self) -> None:
        """Test that large separation gives low coupling."""
        backend = NumpyBackend()

        # Large separation (5mm) with moderate length (10mm)
        separations = np.array([[0.0, 5.0], [5.0, 0.0]])
        lengths = np.array([[0.0, 10.0], [10.0, 0.0]])

        coupling = calculate_crosstalk_matrix(separations, lengths, backend)

        # Coupling should be very low with large spacing
        assert coupling[0, 1] < 0.1
        assert coupling[1, 0] < 0.1

    def test_coupling_increases_with_length(self) -> None:
        """Test that coupling increases with parallel length."""
        backend = NumpyBackend()

        separations = np.array([[0.0, 0.2], [0.2, 0.0]])

        lengths_short = np.array([[0.0, 5.0], [5.0, 0.0]])
        lengths_long = np.array([[0.0, 50.0], [50.0, 0.0]])

        coupling_short = calculate_crosstalk_matrix(separations, lengths_short, backend)
        coupling_long = calculate_crosstalk_matrix(separations, lengths_long, backend)

        # Longer parallel run should have more coupling
        assert coupling_long[0, 1] > coupling_short[0, 1]

    def test_diagonal_is_zero(self) -> None:
        """Test that diagonal is always zero."""
        backend = NumpyBackend()

        # Random matrix
        n = 10
        separations = np.random.rand(n, n) * 2.0
        lengths = np.random.rand(n, n) * 20.0

        coupling = calculate_crosstalk_matrix(separations, lengths, backend)

        # Check diagonal
        for i in range(n):
            assert coupling[i, i] == 0.0


class TestImpedanceBatch:
    """Tests for batch impedance calculation."""

    def test_typical_50_ohm(self) -> None:
        """Test impedance calculation for typical 50 ohm trace."""
        backend = NumpyBackend()

        # Typical 50 ohm microstrip: ~0.2mm width, ~0.2mm height, FR4 (er=4.5)
        widths = np.array([0.2])
        heights = np.array([0.2])
        er = np.array([4.5])

        z0 = calculate_impedance_batch(widths, heights, er, backend)

        # Should be in reasonable range for 50 ohm trace
        assert 40 < z0[0] < 70

    def test_wider_trace_lower_impedance(self) -> None:
        """Test that wider traces have lower impedance."""
        backend = NumpyBackend()

        # Same height and dielectric, different widths
        widths = np.array([0.1, 0.2, 0.4])
        heights = np.array([0.2, 0.2, 0.2])
        er = np.array([4.5, 4.5, 4.5])

        z0 = calculate_impedance_batch(widths, heights, er, backend)

        # Impedance should decrease with width
        assert z0[0] > z0[1] > z0[2]

    def test_impedance_bounds(self) -> None:
        """Test that impedance is clamped to reasonable range."""
        backend = NumpyBackend()

        # Extreme values
        widths = np.array([0.001, 10.0])  # Very narrow, very wide
        heights = np.array([0.2, 0.2])
        er = np.array([4.5, 4.5])

        z0 = calculate_impedance_batch(widths, heights, er, backend)

        # Should be clamped to [10, 200]
        assert 10 <= z0[0] <= 200
        assert 10 <= z0[1] <= 200


class TestNextFextBatch:
    """Tests for NEXT/FEXT batch calculation."""

    def test_saturated_next(self) -> None:
        """Test NEXT saturation for long coupling lengths."""
        backend = NumpyBackend()

        # High coupling coefficient, long length
        k = np.array([0.3])
        lengths = np.array([100.0])  # Long parallel run
        rise_times = np.array([1.0])
        eps_eff = np.array([3.5])

        next_pct, fext_pct = calculate_next_fext_batch(k, lengths, rise_times, eps_eff, backend)

        # NEXT should saturate at k/2 * 100 = 15%
        assert next_pct[0] == pytest.approx(15.0, rel=0.1)

    def test_fext_proportional_to_length(self) -> None:
        """Test that FEXT increases with coupling length."""
        backend = NumpyBackend()

        k = np.array([0.1, 0.1])
        lengths = np.array([10.0, 50.0])  # Short and long
        rise_times = np.array([1.0, 1.0])
        eps_eff = np.array([3.5, 3.5])

        next_pct, fext_pct = calculate_next_fext_batch(k, lengths, rise_times, eps_eff, backend)

        # FEXT should be higher for longer coupling
        assert fext_pct[1] > fext_pct[0]


class TestPairwiseDistances:
    """Tests for pairwise distance calculation."""

    def test_simple_distances(self) -> None:
        """Test pairwise distances for simple positions."""
        backend = NumpyBackend()

        positions = np.array([
            [0.0, 0.0],
            [3.0, 0.0],
            [0.0, 4.0],
        ])

        distances = calculate_pairwise_distances(positions, backend)

        # Check known distances
        assert distances[0, 1] == pytest.approx(3.0)
        assert distances[0, 2] == pytest.approx(4.0)
        assert distances[1, 2] == pytest.approx(5.0)  # 3-4-5 triangle

        # Symmetric
        assert distances[1, 0] == distances[0, 1]

        # Diagonal is zero
        assert distances[0, 0] == 0.0

    def test_distance_matrix_symmetric(self) -> None:
        """Test that distance matrix is symmetric."""
        backend = NumpyBackend()

        # Random positions
        positions = np.random.rand(10, 2) * 100

        distances = calculate_pairwise_distances(positions, backend)

        np.testing.assert_array_almost_equal(distances, distances.T)


class TestParallelLengths:
    """Tests for parallel length estimation."""

    def test_overlapping_traces(self) -> None:
        """Test parallel length for overlapping traces."""
        # Two traces that run parallel for 10mm
        trace_endpoints = np.array([
            [0.0, 0.0, 20.0, 0.0],  # Horizontal trace from (0,0) to (20,0)
            [5.0, 1.0, 15.0, 1.0],  # Parallel trace from (5,1) to (15,1)
        ])

        backend = NumpyBackend()
        parallel_lengths = estimate_parallel_lengths(trace_endpoints, backend)

        # Overlap in x-direction should be 10mm (from x=5 to x=15)
        assert parallel_lengths[0, 1] == pytest.approx(10.0)

    def test_non_overlapping_traces(self) -> None:
        """Test parallel length for non-overlapping traces."""
        # Two traces that don't overlap
        trace_endpoints = np.array([
            [0.0, 0.0, 10.0, 0.0],  # From (0,0) to (10,0)
            [20.0, 0.0, 30.0, 0.0],  # From (20,0) to (30,0) - no overlap
        ])

        backend = NumpyBackend()
        parallel_lengths = estimate_parallel_lengths(trace_endpoints, backend)

        # No overlap
        assert parallel_lengths[0, 1] == 0.0


class TestClassifyCrosstalkRisk:
    """Tests for crosstalk risk classification."""

    def test_acceptable_risk(self) -> None:
        """Test classification of acceptable risk."""
        next_pct = np.array([1.0, 2.0, 2.5])
        fext_pct = np.array([0.5, 1.5, 2.0])

        risk = classify_crosstalk_risk(next_pct, fext_pct)

        np.testing.assert_array_equal(risk, [0, 0, 0])  # All acceptable

    def test_marginal_risk(self) -> None:
        """Test classification of marginal risk."""
        next_pct = np.array([4.0, 5.0, 9.0])
        fext_pct = np.array([3.0, 4.0, 8.0])

        risk = classify_crosstalk_risk(next_pct, fext_pct)

        np.testing.assert_array_equal(risk, [1, 1, 1])  # All marginal

    def test_excessive_risk(self) -> None:
        """Test classification of excessive risk."""
        next_pct = np.array([15.0, 20.0])
        fext_pct = np.array([12.0, 18.0])

        risk = classify_crosstalk_risk(next_pct, fext_pct)

        np.testing.assert_array_equal(risk, [2, 2])  # All excessive

    def test_mixed_risk_levels(self) -> None:
        """Test classification with mixed risk levels."""
        next_pct = np.array([1.0, 5.0, 15.0])
        fext_pct = np.array([2.0, 4.0, 12.0])

        risk = classify_crosstalk_risk(next_pct, fext_pct)

        np.testing.assert_array_equal(risk, [0, 1, 2])  # acceptable, marginal, excessive


class TestGpuCpuEquivalence:
    """Tests to verify GPU and CPU produce equivalent results."""

    def test_crosstalk_matrix_deterministic(self) -> None:
        """Test that crosstalk matrix is deterministic."""
        backend = NumpyBackend()

        # Fixed inputs
        np.random.seed(42)
        separations = np.random.rand(20, 20) * 2.0
        lengths = np.random.rand(20, 20) * 20.0

        # Run twice
        result1 = calculate_crosstalk_matrix(separations, lengths, backend)
        result2 = calculate_crosstalk_matrix(separations, lengths, backend)

        np.testing.assert_array_equal(result1, result2)

    def test_impedance_batch_deterministic(self) -> None:
        """Test that impedance batch is deterministic."""
        backend = NumpyBackend()

        np.random.seed(42)
        widths = np.random.rand(50) * 0.3 + 0.1
        heights = np.random.rand(50) * 0.3 + 0.1
        er = np.random.rand(50) * 2.0 + 3.5

        result1 = calculate_impedance_batch(widths, heights, er, backend)
        result2 = calculate_impedance_batch(widths, heights, er, backend)

        np.testing.assert_array_equal(result1, result2)
