"""Tests for region-based parallel routing (Issue #965)."""

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.parallel import (
    GridRegion,
    RegionBasedNegotiatedRouter,
    RegionPartition,
    classify_nets_by_region,
    partition_grid_into_regions,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


class TestGridRegion:
    """Tests for GridRegion dataclass."""

    def test_contains_point_inside(self):
        """Test that contains_point returns True for point inside region."""
        region = GridRegion(id=0, row=0, col=0, min_gx=0, max_gx=10, min_gy=0, max_gy=10)
        assert region.contains_point(5, 5)
        assert region.contains_point(0, 0)
        assert region.contains_point(9, 9)

    def test_contains_point_outside(self):
        """Test that contains_point returns False for point outside region."""
        region = GridRegion(id=0, row=0, col=0, min_gx=0, max_gx=10, min_gy=0, max_gy=10)
        assert not region.contains_point(10, 5)  # max_gx is exclusive
        assert not region.contains_point(5, 10)  # max_gy is exclusive
        assert not region.contains_point(-1, 5)
        assert not region.contains_point(5, -1)

    def test_is_adjacent_horizontal(self):
        """Test adjacency detection for horizontal neighbors."""
        region1 = GridRegion(id=0, row=0, col=0, min_gx=0, max_gx=10, min_gy=0, max_gy=10)
        region2 = GridRegion(id=1, row=0, col=1, min_gx=10, max_gx=20, min_gy=0, max_gy=10)
        assert region1.is_adjacent(region2)
        assert region2.is_adjacent(region1)

    def test_is_adjacent_vertical(self):
        """Test adjacency detection for vertical neighbors."""
        region1 = GridRegion(id=0, row=0, col=0, min_gx=0, max_gx=10, min_gy=0, max_gy=10)
        region2 = GridRegion(id=2, row=1, col=0, min_gx=0, max_gx=10, min_gy=10, max_gy=20)
        assert region1.is_adjacent(region2)
        assert region2.is_adjacent(region1)

    def test_is_adjacent_diagonal(self):
        """Test that diagonal neighbors are not adjacent."""
        region1 = GridRegion(id=0, row=0, col=0, min_gx=0, max_gx=10, min_gy=0, max_gy=10)
        region_diagonal = GridRegion(
            id=3, row=1, col=1, min_gx=10, max_gx=20, min_gy=10, max_gy=20
        )
        assert not region1.is_adjacent(region_diagonal)

    def test_is_adjacent_same_region(self):
        """Test that a region is not adjacent to itself."""
        region = GridRegion(id=0, row=0, col=0, min_gx=0, max_gx=10, min_gy=0, max_gy=10)
        assert not region.is_adjacent(region)


class TestRegionPartition:
    """Tests for RegionPartition."""

    def test_get_region(self):
        """Test getting a region by row/col."""
        partition = partition_grid_into_regions(100, 100, num_cols=2, num_rows=2)
        region = partition.get_region(0, 0)
        assert region is not None
        assert region.row == 0
        assert region.col == 0

        region = partition.get_region(1, 1)
        assert region is not None
        assert region.row == 1
        assert region.col == 1

    def test_get_region_invalid(self):
        """Test getting a non-existent region returns None."""
        partition = partition_grid_into_regions(100, 100, num_cols=2, num_rows=2)
        region = partition.get_region(5, 5)
        assert region is None

    def test_get_checkerboard_groups(self):
        """Test that checkerboard grouping produces non-adjacent regions."""
        partition = partition_grid_into_regions(100, 100, num_cols=2, num_rows=2)
        group_a, group_b = partition.get_checkerboard_groups()

        # With 2x2, each group should have 2 regions
        assert len(group_a) == 2
        assert len(group_b) == 2

        # Regions within each group should not be adjacent
        for i, r1 in enumerate(group_a):
            for r2 in group_a[i + 1 :]:
                assert not r1.is_adjacent(r2)

        for i, r1 in enumerate(group_b):
            for r2 in group_b[i + 1 :]:
                assert not r1.is_adjacent(r2)

    def test_get_checkerboard_groups_4x4(self):
        """Test checkerboard grouping with 4x4 partition."""
        partition = partition_grid_into_regions(100, 100, num_cols=4, num_rows=4)
        group_a, group_b = partition.get_checkerboard_groups()

        # With 4x4 (16 regions), each group should have 8 regions
        assert len(group_a) == 8
        assert len(group_b) == 8

        # Verify non-adjacency within each group
        for i, r1 in enumerate(group_a):
            for r2 in group_a[i + 1 :]:
                assert not r1.is_adjacent(r2)


class TestPartitionGridIntoRegions:
    """Tests for partition_grid_into_regions function."""

    def test_2x2_partition(self):
        """Test basic 2x2 partitioning."""
        partition = partition_grid_into_regions(100, 100, num_cols=2, num_rows=2)
        assert len(partition.regions) == 4
        assert partition.num_rows == 2
        assert partition.num_cols == 2

    def test_region_boundaries(self):
        """Test that region boundaries are correctly calculated."""
        partition = partition_grid_into_regions(100, 100, num_cols=2, num_rows=2)

        # Check first region (top-left)
        r00 = partition.get_region(0, 0)
        assert r00.min_gx == 0
        assert r00.max_gx == 50
        assert r00.min_gy == 0
        assert r00.max_gy == 50

        # Check last region (bottom-right)
        r11 = partition.get_region(1, 1)
        assert r11.min_gx == 50
        assert r11.max_gx == 100
        assert r11.min_gy == 50
        assert r11.max_gy == 100

    def test_uneven_partition(self):
        """Test partitioning with non-divisible dimensions."""
        partition = partition_grid_into_regions(101, 101, num_cols=2, num_rows=2)

        # Last row/col should take remainder
        r11 = partition.get_region(1, 1)
        assert r11.max_gx == 101
        assert r11.max_gy == 101

    def test_single_region(self):
        """Test 1x1 partition (single region)."""
        partition = partition_grid_into_regions(100, 100, num_cols=1, num_rows=1)
        assert len(partition.regions) == 1
        r00 = partition.get_region(0, 0)
        assert r00.min_gx == 0
        assert r00.max_gx == 100
        assert r00.min_gy == 0
        assert r00.max_gy == 100


class TestClassifyNetsByRegion:
    """Tests for classify_nets_by_region function."""

    @pytest.fixture
    def grid(self):
        """Create a simple routing grid."""
        rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=1.0)
        return RoutingGrid(width=100.0, height=100.0, rules=rules)

    @pytest.fixture
    def partition(self, grid):
        """Create a 2x2 partition."""
        return partition_grid_into_regions(grid.cols, grid.rows, num_cols=2, num_rows=2)

    def test_net_in_single_region(self, grid, partition):
        """Test that a net contained in one region is classified correctly."""
        # Create pads all in the same quadrant (top-left)
        pad1 = Pad(ref="R1", pin="1", x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="N1")
        pad2 = Pad(ref="R1", pin="2", x=20.0, y=20.0, width=1.0, height=1.0, net=1, net_name="N1")

        pad_dict = {("R1", "1"): pad1, ("R1", "2"): pad2}
        nets = {1: [("R1", "1"), ("R1", "2")]}

        region_nets, boundary_nets = classify_nets_by_region(nets, pad_dict, partition, grid)

        # Net should be in one region, not in boundary
        assert 1 not in boundary_nets
        total_assigned = sum(1 for nets in region_nets.values() for n in nets if n == 1)
        assert total_assigned == 1

    def test_boundary_net_detection(self, grid, partition):
        """Test that nets spanning multiple regions are detected as boundary nets."""
        # Create pads across multiple quadrants
        pad1 = Pad(ref="R1", pin="1", x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="N1")
        pad2 = Pad(ref="R1", pin="2", x=10.0, y=60.0, width=1.0, height=1.0, net=1, net_name="N1")
        pad3 = Pad(ref="R1", pin="3", x=60.0, y=10.0, width=1.0, height=1.0, net=1, net_name="N1")
        pad4 = Pad(ref="R1", pin="4", x=60.0, y=60.0, width=1.0, height=1.0, net=1, net_name="N1")

        pad_dict = {
            ("R1", "1"): pad1,
            ("R1", "2"): pad2,
            ("R1", "3"): pad3,
            ("R1", "4"): pad4,
        }
        nets = {1: [("R1", "1"), ("R1", "2"), ("R1", "3"), ("R1", "4")]}

        region_nets, boundary_nets = classify_nets_by_region(nets, pad_dict, partition, grid)

        # Net spans all 4 quadrants equally, should be boundary net
        assert 1 in boundary_nets

    def test_majority_region_assignment(self, grid, partition):
        """Test that nets are assigned to the region with most pads."""
        # 3 pads in top-left, 1 pad in bottom-right
        pad1 = Pad(ref="R1", pin="1", x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="N1")
        pad2 = Pad(ref="R1", pin="2", x=15.0, y=10.0, width=1.0, height=1.0, net=1, net_name="N1")
        pad3 = Pad(ref="R1", pin="3", x=20.0, y=15.0, width=1.0, height=1.0, net=1, net_name="N1")
        pad4 = Pad(ref="R1", pin="4", x=60.0, y=60.0, width=1.0, height=1.0, net=1, net_name="N1")

        pad_dict = {
            ("R1", "1"): pad1,
            ("R1", "2"): pad2,
            ("R1", "3"): pad3,
            ("R1", "4"): pad4,
        }
        nets = {1: [("R1", "1"), ("R1", "2"), ("R1", "3"), ("R1", "4")]}

        region_nets, boundary_nets = classify_nets_by_region(nets, pad_dict, partition, grid)

        # 75% in one region, should NOT be boundary net
        assert 1 not in boundary_nets


