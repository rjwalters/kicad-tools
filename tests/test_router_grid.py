"""Tests for router/grid.py module."""

import pytest

from kicad_tools.exceptions import RoutingError
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.heuristics import (
    DEFAULT_HEURISTIC,
    DIAGONAL_COST,
    CongestionAwareHeuristic,
    DirectionBiasHeuristic,
    GreedyHeuristic,
    HeuristicContext,
    ManhattanHeuristic,
    WeightedCongestionHeuristic,
    octile_distance,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import GridCell, Obstacle, Pad, Point, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


class TestRoutingGridBasic:
    """Basic tests for RoutingGrid class."""

    @pytest.fixture
    def default_rules(self):
        """Create default design rules."""
        return DesignRules()

    @pytest.fixture
    def small_grid(self, default_rules):
        """Create a small routing grid for testing."""
        return RoutingGrid(width=10.0, height=10.0, rules=default_rules)

    def test_grid_initialization(self, small_grid):
        """Test grid initialization with defaults."""
        assert small_grid.width == 10.0
        assert small_grid.height == 10.0
        assert small_grid.num_layers >= 2
        assert small_grid.cols > 0
        assert small_grid.rows > 0

    def test_grid_with_origin(self, default_rules):
        """Test grid with custom origin."""
        grid = RoutingGrid(width=10.0, height=10.0, rules=default_rules, origin_x=5.0, origin_y=5.0)
        assert grid.origin_x == 5.0
        assert grid.origin_y == 5.0

    def test_grid_with_layer_stack(self, default_rules):
        """Test grid with custom layer stack."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(width=10.0, height=10.0, rules=default_rules, layer_stack=stack)
        assert grid.num_layers == 4

    def test_grid_cell_access(self, small_grid):
        """Test accessing grid cells."""
        cell = small_grid.grid[0][0][0]
        assert cell is not None
        assert cell.x == 0
        assert cell.y == 0
        assert cell.layer == 0

    def test_grid_layers_alias(self, small_grid):
        """Test that layers alias equals num_layers."""
        assert small_grid.layers == small_grid.num_layers


class TestRoutingGridCoordinates:
    """Tests for coordinate conversion methods."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_world_to_grid(self, grid):
        """Test world to grid conversion."""
        gx, gy = grid.world_to_grid(1.0, 2.0)
        assert gx == 10  # 1.0 / 0.1 = 10
        assert gy == 20  # 2.0 / 0.1 = 20

    def test_world_to_grid_clamping(self, grid):
        """Test world to grid clamping at boundaries."""
        gx, gy = grid.world_to_grid(-5.0, -5.0)
        assert gx == 0
        assert gy == 0

        gx, gy = grid.world_to_grid(100.0, 100.0)
        assert gx == grid.cols - 1
        assert gy == grid.rows - 1

    def test_grid_to_world(self, grid):
        """Test grid to world conversion."""
        x, y = grid.grid_to_world(10, 20)
        assert abs(x - 1.0) < 0.001
        assert abs(y - 2.0) < 0.001

    def test_roundtrip_conversion(self, grid):
        """Test roundtrip coordinate conversion."""
        original_x, original_y = 2.5, 3.7
        gx, gy = grid.world_to_grid(original_x, original_y)
        back_x, back_y = grid.grid_to_world(gx, gy)
        # Should be within one grid step
        assert abs(back_x - original_x) <= grid.resolution
        assert abs(back_y - original_y) <= grid.resolution

    def test_world_to_grid_floating_point_precision(self):
        """Test that world_to_grid handles floating point precision correctly.

        This tests the bug fix for issue #278 where coordinates like 112.6
        with origin 75.0 and resolution 0.1 would produce:
            (112.6 - 75.0) / 0.1 = 375.9999999999999

        Using int() would truncate to 375, but the correct answer is 376.
        Using round() fixes this.
        """
        rules = DesignRules(grid_resolution=0.1)
        # Use origin that triggers floating point precision issues
        grid = RoutingGrid(width=100.0, height=100.0, rules=rules, origin_x=75.0, origin_y=75.0)

        # These coordinates triggered the bug before the fix
        # (112.6 - 75.0) / 0.1 = 375.9999999999999 -> should be 376
        gx, gy = grid.world_to_grid(112.6, 112.6)
        assert gx == 376, f"Expected 376 but got {gx} - floating point precision bug"
        assert gy == 376, f"Expected 376 but got {gy} - floating point precision bug"

        # Test another edge case
        # (112.8 - 75.0) / 0.1 = 377.9999999999999 -> should be 378
        gx, gy = grid.world_to_grid(112.8, 112.8)
        assert gx == 378, f"Expected 378 but got {gx} - floating point precision bug"
        assert gy == 378, f"Expected 378 but got {gy} - floating point precision bug"

    def test_grid_to_world_fine_grid_precision(self):
        """Test that grid_to_world handles fine grid resolutions correctly (Issue #1005).

        With very fine grid resolutions like 0.025mm, floating point arithmetic
        can produce values like 75.17500000000001 instead of 75.175 due to
        binary floating point representation issues.

        For example: 75.0 + 7 * 0.025 should equal 75.175
        But in Python: 75.0 + 7 * 0.025 = 75.17500000000001

        The fix rounds the result to 4 decimal places (0.1 micron precision)
        to ensure clean coordinate values that KiCad can parse correctly.
        """
        rules = DesignRules(grid_resolution=0.025)
        grid = RoutingGrid(width=50.0, height=50.0, rules=rules, origin_x=75.0, origin_y=75.0)

        # Test multiple grid positions known to cause floating point issues
        test_cases = [
            # (gx, gy, expected_x, expected_y)
            (0, 0, 75.0, 75.0),
            (1, 1, 75.025, 75.025),
            (7, 7, 75.175, 75.175),  # 75.0 + 7 * 0.025 = 75.175
            (100, 100, 77.5, 77.5),
            (200, 200, 80.0, 80.0),
            (400, 400, 85.0, 85.0),
        ]

        for gx, gy, expected_x, expected_y in test_cases:
            x, y = grid.grid_to_world(gx, gy)

            # Verify the result is exactly the expected value (not close, but exact)
            assert x == expected_x, (
                f"grid_to_world({gx}, {gy})[0] = {x!r}, expected {expected_x!r}. "
                f"Floating point precision issue not fixed."
            )
            assert y == expected_y, (
                f"grid_to_world({gx}, {gy})[1] = {y!r}, expected {expected_y!r}. "
                f"Floating point precision issue not fixed."
            )

    def test_grid_to_world_roundtrip_fine_grid(self):
        """Test world_to_grid -> grid_to_world roundtrip with fine grid.

        This verifies that converting world coordinates to grid and back
        produces clean, quantized values suitable for PCB output.
        """
        rules = DesignRules(grid_resolution=0.025)
        grid = RoutingGrid(width=50.0, height=50.0, rules=rules, origin_x=75.0, origin_y=75.0)

        # A coordinate that should snap to grid
        world_x, world_y = 75.175, 75.175
        gx, gy = grid.world_to_grid(world_x, world_y)
        back_x, back_y = grid.grid_to_world(gx, gy)

        # Result should be clean (no floating point noise)
        assert back_x == 75.175, f"Expected 75.175, got {back_x!r}"
        assert back_y == 75.175, f"Expected 75.175, got {back_y!r}"

        # Verify it's not the raw floating point result
        raw_x = 75.0 + gx * 0.025
        if raw_x != 75.175:
            # If raw calculation has FP noise, our rounded version should be cleaner
            assert back_x == 75.175, (
                f"grid_to_world should round to clean value. Raw: {raw_x!r}, got: {back_x!r}"
            )


class TestRoutingGridLayers:
    """Tests for layer management methods."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        return RoutingGrid(width=10.0, height=10.0, rules=rules, layer_stack=stack)

    def test_layer_to_index(self, grid):
        """Test layer enum to grid index mapping."""
        # F.Cu should map to index 0
        idx = grid.layer_to_index(Layer.F_CU.value)
        assert idx == 0

    def test_layer_to_index_invalid(self, grid):
        """Test invalid layer raises error."""
        with pytest.raises(RoutingError):
            grid.layer_to_index(999)

    def test_index_to_layer(self, grid):
        """Test grid index to layer enum mapping."""
        layer_val = grid.index_to_layer(0)
        assert layer_val == Layer.F_CU.value

    def test_index_to_layer_invalid(self, grid):
        """Test invalid index raises error."""
        with pytest.raises(RoutingError):
            grid.index_to_layer(999)

    def test_get_routable_indices(self, grid):
        """Test getting routable layer indices."""
        indices = grid.get_routable_indices()
        assert len(indices) > 0
        assert all(isinstance(i, int) for i in indices)

    def test_is_plane_layer(self, grid):
        """Test plane layer detection."""
        # In 4-layer SIG-GND-PWR-SIG stack, inner layers (1, 2) are power planes
        assert grid.is_plane_layer(1) is True
        assert grid.is_plane_layer(2) is True
        # Outer layers are signal
        assert grid.is_plane_layer(0) is False
        assert grid.is_plane_layer(3) is False


class TestRoutingGridCongestion:
    """Tests for congestion tracking methods."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1, congestion_grid_size=10)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_initial_congestion(self, grid):
        """Test initial congestion is zero."""
        congestion = grid.get_congestion(0, 0, 0)
        assert congestion == 0.0

    def test_update_congestion(self, grid):
        """Test updating congestion."""
        # Update congestion at a cell
        grid._update_congestion(5, 5, 0, delta=1)
        congestion = grid.get_congestion(5, 5, 0)
        assert congestion > 0.0

    def test_congestion_map(self, grid):
        """Test getting congestion statistics."""
        stats = grid.get_congestion_map()
        assert "max_congestion" in stats
        assert "avg_congestion" in stats
        assert "congested_regions" in stats
        assert stats["max_congestion"] >= 0.0
        assert stats["avg_congestion"] >= 0.0


class TestRoutingGridObstacles:
    """Tests for obstacle handling."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_add_rectangular_obstacle(self, grid):
        """Test adding a rectangular obstacle."""
        obs = Obstacle(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, clearance=0.1)
        grid.add_obstacle(obs)

        # Check that cells within obstacle are blocked
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        assert cell.blocked is True

    def test_add_pad_smd(self, grid):
        """Test adding an SMD pad as obstacle."""
        pad = Pad(x=3.0, y=3.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="NET1")
        grid.add_pad(pad)

        # Pad center should be marked with net
        gx, gy = grid.world_to_grid(3.0, 3.0)
        cell = grid.grid[0][gy][gx]
        assert cell.net == 1

    def test_add_pad_through_hole(self, grid):
        """Test adding a through-hole pad."""
        pad = Pad(
            x=3.0,
            y=3.0,
            width=1.7,
            height=1.7,
            layer=Layer.F_CU,
            net=1,
            net_name="NET1",
            through_hole=True,
            drill=1.0,
        )
        grid.add_pad(pad)

        # PTH pad should block all layers
        gx, gy = grid.world_to_grid(3.0, 3.0)
        for layer_idx in range(grid.num_layers):
            cell = grid.grid[layer_idx][gy][gx]
            assert cell.net == 1


class TestRoutingGridRoutes:
    """Tests for route management."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_mark_segment(self, grid):
        """Test marking a route segment."""
        seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
        grid.mark_route(route)

        # Check that cells along segment are marked
        gx1, gy1 = grid.world_to_grid(1.0, 1.0)
        cell = grid.grid[0][gy1][gx1]
        assert cell.net == 1
        assert cell.blocked is True

    def test_mark_via(self, grid):
        """Test marking a via."""
        via = Via(x=5.0, y=5.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
        route = Route(net=2, net_name="NET2", segments=[], vias=[via])
        grid.mark_route(route)

        gx, gy = grid.world_to_grid(5.0, 5.0)
        # Via should mark cells on all layers
        for layer_idx in range(grid.num_layers):
            cell = grid.grid[layer_idx][gy][gx]
            assert cell.net == 2

    def test_mark_and_unmark_route(self, grid):
        """Test marking and unmarking a complete route."""
        seg1 = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(segments=[seg1], vias=[], net=1, net_name="NET1")

        grid.mark_route(route)
        assert route in grid.routes

        grid.unmark_route(route)
        assert route not in grid.routes


class TestRoutingGridIsBlocked:
    """Tests for is_blocked method."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_is_blocked_unblocked(self, grid):
        """Test is_blocked for unblocked cell."""
        assert grid.is_blocked(5, 5, Layer.F_CU) is False

    def test_is_blocked_after_obstacle(self, grid):
        """Test is_blocked after adding obstacle."""
        obs = Obstacle(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, clearance=0.0)
        grid.add_obstacle(obs)

        gx, gy = grid.world_to_grid(5.0, 5.0)
        assert grid.is_blocked(gx, gy, Layer.F_CU) is True

    def test_is_blocked_out_of_bounds(self, grid):
        """Test is_blocked for out-of-bounds coordinates."""
        assert grid.is_blocked(-1, 0, Layer.F_CU) is True
        assert grid.is_blocked(0, -1, Layer.F_CU) is True
        assert grid.is_blocked(grid.cols, 0, Layer.F_CU) is True
        assert grid.is_blocked(0, grid.rows, Layer.F_CU) is True


class TestRoutingGridKeepout:
    """Tests for keepout regions."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_add_keepout(self, grid):
        """Test adding a keepout region."""
        grid.add_keepout(2.0, 2.0, 4.0, 4.0)

        gx, gy = grid.world_to_grid(3.0, 3.0)
        # Should block all routable layers
        for layer_idx in grid.get_routable_indices():
            assert grid.grid[layer_idx][gy][gx].blocked is True

    def test_add_keepout_specific_layers(self, grid):
        """Test adding a keepout region on specific layers."""
        grid.add_keepout(2.0, 2.0, 4.0, 4.0, layers=[Layer.F_CU])

        gx, gy = grid.world_to_grid(3.0, 3.0)
        # Only F.Cu (index 0) should be blocked
        assert grid.grid[0][gy][gx].blocked is True


class TestRoutingGridNegotiatedCongestion:
    """Tests for negotiated congestion routing support."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_reset_route_usage(self, grid):
        """Test resetting route usage counts."""
        # Manually set some usage
        grid.grid[0][0][0].usage_count = 5
        grid.reset_route_usage()
        assert grid.grid[0][0][0].usage_count == 0

    def test_mark_route_usage(self, grid):
        """Test marking route usage."""
        seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])

        cells_used = grid.mark_route_usage(route)
        assert len(cells_used) > 0

        # Check that usage count is incremented
        for gx, gy, layer_idx in cells_used:
            assert grid.grid[layer_idx][gy][gx].usage_count >= 1

    def test_unmark_route_usage(self, grid):
        """Test unmarking route usage."""
        seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])

        grid.mark_route_usage(route)
        grid.unmark_route_usage(route)

        # Usage should be back to 0
        gx, gy = grid.world_to_grid(1.0, 1.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        assert grid.grid[layer_idx][gy][gx].usage_count == 0

    def test_find_overused_cells(self, grid):
        """Test finding overused cells."""
        # Mark same route twice to create overuse
        seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
        route1 = Route(net=1, net_name="NET1", segments=[seg], vias=[])
        route2 = Route(net=2, net_name="NET2", segments=[seg], vias=[])

        grid.mark_route_usage(route1)
        grid.mark_route_usage(route2)

        overused = grid.find_overused_cells()
        assert len(overused) > 0

    def test_update_history_costs(self, grid):
        """Test updating history costs for overused cells."""
        # Create overuse
        grid.grid[0][0][0].usage_count = 3

        grid.update_history_costs(history_increment=2.0)
        # History cost should be 2.0 * (3 - 1) = 4.0
        assert grid.grid[0][0][0].history_cost == 4.0

    def test_get_negotiated_cost(self, grid):
        """Test getting negotiated congestion cost."""
        # Empty cell should have zero cost
        cost = grid.get_negotiated_cost(5, 5, 0)
        assert cost == 0.0

        # Set usage and history
        grid.grid[0][5][5].usage_count = 2
        grid.grid[0][5][5].history_cost = 1.5

        cost = grid.get_negotiated_cost(5, 5, 0, present_cost_factor=1.0)
        # present_cost = 1.0 * 2 = 2.0, history_cost = 1.5, total = 3.5
        assert cost == 3.5

    def test_get_negotiated_cost_obstacle(self, grid):
        """Test that obstacles return infinite cost."""
        grid.grid[0][5][5].is_obstacle = True
        cost = grid.get_negotiated_cost(5, 5, 0)
        assert cost == float("inf")

    def test_get_negotiated_cost_out_of_bounds(self, grid):
        """Test out-of-bounds returns infinite cost."""
        cost = grid.get_negotiated_cost(-1, 0, 0)
        assert cost == float("inf")

    def test_get_total_overflow(self, grid):
        """Test getting total overflow count."""
        # No overflow initially
        assert grid.get_total_overflow() == 0

        # Create overuse
        grid.grid[0][0][0].usage_count = 3  # overflow = 2
        grid.grid[0][1][1].usage_count = 2  # overflow = 1

        assert grid.get_total_overflow() == 3


class TestRoutingGridSegmentCells:
    """Tests for segment cell tracking."""

    @pytest.fixture
    def grid(self):
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules)

    def test_get_segment_cells_horizontal(self, grid):
        """Test getting cells for horizontal segment."""
        seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
        cells = grid._get_segment_cells(seg)

        assert len(cells) > 0
        # All cells should be on same layer and y coordinate
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        gy = grid.world_to_grid(1.0, 1.0)[1]
        for _gx, cell_gy, cell_layer in cells:
            assert cell_gy == gy
            assert cell_layer == layer_idx

    def test_get_segment_cells_vertical(self, grid):
        """Test getting cells for vertical segment."""
        seg = Segment(x1=1.0, y1=1.0, x2=1.0, y2=2.0, width=0.2, layer=Layer.F_CU, net=1)
        cells = grid._get_segment_cells(seg)

        assert len(cells) > 0
        # All cells should be on same layer and x coordinate
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        gx = grid.world_to_grid(1.0, 1.0)[0]
        for cell_gx, _gy, cell_layer in cells:
            assert cell_gx == gx
            assert cell_layer == layer_idx

    def test_get_segment_cells_diagonal(self, grid):
        """Test getting cells for diagonal segment."""
        seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=2.0, width=0.2, layer=Layer.F_CU, net=1)
        cells = grid._get_segment_cells(seg)

        assert len(cells) > 0

    def test_get_via_cells(self, grid):
        """Test getting cells for via."""
        via = Via(x=5.0, y=5.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        cells = grid._get_via_cells(via)

        # Should have one cell per layer
        assert len(cells) == grid.num_layers


class TestLayerStackPresets:
    """Tests for LayerStack preset methods."""

    def test_two_layer(self):
        """Test two-layer stack creation."""
        stack = LayerStack.two_layer()
        assert stack.num_layers == 2
        assert len(stack.signal_layers) == 2
        assert len(stack.plane_layers) == 0

    def test_four_layer_sig_gnd_pwr_sig(self):
        """Test 4-layer SIG-GND-PWR-SIG stack."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        assert stack.num_layers == 4
        assert len(stack.signal_layers) == 4  # All layers routable
        assert len(stack.plane_layers) == 2  # Plane metadata preserved

    def test_four_layer_sig_sig_gnd_pwr(self):
        """Test 4-layer SIG-SIG-GND-PWR stack."""
        stack = LayerStack.four_layer_sig_sig_gnd_pwr()
        assert stack.num_layers == 4

    def test_six_layer(self):
        """Test 6-layer stack creation."""
        stack = LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()
        assert stack.num_layers == 6
        assert len(stack.signal_layers) == 6  # All layers routable
        assert len(stack.plane_layers) == 2  # Plane metadata preserved

    def test_outer_layers(self):
        """Test outer layers property."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        outer = stack.outer_layers
        assert len(outer) == 2
        assert all(layer.is_outer for layer in outer)

    def test_get_layer_by_index(self):
        """Test getting layer by index."""
        stack = LayerStack.two_layer()
        layer = stack.get_layer(0)
        assert layer is not None
        assert layer.name == "F.Cu"

    def test_get_layer_by_name(self):
        """Test getting layer by name."""
        stack = LayerStack.two_layer()
        layer = stack.get_layer_by_name("B.Cu")
        assert layer is not None
        assert layer.index == 1


class TestRipUpRoutePreservesPadClearance:
    """Tests for issue #292: Pad clearance zones must not be corrupted during rip-up/reroute.

    When a route's clearance zone overlaps with a pad's clearance zone, the pad's
    cells should NOT have their net overwritten. This prevents the cells from being
    incorrectly cleared during route rip-up.
    """

    @pytest.fixture
    def grid(self):
        """Create a routing grid with fine resolution for precise testing."""
        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.2)
        return RoutingGrid(width=20.0, height=20.0, rules=rules)

    def test_segment_near_pad_preserves_pad_net_after_ripup(self, grid):
        """Test that unmarking a segment doesn't clear pad blocking.

        Scenario:
        1. Add a pad at (5.0, 5.0) with net=1
        2. Add a segment that passes near the pad (overlapping clearance zones)
        3. Unmark (rip-up) the segment
        4. Verify the pad's cells still have net=1 and are still blocked
        """
        # Add a pad at (5.0, 5.0)
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="PAD_NET")
        grid.add_pad(pad)

        # Get pad center position for verification
        pad_gx, pad_gy = grid.world_to_grid(5.0, 5.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Verify pad is properly set up
        pad_cell = grid.grid[layer_idx][pad_gy][pad_gx]
        assert pad_cell.blocked is True, "Pad center should be blocked"
        assert pad_cell.net == 1, "Pad center should have net=1"

        # Create a segment that passes near the pad (within clearance zone)
        # Segment runs from (4.5, 5.0) to (5.5, 5.0) - passing through pad area
        seg = Segment(x1=4.0, y1=5.0, x2=6.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=2)
        route = Route(net=2, net_name="ROUTE_NET", segments=[seg], vias=[])

        # Mark the route (this should NOT overwrite the pad's net)
        grid.mark_route(route)

        # Now rip-up the route
        grid.unmark_route(route)

        # CRITICAL: Verify pad's cells are still blocked and have the correct net
        pad_cell_after = grid.grid[layer_idx][pad_gy][pad_gx]
        assert pad_cell_after.blocked is True, "Pad center should still be blocked after rip-up"
        assert pad_cell_after.net == 1, "Pad center should still have net=1 after rip-up"

    def test_via_near_pad_preserves_pad_net_after_ripup(self, grid):
        """Test that unmarking a via doesn't clear pad blocking.

        Scenario similar to segment test but with vias.
        """
        # Add a PTH pad at (5.0, 5.0)
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.7,
            height=1.7,
            layer=Layer.F_CU,
            net=1,
            net_name="PAD_NET",
            through_hole=True,
            drill=1.0,
        )
        grid.add_pad(pad)

        # Get pad center position
        pad_gx, pad_gy = grid.world_to_grid(5.0, 5.0)

        # Verify pad is properly set up on all layers
        for layer_idx in range(grid.num_layers):
            pad_cell = grid.grid[layer_idx][pad_gy][pad_gx]
            assert pad_cell.blocked is True
            assert pad_cell.net == 1

        # Create a via near the pad (within clearance zone)
        via = Via(x=5.5, y=5.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
        route = Route(net=2, net_name="VIA_NET", segments=[], vias=[via])

        # Mark the route
        grid.mark_route(route)

        # Rip-up the route
        grid.unmark_route(route)

        # Verify pad's cells are still intact on all layers
        for layer_idx in range(grid.num_layers):
            pad_cell_after = grid.grid[layer_idx][pad_gy][pad_gx]
            assert pad_cell_after.blocked is True, (
                f"Pad should still be blocked on layer {layer_idx}"
            )
            assert pad_cell_after.net == 1, f"Pad should still have net=1 on layer {layer_idx}"

    def test_multiple_ripup_iterations_preserve_pad(self, grid):
        """Test that multiple rip-up/reroute cycles don't corrupt pad clearance.

        This simulates the negotiated congestion routing scenario where routes
        are repeatedly ripped up and rerouted.
        """
        # Add a pad
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="PAD_NET")
        grid.add_pad(pad)

        pad_gx, pad_gy = grid.world_to_grid(5.0, 5.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Simulate multiple rip-up/reroute iterations
        for iteration in range(15):
            seg = Segment(x1=4.0, y1=5.0, x2=6.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=2)
            route = Route(net=2, net_name="ROUTE_NET", segments=[seg], vias=[])

            grid.mark_route(route)
            grid.unmark_route(route)

            # After each iteration, pad should still be intact
            pad_cell = grid.grid[layer_idx][pad_gy][pad_gx]
            assert pad_cell.blocked is True, (
                f"Pad should be blocked after iteration {iteration + 1}"
            )
            assert pad_cell.net == 1, f"Pad net corrupted after iteration {iteration + 1}"


class TestRoutingGridThreadSafety:
    """Tests for thread-safe grid operations (issue #573).

    These tests verify that the optional thread-safe mode works correctly
    and that concurrent access to the grid doesn't cause race conditions.
    """

    @pytest.fixture
    def thread_safe_grid(self):
        """Create a thread-safe routing grid."""
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules, thread_safe=True)

    @pytest.fixture
    def non_thread_safe_grid(self):
        """Create a non-thread-safe routing grid (default)."""
        rules = DesignRules(grid_resolution=0.1)
        return RoutingGrid(width=10.0, height=10.0, rules=rules, thread_safe=False)

    def test_thread_safe_property(self, thread_safe_grid, non_thread_safe_grid):
        """Test thread_safe property reflects initialization."""
        assert thread_safe_grid.thread_safe is True
        assert non_thread_safe_grid.thread_safe is False

    def test_thread_safe_grid_has_lock(self, thread_safe_grid):
        """Test that thread-safe grid creates an RLock."""
        assert thread_safe_grid._lock is not None
        import threading

        assert isinstance(thread_safe_grid._lock, type(threading.RLock()))

    def test_non_thread_safe_grid_no_lock(self, non_thread_safe_grid):
        """Test that non-thread-safe grid doesn't create a lock."""
        assert non_thread_safe_grid._lock is None

    def test_locked_context_manager_thread_safe(self, thread_safe_grid):
        """Test locked() context manager works with thread-safe grid."""
        with thread_safe_grid.locked() as grid:
            assert grid is thread_safe_grid
            # Should be able to perform operations
            seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
            route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
            grid.mark_route(route)

    def test_locked_context_manager_non_thread_safe(self, non_thread_safe_grid):
        """Test locked() context manager works with non-thread-safe grid (no-op)."""
        with non_thread_safe_grid.locked() as grid:
            assert grid is non_thread_safe_grid
            # Should be able to perform operations
            seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
            route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
            grid.mark_route(route)

    def test_concurrent_mark_route(self, thread_safe_grid):
        """Test concurrent marking of routes doesn't cause race conditions.

        This test creates multiple routes and marks them concurrently from
        multiple threads. The grid should remain consistent after all
        operations complete.
        """
        import threading

        routes = []
        # Create routes at different locations to avoid overlap
        for i in range(10):
            seg = Segment(
                x1=float(i),
                y1=float(i),
                x2=float(i) + 0.5,
                y2=float(i),
                width=0.2,
                layer=Layer.F_CU,
                net=i + 1,
            )
            routes.append(Route(net=i + 1, net_name=f"NET{i + 1}", segments=[seg], vias=[]))

        errors = []

        def mark_route(route):
            try:
                thread_safe_grid.mark_route(route)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mark_route, args=(route,)) for route in routes]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent mark_route: {errors}"
        assert len(thread_safe_grid.routes) == 10, "All routes should be marked"

    def test_concurrent_mark_and_unmark(self, thread_safe_grid):
        """Test concurrent mark and unmark operations.

        This simulates the negotiated congestion routing scenario where
        routes are repeatedly marked and unmarked.
        """
        import threading

        # Create initial routes
        routes = []
        for i in range(5):
            seg = Segment(
                x1=float(i),
                y1=5.0,
                x2=float(i) + 0.5,
                y2=5.0,
                width=0.2,
                layer=Layer.F_CU,
                net=i + 1,
            )
            routes.append(Route(net=i + 1, net_name=f"NET{i + 1}", segments=[seg], vias=[]))
            thread_safe_grid.mark_route(routes[-1])

        errors = []
        iterations_per_thread = 20

        def mark_unmark_cycle(route, iterations):
            try:
                for _ in range(iterations):
                    thread_safe_grid.unmark_route(route)
                    thread_safe_grid.mark_route(route)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=mark_unmark_cycle, args=(route, iterations_per_thread))
            for route in routes
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent operations: {errors}"
        # All routes should still be marked after mark/unmark cycles
        assert len(thread_safe_grid.routes) == 5

    def test_concurrent_usage_tracking(self, thread_safe_grid):
        """Test concurrent route usage tracking operations."""
        import threading

        routes = []
        for i in range(8):
            seg = Segment(
                x1=float(i),
                y1=float(i),
                x2=float(i) + 0.3,
                y2=float(i),
                width=0.2,
                layer=Layer.F_CU,
                net=i + 1,
            )
            routes.append(Route(net=i + 1, net_name=f"NET{i + 1}", segments=[seg], vias=[]))

        errors = []

        def mark_usage(route):
            try:
                thread_safe_grid.mark_route_usage(route)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mark_usage, args=(route,)) for route in routes]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent usage tracking: {errors}"

    def test_statistics_include_thread_safe(self, thread_safe_grid, non_thread_safe_grid):
        """Test that get_grid_statistics includes thread_safe info."""
        stats_safe = thread_safe_grid.get_grid_statistics()
        stats_unsafe = non_thread_safe_grid.get_grid_statistics()

        assert "thread_safe" in stats_safe
        assert stats_safe["thread_safe"] is True
        assert stats_unsafe["thread_safe"] is False

    def test_default_is_not_thread_safe(self):
        """Test that the default grid is not thread-safe (for performance)."""
        rules = DesignRules()
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        assert grid.thread_safe is False
        assert grid._lock is None

    def test_reentrant_lock_allows_nested_calls(self, thread_safe_grid):
        """Test that RLock allows nested locking (reentrant behavior)."""
        # This should not deadlock because we use RLock
        with thread_safe_grid.locked():
            with thread_safe_grid.locked():
                seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1)
                route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
                thread_safe_grid.mark_route(route)

        assert len(thread_safe_grid.routes) == 1


