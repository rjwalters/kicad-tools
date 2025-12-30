"""Tests for router/grid.py module."""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Obstacle, Pad, Segment, Via, Route


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
        grid = RoutingGrid(
            width=10.0, height=10.0, rules=default_rules,
            origin_x=5.0, origin_y=5.0
        )
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
        with pytest.raises(ValueError):
            grid.layer_to_index(999)

    def test_index_to_layer(self, grid):
        """Test grid index to layer enum mapping."""
        layer_val = grid.index_to_layer(0)
        assert layer_val == Layer.F_CU.value

    def test_index_to_layer_invalid(self, grid):
        """Test invalid index raises error."""
        with pytest.raises(ValueError):
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
        obs = Obstacle(
            x=5.0, y=5.0, width=1.0, height=1.0,
            layer=Layer.F_CU, clearance=0.1
        )
        grid.add_obstacle(obs)

        # Check that cells within obstacle are blocked
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        assert cell.blocked is True

    def test_add_pad_smd(self, grid):
        """Test adding an SMD pad as obstacle."""
        pad = Pad(
            x=3.0, y=3.0, width=0.5, height=0.5,
            layer=Layer.F_CU, net=1, net_name="NET1"
        )
        grid.add_pad(pad)

        # Pad center should be marked with net
        gx, gy = grid.world_to_grid(3.0, 3.0)
        cell = grid.grid[0][gy][gx]
        assert cell.net == 1

    def test_add_pad_through_hole(self, grid):
        """Test adding a through-hole pad."""
        pad = Pad(
            x=3.0, y=3.0, width=1.7, height=1.7,
            layer=Layer.F_CU, net=1, net_name="NET1",
            through_hole=True, drill=1.0
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
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=2
        )
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
        for gx, cell_gy, cell_layer in cells:
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
        for cell_gx, gy, cell_layer in cells:
            assert cell_gx == gx
            assert cell_layer == layer_idx

    def test_get_segment_cells_diagonal(self, grid):
        """Test getting cells for diagonal segment."""
        seg = Segment(x1=1.0, y1=1.0, x2=2.0, y2=2.0, width=0.2, layer=Layer.F_CU, net=1)
        cells = grid._get_segment_cells(seg)

        assert len(cells) > 0

    def test_get_via_cells(self, grid):
        """Test getting cells for via."""
        via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1
        )
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
        assert len(stack.signal_layers) == 2  # F.Cu and B.Cu
        assert len(stack.plane_layers) == 2  # In1.Cu and In2.Cu

    def test_four_layer_sig_sig_gnd_pwr(self):
        """Test 4-layer SIG-SIG-GND-PWR stack."""
        stack = LayerStack.four_layer_sig_sig_gnd_pwr()
        assert stack.num_layers == 4

    def test_six_layer(self):
        """Test 6-layer stack creation."""
        stack = LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()
        assert stack.num_layers == 6
        assert len(stack.signal_layers) == 4
        assert len(stack.plane_layers) == 2

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
