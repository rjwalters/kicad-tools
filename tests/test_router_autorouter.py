"""Tests for router autorouter, pathfinder, and adaptive autorouter."""

import math

import pytest

from kicad_tools.router.core import AdaptiveAutorouter, Autorouter, RoutingResult
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.heuristics import (
    CongestionAwareHeuristic,
    HeuristicContext,
    ManhattanHeuristic,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import AStarNode, Router
from kicad_tools.router.primitives import Obstacle, Pad, Route, Segment, Via
from kicad_tools.router.rules import (
    DesignRules,
    create_net_class_map,
)


class TestAutorouter:
    """Tests for Autorouter class."""

    def test_autorouter_creation(self):
        """Test creating an autorouter."""
        router = Autorouter(width=50.0, height=50.0)

        assert router.grid is not None
        assert router.router is not None
        assert router.pads == {}
        assert router.nets == {}
        assert router.routes == []

    def test_autorouter_with_origin(self):
        """Test autorouter with custom origin."""
        router = Autorouter(width=50.0, height=50.0, origin_x=100, origin_y=100)

        assert router.grid.origin_x == 100
        assert router.grid.origin_y == 100

    def test_autorouter_with_rules(self):
        """Test autorouter with custom rules."""
        rules = DesignRules(trace_width=0.3, trace_clearance=0.2)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        assert router.rules.trace_width == 0.3
        assert router.rules.trace_clearance == 0.2

    def test_autorouter_with_layer_stack(self):
        """Test autorouter with custom layer stack."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router = Autorouter(width=50.0, height=50.0, layer_stack=stack)

        assert router.grid.num_layers == 4

    def test_add_component(self):
        """Test adding component pads."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {
                "number": "1",
                "x": 10.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "VCC",
            },
            {
                "number": "2",
                "x": 12.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "GND",
            },
        ]
        router.add_component("U1", pads)

        assert ("U1", "1") in router.pads
        assert ("U1", "2") in router.pads
        assert 1 in router.nets
        assert 2 in router.nets

    def test_add_component_tracks_net_names(self):
        """Test that net names are tracked."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ]
        router.add_component("U1", pads)

        assert router.net_names[1] == "VCC"

    def test_add_component_no_net(self):
        """Test adding pad with no net (net=0)."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 0},
        ]
        router.add_component("U1", pads)

        assert ("U1", "1") in router.pads
        assert 0 not in router.nets  # Net 0 not tracked

    def test_add_obstacle(self):
        """Test adding obstacle."""
        router = Autorouter(width=50.0, height=50.0)
        router.add_obstacle(25.0, 25.0, 5.0, 5.0, Layer.F_CU)

        # Verify grid was updated
        gx, gy = router.grid.world_to_grid(25.0, 25.0)
        cell = router.grid.grid[0][gy][gx]
        assert cell.blocked is True

    def test_get_statistics_empty(self):
        """Test statistics for empty router."""
        router = Autorouter(width=50.0, height=50.0)
        stats = router.get_statistics()

        assert stats["routes"] == 0
        assert stats["segments"] == 0
        assert stats["vias"] == 0

    def test_add_multiple_components(self):
        """Test adding multiple components."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1},
                {"number": "2", "x": 12.0, "y": 10.0, "net": 2},
            ],
        )
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1},
                {"number": "2", "x": 22.0, "y": 10.0, "net": 3},
            ],
        )

        assert len(router.pads) == 4
        assert len(router.nets[1]) == 2  # Two pads on net 1

    def test_net_class_map(self):
        """Test net class map usage."""
        net_classes = create_net_class_map(
            power_nets=["VCC", "GND"],
            clock_nets=["CLK", "MCLK"],
        )
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        assert "VCC" in router.net_class_map
        assert "CLK" in router.net_class_map

    def test_through_hole_pad(self):
        """Test through-hole pad handling."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "through_hole": True, "drill": 0.8},
        ]
        router.add_component("J1", pads)

        pad = router.pads[("J1", "1")]
        assert pad.through_hole is True
        assert pad.drill == 0.8

    def test_pad_defaults(self):
        """Test pad default values."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0},
        ]
        router.add_component("U1", pads)

        pad = router.pads[("U1", "1")]
        assert pad.width == 0.5  # Default
        assert pad.height == 0.5  # Default
        assert pad.net == 0  # Default
        assert pad.layer == Layer.F_CU  # Default

    def test_route_net_empty(self):
        """Test routing non-existent net."""
        router = Autorouter(width=50.0, height=50.0)
        routes = router.route_net(999)
        assert routes == []

    def test_route_net_single_pad(self):
        """Test routing net with only one pad (no route needed)."""
        router = Autorouter(width=50.0, height=50.0)
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        routes = router.route_net(1)
        assert routes == []

    def test_route_net_two_pads(self):
        """Test routing between two pads."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add two pads on the same net, reasonably close
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )

        routes = router.route_net(1, use_mst=False)
        assert len(routes) >= 1

    def test_route_net_mst(self):
        """Test MST-based routing for multi-pad nets."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add three pads on the same net
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        router.add_component(
            "U3",
            [
                {"number": "1", "x": 15.0, "y": 20.0, "net": 1, "net_name": "VCC"},
            ],
        )

        routes = router.route_net(1, use_mst=True)
        # MST should produce N-1 routes for N pads
        assert len(routes) >= 1

    def test_get_net_priority_with_pour_net_class(self):
        """Test net priority for pour nets (power nets) returns 99."""
        net_classes = create_net_class_map(power_nets=["VCC"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )

        # Issue #1295: Pour nets (is_pour_net=True) return priority 99
        priority, complexity_tier, neg_constraint, pad_count, distance = router._get_net_priority(1)
        assert priority == 99  # Pour net pushed to back

    def test_get_net_priority_with_signal_net_class(self):
        """Test net priority for non-pour signal net classes."""
        net_classes = create_net_class_map(clock_nets=["CLK"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "CLK"},
            ],
        )

        # Issue #1295: Return is now 5-tuple (priority, complexity_tier, -constraint_score, pad_count, distance)
        priority, complexity_tier, neg_constraint, pad_count, distance = router._get_net_priority(1)
        assert priority == 2  # Clock net has priority 2
        assert pad_count == 1
        assert distance == 0.0  # Single pad has no distance

    def test_get_net_priority_default(self):
        """Test net priority for unknown nets."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "UNKNOWN"},
            ],
        )

        # Issue #1295: Return is now 5-tuple (priority, complexity_tier, -constraint_score, pad_count, distance)
        priority, complexity_tier, neg_constraint, pad_count, distance = router._get_net_priority(1)
        assert priority == 10  # Default low priority
        assert distance == 0.0  # Single pad has no distance

    def test_route_all_empty(self):
        """Test route_all with no nets."""
        router = Autorouter(width=50.0, height=50.0)
        routes = router.route_all()
        assert routes == []

    def test_route_all_skips_net_zero(self):
        """Test that route_all skips net 0."""
        router = Autorouter(width=50.0, height=50.0)
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 0},  # No net
            ],
        )
        routes = router.route_all()
        assert routes == []

    def test_to_sexp_empty(self):
        """Test sexp generation for empty router."""
        router = Autorouter(width=50.0, height=50.0)
        sexp = router.to_sexp()
        assert sexp == ""

    def test_evaluate_solution_empty(self):
        """Test solution evaluation with no routes."""
        router = Autorouter(width=50.0, height=50.0)
        score = router._evaluate_solution([])
        assert score == 0.0

    def test_evaluate_solution_with_routes(self):
        """Test solution evaluation with routes."""
        router = Autorouter(width=50.0, height=50.0)

        # Add a net so total_nets > 0
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 1},
            ],
        )

        route = Route(net=1, net_name="test")
        route.segments.append(Segment(10, 10, 20, 10, 0.2, Layer.F_CU, net=1))

        score = router._evaluate_solution([route])
        assert score > 0

    def test_reset_for_new_trial(self):
        """Test router reset for monte carlo trials."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add pads
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )

        # Add a fake route
        router.routes.append(Route(net=1, net_name="VCC"))

        # Reset
        router._reset_for_new_trial()

        # Pads should still be there, routes should be cleared
        assert ("U1", "1") in router.pads
        assert router.routes == []

    def test_shuffle_within_tiers(self):
        """Test net shuffling preserves priority tiers."""
        # Issue #1295: Use clock (priority 2) and signal (priority 10) to test
        # tier-preserving shuffle. Power nets are now pour nets (priority 99)
        # and would sort last, not first.
        net_classes = create_net_class_map(clock_nets=["CLK1", "CLK2"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # Add clock nets (priority 2) and signal nets (priority 10)
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "CLK1"},
                {"number": "2", "x": 12.0, "y": 10.0, "net": 1, "net_name": "CLK1"},
                {"number": "3", "x": 14.0, "y": 10.0, "net": 2, "net_name": "CLK2"},
                {"number": "4", "x": 16.0, "y": 10.0, "net": 2, "net_name": "CLK2"},
            ],
        )
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 3, "net_name": "SIG1"},
                {"number": "2", "x": 22.0, "y": 10.0, "net": 3, "net_name": "SIG1"},
            ],
        )

        net_order = [1, 2, 3]  # CLK1, CLK2, SIG1
        shuffled = router._shuffle_within_tiers(net_order)

        # Clock nets should come before signal nets
        assert set(shuffled[:2]) == {1, 2}  # Clock nets first
        assert shuffled[2] == 3  # Signal net last

    def test_create_intra_ic_routes(self):
        """Test intra-IC route creation for same-component pins."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add component with two pins on the same net, close together
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
                {"number": "2", "x": 11.0, "y": 10.0, "net": 1, "net_name": "VCC"},  # 1mm apart
            ],
        )

        pads = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads)

        # Should create a direct route between these close pins
        assert len(routes) == 1
        assert len(connected) == 2

    def test_create_intra_ic_routes_too_far(self):
        """Test that intra-IC routes not created for distant pins."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add component with two pins on the same net, far apart
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},  # 10mm apart
            ],
        )

        pads = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads)

        # Too far apart, no intra-IC route
        assert len(routes) == 0
        assert len(connected) == 0

    def test_get_statistics_with_routes(self):
        """Test statistics with actual routes."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add a route manually
        route = Route(net=1, net_name="test")
        route.segments.append(Segment(10, 10, 20, 10, 0.2, Layer.F_CU, net=1))
        route.vias.append(Via(15, 10, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1))
        router.routes.append(route)

        stats = router.get_statistics()

        assert stats["routes"] == 1
        assert stats["segments"] == 1
        assert stats["vias"] == 1
        assert stats["total_length_mm"] == 10.0  # 20-10 = 10mm


