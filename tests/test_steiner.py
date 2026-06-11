"""Tests for Rectilinear Steiner Minimum Tree (RSMT) construction.

This module tests the RSMT implementation in steiner.py, covering:
- Hanan grid construction
- 2-terminal degenerate case
- 3-terminal optimal Steiner topology
- Multi-terminal iterative 1-Steiner insertion
- Edge cases (collinear, coincident, single terminal)
- Integration with Pad objects via build_rsmt()
"""

import pytest

from kicad_tools.router.algorithms.steiner import (
    _build_mst_edges,
    _hanan_grid,
    _iterative_one_steiner,
    _manhattan,
    _mst_cost,
    _solve_3_terminal,
    build_rsmt,
    relocate_blocked_point,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad


def _make_pad(x: float, y: float, net: int = 1) -> Pad:
    """Helper to create a simple test Pad."""
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=f"net{net}",
        layer=Layer.F_CU,
    )


def _total_wirelength(
    points: list[tuple[float, float]], edges: list[tuple[int, int]]
) -> float:
    """Compute total Manhattan wirelength of edges."""
    return sum(
        _manhattan(points[i][0], points[i][1], points[j][0], points[j][1])
        for i, j in edges
    )


class TestManhattanDistance:
    """Tests for Manhattan distance computation."""

    def test_same_point(self):
        assert _manhattan(0, 0, 0, 0) == 0.0

    def test_horizontal(self):
        assert _manhattan(0, 0, 5, 0) == 5.0

    def test_vertical(self):
        assert _manhattan(0, 0, 0, 3) == 3.0

    def test_diagonal(self):
        assert _manhattan(0, 0, 3, 4) == 7.0

    def test_negative_coords(self):
        assert _manhattan(-1, -2, 3, 4) == 10.0


class TestHananGrid:
    """Tests for Hanan grid construction."""

    def test_two_points(self):
        points = [(0.0, 0.0), (2.0, 3.0)]
        candidates = _hanan_grid(points)
        # 2x2 grid = 4 points, minus 2 terminals = 2 candidates
        assert len(candidates) == 2
        assert (0.0, 3.0) in candidates
        assert (2.0, 0.0) in candidates

    def test_three_points_distinct(self):
        points = [(0.0, 0.0), (2.0, 1.0), (1.0, 3.0)]
        candidates = _hanan_grid(points)
        # 3x3 grid = 9 points, minus 3 terminals = 6 candidates
        assert len(candidates) == 6

    def test_collinear_horizontal(self):
        points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        candidates = _hanan_grid(points)
        # 3 unique x, 1 unique y -> 3 grid points, all are terminals
        assert len(candidates) == 0

    def test_single_point(self):
        points = [(1.0, 2.0)]
        candidates = _hanan_grid(points)
        assert len(candidates) == 0

    def test_coincident_points(self):
        points = [(1.0, 1.0), (1.0, 1.0)]
        candidates = _hanan_grid(points)
        assert len(candidates) == 0


class TestBuildMstEdges:
    """Tests for MST construction on raw points."""

    def test_two_points(self):
        points = [(0.0, 0.0), (1.0, 1.0)]
        edges = _build_mst_edges(points)
        assert len(edges) == 1
        assert edges[0] == (0, 1)

    def test_three_points(self):
        points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        edges = _build_mst_edges(points)
        assert len(edges) == 2

    def test_single_point(self):
        assert _build_mst_edges([(0.0, 0.0)]) == []

    def test_empty(self):
        assert _build_mst_edges([]) == []

    def test_connects_all(self):
        points = [(0.0, 0.0), (3.0, 0.0), (0.0, 4.0), (3.0, 4.0)]
        edges = _build_mst_edges(points)
        assert len(edges) == 3
        # Verify all nodes are connected
        connected = {0}
        for i, j in edges:
            connected.add(i)
            connected.add(j)
        assert connected == {0, 1, 2, 3}


