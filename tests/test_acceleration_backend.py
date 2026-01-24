"""Tests for GPU acceleration backend abstraction module."""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.acceleration import (
    ArrayBackend,
    BackendType,
    CPUBackend,
    CUDABackend,
    MetalBackend,
    detect_backends,
    get_backend,
    get_backend_info,
)


class TestBackendType:
    """Tests for BackendType enum."""

    def test_backend_type_values(self):
        """Test BackendType enum values."""
        assert BackendType.CUDA.value == "cuda"
        assert BackendType.METAL.value == "metal"
        assert BackendType.CPU.value == "cpu"

    def test_backend_type_from_string(self):
        """Test creating BackendType from string."""
        assert BackendType("cuda") == BackendType.CUDA
        assert BackendType("metal") == BackendType.METAL
        assert BackendType("cpu") == BackendType.CPU


class TestCPUBackend:
    """Tests for CPU backend."""

    @pytest.fixture
    def backend(self):
        """Create CPU backend instance."""
        return CPUBackend()

    def test_backend_type(self, backend):
        """Test backend type property."""
        assert backend.backend_type == BackendType.CPU

    def test_is_available(self, backend):
        """Test CPU backend is always available."""
        assert backend.is_available() is True

    def test_implements_protocol(self, backend):
        """Test CPU backend implements ArrayBackend protocol."""
        assert isinstance(backend, ArrayBackend)

    def test_array_from_list(self, backend):
        """Test creating array from list."""
        arr = backend.array([1, 2, 3])
        assert isinstance(arr, np.ndarray)
        np.testing.assert_array_equal(arr, [1, 2, 3])

    def test_array_with_dtype(self, backend):
        """Test creating array with specific dtype."""
        arr = backend.array([1, 2, 3], dtype=np.float32)
        assert arr.dtype == np.float32

    def test_zeros(self, backend):
        """Test creating zero-filled array."""
        arr = backend.zeros((3, 4))
        assert arr.shape == (3, 4)
        assert arr.dtype == np.float32
        assert np.all(arr == 0)

    def test_zeros_with_dtype(self, backend):
        """Test zeros with specific dtype."""
        arr = backend.zeros((2, 2), dtype=np.int32)
        assert arr.dtype == np.int32

    def test_ones(self, backend):
        """Test creating array filled with ones."""
        arr = backend.ones((2, 3))
        assert arr.shape == (2, 3)
        assert np.all(arr == 1)

    def test_empty(self, backend):
        """Test creating empty array."""
        arr = backend.empty((5, 5))
        assert arr.shape == (5, 5)

    def test_to_numpy(self, backend):
        """Test to_numpy returns numpy array."""
        arr = backend.array([1, 2, 3])
        result = backend.to_numpy(arr)
        assert isinstance(result, np.ndarray)
        np.testing.assert_array_equal(result, [1, 2, 3])

    def test_from_numpy(self, backend):
        """Test from_numpy returns numpy array."""
        np_arr = np.array([1, 2, 3])
        result = backend.from_numpy(np_arr)
        assert isinstance(result, np.ndarray)
        np.testing.assert_array_equal(result, np_arr)

    def test_synchronize(self, backend):
        """Test synchronize is a no-op for CPU."""
        # Should not raise
        backend.synchronize()


class TestCUDABackend:
    """Tests for CUDA backend."""

    @pytest.fixture
    def backend(self):
        """Create CUDA backend instance."""
        return CUDABackend()

    def test_backend_type(self, backend):
        """Test backend type property."""
        assert backend.backend_type == BackendType.CUDA

    def test_is_available_returns_bool(self, backend):
        """Test is_available returns boolean."""
        result = backend.is_available()
        assert isinstance(result, bool)

    @pytest.mark.skipif(
        not CUDABackend().is_available(),
        reason="CUDA not available",
    )
    def test_implements_protocol_when_available(self, backend):
        """Test CUDA backend implements ArrayBackend protocol when available."""
        assert isinstance(backend, ArrayBackend)

    @pytest.mark.skipif(
        not CUDABackend().is_available(),
        reason="CUDA not available",
    )
    def test_array_operations_when_available(self, backend):
        """Test array operations when CUDA is available."""
        arr = backend.zeros((100, 100))
        result = backend.to_numpy(arr)
        assert isinstance(result, np.ndarray)
        assert result.shape == (100, 100)

    def test_raises_when_unavailable(self):
        """Test proper error when CUDA unavailable."""
        backend = CUDABackend()
        if not backend.is_available():
            with pytest.raises(RuntimeError, match="CuPy"):
                backend.zeros((10, 10))


