"""Tests for GPU-accelerated grid operations."""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.acceleration import (
    BackendType,
    estimate_memory_bytes,
    get_backend,
    to_numpy,
)
from kicad_tools.performance import (
    GpuConfig,
    GpuThresholds,
    PerformanceConfig,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def design_rules():
    """Create default design rules for testing."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.1,
    )


@pytest.fixture
def small_grid_config():
    """Config that won't trigger GPU (small grid threshold)."""
    return PerformanceConfig(
        gpu=GpuConfig(
            backend="auto",
            thresholds=GpuThresholds(min_grid_cells=1_000_000),  # High threshold
        )
    )


@pytest.fixture
def gpu_enabled_config():
    """Config with low threshold to enable GPU testing."""
    return PerformanceConfig(
        gpu=GpuConfig(
            backend="auto",
            thresholds=GpuThresholds(min_grid_cells=100),  # Very low threshold
        )
    )


@pytest.fixture
def cpu_forced_config():
    """Config that forces CPU backend."""
    return PerformanceConfig(
        gpu=GpuConfig(
            backend="cpu",
            thresholds=GpuThresholds(min_grid_cells=100),
        )
    )


class TestGridBackendSelection:
    """Tests for grid backend selection logic."""

    def test_no_config_uses_cpu(self, design_rules):
        """Grid without config should always use CPU."""
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=design_rules,
        )
        assert grid.backend_type == BackendType.CPU
        assert not grid.uses_gpu

    def test_cpu_forced_backend(self, design_rules, cpu_forced_config):
        """Grid with CPU backend should use CPU regardless of size."""
        grid = RoutingGrid(
            width=100.0,  # Large grid
            height=100.0,
            rules=design_rules,
            config=cpu_forced_config,
        )
        assert grid.backend_type == BackendType.CPU
        assert not grid.uses_gpu

    def test_small_grid_uses_cpu(self, design_rules, gpu_enabled_config):
        """Small grid below threshold should use CPU."""
        # Create a tiny grid
        small_config = PerformanceConfig(
            gpu=GpuConfig(
                backend="auto",
                thresholds=GpuThresholds(min_grid_cells=1_000_000),
            )
        )
        grid = RoutingGrid(
            width=1.0,
            height=1.0,
            rules=design_rules,
            config=small_config,
        )
        # Grid is ~100 cells, threshold is 1M, should use CPU
        assert grid.backend_type == BackendType.CPU


class TestGridStatistics:
    """Tests for grid statistics with GPU info."""

    def test_statistics_include_gpu_info(self, design_rules):
        """Grid statistics should include GPU backend info."""
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=design_rules,
        )
        stats = grid.get_grid_statistics()

        assert "gpu_backend" in stats
        assert "uses_gpu" in stats
        assert stats["gpu_backend"] == "cpu"
        assert stats["uses_gpu"] is False

    def test_statistics_memory_calculation(self, design_rules):
        """Memory calculation should work for both CPU and GPU."""
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=design_rules,
        )
        stats = grid.get_grid_statistics()

        # Memory should be reasonable (> 0 and not huge for small grid)
        assert stats["memory_mb"] > 0
        assert stats["memory_mb"] < 100  # Small grid shouldn't be > 100MB


class TestMemoryEstimation:
    """Tests for GPU memory estimation."""

    def test_estimate_memory_small_grid(self):
        """Test memory estimation for small grid."""
        # 100x100x2 = 20,000 cells * 18 bytes = 360,000 bytes
        memory = estimate_memory_bytes(100, 100, 2)
        assert memory == 100 * 100 * 2 * 18

    def test_estimate_memory_large_grid(self):
        """Test memory estimation for large grid."""
        # 1000x1000x4 = 4,000,000 cells * 18 bytes = 72MB
        memory = estimate_memory_bytes(1000, 1000, 4)
        expected_mb = (1000 * 1000 * 4 * 18) / (1024 * 1024)
        actual_mb = memory / (1024 * 1024)
        assert abs(actual_mb - expected_mb) < 0.1