class TestRegionBasedNegotiatedRouter:
    """Tests for RegionBasedNegotiatedRouter class."""

    @pytest.fixture
    def router(self):
        """Create a simple autorouter with some components."""
        router = Autorouter(width=100.0, height=100.0)

        # Add components in different quadrants
        # Top-left quadrant
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 1},
            ],
        )

        # Top-right quadrant
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 60.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 2},
                {"number": "2", "x": 70.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 2},
            ],
        )

        # Bottom-left quadrant
        router.add_component(
            "R3",
            [
                {"number": "1", "x": 10.0, "y": 60.0, "width": 1.0, "height": 1.0, "net": 3},
                {"number": "2", "x": 20.0, "y": 60.0, "width": 1.0, "height": 1.0, "net": 3},
            ],
        )

        # Bottom-right quadrant
        router.add_component(
            "R4",
            [
                {"number": "1", "x": 60.0, "y": 60.0, "width": 1.0, "height": 1.0, "net": 4},
                {"number": "2", "x": 70.0, "y": 60.0, "width": 1.0, "height": 1.0, "net": 4},
            ],
        )

        return router

    def test_get_partition(self, router):
        """Test that partition is created correctly."""
        region_router = RegionBasedNegotiatedRouter(router, partition_rows=2, partition_cols=2)
        partition = region_router.get_partition()

        assert len(partition.regions) == 4
        assert partition.num_rows == 2
        assert partition.num_cols == 2

    def test_partition_caching(self, router):
        """Test that partition is cached and reused."""
        region_router = RegionBasedNegotiatedRouter(router, partition_rows=2, partition_cols=2)
        partition1 = region_router.get_partition()
        partition2 = region_router.get_partition()

        assert partition1 is partition2