class TestGeometricClearanceValidation:
    """Tests for geometric clearance validation (Issue #750).

    The grid-based A* pathfinder uses discrete cells for obstacle checking,
    which can miss clearance violations on diagonal segments that cut through
    obstacle corners. These tests verify the geometric validation catches
    such violations.
    """

    @pytest.fixture
    def grid(self):
        """Create a routing grid with pads for clearance testing."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.127,  # Standard clearance
        )
        return RoutingGrid(width=20.0, height=20.0, rules=rules)

    def test_segment_clearance_valid(self, grid):
        """Test segment with sufficient clearance passes validation."""
        # Add a pad at (5.0, 5.0)
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="NET1")
        grid.add_pad(pad)

        # Create a segment far from the pad (should have sufficient clearance)
        seg = Segment(
            x1=10.0, y1=10.0, x2=12.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg, exclude_net=2)

        assert is_valid is True
        assert clearance > grid.rules.trace_clearance
        assert location is None

    def test_segment_clearance_violation_pad(self, grid):
        """Test segment too close to pad is detected as violation."""
        # Add a pad at (5.0, 5.0) with width 1.0
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="NET1")
        grid.add_pad(pad)

        # Create a segment that passes very close to the pad
        # Pad edge is at 5.5, segment at 5.55 with width 0.2 -> edge at 5.45
        # Clearance = 5.55 - 5.5 - 0.1 = -0.05 (violation!)
        seg = Segment(
            x1=5.55, y1=0.0, x2=5.55, y2=10.0, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg, exclude_net=2)

        assert is_valid is False
        assert clearance < grid.rules.trace_clearance
        assert location is not None

    def test_segment_clearance_same_net_ignored(self, grid):
        """Test that same-net pads are ignored in clearance check."""
        # Add a pad at (5.0, 5.0)
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="NET1")
        grid.add_pad(pad)

        # Create a segment from the same net that passes through the pad
        seg = Segment(
            x1=4.0, y1=5.0, x2=6.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg, exclude_net=1)

        # Same-net should be ignored, so no violation
        assert is_valid is True

    def test_segment_clearance_different_layer_smd(self, grid):
        """Test that SMD pads on different layers don't cause violations."""
        # Add an SMD pad on F.Cu
        pad = Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="NET1")
        grid.add_pad(pad)

        # Create a segment on B.Cu that passes through the same location
        seg = Segment(
            x1=4.0, y1=5.0, x2=6.0, y2=5.0, width=0.2, layer=Layer.B_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg, exclude_net=2)

        # Different layers for SMD, so no violation
        assert is_valid is True

    def test_segment_clearance_pth_blocks_all_layers(self, grid):
        """Test that PTH pads block segments on all layers."""
        # Add a PTH pad (blocks all layers)
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.7,
            height=1.7,
            layer=Layer.F_CU,
            net=1,
            net_name="NET1",
            through_hole=True,
            drill=1.0,
        )
        grid.add_pad(pad)

        # Create a segment on B.Cu that passes close to the PTH pad
        seg = Segment(
            x1=5.95, y1=0.0, x2=5.95, y2=10.0, width=0.2, layer=Layer.B_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg, exclude_net=2)

        # PTH should block on all layers, so this should be a violation
        assert is_valid is False

    def test_segment_to_segment_clearance_violation(self, grid):
        """Test clearance violation between two segments."""
        # Add a route with a segment
        seg1 = Segment(
            x1=5.0, y1=0.0, x2=5.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1"
        )
        route1 = Route(net=1, net_name="NET1", segments=[seg1], vias=[])
        grid.mark_route(route1)

        # Create a new segment that runs parallel and too close
        # Segment 1 edge at 5.1, segment 2 edge at 5.1 (overlapping!)
        seg2 = Segment(
            x1=5.2, y1=0.0, x2=5.2, y2=10.0, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg2, exclude_net=2)

        # Segments are too close (0.2 - 0.1 - 0.1 = 0.0, less than 0.127)
        assert is_valid is False
        assert clearance < grid.rules.trace_clearance

    def test_segment_to_segment_clearance_valid(self, grid):
        """Test segment-to-segment clearance when properly spaced."""
        # Add a route with a segment
        seg1 = Segment(
            x1=5.0, y1=0.0, x2=5.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1"
        )
        route1 = Route(net=1, net_name="NET1", segments=[seg1], vias=[])
        grid.mark_route(route1)

        # Create a new segment with sufficient spacing
        # seg1 edge at 5.1, seg2 edge at 5.33, clearance = 0.23 > 0.127
        seg2 = Segment(
            x1=5.53, y1=0.0, x2=5.53, y2=10.0, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg2, exclude_net=2)

        assert is_valid is True
        assert clearance >= grid.rules.trace_clearance

    def test_segment_to_via_clearance_violation(self, grid):
        """Test clearance violation between segment and via."""
        # Add a route with a via
        via = Via(x=5.0, y=5.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        route1 = Route(net=1, net_name="NET1", segments=[], vias=[via])
        grid.mark_route(route1)

        # Create a segment that passes too close to the via
        # Via edge at 5.3, segment edge at 5.3 (overlapping!)
        seg = Segment(
            x1=5.4, y1=0.0, x2=5.4, y2=10.0, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg, exclude_net=2)

        # Segment is too close to via
        assert is_valid is False

    def test_diagonal_segment_corner_violation(self, grid):
        """Test that diagonal segments cutting through obstacle corners are detected.

        This is the core case for Issue #750: diagonal segments can geometrically
        pass through obstacle corners even when grid-based checking approves them.
        """
        # Add two pads creating a narrow gap
        pad1 = Pad(x=5.0, y=5.0, width=1.0, height=1.0, layer=Layer.F_CU, net=1, net_name="NET1")
        pad2 = Pad(x=6.5, y=6.5, width=1.0, height=1.0, layer=Layer.F_CU, net=3, net_name="NET3")
        grid.add_pad(pad1)
        grid.add_pad(pad2)

        # Create a diagonal segment that would cut through the corner between pads
        # This diagonal passes close to pad1's corner at (5.5, 5.5) and pad2's corner at (6.0, 6.0)
        seg = Segment(
            x1=5.3, y1=5.3, x2=6.7, y2=6.7, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2"
        )

        is_valid, clearance, location = grid.validate_segment_clearance(seg, exclude_net=2)

        # The diagonal should violate clearance with at least one of the pads
        assert is_valid is False

    def test_point_to_segment_distance_horizontal(self, grid):
        """Test point-to-segment distance for horizontal segment."""
        # Point directly above segment
        dist = grid._point_to_segment_distance(5.0, 3.0, 0.0, 0.0, 10.0, 0.0)
        assert abs(dist - 3.0) < 0.001

    def test_point_to_segment_distance_endpoint(self, grid):
        """Test point-to-segment distance when closest point is endpoint."""
        # Point beyond segment end
        dist = grid._point_to_segment_distance(15.0, 0.0, 0.0, 0.0, 10.0, 0.0)
        assert abs(dist - 5.0) < 0.001

    def test_segment_to_segment_distance_parallel(self, grid):
        """Test segment-to-segment distance for parallel segments."""
        # Two parallel horizontal segments, 2.0 apart
        dist = grid._segment_to_segment_distance(0.0, 0.0, 10.0, 0.0, 0.0, 2.0, 10.0, 2.0)
        assert abs(dist - 2.0) < 0.001

    def test_segment_to_segment_distance_perpendicular(self, grid):
        """Test segment-to-segment distance for perpendicular segments."""
        # Horizontal and vertical segments, 1.0 apart at closest
        dist = grid._segment_to_segment_distance(0.0, 0.0, 10.0, 0.0, 5.0, 1.0, 5.0, 5.0)
        assert abs(dist - 1.0) < 0.001

    def test_pads_stored_in_grid(self, grid):
        """Test that pads are stored in grid._pads when added."""
        assert len(grid._pads) == 0

        pad1 = Pad(x=1.0, y=1.0, width=0.5, height=0.5, layer=Layer.F_CU, net=1, net_name="NET1")
        pad2 = Pad(x=5.0, y=5.0, width=0.5, height=0.5, layer=Layer.F_CU, net=2, net_name="NET2")

        grid.add_pad(pad1)
        grid.add_pad(pad2)

        assert len(grid._pads) == 2
        assert grid._pads[0] is pad1
        assert grid._pads[1] is pad2


class TestViaClearanceValidation:
    """Tests for validate_via_clearance() -- Issue #1667.

    Verifies that vias are checked against other-net segments for clearance
    violations. This catches seg-via violations that grid-based checking misses
    due to the rectangular approximation of the via envelope.
    """

    @pytest.fixture
    def grid(self):
        """Create a routing grid for via clearance testing."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.127,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
        )
        return RoutingGrid(width=20.0, height=20.0, rules=rules)

    def test_via_clearance_valid(self, grid):
        """Test via with sufficient clearance to other-net segment passes."""
        # Add an existing route with a segment on F.Cu
        other_route = Route(net=1, net_name="NET1")
        other_route.segments.append(
            Segment(x1=2.0, y1=10.0, x2=18.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1")
        )
        grid.routes.append(other_route)

        # Place a via far from the segment (at y=5.0, segment at y=10.0)
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="NET2",
        )

        is_valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)

        assert is_valid is True
        # Distance = 5.0 - 0.35 (via radius) - 0.1 (seg half width) = 4.55
        assert clearance > grid.rules.via_clearance
        assert location is None

    def test_via_clearance_violation(self, grid):
        """Test via too close to other-net segment is detected as violation."""
        # Add an existing route with a horizontal segment on F.Cu at y=5.0
        other_route = Route(net=1, net_name="NET1")
        other_route.segments.append(
            Segment(x1=2.0, y1=5.0, x2=18.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1")
        )
        grid.routes.append(other_route)

        # Place a via very close to the segment
        # Via center at (10.0, 5.5): distance to segment = 0.5
        # Edge clearance = 0.5 - 0.35 (via radius) - 0.1 (seg half width) = 0.05
        # Required via_clearance = 0.2, so this is a violation
        via = Via(
            x=10.0, y=5.5, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="NET2",
        )

        is_valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)

        assert is_valid is False
        assert clearance < grid.rules.via_clearance
        assert location is not None
        assert location == (10.0, 5.5)

    def test_via_clearance_same_net_ignored(self, grid):
        """Test that same-net segments are ignored in via clearance check."""
        # Add a route on the same net as the via
        same_net_route = Route(net=2, net_name="NET2")
        same_net_route.segments.append(
            Segment(x1=2.0, y1=5.0, x2=18.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2")
        )
        grid.routes.append(same_net_route)

        # Place a via overlapping the same-net segment
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="NET2",
        )

        is_valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)

        # Same-net should be excluded, so no violation
        assert is_valid is True

    def test_via_clearance_different_layer(self, grid):
        """Test that segments on layers the via does not span are ignored."""
        # Add a segment on B.Cu
        other_route = Route(net=1, net_name="NET1")
        other_route.segments.append(
            Segment(x1=2.0, y1=5.0, x2=18.0, y2=5.0, width=0.2, layer=Layer.B_CU, net=1, net_name="NET1")
        )
        grid.routes.append(other_route)

        # Create a via that only spans F.Cu (single-layer check scenario)
        # Standard vias span both layers, so this should still catch it.
        # Use a standard via spanning F.Cu and B.Cu
        via = Via(
            x=10.0, y=5.0, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="NET2",
        )

        is_valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)

        # Via spans B.Cu where the segment is, so this SHOULD detect it
        assert is_valid is False

    def test_via_clearance_at_exact_boundary(self, grid):
        """Test via exactly at clearance boundary passes validation."""
        # Edge-to-edge clearance == required_clearance should pass
        # Required clearance = 0.2, via radius = 0.35, seg half width = 0.1
        # Need: distance - 0.35 - 0.1 = 0.2  =>  distance = 0.65
        other_route = Route(net=1, net_name="NET1")
        other_route.segments.append(
            Segment(x1=2.0, y1=5.0, x2=18.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1")
        )
        grid.routes.append(other_route)

        # Via at distance = 0.65 from segment (center y = 5.65)
        via = Via(
            x=10.0, y=5.65, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="NET2",
        )

        is_valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)

        # Clearance = 0.65 - 0.35 - 0.1 = 0.2 == via_clearance => valid
        assert is_valid is True
        assert abs(clearance - 0.2) < 0.01

    def test_via_clearance_diagonal_segment(self, grid):
        """Test via clearance against a diagonal segment."""
        # Add a diagonal segment from (2,2) to (18,18)
        other_route = Route(net=1, net_name="NET1")
        other_route.segments.append(
            Segment(x1=2.0, y1=2.0, x2=18.0, y2=18.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1")
        )
        grid.routes.append(other_route)

        # Place a via very close to the diagonal segment
        # Via at (10.0, 10.3) -- close to line y=x at point (10.15, 10.15)
        # Distance from (10.0, 10.3) to line y=x: |10.3 - 10.0| / sqrt(2) ~ 0.212
        # Edge clearance = 0.212 - 0.35 - 0.1 = -0.238 (violation)
        via = Via(
            x=10.0, y=10.3, drill=0.35, diameter=0.7,
            layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="NET2",
        )

        is_valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)

        assert is_valid is False
        assert clearance < 0  # overlap

    def test_validate_route_clearance_rejects_via_violation(self, grid):
        """Test _validate_route_clearance rejects a route with via-to-segment violation.

        Issue #1667: Verifies the pathfinder integration -- routes with vias that
        violate clearance to other-net segments should be rejected.
        """
        # Add an existing route
        other_route = Route(net=1, net_name="NET1")
        other_route.segments.append(
            Segment(x1=2.0, y1=5.0, x2=18.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1, net_name="NET1")
        )
        grid.routes.append(other_route)

        # Create a new route that has a via violating clearance
        new_route = Route(net=2, net_name="NET2")
        new_route.segments.append(
            Segment(x1=10.0, y1=15.0, x2=10.0, y2=6.0, width=0.2, layer=Layer.F_CU, net=2, net_name="NET2")
        )
        # Via placed too close to NET1's segment
        new_route.vias.append(
            Via(
                x=10.0, y=5.5, drill=0.35, diameter=0.7,
                layers=(Layer.F_CU, Layer.B_CU), net=2, net_name="NET2",
            )
        )

        # Validate via clearance directly (simulating what _validate_route_clearance does)
        via = new_route.vias[0]
        is_valid, clearance, location = grid.validate_via_clearance(via, exclude_net=2)
        assert is_valid is False
        assert clearance < grid.rules.via_clearance


class TestHeuristics:
    """Tests for heuristic classes."""

    def test_manhattan_heuristic(self):
        """Test Manhattan distance heuristic."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            diagonal_routing=False,  # Use Manhattan distance
        )
        heuristic = ManhattanHeuristic()

        # Distance from (0,0) to (10,10) = 20 * cost_straight
        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate == 20.0

    def test_manhattan_heuristic_name(self):
        """Test heuristic name."""
        h = ManhattanHeuristic()
        assert h.name == "Manhattan"

    def test_direction_bias_heuristic(self):
        """Test direction bias heuristic."""
        rules = DesignRules()
        context = HeuristicContext(goal_x=10, goal_y=0, goal_layer=0, rules=rules)
        heuristic = DirectionBiasHeuristic(turn_penalty_factor=0.5)

        # Moving in goal direction
        estimate_aligned = heuristic.estimate(0, 0, 0, (1, 0), context)
        # Moving perpendicular
        estimate_perpendicular = heuristic.estimate(0, 0, 0, (0, 1), context)

        # Perpendicular should have higher cost
        assert estimate_perpendicular > estimate_aligned

    def test_direction_bias_heuristic_name(self):
        """Test direction bias name."""
        h = DirectionBiasHeuristic(turn_penalty_factor=0.5)
        assert "DirectionBias" in h.name
        assert "0.5" in h.name

    def test_congestion_aware_heuristic(self):
        """Test congestion-aware heuristic."""
        rules = DesignRules()

        def get_congestion_cost(x, y, layer):
            return 0.5  # Constant congestion

        context = HeuristicContext(
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            get_congestion_cost=get_congestion_cost,
            diagonal_routing=False,  # Use Manhattan distance for predictable base
        )
        heuristic = CongestionAwareHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Should include congestion cost (base Manhattan = 20)
        assert estimate > 20.0

    def test_congestion_aware_heuristic_name(self):
        """Test congestion-aware name."""
        h = CongestionAwareHeuristic()
        assert h.name == "CongestionAware"

    def test_weighted_congestion_heuristic(self):
        """Test weighted congestion heuristic."""
        rules = DesignRules()

        def get_congestion_cost(x, y, layer):
            return 1.0

        context = HeuristicContext(
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            get_congestion_cost=get_congestion_cost,
            diagonal_routing=False,  # Use Manhattan distance for predictable base
        )
        heuristic = WeightedCongestionHeuristic(num_samples=3, congestion_multiplier=2.0)

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate > 20.0

    def test_weighted_congestion_heuristic_name(self):
        """Test weighted congestion name."""
        h = WeightedCongestionHeuristic(congestion_multiplier=2.0)
        assert "WeightedCongestion" in h.name
        assert "2.0" in h.name

    def test_greedy_heuristic(self):
        """Test greedy heuristic."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            diagonal_routing=False,  # Use Manhattan distance
        )
        heuristic = GreedyHeuristic(greed_factor=2.0)

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Should be 2x Manhattan distance
        assert estimate == 40.0

    def test_greedy_heuristic_name(self):
        """Test greedy heuristic name."""
        h = GreedyHeuristic(greed_factor=2.0)
        assert "Greedy" in h.name
        assert "2.0" in h.name

    def test_default_heuristic(self):
        """Test default heuristic."""
        assert DEFAULT_HEURISTIC is not None
        assert isinstance(DEFAULT_HEURISTIC, CongestionAwareHeuristic)

    def test_heuristic_with_cost_multiplier(self):
        """Test heuristic with net class cost multiplier."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            cost_multiplier=0.5,  # Power net priority
            diagonal_routing=False,  # Use Manhattan distance
        )
        heuristic = ManhattanHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate == 10.0  # 20 * 0.5

    def test_heuristic_layer_change(self):
        """Test heuristic with layer change."""
        rules = DesignRules()
        context = HeuristicContext(goal_x=0, goal_y=0, goal_layer=1, rules=rules)
        heuristic = ManhattanHeuristic()

        # At goal position but different layer
        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate == rules.cost_via  # Layer change cost


