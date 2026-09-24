"""Exact-parity controls for the optimizer's per-cell obstacle scan (#5240).

``VectorCollisionChecker._check_obstacles_clear`` reads ``RoutingGrid``'s four
backing NumPy planes (``_blocked`` / ``_is_obstacle`` / ``_pad_blocked`` /
``_net``) directly instead of materialising a ``_CellView`` per cell through
``cell_at()``.  That is a pure performance change, so it needs a control that
pins it to the *previous* semantics rather than to its own behaviour.

``_reference_check_obstacles_clear`` below is a faithful transcription of the
pre-change loop -- same traversal, same ``cell_at`` + ``_CellView`` property
reads, same branch order.  Every test here asserts the production method and
that reference agree exactly, on a **real** ``RoutingGrid`` (the mock grids in
``tests/test_router_collision.py`` cannot distinguish the two code paths,
which is precisely why this file exists).

The scenarios deliberately cover each branch the scan can take:

* a hard obstacle on a foreign net             -> blocked
* pad metal whose net was rewritten to 0       -> blocked (Issue #2757)
* an own-net pad (``cell.net == exclude_net``) -> clear
* ``blocked`` route occupancy that is neither
  an obstacle nor pad metal                    -> clear (soft occupancy)
* probes that run off the grid                 -> clipped, not an error

plus a seeded randomized population, which is the part that would catch an
index transposition (``[y, x]`` vs ``[x, y]``) or a wrong plane.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.optimizer.collision import (
    VectorCollisionChecker,
    _iter_dilated_line_cells,
)
from kicad_tools.router.rules import DesignRules

BOARD_W = 12.0
BOARD_H = 9.0


def _reference_check_obstacles_clear(
    checker: VectorCollisionChecker,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer_idx: int,
    width: float,
    exclude_net: int,
) -> bool:
    """The pre-#5240 ``cell_at`` + ``_CellView`` obstacle scan, verbatim.

    Kept structurally identical to the version this file certifies against so
    a future reader can diff the two by eye.
    """
    grid = checker.grid
    gx1, gy1 = grid.world_to_grid(x1, y1)
    gx2, gy2 = grid.world_to_grid(x2, y2)

    total_clearance = width / 2 + grid.rules.trace_clearance
    clearance_cells = int(total_clearance / grid.resolution) + 1

    for check_x, check_y in _iter_dilated_line_cells(gx1, gy1, gx2, gy2, clearance_cells):
        if not (0 <= check_x < grid.cols and 0 <= check_y < grid.rows):
            continue
        cell = grid.cell_at(layer_idx, check_y, check_x)
        if cell.blocked and (cell.is_obstacle or cell.pad_blocked):
            if cell.net != 0 and cell.net == exclude_net:
                continue
            if cell.pad_blocked and cell.net == exclude_net:
                continue
            return False

    return True


def _make_grid(resolution: float = 0.1) -> RoutingGrid:
    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=0.2,
        trace_clearance=0.127,
    )
    return RoutingGrid(BOARD_W, BOARD_H, rules)


def _paint_cell(
    grid: RoutingGrid,
    layer_idx: int,
    gx: int,
    gy: int,
    *,
    blocked: bool = True,
    is_obstacle: bool = False,
    pad_blocked: bool = False,
    net: int = 0,
) -> None:
    """Set one cell through the public ``_CellView`` setters.

    Using the setters (not the arrays) keeps the fixture honest: the test
    populates the grid the way production code does and only the *read* path
    is under test.
    """
    cell = grid.cell_at(layer_idx, gy, gx)
    cell.blocked = blocked
    cell.is_obstacle = is_obstacle
    cell.pad_blocked = pad_blocked
    cell.net = net


def _assert_parity(
    checker: VectorCollisionChecker,
    probes: list[tuple[float, float, float, float, int]],
    *,
    layer_idx: int = 0,
    width: float = 0.2,
) -> list[bool]:
    """Assert production == reference for every probe; return the verdicts."""
    verdicts: list[bool] = []
    for x1, y1, x2, y2, exclude_net in probes:
        got = checker._check_obstacles_clear(x1, y1, x2, y2, layer_idx, width, exclude_net)
        want = _reference_check_obstacles_clear(
            checker, x1, y1, x2, y2, layer_idx, width, exclude_net
        )
        assert got == want, (
            f"array-read scan disagrees with the cell_at reference for "
            f"({x1}, {y1})->({x2}, {y2}) exclude_net={exclude_net}: "
            f"got {got!r}, reference {want!r}"
        )
        assert isinstance(got, bool)
        verdicts.append(got)
    return verdicts


class TestObstacleScanBranchParity:
    """One test per branch the scan can take, each pinned to the reference."""

    def test_foreign_net_hard_obstacle_blocks(self) -> None:
        grid = _make_grid()
        gx, gy = grid.world_to_grid(5.0, 4.0)
        _paint_cell(grid, 0, gx, gy, is_obstacle=True, net=7)
        checker = VectorCollisionChecker(grid)

        probes = [(4.0, 4.0, 6.0, 4.0, 26)]
        assert _assert_parity(checker, probes) == [False]

    def test_skip_net_pad_metal_blocks(self) -> None:
        """Issue #2757: pad copper rewritten to net 0 still blocks."""
        grid = _make_grid()
        gx, gy = grid.world_to_grid(5.0, 4.0)
        _paint_cell(grid, 0, gx, gy, pad_blocked=True, is_obstacle=False, net=0)
        checker = VectorCollisionChecker(grid)

        probes = [(4.0, 4.0, 6.0, 4.0, 26)]
        assert _assert_parity(checker, probes) == [False]

    def test_own_net_pad_is_clear(self) -> None:
        grid = _make_grid()
        gx, gy = grid.world_to_grid(5.0, 4.0)
        _paint_cell(grid, 0, gx, gy, pad_blocked=True, is_obstacle=True, net=26)
        checker = VectorCollisionChecker(grid)

        probes = [(4.0, 4.0, 6.0, 4.0, 26)]
        assert _assert_parity(checker, probes) == [True]

    def test_soft_route_occupancy_is_clear(self) -> None:
        """``blocked`` alone (route occupancy) is not a hard obstacle."""
        grid = _make_grid()
        gx, gy = grid.world_to_grid(5.0, 4.0)
        _paint_cell(grid, 0, gx, gy, is_obstacle=False, pad_blocked=False, net=9)
        checker = VectorCollisionChecker(grid)

        probes = [(4.0, 4.0, 6.0, 4.0, 26)]
        assert _assert_parity(checker, probes) == [True]

    def test_unblocked_obstacle_flag_is_clear(self) -> None:
        """``is_obstacle`` without ``blocked`` must stay clear.

        The scan's outer guard is ``blocked``; a cell with the obstacle flag
        set but ``blocked=False`` is not reachable in production, but pinning
        it keeps the new short-circuit (``if not blocked: continue``) honest.
        """
        grid = _make_grid()
        gx, gy = grid.world_to_grid(5.0, 4.0)
        _paint_cell(grid, 0, gx, gy, blocked=False, is_obstacle=True, net=7)
        checker = VectorCollisionChecker(grid)

        probes = [(4.0, 4.0, 6.0, 4.0, 26)]
        assert _assert_parity(checker, probes) == [True]

    def test_obstacle_on_other_layer_does_not_block(self) -> None:
        """The layer plane is selected once outside the loop -- pin the index."""
        grid = _make_grid()
        gx, gy = grid.world_to_grid(5.0, 4.0)
        _paint_cell(grid, 1, gx, gy, is_obstacle=True, net=7)
        checker = VectorCollisionChecker(grid)

        probes = [(4.0, 4.0, 6.0, 4.0, 26)]
        assert _assert_parity(checker, probes, layer_idx=0) == [True]
        assert _assert_parity(checker, probes, layer_idx=1) == [False]

    def test_probes_running_off_grid_are_clipped(self) -> None:
        grid = _make_grid()
        checker = VectorCollisionChecker(grid)

        probes = [
            (-3.0, -3.0, 1.0, 1.0, 26),
            (BOARD_W - 1.0, BOARD_H - 1.0, BOARD_W + 4.0, BOARD_H + 4.0, 26),
            (-5.0, 4.0, BOARD_W + 5.0, 4.0, 26),
        ]
        assert _assert_parity(checker, probes) == [True, True, True]

    def test_asymmetric_index_population_pins_row_column_order(self) -> None:
        """A single obstacle at a non-square (gx != gy) cell.

        A transposed ``[check_x, check_y]`` read would find nothing here and
        silently report the path clear, so this is the cheapest possible
        guard against that class of mistake.
        """
        grid = _make_grid()
        gx, gy = grid.world_to_grid(9.0, 2.0)
        assert gx != gy
        _paint_cell(grid, 0, gx, gy, is_obstacle=True, net=7)
        checker = VectorCollisionChecker(grid)

        blocking = [(8.0, 2.0, 10.0, 2.0, 26)]
        # The transposed cell -- (gy, gx) read as (y, x) -- must NOT block.
        transposed_world = (
            grid.origin_x + gy * grid.resolution,
            grid.origin_y + gx * grid.resolution,
        )
        clear = [
            (
                transposed_world[0] - 1.0,
                transposed_world[1],
                transposed_world[0] + 1.0,
                transposed_world[1],
                26,
            )
        ]
        assert _assert_parity(checker, blocking) == [False]
        assert _assert_parity(checker, clear) == [True]


