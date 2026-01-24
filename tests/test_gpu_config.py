"""Tests for GPU configuration in PerformanceConfig."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kicad_tools.acceleration.config import should_use_gpu
from kicad_tools.performance import (
    GpuConfig,
    GpuThresholds,
    PerformanceConfig,
)


class TestGpuThresholds:
    """Tests for GpuThresholds dataclass."""

    def test_default_values(self):
        """Test default threshold values."""
        thresholds = GpuThresholds()
        assert thresholds.min_grid_cells == 100_000
        assert thresholds.min_components == 50
        assert thresholds.min_population == 20
        assert thresholds.min_trace_pairs == 100

    def test_custom_values(self):
        """Test custom threshold values."""
        thresholds = GpuThresholds(
            min_grid_cells=50_000,
            min_components=25,
            min_population=10,
            min_trace_pairs=50,
        )
        assert thresholds.min_grid_cells == 50_000
        assert thresholds.min_components == 25
        assert thresholds.min_population == 10
        assert thresholds.min_trace_pairs == 50


class TestGpuConfig:
    """Tests for GpuConfig dataclass."""

    def test_default_values(self):
        """Test default GPU config values."""
        config = GpuConfig()
        assert config.backend == "auto"
        assert config.device_id == 0
        assert config.memory_limit_mb == 0
        assert isinstance(config.thresholds, GpuThresholds)

    def test_custom_values(self):
        """Test custom GPU config values."""
        config = GpuConfig(
            backend="cuda",
            device_id=1,
            memory_limit_mb=4096,
        )
        assert config.backend == "cuda"
        assert config.device_id == 1
        assert config.memory_limit_mb == 4096

    def test_custom_thresholds(self):
        """Test custom thresholds in GPU config."""
        thresholds = GpuThresholds(min_grid_cells=200_000)
        config = GpuConfig(thresholds=thresholds)
        assert config.thresholds.min_grid_cells == 200_000


class TestPerformanceConfigGpu:
    """Tests for GPU settings in PerformanceConfig."""

    def test_default_gpu_config(self):
        """Test PerformanceConfig has default GPU config."""
        config = PerformanceConfig()
        assert isinstance(config.gpu, GpuConfig)
        assert config.gpu.backend == "auto"

    def test_gpu_in_to_dict(self):
        """Test GPU settings are included in to_dict output."""
        config = PerformanceConfig()
        config.gpu = GpuConfig(backend="metal", device_id=1)

        d = config.to_dict()

        assert "gpu" in d
        assert d["gpu"]["backend"] == "metal"
        assert d["gpu"]["device_id"] == 1
        assert "thresholds" in d["gpu"]
        assert d["gpu"]["thresholds"]["min_grid_cells"] == 100_000

    def test_save_and_load_gpu_settings(self):
        """Test GPU settings survive TOML round-trip."""
        # Create config with custom GPU settings
        config = PerformanceConfig(
            cpu_cores=8,
            available_memory_gb=16.0,
            gpu=GpuConfig(
                backend="cuda",
                device_id=2,
                memory_limit_mb=8192,
                thresholds=GpuThresholds(
                    min_grid_cells=50_000,
                    min_components=30,
                    min_population=15,
                    min_trace_pairs=75,
                ),
            ),
        )

        # Save to temp file
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "performance.toml"
            config.save(config_path)

            # Verify file was written
            assert config_path.exists()
            content = config_path.read_text()

            # Check GPU section exists
            assert "[gpu]" in content
            assert 'backend = "cuda"' in content
            assert "device_id = 2" in content
            assert "memory_limit_mb = 8192" in content

            # Check GPU thresholds section exists
            assert "[gpu.thresholds]" in content
            assert "min_grid_cells = 50000" in content
            assert "min_components = 30" in content


class TestShouldUseGpu:
    """Tests for should_use_gpu helper function."""

    def test_cpu_backend_always_returns_false(self):
        """Test CPU backend always returns False."""
        config = PerformanceConfig()
        config.gpu = GpuConfig(backend="cpu")

        # Even large problems should use CPU when backend is cpu
        assert should_use_gpu(config, 1_000_000, "grid") is False
        assert should_use_gpu(config, 1000, "placement") is False

    def test_grid_threshold(self):
        """Test grid threshold logic."""
        config = PerformanceConfig()
        config.gpu = GpuConfig(
            backend="auto",
            thresholds=GpuThresholds(min_grid_cells=100_000),
        )

        # Below threshold: use CPU
        assert should_use_gpu(config, 50_000, "grid") is False
        assert should_use_gpu(config, 99_999, "grid") is False

        # At or above threshold: use GPU
        assert should_use_gpu(config, 100_000, "grid") is True
        assert should_use_gpu(config, 500_000, "grid") is True

    def test_placement_threshold(self):
        """Test placement threshold logic."""
        config = PerformanceConfig()
        config.gpu = GpuConfig(
            backend="cuda",
            thresholds=GpuThresholds(min_components=50),
        )

        assert should_use_gpu(config, 25, "placement") is False
        assert should_use_gpu(config, 50, "placement") is True
        assert should_use_gpu(config, 100, "placement") is True

    def test_evolutionary_threshold(self):
        """Test evolutionary optimizer threshold logic."""
        config = PerformanceConfig()
        config.gpu = GpuConfig(
            backend="metal",
            thresholds=GpuThresholds(min_population=20),
        )

        assert should_use_gpu(config, 10, "evolutionary") is False
        assert should_use_gpu(config, 20, "evolutionary") is True

    def test_signal_integrity_threshold(self):
        """Test signal integrity threshold logic."""
        config = PerformanceConfig()
        config.gpu = GpuConfig(
            backend="auto",
            thresholds=GpuThresholds(min_trace_pairs=100),
        )

        assert should_use_gpu(config, 50, "signal_integrity") is False
        assert should_use_gpu(config, 100, "signal_integrity") is True

    def test_unknown_problem_type_uses_gpu(self):
        """Test unknown problem type defaults to using GPU."""
        config = PerformanceConfig()
        config.gpu = GpuConfig(backend="auto")

        # Unknown type has threshold 0, so any size >= 0 uses GPU
        assert should_use_gpu(config, 0, "unknown_type") is True  # type: ignore
        assert should_use_gpu(config, 100, "unknown_type") is True  # type: ignore


class TestPerformanceConfigLoadWithGpu:
    """Tests for loading PerformanceConfig with GPU settings."""

    def test_load_without_gpu_section(self):
        """Test loading config file without GPU section uses defaults."""
        # Create a minimal config without GPU section
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "performance.toml"
            config_path.write_text('''
[calibration]
date = "2024-01-01"
cpu_cores = 8
memory_gb = 16.0

[routing]
monte_carlo_trials = 16
parallel_workers = 7

[grid]
max_memory_mb = 500
''')
            # We need to patch the config path
            import kicad_tools.performance as perf_module

            original_file = perf_module.PERFORMANCE_CONFIG_FILE
            try:
                perf_module.PERFORMANCE_CONFIG_FILE = config_path

                config = PerformanceConfig.load_calibrated()

                # GPU should have defaults
                assert config.gpu.backend == "auto"
                assert config.gpu.device_id == 0
                assert config.gpu.thresholds.min_grid_cells == 100_000
            finally:
                perf_module.PERFORMANCE_CONFIG_FILE = original_file

    def test_load_with_gpu_section(self):
        """Test loading config file with GPU section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "performance.toml"
            config_path.write_text('''
[calibration]
date = "2024-01-01"
cpu_cores = 8
memory_gb = 16.0

[routing]
monte_carlo_trials = 16
parallel_workers = 7

[grid]
max_memory_mb = 500

[gpu]
backend = "metal"
device_id = 1
memory_limit_mb = 4096

[gpu.thresholds]
min_grid_cells = 200000
min_components = 100
min_population = 50
min_trace_pairs = 200
''')
            import kicad_tools.performance as perf_module

            original_file = perf_module.PERFORMANCE_CONFIG_FILE
            try:
                perf_module.PERFORMANCE_CONFIG_FILE = config_path

                config = PerformanceConfig.load_calibrated()

                # GPU should have loaded values
                assert config.gpu.backend == "metal"
                assert config.gpu.device_id == 1
                assert config.gpu.memory_limit_mb == 4096
                assert config.gpu.thresholds.min_grid_cells == 200_000
                assert config.gpu.thresholds.min_components == 100
                assert config.gpu.thresholds.min_population == 50
                assert config.gpu.thresholds.min_trace_pairs == 200
            finally:
                perf_module.PERFORMANCE_CONFIG_FILE = original_file