class TestSolve3Terminal:
    """Tests for optimal 3-terminal RSMT."""

    def test_right_angle(self):
        """Three pads forming a right angle -- Steiner point at the corner."""
        terminals = [(0.0, 0.0), (3.0, 0.0), (0.0, 4.0)]
        all_points, edges = _solve_3_terminal(terminals)

        rsmt_cost = _total_wirelength(all_points, edges)
        mst_cost = _mst_cost(terminals)
        # RSMT should be no worse than MST
        assert rsmt_cost <= mst_cost + 1e-9

    def test_equilateral_arrangement(self):
        """Steiner point at median coordinates should reduce wirelength."""
        terminals = [(0.0, 0.0), (4.0, 0.0), (2.0, 3.0)]
        all_points, edges = _solve_3_terminal(terminals)

        rsmt_cost = _total_wirelength(all_points, edges)
        mst_cost = _mst_cost(terminals)
        assert rsmt_cost <= mst_cost + 1e-9

    def test_collinear(self):
        """Collinear terminals need no Steiner point."""
        terminals = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        all_points, edges = _solve_3_terminal(terminals)
        # No Steiner points should be added for collinear terminals
        assert len(all_points) == 3
        assert len(edges) == 2

    def test_steiner_coincides_with_terminal(self):
        """When median coincides with a terminal, no extra point needed."""
        terminals = [(0.0, 0.0), (2.0, 0.0), (2.0, 3.0)]
        all_points, edges = _solve_3_terminal(terminals)
        # Steiner point (2, 0) coincides with terminal[1]
        # Should still produce valid tree
        assert len(edges) == len(all_points) - 1


class TestIterativeOneSteiner:
    """Tests for iterative 1-Steiner insertion."""

    def test_two_terminals(self):
        """Two terminals: no Steiner point possible."""
        terminals = [(0.0, 0.0), (3.0, 4.0)]
        all_points, edges = _iterative_one_steiner(terminals)
        assert len(all_points) == 2
        assert len(edges) == 1

    def test_never_worse_than_mst(self):
        """RSMT wirelength must never exceed MST wirelength."""
        terminals = [(0.0, 0.0), (4.0, 0.0), (0.0, 3.0), (4.0, 3.0)]
        all_points, edges = _iterative_one_steiner(terminals)

        rsmt_cost = _total_wirelength(all_points, edges)
        mst_cost = _mst_cost(terminals)
        assert rsmt_cost <= mst_cost + 1e-9

    def test_five_terminal_improvement(self):
        """Five terminals in a cross pattern -- Steiner points help."""
        terminals = [
            (2.0, 0.0),
            (0.0, 2.0),
            (2.0, 2.0),
            (4.0, 2.0),
            (2.0, 4.0),
        ]
        all_points, edges = _iterative_one_steiner(terminals)

        rsmt_cost = _total_wirelength(all_points, edges)
        mst_cost = _mst_cost(terminals)
        assert rsmt_cost <= mst_cost + 1e-9

    def test_large_net_bounded_iterations(self):
        """Larger nets complete with bounded iterations."""
        # 15 terminals in a grid-like arrangement
        terminals = [(float(i), float(j)) for i in range(5) for j in range(3)]
        all_points, edges = _iterative_one_steiner(
            terminals, max_iterations=15
        )
        # Must form a spanning tree
        assert len(edges) == len(all_points) - 1

        rsmt_cost = _total_wirelength(all_points, edges)
        mst_cost = _mst_cost(terminals)
        assert rsmt_cost <= mst_cost + 1e-9

    def test_custom_cost_function(self):
        """Custom cost function is respected."""
        terminals = [(0.0, 0.0), (3.0, 0.0), (0.0, 4.0)]

        # Weighted cost function that penalizes vertical distance
        def weighted_cost(x1, y1, x2, y2):
            return abs(x1 - x2) + 2 * abs(y1 - y2)

        all_points, edges = _iterative_one_steiner(
            terminals, cost_fn=weighted_cost
        )
        assert len(edges) == len(all_points) - 1