class TestGridOperationsWithCPU:
    """Tests for grid operations (CPU backend)."""

    def test_history_cost_update(self, design_rules):
        """Test history cost update works on CPU."""
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=design_rules,
        )

        # Set some usage counts
        grid._usage_count[0, 10, 10] = 2  # Overused
        grid._usage_count[0, 20, 20] = 3  # More overused

        # Update history costs
        grid.update_history_costs(history_increment=1.0)

        # Check history costs were updated
        assert grid._history_cost[0, 10, 10] == 1.0  # (2-1) * 1.0
        assert grid._history_cost[0, 20, 20] == 2.0  # (3-1) * 1.0
        assert grid._history_cost[0, 0, 0] == 0.0  # Not overused

    def test_find_overused_cells(self, design_rules):
        """Test finding overused cells works."""
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=design_rules,
        )

        # Set some usage counts
        grid._usage_count[0, 10, 10] = 2
        grid._usage_count[0, 20, 20] = 3

        # Find overused
        overused = grid.find_overused_cells()

        assert len(overused) == 2
        # Results are (x, y, layer, usage)
        usage_dict = {(x, y, layer): usage for x, y, layer, usage in overused}
        assert usage_dict[(10, 10, 0)] == 2
        assert usage_dict[(20, 20, 0)] == 3

    def test_get_total_overflow(self, design_rules):
        """Test total overflow calculation."""
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=design_rules,
        )

        # Set some usage counts
        grid._usage_count[0, 10, 10] = 2  # overflow = 1
        grid._usage_count[0, 20, 20] = 3  # overflow = 2
        grid._usage_count[0, 30, 30] = 1  # no overflow

        total = grid.get_total_overflow()
        assert total == 3  # 1 + 2


class TestBackendAbstraction:
    """Tests for backend abstraction module."""

    def test_get_cpu_backend(self):
        """Test getting CPU backend returns an ArrayBackend wrapping numpy."""
        from kicad_tools.acceleration.backend import ArrayBackend

        backend = get_backend(BackendType.CPU)
        assert isinstance(backend, ArrayBackend)

    def test_to_numpy_passthrough(self):
        """Test to_numpy with numpy array."""
        arr = np.array([1, 2, 3])
        result = to_numpy(arr)
        assert isinstance(result, np.ndarray)
        np.testing.assert_array_equal(result, arr)

    def test_estimate_memory_consistent(self):
        """Test memory estimation is consistent."""
        # Same inputs should give same output
        m1 = estimate_memory_bytes(500, 500, 4)
        m2 = estimate_memory_bytes(500, 500, 4)
        assert m1 == m2


class TestSyncOperations:
    """Tests for GPU/CPU sync operations."""

    def test_sync_to_cpu_noop_on_cpu(self, design_rules):
        """sync_to_cpu should be no-op when already on CPU."""
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=design_rules,
        )

        # Should not raise, should be no-op
        grid.sync_to_cpu()
        assert grid.backend_type == BackendType.CPU

    def test_sync_to_gpu_without_config(self, design_rules):
        """sync_to_gpu should be no-op without config."""
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=design_rules,
        )

        # Should not raise, should be no-op
        grid.sync_to_gpu()
        assert grid.backend_type == BackendType.CPU


class TestGridWithConfig:
    """Tests for grid with various configurations."""

    def test_grid_with_high_threshold(self, design_rules):
        """Grid with high threshold should use CPU for small grids."""
        config = PerformanceConfig(
            gpu=GpuConfig(
                backend="auto",
                thresholds=GpuThresholds(min_grid_cells=10_000_000),
            )
        )
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=design_rules,
            config=config,
        )
        assert grid.backend_type == BackendType.CPU

    def test_grid_respects_memory_limit(self, design_rules):
        """Grid should fall back to CPU if memory limit exceeded."""
        # Set very low memory limit
        config = PerformanceConfig(
            gpu=GpuConfig(
                backend="auto",
                memory_limit_mb=1,  # 1MB limit, too small for any grid
                thresholds=GpuThresholds(min_grid_cells=100),
            )
        )
        grid = RoutingGrid(
            width=100.0,
            height=100.0,
            rules=design_rules,
            config=config,
        )
        # Should fall back to CPU due to memory limit
        assert grid.backend_type == BackendType.CPU


class TestArrayOperationsEquivalence:
    """Tests to verify GPU and CPU produce same results."""

    def test_history_cost_equivalence(self, design_rules):
        """History cost update should produce same results on CPU."""
        # Create two grids with same setup
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=design_rules,
        )

        # Set up identical usage patterns
        np.random.seed(42)
        random_usage = np.random.randint(0, 4, size=grid._usage_count.shape)
        grid._usage_count = random_usage.astype(np.int16)

        # Update history costs
        grid.update_history_costs(history_increment=0.5)

        # Verify expected values
        expected_mask = random_usage > 1
        expected_increment = 0.5 * (random_usage.astype(np.float32) - 1)
        expected_cost = np.where(expected_mask, expected_increment, 0)

        np.testing.assert_array_almost_equal(
            grid._history_cost,
            expected_cost,
            decimal=5,
        )

    def test_overflow_calculation_equivalence(self, design_rules):
        """Total overflow should be calculated correctly."""
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=design_rules,
        )

        # Set up usage pattern
        np.random.seed(123)
        random_usage = np.random.randint(0, 5, size=grid._usage_count.shape)
        grid._usage_count = random_usage.astype(np.int16)

        # Calculate expected overflow
        expected_overflow = np.sum(np.maximum(0, random_usage - 1))

        actual_overflow = grid.get_total_overflow()
        assert actual_overflow == expected_overflow
