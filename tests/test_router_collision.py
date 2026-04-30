"""Unit tests for VectorCollisionChecker and make_collision_checker.

Tests verify:
- VectorCollisionChecker returns correct results for paths that cross, are
  near, or are far from existing segments
- make_collision_checker selects VectorCollisionChecker when R-tree is
  available and GridCollisionChecker otherwise
- Fallback behavior when R-tree is not populated for a layer
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.collision import (
    GridCollisionChecker,
    VectorCollisionChecker,
    make_collision_checker,
)
from kicad_tools.router.primitives import Segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_grid(
    *,
    rtree_available: bool = True,
    seg_rtree_count: int = 10,
    segments: list[Segment] | None = None,
    trace_clearance: float = 0.15,
    resolution: float = 0.1,
    cols: int = 100,
    rows: int = 100,
):
    """Create a mock RoutingGrid with optional R-tree index data."""
    grid = MagicMock()
    grid._rtree_available = rtree_available
    grid._seg_rtree_count = seg_rtree_count
    grid.cols = cols
    grid.rows = rows
    grid.resolution = resolution
    grid.rules = MagicMock()
    grid.rules.trace_clearance = trace_clearance

    # Default: F.Cu is layer index 0
    grid.layer_to_index = MagicMock(return_value=0)
    grid.world_to_grid = MagicMock(side_effect=lambda x, y: (int(x / resolution), int(y / resolution)))

    if segments:
        # Build mock R-tree data
        items: dict[int, Segment] = {}
        for seg in segments:
            items[id(seg)] = seg

        mock_rtree = MagicMock()
        # intersection returns all segment ids (broad phase returns everything)
        mock_rtree.intersection = MagicMock(return_value=list(items.keys()))
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: items}
    else:
        grid._seg_rtree = {}
        grid._seg_rtree_items = {}

    # Mock the grid cells for obstacle checking -- default: no obstacles
    mock_cell = MagicMock()
    mock_cell.blocked = False
    mock_cell.is_obstacle = False
    mock_cell.net = 0

    # Create a grid array that returns non-blocking cells
    mock_layer = MagicMock()
    mock_row = MagicMock()
    mock_row.__getitem__ = MagicMock(return_value=mock_cell)
    mock_layer.__getitem__ = MagicMock(return_value=mock_row)
    grid.grid = MagicMock()
    grid.grid.__getitem__ = MagicMock(return_value=mock_layer)

    return grid


# ---------------------------------------------------------------------------
# VectorCollisionChecker
# ---------------------------------------------------------------------------


class TestVectorCollisionChecker:
    """Tests for VectorCollisionChecker."""

    def test_clear_path_no_obstacles(self):
        """Path through empty space should be clear."""
        grid = _make_mock_grid(segments=[])
        # Need rtree entry for layer 0
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: {}}

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.25, exclude_net=1)
        assert result is True

    def test_path_crosses_other_net(self):
        """Path that would cross another net's segment should be blocked."""
        other_seg = Segment(x1=2, y1=-2, x2=2, y2=2, width=0.25, layer=Layer.F_CU, net=2)
        grid = _make_mock_grid(segments=[other_seg])

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.25, exclude_net=1)
        assert result is False

    def test_path_near_other_net_within_clearance(self):
        """Path within clearance distance of another net should be blocked."""
        # Other segment 0.2mm away, clearance is 0.15mm, width 0.25 each
        # Edge-to-edge: 0.2 - 0.125 - 0.125 = -0.05 < 0.15 -> blocked
        other_seg = Segment(x1=0, y1=0.2, x2=5, y2=0.2, width=0.25, layer=Layer.F_CU, net=2)
        grid = _make_mock_grid(segments=[other_seg])

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.25, exclude_net=1)
        assert result is False

    def test_path_far_from_other_net(self):
        """Path well beyond clearance should be clear."""
        # Other segment 5mm away -- clearly no violation
        other_seg = Segment(x1=0, y1=5, x2=5, y2=5, width=0.25, layer=Layer.F_CU, net=2)
        grid = _make_mock_grid(segments=[other_seg])

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.25, exclude_net=1)
        assert result is True

    def test_own_net_segments_excluded(self):
        """Segments on the same net should be ignored."""
        own_seg = Segment(x1=2, y1=-2, x2=2, y2=2, width=0.25, layer=Layer.F_CU, net=1)
        grid = _make_mock_grid(segments=[own_seg])

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.25, exclude_net=1)
        assert result is True

    def test_invalid_layer_returns_false(self):
        """Invalid layer should return False."""
        grid = _make_mock_grid()
        grid.layer_to_index.side_effect = ValueError("bad layer")

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.25, exclude_net=1)
        assert result is False

    def test_fallback_when_no_rtree(self):
        """Should fall back to GridCollisionChecker when R-tree is unavailable."""
        grid = _make_mock_grid(rtree_available=False)

        checker = VectorCollisionChecker(grid)
        # The fallback will use GridCollisionChecker which needs grid cells
        # Since our mock grid cells are all non-blocking, path should be clear
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.25, exclude_net=1)
        assert result is True


# ---------------------------------------------------------------------------
# make_collision_checker
# ---------------------------------------------------------------------------


class TestMakeCollisionChecker:
    """Tests for make_collision_checker factory."""

    def test_selects_vector_when_rtree_available(self):
        """Should return VectorCollisionChecker when R-tree is available."""
        grid = _make_mock_grid(rtree_available=True, seg_rtree_count=50)
        checker = make_collision_checker(grid)
        assert isinstance(checker, VectorCollisionChecker)

    def test_selects_grid_when_rtree_unavailable(self):
        """Should return GridCollisionChecker when R-tree is not available."""
        grid = _make_mock_grid(rtree_available=False)
        checker = make_collision_checker(grid)
        assert isinstance(checker, GridCollisionChecker)

    def test_selects_grid_when_no_segments_indexed(self):
        """Should return GridCollisionChecker when no segments are indexed."""
        grid = _make_mock_grid(rtree_available=True, seg_rtree_count=0)
        checker = make_collision_checker(grid)
        assert isinstance(checker, GridCollisionChecker)

    def test_passes_ignore_overflow(self):
        """ignore_overflow flag should be passed through."""
        grid = _make_mock_grid(rtree_available=True, seg_rtree_count=50)
        checker = make_collision_checker(grid, ignore_overflow=True)
        assert isinstance(checker, VectorCollisionChecker)
        assert checker.ignore_overflow is True

        grid2 = _make_mock_grid(rtree_available=False)
        checker2 = make_collision_checker(grid2, ignore_overflow=True)
        assert isinstance(checker2, GridCollisionChecker)
        assert checker2.ignore_overflow is True
