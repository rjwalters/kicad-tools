"""Tests for GPU-accelerated signal integrity calculations.

Tests the signal integrity kernels including crosstalk matrix calculation,
impedance batch computation, and NEXT/FEXT analysis.
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.acceleration.backend import ArrayBackend, BackendType
from kicad_tools.acceleration.kernels.signal_integrity import (
    SignalIntegrityGPUAccelerator,
    calculate_crosstalk_matrix,
    calculate_impedance_batch,
    calculate_next_fext_batch,
    calculate_pairwise_distances,
    classify_crosstalk_risk,
    estimate_parallel_lengths,
)


@pytest.fixture
def cpu_backend() -> ArrayBackend:
    """Create CPU backend for testing."""
    return ArrayBackend.create(BackendType.CPU)


class TestCrosstalkMatrix:
    """Tests for crosstalk matrix calculation."""

    def test_zero_separation(self, cpu_backend: ArrayBackend) -> None:
        """Test that very small separation gives high coupling."""
        separations = np.array([[0.0, 0.05], [0.05, 0.0]])
        lengths = np.array([[0.0, 10.0], [10.0, 0.0]])

        coupling = calculate_crosstalk_matrix(separations, lengths, cpu_backend)

        # Diagonal should be zero (no self-coupling)
        assert coupling[0, 0] == 0.0
        assert coupling[1, 1] == 0.0

        # Off-diagonal should have coupling (capped at 1.0)
        assert 0 < coupling[0, 1] <= 1.0
        assert 0 < coupling[1, 0] <= 1.0

    def test_large_separation(self, cpu_backend: ArrayBackend) -> None:
        """Test that large separation gives low coupling."""
        # Large separation (5mm) with moderate length (10mm)
        separations = np.array([[0.0, 5.0], [5.0, 0.0]])
        lengths = np.array([[0.0, 10.0], [10.0, 0.0]])

        coupling = calculate_crosstalk_matrix(separations, lengths, cpu_backend)

        # Coupling should be very low with large spacing
        assert coupling[0, 1] < 0.1
        assert coupling[1, 0] < 0.1

    def test_coupling_increases_with_length(self, cpu_backend: ArrayBackend) -> None:
        """Test that coupling increases with parallel length."""
        separations = np.array([[0.0, 0.2], [0.2, 0.0]])

        lengths_short = np.array([[0.0, 5.0], [5.0, 0.0]])
        lengths_long = np.array([[0.0, 50.0], [50.0, 0.0]])

        coupling_short = calculate_crosstalk_matrix(separations, lengths_short, cpu_backend)
        coupling_long = calculate_crosstalk_matrix(separations, lengths_long, cpu_backend)

        # Longer parallel run should have more coupling
        assert coupling_long[0, 1] > coupling_short[0, 1]

    def test_diagonal_is_zero(self, cpu_backend: ArrayBackend) -> None:
        """Test that diagonal is always zero."""
        # Random matrix
        n = 10
        separations = np.random.rand(n, n) * 2.0
        lengths = np.random.rand(n, n) * 20.0

        coupling = calculate_crosstalk_matrix(separations, lengths, cpu_backend)

        # Check diagonal
        for i in range(n):
            assert coupling[i, i] == 0.0


class TestImpedanceBatch:
    """Tests for batch impedance calculation."""

    def test_typical_50_ohm(self, cpu_backend: ArrayBackend) -> None:
        """Test impedance calculation for typical 50 ohm trace."""
        # Typical 50 ohm microstrip: ~0.2mm width, ~0.2mm height, FR4 (er=4.5)
        widths = np.array([0.2])
        heights = np.array([0.2])
        er = np.array([4.5])

        z0 = calculate_impedance_batch(widths, heights, er, cpu_backend)

        # Should be in reasonable range for 50 ohm trace
        assert 40 < z0[0] < 70

    def test_wider_trace_lower_impedance(self, cpu_backend: ArrayBackend) -> None:
        """Test that wider traces have lower impedance."""
        # Same height and dielectric, different widths
        widths = np.array([0.1, 0.2, 0.4])
        heights = np.array([0.2, 0.2, 0.2])
        er = np.array([4.5, 4.5, 4.5])

        z0 = calculate_impedance_batch(widths, heights, er, cpu_backend)

        # Impedance should decrease with width
        assert z0[0] > z0[1] > z0[2]

    def test_impedance_bounds(self, cpu_backend: ArrayBackend) -> None:
        """Test that impedance is clamped to reasonable range."""
        # Extreme values
        widths = np.array([0.001, 10.0])  # Very narrow, very wide
        heights = np.array([0.2, 0.2])
        er = np.array([4.5, 4.5])

        z0 = calculate_impedance_batch(widths, heights, er, cpu_backend)

        # Should be clamped to [10, 200]
        assert 10 <= z0[0] <= 200
        assert 10 <= z0[1] <= 200


class TestNextFextBatch:
    """Tests for NEXT/FEXT batch calculation."""

    def test_saturated_next(self, cpu_backend: ArrayBackend) -> None:
        """Test NEXT saturation for long coupling lengths."""
        # High coupling coefficient, long length
        k = np.array([0.3])
        lengths = np.array([100.0])  # Long parallel run
        rise_times = np.array([1.0])
        eps_eff = np.array([3.5])

        next_pct, fext_pct = calculate_next_fext_batch(
            k, lengths, rise_times, eps_eff, cpu_backend
        )

        # NEXT should saturate at k/2 * 100 = 15%
        assert next_pct[0] == pytest.approx(15.0, rel=0.1)

    def test_fext_proportional_to_length(self, cpu_backend: ArrayBackend) -> None:
        """Test that FEXT increases with coupling length."""
        k = np.array([0.1, 0.1])
        lengths = np.array([10.0, 50.0])  # Short and long
        rise_times = np.array([1.0, 1.0])
        eps_eff = np.array([3.5, 3.5])

        next_pct, fext_pct = calculate_next_fext_batch(
            k, lengths, rise_times, eps_eff, cpu_backend
        )

        # FEXT should be higher for longer coupling
        assert fext_pct[1] > fext_pct[0]


class TestPairwiseDistances:
    """Tests for pairwise distance calculation."""

    def test_simple_distances(self, cpu_backend: ArrayBackend) -> None:
        """Test pairwise distances for simple positions."""
        positions = np.array([
            [0.0, 0.0],
            [3.0, 0.0],
            [0.0, 4.0],
        ])

        distances = calculate_pairwise_distances(positions, cpu_backend)

        # Check known distances
        assert distances[0, 1] == pytest.approx(3.0)
        assert distances[0, 2] == pytest.approx(4.0)
        assert distances[1, 2] == pytest.approx(5.0)  # 3-4-5 triangle

        # Symmetric
        assert distances[1, 0] == distances[0, 1]

        # Diagonal is zero
        assert distances[0, 0] == 0.0

    def test_distance_matrix_symmetric(self, cpu_backend: ArrayBackend) -> None:
        """Test that distance matrix is symmetric."""
        # Random positions
        positions = np.random.rand(10, 2) * 100

        distances = calculate_pairwise_distances(positions, cpu_backend)

        np.testing.assert_array_almost_equal(distances, distances.T)


class TestParallelLengths:
    """Tests for parallel length estimation."""

    def test_overlapping_traces(self, cpu_backend: ArrayBackend) -> None:
        """Test parallel length for overlapping traces."""
        # Two traces that run parallel for 10mm
        trace_endpoints = np.array([
            [0.0, 0.0, 20.0, 0.0],  # Horizontal trace from (0,0) to (20,0)
            [5.0, 1.0, 15.0, 1.0],  # Parallel trace from (5,1) to (15,1)
        ])

        parallel_lengths = estimate_parallel_lengths(trace_endpoints, cpu_backend)

        # Overlap in x-direction should be 10mm (from x=5 to x=15)
        assert parallel_lengths[0, 1] == pytest.approx(10.0)

    def test_non_overlapping_traces(self, cpu_backend: ArrayBackend) -> None:
        """Test parallel length for non-overlapping traces."""
        # Two traces that don't overlap
        trace_endpoints = np.array([
            [0.0, 0.0, 10.0, 0.0],  # From (0,0) to (10,0)
            [20.0, 0.0, 30.0, 0.0],  # From (20,0) to (30,0) - no overlap
        ])

        parallel_lengths = estimate_parallel_lengths(trace_endpoints, cpu_backend)

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


class TestSignalIntegrityAccelerator:
    """Tests for the SignalIntegrityGPUAccelerator class."""

    def test_analyze_crosstalk(self, cpu_backend: ArrayBackend) -> None:
        """Test full crosstalk analysis."""
        accelerator = SignalIntegrityGPUAccelerator(backend=cpu_backend)

        separations = np.array([
            [0.0, 0.2, 1.0],
            [0.2, 0.0, 0.3],
            [1.0, 0.3, 0.0],
        ])
        lengths = np.array([
            [0.0, 15.0, 5.0],
            [15.0, 0.0, 20.0],
            [5.0, 20.0, 0.0],
        ])

        result = accelerator.analyze_crosstalk(separations, lengths)

        assert "coupling" in result
        assert "max_coupling" in result
        assert "risk" in result

        assert result["coupling"].shape == (3, 3)
        assert result["max_coupling"].shape == (3,)
        assert result["risk"].shape == (3,)

    def test_calculate_impedances(self, cpu_backend: ArrayBackend) -> None:
        """Test batch impedance calculation."""
        accelerator = SignalIntegrityGPUAccelerator(backend=cpu_backend)

        widths = np.array([0.15, 0.2, 0.25])
        heights = np.array([0.2, 0.2, 0.2])
        er = np.array([4.5, 4.5, 4.5])

        z0 = accelerator.calculate_impedances(widths, heights, er)

        assert z0.shape == (3,)
        # All should be reasonable impedance values
        assert all(10 <= z <= 200 for z in z0)

    def test_analyze_net_pair(self, cpu_backend: ArrayBackend) -> None:
        """Test single net pair analysis."""
        accelerator = SignalIntegrityGPUAccelerator(backend=cpu_backend)

        result = accelerator.analyze_net_pair(
            coupling_coefficient=0.2,
            parallel_length_mm=25.0,
            rise_time_ns=1.0,
            effective_dielectric=3.5,
        )

        assert "next_percent" in result
        assert "fext_percent" in result
        assert "risk" in result
        assert "risk_label" in result

        assert result["risk_label"] in ["acceptable", "marginal", "excessive"]


class TestDeterminism:
    """Tests to verify calculations are deterministic."""

    def test_crosstalk_matrix_deterministic(self, cpu_backend: ArrayBackend) -> None:
        """Test that crosstalk matrix is deterministic."""
        # Fixed inputs
        np.random.seed(42)
        separations = np.random.rand(20, 20) * 2.0
        lengths = np.random.rand(20, 20) * 20.0

        # Run twice
        result1 = calculate_crosstalk_matrix(separations, lengths, cpu_backend)
        result2 = calculate_crosstalk_matrix(separations, lengths, cpu_backend)

        np.testing.assert_array_equal(result1, result2)

    def test_impedance_batch_deterministic(self, cpu_backend: ArrayBackend) -> None:
        """Test that impedance batch is deterministic."""
        np.random.seed(42)
        widths = np.random.rand(50) * 0.3 + 0.1
        heights = np.random.rand(50) * 0.3 + 0.1
        er = np.random.rand(50) * 2.0 + 3.5

        result1 = calculate_impedance_batch(widths, heights, er, cpu_backend)
        result2 = calculate_impedance_batch(widths, heights, er, cpu_backend)

        np.testing.assert_array_equal(result1, result2)
