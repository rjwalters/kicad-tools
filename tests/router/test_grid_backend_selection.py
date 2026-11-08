"""Issue #8609: pure-Python Router must not run on non-NumPy grid arrays."""

from __future__ import annotations

import numpy as np

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules


class _FakeArray:
    """Non-ndarray wrapper standing in for an MLX/CuPy device array."""

    def __init__(self, arr):
        self._arr = np.asarray(arr)

    def __array__(self, dtype=None, copy=None):
        return self._arr


def _fake_to_gpu(grid: RoutingGrid) -> None:
    for name in (
        "_blocked",
        "_net",
        "_usage_count",
        "_history_cost",
        "_is_obstacle",
        "_is_zone",
        "_pad_blocked",
        "_original_net",
    ):
        setattr(grid, name, _FakeArray(getattr(grid, name)))
    grid._backend_type = type(grid._backend_type).METAL
    grid._backend = object()


def test_router_forces_numpy_arrays(monkeypatch):
    monkeypatch.setattr("kicad_tools.router.grid.to_numpy", lambda a: np.asarray(a))
    rules = DesignRules()
    grid = RoutingGrid(10, 10, rules)
    _fake_to_gpu(grid)
    assert grid.uses_gpu
    Router(grid, rules)
    assert not grid.uses_gpu
    assert grid._backend is np
    for name in ("_blocked", "_net", "_usage_count", "_history_cost"):
        assert isinstance(getattr(grid, name), np.ndarray), name


def test_cpu_grid_unchanged():
    rules = DesignRules()
    grid = RoutingGrid(10, 10, rules)
    blocked = grid._blocked
    Router(grid, rules)
    assert grid._blocked is blocked
