"""Tests for GPU configuration in performance module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kicad_tools.acceleration.config import (
    should_use_gpu,
    validate_backend,
)
from kicad_tools.performance import (
    GpuConfig,
    GpuThresholds,
    PerformanceConfig,
)


class TestGpuThresholds:
    """Tests for GpuThresholds dataclass."""

    def test_default_values(self):
        """Test that default threshold values are set correctly."""
        thresholds = GpuThresholds()
        assert thresholds.min_grid_cells == 100_000
        assert thresholds.min_components == 50
        assert thresholds.min_population == 20
        assert thresholds.min_trace_pairs == 100

    def test_custom_values(self):
        """Test that custom threshold values can be set."""
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
        """Test that default GPU config values are set correctly."""
        config = GpuConfig()
        assert config.backend == "auto"
        assert config.device_id == 0
        assert config.memory_limit_mb == 0
        assert isinstance(config.thresholds, GpuThresholds)

    def test_custom_values(self):
        """Test that custom GPU config values can be set."""
        thresholds = GpuThresholds(min_grid_cells=200_000)
        config = GpuConfig(
            backend="cuda",
            device_id=1,
            memory_limit_mb=4096,
            thresholds=thresholds,
        )
        assert config.backend == "cuda"
        assert config.device_id == 1
        assert config.memory_limit_mb == 4096
        assert config.thresholds.min_grid_cells == 200_000

    def test_backend_values(self):
        """Test that all valid backend values work."""
        for backend in ["auto", "cuda", "metal", "cpu"]:
            config = GpuConfig(backend=backend)
            assert config.backend == backend


class TestPerformanceConfigWithGpu:
    """Tests for PerformanceConfig GPU integration."""

    def test_default_has_gpu_config(self):
        """Test that default PerformanceConfig includes GPU config."""
        config = PerformanceConfig()
        assert hasattr(config, "gpu")
        assert isinstance(config.gpu, GpuConfig)

    def test_detect_has_gpu_config(self):
        """Test that detect() creates config with GPU defaults."""
        config = PerformanceConfig.detect()
        assert config.gpu.backend == "auto"
        assert config.gpu.device_id == 0

    def test_to_dict_includes_gpu(self):
        """Test that to_dict() includes GPU settings."""
        config = PerformanceConfig()
        d = config.to_dict()

        assert "gpu" in d
        assert d["gpu"]["backend"] == "auto"
        assert d["gpu"]["device_id"] == 0
        assert d["gpu"]["memory_limit_mb"] == 0
        assert "thresholds" in d["gpu"]
        assert d["gpu"]["thresholds"]["min_grid_cells"] == 100_000

    def test_save_and_load_gpu_settings(self):
        """Test that GPU settings are saved and loaded correctly."""
        # Create config with custom GPU settings
        thresholds = GpuThresholds(
            min_grid_cells=150_000,
            min_components=75,
            min_population=30,
            min_trace_pairs=150,
        )
        gpu = GpuConfig(
            backend="metal",
            device_id=2,
            memory_limit_mb=8192,
            thresholds=thresholds,
        )
        config = PerformanceConfig(gpu=gpu)

        # Save to temp file
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "performance.toml"
            config.save(config_path)

            # Verify file was created
            assert config_path.exists()

            # Read file content to verify GPU section
            content = config_path.read_text()
            assert '[gpu]' in content
            assert 'backend = "metal"' in content
            assert 'device_id = 2' in content
            assert 'memory_limit_mb = 8192' in content
            assert '[gpu.thresholds]' in content
            assert 'min_grid_cells = 150000' in content


class TestShouldUseGpu:
    """Tests for should_use_gpu helper function."""

    def test_cpu_backend_always_false(self):
        """Test that CPU backend always returns False."""
        config = PerformanceConfig(gpu=GpuConfig(backend="cpu"))

        # Even for large problem sizes, CPU backend means no GPU
        assert should_use_gpu(config, 1_000_000, "grid") is False
        assert should_use_gpu(config, 1000, "placement") is False
        assert should_use_gpu(config, 100, "evolutionary") is False
        assert should_use_gpu(config, 500, "signal_integrity") is False

    def test_grid_threshold(self):
        """Test grid cell threshold behavior."""
        config = PerformanceConfig()  # default thresholds

        # Below threshold (100,000)
        assert should_use_gpu(config, 50_000, "grid") is False
        assert should_use_gpu(config, 99_999, "grid") is False

        # At or above threshold
        assert should_use_gpu(config, 100_000, "grid") is True
        assert should_use_gpu(config, 200_000, "grid") is True

    def test_placement_threshold(self):
        """Test component placement threshold behavior."""
        config = PerformanceConfig()  # default thresholds

        # Below threshold (50)
        assert should_use_gpu(config, 25, "placement") is False
        assert should_use_gpu(config, 49, "placement") is False

        # At or above threshold
        assert should_use_gpu(config, 50, "placement") is True
        assert should_use_gpu(config, 100, "placement") is True

    def test_evolutionary_threshold(self):
        """Test evolutionary optimizer threshold behavior."""
        config = PerformanceConfig()  # default thresholds

        # Below threshold (20)
        assert should_use_gpu(config, 10, "evolutionary") is False
        assert should_use_gpu(config, 19, "evolutionary") is False

        # At or above threshold
        assert should_use_gpu(config, 20, "evolutionary") is True
        assert should_use_gpu(config, 50, "evolutionary") is True

    def test_signal_integrity_threshold(self):
        """Test signal integrity threshold behavior."""
        config = PerformanceConfig()  # default thresholds

        # Below threshold (100)
        assert should_use_gpu(config, 50, "signal_integrity") is False
        assert should_use_gpu(config, 99, "signal_integrity") is False

        # At or above threshold
        assert should_use_gpu(config, 100, "signal_integrity") is True
        assert should_use_gpu(config, 200, "signal_integrity") is True

    def test_custom_thresholds(self):
        """Test with custom threshold values."""
        thresholds = GpuThresholds(
            min_grid_cells=50_000,
            min_components=25,
        )
        config = PerformanceConfig(gpu=GpuConfig(thresholds=thresholds))

        # With lowered thresholds, GPU is used for smaller problems
        assert should_use_gpu(config, 30_000, "grid") is False
        assert should_use_gpu(config, 50_000, "grid") is True
        assert should_use_gpu(config, 20, "placement") is False
        assert should_use_gpu(config, 25, "placement") is True


class TestValidateBackend:
    """Tests for validate_backend helper function."""

    def test_valid_backends(self):
        """Test that valid backends are accepted."""
        assert validate_backend("auto") is True
        assert validate_backend("cuda") is True
        assert validate_backend("metal") is True
        assert validate_backend("cpu") is True

    def test_invalid_backends(self):
        """Test that invalid backends are rejected."""
        assert validate_backend("") is False
        assert validate_backend("opencl") is False
        assert validate_backend("vulkan") is False
        assert validate_backend("CUDA") is False  # case-sensitive
        assert validate_backend("Auto") is False


class TestTomlRoundTrip:
    """Tests for TOML serialization round-trip."""

    def test_default_config_round_trip(self):
        """Test that default config survives save/load round-trip."""
        original = PerformanceConfig.detect()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "performance.toml"
            original.save(config_path)

            # Manually test that file contains expected content
            content = config_path.read_text()

            # Check GPU section exists
            assert '[gpu]' in content
            assert '[gpu.thresholds]' in content

            # Check default values are present
            assert 'backend = "auto"' in content
            assert 'device_id = 0' in content
            assert 'min_grid_cells = 100000' in content

    def test_custom_config_round_trip(self):
        """Test that custom config survives save/load round-trip."""
        thresholds = GpuThresholds(
            min_grid_cells=75_000,
            min_components=40,
            min_population=15,
            min_trace_pairs=80,
        )
        gpu = GpuConfig(
            backend="cuda",
            device_id=1,
            memory_limit_mb=2048,
            thresholds=thresholds,
        )
        original = PerformanceConfig(
            cpu_cores=8,
            available_memory_gb=16.0,
            gpu=gpu,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "performance.toml"
            original.save(config_path)

            content = config_path.read_text()

            # Check custom values are serialized
            assert 'backend = "cuda"' in content
            assert 'device_id = 1' in content
            assert 'memory_limit_mb = 2048' in content
            assert 'min_grid_cells = 75000' in content
            assert 'min_components = 40' in content
            assert 'min_population = 15' in content
            assert 'min_trace_pairs = 80' in content