class TestBuildRsmt:
    """Tests for the public build_rsmt() API with Pad objects."""

    def test_single_pad(self):
        """Single pad: no edges."""
        pads = [_make_pad(0, 0)]
        extended, edges = build_rsmt(pads)
        assert len(extended) == 1
        assert edges == []

    def test_two_pads(self):
        """Two pads: single edge, no Steiner points."""
        pads = [_make_pad(0, 0), _make_pad(3, 4)]
        extended, edges = build_rsmt(pads)
        assert len(extended) == 2
        assert len(edges) == 1
        assert edges[0] == (0, 1)
        assert not extended[0].steiner_point
        assert not extended[1].steiner_point

    def test_three_pads_steiner_flag(self):
        """Three pads: Steiner points marked with steiner_point=True."""
        pads = [_make_pad(0, 0), _make_pad(4, 0), _make_pad(2, 3)]
        extended, edges = build_rsmt(pads)

        # Original pads are not marked as Steiner
        for i in range(3):
            assert not extended[i].steiner_point

        # Any added pads are marked as Steiner
        for i in range(3, len(extended)):
            assert extended[i].steiner_point

        # Tree connectivity: edges = num_points - 1
        assert len(edges) == len(extended) - 1

    def test_steiner_pads_inherit_net_info(self):
        """Steiner point pads inherit net from original pads."""
        pads = [_make_pad(0, 0, net=42), _make_pad(4, 0, net=42), _make_pad(2, 3, net=42)]
        extended, edges = build_rsmt(pads)

        for pad in extended:
            assert pad.net == 42
            assert pad.net_name == "net42"

    def test_edges_sorted_by_length(self):
        """Edges are sorted by Manhattan distance (shortest first)."""
        pads = [_make_pad(0, 0), _make_pad(1, 0), _make_pad(10, 10)]
        extended, edges = build_rsmt(pads)

        costs = []
        for i, j in edges:
            cost = _manhattan(extended[i].x, extended[i].y, extended[j].x, extended[j].y)
            costs.append(cost)
        # Verify non-decreasing order
        for k in range(len(costs) - 1):
            assert costs[k] <= costs[k + 1] + 1e-9

    def test_never_worse_than_mst_four_pads(self):
        """Four pads in a rectangle: RSMT <= MST."""
        pads = [
            _make_pad(0, 0),
            _make_pad(4, 0),
            _make_pad(0, 3),
            _make_pad(4, 3),
        ]
        extended, edges = build_rsmt(pads)

        rsmt_cost = sum(
            _manhattan(extended[i].x, extended[i].y, extended[j].x, extended[j].y)
            for i, j in edges
        )
        terminals = [(p.x, p.y) for p in pads]
        mst_cost = _mst_cost(terminals)
        assert rsmt_cost <= mst_cost + 1e-9

    def test_empty_pads(self):
        """Empty pad list: no edges."""
        extended, edges = build_rsmt([])
        assert extended == []
        assert edges == []

    def test_coincident_pads(self):
        """Coincident pads: handles gracefully."""
        pads = [_make_pad(1, 1), _make_pad(1, 1), _make_pad(3, 3)]
        extended, edges = build_rsmt(pads)
        # Should produce a valid tree
        assert len(edges) == len(extended) - 1

    def test_congestion_fn_passed_through(self):
        """Custom congestion function is used for edge costs."""
        pads = [_make_pad(0, 0), _make_pad(3, 0), _make_pad(0, 4)]

        def custom_cost(x1, y1, x2, y2):
            return abs(x1 - x2) + abs(y1 - y2)

        extended, edges = build_rsmt(pads, congestion_fn=custom_cost)
        assert len(edges) == len(extended) - 1

    def test_nine_terminals(self):
        """Nine terminals (upper bound for small-net solver)."""
        pads = [_make_pad(float(i), float(j)) for i in range(3) for j in range(3)]
        extended, edges = build_rsmt(pads)

        rsmt_cost = sum(
            _manhattan(extended[i].x, extended[i].y, extended[j].x, extended[j].y)
            for i, j in edges
        )
        terminals = [(p.x, p.y) for p in pads]
        mst_cost = _mst_cost(terminals)
        assert rsmt_cost <= mst_cost + 1e-9

    def test_large_net_over_nine(self):
        """Net with >9 terminals uses bounded iterative solver."""
        pads = [_make_pad(float(i * 2), float(j * 3)) for i in range(4) for j in range(3)]
        assert len(pads) == 12
        extended, edges = build_rsmt(pads)

        # Valid spanning tree
        assert len(edges) == len(extended) - 1

        rsmt_cost = sum(
            _manhattan(extended[i].x, extended[i].y, extended[j].x, extended[j].y)
            for i, j in edges
        )
        terminals = [(p.x, p.y) for p in pads]
        mst_cost = _mst_cost(terminals)
        assert rsmt_cost <= mst_cost + 1e-9


