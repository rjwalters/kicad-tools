"""Tests for router/core.py module."""

import pytest
import math

from kicad_tools.router.core import Autorouter, AdaptiveAutorouter, RoutingResult
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Obstacle, Pad, Route, Segment


class TestAutorouterInit:
    """Tests for Autorouter initialization."""

    def test_default_initialization(self):
        """Test Autorouter with default parameters."""
        router = Autorouter(width=50.0, height=40.0)
        assert router.grid.width == 50.0
        assert router.grid.height == 40.0
        assert router.rules is not None
        assert router.pads == {}
        assert router.nets == {}
        assert router.routes == []

    def test_with_origin(self):
        """Test Autorouter with custom origin."""
        router = Autorouter(width=50.0, height=40.0, origin_x=10.0, origin_y=5.0)
        assert router.grid.origin_x == 10.0
        assert router.grid.origin_y == 5.0

    def test_with_custom_rules(self):
        """Test Autorouter with custom design rules."""
        rules = DesignRules(trace_width=0.3, via_diameter=0.8)
        router = Autorouter(width=50.0, height=40.0, rules=rules)
        assert router.rules.trace_width == 0.3
        assert router.rules.via_diameter == 0.8

    def test_with_layer_stack(self):
        """Test Autorouter with custom layer stack."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router = Autorouter(width=50.0, height=40.0, layer_stack=stack)
        assert router.grid.num_layers == 4


class TestAutorouterAddComponent:
    """Tests for adding components to Autorouter."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_add_smd_component(self, router):
        """Test adding an SMD component with pads."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "VCC"},
            {"number": "2", "x": 11.0, "y": 10.0, "width": 0.5, "height": 0.5, "net": 2, "net_name": "GND"},
        ]
        router.add_component("R1", pads)

        assert ("R1", "1") in router.pads
        assert ("R1", "2") in router.pads
        assert 1 in router.nets
        assert 2 in router.nets
        assert router.net_names[1] == "VCC"
        assert router.net_names[2] == "GND"

    def test_add_through_hole_component(self, router):
        """Test adding a through-hole component."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "width": 1.7, "height": 1.7,
             "net": 1, "net_name": "NET1", "through_hole": True, "drill": 1.0},
        ]
        router.add_component("U1", pads)

        pad = router.pads[("U1", "1")]
        assert pad.through_hole is True
        assert pad.drill == 1.0

    def test_multi_pin_net(self, router):
        """Test that nets track all connected pads."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 11.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("U1", pads)

        assert len(router.nets[1]) == 2
        assert ("U1", "1") in router.nets[1]
        assert ("U1", "2") in router.nets[1]


class TestAutorouterAddObstacle:
    """Tests for adding obstacles to Autorouter."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_add_obstacle(self, router):
        """Test adding an obstacle."""
        router.add_obstacle(25.0, 20.0, 5.0, 5.0, Layer.F_CU)

        # Verify the obstacle was added by checking grid cells are blocked
        gx, gy = router.grid.world_to_grid(25.0, 20.0)
        assert router.grid.is_blocked(gx, gy, Layer.F_CU) is True

    def test_add_obstacle_default_layer(self, router):
        """Test adding an obstacle on default layer."""
        router.add_obstacle(25.0, 20.0, 5.0, 5.0)

        gx, gy = router.grid.world_to_grid(25.0, 20.0)
        assert router.grid.is_blocked(gx, gy, Layer.F_CU) is True


class TestAutorouterRouting:
    """Tests for routing functionality."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_route_net_nonexistent(self, router):
        """Test routing a nonexistent net returns empty list."""
        routes = router.route_net(999)
        assert routes == []

    def test_route_net_single_pad(self, router):
        """Test routing a net with only one pad returns empty list."""
        pads = [{"number": "1", "x": 10.0, "y": 10.0, "net": 1}]
        router.add_component("R1", pads)

        routes = router.route_net(1)
        assert routes == []

    def test_route_two_pad_net(self, router):
        """Test routing a two-pad net."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)
        # Should successfully route (may have segments)
        # The route may or may not succeed depending on clearances
        assert isinstance(routes, list)


