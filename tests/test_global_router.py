"""Tests for hierarchical routing foundation (Issue #1095 Phase A).

Tests cover:
1. RegionGraph construction and spatial queries
2. GlobalRouter corridor assignment
3. Integration with existing sparse router corridor infrastructure
4. Hierarchical strategy integration via Autorouter
5. Regression: existing routing strategies remain unaffected
"""

import math

import pytest

from kicad_tools.router import DesignRules, Pad
from kicad_tools.router.core import Autorouter
from kicad_tools.router.global_router import (
    CorridorAssignment,
    GlobalRouter,
    GlobalRoutingResult,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.region_graph import Region, RegionEdge, RegionGraph
from kicad_tools.router.sparse import Corridor, SparseRouter, Waypoint


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rules():
    """Standard design rules for testing."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.7,
        grid_resolution=0.1,
    )


@pytest.fixture
def small_board_graph():
    """A 20x20mm board with 4x4 region grid."""
    return RegionGraph(
        board_width=20.0,
        board_height=20.0,
        origin_x=0.0,
        origin_y=0.0,
        num_cols=4,
        num_rows=4,
    )


@pytest.fixture
def standard_board_graph():
    """A 65x56mm board (representative of chorus-test-revA) with 10x10 grid."""
    return RegionGraph(
        board_width=65.0,
        board_height=56.0,
        origin_x=0.0,
        origin_y=0.0,
        num_cols=10,
        num_rows=10,
    )


@pytest.fixture
def sample_pads():
    """Sample pads for a simple 2-net board."""
    return [
        # Net 1: left-to-right connection
        Pad(x=2.0, y=10.0, width=1.0, height=1.0, net=1, net_name="VCC",
            ref="R1", pin="1", layer=Layer.F_CU),
        Pad(x=18.0, y=10.0, width=1.0, height=1.0, net=1, net_name="VCC",
            ref="R1", pin="2", layer=Layer.F_CU),
        # Net 2: top-to-bottom connection
        Pad(x=10.0, y=2.0, width=1.0, height=1.0, net=2, net_name="GND",
            ref="R2", pin="1", layer=Layer.F_CU),
        Pad(x=10.0, y=18.0, width=1.0, height=1.0, net=2, net_name="GND",
            ref="R2", pin="2", layer=Layer.F_CU),
    ]


@pytest.fixture
def sample_nets():
    """Net-to-pad mapping for the sample board."""
    return {
        1: [("R1", "1"), ("R1", "2")],
        2: [("R2", "1"), ("R2", "2")],
    }


@pytest.fixture
def sample_pad_dict(sample_pads):
    """Pad dictionary mapping (ref, pin) to Pad objects."""
    return {
        ("R1", "1"): sample_pads[0],
        ("R1", "2"): sample_pads[1],
        ("R2", "1"): sample_pads[2],
        ("R2", "2"): sample_pads[3],
    }


# =============================================================================
# RegionGraph Construction Tests
# =============================================================================


class TestRegionGraphConstruction:
    """Test RegionGraph is correctly built from board parameters."""

    def test_region_count_matches_grid(self, small_board_graph):
        """RegionGraph creates correct number of regions."""
        assert small_board_graph.get_region_count() == 16  # 4x4

    def test_standard_board_region_count(self, standard_board_graph):
        """Standard board gets 100 regions from 10x10 grid."""
        assert standard_board_graph.get_region_count() == 100

    def test_region_covers_board_area(self, small_board_graph):
        """Regions collectively cover the entire board area."""
        graph = small_board_graph
        # Check corners are within some region
        corners = [
            (0.0, 0.0),      # top-left
            (19.9, 0.0),     # top-right
            (0.0, 19.9),     # bottom-left
            (19.9, 19.9),    # bottom-right
            (10.0, 10.0),    # center
        ]
        for x, y in corners:
            region = graph.get_region_at(x, y)
            assert region is not None, f"No region at ({x}, {y})"

    def test_region_has_correct_bounds(self, small_board_graph):
        """Regions have correct spatial bounds."""
        graph = small_board_graph
        # First region (top-left) should span (0,0) to (5,5)
        region = graph.get_region_at(2.5, 2.5)
        assert region is not None
        assert region.row == 0
        assert region.col == 0
        assert abs(region.min_x - 0.0) < 0.01
        assert abs(region.min_y - 0.0) < 0.01
        assert abs(region.max_x - 5.0) < 0.01
        assert abs(region.max_y - 5.0) < 0.01

    def test_region_center_computation(self):
        """Region center is correctly computed."""
        region = Region(
            id=0, row=0, col=0,
            min_x=0.0, min_y=0.0, max_x=10.0, max_y=8.0,
        )
        assert abs(region.center_x - 5.0) < 0.01
        assert abs(region.center_y - 4.0) < 0.01

    def test_edges_connect_adjacent_regions(self, small_board_graph):
        """Adjacent regions are connected by edges."""
        graph = small_board_graph
        edge_count = graph.get_edge_count()
        # 4x4 grid: 3 horizontal edges per row * 4 rows + 4 vertical edges per col * 3 cols
        # = 12 + 12 = 24 edges, but each is bidirectional = 48 directed edges
        assert edge_count == 48

    def test_non_adjacent_regions_not_connected(self, small_board_graph):
        """Non-adjacent regions should not have direct edges."""
        graph = small_board_graph
        # Region at (0,0) should not be directly connected to (0,2)
        top_left = graph.get_region_at(2.5, 2.5)
        assert top_left is not None
        for edge in graph.edges[top_left.id]:
            target = graph.regions[edge.target]
            # Should only connect to row 0 col 1, or row 1 col 0
            assert (target.row == 0 and target.col == 1) or \
                   (target.row == 1 and target.col == 0)

    def test_single_region_graph(self):
        """A 1x1 region graph has one region and no edges."""
        graph = RegionGraph(
            board_width=10.0, board_height=10.0,
            num_cols=1, num_rows=1,
        )
        assert graph.get_region_count() == 1
        assert graph.get_edge_count() == 0

    def test_minimum_dimensions(self):
        """RegionGraph enforces minimum 1 column and 1 row."""
        graph = RegionGraph(
            board_width=10.0, board_height=10.0,
            num_cols=0, num_rows=0,
        )
        assert graph.num_cols >= 1
        assert graph.num_rows >= 1
        assert graph.get_region_count() >= 1

    def test_point_outside_board(self, small_board_graph):
        """Points outside the board return None."""
        assert small_board_graph.get_region_at(-1.0, 10.0) is None
        assert small_board_graph.get_region_at(10.0, -1.0) is None
        assert small_board_graph.get_region_at(21.0, 10.0) is None
        assert small_board_graph.get_region_at(10.0, 21.0) is None


# =============================================================================
# RegionGraph Obstacle Registration Tests
# =============================================================================


class TestRegionGraphObstacles:
    """Test obstacle registration and capacity adjustment."""

    def test_obstacle_reduces_capacity(self, small_board_graph, sample_pads):
        """Registering obstacles reduces region capacity."""
        graph = small_board_graph
        # Get initial capacity of region containing first pad
        region = graph.get_region_at(2.0, 10.0)
        assert region is not None
        initial_capacity = region.capacity

        graph.register_obstacles(sample_pads)

        # Capacity should be reduced
        assert region.capacity <= initial_capacity
        assert region.obstacle_count > 0

    def test_empty_region_has_full_capacity(self, small_board_graph, sample_pads):
        """Regions without obstacles retain full capacity."""
        graph = small_board_graph
        graph.register_obstacles(sample_pads)

        # Region in far corner (no pads nearby)
        empty_region = graph.get_region_at(17.5, 2.5)
        assert empty_region is not None
        # Should have full capacity (no obstacles)
        assert empty_region.obstacle_count == 0
        assert empty_region.capacity == graph.base_capacity


# =============================================================================
# RegionGraph Path Finding Tests
# =============================================================================


class TestRegionGraphPathFinding:
    """Test A* path finding on the region graph."""

    def test_path_between_adjacent_regions(self, small_board_graph):
        """Path between adjacent regions is a direct 2-region path."""
        graph = small_board_graph
        r1 = graph.get_region_at(2.5, 2.5)
        r2 = graph.get_region_at(7.5, 2.5)
        assert r1 is not None and r2 is not None

        path = graph.find_path(r1.id, r2.id)
        assert path is not None
        assert len(path) == 2
        assert path[0] == r1.id
        assert path[-1] == r2.id

    def test_path_across_board(self, small_board_graph):
        """Path from corner to corner traverses multiple regions."""
        graph = small_board_graph
        top_left = graph.get_region_at(2.5, 2.5)
        bottom_right = graph.get_region_at(17.5, 17.5)
        assert top_left is not None and bottom_right is not None

        path = graph.find_path(top_left.id, bottom_right.id)
        assert path is not None
        # Manhattan distance in grid: 3 cols + 3 rows = 6 steps + 1 = 7 regions minimum
        assert len(path) >= 7
        assert path[0] == top_left.id
        assert path[-1] == bottom_right.id

    def test_same_region_path(self, small_board_graph):
        """Path from a region to itself is a single-element list."""
        graph = small_board_graph
        r = graph.get_region_at(10.0, 10.0)
        assert r is not None

        path = graph.find_path(r.id, r.id)
        assert path == [r.id]

    def test_path_with_invalid_regions(self, small_board_graph):
        """Invalid region IDs return None."""
        assert small_board_graph.find_path(999, 0) is None
        assert small_board_graph.find_path(0, 999) is None

    def test_path_continuity(self, small_board_graph):
        """Each consecutive pair of regions in a path should be adjacent."""
        graph = small_board_graph
        r1 = graph.get_region_at(2.5, 2.5)
        r2 = graph.get_region_at(17.5, 17.5)
        assert r1 is not None and r2 is not None

        path = graph.find_path(r1.id, r2.id)
        assert path is not None

        for i in range(len(path) - 1):
            region_a = graph.regions[path[i]]
            region_b = graph.regions[path[i + 1]]
            # Adjacent means differing by exactly 1 in row or col (not both)
            row_diff = abs(region_a.row - region_b.row)
            col_diff = abs(region_a.col - region_b.col)
            assert (row_diff == 1 and col_diff == 0) or \
                   (row_diff == 0 and col_diff == 1), \
                f"Non-adjacent step: ({region_a.row},{region_a.col}) -> ({region_b.row},{region_b.col})"


# =============================================================================
# RegionGraph Utilization Tests
# =============================================================================


class TestRegionGraphUtilization:
    """Test utilization tracking and congestion effects."""

    def test_utilization_increases_after_path(self, small_board_graph):
        """Assigning a path increases region utilization."""
        graph = small_board_graph
        r1 = graph.get_region_at(2.5, 2.5)
        r2 = graph.get_region_at(7.5, 2.5)
        assert r1 is not None and r2 is not None

        path = graph.find_path(r1.id, r2.id)
        assert path is not None

        # Check initial utilization is 0
        for rid in path:
            assert graph.regions[rid].utilization == 0

        graph.update_utilization(path)

        # Check utilization increased
        for rid in path:
            assert graph.regions[rid].utilization == 1

    def test_congestion_cost_increases_with_utilization(self):
        """Edge congestion cost increases with utilization."""
        edge = RegionEdge(source=0, target=1, capacity=10, utilization=0, distance=5.0)
        low_cost = edge.congestion_cost

        edge.utilization = 5
        mid_cost = edge.congestion_cost

        edge.utilization = 9
        high_cost = edge.congestion_cost

        assert low_cost < mid_cost < high_cost

    def test_waypoint_coords_from_path(self, small_board_graph):
        """Path can be converted to waypoint coordinates."""
        graph = small_board_graph
        r1 = graph.get_region_at(2.5, 2.5)
        r2 = graph.get_region_at(7.5, 2.5)
        assert r1 is not None and r2 is not None

        path = graph.find_path(r1.id, r2.id)
        assert path is not None

        coords = graph.path_to_waypoint_coords(path)
        assert len(coords) == len(path)
        for x, y in coords:
            assert 0.0 <= x <= 20.0
            assert 0.0 <= y <= 20.0


# =============================================================================
# RegionGraph Statistics Tests
# =============================================================================


class TestRegionGraphStatistics:
    """Test statistics reporting."""

    def test_statistics_keys(self, small_board_graph):
        """Statistics dict has expected keys."""
        stats = small_board_graph.get_statistics()
        expected_keys = {
            "num_regions", "num_rows", "num_cols", "num_edges",
            "total_capacity", "total_utilization", "max_utilization",
            "regions_with_obstacles",
        }
        assert set(stats.keys()) == expected_keys

    def test_initial_statistics(self, small_board_graph):
        """Initial statistics show zero utilization."""
        stats = small_board_graph.get_statistics()
        assert stats["num_regions"] == 16
        assert stats["num_rows"] == 4
        assert stats["num_cols"] == 4
        assert stats["total_utilization"] == 0
        assert stats["max_utilization"] == 0
        assert stats["regions_with_obstacles"] == 0


# =============================================================================
# GlobalRouter Tests
# =============================================================================


class TestGlobalRouter:
    """Test the GlobalRouter for corridor assignment."""

    def test_route_single_net(self, small_board_graph, sample_pads):
        """Route a single net and get a corridor assignment."""
        graph = small_board_graph
        graph.register_obstacles(sample_pads)

        router = GlobalRouter(
            region_graph=graph,
            corridor_width=0.5,
        )

        # Route net 1 (left-to-right)
        assignment = router.route_net(
            net=1,
            pad_positions=[(2.0, 10.0), (18.0, 10.0)],
        )

        assert assignment is not None
        assert assignment.net == 1
        assert len(assignment.region_path) >= 2
        assert isinstance(assignment.corridor, Corridor)
        assert assignment.corridor.net == 1
        assert assignment.corridor.width == 0.5

    def test_corridor_waypoints_span_pads(self, small_board_graph):
        """Corridor waypoints should start near source pad and end near target pad."""
        router = GlobalRouter(
            region_graph=small_board_graph,
            corridor_width=1.0,
        )

        assignment = router.route_net(
            net=1,
            pad_positions=[(2.0, 10.0), (18.0, 10.0)],
        )

        assert assignment is not None
        coords = assignment.waypoint_coords
        assert len(coords) >= 2

        # First waypoint should be at source pad
        assert abs(coords[0][0] - 2.0) < 0.01
        assert abs(coords[0][1] - 10.0) < 0.01

        # Last waypoint should be at target pad
        assert abs(coords[-1][0] - 18.0) < 0.01
        assert abs(coords[-1][1] - 10.0) < 0.01

    def test_route_all_nets(self, small_board_graph, sample_nets, sample_pad_dict, sample_pads):
        """Route all nets and get assignments for each."""
        graph = small_board_graph
        graph.register_obstacles(sample_pads)

        router = GlobalRouter(
            region_graph=graph,
            corridor_width=0.5,
        )

        result = router.route_all(
            nets=sample_nets,
            pad_dict=sample_pad_dict,
        )

        assert isinstance(result, GlobalRoutingResult)
        assert len(result.assignments) == 2  # Both nets should succeed
        assert 1 in result.assignments
        assert 2 in result.assignments
        assert len(result.failed_nets) == 0

    def test_corridors_contain_pad_positions(self, small_board_graph, sample_pads):
        """Corridors should contain their source and target pad positions."""
        graph = small_board_graph
        graph.register_obstacles(sample_pads)

        router = GlobalRouter(
            region_graph=graph,
            corridor_width=3.0,  # Wide corridor for containment check
        )

        assignment = router.route_net(
            net=1,
            pad_positions=[(2.0, 10.0), (18.0, 10.0)],
        )

        assert assignment is not None
        corridor = assignment.corridor

        # Both pad positions should be within the corridor
        assert corridor.contains_point(2.0, 10.0, 0)
        assert corridor.contains_point(18.0, 10.0, 0)

    def test_multi_pad_net_uses_most_distant_pair(self, small_board_graph):
        """Multi-pad nets use the most distant pair as corridor endpoints."""
        router = GlobalRouter(
            region_graph=small_board_graph,
            corridor_width=1.0,
        )

        # 3-pad net: endpoints should be the two most distant
        assignment = router.route_net(
            net=3,
            pad_positions=[(2.0, 10.0), (10.0, 10.0), (18.0, 10.0)],
        )

        assert assignment is not None
        coords = assignment.waypoint_coords

        # Corridor should span from leftmost to rightmost pad
        assert abs(coords[0][0] - 2.0) < 0.01 or abs(coords[-1][0] - 2.0) < 0.01
        assert abs(coords[0][0] - 18.0) < 0.01 or abs(coords[-1][0] - 18.0) < 0.01

    def test_insufficient_pads_returns_none(self, small_board_graph):
        """Net with fewer than 2 pads cannot be routed."""
        router = GlobalRouter(
            region_graph=small_board_graph,
            corridor_width=0.5,
        )

        result = router.route_net(net=1, pad_positions=[(5.0, 5.0)])
        assert result is None

        result = router.route_net(net=1, pad_positions=[])
        assert result is None

    def test_pads_outside_board_returns_none(self, small_board_graph):
        """Pads outside board bounds result in failed routing."""
        router = GlobalRouter(
            region_graph=small_board_graph,
            corridor_width=0.5,
        )

        result = router.route_net(
            net=1,
            pad_positions=[(-10.0, 10.0), (30.0, 10.0)],
        )
        assert result is None

    def test_utilization_updates_after_routing(self, small_board_graph, sample_nets, sample_pad_dict):
        """Region utilization increases after global routing."""
        graph = small_board_graph
        router = GlobalRouter(region_graph=graph, corridor_width=0.5)

        # Initial utilization should be zero
        stats = graph.get_statistics()
        assert stats["total_utilization"] == 0

        router.route_all(nets=sample_nets, pad_dict=sample_pad_dict)

        # Utilization should have increased
        stats = graph.get_statistics()
        assert stats["total_utilization"] > 0

    def test_net_order_is_respected(self, small_board_graph, sample_nets, sample_pad_dict):
        """Nets are routed in the specified order."""
        router = GlobalRouter(
            region_graph=small_board_graph,
            corridor_width=0.5,
        )

        # Route net 2 first, then net 1
        result = router.route_all(
            nets=sample_nets,
            pad_dict=sample_pad_dict,
            net_order=[2, 1],
        )

        # Both should succeed regardless of order
        assert len(result.assignments) == 2


# =============================================================================
# Integration with Existing Sparse Router
# =============================================================================


class TestSparseRouterIntegration:
    """Test that global routing integrates with the existing Corridor infrastructure."""

    def test_corridor_from_global_router_is_valid(self, small_board_graph, rules):
        """Corridors from GlobalRouter are compatible with SparseRoutingGraph."""
        router = GlobalRouter(
            region_graph=small_board_graph,
            corridor_width=rules.trace_clearance * 2,
        )

        assignment = router.route_net(
            net=1,
            pad_positions=[(2.0, 10.0), (18.0, 10.0)],
        )

        assert assignment is not None
        corridor = assignment.corridor

        # Corridor should have valid structure
        assert corridor.net == 1
        assert corridor.width == rules.trace_clearance * 2
        assert len(corridor.waypoints) >= 2
        assert len(corridor.layer_segments) >= 1

        # Bounding box should be valid
        bbox = corridor.get_bounding_box()
        assert bbox[0] < bbox[2]  # min_x < max_x
        assert bbox[1] < bbox[3]  # min_y < max_y

    def test_corridor_can_be_reserved_in_sparse_router(self, rules):
        """Corridors from GlobalRouter can be reserved in SparseRouter."""
        sparse_router = SparseRouter(
            width=20.0,
            height=20.0,
            rules=rules,
        )

        # Create a corridor like GlobalRouter would
        waypoints = [
            Waypoint(x=2.0, y=10.0, layer=0, waypoint_type="global"),
            Waypoint(x=10.0, y=10.0, layer=0, waypoint_type="global"),
            Waypoint(x=18.0, y=10.0, layer=0, waypoint_type="global"),
        ]

        corridor = Corridor.from_waypoints(
            waypoints=waypoints,
            net=1,
            width=rules.trace_clearance * 2,
        )

        # Reserve in sparse router
        sparse_router.graph.reserved_corridors[1] = corridor
        assert sparse_router.get_corridor(1) is not None
        assert sparse_router.get_corridor(1).net == 1

    def test_corridor_containment_check(self, rules):
        """Corridor containment works correctly for global routing corridors."""
        waypoints = [
            Waypoint(x=0.0, y=10.0, layer=0, waypoint_type="global"),
            Waypoint(x=20.0, y=10.0, layer=0, waypoint_type="global"),
        ]

        corridor = Corridor.from_waypoints(
            waypoints=waypoints,
            net=1,
            width=2.0,  # 2mm half-width
        )

        # Points within 2mm of the centerline (y=10) should be contained
        assert corridor.contains_point(10.0, 10.0, 0)  # On centerline
        assert corridor.contains_point(10.0, 11.0, 0)  # 1mm from centerline
        assert corridor.contains_point(10.0, 11.9, 0)  # Just inside

        # Points far from centerline should not be contained
        assert not corridor.contains_point(10.0, 13.0, 0)  # 3mm away
        assert not corridor.contains_point(10.0, 0.0, 0)   # Far away

        # Points on wrong layer should not be contained
        assert not corridor.contains_point(10.0, 10.0, 1)


# =============================================================================
# Autorouter Hierarchical Strategy Integration Tests
# =============================================================================


class TestHierarchicalStrategyIntegration:
    """Test hierarchical routing via Autorouter API."""

    @pytest.fixture
    def autorouter(self, rules):
        """Create an Autorouter with a small board."""
        router = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            force_python=True,
        )

        # Add pads for two nets
        router.add_component(
            ref="R1",
            pads=[
                {"number": "1", "x": 3.0, "y": 10.0, "net": 1, "net_name": "VCC"},
                {"number": "2", "x": 17.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        router.add_component(
            ref="R2",
            pads=[
                {"number": "1", "x": 10.0, "y": 3.0, "net": 2, "net_name": "GND"},
                {"number": "2", "x": 10.0, "y": 17.0, "net": 2, "net_name": "GND"},
            ],
        )

        return router

    def test_hierarchical_routing_runs(self, autorouter):
        """Hierarchical routing completes without errors."""
        routes = autorouter.route_all_hierarchical(
            num_cols=4,
            num_rows=4,
            use_negotiated=False,
        )
        # Should return a list (may be empty if routing fails on small board)
        assert isinstance(routes, list)

    def test_hierarchical_via_advanced_entry_point(self, autorouter):
        """Hierarchical routing accessible via route_all_advanced."""
        routes = autorouter.route_all_advanced(
            use_hierarchical=True,
            use_negotiated=False,
        )
        assert isinstance(routes, list)

    def test_standard_routing_unchanged(self, autorouter):
        """Standard routing is not affected by hierarchical additions."""
        routes = autorouter.route_all()
        assert isinstance(routes, list)

    def test_negotiated_routing_unchanged(self, autorouter):
        """Negotiated routing is not affected by hierarchical additions."""
        routes = autorouter.route_all_negotiated()
        assert isinstance(routes, list)

    def test_two_phase_routing_unchanged(self, autorouter):
        """Two-phase routing is not affected by hierarchical additions."""
        routes = autorouter.route_all_two_phase(use_negotiated=False)
        assert isinstance(routes, list)

    def test_advanced_default_unchanged(self, autorouter):
        """Default route_all_advanced behavior is not changed."""
        routes = autorouter.route_all_advanced()
        assert isinstance(routes, list)


# =============================================================================
# Region Data Structure Tests
# =============================================================================


class TestRegionDataStructure:
    """Test Region dataclass behavior."""

    def test_contains_point(self):
        """Region correctly identifies contained points."""
        region = Region(
            id=0, row=0, col=0,
            min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0,
        )
        assert region.contains_point(5.0, 5.0)
        assert region.contains_point(0.0, 0.0)   # Inclusive lower bound
        assert region.contains_point(10.0, 10.0)  # Inclusive upper bound
        assert not region.contains_point(-1.0, 5.0)
        assert not region.contains_point(5.0, -1.0)
        assert not region.contains_point(11.0, 5.0)
        assert not region.contains_point(5.0, 11.0)

    def test_remaining_capacity(self):
        """Remaining capacity is correctly computed."""
        region = Region(
            id=0, row=0, col=0,
            min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0,
            capacity=10, utilization=3,
        )
        assert region.remaining_capacity == 7

    def test_remaining_capacity_never_negative(self):
        """Remaining capacity is clamped to 0."""
        region = Region(
            id=0, row=0, col=0,
            min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0,
            capacity=5, utilization=10,
        )
        assert region.remaining_capacity == 0

    def test_width_and_height(self):
        """Region width and height computed from bounds."""
        region = Region(
            id=0, row=0, col=0,
            min_x=5.0, min_y=10.0, max_x=15.0, max_y=25.0,
        )
        assert abs(region.width - 10.0) < 0.01
        assert abs(region.height - 15.0) < 0.01


# =============================================================================
# RegionEdge Data Structure Tests
# =============================================================================


class TestRegionEdgeDataStructure:
    """Test RegionEdge dataclass behavior."""

    def test_congestion_cost_uncongested(self):
        """Uncongested edge has base cost of 1.0."""
        edge = RegionEdge(source=0, target=1, capacity=10, utilization=0)
        assert abs(edge.congestion_cost - 1.0) < 0.01

    def test_congestion_cost_full_capacity(self):
        """Full capacity edge has high cost."""
        edge = RegionEdge(source=0, target=1, capacity=10, utilization=10)
        assert edge.congestion_cost >= 5.0

    def test_congestion_cost_zero_capacity(self):
        """Zero capacity edge has very high cost."""
        edge = RegionEdge(source=0, target=1, capacity=0, utilization=0)
        assert edge.congestion_cost >= 50.0

    def test_remaining_capacity(self):
        """Remaining edge capacity is correctly computed."""
        edge = RegionEdge(source=0, target=1, capacity=10, utilization=7)
        assert edge.remaining_capacity == 3


# =============================================================================
# CorridorAssignment Tests
# =============================================================================


class TestCorridorAssignment:
    """Test CorridorAssignment data structure."""

    def test_assignment_fields(self):
        """CorridorAssignment stores all expected fields."""
        waypoints = [
            Waypoint(x=0.0, y=0.0, layer=0),
            Waypoint(x=10.0, y=10.0, layer=0),
        ]
        corridor = Corridor.from_waypoints(waypoints, net=1, width=0.5)

        assignment = CorridorAssignment(
            net=1,
            region_path=[0, 1, 2],
            corridor=corridor,
            waypoint_coords=[(0.0, 0.0), (5.0, 5.0), (10.0, 10.0)],
        )

        assert assignment.net == 1
        assert assignment.region_path == [0, 1, 2]
        assert assignment.corridor.net == 1
        assert len(assignment.waypoint_coords) == 3


# =============================================================================
# GlobalRoutingResult Tests
# =============================================================================


class TestGlobalRoutingResult:
    """Test GlobalRoutingResult data structure."""

    def test_empty_result(self):
        """Empty result has no assignments or failures."""
        result = GlobalRoutingResult()
        assert len(result.assignments) == 0
        assert len(result.failed_nets) == 0
        assert result.region_graph is None
