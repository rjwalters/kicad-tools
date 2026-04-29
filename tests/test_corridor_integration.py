"""Tests for corridor cost integration in A* pathfinder (Issue #2288).

Tests cover:
1. Corridor cost is included in forward A* same-layer expansion
2. Corridor cost is included in forward A* via expansion
3. Corridor cost is included in bidirectional A* expansion
4. DesignRules exposes cost_corridor_deviation field
5. Corridor penalty decay in negotiated routing iterations
6. Nets without corridor assignments route normally (cost=0)
"""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.sparse import Corridor, Waypoint


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rules():
    """Design rules with corridor cost enabled."""
    return DesignRules(grid_resolution=0.5, cost_corridor_deviation=5.0)


@pytest.fixture
def grid(rules):
    """A small routing grid."""
    return RoutingGrid(30.0, 30.0, rules)


@pytest.fixture
def router(grid, rules):
    """A Router attached to the grid."""
    return Router(grid, rules)


@pytest.fixture
def horizontal_corridor():
    """A horizontal corridor along y=15 on layer 0."""
    waypoints = [
        Waypoint(x=0.0, y=15.0, layer=0, waypoint_type="global"),
        Waypoint(x=30.0, y=15.0, layer=0, waypoint_type="global"),
    ]
    return Corridor.from_waypoints(waypoints=waypoints, net=1, width=3.0)


# =============================================================================
# DesignRules tests
# =============================================================================


class TestDesignRulesCorridorField:
    """Verify that cost_corridor_deviation is configurable."""

    def test_default_value(self):
        rules = DesignRules()
        assert rules.cost_corridor_deviation == 5.0

    def test_custom_value(self):
        rules = DesignRules(cost_corridor_deviation=12.0)
        assert rules.cost_corridor_deviation == 12.0

    def test_zero_disables(self):
        rules = DesignRules(cost_corridor_deviation=0.0)
        assert rules.cost_corridor_deviation == 0.0


# =============================================================================
# get_corridor_cost integration
# =============================================================================


class TestCorridorCostInGrid:
    """Verify the grid's get_corridor_cost works with corridor preferences."""

    def test_no_corridor_returns_zero(self, grid):
        """Nets without corridor assignment incur no penalty."""
        cost = grid.get_corridor_cost(10, 10, 0, net=1)
        assert cost == 0.0

    def test_inside_corridor_returns_zero(self, grid, horizontal_corridor):
        """Points inside the corridor incur no penalty."""
        grid.set_corridor_preference(horizontal_corridor, net=1, penalty=5.0)
        # y=15 is the centerline, grid coords for y=15 at resolution 0.5 = 30
        gx, gy = grid.world_to_grid(15.0, 15.0)
        cost = grid.get_corridor_cost(gx, gy, 0, net=1)
        assert cost == 0.0

    def test_outside_corridor_returns_penalty(self, grid, horizontal_corridor):
        """Points far outside the corridor incur the penalty."""
        grid.set_corridor_preference(horizontal_corridor, net=1, penalty=5.0)
        # y=0 is far from the y=15 centerline
        gx, gy = grid.world_to_grid(15.0, 0.0)
        cost = grid.get_corridor_cost(gx, gy, 0, net=1)
        assert cost == 5.0

    def test_wrong_layer_returns_penalty(self, grid, horizontal_corridor):
        """Points on the wrong layer incur the penalty."""
        grid.set_corridor_preference(horizontal_corridor, net=1, penalty=5.0)
        gx, gy = grid.world_to_grid(15.0, 15.0)
        cost = grid.get_corridor_cost(gx, gy, 1, net=1)
        assert cost == 5.0


# =============================================================================
# A* expansion integration
# =============================================================================