class TestRouteAllNegotiatedWithRegionParallel:
    """Integration tests for route_all_negotiated with region_parallel enabled."""

    @pytest.fixture
    def router_with_nets(self):
        """Create a router with nets in different regions for testing."""
        router = Autorouter(width=100.0, height=100.0)

        # Add simple 2-pin nets in each quadrant
        # Net 1: Top-left
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 25.0, "y": 25.0, "width": 1.0, "height": 1.0, "net": 1},
            ],
        )

        # Net 2: Top-right
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 60.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 2},
                {"number": "2", "x": 75.0, "y": 25.0, "width": 1.0, "height": 1.0, "net": 2},
            ],
        )

        # Net 3: Bottom-left
        router.add_component(
            "R3",
            [
                {"number": "1", "x": 10.0, "y": 60.0, "width": 1.0, "height": 1.0, "net": 3},
                {"number": "2", "x": 25.0, "y": 75.0, "width": 1.0, "height": 1.0, "net": 3},
            ],
        )

        # Net 4: Bottom-right
        router.add_component(
            "R4",
            [
                {"number": "1", "x": 60.0, "y": 60.0, "width": 1.0, "height": 1.0, "net": 4},
                {"number": "2", "x": 75.0, "y": 75.0, "width": 1.0, "height": 1.0, "net": 4},
            ],
        )

        return router

    def test_route_all_negotiated_with_region_parallel(self, router_with_nets):
        """Test that routing completes with region_parallel enabled."""
        routes = router_with_nets.route_all_negotiated(
            max_iterations=3,
            region_parallel=True,
            partition_rows=2,
            partition_cols=2,
            max_parallel_workers=4,
        )

        # Should route all 4 nets
        routed_nets = {r.net for r in routes}
        assert 1 in routed_nets
        assert 2 in routed_nets
        assert 3 in routed_nets
        assert 4 in routed_nets

    def test_region_parallel_produces_same_result_as_sequential(self, router_with_nets):
        """Test that region parallel produces valid routes like sequential mode."""
        # Create two separate routers with the same initial state
        router1 = Autorouter(width=100.0, height=100.0)
        router2 = Autorouter(width=100.0, height=100.0)

        for router in [router1, router2]:
            router.add_component(
                "R1",
                [
                    {"number": "1", "x": 10.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 1},
                    {"number": "2", "x": 25.0, "y": 25.0, "width": 1.0, "height": 1.0, "net": 1},
                ],
            )
            router.add_component(
                "R2",
                [
                    {"number": "1", "x": 60.0, "y": 10.0, "width": 1.0, "height": 1.0, "net": 2},
                    {"number": "2", "x": 75.0, "y": 25.0, "width": 1.0, "height": 1.0, "net": 2},
                ],
            )

        # Route with sequential mode
        routes_seq = router1.route_all_negotiated(max_iterations=3, region_parallel=False)

        # Route with region parallel mode
        routes_par = router2.route_all_negotiated(
            max_iterations=3, region_parallel=True, partition_rows=2, partition_cols=2
        )

        # Both should route all nets
        nets_seq = {r.net for r in routes_seq}
        nets_par = {r.net for r in routes_par}

        assert nets_seq == nets_par

    def test_thread_safety_enabled_with_region_parallel(self, router_with_nets):
        """Test that thread safety is enabled when using region_parallel."""
        # Initially thread safety should be off
        assert not router_with_nets.grid.thread_safe

        # After routing with region_parallel, grid should have thread safety
        router_with_nets.route_all_negotiated(
            max_iterations=1, region_parallel=True, partition_rows=2, partition_cols=2
        )

        assert router_with_nets.grid.thread_safe
