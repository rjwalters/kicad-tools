"""Tests for via placement optimization (Issue #1019).

This module tests the via impact scoring and exclusion zone features that
help avoid blocking adjacent nets when routing near fine-pitch ICs.
"""

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


class TestViaImpactScoring:
    """Tests for via impact scoring functionality."""

    def test_default_via_impact_disabled(self):
        """Test that via impact scoring is disabled by default."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # Default weight is 1.0, so it should be enabled
        assert router._via_impact_enabled is True

    def test_via_impact_disabled_when_weight_zero(self):
        """Test that via impact scoring can be disabled with weight=0."""
        rules = DesignRules(via_impact_weight=0.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        assert router._via_impact_enabled is False

    def test_set_unrouted_pads(self):
        """Test setting unrouted pads for impact scoring."""
        rules = DesignRules(via_impact_weight=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # Create some test pads
        pad1 = Pad(x=1.0, y=1.0, width=0.5, height=0.5, net=1, net_name="NET1")
        pad2 = Pad(x=2.0, y=2.0, width=0.5, height=0.5, net=2, net_name="NET2")
        pad3 = Pad(x=3.0, y=3.0, width=0.5, height=0.5, net=3, net_name="NET3")

        router.set_unrouted_pads([pad1, pad2, pad3])

        assert len(router._unrouted_pad_positions) == 3
        assert (1.0, 1.0, 1) in router._unrouted_pad_positions
        assert (2.0, 2.0, 2) in router._unrouted_pad_positions
        assert (3.0, 3.0, 3) in router._unrouted_pad_positions

    def test_via_impact_cost_no_blocking(self):
        """Test that via impact cost is 0 when no pads are blocked."""
        rules = DesignRules(via_impact_weight=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # Create a pad far from the via position
        pad = Pad(x=9.0, y=9.0, width=0.5, height=0.5, net=2, net_name="NET2")
        router.set_unrouted_pads([pad])

        # Via at (1.0, 1.0) should not impact pad at (9.0, 9.0)
        cost = router._get_via_impact_cost(1.0, 1.0, current_net=1)
        assert cost == 0.0

    def test_via_impact_cost_blocking(self):
        """Test that via impact cost is positive when pads would be blocked."""
        rules = DesignRules(via_impact_weight=1.0, via_diameter=0.7, via_clearance=0.2)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # Create a pad very close to the via position
        # Via blocks at via_radius + trace_clearance + trace_width/2 = 0.35 + 0.2 + 0.1 = 0.65mm
        pad = Pad(x=1.5, y=1.0, width=0.5, height=0.5, net=2, net_name="NET2")
        router.set_unrouted_pads([pad])

        # Via at (1.0, 1.0) should block pad at (1.5, 1.0) - only 0.5mm away
        cost = router._get_via_impact_cost(1.0, 1.0, current_net=1)
        assert cost > 0.0

    def test_via_impact_cost_same_net_excluded(self):
        """Test that pads on the same net are excluded from impact calculation."""
        rules = DesignRules(via_impact_weight=1.0, via_diameter=0.7, via_clearance=0.2)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # Create a pad very close to the via position, but on the SAME net
        pad = Pad(x=1.5, y=1.0, width=0.5, height=0.5, net=1, net_name="NET1")
        router.set_unrouted_pads([pad])

        # Via at (1.0, 1.0) should NOT impact pad on same net
        cost = router._get_via_impact_cost(1.0, 1.0, current_net=1)
        assert cost == 0.0

    def test_via_impact_weight_scales_cost(self):
        """Test that via_impact_weight scales the impact cost."""
        # High weight
        rules_high = DesignRules(via_impact_weight=5.0, via_diameter=0.7, via_clearance=0.2)
        grid_high = RoutingGrid(10.0, 10.0, rules_high)
        router_high = Router(grid_high, rules_high)

        # Low weight
        rules_low = DesignRules(via_impact_weight=1.0, via_diameter=0.7, via_clearance=0.2)
        grid_low = RoutingGrid(10.0, 10.0, rules_low)
        router_low = Router(grid_low, rules_low)

        # Same pad setup
        pad = Pad(x=1.5, y=1.0, width=0.5, height=0.5, net=2, net_name="NET2")
        router_high.set_unrouted_pads([pad])
        router_low.set_unrouted_pads([pad])

        cost_high = router_high._get_via_impact_cost(1.0, 1.0, current_net=1)
        cost_low = router_low._get_via_impact_cost(1.0, 1.0, current_net=1)

        assert cost_high == cost_low * 5.0


class TestViaExclusionZone:
    """Tests for via exclusion zone functionality."""

    def test_exclusion_zone_disabled_by_default(self):
        """Test that via exclusion zone is disabled when distance is 0."""
        rules = DesignRules(via_exclusion_from_fine_pitch=0.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        assert router._via_exclusion_cells == 0

    def test_exclusion_zone_enabled(self):
        """Test that exclusion zone is calculated when distance > 0."""
        rules = DesignRules(via_exclusion_from_fine_pitch=1.5, grid_resolution=0.1)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # 1.5mm / 0.1mm = 15 cells
        assert router._via_exclusion_cells == 15

    def test_via_not_in_exclusion_zone_no_fine_pitch(self):
        """Test that via is allowed when no fine-pitch pads exist."""
        rules = DesignRules(via_exclusion_from_fine_pitch=1.5, grid_resolution=0.1)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # No fine-pitch pads registered
        gx, gy = grid.world_to_grid(5.0, 5.0)
        assert router._is_via_in_exclusion_zone(gx, gy) is False

    def test_via_in_exclusion_zone(self):
        """Test that via is blocked when too close to fine-pitch pad."""
        rules = DesignRules(
            via_exclusion_from_fine_pitch=1.5,
            grid_resolution=0.1,
            fine_pitch_threshold=0.8,
        )
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # Create a fine-pitch pad (pitch < 0.8mm)
        pad = Pad(x=5.0, y=5.0, width=0.3, height=0.5, net=1, net_name="NET1", ref="U1", pin="1")
        # Add another pad to the same component to establish pitch
        pad2 = Pad(x=5.65, y=5.0, width=0.3, height=0.5, net=2, net_name="NET2", ref="U1", pin="2")
        grid.add_pad(pad)
        grid.add_pad(pad2)

        # Set up unrouted pads to trigger fine-pitch detection
        router.set_unrouted_pads([pad, pad2])

        # Via at (5.5, 5.0) is 0.5mm from pad at (5.0, 5.0), within 1.5mm exclusion
        gx, gy = grid.world_to_grid(5.5, 5.0)
        assert router._is_via_in_exclusion_zone(gx, gy) is True

    def test_via_outside_exclusion_zone(self):
        """Test that via is allowed when far from fine-pitch pads."""
        rules = DesignRules(
            via_exclusion_from_fine_pitch=1.0,
            grid_resolution=0.1,
            fine_pitch_threshold=0.8,
        )
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules)

        # Create a fine-pitch pad
        pad = Pad(x=2.0, y=2.0, width=0.3, height=0.5, net=1, net_name="NET1", ref="U1", pin="1")
        pad2 = Pad(x=2.65, y=2.0, width=0.3, height=0.5, net=2, net_name="NET2", ref="U1", pin="2")
        grid.add_pad(pad)
        grid.add_pad(pad2)

        router.set_unrouted_pads([pad, pad2])

        # Via at (8.0, 8.0) is far from pad at (2.0, 2.0), outside 1.0mm exclusion
        gx, gy = grid.world_to_grid(8.0, 8.0)
        assert router._is_via_in_exclusion_zone(gx, gy) is False


class TestDesignRulesViaParams:
    """Tests for new DesignRules via-related parameters."""

    def test_via_exclusion_default(self):
        """Test default value for via_exclusion_from_fine_pitch."""
        rules = DesignRules()
        assert rules.via_exclusion_from_fine_pitch == 0.0

    def test_via_impact_weight_default(self):
        """Test default value for via_impact_weight."""
        rules = DesignRules()
        assert rules.via_impact_weight == 1.0

    def test_via_exclusion_custom(self):
        """Test custom via_exclusion_from_fine_pitch value."""
        rules = DesignRules(via_exclusion_from_fine_pitch=2.0)
        assert rules.via_exclusion_from_fine_pitch == 2.0

    def test_via_impact_weight_custom(self):
        """Test custom via_impact_weight value."""
        rules = DesignRules(via_impact_weight=3.5)
        assert rules.via_impact_weight == 3.5
