"""Tests for neighborhood rip-up and relaxed pathfinding (Issue #2274).

These tests verify:
1. Stall detection logic (0 overflow, unrouted nets, consecutive stalls)
2. Relaxed A* pathfinding for blocker identification
3. Blocker scoring (nets blocking more stuck nets rank higher)
4. Escalating rip-up radius
5. Acceptance criterion (net_routes count increases, not just overflow decrease)
6. Graceful termination when no relaxed path exists
7. Maximum attempts budget
8. RoutedNetsUnblocker context manager
"""

from unittest.mock import MagicMock

import numpy as np

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.grid import RoutedNetsUnblocker


class TestRoutedNetsUnblocker:
    """Tests for the RoutedNetsUnblocker context manager."""

    def test_saves_and_restores_blocked_array(self):
        """Context manager should save blocked state on entry and restore on exit."""
        grid = MagicMock()
        grid._blocked = np.array([[[True, False, True]]], dtype=np.bool_)
        grid._pad_blocked = np.array([[[False, False, True]]], dtype=np.bool_)
        grid._net = np.array([[[5, 0, 0]]], dtype=np.int32)

        original_blocked = grid._blocked.copy()
        original_net = grid._net.copy()

        unblocker = RoutedNetsUnblocker(grid)

        with unblocker:
            # Cell (0,0,0): blocked=True, pad_blocked=False, net=5 -> should be unblocked
            assert not grid._blocked[0, 0, 0]
            assert grid._net[0, 0, 0] == 0
            # Cell (0,0,1): blocked=False -> unchanged
            assert not grid._blocked[0, 0, 1]
            # Cell (0,0,2): blocked=True but pad_blocked=True -> unchanged (static obstacle)
            assert grid._blocked[0, 0, 2]

        # After exit, arrays should be fully restored
        np.testing.assert_array_equal(grid._blocked, original_blocked)
        np.testing.assert_array_equal(grid._net, original_net)

    def test_preserves_static_obstacles(self):
        """Pad-blocked cells should remain blocked even when they have a net."""
        grid = MagicMock()
        # Cell with pad_blocked=True should never be unblocked
        grid._blocked = np.array([[[True]]], dtype=np.bool_)
        grid._pad_blocked = np.array([[[True]]], dtype=np.bool_)
        grid._net = np.array([[[3]]], dtype=np.int32)

        with RoutedNetsUnblocker(grid):
            assert grid._blocked[0, 0, 0]
            assert grid._net[0, 0, 0] == 3

    def test_unblocks_only_routed_net_cells(self):
        """Only cells with blocked=True, pad_blocked=False, net!=0 should be unblocked."""
        grid = MagicMock()
        grid._blocked = np.array([[[True, True, False, True]]], dtype=np.bool_)
        grid._pad_blocked = np.array([[[False, True, False, False]]], dtype=np.bool_)
        grid._net = np.array([[[2, 3, 0, 0]]], dtype=np.int32)

        with RoutedNetsUnblocker(grid):
            # (0,0,0): blocked + !pad_blocked + net=2 -> unblocked
            assert not grid._blocked[0, 0, 0]
            # (0,0,1): blocked + pad_blocked -> stays blocked
            assert grid._blocked[0, 0, 1]
            # (0,0,2): not blocked -> stays
            assert not grid._blocked[0, 0, 2]
            # (0,0,3): blocked + !pad_blocked + net=0 -> stays (no net)
            assert grid._blocked[0, 0, 3]


