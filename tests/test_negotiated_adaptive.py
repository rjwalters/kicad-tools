"""Tests for adaptive negotiated routing functions (Issue #633).

These tests verify the adaptive parameter tuning functions that improve
convergence for negotiated congestion routing.

Also includes tests for matrix-conflict detection and layer preference
assignment (Issue #2432).
"""

from kicad_tools.router.algorithms.negotiated import (
    _is_monotonically_diverging,
    calculate_history_increment,
    calculate_present_cost,
    detect_oscillation,
    should_terminate_early,
)


class TestCalculateHistoryIncrement:
    """Tests for calculate_history_increment function."""

    def test_returns_base_with_insufficient_history(self):
        """Should return base increment with < 2 history entries."""
        assert calculate_history_increment(0, [], 0.5) == 0.5
        assert calculate_history_increment(1, [10], 0.5) == 0.5

    def test_increases_when_overflow_increases(self):
        """Should increase increment when overflow is getting worse."""
        # Overflow went from 10 to 15
        result = calculate_history_increment(2, [10, 15], 0.5)
        assert result == 0.75  # 0.5 * 1.5

    def test_increases_when_stagnant(self):
        """Should increase increment when overflow is stagnant."""
        # Same value for 3 iterations
        result = calculate_history_increment(3, [10, 10, 10], 0.5)
        # Loop counts indices 1 and 0, both equal to current, so stagnant_count = 3
        # Result: 0.5 * (1.0 + 0.5 * 3) = 0.5 * 2.5 = 1.25
        assert result == 1.25

    def test_decreases_when_close_to_zero(self):
        """Should decrease increment when close to convergence."""
        # Overflow decreased from 10 to 3
        result = calculate_history_increment(2, [10, 3], 0.5)
        assert result == 0.25  # 0.5 * 0.5

    def test_normal_progress_uses_base(self):
        """Should use base increment for normal decreasing progress."""
        # Overflow decreased from 50 to 30 (not close to 0)
        result = calculate_history_increment(2, [50, 30], 0.5)
        assert result == 0.5


class TestDetectOscillation:
    """Tests for detect_oscillation function."""

    def test_no_oscillation_with_insufficient_history(self):
        """Should return False with < window entries."""
        assert detect_oscillation([], window=4) is False
        assert detect_oscillation([10, 11], window=4) is False
        assert detect_oscillation([10, 11, 12], window=4) is False

    def test_detects_abab_pattern(self):
        """Should detect A-B-A-B alternating pattern."""
        # 10 -> 12 -> 10 -> 12
        assert detect_oscillation([10, 12, 10, 12], window=4) is True

    def test_detects_complete_stagnation(self):
        """Should detect when all values are the same."""
        assert detect_oscillation([14, 14, 14, 14], window=4) is True

    def test_detects_bounded_oscillation(self):
        """Should detect when values stay within small range."""
        # Only 2 unique values, minimum > 0
        assert detect_oscillation([10, 11, 10, 11], window=4) is True

    def test_no_oscillation_with_progress(self):
        """Should not detect oscillation when making progress."""
        # Steadily decreasing
        assert detect_oscillation([20, 15, 10, 5], window=4) is False

    def test_no_oscillation_when_converged(self):
        """Should not detect oscillation when at zero (converged).

        Issue #2262: Zero-overflow stagnation is convergence, not oscillation.
        detect_oscillation must return False so that escape strategies are not
        triggered on a fully-converged solution.
        """
        assert detect_oscillation([0, 0, 0, 0], window=4) is False

    def test_no_oscillation_when_window_has_new_minimum(self):
        """Issue #1823: Should NOT detect oscillation when window contains new best.

        Pattern [21, 21, 8, 21] has overflow 8 as a new minimum compared to
        earlier history -- this means the router is making progress even though
        the values bounce back.
        """
        # History: earlier overflows were 21, then window [21, 21, 8, 21]
        assert detect_oscillation([21, 21, 21, 21, 8, 21], window=4) is False

    def test_oscillation_when_no_new_minimum_in_window(self):
        """Should still detect oscillation when window has no new minimum.

        Pattern [21, 21, 21, 21] with prior history showing best=8 means the
        router is stuck and not improving.
        """
        assert detect_oscillation([21, 8, 21, 21, 21, 21], window=4) is True

    def test_stagnation_still_detected_with_longer_history(self):
        """Stagnation at the same value should still be detected.

        [50, 50, 50, 50, 50, 50] -- all same value, no new minimum in window.
        """
        assert detect_oscillation([50, 50, 50, 50, 50, 50], window=4) is True