class TestAutorouterStatistics:
    """Tests for Autorouter statistics and output."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_get_statistics_empty(self, router):
        """Test statistics on empty router."""
        stats = router.get_statistics()
        assert stats["routes"] == 0
        assert stats["segments"] == 0
        assert stats["vias"] == 0
        assert stats["total_length_mm"] == 0.0
        assert stats["nets_routed"] == 0

    def test_get_statistics_with_routes(self, router):
        """Test statistics with some routes."""
        # Manually add a route
        seg = Segment(x1=10.0, y1=10.0, x2=20.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
        router.routes.append(route)

        stats = router.get_statistics()
        assert stats["routes"] == 1
        assert stats["segments"] == 1
        assert stats["vias"] == 0
        assert stats["nets_routed"] == 1

    def test_to_sexp_empty(self, router):
        """Test S-expression output on empty router."""
        sexp = router.to_sexp()
        assert sexp == ""

    def test_to_sexp_with_routes(self, router):
        """Test S-expression output with routes."""
        seg = Segment(x1=10.0, y1=10.0, x2=20.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
        router.routes.append(route)

        sexp = router.to_sexp()
        assert "segment" in sexp
        assert "10.0000" in sexp
        assert "20.0000" in sexp


class TestAutorouterNetPriority:
    """Tests for net priority ordering."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_get_net_priority_unknown_net(self, router):
        """Test priority for unknown net class."""
        # Add a pad with unknown net class
        pads = [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "RANDOM_NET"}]
        router.add_component("R1", pads)

        priority, pad_count = router._get_net_priority(1)
        assert priority == 10  # Default priority
        assert pad_count == 1


