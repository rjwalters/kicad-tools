"""Unit tests for VectorCollisionChecker and make_collision_checker.

Tests verify:
- VectorCollisionChecker returns correct results for paths that cross, are
  near, or are far from existing segments
- make_collision_checker selects VectorCollisionChecker when R-tree is
  available and GridCollisionChecker otherwise
- Fallback behavior when R-tree is not populated for a layer
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
    routes: list | None = None,
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
    grid.world_to_grid = MagicMock(
        side_effect=lambda x, y: (int(x / resolution), int(y / resolution))
    )

    # Issue #2955: VectorCollisionChecker now consults ``grid.routes`` for
    # foreign-net via checks.  Default to an empty list so pre-existing
    # tests that exercise only segment / pad logic are unaffected.
    grid.routes = routes if routes is not None else []

    # Issue #2960: Default the via R-tree to disabled so the collision
    # checker falls back to the linear scan over ``grid.routes`` (the
    # pre-#2960 contract) for tests that haven't built one explicitly.
    # MagicMock auto-creates truthy attribute proxies, which would
    # otherwise trick the collision checker into querying a fake R-tree.
    grid._via_rtree = None
    grid._via_rtree_items = {}
    grid._via_rtree_count = 0

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


# ---------------------------------------------------------------------------
# Issue #2758 regression: pad_blocked cells must block path even when
# cell.net == 0 (skip_nets case: pour-net pads like GND, +1V2, +1V8)
# ---------------------------------------------------------------------------


def _make_mock_grid_with_pad_cell(
    *,
    pad_blocked: bool,
    cell_net: int,
    is_obstacle: bool = False,
    blocked: bool = True,
):
    """Create a mock grid where any cell access returns a pad cell."""
    grid = MagicMock()
    grid._rtree_available = False
    grid.cols = 100
    grid.rows = 100
    grid.resolution = 0.1
    grid.rules = MagicMock()
    grid.rules.trace_clearance = 0.15
    grid.layer_to_index = MagicMock(return_value=0)
    grid.world_to_grid = MagicMock(side_effect=lambda x, y: (int(x / 0.1), int(y / 0.1)))
    grid._seg_rtree = {}
    grid._seg_rtree_items = {}
    # Issue #2955: VectorCollisionChecker iterates grid.routes for foreign vias.
    grid.routes = []
    # Issue #2960: disable the via R-tree by default so the fallback
    # linear scan path is exercised (see ``_make_mock_grid``).
    grid._via_rtree = None
    grid._via_rtree_items = {}
    grid._via_rtree_count = 0

    pad_cell = MagicMock()
    pad_cell.blocked = blocked
    pad_cell.pad_blocked = pad_blocked
    pad_cell.is_obstacle = is_obstacle
    pad_cell.net = cell_net

    mock_layer = MagicMock()
    mock_row = MagicMock()
    mock_row.__getitem__ = MagicMock(return_value=pad_cell)
    mock_layer.__getitem__ = MagicMock(return_value=mock_row)
    grid.grid = MagicMock()
    grid.grid.__getitem__ = MagicMock(return_value=mock_layer)

    return grid


class TestGridCollisionCheckerPadBlocked:
    """Issue #2758: GridCollisionChecker must block paths through pad copper
    even when the pad belongs to a skipped pour net (cell.net == 0).

    Background: ``load_pcb_for_routing(skip_nets=["GND", "+1V2", ...])`` maps
    every pad on a skipped net to ``net_num = 0`` so the pad is registered as
    an obstacle only, not as a routable net.  ``_add_pad_unsafe`` then marks
    those cells as ``pad_blocked=True`` (pad metal) and ``cell.net = 0``.
    Pre-fix, ``path_is_clear`` only rejected cells with ``is_obstacle=True``
    or ``cell.net != 0 and cell.net != exclude_net`` -- so pad metal on a
    skipped net was silently treated as clear, allowing optimizer shortcuts
    to cut across BGA pad copper on F.Cu.  This produced the 7-violation U4
    TMDS cluster on board 07 once PR #2753's coord-space fix exposed it.
    """

    def test_pad_metal_on_skip_net_blocks_path(self):
        """Pad-metal cell with cell.net=0 (skip_net pad) must block path."""
        grid = _make_mock_grid_with_pad_cell(pad_blocked=True, cell_net=0, is_obstacle=False)
        checker = GridCollisionChecker(grid)
        # Trace from net 26 (TMDS_D2_N) tries to cross pad metal of a
        # +1V2/GND pad -- must be rejected.
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.15, exclude_net=26)
        assert result is False, (
            "Path through pad metal on skipped pour net (cell.net=0) "
            "must be rejected even when is_obstacle=False"
        )

    def test_pad_metal_on_other_net_blocks_path(self):
        """Pad-metal cell belonging to a different routable net blocks path."""
        grid = _make_mock_grid_with_pad_cell(pad_blocked=True, cell_net=5, is_obstacle=False)
        checker = GridCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.15, exclude_net=26)
        assert result is False

    def test_own_pad_metal_does_not_block_path(self):
        """Trace's own-net pad metal does NOT block the trace.

        A trace must be able to reach its own pad.
        """
        grid = _make_mock_grid_with_pad_cell(pad_blocked=True, cell_net=26, is_obstacle=False)
        checker = GridCollisionChecker(grid)
        # Trace on net 26 reaching its own pad (also net 26).
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.15, exclude_net=26)
        assert result is True, "A trace must be allowed to terminate at its own-net pad"

    def test_pad_clearance_halo_with_net_blocks_other_net(self):
        """Clearance-halo cells (pad_blocked=False, cell.net=padnet) block
        traces on a different net via the existing cell.net check.

        This is the pre-existing behavior and is not changed by the fix.
        """
        grid = _make_mock_grid_with_pad_cell(
            pad_blocked=False, cell_net=5, is_obstacle=False, blocked=True
        )
        checker = GridCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.15, exclude_net=26)
        assert result is False


class TestVectorCollisionCheckerPadBlocked:
    """Issue #2758 mirror for VectorCollisionChecker._check_obstacles_clear."""

    def test_pad_metal_on_skip_net_blocks_path(self):
        """VectorCollisionChecker must also reject pad metal on cell.net=0."""
        grid = _make_mock_grid_with_pad_cell(pad_blocked=True, cell_net=0, is_obstacle=False)
        # Set up minimal R-tree so VectorCollisionChecker runs its own logic
        # rather than falling back to GridCollisionChecker.
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._rtree_available = True
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: {}}

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.15, exclude_net=26)
        assert result is False, "VectorCollisionChecker must also reject pad metal on skip nets"

    def test_own_pad_metal_does_not_block_path(self):
        """Own-net pad metal must NOT block VectorCollisionChecker either."""
        grid = _make_mock_grid_with_pad_cell(pad_blocked=True, cell_net=26, is_obstacle=False)
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._rtree_available = True
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: {}}

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(0, 0, 5, 0, Layer.F_CU, 0.15, exclude_net=26)
        assert result is True

        grid2 = _make_mock_grid(rtree_available=False)
        checker2 = make_collision_checker(grid2, ignore_overflow=True)
        assert isinstance(checker2, GridCollisionChecker)
        assert checker2.ignore_overflow is True