class TestMetalBackend:
    """Tests for Metal backend."""

    @pytest.fixture
    def backend(self):
        """Create Metal backend instance."""
        return MetalBackend()

    def test_backend_type(self, backend):
        """Test backend type property."""
        assert backend.backend_type == BackendType.METAL

    def test_is_available_returns_bool(self, backend):
        """Test is_available returns boolean."""
        result = backend.is_available()
        assert isinstance(result, bool)

    @pytest.mark.skipif(
        not MetalBackend().is_available(),
        reason="Metal not available",
    )
    def test_implements_protocol_when_available(self, backend):
        """Test Metal backend implements ArrayBackend protocol when available."""
        assert isinstance(backend, ArrayBackend)

    @pytest.mark.skipif(
        not MetalBackend().is_available(),
        reason="Metal not available",
    )
    def test_array_operations_when_available(self, backend):
        """Test array operations when Metal is available."""
        arr = backend.zeros((100, 100))
        result = backend.to_numpy(arr)
        assert isinstance(result, np.ndarray)
        assert result.shape == (100, 100)

    def test_raises_when_unavailable(self):
        """Test proper error when Metal unavailable."""
        import platform

        backend = MetalBackend()
        if not backend.is_available():
            expected_msg = "MLX" if platform.system() == "Darwin" else "MLX"
            with pytest.raises(RuntimeError, match=expected_msg):
                backend.zeros((10, 10))


class TestDetection:
    """Tests for backend detection functions."""

    def test_detect_backends_returns_list(self):
        """Test detect_backends returns a list."""
        result = detect_backends()
        assert isinstance(result, list)

    def test_detect_backends_contains_cpu(self):
        """Test CPU is always in detected backends."""
        result = detect_backends()
        assert BackendType.CPU in result

    def test_detect_backends_cpu_is_last(self):
        """Test CPU is last in priority order."""
        result = detect_backends()
        assert result[-1] == BackendType.CPU

    def test_get_backend_returns_backend(self):
        """Test get_backend returns an ArrayBackend."""
        backend = get_backend()
        assert isinstance(backend, ArrayBackend)

    def test_get_backend_cpu_always_works(self):
        """Test get_backend with CPU always works."""
        backend = get_backend(BackendType.CPU)
        assert backend.backend_type == BackendType.CPU

    def test_get_backend_accepts_string(self):
        """Test get_backend accepts string backend type."""
        backend = get_backend("cpu")
        assert backend.backend_type == BackendType.CPU

    def test_get_backend_invalid_string(self):
        """Test get_backend raises for invalid string."""
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend("invalid_backend")

    def test_get_backend_unavailable_cuda(self):
        """Test get_backend raises for unavailable CUDA."""
        if not CUDABackend().is_available():
            with pytest.raises(ValueError, match="CUDA"):
                get_backend(BackendType.CUDA)

    def test_get_backend_unavailable_metal(self):
        """Test get_backend raises for unavailable Metal."""
        if not MetalBackend().is_available():
            with pytest.raises(ValueError, match="Metal"):
                get_backend(BackendType.METAL)

    def test_get_backend_info_returns_dict(self):
        """Test get_backend_info returns a dictionary."""
        info = get_backend_info()
        assert isinstance(info, dict)

    def test_get_backend_info_contains_required_keys(self):
        """Test get_backend_info contains required keys."""
        info = get_backend_info()
        assert "platform" in info
        assert "cuda_available" in info
        assert "metal_available" in info
        assert "cpu_available" in info
        assert "preferred_backend" in info

    def test_get_backend_info_cpu_always_available(self):
        """Test CPU is always reported as available."""
        info = get_backend_info()
        assert info["cpu_available"] is True


class TestBackendInteroperability:
    """Tests for backend interoperability with numpy."""

    @pytest.fixture
    def backend(self):
        """Get the best available backend."""
        return get_backend()

    def test_round_trip(self, backend):
        """Test numpy -> backend -> numpy round trip."""
        original = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32)

        # To backend
        on_backend = backend.from_numpy(original)

        # Back to numpy
        result = backend.to_numpy(on_backend)

        np.testing.assert_array_equal(result, original)

    def test_zeros_to_numpy(self, backend):
        """Test zeros array converts correctly to numpy."""
        arr = backend.zeros((10, 10))
        result = backend.to_numpy(arr)
        assert np.all(result == 0)

    def test_ones_to_numpy(self, backend):
        """Test ones array converts correctly to numpy."""
        arr = backend.ones((10, 10))
        result = backend.to_numpy(arr)
        assert np.all(result == 1)

    def test_dtype_preservation(self, backend):
        """Test dtype is preserved through operations."""
        for dtype in [np.float32, np.int32]:
            arr = backend.zeros((5, 5), dtype=dtype)
            result = backend.to_numpy(arr)
            # Note: Metal may not preserve all dtypes exactly
            if backend.backend_type != BackendType.METAL:
                assert result.dtype == dtype