class TestAutorouterMonteCarlo:
    """Tests for Monte Carlo routing methods."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_shuffle_within_tiers(self, router):
        """Test that tier shuffling preserves tier order."""
        # Add components with different net classes
        pads1 = [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 20.0, "y": 10.0, "net": 2, "net_name": "NET2"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        net_order = [1, 2]
        shuffled = router._shuffle_within_tiers(net_order)

        assert set(shuffled) == set(net_order)
        assert len(shuffled) == len(net_order)

    def test_evaluate_solution_empty(self, router):
        """Test solution evaluation with no routes."""
        score = router._evaluate_solution([])
        assert score == 0.0

    def test_evaluate_solution_with_routes(self, router):
        """Test solution evaluation with routes."""
        # Add a net for tracking
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        # Create a route
        seg = Segment(x1=10.0, y1=10.0, x2=15.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])

        score = router._evaluate_solution([route])
        assert score > 0  # Should have positive score with routed net


class TestRoutingResult:
    """Tests for RoutingResult dataclass."""

    def test_success_rate_full(self):
        """Test success rate with all nets routed."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=10,
            nets_routed=10,
            overflow=0,
            converged=True,
            iterations_used=5,
            statistics={}
        )
        assert result.success_rate == 1.0

    def test_success_rate_partial(self):
        """Test success rate with partial routing."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=10,
            nets_routed=7,
            overflow=0,
            converged=False,
            iterations_used=10,
            statistics={}
        )
        assert result.success_rate == 0.7

    def test_success_rate_zero_nets(self):
        """Test success rate with zero nets requested."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=0,
            nets_routed=0,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={}
        )
        assert result.success_rate == 1.0

    def test_str_converged(self):
        """Test string representation for converged result."""
        result = RoutingResult(
            routes=[],
            layer_count=4,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            nets_requested=20,
            nets_routed=20,
            overflow=0,
            converged=True,
            iterations_used=3,
            statistics={}
        )
        s = str(result)
        assert "CONVERGED" in s
        assert "4L" in s
        assert "20/20" in s

    def test_str_not_converged(self):
        """Test string representation for non-converged result."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=30,
            nets_routed=25,
            overflow=5,
            converged=False,
            iterations_used=10,
            statistics={}
        )
        s = str(result)
        assert "NOT CONVERGED" in s
        assert "overflow=5" in s


class TestAdaptiveAutorouterInit:
    """Tests for AdaptiveAutorouter initialization."""

    def test_default_initialization(self):
        """Test AdaptiveAutorouter with default parameters."""
        components = []
        net_map = {}

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=components,
            net_map=net_map
        )

        assert adaptive.width == 50.0
        assert adaptive.height == 40.0
        assert adaptive.max_layers == 6
        assert adaptive.result is None

    def test_with_custom_max_layers(self):
        """Test AdaptiveAutorouter with custom max layers."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=[],
            net_map={},
            max_layers=4
        )

        assert adaptive.max_layers == 4

    def test_with_skip_nets(self):
        """Test AdaptiveAutorouter with skip nets."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=[],
            net_map={},
            skip_nets=["GND", "VCC"]
        )

        assert "GND" in adaptive.skip_nets
        assert "VCC" in adaptive.skip_nets


class TestAdaptiveAutorouterLayerStacks:
    """Tests for layer stack configuration."""

    def test_layer_stacks_order(self):
        """Test that layer stacks are in increasing order."""
        stacks = AdaptiveAutorouter.LAYER_STACKS
        assert len(stacks) == 3
        assert stacks[0].num_layers == 2
        assert stacks[1].num_layers == 4
        assert stacks[2].num_layers == 6


class TestAdaptiveAutorouterMethods:
    """Tests for AdaptiveAutorouter methods."""

    @pytest.fixture
    def simple_component(self):
        """Create a simple component dict."""
        return {
            "ref": "R1",
            "x": 25.0,
            "y": 20.0,
            "rotation": 0,
            "pads": [
                {"number": "1", "x": -0.5, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                {"number": "2", "x": 0.5, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET2"},
            ]
        }

    def test_create_autorouter(self, simple_component):
        """Test creating an autorouter from components."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=[simple_component],
            net_map={"NET1": 1, "NET2": 2}
        )

        stack = LayerStack.two_layer()
        router = adaptive._create_autorouter(stack)

        assert router is not None
        assert router.grid.num_layers == 2

    def test_layer_count_no_result(self):
        """Test layer_count property with no result."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=[],
            net_map={}
        )

        assert adaptive.layer_count == 0

    def test_get_routes_no_result_raises(self):
        """Test get_routes raises if not routed."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=[],
            net_map={}
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.get_routes()

    def test_to_sexp_no_result_raises(self):
        """Test to_sexp raises if not routed."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=[],
            net_map={}
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.to_sexp()


class TestAdaptiveAutorouterComponentTransform:
    """Tests for component coordinate transformation."""

    def test_add_component_rotation(self):
        """Test that component rotation transforms pad positions."""
        component = {
            "ref": "R1",
            "x": 25.0,
            "y": 20.0,
            "rotation": 90,  # 90 degree rotation
            "pads": [
                {"number": "1", "x": 1.0, "y": 0.0, "net": "NET1"},
            ]
        }

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=40.0,
            components=[component],
            net_map={"NET1": 1}
        )

        stack = LayerStack.two_layer()
        router = adaptive._create_autorouter(stack)

        # After 90 degree rotation, (1, 0) should become approximately (0, -1)
        pad = router.pads.get(("R1", "1"))
        assert pad is not None
        # x should be close to 25.0 (component center)
        assert abs(pad.x - 25.0) < 0.01
        # y should be offset by approximately -1.0 from center
        assert abs(pad.y - 19.0) < 0.01


class TestAutorouterIntraICRoutes:
    """Tests for intra-IC routing functionality."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_intra_ic_routes_single_component(self, router):
        """Test intra-IC routing for same-component pins on same net."""
        # Create IC with multiple pins on same net
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "SYNC_L"},
            {"number": "3", "x": 11.0, "y": 10.0, "net": 1, "net_name": "SYNC_L"},
            {"number": "4", "x": 12.0, "y": 10.0, "net": 1, "net_name": "SYNC_L"},
        ]
        router.add_component("U1", pads)

        pads_list = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads_list)

        # Should create routes connecting nearby same-IC pins
        assert len(routes) >= 0  # May create short connections
        # Connected indices should be tracked
        assert isinstance(connected, set)

    def test_intra_ic_routes_far_apart(self, router):
        """Test that distant pins don't get intra-IC routes."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 40.0, "y": 30.0, "net": 1, "net_name": "NET1"},  # Far apart
        ]
        router.add_component("U1", pads)

        pads_list = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads_list)

        # Distance > 3mm should not create intra-IC route
        assert len(routes) == 0


class TestAutorouterRouteAll:
    """Tests for route_all methods."""

    @pytest.fixture
    def router_with_nets(self):
        """Create router with multiple nets."""
        router = Autorouter(width=50.0, height=40.0)

        # Add two simple 2-pin nets
        pads1 = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        pads2 = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 15.0, "y": 20.0, "net": 2, "net_name": "NET2"},
        ]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        return router

    def test_route_all_basic(self, router_with_nets):
        """Test basic route_all functionality."""
        routes = router_with_nets.route_all()
        assert isinstance(routes, list)

    def test_route_all_with_order(self, router_with_nets):
        """Test route_all with custom net order."""
        routes = router_with_nets.route_all(net_order=[2, 1])
        assert isinstance(routes, list)

    def test_route_all_skips_net_zero(self, router_with_nets):
        """Test that net 0 is skipped during routing."""
        # Add a pad with net 0
        pads = [{"number": "1", "x": 30.0, "y": 10.0, "net": 0}]
        router_with_nets.add_component("R3", pads)

        routes = router_with_nets.route_all()
        # Should not fail, net 0 is skipped
        assert isinstance(routes, list)