class TestRoutingResult:
    """Tests for RoutingResult dataclass."""

    def test_routing_result_creation(self):
        """Test creating a routing result."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=10,
            nets_routed=8,
            overflow=2,
            converged=False,
            iterations_used=5,
            statistics={"routes": 8},
        )

        assert result.layer_count == 2
        assert result.nets_requested == 10
        assert result.nets_routed == 8
        assert result.converged is False

    def test_routing_result_success_rate(self):
        """Test success rate calculation."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=10,
            nets_routed=8,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )

        assert result.success_rate == 0.8

    def test_routing_result_success_rate_zero_nets(self):
        """Test success rate with zero nets."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=0,
            nets_routed=0,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )

        assert result.success_rate == 1.0

    def test_routing_result_str(self):
        """Test string representation."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=10,
            nets_routed=10,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )

        s = str(result)
        assert "CONVERGED" in s
        assert "2L" in s
        assert "10/10" in s


class TestAStarNode:
    """Tests for AStarNode dataclass."""

    def test_astar_node_creation(self):
        """Test creating an A* node."""
        node = AStarNode(f_score=10.0, g_score=5.0, x=3, y=4, layer=0)

        assert node.f_score == 10.0
        assert node.g_score == 5.0
        assert node.x == 3
        assert node.y == 4
        assert node.layer == 0
        assert node.parent is None
        assert node.via_from_parent is False

    def test_astar_node_ordering(self):
        """Test node ordering by f_score."""
        node1 = AStarNode(f_score=10.0, g_score=5.0, x=0, y=0, layer=0)
        node2 = AStarNode(f_score=5.0, g_score=3.0, x=1, y=1, layer=0)

        # Lower f_score should come first
        assert node2 < node1

    def test_astar_node_with_parent(self):
        """Test node with parent reference."""
        parent = AStarNode(f_score=5.0, g_score=2.0, x=0, y=0, layer=0)
        child = AStarNode(f_score=10.0, g_score=5.0, x=1, y=0, layer=0, parent=parent)

        assert child.parent is parent

    def test_astar_node_via(self):
        """Test node representing a via transition."""
        node = AStarNode(
            f_score=15.0, g_score=10.0, x=5, y=5, layer=1, parent=None, via_from_parent=True
        )

        assert node.via_from_parent is True

    def test_astar_node_direction(self):
        """Test node direction tracking."""
        node = AStarNode(
            f_score=10.0,
            g_score=5.0,
            x=1,
            y=0,
            layer=0,
            direction=(1, 0),  # Moving right
        )

        assert node.direction == (1, 0)


