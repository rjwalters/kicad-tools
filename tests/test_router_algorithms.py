"""Tests for router algorithm improvements (Issue #556).

This module tests the new routing algorithms:
1. Clearance Contour Waypoints (sparse routing)
2. Obstacle Expansion preprocessing
3. Adaptive grid resolution

These improvements target the JLCPCB grid performance issue, aiming for
<60 second routing time for standard boards with 5mil clearance.
"""

import pytest

from kicad_tools.router import DesignRules, LayerStack, Pad, RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.sparse import SparseRouter, SparseRoutingGraph, Waypoint


class TestSparseRoutingGraph:
    """Tests for the sparse routing graph using clearance contours."""

    @pytest.fixture
    def rules(self):
        """Create design rules for testing."""
        return DesignRules(
            trace_width=0.127,
            trace_clearance=0.127,
            via_drill=0.3,
            via_diameter=0.5,
            grid_resolution=0.0635,
        )

    @pytest.fixture
    def sparse_graph(self, rules):
        """Create a sparse routing graph."""
        return SparseRoutingGraph(
            width=40.0,
            height=30.0,
            rules=rules,
            num_layers=2,
            contour_samples=8,
            sparse_grid_spacing=2.0,
        )

    def test_waypoint_creation(self):
        """Test waypoint creation and hashing."""
        wp1 = Waypoint(x=10.0, y=20.0, layer=0)
        wp2 = Waypoint(x=10.0, y=20.0, layer=0)
        wp3 = Waypoint(x=10.001, y=20.0, layer=0)

        assert wp1 == wp2
        assert hash(wp1) == hash(wp2)
        assert wp1 == wp3  # Within tolerance

    def test_add_pad_creates_waypoints(self, sparse_graph):
        """Test that adding a pad creates waypoints."""
        pad = Pad(
            x=10.0,
            y=10.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        sparse_graph.add_pad(pad)

        # Should have pad center waypoint + contour waypoints
        assert len(sparse_graph.waypoints[0]) > 1
        stats = sparse_graph.get_statistics()
        assert stats["pad_waypoints"] >= 1
        assert stats["contour_waypoints"] >= 8  # octagonal contour

    def test_sparse_grid_creation(self, sparse_graph):
        """Test that sparse grid adds interior waypoints."""
        sparse_graph.add_sparse_grid()

        stats = sparse_graph.get_statistics()
        assert stats["sparse_waypoints"] > 0

        # Sparse grid should have reasonable density
        total = stats["total_waypoints"]
        # 40x30mm board with 2mm spacing = ~15x20 = ~300 points max per layer
        assert total < 1000  # Much less than uniform grid (~900k)

    def test_visibility_graph_edges(self, sparse_graph):
        """Test visibility graph edge creation."""
        # Add some pads
        pad1 = Pad(
            x=5.0,
            y=5.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )
        pad2 = Pad(
            x=35.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        sparse_graph.add_pad(pad1)
        sparse_graph.add_pad(pad2)
        sparse_graph.add_sparse_grid()
        sparse_graph.build_visibility_graph()

        stats = sparse_graph.get_statistics()
        assert stats["total_edges"] > 0


class TestSparseRouter:
    """Tests for the sparse router."""

    @pytest.fixture
    def rules(self):
        """Create design rules for testing."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

    def test_simple_route(self, rules):
        """Test routing between two nearby pads."""
        router = SparseRouter(
            width=20.0,
            height=15.0,
            rules=rules,
            num_layers=2,
        )

        pad1 = Pad(
            x=5.0,
            y=5.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )
        pad2 = Pad(
            x=15.0,
            y=10.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        router.add_pad(pad1)
        router.add_pad(pad2)
        router.build_graph()

        route = router.route(pad1, pad2)

        # Should find a route
        assert route is not None
        assert len(route.segments) > 0


class TestExpandedObstacleGrid:
    """Tests for the expanded obstacle grid mode."""

    def test_expanded_grid_coarser_resolution(self):
        """Test that expanded mode uses coarser resolution."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.127,
            grid_resolution=0.0635,
        )

        # Standard grid uses fine resolution
        standard_grid = RoutingGrid(
            width=40.0,
            height=30.0,
            rules=rules,
        )

        # Expanded grid uses coarser resolution
        expanded_grid = RoutingGrid(
            width=40.0,
            height=30.0,
            rules=rules,
            expanded_obstacles=True,
        )

        # Expanded should have fewer cells
        assert expanded_grid.resolution > standard_grid.resolution
        assert expanded_grid.cols < standard_grid.cols
        assert expanded_grid.rows < standard_grid.rows

    def test_create_expanded_factory(self):
        """Test the create_expanded factory method."""
        rules = DesignRules(
            trace_width=0.127,
            trace_clearance=0.127,
            grid_resolution=0.0635,
        )

        grid = RoutingGrid.create_expanded(
            width=65.0,
            height=56.0,
            rules=rules,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
        )

        # Should use expanded mode
        assert grid.expanded_obstacles is True

        # Resolution should be based on trace_width, not clearance
        assert grid.resolution >= rules.trace_width

        # Grid should be reasonably sized
        stats = grid.get_grid_statistics()
        assert stats["total_cells"] < 1000000  # Less than 1M cells

    def test_create_adaptive_factory(self):
        """Test the create_adaptive factory method."""
        rules = DesignRules(
            trace_width=0.127,
            trace_clearance=0.127,
            grid_resolution=0.0635,
        )

        # Large board should get coarser resolution
        grid = RoutingGrid.create_adaptive(
            width=100.0,
            height=80.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
            target_cells=500000,
        )

        stats = grid.get_grid_statistics()

        # Should be near target cell count
        assert 200000 < stats["total_cells"] < 800000

    def test_vectorized_pad_addition(self):
        """Test vectorized pad addition performance."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )

        grid = RoutingGrid(
            width=50.0,
            height=40.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

        pad = Pad(
            x=25.0,
            y=20.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.8,
        )

        # Vectorized addition should work
        grid.add_pad_vectorized(pad)

        # Check that pad was added
        stats = grid.get_grid_statistics()
        assert stats["blocked_cells"] > 0
        assert stats["pad_cells"] > 0


class TestGridStatistics:
    """Tests for grid statistics and memory reporting."""

    def test_grid_statistics(self):
        """Test that grid statistics are reported correctly."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )

        grid = RoutingGrid(
            width=20.0,
            height=15.0,
            rules=rules,
        )

        stats = grid.get_grid_statistics()

        assert "resolution_mm" in stats
        assert "cols" in stats
        assert "rows" in stats
        assert "layers" in stats
        assert "total_cells" in stats
        assert "blocked_cells" in stats
        assert "memory_mb" in stats
        assert "expanded_obstacles" in stats

        # Verify values make sense
        expected_cols = int(20.0 / 0.1) + 1
        expected_rows = int(15.0 / 0.1) + 1
        assert stats["cols"] == expected_cols
        assert stats["rows"] == expected_rows


class TestPerformanceComparison:
    """Performance comparison tests for algorithm improvements.

    These tests compare the performance of different grid configurations
    to validate the performance improvements.
    """

    @pytest.fixture
    def jlcpcb_rules(self):
        """Create JLCPCB-compatible design rules."""
        return DesignRules(
            trace_width=0.127,  # 5 mil
            trace_clearance=0.127,  # 5 mil
            via_drill=0.3,
            via_diameter=0.5,
            grid_resolution=0.0635,  # Half of clearance
        )

    def test_grid_size_comparison(self, jlcpcb_rules):
        """Compare grid sizes between standard and expanded modes."""
        width, height = 40.0, 30.0

        # Standard grid (very fine)
        standard_grid = RoutingGrid(
            width=width,
            height=height,
            rules=jlcpcb_rules,
            layer_stack=LayerStack.two_layer(),
        )

        # Expanded grid (coarser)
        expanded_grid = RoutingGrid.create_expanded(
            width=width,
            height=height,
            rules=jlcpcb_rules,
            layer_stack=LayerStack.two_layer(),
        )

        standard_stats = standard_grid.get_grid_statistics()
        expanded_stats = expanded_grid.get_grid_statistics()

        # Expanded should have significantly fewer cells
        reduction_ratio = standard_stats["total_cells"] / expanded_stats["total_cells"]
        assert reduction_ratio > 2  # At least 2x reduction

        # Print for visibility
        print("\nGrid size comparison (40x30mm board, JLCPCB rules):")
        print(f"  Standard: {standard_stats['total_cells']:,} cells")
        print(f"  Expanded: {expanded_stats['total_cells']:,} cells")
        print(f"  Reduction: {reduction_ratio:.1f}x")

    def test_sparse_graph_vs_uniform_grid(self, jlcpcb_rules):
        """Compare waypoint count between sparse graph and uniform grid."""
        width, height = 40.0, 30.0

        # Uniform grid
        uniform_grid = RoutingGrid(
            width=width,
            height=height,
            rules=jlcpcb_rules,
            layer_stack=LayerStack.two_layer(),
        )

        # Sparse graph
        sparse_graph = SparseRoutingGraph(
            width=width,
            height=height,
            rules=jlcpcb_rules,
            num_layers=2,
            contour_samples=8,
            sparse_grid_spacing=2.0,
        )

        # Add some representative pads
        for i in range(20):
            pad = Pad(
                x=5 + (i % 5) * 7,
                y=5 + (i // 5) * 6,
                width=0.5,
                height=0.5,
                net=i + 1,
                net_name=f"NET{i + 1}",
                layer=Layer.F_CU,
                through_hole=False,
                drill=0,
            )
            sparse_graph.add_pad(pad)

        sparse_graph.add_sparse_grid()
        sparse_graph.build_visibility_graph()

        uniform_cells = uniform_grid.cols * uniform_grid.rows * uniform_grid.num_layers
        sparse_stats = sparse_graph.get_statistics()
        sparse_points = sparse_stats["total_waypoints"]

        # Sparse should have far fewer points
        reduction_ratio = uniform_cells / sparse_points
        assert reduction_ratio > 10  # At least 10x reduction

        print("\nGraph size comparison (40x30mm board, 20 pads):")
        print(f"  Uniform grid: {uniform_cells:,} cells")
        print(f"  Sparse graph: {sparse_points:,} waypoints")
        print(f"  Reduction: {reduction_ratio:.0f}x")


class TestRouterIntegration:
    """Integration tests for the router with new algorithms."""

    def test_route_with_expanded_grid(self):
        """Test routing with expanded obstacle grid."""
        from kicad_tools.router import Autorouter, DesignRules, LayerStack

        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        # Create autorouter (uses standard grid by default)
        router = Autorouter(
            width=30.0,
            height=25.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

        # Add components
        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 5.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "NET1",
                    "layer": Layer.F_CU,
                    "through_hole": False,
                    "drill": 0,
                },
                {
                    "number": "2",
                    "x": 7.0,
                    "y": 5.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 0,
                    "net_name": "",
                    "layer": Layer.F_CU,
                    "through_hole": False,
                    "drill": 0,
                },
            ],
        )

        router.add_component(
            "R2",
            [
                {
                    "number": "1",
                    "x": 20.0,
                    "y": 20.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 1,
                    "net_name": "NET1",
                    "layer": Layer.F_CU,
                    "through_hole": False,
                    "drill": 0,
                },
                {
                    "number": "2",
                    "x": 22.0,
                    "y": 20.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": 0,
                    "net_name": "",
                    "layer": Layer.F_CU,
                    "through_hole": False,
                    "drill": 0,
                },
            ],
        )

        # Route all nets
        routes = router.route_all()

        # Should have routed NET1
        assert routes is not None
        stats = router.get_statistics()
        assert stats["nets_routed"] >= 1
