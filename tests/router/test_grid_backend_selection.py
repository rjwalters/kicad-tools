"""Issue #8609: pure-Python Router must not run on non-NumPy grid arrays."""

from __future__ import annotations

import numpy as np

from kicad_tools.acceleration import BackendType
from kicad_tools.performance import PerformanceConfig
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules


class _FakeArray:
    """Non-ndarray wrapper standing in for an MLX/CuPy device array."""

    def __init__(self, arr):
        self._arr = np.asarray(arr)

    def __array__(self, dtype=None, copy=None):
        return self._arr


class _FakeMetalBackend:
    """Mimics ``acceleration.backend.ArrayBackend`` on Metal.

    Deliberately has NO ``cuda`` / ``_mx`` attributes (like the real
    ``ArrayBackend``), only ``backend_type`` -- the shape that made the old
    attribute probing in ``_select_backend`` misclassify MLX as CPU.
    """

    backend_type = BackendType.METAL

    def zeros(self, shape, dtype=None):
        return _FakeArray(np.zeros(shape, dtype=dtype))


def _make_fake_gpu_grid(monkeypatch, rules) -> RoutingGrid:
    """Build a grid through the REAL ``_select_backend`` with a fake backend."""
    monkeypatch.setattr(
        "kicad_tools.router.grid.get_backend", lambda config=None: _FakeMetalBackend()
    )
    monkeypatch.setattr("kicad_tools.router.grid.should_use_gpu", lambda *a, **k: True)
    monkeypatch.setattr("kicad_tools.router.grid.check_memory_available", lambda *a, **k: True)
    monkeypatch.setattr("kicad_tools.router.grid.to_numpy", lambda a: np.asarray(a))
    return RoutingGrid(10, 10, rules, config=PerformanceConfig())


def test_select_backend_reports_array_backend_type(monkeypatch):
    grid = _make_fake_gpu_grid(monkeypatch, DesignRules())
    assert grid._backend_type == BackendType.METAL
    assert grid.uses_gpu
    assert not isinstance(grid._blocked, np.ndarray)


def test_router_forces_numpy_arrays(monkeypatch):
    rules = DesignRules()
    grid = _make_fake_gpu_grid(monkeypatch, rules)
    Router(grid, rules)
    assert not grid.uses_gpu
    assert grid._backend is np
    for name in ("_blocked", "_net", "_usage_count", "_history_cost"):
        assert isinstance(getattr(grid, name), np.ndarray), name


def test_ensure_numpy_converts_even_when_tagged_cpu(monkeypatch):
    """Arrays decide, not the backend tag (the original misclassified-MLX state)."""
    rules = DesignRules()
    grid = _make_fake_gpu_grid(monkeypatch, rules)
    grid._backend_type = BackendType.CPU  # tag lies; arrays are still non-NumPy
    grid.ensure_numpy_arrays()
    assert isinstance(grid._blocked, np.ndarray)
    assert grid._backend is np


def test_cpu_grid_unchanged():
    rules = DesignRules()
    grid = RoutingGrid(10, 10, rules)
    blocked = grid._blocked
    Router(grid, rules)
    assert grid._blocked is blocked