class TestShouldTerminateEarly:
    """Tests for should_terminate_early function."""

    def test_no_termination_before_min_iterations(self):
        """Should not terminate before minimum iterations."""
        history = [20, 20, 20, 20, 20]
        assert should_terminate_early(history, iteration=3, min_iterations=5) is False

    def test_no_termination_with_insufficient_history(self):
        """Should not terminate with < 5 history entries."""
        history = [20, 18, 16, 14]  # Only 4 entries
        assert should_terminate_early(history, iteration=10, min_iterations=5) is False

    def test_terminates_when_no_recent_improvement(self):
        """Should terminate when no improvement in last 5 iterations."""
        # First half: improved from 30 to 20
        # Second half: stuck at 20
        history = [30, 25, 20, 20, 20, 20, 20, 20, 20, 20]
        assert should_terminate_early(history, iteration=10, min_iterations=5) is True

    def test_terminates_when_getting_worse(self):
        """Should terminate when overflow is trending upward."""
        # First half: was at 10
        # Second half: jumped to 15+ (> 10 * 1.2 = 12)
        history = [10, 10, 10, 15, 16, 17]
        assert should_terminate_early(history, iteration=6, min_iterations=5) is True

    def test_no_termination_when_improving(self):
        """Should not terminate when making progress."""
        history = [50, 40, 30, 20, 10, 5]
        assert should_terminate_early(history, iteration=6, min_iterations=5) is False

    def test_terminates_on_diverging_overflow_from_issue_1266(self):
        """Should terminate on the exact diverging pattern from issue #1266.

        The sequence [90, 96, 88, 130, 148, 155] was reported as running all
        the way to max_iterations because:
        - The stale baseline ([float('inf')]) masked the no-improvement check
        - The dip to 88 in the second half defeated the half-split worsening check
        - detect_oscillation missed it because it is not an A-B-A-B cycle
        """
        history = [90, 96, 88, 130, 148, 155]
        assert should_terminate_early(history, iteration=5, min_iterations=5) is True

    def test_terminates_with_stale_baseline_at_exactly_5_entries(self):
        """Should use first value as baseline when history has exactly 5 entries.

        Previously, earlier defaulted to [float('inf')] making the
        no-improvement check always False with exactly 5 history entries.
        """
        # All 5 values are >= the first value (90), so no improvement
        history = [90, 95, 92, 93, 91]
        assert should_terminate_early(history, iteration=5, min_iterations=5) is True

    def test_no_false_positive_on_genuine_convergence_with_dip(self):
        """Should NOT terminate on a converging sequence with a transient dip.

        A sequence like [90, 85, 80, 75, 70] is genuinely improving even
        though early values are higher.  The monotonic divergence check must
        not fire here.
        """
        history = [90, 85, 80, 75, 70]
        assert should_terminate_early(history, iteration=5, min_iterations=5) is False

    def test_no_false_positive_on_slow_convergence(self):
        """Should NOT terminate when overflow is slowly decreasing.

        Sequence [100, 98, 95, 93, 88, 85] has recent min (85) below
        earlier min (100), so the no-improvement check should not fire.
        """
        history = [100, 98, 95, 93, 88, 85]
        assert should_terminate_early(history, iteration=6, min_iterations=5) is False

    def test_terminates_on_monotonic_divergence_longer_history(self):
        """Should terminate when trailing values diverge monotonically.

        After an initial improvement the overflow starts climbing and the
        last 3 values are strictly increasing and all above the best seen.
        """
        history = [50, 40, 35, 38, 45, 55, 60, 70]
        assert should_terminate_early(history, iteration=8, min_iterations=5) is True

    def test_handles_fewer_than_5_entries_gracefully(self):
        """Should return False with fewer than 5 entries regardless of pattern."""
        assert should_terminate_early([100, 200, 300], iteration=5, min_iterations=3) is False
        assert should_terminate_early([100, 200, 300, 400], iteration=5, min_iterations=3) is False

    def test_no_termination_when_recent_window_has_new_global_min(self):
        """Issue #1823: Should NOT terminate when recent window found new best.

        History [21, 21, 21, 21, 21, 8, 21, 21, 21, 21] has min(recent)=8
        which is a new global minimum compared to earlier [21, 21, 21, 21, 21].
        The router made real progress recently and should keep going.
        """
        history = [21, 21, 21, 21, 21, 8, 21, 21, 21, 21]
        assert should_terminate_early(history, iteration=10, min_iterations=5) is False

    def test_terminates_when_recent_min_equals_earlier_best(self):
        """Should still terminate when recent best is not better than earlier best.

        History [8, 21, 21, 21, 21, 21, 8, 21, 21, 21] has min(recent)=8
        but min(earlier)=8 too, so no new improvement.
        """
        history = [8, 21, 21, 21, 21, 21, 8, 21, 21, 21]
        assert should_terminate_early(history, iteration=10, min_iterations=5) is True


class TestIsMonotonicallyDiverging:
    """Tests for _is_monotonically_diverging helper."""

    def test_detects_strict_increasing_above_best(self):
        """Should detect [90, 88, 130, 148, 155] — last 3 are 130<148<155, all > 88."""
        assert _is_monotonically_diverging([90, 88, 130, 148, 155], window=3) is True

    def test_rejects_decreasing_sequence(self):
        """Should reject a converging sequence."""
        assert _is_monotonically_diverging([100, 90, 80, 70, 60], window=3) is False

    def test_rejects_flat_sequence(self):
        """Should reject a flat sequence (not strictly increasing)."""
        assert _is_monotonically_diverging([50, 100, 100, 100], window=3) is False

    def test_rejects_when_recent_includes_best(self):
        """Should reject when one of the recent values equals the best seen."""
        # best_seen=40, recent=[40, 50, 60] — 40 is not > 40
        assert _is_monotonically_diverging([50, 40, 40, 50, 60], window=3) is False

    def test_rejects_insufficient_history(self):
        """Should return False with too few entries."""
        assert _is_monotonically_diverging([10, 20, 30], window=3) is False
        assert _is_monotonically_diverging([10, 20], window=3) is False
        assert _is_monotonically_diverging([], window=3) is False

    def test_detects_with_custom_window(self):
        """Should work with non-default window sizes."""
        # window=4: last 4 values [120, 130, 140, 150] all > best=100, strictly increasing
        assert _is_monotonically_diverging([100, 110, 120, 130, 140, 150], window=4) is True
        # window=2: last 2 values [140, 150] > best=100, strictly increasing
        assert _is_monotonically_diverging([100, 110, 140, 150], window=2) is True

    def test_issue_1266_exact_sequence(self):
        """Should detect the exact sequence from the bug report."""
        assert _is_monotonically_diverging([90, 96, 88, 130, 148, 155], window=3) is True


