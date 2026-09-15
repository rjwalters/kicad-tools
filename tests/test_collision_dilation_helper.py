"""Exact-equivalence regression tests for ``_iter_dilated_line_cells``.

Issue #5240 (partial increment): ``GridCollisionChecker._get_path_cells``
and ``VectorCollisionChecker._check_obstacles_clear`` both used to walk a
Bresenham line and, at *every* rasterized point, re-emit the full
``(2*clearance+1) x (2*clearance+1)`` window of grid cells around that
point (deduplicated via a Python ``set``). Profiling a board-06 re-route
showed this as the hottest leaf frame in the post-route trace-optimization
pass -- board 06's clearance envelope gives ``clearance_cells`` ~= 7, so
every rasterized point re-issued a 225-cell window even though consecutive
windows overlap in all but a thin leading strip.

``_iter_dilated_line_cells`` replaces the per-point full-window
recomputation with an *incremental* dilation: after the first point, each
subsequent step only emits the new leading column/row that the previous
window did not already cover. This module holds the correctness evidence
for that optimization -- it must produce **exactly** the same cell set as
the original nested-loop algorithm (reimplemented below, unmodified, as
the reference), for every input, not just "usually the same" or "close
enough". A silently narrower cell set would let the optimizer route
traces through clearance violations; a silently wider one would only cost
performance, but neither is acceptable to swap in silently.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router.optimizer.collision import _iter_dilated_line_cells


def _reference_dilated_line_cells(
    gx1: int, gy1: int, gx2: int, gy2: int, clearance: int
) -> set[tuple[int, int]]:
    """Pre-#5240 reference implementation: full window at every point.

    This is a verbatim reimplementation of the original
    ``GridCollisionChecker._get_path_cells`` body (before the #5240
    incremental-dilation rewrite) -- kept here, independent of the
    production code, so a future edit to the fast path cannot silently
    "fix" this test to match a changed (and possibly wrong) behavior.
    """
    cells: set[tuple[int, int]] = set()
    dx = abs(gx2 - gx1)
    dy = abs(gy2 - gy1)
    sx = 1 if gx1 < gx2 else -1
    sy = 1 if gy1 < gy2 else -1
    err = dx - dy

    gx, gy = gx1, gy1
    while True:
        for cy in range(-clearance, clearance + 1):
            for cx in range(-clearance, clearance + 1):
                cells.add((gx + cx, gy + cy))

        if gx == gx2 and gy == gy2:
            break

        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            gx += sx
        if e2 < dx:
            err += dx
            gy += sy

    return cells


class TestDilatedLineCellsExactEquivalence:
    """The fast path's cell SET must exactly match the reference algorithm."""

    def test_randomized_lines_exact_match(self) -> None:
        """Exhaustive randomized parity across lines, clearances, and directions."""
        rng = random.Random(5240)
        mismatches: list[tuple[int, int, int, int, int]] = []
        for _ in range(5000):
            gx1 = rng.randint(-25, 25)
            gy1 = rng.randint(-25, 25)
            gx2 = rng.randint(-25, 25)
            gy2 = rng.randint(-25, 25)
            clearance = rng.randint(0, 10)

            expected = _reference_dilated_line_cells(gx1, gy1, gx2, gy2, clearance)
            actual = set(_iter_dilated_line_cells(gx1, gy1, gx2, gy2, clearance))

            if expected != actual:
                mismatches.append((gx1, gy1, gx2, gy2, clearance))

        assert not mismatches, f"{len(mismatches)} mismatching inputs: {mismatches[:5]}"

    @pytest.mark.parametrize(
        ("gx1", "gy1", "gx2", "gy2", "clearance"),
        [
            (5, 5, 5, 5, 0),  # single point, zero clearance
            (5, 5, 5, 5, 3),  # single point, nonzero clearance
            (0, 0, 0, 0, 7),  # single point, board06-realistic clearance
            (0, 0, 10, 0, 0),  # horizontal line, zero clearance
            (0, 0, 0, 10, 0),  # vertical line, zero clearance
            (0, 0, 5, 5, 0),  # pure diagonal (45 degrees), zero clearance
            (-5, -5, 5, 5, 5),  # diagonal spanning the origin
            (0, 0, 100, 1, 7),  # near-horizontal shallow line
            (0, 0, 1, 100, 7),  # near-vertical steep line
            (10, 10, 0, 0, 7),  # reversed diagonal direction
            (10, 0, 0, 10, 7),  # anti-diagonal
        ],
    )
    def test_edge_cases_exact_match(
        self, gx1: int, gy1: int, gx2: int, gy2: int, clearance: int
    ) -> None:
        expected = _reference_dilated_line_cells(gx1, gy1, gx2, gy2, clearance)
        actual = set(_iter_dilated_line_cells(gx1, gy1, gx2, gy2, clearance))
        assert actual == expected

    def test_diagonal_step_may_repeat_a_corner_cell(self) -> None:
        """A diagonal Bresenham step's new column and new row share one
        corner cell, which the generator documents yielding twice. This is
        harmless for set-deduplicating callers (``_get_path_cells``) and for
        early-exit obstacle scans (``_check_obstacles_clear``), but pin the
        documented behavior explicitly so a future refactor notices if it
        silently drops the duplicate (which would be fine) or starts
        emitting *wrong* cells (which would not be)."""
        emitted = list(_iter_dilated_line_cells(0, 0, 5, 5, 2))
        assert len(emitted) > len(set(emitted))
        assert set(emitted) == _reference_dilated_line_cells(0, 0, 5, 5, 2)
