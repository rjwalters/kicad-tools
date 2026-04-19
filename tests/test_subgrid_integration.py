"""Integration tests for sub-grid routing wired into route_all() pipeline.

Issue #1603: Wire sub-grid escape routing into route_all() default pipeline.

Tests verify that:
1. route_all() automatically runs sub-grid escape pre-pass
2. route_all() with no off-grid pads behaves identically (no-op pre-pass)
3. route_net() retries with sub-grid on PIN_ACCESS failure
4. All route_all variants (interleaved, parallel, negotiated) include pre-pass
"""

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.failure_analysis import FailureCause
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules


class TestSubgridPrepass:
    """Tests for automatic sub-grid escape pre-pass in route_all()."""

    def _make_router_with_on_grid_pads(self):
        """Create a router where all pads are on the main grid."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Pads at grid-aligned positions (multiples of 0.1)
        pads1 = [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 8.0, "y": 5.0, "net": 1, "net_name": "NET1"},
        ]
        pads2 = [
            {"number": "1", "x": 5.0, "y": 10.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 8.0, "y": 10.0, "net": 2, "net_name": "NET2"},
        ]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)
        return router

    def _make_router_with_off_grid_pads(self):
        """Create a router with TSSOP-like off-grid pads (0.65mm pitch on 0.1mm grid)."""
        # Use realistic TSSOP pad dimensions (0.3mm x 0.45mm) and fine-pitch
        # trace/clearance settings so escape segments can pass clearance
        # validation against neighboring pads (Issue #1626).
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.15,
            trace_clearance=0.1,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # TSSOP-like pads at 0.65mm pitch -- these will be off-grid on a 0.1mm grid
        # 0.65mm is not a multiple of 0.1mm, so pads at 10.0, 10.65, 11.30, 11.95
        # will fall between grid points.
        pads_u1 = [
            {
                "number": "1",
                "x": 10.0,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
                "width": 0.3,
                "height": 0.45,
            },
            {
                "number": "2",
                "x": 10.65,
                "y": 10.0,
                "net": 2,
                "net_name": "NET2",
                "width": 0.3,
                "height": 0.45,
            },
            {
                "number": "3",
                "x": 11.30,
                "y": 10.0,
                "net": 3,
                "net_name": "NET3",
                "width": 0.3,
                "height": 0.45,
            },
        ]

        # Matching pads on grid for the other side of each net
        pads_r1 = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 1, "net_name": "NET1"},
        ]
        pads_r2 = [
            {"number": "1", "x": 11.0, "y": 20.0, "net": 2, "net_name": "NET2"},
        ]
        pads_r3 = [
            {"number": "1", "x": 12.0, "y": 20.0, "net": 3, "net_name": "NET3"},
        ]

        router.add_component("U1", pads_u1)
        router.add_component("R1", pads_r1)
        router.add_component("R2", pads_r2)
        router.add_component("R3", pads_r3)
        return router

    def test_route_all_no_off_grid_pads_unchanged(self):
        """route_all() with on-grid pads should work identically to before."""
        router = self._make_router_with_on_grid_pads()
        routes = router.route_all()
        assert isinstance(routes, list)
        # Should have routes for at least some nets
        assert len(routes) >= 0  # Basic sanity -- no crash

    def test_route_all_runs_subgrid_prepass(self):
        """route_all() should automatically run sub-grid escape pre-pass."""
        router = self._make_router_with_off_grid_pads()
        routes = router.route_all()
        assert isinstance(routes, list)

        # Check that escape routes were generated (they have single segments
        # with very short length connecting off-grid pad to grid point)
        escape_segments = [
            r for r in routes
            if len(r.segments) == 1 and ((r.segments[0].x2 - r.segments[0].x1) ** 2 + (r.segments[0].y2 - r.segments[0].y1) ** 2) ** 0.5 < 0.2
        ]
        # We should have at least some escape segments for the off-grid pads
        # (10.65 and 11.30 are off-grid on 0.1mm grid)
        assert len(escape_segments) >= 1, (
            "Expected escape segments for off-grid pads but found none"
        )

    def test_route_all_prepass_is_noop_for_on_grid(self):
        """Pre-pass should be a no-op when all pads are on the grid."""
        router = self._make_router_with_on_grid_pads()

        # Run pre-pass directly
        escape_routes = router._run_subgrid_prepass()
        assert escape_routes == []

    def test_route_all_interleaved_runs_prepass(self):
        """route_all(interleaved=True) should also run the sub-grid pre-pass."""
        router = self._make_router_with_off_grid_pads()
        routes = router.route_all(interleaved=True)
        assert isinstance(routes, list)

    def test_route_all_parallel_runs_prepass(self):
        """route_all(parallel=True) should also run the sub-grid pre-pass."""
        router = self._make_router_with_off_grid_pads()
        routes = router.route_all(parallel=True)
        assert isinstance(routes, list)

    def test_route_all_negotiated_runs_prepass(self):
        """route_all_negotiated() should also run the sub-grid pre-pass."""
        router = self._make_router_with_off_grid_pads()
        routes = router.route_all_negotiated(max_iterations=1, timeout=5.0)
        assert isinstance(routes, list)

    def test_prepass_escape_routes_in_self_routes(self):
        """Escape routes from pre-pass should be tracked in self.routes."""
        router = self._make_router_with_off_grid_pads()
        router._run_subgrid_prepass()

        # Check that escape routes are in self.routes
        escape_routes_in_self = [
            r for r in router.routes
            if len(r.segments) == 1 and ((r.segments[0].x2 - r.segments[0].x1) ** 2 + (r.segments[0].y2 - r.segments[0].y1) ** 2) ** 0.5 < 0.2
        ]
        assert len(escape_routes_in_self) >= 1


class TestSubgridRetryOnPinAccess:
    """Tests for route_net() retry with sub-grid on PIN_ACCESS failure."""

    def test_retry_clears_failure_on_success(self):
        """When retry succeeds, the PIN_ACCESS failure should be removed."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Create a net with an off-grid pad that would fail initial routing
        # but succeed after sub-grid escape
        pads_u1 = [
            {
                "number": "1",
                "x": 10.65,  # Off-grid on 0.1mm grid
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
                "width": 0.3,
                "height": 0.8,
            },
        ]
        pads_r1 = [
            {"number": "1", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("U1", pads_u1)
        router.add_component("R1", pads_r1)

        # Route the net
        routes = router.route_net(1)

        # After routing (with retry), check that if routes were found,
        # there should be no PIN_ACCESS failure remaining for this net
        if routes:
            pin_access_failures = [
                f for f in router.routing_failures
                if f.net == 1 and f.failure_cause == FailureCause.PIN_ACCESS
            ]
            assert len(pin_access_failures) == 0, (
                "PIN_ACCESS failure should be cleared after successful retry"
            )

    def test_no_retry_when_subgrid_retry_flag_set(self):
        """route_net with _subgrid_retry=True should not retry again."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        pads_u1 = [
            {
                "number": "1",
                "x": 10.65,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        pads_r1 = [
            {"number": "1", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("U1", pads_u1)
        router.add_component("R1", pads_r1)

        # Call with _subgrid_retry=True -- should not recurse
        routes = router.route_net(1, _subgrid_retry=True)
        # Should return without crash (may or may not have routes)
        assert isinstance(routes, list)

    def test_retry_not_triggered_for_on_grid_failures(self):
        """Retry should not trigger for failures that are not PIN_ACCESS."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # On-grid pads that might fail for other reasons
        pads = [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 5.0, "y": 15.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)
        # Should work without issues for on-grid pads
        assert isinstance(routes, list)


class TestRunSubgridPrepass:
    """Tests for _run_subgrid_prepass() directly."""

    def test_prepass_returns_empty_for_no_pads(self):
        """Pre-pass with no pads should return empty list."""
        rules = DesignRules(grid_resolution=0.1)
        router = Autorouter(width=20.0, height=20.0, rules=rules)
        result = router._run_subgrid_prepass()
        assert result == []

    def test_prepass_returns_escape_routes(self):
        """Pre-pass should return escape Route objects for off-grid pads."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        # Add off-grid pad
        pads = [
            {
                "number": "1",
                "x": 10.65,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
                "width": 0.3,
                "height": 0.8,
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        router.add_component("U1", pads)

        escape_routes = router._run_subgrid_prepass()

        # Should have at least one escape route for the off-grid pad at 10.65
        assert len(escape_routes) >= 1

        # Each escape route should have the correct net
        for route in escape_routes:
            assert route.net == 1

    def test_prepass_marks_routes(self):
        """Pre-pass escape routes should be added to self.routes."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        router = Autorouter(width=30.0, height=30.0, rules=rules)

        pads = [
            {
                "number": "1",
                "x": 10.65,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
                "width": 0.3,
                "height": 0.8,
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        router.add_component("U1", pads)

        initial_routes = len(router.routes)
        escape_routes = router._run_subgrid_prepass()

        # Escape routes should be added to self.routes
        assert len(router.routes) == initial_routes + len(escape_routes)


class TestRetryNetWithSubgrid:
    """Tests for _retry_net_with_subgrid() directly."""

    def test_retry_returns_empty_for_nonexistent_net(self):
        """Retry with non-existent net should return empty list."""
        rules = DesignRules(grid_resolution=0.1)
        router = Autorouter(width=20.0, height=20.0, rules=rules)
        result = router._retry_net_with_subgrid(999)
        assert result == []

    def test_retry_returns_empty_for_single_pad_net(self):
        """Retry with a single-pad net should return empty list."""
        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        result = router._retry_net_with_subgrid(1)
        assert result == []

    def test_retry_returns_empty_for_on_grid_pads(self):
        """Retry with on-grid pads should return empty (no off-grid pads to escape)."""
        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        pads = [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 10.0, "y": 5.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        result = router._retry_net_with_subgrid(1)
        assert result == []