class TestBuildRsmtCongestionFn:
    """Tests for build_rsmt with a congestion_fn parameter."""

    def test_congestion_fn_biases_steiner_topology(self):
        """A congestion hotspot in the center should push Steiner point away.

        Four pads at (0,0), (10,0), (0,10), (10,10) form a square.
        With pure Manhattan cost the Steiner point sits near the centre.
        A heavy penalty at the centre should bias the tree to avoid it.
        """
        pads = [
            _make_pad(0, 0),
            _make_pad(10, 0),
            _make_pad(0, 10),
            _make_pad(10, 10),
        ]

        # Pure Manhattan baseline
        ext_base, edges_base = build_rsmt(pads)

        # Congestion function that heavily penalises edges whose midpoint
        # is close to (5, 5).
        def hot_center(x1, y1, x2, y2):
            manhattan = abs(x1 - x2) + abs(y1 - y2)
            mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
            dist_to_center = abs(mid_x - 5) + abs(mid_y - 5)
            # Add huge penalty when near center
            penalty = max(0, 20 - dist_to_center) * 5
            return manhattan + penalty

        ext_cong, edges_cong = build_rsmt(pads, congestion_fn=hot_center)

        # Both must produce valid spanning trees
        assert len(edges_base) == len(ext_base) - 1
        assert len(edges_cong) == len(ext_cong) - 1

        # The congestion-aware tree should still connect all original pads
        connected = set()
        for i, j in edges_cong:
            connected.add(i)
            connected.add(j)
        for idx in range(len(pads)):
            assert idx in connected

    def test_weight_zero_matches_manhattan(self):
        """congestion_fn that returns pure Manhattan should match baseline."""
        pads = [_make_pad(0, 0), _make_pad(5, 0), _make_pad(0, 5)]

        ext_base, edges_base = build_rsmt(pads)

        def pure_manhattan(x1, y1, x2, y2):
            return abs(x1 - x2) + abs(y1 - y2)

        ext_fn, edges_fn = build_rsmt(pads, congestion_fn=pure_manhattan)

        # Costs should be identical
        base_cost = sum(
            _manhattan(ext_base[i].x, ext_base[i].y, ext_base[j].x, ext_base[j].y)
            for i, j in edges_base
        )
        fn_cost = sum(
            _manhattan(ext_fn[i].x, ext_fn[i].y, ext_fn[j].x, ext_fn[j].y)
            for i, j in edges_fn
        )
        assert abs(base_cost - fn_cost) < 1e-9

    def test_none_congestion_fn_is_default(self):
        """Passing congestion_fn=None should behave identically to omitting it."""
        pads = [_make_pad(0, 0), _make_pad(8, 0), _make_pad(4, 6)]

        ext1, edges1 = build_rsmt(pads)
        ext2, edges2 = build_rsmt(pads, congestion_fn=None)

        assert len(ext1) == len(ext2)
        assert edges1 == edges2


class TestSteinerGridSnap:
    """PR #3481 fix: synthetic Steiner points must be snappable to the
    routing grid.

    Hanan-grid candidates inherit raw terminal coordinates, which
    generally do NOT align to the routing grid.  Real pads get off-grid
    rescue via sub-grid / waypoint injection, but virtual Steiner pads
    have no ``ref`` so no rescue applies — an off-grid Steiner point
    fails ``pin_access`` with ``PADS_OFF_GRID: steiner@(...)`` (the
    softstart SRC_POS / BUS_LINE / SCAP_POS+ / VRECT signature).
    """

    @staticmethod
    def _snap_0075(x: float, y: float) -> tuple[float, float]:
        """Snap to a 0.075 mm grid (the softstart production grid)."""
        res = 0.075
        return (round(x / res) * res, round(y / res) * res)

    def test_steiner_points_snapped(self):
        """All synthetic points land exactly on the snap grid."""
        # Integer-mm terminals are off-grid on the 0.075 grid
        # (46.0 / 0.075 = 613.33), reproducing the softstart failure.
        # Cross topology forces a synthetic branch point near (50, 50).
        pads = [
            _make_pad(40.0, 50.0),
            _make_pad(60.0, 50.0),
            _make_pad(50.0, 40.0),
            _make_pad(50.0, 60.0),
        ]
        extended, _edges = build_rsmt(pads, snap_fn=self._snap_0075)

        steiner = [p for p in extended if p.steiner_point]
        assert steiner, "3-terminal L-shaped net must produce a Steiner point"
        for p in steiner:
            sx, sy = self._snap_0075(p.x, p.y)
            assert abs(p.x - sx) < 1e-9 and abs(p.y - sy) < 1e-9, (
                f"Steiner point ({p.x}, {p.y}) is off the 0.075 mm grid"
            )

    def test_terminals_never_snapped(self):
        """Terminal pads keep their exact coordinates (only synthetic
        points are snapped — pads have their own off-grid rescue)."""
        coords = [(46.01, 42.02), (72.03, 42.04), (46.05, 58.06)]
        pads = [_make_pad(x, y) for x, y in coords]
        extended, _edges = build_rsmt(pads, snap_fn=self._snap_0075)

        for pad, (x, y) in zip(extended[: len(coords)], coords, strict=False):
            assert pad.x == x and pad.y == y

    def test_no_snap_fn_preserves_legacy_behavior(self):
        """Without snap_fn the output is unchanged (backward compat)."""
        pads = [_make_pad(0, 0), _make_pad(8, 0), _make_pad(4, 6)]
        ext1, edges1 = build_rsmt(pads)
        ext2, edges2 = build_rsmt(pads, snap_fn=None)
        assert [(p.x, p.y) for p in ext1] == [(p.x, p.y) for p in ext2]
        assert edges1 == edges2