class TestObstacleScanRandomizedParity:
    """Seeded population + probe sweep: the broad net for read-path mistakes."""

    @pytest.mark.parametrize("seed", [1, 7, 2026])
    def test_randomized_population_matches_reference(self, seed: int) -> None:
        rng = random.Random(seed)
        grid = _make_grid()
        checker = VectorCollisionChecker(grid)

        nets = [0, 3, 7, 26]
        for _ in range(900):
            layer_idx = rng.randrange(grid.num_layers)
            gx = rng.randrange(grid.cols)
            gy = rng.randrange(grid.rows)
            kind = rng.random()
            _paint_cell(
                grid,
                layer_idx,
                gx,
                gy,
                blocked=kind > 0.1,
                is_obstacle=kind > 0.7,
                pad_blocked=0.35 < kind <= 0.7,
                net=rng.choice(nets),
            )

        probes: list[tuple[float, float, float, float, int]] = []
        for _ in range(240):
            x1 = rng.uniform(-1.0, BOARD_W + 1.0)
            y1 = rng.uniform(-1.0, BOARD_H + 1.0)
            x2 = x1 + rng.uniform(-3.0, 3.0)
            y2 = y1 + rng.uniform(-3.0, 3.0)
            probes.append((x1, y1, x2, y2, rng.choice(nets)))

        for layer_idx in range(grid.num_layers):
            verdicts = _assert_parity(checker, probes, layer_idx=layer_idx)
            # A parity assertion over probes that all answer the same way
            # would be vacuous -- require both verdicts to be exercised.
            assert any(verdicts), "no probe was reported clear; population is too dense"
            assert not all(verdicts), "no probe was reported blocked; population is too sparse"

    def test_width_sweep_matches_reference(self) -> None:
        """Clearance width drives ``clearance_cells``; sweep it explicitly."""
        rng = random.Random(99)
        grid = _make_grid(resolution=0.05)
        checker = VectorCollisionChecker(grid)

        for _ in range(400):
            _paint_cell(
                grid,
                0,
                rng.randrange(grid.cols),
                rng.randrange(grid.rows),
                is_obstacle=rng.random() > 0.5,
                pad_blocked=rng.random() > 0.5,
                net=rng.choice([0, 5, 26]),
            )

        probes = [(2.0, 2.0, 4.5, 3.25, 26), (1.0, 7.0, 8.0, 7.0, 5)]
        for width in (0.1, 0.2, 0.4, 0.8):
            _assert_parity(checker, probes, width=width)
