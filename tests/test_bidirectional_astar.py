"""Tests for bidirectional A* search (Issue #964).

This module tests the bidirectional A* implementation in the router pathfinder,
which provides parallel frontier exploration for improved performance on large paths.
"""

import pytest

from kicad_tools.router import DesignRules, RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad


class TestBidirectionalAStarBasic:
    """Basic tests for bidirectional A* routing."""

    @pytest.fixture
    def rules(self):
        """Create design rules for testing."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
            bidirectional_search=True,
            bidirectional_threshold=10,  # Low threshold for testing
        )

    @pytest.fixture
    def grid(self, rules):
        """Create a routing grid for testing."""
        return RoutingGrid(
            width=50.0,
            height=40.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

    @pytest.fixture
    def router(self, grid, rules):
        """Create a router for testing."""
        return Router(grid=grid, rules=rules)

    def test_bidirectional_simple_route(self, router):
        """Test basic bidirectional routing between two pads."""
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
            x=45.0,
            y=35.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        # Use bidirectional routing
        route = router.route_bidirectional(pad1, pad2)

        assert route is not None
        assert len(route.segments) > 0
        assert route.net == 1
        assert route.net_name == "NET1"

    def test_bidirectional_same_as_standard(self, router):
        """Test that bidirectional produces valid routes like standard A*."""
        pad1 = Pad(
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
        pad2 = Pad(
            x=40.0,
            y=30.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        # Route using both methods
        route_standard = router.route(pad1, pad2)
        route_bidi = router.route_bidirectional(pad1, pad2)

        # Both should find routes
        assert route_standard is not None
        assert route_bidi is not None

        # Both should have valid segments
        assert len(route_standard.segments) > 0
        assert len(route_bidi.segments) > 0

    def test_bidirectional_short_route(self, router):
        """Test bidirectional routing for short distances."""
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
            x=10.0,
            y=5.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        route = router.route_bidirectional(pad1, pad2)

        assert route is not None
        assert len(route.segments) >= 1


class TestRouteAuto:
    """Tests for automatic algorithm selection."""

    @pytest.fixture
    def rules_with_high_threshold(self):
        """Create rules with high threshold to force standard A*."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
            bidirectional_search=True,
            bidirectional_threshold=10000,  # Very high, forces standard A*
        )

    @pytest.fixture
    def rules_with_low_threshold(self):
        """Create rules with low threshold to force bidirectional A*."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
            bidirectional_search=True,
            bidirectional_threshold=5,  # Very low, forces bidirectional
        )

    @pytest.fixture
    def rules_disabled(self):
        """Create rules with bidirectional search disabled."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
            bidirectional_search=False,
        )

    def test_route_auto_uses_standard_for_short_paths(self, rules_with_high_threshold):
        """Test that route_auto uses standard A* for short paths."""
        grid = RoutingGrid(
            width=30.0,
            height=25.0,
            rules=rules_with_high_threshold,
            layer_stack=LayerStack.two_layer(),
        )
        router = Router(grid=grid, rules=rules_with_high_threshold)

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
            x=25.0,
            y=20.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        # route_auto should work regardless of which algorithm is selected
        route = router.route_auto(pad1, pad2)
        assert route is not None
        assert len(route.segments) > 0

    def test_route_auto_uses_bidirectional_for_long_paths(self, rules_with_low_threshold):
        """Test that route_auto uses bidirectional A* for long paths."""
        grid = RoutingGrid(
            width=100.0,
            height=80.0,
            rules=rules_with_low_threshold,
            layer_stack=LayerStack.two_layer(),
        )
        router = Router(grid=grid, rules=rules_with_low_threshold)

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
            x=95.0,
            y=75.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        # route_auto should work regardless of which algorithm is selected
        route = router.route_auto(pad1, pad2)
        assert route is not None
        assert len(route.segments) > 0

    def test_route_auto_disabled(self, rules_disabled):
        """Test that route_auto falls back to standard when disabled."""
        grid = RoutingGrid(
            width=30.0,
            height=25.0,
            rules=rules_disabled,
            layer_stack=LayerStack.two_layer(),
        )
        router = Router(grid=grid, rules=rules_disabled)

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
            x=25.0,
            y=20.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        route = router.route_auto(pad1, pad2)
        assert route is not None
        assert len(route.segments) > 0