class TestRouterPathfinder:
    """Tests for Router (pathfinder) class."""

    def test_router_creation(self):
        """Test creating a router."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        assert router.grid is grid
        assert router.rules is rules
        assert router.heuristic is not None

    def test_router_with_custom_heuristic(self):
        """Test router with custom heuristic."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        heuristic = ManhattanHeuristic()
        router = Router(grid, rules, heuristic=heuristic)

        assert router.heuristic is heuristic

    def test_router_get_net_class(self):
        """Test getting net class for a net."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        net_classes = create_net_class_map(power_nets=["VCC"])
        router = Router(grid, rules, net_class_map=net_classes)

        nc = router._get_net_class("VCC")
        assert nc is not None
        assert nc.name == "Power"

        nc_unknown = router._get_net_class("UNKNOWN")
        assert nc_unknown is None

    def test_router_route_simple(self):
        """Test simple routing between two pads."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Create two pads on the same layer
        start_pad = Pad(
            x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

        # Add pads to grid
        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        assert route is not None
        assert route.net == 1
        assert len(route.segments) > 0

    def test_router_route_with_weight(self):
        """Test weighted A* routing."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        start_pad = Pad(
            x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        # Higher weight = faster but potentially suboptimal
        route = router.route(start_pad, end_pad, weight=2.0)

        assert route is not None

    def test_router_route_blocked(self):
        """Test routing around obstacles."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Create obstacle between pads
        obstacle = Obstacle(x=25.0, y=25.0, width=5.0, height=20.0, layer=Layer.F_CU)
        grid.add_obstacle(obstacle)

        start_pad = Pad(
            x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        # Should still find a route (going around)
        assert route is not None

    def test_router_is_trace_blocked(self):
        """Test trace blocking check."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Block a cell
        grid.grid[0][10][10].blocked = True
        grid.grid[0][10][10].is_obstacle = True

        # Check that trace is blocked at that location
        blocked = router._is_trace_blocked(10, 10, 0, net=1)
        assert blocked is True

    def test_router_is_trace_blocked_same_net(self):
        """Test trace not blocked by same net cells."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Block a cell with same net (e.g., a pad)
        grid.grid[0][10][10].blocked = True
        grid.grid[0][10][10].net = 1
        grid.grid[0][10][10].is_obstacle = False

        # Should not be blocked for same net
        blocked = router._is_trace_blocked(10, 10, 0, net=1)
        assert blocked is False

    def test_router_is_via_blocked(self):
        """Test via blocking check."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Block a cell with obstacle
        grid.grid[0][10][10].blocked = True
        grid.grid[0][10][10].is_obstacle = True

        blocked = router._is_via_blocked(10, 10, 0, net=1)
        assert blocked is True

    def test_router_get_congestion_cost(self):
        """Test congestion cost calculation."""
        rules = DesignRules(grid_resolution=0.5, congestion_threshold=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Without congestion
        cost = router._get_congestion_cost(10, 10, 0)
        assert cost == 0.0

    def test_router_reconstructs_route_correctly(self):
        """Test route reconstruction from A* nodes."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        start_pad = Pad(
            x=10.0, y=10.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=15.0, y=10.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        assert route is not None
        # Check route properties
        assert route.net == 1
        assert route.net_name == "test"
        # Should have segments connecting the pads
        if route.segments:
            first_seg = route.segments[0]
            assert first_seg.layer == Layer.F_CU

    def test_router_through_hole_pads(self):
        """Test routing with through-hole pads."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Through-hole pads can be accessed from any layer
        start_pad = Pad(
            x=10.0,
            y=25.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="test",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.8,
        )
        end_pad = Pad(
            x=40.0,
            y=25.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="test",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.8,
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        assert route is not None

    def test_trace_half_width_cells_includes_clearance(self):
        """Test that trace blocking radius includes clearance (issue #553).

        Previously, _trace_half_width_cells only accounted for trace_width/2,
        causing DRC clearance violations when traces were placed too close to
        obstacles. The fix ensures the calculation includes both trace half-width
        AND clearance distance.

        The formula should be: ceil((trace_width/2 + trace_clearance) / resolution)
        """
        # Set up design rules with specific values
        trace_width = 0.15  # mm
        trace_clearance = 0.127  # mm (JLCPCB minimum)
        grid_resolution = 0.1  # mm

        rules = DesignRules(
            trace_width=trace_width,
            trace_clearance=trace_clearance,
            grid_resolution=grid_resolution,
        )
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Expected: ceil((0.15/2 + 0.127) / 0.1) = ceil((0.075 + 0.127) / 0.1)
        #         = ceil(0.202 / 0.1) = ceil(2.02) = 3
        expected_cells = max(1, math.ceil((trace_width / 2 + trace_clearance) / grid_resolution))

        assert router._trace_half_width_cells == expected_cells
        assert router._trace_half_width_cells == 3  # Explicit check

        # The OLD (buggy) calculation would have been:
        # ceil(0.15/2 / 0.1) = ceil(0.75) = 1
        # Verify we're NOT using the old formula
        old_buggy_cells = max(1, math.ceil((trace_width / 2) / grid_resolution))
        assert (
            router._trace_half_width_cells != old_buggy_cells or expected_cells == old_buggy_cells
        )

    def test_trace_clearance_prevents_drc_violations(self):
        """Test that router respects clearance when routing near obstacles.

        This is a functional test for issue #553. With the fix, a trace should
        not be placed within clearance distance of an obstacle, even if the
        grid resolution equals the clearance value.
        """
        # Use grid resolution equal to clearance (the problematic case)
        rules = DesignRules(
            trace_width=0.15,
            trace_clearance=0.127,
            grid_resolution=0.127,  # Same as clearance - previously problematic
        )
        grid = RoutingGrid(30.0, 30.0, rules)
        router = Router(grid, rules)

        # Place an obstacle in the middle
        obstacle = Obstacle(x=15.0, y=15.0, width=2.0, height=2.0, layer=Layer.F_CU)
        grid.add_obstacle(obstacle)

        # Create pads on opposite sides that would need to route around obstacle
        start_pad = Pad(
            x=5.0, y=15.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=25.0, y=15.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        # Should find a route that goes around the obstacle
        assert route is not None

        # Verify no segment passes through the obstacle's clearance zone
        # Obstacle is at (15, 15) with 2mm width/height
        # Clearance zone extends from (14-clearance, 14-clearance) to (16+clearance, 16+clearance)
        clearance = rules.trace_clearance
        trace_half = rules.trace_width / 2
        min_safe_distance = clearance + trace_half  # Trace edge must be this far from obstacle

        for seg in route.segments:
            if seg.layer != Layer.F_CU:
                continue
            # For horizontal segments at y near obstacle
            if seg.y1 == seg.y2:  # Horizontal segment
                y_dist = abs(seg.y1 - 15.0) - 1.0  # Distance from y to obstacle edge
                if y_dist < min_safe_distance:
                    # This segment is within clearance height band, check x doesn't cross
                    x_min = min(seg.x1, seg.x2)
                    x_max = max(seg.x1, seg.x2)
                    # If segment crosses the obstacle x-range
                    if x_min < 16.0 + clearance and x_max > 14.0 - clearance:
                        # This would be a clearance violation - fail
                        assert y_dist >= min_safe_distance, (
                            f"Segment at y={seg.y1} too close to obstacle"
                        )

    def test_routes_gnd_when_pth_pads_overlap_clearance_zones(self):
        """Test that router routes GND when PTH pad clearance zones overlap.

        This is a regression test for issue #864. The simple-led board has:
        - J1: Power connector with PTH pads (VCC on pin 1, GND on pin 2)
        - R1: SMD resistor (VCC on pin 1, LED_ANODE on pin 2)
        - D1: LED with PTH pads (GND on pin 1, LED_ANODE on pin 2)

        The GND net connects J1.2 to D1.1. Both are PTH pads, and their
        clearance zones may overlap. The router should still find a path
        through same-net clearance zones.
        """
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
        grid = RoutingGrid(25.0, 20.0, rules, origin_x=100.0, origin_y=100.0)
        router = Router(grid, rules)

        # J1.2 (GND) - PTH pad at (105, 111.27)
        j1_gnd = Pad(
            x=105.0,
            y=111.27,
            width=1.7,
            height=1.7,
            net=3,
            net_name="GND",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
        )

        # D1.1 (GND) - PTH pad at (120, 108.73) with 90 degree rotation
        d1_gnd = Pad(
            x=118.73,  # 120 - 1.27 due to 90 degree rotation
            y=110.0,
            width=1.8,
            height=1.8,
            net=3,
            net_name="GND",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.9,
        )

        grid.add_pad(j1_gnd)
        grid.add_pad(d1_gnd)

        route = router.route(j1_gnd, d1_gnd)

        # The router should find a route between the two GND pads
        assert route is not None, "Router should find route for GND net"
        assert len(route.segments) > 0, "Route should have at least one segment"
        assert route.net == 3, "Route should be for GND net (net ID 3)"

    def test_same_net_clearance_zones_passable(self):
        """Test that same-net clearance zones don't block routing.

        Issue #864: When two pads of the same net have overlapping clearance
        zones, the router should allow traces to pass through since they
        belong to the same net.
        """
        rules = DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)
        grid = RoutingGrid(20.0, 10.0, rules)
        router = Router(grid, rules)

        # Create two same-net pads close together (clearance zones will overlap)
        pad1 = Pad(x=5.0, y=5.0, width=1.0, height=1.0, net=1, net_name="NET1", layer=Layer.F_CU)
        pad2 = Pad(x=15.0, y=5.0, width=1.0, height=1.0, net=1, net_name="NET1", layer=Layer.F_CU)

        grid.add_pad(pad1)
        grid.add_pad(pad2)

        route = router.route(pad1, pad2)

        # Should successfully route between same-net pads
        assert route is not None, "Should route between same-net pads"
        assert len(route.segments) > 0


class TestAdaptiveAutorouter:
    """Tests for AdaptiveAutorouter class."""

    def test_adaptive_autorouter_creation(self):
        """Test creating adaptive autorouter."""
        components = [
            {
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
            }
        ]
        net_map = {"VCC": 1, "GND": 2}

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=components,
            net_map=net_map,
        )

        assert adaptive.width == 50.0
        assert adaptive.height == 50.0
        assert adaptive.max_layers == 6

    def test_adaptive_autorouter_skip_nets(self):
        """Test skip nets parameter."""
        components = []
        net_map = {}

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=components,
            net_map=net_map,
            skip_nets=["GND", "VCC"],
        )

        assert "GND" in adaptive.skip_nets
        assert "VCC" in adaptive.skip_nets

    def test_adaptive_autorouter_max_layers(self):
        """Test max layers limit."""
        components = []
        net_map = {}

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=components,
            net_map=net_map,
            max_layers=4,
        )

        assert adaptive.max_layers == 4

    def test_adaptive_layer_stacks(self):
        """Test layer stack progression."""
        assert len(AdaptiveAutorouter.LAYER_STACKS) == 3
        assert AdaptiveAutorouter.LAYER_STACKS[0].num_layers == 2
        assert AdaptiveAutorouter.LAYER_STACKS[1].num_layers == 4
        assert AdaptiveAutorouter.LAYER_STACKS[2].num_layers == 6

    def test_adaptive_to_sexp_no_route(self):
        """Test to_sexp before routing raises error."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=[],
            net_map={},
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.to_sexp()

    def test_adaptive_get_routes_no_route(self):
        """Test get_routes before routing raises error."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=[],
            net_map={},
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.get_routes()

    def test_adaptive_layer_count_no_route(self):
        """Test layer_count before routing returns 0."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=[],
            net_map={},
        )

        assert adaptive.layer_count == 0

    def test_adaptive_check_convergence(self):
        """Test convergence checking."""
        components = [
            {
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                ],
            }
        ]
        net_map = {"NET1": 1}

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=components,
            net_map=net_map,
            verbose=False,
        )

        # Create a router to test convergence check
        stack = LayerStack.two_layer()
        router = adaptive._create_autorouter(stack)

        # No routes yet, no overflow
        converged = adaptive._check_convergence(router, overflow=0)
        # Should not converge because no nets are routed
        assert converged is False