class TestRelocateBlockedPoint:
    """Issue #3471: Steiner branch points must not sit on blocked cells.

    Board 05's ISENSE_A+ produced a Steiner point at (136.9, 176.0) --
    on a MOSFET through-hole leg -- so every incident A* edge failed and
    the net was classified ``blocked_path`` even on an empty board.
    ``relocate_blocked_point`` ring-scans for the nearest free cell.
    """

    def test_free_cell_unchanged(self):
        assert relocate_blocked_point(5, 5, lambda x, y: False) == (5, 5)

    def test_relocates_to_adjacent_free_cell(self):
        blocked = {(5, 5)}
        gx, gy = relocate_blocked_point(5, 5, lambda x, y: (x, y) in blocked)
        assert (gx, gy) != (5, 5)
        assert max(abs(gx - 5), abs(gy - 5)) == 1

    def test_relocation_is_deterministic(self):
        blocked = {(5, 5)}
        results = {
            relocate_blocked_point(5, 5, lambda x, y: (x, y) in blocked)
            for _ in range(10)
        }
        assert len(results) == 1

    def test_ring_scan_finds_nearest_ring(self):
        """A 3x3 blocked block forces relocation to Chebyshev radius 2."""
        blocked = {(x, y) for x in range(4, 7) for y in range(4, 7)}
        gx, gy = relocate_blocked_point(5, 5, lambda x, y: (x, y) in blocked)
        assert (gx, gy) not in blocked
        assert max(abs(gx - 5), abs(gy - 5)) == 2

    def test_all_blocked_returns_original(self):
        gx, gy = relocate_blocked_point(
            5, 5, lambda x, y: True, max_radius=3
        )
        assert (gx, gy) == (5, 5)

    def test_build_rsmt_snap_fn_can_relocate(self):
        """End-to-end: a snap_fn embedding the relocation moves the
        synthetic branch point off a blocked cell while terminals keep
        their exact coordinates."""
        # Cross topology forces a branch point near (50, 50).
        pads = [
            _make_pad(40.0, 50.0),
            _make_pad(60.0, 50.0),
            _make_pad(50.0, 40.0),
            _make_pad(50.0, 60.0),
        ]
        blocked_cell = (50, 50)

        def snap(x: float, y: float) -> tuple[float, float]:
            gx, gy = round(x), round(y)  # 1mm grid for the test
            gx, gy = relocate_blocked_point(
                gx, gy, lambda cx, cy: (cx, cy) == blocked_cell
            )
            return float(gx), float(gy)

        extended, _edges = build_rsmt(pads, snap_fn=snap)
        steiner = [p for p in extended if p.steiner_point]
        assert steiner, "cross topology must produce a Steiner point"
        for p in steiner:
            assert (round(p.x), round(p.y)) != blocked_cell
        for pad, original in zip(extended[:4], pads, strict=False):
            assert (pad.x, pad.y) == (original.x, original.y)
