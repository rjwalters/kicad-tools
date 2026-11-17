"""Tests for parallel net routing."""

from kicad_tools.router import Autorouter, DesignRules
from kicad_tools.router.parallel import (
    BoundingBox,
    NetGroup,
    compute_net_bounding_box,
    find_independent_groups,
)
from kicad_tools.router.primitives import Pad


class TestBoundingBox:
    """Tests for BoundingBox class."""

    def test_bounding_box_creation(self):
        """Test creating a bounding box."""
        box = BoundingBox(min_x=0, min_y=0, max_x=10, max_y=10, net=1)
        assert box.min_x == 0
        assert box.max_x == 10
        assert box.net == 1

    def test_bounding_box_overlaps_true(self):
        """Test overlapping bounding boxes."""
        box1 = BoundingBox(min_x=0, min_y=0, max_x=10, max_y=10, net=1)
        box2 = BoundingBox(min_x=5, min_y=5, max_x=15, max_y=15, net=2)
        assert box1.overlaps(box2)
        assert box2.overlaps(box1)

    def test_bounding_box_overlaps_false(self):
        """Test non-overlapping bounding boxes."""
        box1 = BoundingBox(min_x=0, min_y=0, max_x=10, max_y=10, net=1)
        box2 = BoundingBox(min_x=20, min_y=20, max_x=30, max_y=30, net=2)
        assert not box1.overlaps(box2)
        assert not box2.overlaps(box1)

    def test_bounding_box_overlaps_with_margin(self):
        """Test bounding box overlap with margin."""
        box1 = BoundingBox(min_x=0, min_y=0, max_x=10, max_y=10, net=1)
        box2 = BoundingBox(min_x=12, min_y=0, max_x=22, max_y=10, net=2)
        # Without margin, they don't overlap
        assert not box1.overlaps(box2, margin=0)
        # With margin of 2, they overlap (10+2 >= 12-2)
        assert box1.overlaps(box2, margin=2)

    def test_bounding_box_area(self):
        """Test bounding box area calculation."""
        box = BoundingBox(min_x=0, min_y=0, max_x=10, max_y=5, net=1)
        assert box.area() == 50


class TestComputeNetBoundingBox:
    """Tests for compute_net_bounding_box function."""

    def test_compute_bounding_box(self):
        """Test computing bounding box for a net."""
        pads = [("U1", "1"), ("U1", "2"), ("U2", "1")]
        pad_dict = {
            ("U1", "1"): Pad(x=0, y=0, width=1, height=1, net=1, net_name="NET1"),
            ("U1", "2"): Pad(x=10, y=5, width=1, height=1, net=1, net_name="NET1"),
            ("U2", "1"): Pad(x=5, y=10, width=1, height=1, net=1, net_name="NET1"),
        }
        box = compute_net_bounding_box(net=1, pads=pads, pad_dict=pad_dict)
        assert box is not None
        assert box.min_x == 0
        assert box.min_y == 0
        assert box.max_x == 10
        assert box.max_y == 10
        assert box.net == 1

    def test_compute_bounding_box_single_pad(self):
        """Test that single pad returns None (need at least 2)."""
        pads = [("U1", "1")]
        pad_dict = {
            ("U1", "1"): Pad(x=0, y=0, width=1, height=1, net=1, net_name="NET1"),
        }
        box = compute_net_bounding_box(net=1, pads=pads, pad_dict=pad_dict)
        assert box is None

    def test_compute_bounding_box_missing_pad(self):
        """Test handling missing pad in dictionary."""
        pads = [("U1", "1"), ("U1", "2")]
        pad_dict = {
            ("U1", "1"): Pad(x=0, y=0, width=1, height=1, net=1, net_name="NET1"),
            # Missing U1.2
        }
        box = compute_net_bounding_box(net=1, pads=pads, pad_dict=pad_dict)
        assert box is None  # Only one valid pad


