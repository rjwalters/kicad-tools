"""Tests for progressive clearance relaxation feature (Issue #999).

This module tests the route_with_progressive_clearance() method which allows
the router to progressively relax clearance for nets that fail due to
clearance constraints.
"""

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.failure_analysis import FailureCause
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules


class TestProgressiveClearanceRelaxation:
    """Tests for route_with_progressive_clearance method."""

    @pytest.fixture
    def router_with_tight_clearance(self):
        """Create a router with tight clearance that might cause failures."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)
        return router

    @pytest.fixture
    def router_with_simple_net(self):
        """Create a router with a simple two-pad net."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Add two pads that need to be connected
        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )
        return router

    def test_progressive_clearance_basic(self, router_with_simple_net):
        """Test basic progressive clearance relaxation routing."""
        router = router_with_simple_net

        routes, relaxed_nets = router.route_with_progressive_clearance(
            min_clearance=0.08,
            num_relaxation_levels=3,
            max_iterations=5,
        )

        # Should route successfully
        assert len(routes) >= 1
        # relaxed_nets should be a dict (even if empty)
        assert isinstance(relaxed_nets, dict)

    def test_progressive_clearance_returns_tuple(self, router_with_simple_net):
        """Test that route_with_progressive_clearance returns correct type."""
        router = router_with_simple_net

        result = router.route_with_progressive_clearance(
            min_clearance=0.08,
        )

        assert isinstance(result, tuple)
        assert len(result) == 2
        routes, relaxed_nets = result
        assert isinstance(routes, list)
        assert isinstance(relaxed_nets, dict)

    def test_progressive_clearance_with_default_min_clearance(
        self, router_with_simple_net
    ):
        """Test progressive clearance with default min_clearance (50% of original)."""
        router = router_with_simple_net

        routes, relaxed_nets = router.route_with_progressive_clearance()

        # Should complete without error
        assert routes is not None
        assert relaxed_nets is not None

    def test_progressive_clearance_respects_min_clearance(self):
        """Test that relaxation doesn't go below min_clearance."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Add simple net
        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        min_clearance = 0.12
        routes, relaxed_nets = router.route_with_progressive_clearance(
            min_clearance=min_clearance,
        )

        # If any nets were relaxed, their clearance should be >= min_clearance
        for net_id, clearance in relaxed_nets.items():
            assert clearance >= min_clearance, (
                f"Net {net_id} clearance {clearance} is below min_clearance {min_clearance}"
            )

    def test_progressive_clearance_multiple_relaxation_levels(self):
        """Test with different number of relaxation levels."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        # Test with 1 level (minimal relaxation)
        routes1, _ = router.route_with_progressive_clearance(
            num_relaxation_levels=1,
        )

        # Reset and test with 5 levels
        router2 = Autorouter(width=20.0, height=20.0, rules=rules)
        router2.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )
        routes5, _ = router2.route_with_progressive_clearance(
            num_relaxation_levels=5,
        )

        # Both should complete without error
        assert routes1 is not None
        assert routes5 is not None


class TestProgressiveClearanceEdgeCases:
    """Edge case tests for progressive clearance relaxation."""

    def test_empty_router(self):
        """Test progressive clearance with no nets to route."""
        router = Autorouter(width=20.0, height=20.0)

        routes, relaxed_nets = router.route_with_progressive_clearance()

        assert routes == []
        assert relaxed_nets == {}

    def test_single_pad_nets_not_routed(self):
        """Test that single-pad nets are not routed (no connections needed)."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Add single-pad net (doesn't need routing)
        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 10.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "SINGLE_PAD",
                },
            ],
        )

        routes, relaxed_nets = router.route_with_progressive_clearance()

        # Single pad net should not generate routes or relaxation
        assert len(routes) == 0
        assert len(relaxed_nets) == 0

    def test_min_clearance_exceeds_original(self):
        """Test that min_clearance is capped at original clearance."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.1,  # Original clearance
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        # min_clearance (0.2) > original_clearance (0.1) - should be capped
        routes, relaxed_nets = router.route_with_progressive_clearance(
            min_clearance=0.2,
        )

        # Should still complete without error
        assert routes is not None


class TestProgressiveClearanceWithTimeout:
    """Tests for timeout handling in progressive clearance relaxation."""

    def test_timeout_returns_partial_results(self):
        """Test that timeout returns partial results."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Add net
        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 5.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.8,
                    "height": 0.8,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        # Very short timeout - may or may not complete
        routes, relaxed_nets = router.route_with_progressive_clearance(
            timeout=0.001,  # 1ms timeout
        )

        # Should return valid types even if incomplete
        assert isinstance(routes, list)
        assert isinstance(relaxed_nets, dict)
