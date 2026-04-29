"""Tests for pre-route RUDY congestion estimator (Issue #2278)."""

from __future__ import annotations

import time

import pytest

from kicad_tools.router.congestion_estimator import (
    CongestionEstimator,
    NetBBox,
    TileGrid,
)
from kicad_tools.router.primitives import Pad


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pad(x: float, y: float, net: int = 1, ref: str = "U1", pin: str = "1") -> Pad:
    """Create a minimal Pad for testing."""
    return Pad(
        x=x, y=y, width=0.5, height=0.5,
        net=net, net_name=f"Net{net}", ref=ref, pin=pin,
    )


def _build_simple_estimator(
    nets: dict[int, list[tuple[str, str]]],
    pads: dict[tuple[str, str], Pad],
    width: float = 100.0,
    height: float = 100.0,
    target_tiles: int = 100,
) -> CongestionEstimator:
    """Build a CongestionEstimator with default board dimensions."""
    return CongestionEstimator.from_nets(
        nets=nets,
        pads=pads,
        board_origin_x=0.0,
        board_origin_y=0.0,
        board_width=width,
        board_height=height,
        target_tiles=target_tiles,
    )


# ---------------------------------------------------------------------------
# TileGrid tests
# ---------------------------------------------------------------------------

class TestTileGrid:
    """Tests for TileGrid construction and coordinate mapping."""

    def test_from_board_default(self):
        grid = TileGrid.from_board(0, 0, 100, 100, target_tiles=100)
        assert grid.cols == 10
        assert grid.rows == 10
        assert grid.tile_w == pytest.approx(10.0)
        assert grid.tile_h == pytest.approx(10.0)

    def test_from_board_rectangular(self):
        grid = TileGrid.from_board(0, 0, 200, 50, target_tiles=100)
        # Aspect ratio 4:1 -> cols ~20, rows ~5
        assert grid.cols > grid.rows

    def test_from_board_zero_area(self):
        grid = TileGrid.from_board(0, 0, 0, 0, target_tiles=100)
        assert grid.cols == 1
        assert grid.rows == 1

    def test_tile_at_basic(self):
        grid = TileGrid.from_board(0, 0, 100, 100, target_tiles=100)
        # Point at (5, 5) should be in tile (0, 0) with 10mm tiles
        col, row = grid.tile_at(5, 5)
        assert col == 0
        assert row == 0

    def test_tile_at_center(self):
        grid = TileGrid.from_board(0, 0, 100, 100, target_tiles=100)
        col, row = grid.tile_at(55, 55)
        assert col == 5
        assert row == 5

    def test_tile_at_clamped(self):
        grid = TileGrid.from_board(0, 0, 100, 100, target_tiles=100)
        # Out of bounds should clamp
        col, row = grid.tile_at(-10, -10)
        assert col == 0
        assert row == 0
        col, row = grid.tile_at(200, 200)
        assert col == grid.cols - 1
        assert row == grid.rows - 1

    def test_tile_range(self):
        grid = TileGrid.from_board(0, 0, 100, 100, target_tiles=100)
        col_lo, row_lo, col_hi, row_hi = grid.tile_range(15, 25, 45, 65)
        assert col_lo == 1
        assert row_lo == 2
        assert col_hi == 4
        assert row_hi == 6


# ---------------------------------------------------------------------------
# NetBBox tests
# ---------------------------------------------------------------------------

class TestNetBBox:
    """Tests for NetBBox property calculations."""

    def test_hpwl(self):
        bbox = NetBBox(net_id=1, min_x=10, min_y=20, max_x=30, max_y=50, pad_count=3)
        assert bbox.width == 20.0
        assert bbox.height == 30.0
        assert bbox.hpwl == 50.0

    def test_zero_size(self):
        bbox = NetBBox(net_id=1, min_x=10, min_y=20, max_x=10, max_y=20, pad_count=2)
        assert bbox.hpwl == 0.0


# ---------------------------------------------------------------------------
# CongestionEstimator core tests
# ---------------------------------------------------------------------------