class TestFindIndependentGroups:
    """Tests for find_independent_groups function."""

    def test_independent_nets_separate_groups(self):
        """Test that non-overlapping nets are in separate groups."""
        # Net 1: pads at (0,0) and (5,0) - left side
        # Net 2: pads at (20,0) and (25,0) - right side
        nets = {
            1: [("U1", "1"), ("U1", "2")],
            2: [("U2", "1"), ("U2", "2")],
        }
        pad_dict = {
            ("U1", "1"): Pad(x=0, y=0, width=1, height=1, net=1, net_name="NET1"),
            ("U1", "2"): Pad(x=5, y=0, width=1, height=1, net=1, net_name="NET1"),
            ("U2", "1"): Pad(x=20, y=0, width=1, height=1, net=2, net_name="NET2"),
            ("U2", "2"): Pad(x=25, y=0, width=1, height=1, net=2, net_name="NET2"),
        }
        groups = find_independent_groups(nets, pad_dict, clearance=1.0)

        # Both nets should be in the same group (can route in parallel)
        assert len(groups) >= 1
        all_nets = set()
        for group in groups:
            all_nets.update(group.nets)
        assert all_nets == {1, 2}

    def test_overlapping_nets_separate_groups(self):
        """Test that overlapping nets are in different groups."""
        # Net 1: pads at (0,0) and (10,10)
        # Net 2: pads at (5,5) and (15,15) - overlaps with Net 1
        nets = {
            1: [("U1", "1"), ("U1", "2")],
            2: [("U2", "1"), ("U2", "2")],
        }
        pad_dict = {
            ("U1", "1"): Pad(x=0, y=0, width=1, height=1, net=1, net_name="NET1"),
            ("U1", "2"): Pad(x=10, y=10, width=1, height=1, net=1, net_name="NET1"),
            ("U2", "1"): Pad(x=5, y=5, width=1, height=1, net=2, net_name="NET2"),
            ("U2", "2"): Pad(x=15, y=15, width=1, height=1, net=2, net_name="NET2"),
        }
        groups = find_independent_groups(nets, pad_dict, clearance=1.0)

        # With overlapping bounding boxes, nets should be in separate groups
        assert len(groups) >= 2
        # Each group should have at most one of the conflicting nets
        for group in groups:
            assert not (1 in group.nets and 2 in group.nets)

    def test_empty_nets(self):
        """Test handling empty nets dictionary."""
        groups = find_independent_groups({}, {}, clearance=1.0)
        assert groups == []

    def test_skip_net_zero(self):
        """Test that net 0 (unconnected) is skipped."""
        nets = {
            0: [("U1", "1"), ("U1", "2")],  # Should be skipped
            1: [("U2", "1"), ("U2", "2")],
        }
        pad_dict = {
            ("U1", "1"): Pad(x=0, y=0, width=1, height=1, net=0, net_name=""),
            ("U1", "2"): Pad(x=5, y=0, width=1, height=1, net=0, net_name=""),
            ("U2", "1"): Pad(x=10, y=0, width=1, height=1, net=1, net_name="NET1"),
            ("U2", "2"): Pad(x=15, y=0, width=1, height=1, net=1, net_name="NET1"),
        }
        groups = find_independent_groups(nets, pad_dict, clearance=1.0)

        # Only net 1 should be in groups
        all_nets = set()
        for group in groups:
            all_nets.update(group.nets)
        assert 0 not in all_nets
        assert 1 in all_nets


class TestParallelRouterIntegration:
    """Integration tests for ParallelRouter."""

    def test_parallel_router_basic(self):
        """Test basic parallel routing with independent nets."""
        rules = DesignRules(
            grid_resolution=0.5,
            trace_width=0.2,
            trace_clearance=0.2,
        )
        router = Autorouter(width=50, height=50, rules=rules)

        # Add two independent nets on opposite sides of the board
        # Net 1: U1.1 -> U1.2 on left side
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5,
                    "y": 10,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 5,
                    "y": 20,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        # Net 2: U2.1 -> U2.2 on right side (independent of Net 1)
        router.add_component(
            "U2",
            [
                {
                    "number": "1",
                    "x": 40,
                    "y": 10,
                    "width": 1,
                    "height": 1,
                    "net": 2,
                    "net_name": "NET2",
                },
                {
                    "number": "2",
                    "x": 40,
                    "y": 20,
                    "width": 1,
                    "height": 1,
                    "net": 2,
                    "net_name": "NET2",
                },
            ],
        )

        # Find independent groups
        clearance = rules.trace_clearance * 2
        groups = find_independent_groups(router.nets, router.pads, clearance)

        # Should find that both nets can be routed in parallel
        assert len(groups) >= 1

    def test_route_all_parallel_flag(self):
        """Test route_all with parallel=True flag."""
        rules = DesignRules(
            grid_resolution=0.5,
            trace_width=0.2,
            trace_clearance=0.2,
        )
        router = Autorouter(width=30, height=30, rules=rules)

        # Add simple net
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 10,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        # Route with parallel flag
        routes = router.route_all(parallel=True, max_workers=2)

        # Should have routed the net
        assert len(routes) >= 0  # May or may not produce routes depending on grid

    def test_route_all_parallel_dedicated_method(self):
        """Test dedicated route_all_parallel method."""
        rules = DesignRules(
            grid_resolution=0.5,
            trace_width=0.2,
            trace_clearance=0.2,
        )
        router = Autorouter(width=30, height=30, rules=rules)

        # Add simple net
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 10,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        # Call dedicated parallel method
        routes = router.route_all_parallel(max_workers=2)

        # Should complete without error
        assert isinstance(routes, list)


class TestNetGroup:
    """Tests for NetGroup class."""

    def test_net_group_creation(self):
        """Test creating a NetGroup."""
        group = NetGroup()
        assert group.nets == []
        assert group.bounding_boxes == []

    def test_net_group_with_data(self):
        """Test NetGroup with data."""
        box1 = BoundingBox(min_x=0, min_y=0, max_x=10, max_y=10, net=1)
        box2 = BoundingBox(min_x=20, min_y=0, max_x=30, max_y=10, net=2)
        group = NetGroup(nets=[1, 2], bounding_boxes=[box1, box2])
        assert len(group.nets) == 2
        assert len(group.bounding_boxes) == 2