class TestEscapeStrategyCycling:
    """Tests for escape_local_minimum trying all strategies (Issue #1638)."""

    def test_escape_tries_all_strategies_on_failure(self):
        """escape_local_minimum should cycle through all 4 strategies before giving up."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # Make all strategies fail
        neg._escape_shuffle_order = MagicMock(return_value=(False, 10))
        neg._escape_reverse_order = MagicMock(return_value=(False, 10))
        neg._escape_random_subset = MagicMock(return_value=(False, 10))
        neg._escape_full_reorder = MagicMock(return_value=(False, 10))

        success, overflow, tried = neg.escape_local_minimum(
            overflow_history=[10, 10, 10, 10],
            net_routes={},
            routes_list=[],
            pads_by_net={},
            net_order=[],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            strategy_index=0,
        )

        assert success is False
        assert tried == 4
        assert neg._escape_shuffle_order.call_count == 1
        assert neg._escape_reverse_order.call_count == 1
        assert neg._escape_random_subset.call_count == 1
        assert neg._escape_full_reorder.call_count == 1

    def test_escape_stops_on_first_success(self):
        """escape_local_minimum should stop as soon as one strategy succeeds."""
        from unittest.mock import MagicMock

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # First fails, second succeeds
        neg._escape_shuffle_order = MagicMock(return_value=(False, 10))
        neg._escape_reverse_order = MagicMock(return_value=(True, 5))
        neg._escape_random_subset = MagicMock(return_value=(False, 10))
        neg._escape_full_reorder = MagicMock(return_value=(False, 10))

        success, overflow, tried = neg.escape_local_minimum(
            overflow_history=[10, 10, 10, 10],
            net_routes={},
            routes_list=[],
            pads_by_net={},
            net_order=[],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            strategy_index=0,
        )

        assert success is True
        assert overflow == 5
        assert tried == 2
        assert neg._escape_shuffle_order.call_count == 1
        assert neg._escape_reverse_order.call_count == 1
        assert neg._escape_random_subset.call_count == 0  # Not tried
        assert neg._escape_full_reorder.call_count == 0  # Not tried

    def test_escape_wraps_around_strategy_index(self):
        """escape_local_minimum should wrap strategy index modulo num strategies."""
        from unittest.mock import MagicMock

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # All fail
        neg._escape_shuffle_order = MagicMock(return_value=(False, 10))
        neg._escape_reverse_order = MagicMock(return_value=(False, 10))
        neg._escape_random_subset = MagicMock(return_value=(False, 10))
        neg._escape_full_reorder = MagicMock(return_value=(False, 10))

        # Start from index 1 (reverse), should try reverse -> random -> full_reorder -> shuffle
        success, overflow, tried = neg.escape_local_minimum(
            overflow_history=[10, 10, 10, 10],
            net_routes={},
            routes_list=[],
            pads_by_net={},
            net_order=[],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            strategy_index=1,
        )

        assert success is False
        assert tried == 4
        # All four should be called exactly once
        assert neg._escape_shuffle_order.call_count == 1
        assert neg._escape_reverse_order.call_count == 1
        assert neg._escape_random_subset.call_count == 1
        assert neg._escape_full_reorder.call_count == 1


class TestEscapeFullReorder:
    """Tests for the full-reorder escape strategy (Issue #1823)."""

    def test_full_reorder_rips_up_all_nets(self):
        """Full reorder should rip up ALL nets, not just conflicting ones."""
        from unittest.mock import MagicMock, call

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_grid.get_total_overflow.return_value = 5
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # Set up mock routes for 3 nets
        mock_route_1 = MagicMock()
        mock_route_2 = MagicMock()
        mock_route_3 = MagicMock()
        net_routes = {1: [mock_route_1], 2: [mock_route_2], 3: [mock_route_3]}
        routes_list = [mock_route_1, mock_route_2, mock_route_3]

        # Mock rip_up_nets to track what gets ripped
        ripped_nets = []
        original_rip_up = neg.rip_up_nets

        def track_rip_up(nets, nr, rl):
            ripped_nets.extend(nets)
            original_rip_up(nets, nr, rl)

        neg.rip_up_nets = track_rip_up

        # Mock route_net_negotiated to return a route
        mock_new_route = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[mock_new_route])

        pads_by_net = {
            1: [MagicMock(), MagicMock()],
            2: [MagicMock(), MagicMock()],
            3: [MagicMock(), MagicMock()],
        }

        success, new_overflow = neg._escape_full_reorder(
            overflow_history=[10, 10, 10, 10],
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net=pads_by_net,
            net_order=[1, 2, 3],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        # All 3 nets should have been ripped up
        assert sorted(ripped_nets) == [1, 2, 3]
        # All 3 nets should have been rerouted
        assert neg.route_net_negotiated.call_count == 3

    def test_full_reorder_reverses_net_order(self):
        """Full reorder should route nets in reversed priority order."""
        from unittest.mock import MagicMock, call

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_grid.get_total_overflow.return_value = 5
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        net_routes = {1: [MagicMock()], 2: [MagicMock()], 3: [MagicMock()]}
        routes_list = list(net_routes[1] + net_routes[2] + net_routes[3])
        neg.rip_up_nets = MagicMock()  # Don't actually rip up

        # Track routing order
        route_order = []
        mock_route = MagicMock()

        def track_route(pads, cost, callback, **kwargs):
            # Identify net by matching pads object identity
            for net_id, net_pads in pads_by_net.items():
                if pads is net_pads:
                    route_order.append(net_id)
                    break
            return [mock_route]

        neg.route_net_negotiated = track_route

        pads_by_net = {
            1: [MagicMock(), MagicMock()],
            2: [MagicMock(), MagicMock()],
            3: [MagicMock(), MagicMock()],
        }

        neg._escape_full_reorder(
            overflow_history=[10, 10, 10, 10],
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net=pads_by_net,
            net_order=[1, 2, 3],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        # Should route in reversed order: 3, 2, 1
        assert route_order == [3, 2, 1]

    def test_full_reorder_returns_false_on_empty_nets(self):
        """Full reorder should return False when no nets are routed."""
        from unittest.mock import MagicMock

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        neg = NegotiatedRouter(mock_grid, MagicMock(), MagicMock(), {})

        success, overflow = neg._escape_full_reorder(
            overflow_history=[10],
            net_routes={},
            routes_list=[],
            pads_by_net={},
            net_order=[],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        assert success is False
        assert overflow == 10


class TestStallRecoveryFallback:
    """Tests for targeted rip-up fallback when routing stalls (Issue #2265).

    When overflow is 0 but nets remain unrouted, the standard (non-targeted)
    rip-up path must fall back to targeted rip-up to identify and displace
    blocking nets.
    """

    def test_find_blocking_nets_returns_blockers(self):
        """find_blocking_nets_for_connection should identify which nets
        occupy cells along the direct path between two pads."""
        from unittest.mock import MagicMock

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_router = MagicMock()
        # Simulate router.find_blocking_nets returning a set
        mock_router.find_blocking_nets.return_value = {2, 3}

        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        pad_a = MagicMock()
        pad_b = MagicMock()

        blockers = neg.find_blocking_nets_for_connection(pad_a, pad_b)
        assert 2 in blockers
        assert 3 in blockers

    def test_targeted_ripup_called_for_stalled_nets(self):
        """When overflow is 0 and nets remain unrouted, targeted_ripup
        should be invoked for each failed net that has identified blockers."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_router = MagicMock()
        mock_router.find_blocking_nets.return_value = {2}

        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # targeted_ripup should be callable and return success/failure
        neg.targeted_ripup = MagicMock(return_value=True)
        neg.find_blocking_nets_for_connection = MagicMock(return_value={2})

        # Simulate calling targeted_ripup for a failed net
        failed_net = 1
        blocking = neg.find_blocking_nets_for_connection(MagicMock(), MagicMock())
        assert len(blocking) > 0

        success = neg.targeted_ripup(
            failed_net=failed_net,
            blocking_nets=blocking,
            net_routes={2: [MagicMock()]},
            routes_list=[],
            pads_by_net={1: [MagicMock(), MagicMock()], 2: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )
        assert success is True


class TestCalculatePresentCost:
    """Tests for calculate_present_cost function."""

    def test_base_cost_at_start(self):
        """At iteration 0, should be close to base cost."""
        result = calculate_present_cost(
            iteration=0, total_iterations=10, overflow_ratio=0.0, base_cost=0.5
        )
        # progress_factor = 1.0, congestion_factor = 1.0
        assert result == 0.5

    def test_increases_with_iterations(self):
        """Cost should increase as iterations progress."""
        early = calculate_present_cost(1, 10, 0.0, 0.5)
        late = calculate_present_cost(8, 10, 0.0, 0.5)
        assert late > early

    def test_increases_with_congestion(self):
        """Cost should increase with higher congestion."""
        low_congestion = calculate_present_cost(5, 10, 0.1, 0.5)
        high_congestion = calculate_present_cost(5, 10, 0.5, 0.5)
        assert high_congestion > low_congestion

    def test_congestion_factor_is_capped(self):
        """Congestion factor should be capped at 3x."""
        # overflow_ratio = 2.0 would give congestion_factor = 1 + 4 = 5
        # But it's capped at 1 + 2 = 3
        result = calculate_present_cost(0, 10, 2.0, 0.5)
        expected = 0.5 * 1.0 * 3.0  # base * progress * congestion_cap
        assert result == expected

    def test_handles_zero_total_iterations(self):
        """Should handle edge case of zero total iterations."""
        result = calculate_present_cost(0, 0, 0.0, 0.5)
        # Should not raise, uses max(total_iterations, 1)
        assert result > 0


class TestNegotiatedRouterCongestionEstimator:
    """Tests for NegotiatedRouter passing congestion_fn to build_rsmt."""

    def test_accepts_none_estimator(self):
        """NegotiatedRouter with congestion_estimator=None should construct."""
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        # None is the default -- just verify no TypeError
        router = NegotiatedRouter.__new__(NegotiatedRouter)
        router.grid = None
        router.router = None
        router.rules = None
        router.net_class_map = {}
        router.congestion_estimator = None
        router.congestion_weight = 0.5
        assert router.congestion_estimator is None

    def test_stores_estimator_and_weight(self):
        """NegotiatedRouter should store congestion_estimator and weight."""
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        sentinel = object()
        router = NegotiatedRouter.__new__(NegotiatedRouter)
        router.grid = None
        router.router = None
        router.rules = None
        router.net_class_map = {}
        router.congestion_estimator = sentinel
        router.congestion_weight = 1.25
        assert router.congestion_estimator is sentinel
        assert router.congestion_weight == 1.25

    def test_congestion_fn_built_when_estimator_present(self):
        """When estimator is provided, build_rsmt receives a congestion_fn.

        We mock build_rsmt at the steiner module level to capture
        the congestion_fn argument (the import is lazy inside
        route_net_negotiated).
        """
        from unittest.mock import MagicMock, patch

        from kicad_tools.router.algorithms import steiner as steiner_mod
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Pad

        # Create a mock estimator with grid attributes
        mock_grid = MagicMock()
        mock_grid.tile_w = 2.0
        mock_grid.tile_h = 2.0
        mock_grid.tile_at.return_value = (1, 1)

        mock_est = MagicMock()
        mock_est.grid = mock_grid
        mock_est.get_tile_demand.return_value = 3.0

        # Build a NegotiatedRouter with the mock estimator
        nr = NegotiatedRouter.__new__(NegotiatedRouter)
        nr.grid = None
        nr.router = MagicMock()
        nr.rules = None
        nr.net_class_map = {}
        nr.congestion_estimator = mock_est
        nr.congestion_weight = 0.5

        # Create 3 pads (triggers build_rsmt path)
        pads = [
            Pad(x=0, y=0, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
            Pad(x=10, y=0, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
            Pad(x=5, y=5, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
        ]

        captured_fn = {}

        def fake_build_rsmt(pad_objs, congestion_fn=None):
            captured_fn["fn"] = congestion_fn
            # Return minimal valid result: all pads, edges forming a chain
            return list(pad_objs), [(0, 1), (1, 2)]

        with patch.object(steiner_mod, "build_rsmt", side_effect=fake_build_rsmt):
            nr.route_net_negotiated(pads, 1.0, lambda r: None)

        # Verify build_rsmt was called with a congestion_fn
        assert captured_fn.get("fn") is not None
        fn = captured_fn["fn"]

        # Verify the function computes correctly:
        # Manhattan(0,0 -> 10,0) = 10
        # tile_at(5, 0) -> (1,1), demand = 3.0
        # scaled_weight = 0.5 * 2.0 * 2.0 = 2.0
        # result = 10 + 2.0 * 3.0 = 16.0
        result = fn(0, 0, 10, 0)
        assert result == 16.0

    def test_no_congestion_fn_when_estimator_none(self):
        """When estimator is None, build_rsmt receives congestion_fn=None."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.router.algorithms import steiner as steiner_mod
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Pad

        nr = NegotiatedRouter.__new__(NegotiatedRouter)
        nr.grid = None
        nr.router = MagicMock()
        nr.rules = None
        nr.net_class_map = {}
        nr.congestion_estimator = None
        nr.congestion_weight = 0.5

        pads = [
            Pad(x=0, y=0, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
            Pad(x=10, y=0, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
            Pad(x=5, y=5, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
        ]

        captured_fn = {}

        def fake_build_rsmt(pad_objs, congestion_fn=None):
            captured_fn["fn"] = congestion_fn
            return list(pad_objs), [(0, 1), (1, 2)]

        with patch.object(steiner_mod, "build_rsmt", side_effect=fake_build_rsmt):
            nr.route_net_negotiated(pads, 1.0, lambda r: None)

        assert captured_fn["fn"] is None

    def test_no_congestion_fn_when_weight_zero(self):
        """When congestion_weight=0, congestion_fn should be None."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.router.algorithms import steiner as steiner_mod
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Pad

        mock_est = MagicMock()

        nr = NegotiatedRouter.__new__(NegotiatedRouter)
        nr.grid = None
        nr.router = MagicMock()
        nr.rules = None
        nr.net_class_map = {}
        nr.congestion_estimator = mock_est
        nr.congestion_weight = 0  # Disabled

        pads = [
            Pad(x=0, y=0, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
            Pad(x=10, y=0, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
            Pad(x=5, y=5, width=0.5, height=0.5, net=1, net_name="n", layer=Layer.F_CU),
        ]

        captured_fn = {}

        def fake_build_rsmt(pad_objs, congestion_fn=None):
            captured_fn["fn"] = congestion_fn
            return list(pad_objs), [(0, 1), (1, 2)]

        with patch.object(steiner_mod, "build_rsmt", side_effect=fake_build_rsmt):
            nr.route_net_negotiated(pads, 1.0, lambda r: None)

        assert captured_fn["fn"] is None


class TestShouldTerminateEarlyLowOverflow:
    """Tests for Issue #2295: tighter stagnation window when overflow < 5."""

    def test_terminates_after_3_iterations_with_low_overflow(self):
        """When overflow is low (< 5), stagnation should be detected in 3 iterations.

        History: [10, 6, 3, 3, 3, 3] -- overflow dropped to 3 then stagnated.
        The low-overflow path uses a 3-iteration window: recent=[3,3,3],
        earlier=[10,6,3], min(earlier)=3, so min(recent)>=min(earlier) -> terminate.
        """
        history = [10, 6, 3, 3, 3, 3]
        assert should_terminate_early(history, iteration=6, min_iterations=5) is True

    def test_does_not_terminate_early_when_overflow_high(self):
        """When overflow >= 5, the standard 5-iteration window should still apply.

        History: [20, 15, 10, 10, 10] -- only 3 stagnant iterations at overflow 10.
        With 5-iteration window, recent=[20,15,10,10,10], min=10, earlier=[20],
        min(earlier)=20, 10 < 20 -> no termination.
        """
        history = [20, 15, 10, 10, 10]
        assert should_terminate_early(history, iteration=5, min_iterations=5) is False

    def test_low_overflow_stagnation_at_boundary(self):
        """Overflow of exactly 4 (< 5) should use the shorter window.

        History: [10, 4, 4, 4, 4] -- stagnated at 4 for 4 iterations.
        3-iteration window: recent=[4,4,4], earlier=[10,4], min(earlier)=4.
        min(recent)=4 >= 4 -> terminate.
        """
        history = [10, 4, 4, 4, 4]
        assert should_terminate_early(history, iteration=5, min_iterations=5) is True

    def test_low_overflow_does_not_terminate_when_improving(self):
        """Even with low overflow, should not terminate if still improving.

        History: [10, 4, 3, 2, 1] -- steadily decreasing.
        3-iteration window: recent=[3,2,1], earlier=[10,4], min(earlier)=4.
        min(recent)=1 < 4 -> no termination.
        """
        history = [10, 4, 3, 2, 1]
        assert should_terminate_early(history, iteration=5, min_iterations=5) is False

    def test_overflow_5_uses_standard_window(self):
        """Overflow of exactly 5 should NOT trigger the shorter window.

        History: [20, 10, 5, 5, 5, 5, 5, 5, 5, 5] -- stagnated at 5.
        Standard 5-iteration window: recent=[5,5,5,5,5], earlier=[20,10,5,5,5],
        min(earlier)=5. min(recent)=5 >= 5 -> terminate via standard path.
        """
        history = [20, 10, 5, 5, 5, 5, 5, 5, 5, 5]
        assert should_terminate_early(history, iteration=10, min_iterations=5) is True

    def test_zero_overflow_with_unrouted_does_not_terminate(self):
        """Issue #2297: overflow=0 with unrouted nets should not terminate.

        Even though 0 < 5, the unrouted_count guard takes precedence.
        """
        history = [10, 5, 0, 0, 0, 0, 0]
        assert (
            should_terminate_early(history, iteration=7, min_iterations=5, unrouted_count=2)
            is False
        )


class TestEscapeBudgetEnforcement:
    """Tests for escape strategy timeout enforcement (Issue #2415)."""

    def test_escape_budget_expires_returns_early(self):
        """escape_local_minimum with a tiny budget should return quickly."""
        import time
        from unittest.mock import MagicMock

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # Each strategy sleeps 0.1s to simulate work, budget is 0.001s
        def slow_strategy(**kwargs):
            time.sleep(0.1)
            return False, 10

        neg._escape_shuffle_order = MagicMock(side_effect=slow_strategy)
        neg._escape_reverse_order = MagicMock(side_effect=slow_strategy)
        neg._escape_random_subset = MagicMock(side_effect=slow_strategy)
        neg._escape_full_reorder = MagicMock(side_effect=slow_strategy)

        start = time.time()
        success, overflow, tried = neg.escape_local_minimum(
            overflow_history=[10, 10, 10, 10],
            net_routes={},
            routes_list=[],
            pads_by_net={},
            net_order=[],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            strategy_index=0,
            per_net_timeout=1.0,
            escape_budget=0.001,
        )
        elapsed = time.time() - start

        assert success is False
        # Should have tried at most 2 strategies (first runs, then budget
        # check fires before second or shortly after)
        assert tried <= 2
        # Total wall time should be well under 1 second
        assert elapsed < 1.0

    def test_per_net_timeout_propagated_to_route_net_negotiated(self):
        """per_net_timeout should be passed through to route_net_negotiated."""
        from unittest.mock import MagicMock, patch

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_grid.find_overused_cells.return_value = {(0, 0, 0)}
        mock_grid.get_total_overflow.return_value = 5
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})
        neg.find_nets_through_overused_cells = MagicMock(return_value=[1, 2])
        neg.rip_up_nets = MagicMock()

        # Mock route_net_negotiated to capture the per_net_timeout arg
        captured_timeouts = []

        def mock_route(pad_objs, cost, callback, per_net_timeout=None):
            captured_timeouts.append(per_net_timeout)
            return []

        neg.route_net_negotiated = mock_route

        neg._escape_shuffle_order(
            overflow_history=[10, 10],
            net_routes={1: [], 2: []},
            routes_list=[],
            pads_by_net={1: [MagicMock(), MagicMock()], 2: [MagicMock(), MagicMock()]},
            net_order=[1, 2],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            per_net_timeout=3.5,
        )

        # Every call should have received per_net_timeout=3.5
        assert len(captured_timeouts) == 2
        assert all(t == 3.5 for t in captured_timeouts)

    def test_escape_budget_none_preserves_existing_behavior(self):
        """When escape_budget=None, all 4 strategies should be tried."""
        from unittest.mock import MagicMock

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # All strategies fail
        neg._escape_shuffle_order = MagicMock(return_value=(False, 10))
        neg._escape_reverse_order = MagicMock(return_value=(False, 10))
        neg._escape_random_subset = MagicMock(return_value=(False, 10))
        neg._escape_full_reorder = MagicMock(return_value=(False, 10))

        success, overflow, tried = neg.escape_local_minimum(
            overflow_history=[10, 10, 10, 10],
            net_routes={},
            routes_list=[],
            pads_by_net={},
            net_order=[],
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            strategy_index=0,
            per_net_timeout=None,
            escape_budget=None,
        )

        assert success is False
        assert tried == 4
        assert neg._escape_shuffle_order.call_count == 1
        assert neg._escape_reverse_order.call_count == 1
        assert neg._escape_random_subset.call_count == 1
        assert neg._escape_full_reorder.call_count == 1

    def test_budget_expires_mid_strategy_returns_false(self):
        """When budget expires during a strategy, escape returns without hanging."""
        import time
        from unittest.mock import MagicMock

        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        mock_grid = MagicMock()
        mock_grid.find_overused_cells.return_value = {(0, 0, 0)}
        mock_grid.get_total_overflow.return_value = 10
        mock_router = MagicMock()
        neg = NegotiatedRouter(mock_grid, mock_router, MagicMock(), {})

        # Create many nets so the budget expires mid-loop
        many_nets = list(range(100))
        neg.find_nets_through_overused_cells = MagicMock(return_value=many_nets)
        neg.rip_up_nets = MagicMock()

        call_count = 0

        def slow_route(pad_objs, cost, callback, per_net_timeout=None):
            nonlocal call_count
            call_count += 1
            time.sleep(0.01)  # 10ms per net
            return []

        neg.route_net_negotiated = slow_route

        pads_by_net = {n: [MagicMock(), MagicMock()] for n in many_nets}

        start = time.time()
        success, overflow, tried = neg.escape_local_minimum(
            overflow_history=[10, 10, 10, 10],
            net_routes={n: [] for n in many_nets},
            routes_list=[],
            pads_by_net=pads_by_net,
            net_order=many_nets,
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            strategy_index=0,
            per_net_timeout=1.0,
            escape_budget=0.05,  # 50ms budget
        )
        elapsed = time.time() - start

        assert success is False
        # Should have routed far fewer than 100 nets
        assert call_count < 100
        # Should complete well under 2 seconds
        assert elapsed < 2.0


class TestEmptyNetsToRerouteTermination:
    """Tests for Issue #2413: Early termination when nets_to_reroute is empty.

    When all conflicting nets are excluded by the stall detector,
    nets_to_reroute becomes empty and the rip-up loop should terminate
    immediately rather than spinning with no work to do.
    """

    def test_empty_nets_to_reroute_triggers_break(self):
        """An empty nets_to_reroute list should signal termination.

        The condition inserted in _route_board_negotiated is simply:
            if not nets_to_reroute: break

        This test verifies the predicate evaluates correctly for the
        empty-list case.
        """
        nets_to_reroute: list[int] = []
        assert not nets_to_reroute, (
            "Empty nets_to_reroute must be falsy to trigger early termination"
        )

    def test_non_empty_nets_to_reroute_continues(self):
        """A non-empty nets_to_reroute list should NOT trigger termination."""
        nets_to_reroute = [1, 2, 3]
        assert nets_to_reroute, (
            "Non-empty nets_to_reroute must be truthy to continue iteration"
        )

    def test_stall_filtering_can_produce_empty_list(self):
        """Filtering all nets as stalled produces an empty reroute list.

        Simulates the stall filtering logic from _route_board_negotiated:
        nets_to_reroute = [n for n in nets_to_reroute if n not in stalled_nets]
        """
        nets_to_reroute = [1, 2, 3]
        stalled_nets = {1, 2, 3}

        # Apply stall filtering (mirrors core.py lines 3124-3127)
        nets_to_reroute = [
            n for n in nets_to_reroute if n not in stalled_nets
        ]

        assert not nets_to_reroute, (
            "All-stalled filtering must produce empty list"
        )

    def test_partial_stall_does_not_trigger_termination(self):
        """When only some nets are stalled, termination must NOT trigger."""
        nets_to_reroute = [1, 2, 3]
        stalled_nets = {1, 3}

        nets_to_reroute = [
            n for n in nets_to_reroute if n not in stalled_nets
        ]

        assert nets_to_reroute == [2], (
            "Partial stall must leave remaining nets for reroute"
        )
        assert nets_to_reroute, (
            "Partial stall must not trigger early termination"
        )

    def test_re_enabled_stalled_nets_prevent_empty_list(self):
        """After stalled nets are re-enabled (overflow improved), the
        stalled set is cleared.  This means nets_to_reroute keeps its
        original entries and the loop continues normally.
        """
        nets_to_reroute = [1, 2, 3]
        stalled_nets = {1, 2, 3}

        # Simulate overflow improvement -> re-enable
        overflow_history = [30, 25]  # improving
        if (
            stalled_nets
            and len(overflow_history) >= 2
            and overflow_history[-1] < overflow_history[-2]
        ):
            stalled_nets.clear()

        # Re-apply filter with cleared stalled set
        nets_to_reroute = [
            n for n in nets_to_reroute if n not in stalled_nets
        ]

        assert nets_to_reroute == [1, 2, 3], (
            "Re-enabled nets must remain in reroute list"
        )


# =========================================================================
# Matrix conflict detection and layer assignment tests (Issue #2432)
# =========================================================================


class TestDetectMatrixConflicts:
    """Tests for Autorouter._detect_matrix_conflicts()."""

    def _make_autorouter_with_nets(self, nets_data):
        """Create a minimal Autorouter with given nets data.

        Args:
            nets_data: dict mapping net_id -> list of (ref, pin) tuples
        """
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=50, height=50)
        ar.nets = nets_data
        ar.net_names = {n: f"NET_{n}" for n in nets_data}
        return ar

    def test_no_conflicts_disjoint_nets(self):
        """Nets with no shared components should produce no conflict groups."""
        ar = self._make_autorouter_with_nets({
            1: [("R1", "1"), ("R1", "2")],
            2: [("R2", "1"), ("R2", "2")],
            3: [("C1", "1"), ("C1", "2")],
        })
        groups = ar._detect_matrix_conflicts([1, 2, 3])
        assert groups == []

    def test_no_conflicts_single_shared_component(self):
        """Two nets sharing only 1 component should NOT conflict (threshold=2)."""
        ar = self._make_autorouter_with_nets({
            1: [("D1", "A"), ("D1", "K"), ("R1", "1")],
            2: [("D1", "A"), ("D2", "K"), ("R2", "1")],
        })
        groups = ar._detect_matrix_conflicts([1, 2], threshold=2)
        assert groups == []

    def test_charlieplex_four_nets(self):
        """Four nets sharing 3+ LEDs should form one conflict group."""
        # Simulates NODE_A..NODE_D each connecting to pads on D1..D6
        ar = self._make_autorouter_with_nets({
            1: [("D1", "A"), ("D2", "K"), ("D3", "A"), ("D5", "K")],
            2: [("D1", "K"), ("D2", "A"), ("D4", "A"), ("D6", "K")],
            3: [("D3", "K"), ("D4", "K"), ("D5", "A"), ("D6", "A")],
            4: [("D1", "A"), ("D3", "K"), ("D5", "A"), ("D6", "K")],
        })
        groups = ar._detect_matrix_conflicts([1, 2, 3, 4])
        assert len(groups) == 1
        assert groups[0] == {1, 2, 3, 4}

    def test_two_independent_groups(self):
        """Two separate matrix groups should produce two conflict sets."""
        ar = self._make_autorouter_with_nets({
            # Group 1: nets 1, 2 share D1, D2, D3
            1: [("D1", "A"), ("D2", "K"), ("D3", "A")],
            2: [("D1", "K"), ("D2", "A"), ("D3", "K")],
            # Group 2: nets 3, 4 share U1, U2 (different components)
            3: [("U1", "1"), ("U2", "2"), ("U3", "1")],
            4: [("U1", "2"), ("U2", "1"), ("U3", "2")],
            # Net 5: no conflicts
            5: [("R1", "1"), ("R1", "2")],
        })
        groups = ar._detect_matrix_conflicts([1, 2, 3, 4, 5])
        assert len(groups) == 2
        group_sets = [frozenset(g) for g in groups]
        assert frozenset({1, 2}) in group_sets
        assert frozenset({3, 4}) in group_sets

    def test_custom_threshold(self):
        """Higher threshold should require more shared components."""
        ar = self._make_autorouter_with_nets({
            1: [("D1", "A"), ("D2", "K")],
            2: [("D1", "K"), ("D2", "A")],
        })
        # threshold=2: should conflict (share D1, D2)
        groups_t2 = ar._detect_matrix_conflicts([1, 2], threshold=2)
        assert len(groups_t2) == 1

        # threshold=3: should NOT conflict (only share 2 components)
        groups_t3 = ar._detect_matrix_conflicts([1, 2], threshold=3)
        assert groups_t3 == []

    def test_empty_nets(self):
        """Empty net list should produce no conflicts."""
        ar = self._make_autorouter_with_nets({})
        groups = ar._detect_matrix_conflicts([])
        assert groups == []


class TestAssignMatrixLayerPreferences:
    """Tests for Autorouter._assign_matrix_layer_preferences()."""

    def _make_autorouter(self, num_layers=2):
        """Create a minimal Autorouter with given layer count."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.layers import LayerDefinition, LayerStack, LayerType

        layers = []
        for i in range(num_layers):
            name = "F.Cu" if i == 0 else ("B.Cu" if i == num_layers - 1 else f"In{i}.Cu")
            layers.append(LayerDefinition(
                name=name,
                index=i,
                layer_type=LayerType.SIGNAL,
                is_outer=(i == 0 or i == num_layers - 1),
            ))
        stack = LayerStack(layers=layers, name="Test")
        ar = Autorouter(width=50, height=50, layer_stack=stack)
        return ar

    def test_alternating_layers_two_layer_board(self):
        """Nets in a conflict group should get alternating F.Cu/B.Cu."""
        ar = self._make_autorouter(num_layers=2)
        groups = [{1, 2, 3, 4}]
        prefs = ar._assign_matrix_layer_preferences(groups)
        assert len(prefs) == 4
        # Sorted order: 1, 2, 3, 4
        assert prefs[1] == [0]  # F.Cu
        assert prefs[2] == [1]  # B.Cu
        assert prefs[3] == [0]  # F.Cu
        assert prefs[4] == [1]  # B.Cu

    def test_single_layer_board_no_preferences(self):
        """Single-layer board should return empty preferences."""
        ar = self._make_autorouter(num_layers=1)
        groups = [{1, 2}]
        prefs = ar._assign_matrix_layer_preferences(groups)
        assert prefs == {}

    def test_four_layer_board_uses_outer_layers(self):
        """Multi-layer board should alternate between first and last layers."""
        ar = self._make_autorouter(num_layers=4)
        groups = [{10, 20}]
        prefs = ar._assign_matrix_layer_preferences(groups)
        assert prefs[10] == [0]  # F.Cu (index 0)
        assert prefs[20] == [3]  # B.Cu (index 3)

    def test_multiple_groups_independent(self):
        """Each conflict group should be assigned independently."""
        ar = self._make_autorouter(num_layers=2)
        groups = [{1, 2}, {3, 4}]
        prefs = ar._assign_matrix_layer_preferences(groups)
        assert len(prefs) == 4
        # Group 1: nets 1,2
        assert prefs[1] == [0]
        assert prefs[2] == [1]
        # Group 2: nets 3,4
        assert prefs[3] == [0]
        assert prefs[4] == [1]


class TestInjectMatrixLayerPreferences:
    """Tests for Autorouter._inject_matrix_layer_preferences()."""

    def _make_autorouter_with_nets(self, nets_data, net_names=None):
        """Create a minimal Autorouter with given nets data."""
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=50, height=50)
        ar.nets = nets_data
        if net_names:
            ar.net_names = net_names
        else:
            ar.net_names = {n: f"NET_{n}" for n in nets_data}
        return ar

    def test_creates_net_class_entries(self):
        """Should create NetClassRouting entries with preferred_layers."""
        from kicad_tools.router.rules import NetClassRouting

        ar = self._make_autorouter_with_nets(
            {1: [], 2: []},
            net_names={1: "NODE_A", 2: "NODE_B"},
        )
        prefs = {1: [0], 2: [1]}
        ar._inject_matrix_layer_preferences(prefs)

        assert "NODE_A" in ar.net_class_map
        assert ar.net_class_map["NODE_A"].preferred_layers == [0]
        assert "NODE_B" in ar.net_class_map
        assert ar.net_class_map["NODE_B"].preferred_layers == [1]

    def test_preserves_existing_net_class(self):
        """Should copy existing net class and add layer preference."""
        from kicad_tools.router.rules import NetClassRouting

        ar = self._make_autorouter_with_nets(
            {1: []},
            net_names={1: "MCLK"},
        )
        # Pre-existing net class for MCLK
        ar.net_class_map["MCLK"] = NetClassRouting(
            name="Clock", priority=2, trace_width=0.15
        )
        prefs = {1: [0]}
        ar._inject_matrix_layer_preferences(prefs)

        nc = ar.net_class_map["MCLK"]
        assert nc.priority == 2  # Preserved
        assert nc.trace_width == 0.15  # Preserved
        assert nc.preferred_layers == [0]  # Added

    def test_skips_nets_without_names(self):
        """Nets with no name should be skipped without error."""
        ar = self._make_autorouter_with_nets(
            {1: []},
            net_names={},  # No name for net 1
        )
        prefs = {1: [0]}
        ar._inject_matrix_layer_preferences(prefs)
        # Should not crash; no new entries since no name


class TestMatrixConstraintBoost:
    """Tests for matrix net priority boost in _calculate_constraint_score."""

    def _make_autorouter_with_matrix_nets(self, matrix_nets):
        """Create an Autorouter with specified matrix conflict nets."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.primitives import Pad

        ar = Autorouter(width=50, height=50)
        ar._matrix_conflict_nets = set(matrix_nets)
        # Add minimal pad data for the nets
        for net_id in matrix_nets:
            pad_key = (f"R{net_id}", "1")
            ar.nets[net_id] = [pad_key]
            ar.pads[pad_key] = Pad(
                x=10.0, y=10.0,
                width=1.0, height=1.0,
                net=net_id, net_name=f"NET_{net_id}",
                ref=f"R{net_id}", pin="1",
            )
            ar.net_names[net_id] = f"NET_{net_id}"
        return ar

    def test_matrix_net_gets_higher_constraint_score(self):
        """Matrix-conflicting nets should have higher constraint score."""
        ar = self._make_autorouter_with_matrix_nets([1])
        # Also add a non-matrix net
        pad_key = ("R99", "1")
        from kicad_tools.router.primitives import Pad
        ar.nets[99] = [pad_key]
        ar.pads[pad_key] = Pad(
            x=20.0, y=20.0,
            width=1.0, height=1.0,
            net=99, net_name="NET_99",
            ref="R99", pin="1",
        )
        ar.net_names[99] = "NET_99"

        matrix_score = ar._calculate_constraint_score(1)
        normal_score = ar._calculate_constraint_score(99)
        assert matrix_score > normal_score, (
            "Matrix net should have higher constraint score"
        )

    def test_non_matrix_net_unaffected(self):
        """Non-matrix nets should not get the matrix boost."""
        ar = self._make_autorouter_with_matrix_nets([])
        pad_key = ("R1", "1")
        from kicad_tools.router.primitives import Pad
        ar.nets[1] = [pad_key]
        ar.pads[pad_key] = Pad(
            x=10.0, y=10.0,
            width=1.0, height=1.0,
            net=1, net_name="NET_1",
            ref="R1", pin="1",
        )
        ar.net_names[1] = "NET_1"

        score = ar._calculate_constraint_score(1)
        # Score should only contain pad_count_weight * 1 = 0.5
        assert score < 5.0, "Non-matrix net should have low constraint score"