class TestRoutingGrid:
    """Tests for RoutingGrid class."""

    def test_grid_creation(self):
        """Test creating a routing grid."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules)

        assert grid.width == 10.0
        assert grid.height == 10.0
        assert grid.resolution == 0.5
        assert grid.cols == 21  # (10 / 0.5) + 1
        assert grid.rows == 21

    def test_grid_with_origin(self):
        """Test grid with custom origin."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=100, origin_y=50)

        assert grid.origin_x == 100
        assert grid.origin_y == 50

    def test_world_to_grid(self):
        """Test world to grid coordinate conversion."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=0, origin_y=0)

        gx, gy = grid.world_to_grid(2.5, 3.5)
        assert gx == 5
        assert gy == 7

    def test_grid_to_world(self):
        """Test grid to world coordinate conversion."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=0, origin_y=0)

        x, y = grid.grid_to_world(5, 7)
        assert x == 2.5
        assert y == 3.5

    def test_world_to_grid_with_origin(self):
        """Test coordinate conversion with origin offset."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=100, origin_y=50)

        gx, gy = grid.world_to_grid(102.5, 53.5)
        assert gx == 5
        assert gy == 7

    def test_layer_stack_default(self):
        """Test default layer stack is 2-layer."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        assert grid.num_layers == 2
        assert grid.layers == 2  # Alias

    def test_layer_stack_custom(self):
        """Test custom layer stack."""
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(10.0, 10.0, rules, layer_stack=stack)

        assert grid.num_layers == 4

    def test_layer_to_index(self):
        """Test layer enum to grid index mapping."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        idx = grid.layer_to_index(Layer.F_CU.value)
        assert idx == 0

    def test_index_to_layer(self):
        """Test grid index to layer enum mapping."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        layer_value = grid.index_to_layer(0)
        assert layer_value == Layer.F_CU.value

    def test_layer_to_index_invalid(self):
        """Test invalid layer value raises."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        with pytest.raises(RoutingError):
            grid.layer_to_index(999)

    def test_index_to_layer_invalid(self):
        """Test invalid grid index raises."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        with pytest.raises(RoutingError):
            grid.index_to_layer(999)

    def test_get_routable_indices(self):
        """Test getting routable layer indices."""
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(10.0, 10.0, rules, layer_stack=stack)

        indices = grid.get_routable_indices()
        assert 0 in indices  # F.Cu
        assert 3 in indices  # B.Cu

    def test_is_plane_layer(self):
        """Test plane layer check."""
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(10.0, 10.0, rules, layer_stack=stack)

        assert grid.is_plane_layer(1) is True  # GND
        assert grid.is_plane_layer(0) is False  # F.Cu signal

    def test_congestion_tracking(self):
        """Test congestion tracking."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        # Initial congestion should be 0
        congestion = grid.get_congestion(5, 5, 0)
        assert congestion == 0.0

    def test_congestion_map(self):
        """Test congestion statistics."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        stats = grid.get_congestion_map()
        assert "max_congestion" in stats
        assert "avg_congestion" in stats
        assert "congested_regions" in stats
        assert stats["max_congestion"] == 0.0

    def test_add_obstacle(self):
        """Test adding obstacle to grid."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        obs = Obstacle(5.0, 5.0, 1.0, 1.0, Layer.F_CU)
        grid.add_obstacle(obs)

        # The obstacle should block some cells
        # Check center of obstacle region
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        assert cell.blocked is True

    def test_add_pad(self):
        """Test adding pad to grid."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        pad = Pad(x=5.0, y=5.0, width=0.5, height=0.5, net=1, net_name="VCC", layer=Layer.F_CU)
        grid.add_pad(pad)

        # Pad should be added (verify grid was modified)
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        # Cell should be assigned to net
        assert cell.net == 1

    def test_add_pad_sets_pad_ownership(self):
        """Test that add_pad sets pad_blocked and original_net fields."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        pad = Pad(x=5.0, y=5.0, width=0.5, height=0.5, net=3, net_name="VCC", layer=Layer.F_CU)
        grid.add_pad(pad)

        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]

        # Pad cells should be marked as pad-blocked with original_net set
        assert cell.pad_blocked is True
        assert cell.original_net == 3
        assert cell.net == 3

    def test_unmark_route_preserves_pad_cells(self):
        """Test that unmarking a route doesn't corrupt pad cells.

        This is the key bug fix from issue #294: when a route passes over a pad
        cell and then gets ripped up, the pad cell should remain blocked with
        its original net, not be cleared to net=0.
        """
        rules = DesignRules(trace_clearance=0.1, trace_width=0.2)
        grid = RoutingGrid(10.0, 10.0, rules)

        # Add a pad at (5, 5) with net=3
        pad = Pad(x=5.0, y=5.0, width=0.5, height=0.5, net=3, net_name="VCC", layer=Layer.F_CU)
        grid.add_pad(pad)

        # Verify pad cell state before marking route
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        assert cell.pad_blocked is True
        assert cell.original_net == 3
        assert cell.net == 3
        assert cell.blocked is True

        # Create a route that passes through the pad area (same net)
        route = Route(net=3, net_name="VCC")
        route.segments.append(
            Segment(x1=4.0, y1=5.0, x2=6.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=3)
        )

        # Mark the route
        grid.mark_route(route)

        # Cell should still be blocked with net=3
        assert cell.blocked is True
        assert cell.net == 3

        # Now unmark (rip-up) the route
        grid.unmark_route(route)

        # BUG FIX: Pad cell should STILL be blocked with its original net
        # Before the fix, this would have been cleared to blocked=False, net=0
        assert cell.blocked is True, "Pad cell should remain blocked after route rip-up"
        assert cell.net == 3, "Pad cell should retain its original net after route rip-up"
        assert cell.pad_blocked is True, "pad_blocked flag should be preserved"

    def test_grid_bounds_clamping(self):
        """Test coordinate clamping at grid boundaries."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules)

        # Coordinates beyond grid should be clamped
        gx, gy = grid.world_to_grid(-10.0, -10.0)
        assert gx == 0
        assert gy == 0

        gx, gy = grid.world_to_grid(100.0, 100.0)
        assert gx == grid.cols - 1
        assert gy == grid.rows - 1