class TestBidirectionalWithObstacles:
    """Tests for bidirectional A* with obstacles."""

    @pytest.fixture
    def rules(self):
        """Create design rules for testing."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.2,
            bidirectional_search=True,
            bidirectional_threshold=10,
        )

    def test_bidirectional_with_blocking_pad(self, rules):
        """Test bidirectional routing around obstacles."""
        grid = RoutingGrid(
            width=40.0,
            height=30.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )

        # Add a blocking pad in the middle
        blocking_pad = Pad(
            x=20.0,
            y=15.0,
            width=5.0,
            height=5.0,
            net=2,  # Different net
            net_name="GND",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
        )
        grid.add_pad(blocking_pad)

        router = Router(grid=grid, rules=rules)

        pad1 = Pad(
            x=5.0,
            y=15.0,
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
            y=15.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=False,
            drill=0,
        )

        # Should find a route around the obstacle
        route = router.route_bidirectional(pad1, pad2)

        assert route is not None
        assert len(route.segments) > 0


class TestBidirectionalWithLayerChange:
    """Tests for bidirectional A* with layer changes."""

    @pytest.fixture
    def rules(self):
        """Create design rules for testing."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.2,
            cost_via=5.0,  # Reasonable via cost
            bidirectional_search=True,
            bidirectional_threshold=10,
        )

    def test_bidirectional_different_layers(self, rules):
        """Test bidirectional routing between pads on different layers."""
        grid = RoutingGrid(
            width=30.0,
            height=25.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        router = Router(grid=grid, rules=rules)

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
            x=25.0,
            y=20.0,
            width=0.5,
            height=0.5,
            net=1,
            net_name="NET1",
            layer=Layer.B_CU,  # Different layer
            through_hole=False,
            drill=0,
        )

        route = router.route_bidirectional(pad1, pad2)

        assert route is not None
        # Should have at least one via for layer change
        assert len(route.vias) >= 1

    def test_bidirectional_pth_pads(self, rules):
        """Test bidirectional routing with through-hole pads."""
        grid = RoutingGrid(
            width=30.0,
            height=25.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        router = Router(grid=grid, rules=rules)

        pad1 = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.8,
        )
        pad2 = Pad(
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

        route = router.route_bidirectional(pad1, pad2)

        assert route is not None
        assert len(route.segments) > 0


class TestBidirectionalConfiguration:
    """Tests for bidirectional A* configuration."""

    def test_default_config_values(self):
        """Test default configuration values."""
        rules = DesignRules()

        assert rules.bidirectional_search is True
        assert rules.bidirectional_threshold == 1000
        assert rules.parallel_workers == 2

    def test_custom_config_values(self):
        """Test custom configuration values."""
        rules = DesignRules(
            bidirectional_search=False,
            bidirectional_threshold=500,
            parallel_workers=4,
        )

        assert rules.bidirectional_search is False
        assert rules.bidirectional_threshold == 500
        assert rules.parallel_workers == 4


class TestRouteAutoFallbackPlumbing:
    """Issue #6527: route_auto as a drop-in for the C++ Python fallback."""

    @staticmethod
    def _make(threshold):
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
            bidirectional_search=True,
            bidirectional_threshold=threshold,
        )
        grid = RoutingGrid(width=50.0, height=40.0, rules=rules, layer_stack=LayerStack.two_layer())
        return Router(grid=grid, rules=rules)

    @staticmethod
    def _pads(x2=45.0):
        def mk(x, y):
            return Pad(
                x=x,
                y=y,
                width=0.5,
                height=0.5,
                net=1,
                net_name="N1",
                layer=Layer.F_CU,
                through_hole=False,
                drill=0,
            )

        return mk(5.0, 5.0), mk(x2, 5.0)

    def test_flag_default_off(self):
        assert DesignRules().fallback_bidirectional is False

    def test_steiner_goals_never_ignored(self, monkeypatch):
        """With extra goals the bidirectional half declines; one-way gets them."""
        router = self._make(10)
        a, b = self._pads()
        goals = {(100, 50, 0)}
        seen = {}
        orig = router.route

        def spy(*args, **kw):
            seen["goals"] = kw.get("extra_goal_cells")
            return orig(*args, **kw)

        monkeypatch.setattr(router, "route", spy)
        monkeypatch.setattr(
            router,
            "_expand_bidirectional_neighbors",
            lambda *a, **k: pytest.fail("bidirectional must not run with goals"),
        )
        router.route_auto(a, b, extra_goal_cells=goals)
        assert seen["goals"] == goals
        assert router.route_bidirectional(a, b, extra_goal_cells=goals) is None
        assert router.last_route_exhausted is False

    def test_bidirectional_success_status(self):
        router = self._make(10)
        a, b = self._pads()
        assert router.route_auto(a, b) is not None
        assert router.last_route_exhausted is False
        assert router.last_route_deadline_hit is False

    def test_bidirectional_exhausted_status(self):
        router = self._make(10)
        a, b = self._pads()
        # Wall off the end pad's row on every layer so the search drains.
        for layer in range(router.grid.num_layers):
            for gy in range(router.grid.rows):
                router.grid.grid[layer][gy][router.grid.world_to_grid(25.0, 5.0)[0]].blocked = True
        assert router.route_bidirectional(a, b) is None
        assert router.last_route_exhausted is True
        assert router.last_route_deadline_hit is False

    def test_deadline_hit_in_bidirectional_half(self, monkeypatch):
        """Expired budget: the bidirectional half hits the deadline; no retry."""
        router = self._make(10)
        a, b = self._pads()
        called = []
        monkeypatch.setattr(router, "route", lambda *a, **k: called.append(k) or None)
        monkeypatch.setattr(
            "kicad_tools.router.pathfinder._wall_deadline.clamp_search_timeout",
            lambda t: t,
        )
        for layer in range(router.grid.num_layers):
            for gy in range(router.grid.rows):
                router.grid.grid[layer][gy][router.grid.world_to_grid(25.0, 5.0)[0]].blocked = True
        import itertools

        import kicad_tools.router.pathfinder as pf

        ticks = itertools.count()
        monkeypatch.setattr(pf.time, "monotonic", lambda: float(next(ticks)))
        assert router.route_auto(a, b, per_net_timeout=0.5) is None
        assert router.last_route_deadline_hit is True
        assert router.last_route_exhausted is False
        assert called == []

    def test_failed_bidirectional_and_retry_share_one_deadline(self, monkeypatch):
        router = self._make(10)
        a, b = self._pads()
        got = {}
        monkeypatch.setattr(router, "route_bidirectional", lambda *a, **k: None)
        monkeypatch.setattr(router, "route", lambda *a, **k: got.update(k) or None)
        import kicad_tools.router.pathfinder as pf

        clock = iter([100.0, 103.0, 103.0, 103.0])
        monkeypatch.setattr(pf.time, "monotonic", lambda: next(clock))
        router.route_auto(a, b, per_net_timeout=10.0)
        assert got["per_net_timeout"] == pytest.approx(7.0)

    def test_below_threshold_keeps_one_way(self, monkeypatch):
        router = self._make(10000)
        a, b = self._pads()
        monkeypatch.setattr(
            router,
            "route_bidirectional",
            lambda *a, **k: pytest.fail("below threshold must stay one-way"),
        )
        assert router.route_auto(a, b, per_net_timeout=30.0) is not None