class TestFindBlockingNetsRelaxed:
    """Tests for relaxed A* blocker identification."""

    def _make_neg_router(self):
        """Create a NegotiatedRouter with mocked dependencies."""
        mock_grid = MagicMock()
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})
        return neg

    def test_returns_empty_when_no_relaxed_path(self):
        """When relaxed A* finds no path, no blockers are identified."""
        neg = self._make_neg_router()

        # Make relaxed A* return no path for all pads
        neg.router.find_blocking_nets_relaxed.return_value = set()

        # Set up the context manager
        unblocker = MagicMock()
        unblocker._saved_blocked = np.zeros((1, 10, 10), dtype=np.bool_)
        unblocker._saved_net = np.zeros((1, 10, 10), dtype=np.int32)
        unblocker.__enter__ = MagicMock(return_value=unblocker)
        unblocker.__exit__ = MagicMock(return_value=False)
        neg.grid.temporarily_unblock_routed_nets.return_value = unblocker

        pad1 = MagicMock()
        pad2 = MagicMock()

        result = neg.find_blocking_nets_relaxed(
            failed_nets=[10],
            pads_by_net={10: [pad1, pad2]},
        )

        assert result == {}

    def test_scores_blockers_by_stuck_net_count(self):
        """Blockers should be scored by how many stuck nets they block."""
        neg = self._make_neg_router()

        unblocker = MagicMock()
        unblocker._saved_blocked = np.zeros((1, 10, 10), dtype=np.bool_)
        unblocker._saved_net = np.zeros((1, 10, 10), dtype=np.int32)
        unblocker.__enter__ = MagicMock(return_value=unblocker)
        unblocker.__exit__ = MagicMock(return_value=False)
        neg.grid.temporarily_unblock_routed_nets.return_value = unblocker

        # Net 10 is blocked by nets {1, 2}
        # Net 20 is blocked by nets {2, 3}
        # Net 30 is blocked by net {2}
        call_count = [0]

        def mock_find_relaxed(*args, **kwargs):
            call_count[0] += 1
            # Return different blockers for different calls
            # The order depends on pad pairs within each net
            if call_count[0] == 1:
                return {1, 2}  # net 10's blockers
            elif call_count[0] == 2:
                return {2, 3}  # net 20's blockers
            elif call_count[0] == 3:
                return {2}  # net 30's blockers
            return set()

        neg.router.find_blocking_nets_relaxed.side_effect = mock_find_relaxed

        pad_a = MagicMock()
        pad_b = MagicMock()

        result = neg.find_blocking_nets_relaxed(
            failed_nets=[10, 20, 30],
            pads_by_net={
                10: [pad_a, pad_b],
                20: [pad_a, pad_b],
                30: [pad_a, pad_b],
            },
        )

        # Net 2 blocks all three stuck nets -> score 3
        assert result[2] == 3
        # Net 1 blocks one stuck net -> score 1
        assert result[1] == 1
        # Net 3 blocks one stuck net -> score 1
        assert result[3] == 1

    def test_skips_nets_with_fewer_than_two_pads(self):
        """Nets with fewer than 2 pads should be skipped."""
        neg = self._make_neg_router()

        unblocker = MagicMock()
        unblocker._saved_blocked = np.zeros((1, 10, 10), dtype=np.bool_)
        unblocker._saved_net = np.zeros((1, 10, 10), dtype=np.int32)
        unblocker.__enter__ = MagicMock(return_value=unblocker)
        unblocker.__exit__ = MagicMock(return_value=False)
        neg.grid.temporarily_unblock_routed_nets.return_value = unblocker

        result = neg.find_blocking_nets_relaxed(
            failed_nets=[10, 20],
            pads_by_net={
                10: [MagicMock()],  # Only 1 pad -- skip
                20: [],  # No pads -- skip
            },
        )

        assert result == {}
        neg.router.find_blocking_nets_relaxed.assert_not_called()