# ---------------------------------------------------------------------------
# Issue #2955: VectorCollisionChecker must reject paths that graze or punch
# foreign-net through-hole vias.  The pre-fix VectorCollisionChecker only
# consulted the R-tree (segments only) and ``_check_obstacles_clear``
# (pads/keepouts only), so the trace optimizer's ``compress_staircase`` /
# ``convert_45_corners`` passes could rewrite a clearance-respecting zigzag
# into a diagonal that crossed a foreign via.  Board-03 canonical:
# XTAL1's B.Cu trace was compressed into a single off-grid segment passing
# 0.14 mm from XTAL2's via at world (125.6, 128.3), producing the
# ``clearance_segment_segment`` / ``clearance_segment_via`` DRC pair.
#
# ``GridCollisionChecker`` was implicitly safe because ``_mark_via`` paints
# ``cell.net = via.net`` on every blocked cell within the via's clearance
# envelope, so the Bresenham walk's ``cell.net != exclude_net`` soft-block
# branch rejects the path.  This test suite pins the corresponding behaviour
# into the R-tree path.
# ---------------------------------------------------------------------------


def _make_via(x: float, y: float, net: int, *, diameter: float = 0.6, drill: float = 0.3):
    """Build a through-hole Via primitive for VectorCollisionChecker tests."""
    from kicad_tools.router.primitives import Via

    return Via(
        x=x,
        y=y,
        drill=drill,
        diameter=diameter,
        layers=(Layer.F_CU, Layer.B_CU),
        net=net,
    )


def _make_route_with_via(net: int, via):
    """Build a Route containing a single via and no segments."""
    from kicad_tools.router.primitives import Route

    route = Route(net=net, net_name=f"net_{net}")
    route.vias.append(via)
    return route


