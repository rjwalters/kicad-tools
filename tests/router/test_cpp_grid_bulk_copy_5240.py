"""``CppGrid.from_routing_grid``'s bulk blocked-cell copy visits only blocked cells.

Issue #5240 (CI runtime): an instrumented board-06 re-route showed a single
``CppGrid.from_routing_grid`` call costing 19-23 s on the board's
2001 x 1601 x 4 (12.8M-cell) grid, while the C++ coupled A* search it feeds
burned its whole 1000-iteration budget in 0.03 s.  ``CoupledPathfinder`` is
constructed once per differential pair, so board 06's nine-pair diff-pair
pre-phase paid that marshalling cost nine times.

The cost was the bulk copy itself: a triple Python loop over every cell in
the board that built a throwaway ``_CellView`` per cell purely to read
``.blocked`` and discard the ~84% of cells that are free.  The fix selects
the blocked cells with ``np.nonzero`` and gathers their attributes with
NumPy fancy indexing, visiting only the cells that produce a
``mark_blocked`` call.

These tests are a same-values correctness cross-check against an
independent per-cell reference walk (the pattern used by the sibling
fixes on this issue, #5253/#5256/#5267/#5269/#5275), not a timing
assertion -- CI runner variance makes wall-clock assertions flaky.  The
reproducible local microbenchmark is in the PR description.

What must hold for the optimization to be behaviour-preserving:

1. The SET of cells marked blocked in the C++ grid is unchanged.
2. Each marked cell carries the same ``net`` / ``is_obstacle`` /
   ``pad_blocked`` payload the per-cell walk would have passed.
3. ``mark_blocked`` is called in the same ORDER (layer, then y, then x,
   ascending), since C++-side bookkeeping may depend on call sequence.
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")


def _make_grid(seed: int = 20260912) -> RoutingGrid:
    """A small grid with a deterministic, mixed blocked/free population."""
    rules = DesignRules(grid_resolution=0.5)
    grid = RoutingGrid(12.0, 9.0, rules)
    rng = np.random.default_rng(seed)
    shape = (grid.num_layers, grid.rows, grid.cols)
    assert grid._blocked.shape == shape

    blocked = rng.random(shape) < 0.35
    # Guarantee a fully blocked edge band and at least one blocked cell on
    # every layer, so the flattened index never degenerates to "empty".
    blocked[:, 0, :] = True
    blocked[:, -1, :] = True
    grid._blocked = blocked
    # Payloads deliberately carry values on FREE cells too: the copy must
    # ignore those, exactly as the per-cell walk's ``if py_cell.blocked``
    # guard did.
    grid._net = rng.integers(0, 25, size=shape, dtype=np.int32)
    grid._is_obstacle = rng.random(shape) < 0.4
    grid._pad_blocked = rng.random(shape) < 0.25
    return grid


def _reference_marks(grid: RoutingGrid) -> list[tuple[int, int, int, int, bool, bool]]:
    """Per-cell reference walk: what the pre-#5240 bulk copy would have emitted.

    Reads through ``cell_at`` (the public ``_CellView`` accessor), so this is
    an independent path from the vectorised NumPy gather under test.
    """
    marks: list[tuple[int, int, int, int, bool, bool]] = []
    for layer in range(grid.num_layers):
        for y in range(grid.rows):
            for x in range(grid.cols):
                cell = grid.cell_at(layer, y, x)
                if cell.blocked:
                    marks.append(
                        (
                            x,
                            y,
                            layer,
                            int(cell.net),
                            bool(cell.is_obstacle),
                            bool(grid._pad_blocked[layer, y, x]),
                        )
                    )
    return marks


def _actual_marks(grid: RoutingGrid) -> list[tuple[int, int, int, int, bool, bool]]:
    """Recorded ``mark_blocked`` calls made by the real ``from_routing_grid``."""
    recorded: list[tuple[int, int, int, int, bool, bool]] = []
    real_ctor = CppGrid.__init__

    def recording_ctor(self, *args, **kwargs):
        real_ctor(self, *args, **kwargs)
        real_mark = self._impl.mark_blocked

        def mark_blocked(x, y, layer, net, is_obstacle, pad_blocked):
            recorded.append(
                (
                    int(x),
                    int(y),
                    int(layer),
                    int(net),
                    bool(is_obstacle),
                    bool(pad_blocked),
                )
            )
            return real_mark(x, y, layer, net, is_obstacle, pad_blocked)

        # ``_impl`` is a nanobind object, so patch a Python-level shim onto
        # the CppGrid wrapper's captured reference instead of the C++ type.
        self._impl = _ImplProxy(self._impl, mark_blocked)

    CppGrid.__init__ = recording_ctor  # type: ignore[method-assign]
    try:
        CppGrid.from_routing_grid(grid)
    finally:
        CppGrid.__init__ = real_ctor  # type: ignore[method-assign]
    return recorded


class _ImplProxy:
    """Forwards every attribute to the real ``Grid3D`` but intercepts one method."""

    def __init__(self, impl, mark_blocked):
        object.__setattr__(self, "_impl", impl)
        object.__setattr__(self, "mark_blocked", mark_blocked)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_impl"), name)


def test_bulk_copy_marks_exactly_the_blocked_cells():
    """Same cells, same payloads, same order as the per-cell reference walk."""
    grid = _make_grid()
    expected = _reference_marks(grid)
    # Sanity: the fixture must exercise both branches of the guard.
    total_cells = grid.num_layers * grid.rows * grid.cols
    assert 0 < len(expected) < total_cells

    actual = _actual_marks(_make_grid())
    assert actual == expected


def test_bulk_copy_payloads_match_grid_arrays():
    """Each marked cell's net/is_obstacle/pad_blocked come from its own cell."""
    grid = _make_grid()
    for x, y, layer, net, is_obstacle, pad_blocked in _actual_marks(grid):
        assert bool(grid._blocked[layer, y, x]) is True
        assert net == int(grid._net[layer, y, x])
        assert is_obstacle == bool(grid._is_obstacle[layer, y, x])
        assert pad_blocked == bool(grid._pad_blocked[layer, y, x])


def test_bulk_copy_reaches_the_cpp_grid():
    """The resulting C++ grid reports the same blocked-cell count as Python."""
    grid = _make_grid()
    cpp_grid = CppGrid.from_routing_grid(grid)
    assert cpp_grid._impl.count_blocked() == int(np.count_nonzero(grid._blocked))
    # Spot-check payload round-trip through the real (unproxied) C++ grid.
    layer_idx, y_idx, x_idx = np.nonzero(grid._blocked)
    for i in range(0, len(x_idx), max(1, len(x_idx) // 50)):
        layer, y, x = int(layer_idx[i]), int(y_idx[i]), int(x_idx[i])
        cell = cpp_grid._impl.at(x, y, layer)
        assert cell.blocked is True
        assert cell.net == int(grid._net[layer, y, x])
        assert cell.is_obstacle == bool(grid._is_obstacle[layer, y, x])
        assert cell.pad_blocked == bool(grid._pad_blocked[layer, y, x])


def test_bulk_copy_with_no_blocked_cells_marks_nothing():
    """A grid with an all-free blocked plane emits zero ``mark_blocked`` calls."""
    grid = _make_grid()
    grid._blocked = np.zeros_like(grid._blocked)
    assert _actual_marks(grid) == []
    assert CppGrid.from_routing_grid(grid)._impl.count_blocked() == 0