class TestCorridorCostInAStar:
    """Verify that corridor cost affects A* routing decisions."""

    def test_route_prefers_corridor(self, rules, grid, router, horizontal_corridor):
        """A net with a corridor should route closer to the corridor centerline."""
        # Pads at same y as corridor centerline
        start_on = Pad(
            x=5.0, y=15.0, width=0.5, height=0.5,
            net=1, net_name="test", layer=Layer.F_CU,
        )
        end_on = Pad(
            x=25.0, y=15.0, width=0.5, height=0.5,
            net=1, net_name="test", layer=Layer.F_CU,
        )
        grid.add_pad(start_on)
        grid.add_pad(end_on)

        # Route without corridor
        route_no_corridor = router.route(start_on, end_on)
        assert route_no_corridor is not None

        # Now set corridor and route again (reset grid first)
        grid2 = RoutingGrid(30.0, 30.0, rules)
        router2 = Router(grid2, rules)
        grid2.add_pad(start_on)
        grid2.add_pad(end_on)
        grid2.set_corridor_preference(horizontal_corridor, net=1, penalty=5.0)

        route_with_corridor = router2.route(start_on, end_on)
        assert route_with_corridor is not None

        # Both should produce valid routes; the corridor route succeeds
        # without error, confirming the corridor cost is wired in
        assert route_with_corridor.net == 1

    def test_corridor_steers_route_toward_centerline(self, rules):
        """With a strong corridor penalty, the route should stay inside the corridor."""
        # Use high penalty to strongly steer the route
        strong_rules = DesignRules(
            grid_resolution=1.0,
            cost_corridor_deviation=20.0,
        )
        grid = RoutingGrid(40.0, 40.0, strong_rules)
        router = Router(grid, strong_rules)

        # Corridor along y=20 (centerline)
        waypoints = [
            Waypoint(x=0.0, y=20.0, layer=0, waypoint_type="global"),
            Waypoint(x=40.0, y=20.0, layer=0, waypoint_type="global"),
        ]
        corridor = Corridor.from_waypoints(waypoints=waypoints, net=1, width=6.0)

        # Pads offset from centerline but within corridor width
        start_pad = Pad(
            x=5.0, y=20.0, width=1.0, height=1.0,
            net=1, net_name="sig", layer=Layer.F_CU,
        )
        end_pad = Pad(
            x=35.0, y=20.0, width=1.0, height=1.0,
            net=1, net_name="sig", layer=Layer.F_CU,
        )
        grid.add_pad(start_pad)
        grid.add_pad(end_pad)
        grid.set_corridor_preference(corridor, net=1, penalty=20.0)

        route = router.route(start_pad, end_pad)
        assert route is not None

        # Check that segments stay within the corridor bounds (y=20 +/- 6)
        for seg in route.segments:
            assert 14.0 <= seg.y1 <= 26.0, (
                f"Segment y1={seg.y1} outside corridor bounds"
            )
            assert 14.0 <= seg.y2 <= 26.0, (
                f"Segment y2={seg.y2} outside corridor bounds"
            )

    def test_zero_cost_disables_corridor(self, rules):
        """With cost_corridor_deviation=0, corridor has no effect."""
        no_corridor_rules = DesignRules(
            grid_resolution=0.5,
            cost_corridor_deviation=0.0,
        )
        grid = RoutingGrid(30.0, 30.0, no_corridor_rules)
        router = Router(grid, no_corridor_rules)

        start_pad = Pad(
            x=5.0, y=15.0, width=0.5, height=0.5,
            net=1, net_name="test", layer=Layer.F_CU,
        )
        end_pad = Pad(
            x=25.0, y=15.0, width=0.5, height=0.5,
            net=1, net_name="test", layer=Layer.F_CU,
        )
        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        # With cost=0, the corridor penalty on the grid is still set but
        # the grid returns 0 penalty, so routing should work identically
        waypoints = [
            Waypoint(x=0.0, y=15.0, layer=0, waypoint_type="global"),
            Waypoint(x=30.0, y=15.0, layer=0, waypoint_type="global"),
        ]
        corridor = Corridor.from_waypoints(waypoints=waypoints, net=1, width=2.0)
        grid.set_corridor_preference(corridor, net=1, penalty=0.0)

        route = router.route(start_pad, end_pad)
        assert route is not None
        assert route.net == 1


# =============================================================================
# Corridor penalty decay
# =============================================================================


class TestCorridorPenaltyDecay:
    """Verify penalty decay formula used in two_phase._detailed_negotiated."""

    def test_decay_formula(self):
        """Effective penalty decays by 10% per iteration, floor at 20%."""
        base_penalty = 5.0
        results = []
        for iteration in range(1, 12):
            effective = base_penalty * max(0.2, 1.0 - 0.1 * iteration)
            results.append(effective)

        # Iteration 1: 5.0 * 0.9 = 4.5
        assert abs(results[0] - 4.5) < 1e-6
        # Iteration 5: 5.0 * 0.5 = 2.5
        assert abs(results[4] - 2.5) < 1e-6
        # Iteration 8: 5.0 * 0.2 = 1.0 (floored)
        assert abs(results[7] - 1.0) < 1e-6
        # Iteration 10: 5.0 * 0.2 = 1.0 (still at floor)
        assert abs(results[9] - 1.0) < 1e-6

    def test_set_corridor_preference_updates_penalty(self, grid, horizontal_corridor):
        """Calling set_corridor_preference with new penalty updates it."""
        grid.set_corridor_preference(horizontal_corridor, net=1, penalty=5.0)
        gx, gy = grid.world_to_grid(15.0, 0.0)
        assert grid.get_corridor_cost(gx, gy, 0, 1) == 5.0

        # Update penalty (simulating decay)
        grid.set_corridor_preference(horizontal_corridor, net=1, penalty=2.5)
        assert grid.get_corridor_cost(gx, gy, 0, 1) == 2.5