class TestVectorCollisionCheckerForeignVia:
    """Issue #2955: VectorCollisionChecker rejects paths punching foreign vias."""

    def test_path_through_foreign_via_blocked(self):
        """Path whose center passes through a foreign-net via must be rejected.

        Canonical board-03 XTAL geometry: XTAL1 B.Cu trace from
        (128.68, 128.01) -> (122.71, 128.31) parametrically passes through
        x = 125.6 at y ~= 128.16, only 0.14 mm from XTAL2's via centre at
        (125.6, 128.3).  Edge-to-edge clearance with via_radius=0.3 and
        trace_half_width=0.1: 0.14 - 0.3 - 0.1 = -0.26 mm -- well below
        the 0.15 mm minimum clearance.
        """
        via = _make_via(125.6, 128.3, net=16)  # XTAL2 via
        route = _make_route_with_via(net=16, via=via)
        # No foreign segments in the R-tree -- isolate the via path.
        grid = _make_mock_grid(routes=[route])
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: {}}

        checker = VectorCollisionChecker(grid)
        # XTAL1 (net 15) compressed B.Cu segment -- the path the optimizer
        # produced pre-fix.  Must be rejected.
        result = checker.path_is_clear(
            128.68,
            128.01,
            122.71,
            128.31,
            Layer.B_CU,
            0.2,
            exclude_net=15,
        )
        assert result is False, (
            "Optimizer path through XTAL2 via must be rejected by "
            "VectorCollisionChecker (issue #2955)."
        )

    def test_own_net_via_does_not_block_path(self):
        """A via on the trace's own net must NOT block the trace.

        A trace must be allowed to terminate at / extend from its own via.
        """
        via = _make_via(2.5, 0.0, net=1)
        route = _make_route_with_via(net=1, via=via)
        grid = _make_mock_grid(routes=[route])
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: {}}

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.F_CU,
            0.2,
            exclude_net=1,
        )
        assert result is True

    def test_via_well_outside_clearance_does_not_block(self):
        """A foreign via outside the clearance envelope must NOT block."""
        # Via 5 mm above the trace -- nowhere near clearance.
        via = _make_via(2.5, 5.0, net=2)
        route = _make_route_with_via(net=2, via=via)
        grid = _make_mock_grid(routes=[route])
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: {}}

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.F_CU,
            0.2,
            exclude_net=1,
        )
        assert result is True

    def test_via_within_clearance_blocks_path(self):
        """A foreign via whose envelope grazes the trace must block.

        Geometry: via at (2.5, 0.5) with diameter 0.6 (radius 0.3).
        Trace half-width 0.1.  Required clearance 0.15.
        Distance from via centre to trace line = 0.5 mm.
        Edge-to-edge clearance = 0.5 - 0.3 - 0.1 = 0.1 mm < 0.15 mm.
        """
        via = _make_via(2.5, 0.5, net=2)
        route = _make_route_with_via(net=2, via=via)
        grid = _make_mock_grid(routes=[route])
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._seg_rtree = {0: mock_rtree}
        grid._seg_rtree_items = {0: {}}

        checker = VectorCollisionChecker(grid)
        result = checker.path_is_clear(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.F_CU,
            0.2,
            exclude_net=1,
        )
        assert result is False

    def test_via_on_different_layer_does_not_block_blind_via(self):
        """A blind via that does not touch the trace's layer must NOT block.

        Synthetic blind via case: via spans (F.Cu, F.Cu) -- only F.Cu.
        Trace on B.Cu (layer index 1) must not be rejected.  This guards
        the layer-aware ``_via_on_layer`` helper.
        """
        from kicad_tools.router.primitives import Via

        via = Via(
            x=2.5,
            y=0.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.F_CU),  # F.Cu-only "blind" via
            net=2,
        )
        route = _make_route_with_via(net=2, via=via)
        grid = _make_mock_grid(routes=[route])

        # Override layer_to_index so F.Cu=0, B.Cu=1
        def _layer_to_index(name: str) -> int:
            return {"F.Cu": 0, "B.Cu": 1}.get(name, 0)

        grid.layer_to_index = MagicMock(side_effect=_layer_to_index)
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._seg_rtree = {1: mock_rtree}  # B.Cu R-tree
        grid._seg_rtree_items = {1: {}}

        checker = VectorCollisionChecker(grid)
        # Trace on B.Cu, the F.Cu-only via should be ignored on B.Cu.
        result = checker.path_is_clear(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.B_CU,
            0.2,
            exclude_net=1,
        )
        assert result is True, (
            "Blind via that does not touch the trace's layer must not block the path."
        )

    def test_through_hole_via_blocks_on_both_layers(self):
        """A TH via (F.Cu <-> B.Cu) must block on F.Cu AND B.Cu."""
        via = _make_via(2.5, 0.0, net=2)
        route = _make_route_with_via(net=2, via=via)
        grid = _make_mock_grid(routes=[route])

        def _layer_to_index(name: str) -> int:
            return {"F.Cu": 0, "B.Cu": 1}.get(name, 0)

        grid.layer_to_index = MagicMock(side_effect=_layer_to_index)
        mock_rtree = MagicMock()
        mock_rtree.intersection = MagicMock(return_value=[])
        grid._seg_rtree = {0: mock_rtree, 1: mock_rtree}
        grid._seg_rtree_items = {0: {}, 1: {}}

        checker = VectorCollisionChecker(grid)
        # F.Cu trace through the via -- must reject.
        f_result = checker.path_is_clear(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.F_CU,
            0.2,
            exclude_net=1,
        )
        assert f_result is False
        # B.Cu trace through the via -- must also reject.
        b_result = checker.path_is_clear(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.B_CU,
            0.2,
            exclude_net=1,
        )
        assert b_result is False