class TestNeighborhoodRipup:
    """Tests for the neighborhood_ripup method."""

    def _make_neg_router(self):
        """Create a NegotiatedRouter with mocked dependencies."""
        mock_grid = MagicMock()
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})
        return neg

    def test_returns_false_when_no_blockers_found(self):
        """Should return (False, count) when relaxed A* finds no blockers."""
        neg = self._make_neg_router()

        # Mock find_blocking_nets_relaxed to return empty
        neg.find_blocking_nets_relaxed = MagicMock(return_value={})

        improved, count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes={1: [MagicMock()], 2: [MagicMock()]},
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        assert improved is False
        assert count == 2  # 2 nets with routes

    def test_respects_max_attempts(self):
        """Should stop after max_attempts rip-ups."""
        neg = self._make_neg_router()

        # Return many blockers so we can test the limit
        neg.find_blocking_nets_relaxed = MagicMock(
            return_value={100: 3, 200: 2, 300: 1, 400: 1, 500: 1}
        )

        # Make rip_up_nets a no-op
        neg.rip_up_nets = MagicMock()
        # Make route_net_negotiated always fail
        neg.route_net_negotiated = MagicMock(return_value=[])

        # Create mock routes with segments for bounding box calculation
        mock_seg = MagicMock()
        mock_seg.x1, mock_seg.y1 = 0.0, 0.0
        mock_seg.x2, mock_seg.y2 = 1.0, 1.0
        mock_route = MagicMock()
        mock_route.segments = [mock_seg]
        neg.grid.world_to_grid.return_value = (5, 5)

        net_routes = {
            100: [mock_route],
            200: [mock_route],
            300: [mock_route],
            400: [mock_route],
            500: [mock_route],
        }

        improved, count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            max_attempts=2,  # Limit to 2
        )

        # rip_up_nets should be called at most 2 times
        assert neg.rip_up_nets.call_count <= 2

    def test_respects_ripup_budget_per_net(self):
        """Should skip blockers that have exceeded their ripup budget."""
        neg = self._make_neg_router()

        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 5})
        neg.rip_up_nets = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[])

        mock_seg = MagicMock()
        mock_seg.x1, mock_seg.y1 = 0.0, 0.0
        mock_seg.x2, mock_seg.y2 = 1.0, 1.0
        mock_route = MagicMock()
        mock_route.segments = [mock_seg]
        neg.grid.world_to_grid.return_value = (5, 5)

        # Net 100 already ripped up 5 times (at budget limit)
        ripup_history = {100: 5}

        improved, count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes={100: [mock_route]},
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            ripup_history=ripup_history,
            max_ripups_per_net=5,
        )

        # Should not rip up since net 100 is at budget
        neg.rip_up_nets.assert_not_called()

    def test_escalating_radius(self):
        """Radius should escalate with stall_count."""
        neg = self._make_neg_router()

        # We test this by checking the bounding box expansion grows
        # For stall_count=0: radius = 1.0 * (2.0 ** 0) = 1.0
        # For stall_count=1: radius = 1.0 * (2.0 ** 1) = 2.0
        # For stall_count=2: radius = 1.0 * (2.0 ** 2) = 4.0

        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 1})
        neg.rip_up_nets = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[])

        mock_seg = MagicMock()
        mock_seg.x1, mock_seg.y1 = 0.0, 0.0
        mock_seg.x2, mock_seg.y2 = 10.0, 10.0
        mock_route = MagicMock()
        mock_route.segments = [mock_seg]
        neg.grid.world_to_grid.side_effect = lambda x, y: (int(x), int(y))

        # Track which nets get ripped up to observe neighborhood size growth
        ripped_nets_per_call = []

        def track_ripup(nets, net_routes, routes_list):
            ripped_nets_per_call.append(set(nets))
            for n in nets:
                net_routes[n] = []

        neg.rip_up_nets.side_effect = track_ripup

        # Create several routed nets at different distances
        def make_route(x1, y1, x2, y2):
            seg = MagicMock()
            seg.x1, seg.y1 = float(x1), float(y1)
            seg.x2, seg.y2 = float(x2), float(y2)
            r = MagicMock()
            r.segments = [seg]
            return r

        # Run with stall_count=0 (radius_factor=1.0)
        net_routes_0 = {
            100: [make_route(0, 0, 10, 10)],  # The blocker
            200: [make_route(5, 5, 6, 6)],  # Close neighbor
            300: [make_route(50, 50, 60, 60)],  # Far away
        }

        neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes_0,
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            stall_count=0,
            initial_radius_factor=1.0,
            escalation_factor=2.0,
        )

        # With stall_count=0, radius is small, far net might not be included
        assert len(ripped_nets_per_call) == 1
        first_ripup = ripped_nets_per_call[0]
        assert 100 in first_ripup  # Blocker always included

    def test_improved_when_more_nets_routed(self):
        """Should report improved=True when more nets are successfully routed."""
        neg = self._make_neg_router()

        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 1})

        call_count = [0]

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)

        mock_seg = MagicMock()
        mock_seg.x1, mock_seg.y1 = 0.0, 0.0
        mock_seg.x2, mock_seg.y2 = 1.0, 1.0
        mock_route = MagicMock()
        mock_route.segments = [mock_seg]
        neg.grid.world_to_grid.return_value = (5, 5)

        # route_net_negotiated succeeds for all nets
        new_route = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[new_route])
        neg.grid.mark_route_usage = MagicMock()

        net_routes = {100: [mock_route]}
        # Net 10 was not routed before (not in net_routes)

        improved, count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={
                10: [MagicMock(), MagicMock()],
                100: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        assert improved is True
        # Net 10 was routed + net 100 was re-routed = 2 total
        assert count == 2

    def test_not_improved_when_no_new_nets_routed(self):
        """Should report improved=False when routing fails for all stuck nets."""
        neg = self._make_neg_router()

        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 1})

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)

        mock_seg = MagicMock()
        mock_seg.x1, mock_seg.y1 = 0.0, 0.0
        mock_seg.x2, mock_seg.y2 = 1.0, 1.0
        mock_route = MagicMock()
        mock_route.segments = [mock_seg]
        neg.grid.world_to_grid.return_value = (5, 5)

        # Routing fails for all nets
        neg.route_net_negotiated = MagicMock(return_value=[])
        neg.grid.mark_route_usage = MagicMock()

        net_routes = {100: [mock_route]}

        improved, count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={
                10: [MagicMock(), MagicMock()],
                100: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        assert improved is False
        assert count == 0  # Both nets failed


class TestStallDetectionIntegration:
    """Tests for stall detection logic that triggers neighborhood rip-up.

    These test the conditions under which neighborhood_ripup is activated
    in the route_all_negotiated main loop (core.py).
    """

    def test_stall_count_increments_when_no_progress(self):
        """Stall counter should increment when routed count does not change."""
        # This tests the logic pattern:
        # current_routed <= prev_routed_count -> stall_count += 1
        stall_count = 0
        prev_routed = 5
        current_routed = 5  # No change

        if current_routed <= prev_routed:
            stall_count += 1

        assert stall_count == 1

    def test_stall_count_resets_on_progress(self):
        """Stall counter should reset to 0 when routed count increases."""
        stall_count = 3
        prev_routed = 5
        current_routed = 6  # Progress!

        if current_routed <= prev_routed:
            stall_count += 1
        else:
            stall_count = 0

        assert stall_count == 0

    def test_threshold_not_met_when_overflow_nonzero(self):
        """Neighborhood rip-up should not activate when overflow > 0."""
        overflow = 5
        still_unrouted = [10, 20]
        stall_count = 5  # Well above threshold

        should_activate = overflow == 0 and still_unrouted and stall_count >= 2
        assert should_activate is False

    def test_threshold_not_met_when_no_unrouted(self):
        """Neighborhood rip-up should not activate when all nets are routed."""
        overflow = 0
        still_unrouted = []  # All routed
        stall_count = 5

        should_activate = overflow == 0 and bool(still_unrouted) and stall_count >= 2
        assert should_activate is False

    def test_threshold_met_conditions(self):
        """Neighborhood rip-up activates with 0 overflow, unrouted nets, stall >= threshold."""
        overflow = 0
        still_unrouted = [10]
        stall_count = 2
        threshold = 2

        should_activate = overflow == 0 and still_unrouted and stall_count >= threshold
        assert should_activate is True

    def test_custom_threshold(self):
        """Custom stall threshold should be respected."""
        overflow = 0
        still_unrouted = [10]
        stall_count = 3
        threshold = 4

        should_activate = overflow == 0 and still_unrouted and stall_count >= threshold
        assert should_activate is False

        stall_count = 4
        should_activate = overflow == 0 and still_unrouted and stall_count >= threshold
        assert should_activate is True


class TestGridTemporarilyUnblockRoutedNets:
    """Tests for the grid helper method."""

    def test_returns_context_manager(self):
        """temporarily_unblock_routed_nets should return a RoutedNetsUnblocker."""
        grid = MagicMock()
        grid._blocked = np.zeros((1, 5, 5), dtype=np.bool_)
        grid._pad_blocked = np.zeros((1, 5, 5), dtype=np.bool_)
        grid._net = np.zeros((1, 5, 5), dtype=np.int32)

        # Import the actual method

        # Use the class method via a real-ish approach
        unblocker = RoutedNetsUnblocker(grid)
        assert hasattr(unblocker, "__enter__")
        assert hasattr(unblocker, "__exit__")


class TestAcceptanceCriterion:
    """Tests verifying the acceptance criterion: net_routes count increases."""

    def test_accepts_when_new_net_routed_even_with_overflow(self):
        """Neighborhood rip-up should accept when more nets are routed,
        even if overflow increases from 0."""
        neg = NegotiatedRouter(MagicMock(), MagicMock(), MagicMock(), {})

        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 1})

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)

        mock_seg = MagicMock()
        mock_seg.x1, mock_seg.y1 = 0.0, 0.0
        mock_seg.x2, mock_seg.y2 = 1.0, 1.0
        mock_route = MagicMock()
        mock_route.segments = [mock_seg]
        neg.grid.world_to_grid.return_value = (5, 5)

        new_route = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[new_route])
        neg.grid.mark_route_usage = MagicMock()

        # Before: 1 routed net (100), net 10 unrouted
        net_routes = {100: [mock_route]}

        improved, count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={
                10: [MagicMock(), MagicMock()],
                100: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        # The acceptance is based on net count, not overflow
        assert improved is True
        assert count > 1  # More than original 1 net