class TestCongestionEstimator:
    """Tests for RUDY computation."""

    def test_single_2pin_net(self):
        """RUDY demand for a single 2-pin net equals HPWL / n_tiles."""
        pads = {
            ("R1", "1"): _make_pad(10, 10, net=1, ref="R1", pin="1"),
            ("R1", "2"): _make_pad(50, 10, net=1, ref="R1", pin="2"),
        }
        nets = {1: [("R1", "1"), ("R1", "2")]}

        est = _build_simple_estimator(nets, pads, width=100, height=100, target_tiles=100)

        # Net spans x=[10,50], y=[10,10] -> HPWL = 40 + 0 = 40
        # With 10x10 tiles of 10mm each, bbox covers tiles col 1..5, row 1..1
        # n_tiles = 5 * 1 = 5, demand_per_tile = 40/5 = 8.0
        score = est.get_net_congestion_score(1)
        assert score > 0

        # Check some tiles have demand
        total_demand = sum(
            est.get_tile_demand(r, c)
            for r in range(est.grid.rows)
            for c in range(est.grid.cols)
        )
        assert total_demand == pytest.approx(40.0)  # Total demand = HPWL

    def test_overlapping_nets_accumulate(self):
        """Two nets overlapping the same tile accumulate demand correctly."""
        pads = {
            ("R1", "1"): _make_pad(10, 10, net=1, ref="R1", pin="1"),
            ("R1", "2"): _make_pad(30, 10, net=1, ref="R1", pin="2"),
            ("R2", "1"): _make_pad(10, 10, net=2, ref="R2", pin="1"),
            ("R2", "2"): _make_pad(30, 10, net=2, ref="R2", pin="2"),
        }
        nets = {
            1: [("R1", "1"), ("R1", "2")],
            2: [("R2", "1"), ("R2", "2")],
        }

        est = _build_simple_estimator(nets, pads, width=100, height=100, target_tiles=100)

        # Both nets have same bbox -> tiles in overlap get double demand
        # Net 1: HPWL=20, Net 2: HPWL=20 -> total HPWL distributed = 40
        total_demand = sum(
            est.get_tile_demand(r, c)
            for r in range(est.grid.rows)
            for c in range(est.grid.cols)
        )
        assert total_demand == pytest.approx(40.0)

    def test_single_pad_net_zero_demand(self):
        """Net with only one pad produces zero demand."""
        pads = {
            ("U1", "1"): _make_pad(50, 50, net=1, ref="U1", pin="1"),
        }
        nets = {1: [("U1", "1")]}

        est = _build_simple_estimator(nets, pads)
        assert est.get_net_congestion_score(1) == 0.0

    def test_empty_nets(self):
        """Empty net list produces all-zero demand grid."""
        est = _build_simple_estimator({}, {})
        for r in range(est.grid.rows):
            for c in range(est.grid.cols):
                assert est.get_tile_demand(r, c) == 0.0

    def test_pads_in_same_tile(self):
        """Net with all pads in one tile produces nonzero demand."""
        pads = {
            ("U1", "1"): _make_pad(5, 5, net=1, ref="U1", pin="1"),
            ("U1", "2"): _make_pad(6, 6, net=1, ref="U1", pin="2"),
        }
        nets = {1: [("U1", "1"), ("U1", "2")]}

        est = _build_simple_estimator(nets, pads, width=100, height=100, target_tiles=100)

        # Bbox is very small but non-zero, covering 1 tile
        score = est.get_net_congestion_score(1)
        # HPWL = 1 + 1 = 2, spread across 1 tile -> demand = 2
        assert score > 0

    def test_pour_nets_excluded(self):
        """Pour nets are excluded from RUDY computation."""
        pads = {
            ("U1", "1"): _make_pad(10, 10, net=1, ref="U1", pin="1"),
            ("U1", "2"): _make_pad(90, 90, net=1, ref="U1", pin="2"),
        }
        nets = {1: [("U1", "1"), ("U1", "2")]}

        est = CongestionEstimator.from_nets(
            nets=nets,
            pads=pads,
            board_origin_x=0, board_origin_y=0,
            board_width=100, board_height=100,
            pour_net_ids={1},
        )
        assert est.get_net_congestion_score(1) == 0.0
        total_demand = sum(
            est.get_tile_demand(r, c)
            for r in range(est.grid.rows)
            for c in range(est.grid.cols)
        )
        assert total_demand == 0.0

    def test_net_zero_excluded(self):
        """Net 0 (unconnected) is always excluded."""
        pads = {
            ("U1", "1"): _make_pad(10, 10, net=0, ref="U1", pin="1"),
            ("U1", "2"): _make_pad(90, 90, net=0, ref="U1", pin="2"),
        }
        nets = {0: [("U1", "1"), ("U1", "2")]}

        est = _build_simple_estimator(nets, pads)
        assert est.get_net_congestion_score(0) == 0.0

    def test_get_demand_grid(self):
        """get_demand_grid returns the full 2-D demand array."""
        pads = {
            ("R1", "1"): _make_pad(10, 10, net=1, ref="R1", pin="1"),
            ("R1", "2"): _make_pad(50, 50, net=1, ref="R1", pin="2"),
        }
        nets = {1: [("R1", "1"), ("R1", "2")]}

        est = _build_simple_estimator(nets, pads, target_tiles=25)
        grid = est.get_demand_grid()
        assert len(grid) == est.grid.rows
        assert len(grid[0]) == est.grid.cols

    def test_get_tile_demand_out_of_range(self):
        """Out-of-range tile indices return 0.0."""
        est = _build_simple_estimator({}, {})
        assert est.get_tile_demand(-1, 0) == 0.0
        assert est.get_tile_demand(0, 999) == 0.0

    def test_format_ascii_heatmap(self):
        """ASCII heatmap produces non-empty output."""
        pads = {
            ("R1", "1"): _make_pad(10, 10, net=1, ref="R1", pin="1"),
            ("R1", "2"): _make_pad(90, 90, net=1, ref="R1", pin="2"),
        }
        nets = {1: [("R1", "1"), ("R1", "2")]}

        est = _build_simple_estimator(nets, pads)
        heatmap = est.format_ascii_heatmap()
        assert "RUDY Congestion Map" in heatmap
        assert len(heatmap) > 50

    def test_format_json(self):
        """JSON format contains required keys."""
        pads = {
            ("R1", "1"): _make_pad(10, 10, net=1, ref="R1", pin="1"),
            ("R1", "2"): _make_pad(50, 50, net=1, ref="R1", pin="2"),
        }
        nets = {1: [("R1", "1"), ("R1", "2")]}

        est = _build_simple_estimator(nets, pads)
        data = est.format_json()
        assert "dimensions" in data
        assert "tile_size" in data
        assert "grid" in data
        assert "net_scores" in data
        assert data["dimensions"]["rows"] == est.grid.rows
        assert data["dimensions"]["cols"] == est.grid.cols

    def test_asymmetric_congestion_changes_ordering(self):
        """Nets in congested regions get higher scores than isolated nets."""
        # Create a cluster of nets on the left side and one isolated net on the right
        pads = {}
        nets = {}

        # 5 overlapping nets on left side (congested)
        for i in range(1, 6):
            ref = f"L{i}"
            pads[(ref, "1")] = _make_pad(5, 10 + i, net=i, ref=ref, pin="1")
            pads[(ref, "2")] = _make_pad(25, 10 + i, net=i, ref=ref, pin="2")
            nets[i] = [(ref, "1"), (ref, "2")]

        # 1 isolated net on right side (uncongested)
        pads[("R1", "1")] = _make_pad(70, 50, net=6, ref="R1", pin="1")
        pads[("R1", "2")] = _make_pad(90, 50, net=6, ref="R1", pin="2")
        nets[6] = [("R1", "1"), ("R1", "2")]

        est = _build_simple_estimator(nets, pads)

        # Any of the left-side nets should have higher congestion than the isolated net
        left_score = est.get_net_congestion_score(1)
        right_score = est.get_net_congestion_score(6)
        assert left_score > right_score


class TestCongestionEstimatorPerformance:
    """Performance tests for RUDY computation."""

    def test_100_nets_under_100ms(self):
        """100-net random board completes in <100ms."""
        import random

        random.seed(42)
        pads = {}
        nets = {}

        for i in range(1, 101):
            ref = f"U{i}"
            x1, y1 = random.uniform(0, 100), random.uniform(0, 100)
            x2, y2 = random.uniform(0, 100), random.uniform(0, 100)
            pads[(ref, "1")] = _make_pad(x1, y1, net=i, ref=ref, pin="1")
            pads[(ref, "2")] = _make_pad(x2, y2, net=i, ref=ref, pin="2")
            nets[i] = [(ref, "1"), (ref, "2")]

        start = time.monotonic()
        est = _build_simple_estimator(nets, pads)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 100, f"RUDY took {elapsed_ms:.1f}ms for 100 nets (limit: 100ms)"
        assert len(est.net_scores) == 100
