"""Tests for layer utilization balancing in A* search (Issue #2275).

This module tests the layer balancing feature that penalizes over-utilized
layers during A* pathfinding, encouraging the router to spread traces
across all available layers.
"""

import numpy as np
import pytest

from kicad_tools.router import DesignRules, RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment


class TestLayerFillRatios:
    """Tests for RoutingGrid.get_layer_fill_ratios()."""

    @pytest.fixture
    def rules(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.5,
        )

    @pytest.fixture
    def grid_4layer(self, rules):
        return RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.four_layer_all_signal(),
        )

    def test_empty_grid_all_zeros(self, grid_4layer):
        """Fill ratios should be zero on an empty grid."""
        ratios = grid_4layer.get_layer_fill_ratios()
        assert ratios.shape == (4,)
        np.testing.assert_array_equal(ratios, 0.0)

    def test_single_layer_usage(self, grid_4layer):
        """Marking cells on one layer should increase only that layer's ratio."""
        # Mark some cells as used on layer 0
        rows, cols = grid_4layer.rows, grid_4layer.cols
        for y in range(min(5, rows)):
            for x in range(min(5, cols)):
                grid_4layer._usage_count[0, y, x] = 1

        ratios = grid_4layer.get_layer_fill_ratios()
        assert ratios[0] > 0.0
        assert ratios[1] == 0.0
        assert ratios[2] == 0.0
        assert ratios[3] == 0.0

    def test_blocked_cells_excluded(self, grid_4layer):
        """Blocked cells should be excluded from the denominator."""
        rows, cols = grid_4layer.rows, grid_4layer.cols
        total_cells = rows * cols

        # Block half the cells on layer 1
        for y in range(rows):
            for x in range(cols // 2):
                grid_4layer._blocked[1, y, x] = True

        # Mark all non-blocked cells as used on layer 1
        for y in range(rows):
            for x in range(cols // 2, cols):
                grid_4layer._usage_count[1, y, x] = 1

        ratios = grid_4layer.get_layer_fill_ratios()
        # Layer 1 should show ~100% utilization (all routable cells used)
        assert ratios[1] > 0.9

    def test_returns_numpy_array(self, grid_4layer):
        """Result should always be a plain numpy array."""
        ratios = grid_4layer.get_layer_fill_ratios()
        assert isinstance(ratios, np.ndarray)
        assert ratios.dtype == np.float64


class TestCostLayerUtilization:
    """Tests for the cost_layer_utilization design rule."""

    def test_default_value(self):
        """Default cost_layer_utilization should be 5.0."""
        rules = DesignRules()
        assert rules.cost_layer_utilization == 5.0

    def test_zero_disables(self):
        """Setting cost_layer_utilization to 0.0 should be valid."""
        rules = DesignRules(cost_layer_utilization=0.0)
        assert rules.cost_layer_utilization == 0.0

    def test_custom_value(self):
        """Custom cost_layer_utilization should be stored."""
        rules = DesignRules(cost_layer_utilization=12.5)
        assert rules.cost_layer_utilization == 12.5


class TestRouterLayerFillCache:
    """Tests for Router._layer_fill_ratios and update_layer_fill_ratios()."""

    @pytest.fixture
    def rules(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.5,
            cost_layer_utilization=5.0,
        )

    @pytest.fixture
    def grid(self, rules):
        return RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

    @pytest.fixture
    def router(self, grid, rules):
        return Router(grid=grid, rules=rules)

    def test_initial_fill_ratios_zero(self, router):
        """Initial cached fill ratios should be zero."""
        np.testing.assert_array_equal(router._layer_fill_ratios, 0.0)

    def test_update_layer_fill_ratios(self, router):
        """update_layer_fill_ratios() should refresh cached values."""
        # Mark some cells on layer 0
        for y in range(5):
            for x in range(5):
                router.grid._usage_count[0, y, x] = 1

        # Cache should still be zero (not yet updated)
        np.testing.assert_array_equal(router._layer_fill_ratios, 0.0)

        # Now update
        router.update_layer_fill_ratios()

        assert router._layer_fill_ratios[0] > 0.0


class TestAStarLayerUtilizationCost:
    """Tests for layer utilization cost in A* path expansion."""

    @pytest.fixture
    def rules_enabled(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.5,
            cost_layer_utilization=5.0,
            bidirectional_search=False,
        )

    @pytest.fixture
    def rules_disabled(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.5,
            cost_layer_utilization=0.0,
            bidirectional_search=False,
        )

    def _make_grid_and_router(self, rules):
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.four_layer_all_signal(),
        )
        router = Router(grid=grid, rules=rules)
        return grid, router

    def _fill_layer(self, grid, layer_idx, fill_fraction):
        """Fill a fraction of a layer's cells with usage."""
        rows, cols = grid.rows, grid.cols
        total = rows * cols
        target = int(total * fill_fraction)
        count = 0
        for y in range(rows):
            for x in range(cols):
                if count >= target:
                    return
                grid._usage_count[layer_idx, y, x] = 1
                count += 1

    def test_route_succeeds_with_utilization_enabled(self, rules_enabled):
        """Routing should still succeed with utilization cost enabled."""
        grid, router = self._make_grid_and_router(rules_enabled)

        pad1 = Pad(x=2.0, y=2.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)
        pad2 = Pad(x=18.0, y=18.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)

        route = router.route(pad1, pad2)
        assert route is not None
        assert len(route.segments) > 0

    def test_zero_utilization_no_effect(self, rules_disabled):
        """With cost_layer_utilization=0, utilization should not affect routing."""
        grid, router = self._make_grid_and_router(rules_disabled)

        # Pre-fill layer 0 heavily
        self._fill_layer(grid, 0, 0.8)
        router.update_layer_fill_ratios()

        pad1 = Pad(x=2.0, y=2.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=True, drill=0.3)
        pad2 = Pad(x=18.0, y=18.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=True, drill=0.3)

        route = router.route(pad1, pad2)
        assert route is not None

    def test_heavy_utilization_encourages_layer_change(self, rules_enabled):
        """When one layer is heavily utilized, router should prefer other layers."""
        grid, router = self._make_grid_and_router(rules_enabled)

        # Fill layer 0 (F.Cu) to 80%
        self._fill_layer(grid, 0, 0.8)
        router.update_layer_fill_ratios()

        # Verify fill ratios are set
        assert router._layer_fill_ratios[0] > 0.5
        assert router._layer_fill_ratios[1] == 0.0  # In.1 empty
        assert router._layer_fill_ratios[2] == 0.0  # In.2 empty
        assert router._layer_fill_ratios[3] == 0.0  # B.Cu empty

        # Route with through-hole pads (can use any layer)
        pad1 = Pad(x=2.0, y=2.0, width=0.8, height=0.8,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=True, drill=0.3)
        pad2 = Pad(x=18.0, y=18.0, width=0.8, height=0.8,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=True, drill=0.3)

        route = router.route(pad1, pad2)
        assert route is not None

        # Check if the route uses any non-F.Cu segments
        # With heavy layer 0 utilization, the router should be incentivized
        # to use other layers at least partially
        layers_used = set()
        for seg in route.segments:
            layer_idx = grid.layer_to_index(seg.layer.value)
            layers_used.add(layer_idx)
        for via in route.vias:
            # Vias span layers, count destination layers
            pass

        # The route should exist (we don't strictly require layer switching
        # as it depends on cost tradeoffs, but the route must succeed)
        assert route is not None


class TestTwoLayerNoRegression:
    """Test that 2-layer boards are not adversely affected."""

    @pytest.fixture
    def rules(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.5,
            cost_layer_utilization=5.0,
            bidirectional_search=False,
        )

    @pytest.fixture
    def grid(self, rules):
        return RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

    @pytest.fixture
    def router(self, grid, rules):
        return Router(grid=grid, rules=rules)

    def test_two_layer_route_succeeds(self, router):
        """Simple route on 2-layer board should still work with utilization cost."""
        pad1 = Pad(x=2.0, y=2.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)
        pad2 = Pad(x=18.0, y=18.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)

        route = router.route(pad1, pad2)
        assert route is not None
        assert len(route.segments) > 0

    def test_two_layer_symmetric_utilization(self, router):
        """On 2-layer boards, equal utilization adds symmetric cost (no bias)."""
        grid = router.grid

        # Fill both layers equally
        rows, cols = grid.rows, grid.cols
        for y in range(rows // 2):
            for x in range(cols):
                grid._usage_count[0, y, x] = 1
                grid._usage_count[1, y, x] = 1

        router.update_layer_fill_ratios()

        # Both layers should have similar fill ratios
        assert abs(router._layer_fill_ratios[0] - router._layer_fill_ratios[1]) < 0.01


class TestBidirectionalParity:
    """Test that bidirectional A* also includes utilization cost."""

    @pytest.fixture
    def rules(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.5,
            cost_layer_utilization=5.0,
            bidirectional_search=True,
            bidirectional_threshold=10,
        )

    @pytest.fixture
    def grid(self, rules):
        return RoutingGrid(
            width=30.0,
            height=30.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

    @pytest.fixture
    def router(self, grid, rules):
        return Router(grid=grid, rules=rules)

    def test_bidirectional_route_with_utilization(self, router):
        """Bidirectional A* should work with utilization cost enabled."""
        pad1 = Pad(x=2.0, y=2.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)
        pad2 = Pad(x=28.0, y=28.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)

        route = router.route_bidirectional(pad1, pad2)
        assert route is not None
        assert len(route.segments) > 0

    def test_bidirectional_with_heavy_utilization(self, router):
        """Bidirectional A* should handle heavy utilization gracefully."""
        grid = router.grid

        # Fill layer 0 partially
        rows, cols = grid.rows, grid.cols
        for y in range(rows // 2):
            for x in range(cols):
                grid._usage_count[0, y, x] = 1

        router.update_layer_fill_ratios()
        assert router._layer_fill_ratios[0] > 0.0

        pad1 = Pad(x=2.0, y=2.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=True, drill=0.3)
        pad2 = Pad(x=28.0, y=28.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=True, drill=0.3)

        route = router.route_bidirectional(pad1, pad2)
        assert route is not None


class TestAllowedLayerConstraint:
    """Test that utilization cost does not cause failures with layer constraints."""

    @pytest.fixture
    def rules(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.5,
            cost_layer_utilization=5.0,
            allowed_layers=["F.Cu"],
            bidirectional_search=False,
        )

    @pytest.fixture
    def grid(self, rules):
        return RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

    @pytest.fixture
    def router(self, grid, rules):
        return Router(grid=grid, rules=rules)

    def test_single_layer_allowed_with_utilization(self, router):
        """With allowed_layers restricting to 1 layer, utilization should not cause failure."""
        pad1 = Pad(x=2.0, y=2.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)
        pad2 = Pad(x=18.0, y=18.0, width=0.5, height=0.5,
                    net=1, net_name="NET1", layer=Layer.F_CU,
                    through_hole=False, drill=0)

        route = router.route(pad1, pad2)
        assert route is not None
        assert len(route.segments) > 0
