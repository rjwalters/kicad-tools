"""Tests for best-state tracking in two-phase negotiated routing (Issue #2305).

When overflow oscillates during rip-up-and-reroute iterations, the router
should return the routing state with the lowest overflow observed, not
whatever state the final iteration left behind.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter
from kicad_tools.router.primitives import Route, Segment

# =============================================================================
# Helpers
# =============================================================================


def _make_route(net: int, tag: str = "") -> Route:
    """Create a minimal Route for testing."""
    return Route(
        net=net,
        net_name=f"Net{net}{'_' + tag if tag else ''}",
        segments=[
            Segment(
                x1=0.0,
                y1=0.0,
                x2=1.0,
                y2=1.0,
                width=0.2,
                layer=0,
                net=net,
            )
        ],
    )


class FakeGrid:
    """Minimal grid mock that returns controllable overflow values."""

    def __init__(self, overflow_sequence: list[int]):
        """overflow_sequence: overflow values returned by successive calls."""
        self._overflow_seq = list(overflow_sequence)
        self._overflow_idx = 0
        self._marked_routes: list[Route] = []
        self.width = 20.0
        self.height = 20.0
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.num_layers = 1

    def get_total_overflow(self) -> int:
        idx = min(self._overflow_idx, len(self._overflow_seq) - 1)
        val = self._overflow_seq[idx]
        self._overflow_idx += 1
        return val

    def mark_route_usage(self, route: Route) -> None:
        self._marked_routes.append(route)

    def unmark_route_usage(self, route: Route) -> None:
        if route in self._marked_routes:
            self._marked_routes.remove(route)

    def unmark_route(self, route: Route) -> None:
        pass

    def update_history_costs(self, increment: float) -> None:
        pass

    def find_overused_cells(self) -> set:
        return {(5, 5, 0)}  # dummy overused cell

    def set_corridor_preference(self, corridor, net, penalty) -> None:
        pass

    def clear_all_corridor_preferences(self) -> None:
        pass


class FakeNegotiatedRouter:
    """Minimal NegotiatedRouter mock."""

    def __init__(self, grid, router, rules, net_class_map):
        self.grid = grid

    def find_nets_through_overused_cells(self, net_routes, overused):
        # Always reroute all nets that have routes
        return list(net_routes.keys())

    # Clearance-matrix violation finders (issues #3002/#3020/#3433): the
    # two-phase loop's best-state comparator queries these after every
    # iteration. The fake reports a clean board so the overflow sequence
    # alone drives best-state selection (issue #3436 stale-fake fix).
    def find_nets_with_segment_via_violations(
        self, net_routes, trace_clearance, cache_key=None, extra_routes=None
    ):
        return []

    def find_nets_with_via_segment_violations(
        self, net_routes, trace_clearance, cache_key=None, extra_routes=None
    ):
        return []

    def find_nets_with_segment_segment_violations(
        self, net_routes, trace_clearance, cache_key=None, extra_routes=None
    ):
        return []

    def find_segment_segment_violation_pairs(
        self, net_routes, trace_clearance, extra_routes=None, copper_overlap_only=False
    ):
        return []

    def rip_up_nets(self, nets, net_routes, routes_list):
        for net in nets:
            for route in net_routes.get(net, []):
                self.grid.unmark_route_usage(route)
                if route in routes_list:
                    routes_list.remove(route)
            net_routes[net] = []


# =============================================================================
# Tests
# =============================================================================


class TestBestStateTracking:
    """Verify the router returns the best-overflow state, not the final one."""

    def _build_two_phase(
        self,
        overflow_sequence: list[int],
        routes_per_iteration: dict[int, list[Route]] | None = None,
    ) -> tuple[TwoPhaseRouter, FakeGrid]:
        """Build a TwoPhaseRouter with controlled overflow behavior.

        Args:
            overflow_sequence: Overflow values returned by grid.get_total_overflow()
                in order. The first value is for the initial pass; subsequent
                values are for each rip-up-and-reroute iteration; the final
                value is for the post-loop best-state check.
            routes_per_iteration: Optional mapping of call index to routes
                returned by _route_net_with_corridor. If not provided, each
                call returns a single-segment route.
        """
        grid = FakeGrid(overflow_sequence)
        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        # Issue #2597: Numeric defaults for stagnation thresholds so the
        # rip-up cohort detector compares floats, not MagicMock objects.
        rules.stagnation_overflow_delta_threshold = 0.20
        rules.stagnation_jaccard_threshold = 0.8

        call_count = [0]

        def fake_route_net_with_corridor(net, present_factor, per_net_timeout=None):
            idx = call_count[0]
            call_count[0] += 1
            if routes_per_iteration and idx in routes_per_iteration:
                return routes_per_iteration[idx]
            return [_make_route(net, tag=f"iter{idx}")]

        two_phase = TwoPhaseRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets={1: [("R1", "1"), ("R1", "2")]},
            net_names={1: "VCC"},
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=fake_route_net_with_corridor,
            mark_route=lambda r: None,
        )

        return two_phase, grid

    def test_returns_best_state_when_overflow_oscillates(self):
        """When overflow oscillates (e.g. 5 -> 2 -> 8), return the iter with overflow=2."""
        # Overflow sequence:
        #   [0] initial pass = 5
        #   [1] iteration 1  = 2  <-- best
        #   [2] iteration 2  = 8
        #   [3] post-loop check = 8 (same as final iteration)
        two_phase, grid = self._build_two_phase([5, 2, 8, 8])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        # The returned routes should correspond to the best state (overflow=2),
        # not the final iteration (overflow=8).
        assert len(routes) > 0
        # Verify via route net_name tags that we got iteration-1 routes
        # (the snapshot), not iteration-2 routes.
        route_names = [r.net_name for r in routes]
        assert any("iter" in name for name in route_names)

    def test_no_restoration_when_final_is_best(self):
        """When the final iteration has the lowest overflow, no restoration occurs."""
        # Overflow sequence:
        #   [0] initial pass = 5
        #   [1] iteration 1  = 3
        #   [2] iteration 2  = 1  <-- best AND final
        #   [3] post-loop check = 1
        two_phase, grid = self._build_two_phase([5, 3, 1, 1])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        assert len(routes) > 0

    def test_initial_pass_is_best(self):
        """When the initial pass has the best overflow, it is preserved."""
        # Overflow sequence:
        #   [0] initial pass = 2  <-- best
        #   [1] iteration 1  = 5
        #   [2] post-loop check = 5
        two_phase, grid = self._build_two_phase([2, 5, 5])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        assert len(routes) > 0

    def test_zero_overflow_initial_no_iterations(self):
        """When initial pass has overflow=0, no iterations run and no snapshot overhead."""
        # Overflow sequence:
        #   [0] initial pass = 0
        #   [1] post-loop check = 0
        two_phase, grid = self._build_two_phase([0, 0])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        assert len(routes) > 0

    def test_convergence_at_zero_returns_immediately(self):
        """When an iteration converges to overflow=0, that state is returned."""
        # Overflow sequence:
        #   [0] initial pass = 3
        #   [1] iteration 1  = 0  <-- converged
        #   [2] post-loop check = 0
        two_phase, grid = self._build_two_phase([3, 0, 0])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        assert len(routes) > 0

    def test_restoration_log_message(self, capsys):
        """Verify log message appears when restoring a previous best state."""
        # Overflow sequence:
        #   [0] initial pass = 5
        #   [1] iteration 1  = 2  <-- best
        #   [2] iteration 2  = 8
        #   [3] post-loop check = 8
        two_phase, grid = self._build_two_phase([5, 2, 8, 8])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        captured = capsys.readouterr()
        assert "Restoring iteration 1 state" in captured.out
        assert "overflow=2" in captured.out
        assert "overflow=8" in captured.out

    def test_no_restoration_log_when_final_is_best(self, capsys):
        """No restoration log when final iteration is the best."""
        # Overflow sequence:
        #   [0] initial pass = 5
        #   [1] iteration 1  = 3
        #   [2] iteration 2  = 1  <-- best AND final
        #   [3] post-loop check = 1
        two_phase, grid = self._build_two_phase([5, 3, 1, 1])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        captured = capsys.readouterr()
        assert "Restoring" not in captured.out

    def test_grid_state_consistent_after_restoration(self):
        """After restoring best state, grid marked routes match returned routes."""
        # Overflow sequence:
        #   [0] initial pass = 5
        #   [1] iteration 1  = 2  <-- best
        #   [2] iteration 2  = 8
        #   [3] post-loop check = 8
        two_phase, grid = self._build_two_phase([5, 2, 8, 8])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
            )

        # After restoration, the grid's marked routes should match two_phase.routes
        # (which should equal the returned routes)
        assert len(two_phase.routes) == len(routes)
        # Grid's _marked_routes should have the same count
        assert len(grid._marked_routes) == len(routes)


class TestEarlyStopOverflowRegression:
    """Verify early-stop fires when overflow regresses across iterations (Issue #2317)."""

    def _build_two_phase(
        self,
        overflow_sequence: list[int],
    ) -> tuple[TwoPhaseRouter, FakeGrid]:
        """Build a TwoPhaseRouter with controlled overflow behavior."""
        grid = FakeGrid(overflow_sequence)
        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        rules.corridor_decay_rate = 0.1
        rules.corridor_decay_floor = 0.1
        # Issue #2597: Numeric defaults for stagnation thresholds so the
        # rip-up cohort detector compares floats, not MagicMock objects.
        rules.stagnation_overflow_delta_threshold = 0.20
        rules.stagnation_jaccard_threshold = 0.8

        call_count = [0]

        def fake_route_net_with_corridor(net, present_factor, per_net_timeout=None):
            idx = call_count[0]
            call_count[0] += 1
            return [_make_route(net, tag=f"iter{idx}")]

        two_phase = TwoPhaseRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets={1: [("R1", "1"), ("R1", "2")]},
            net_names={1: "VCC"},
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=fake_route_net_with_corridor,
            mark_route=lambda r: None,
        )

        return two_phase, grid

    def test_early_stop_on_regression(self, capsys):
        """Early stop fires when overflow regresses after reaching a best.

        Overflow sequence: [81, 18, 25, 30, ...].
        With patience=2, should_terminate_early should fire after we have
        enough history showing regression (no improvement over 18).
        The loop should NOT run all max_iterations.
        """
        # Overflow sequence:
        #   [0] initial pass = 81
        #   [1] iteration 1  = 18  <-- best
        #   [2] iteration 2  = 25  (regression)
        #   [3] iteration 3  = 30  (further regression)
        #   [4] iteration 4  = 35  (further regression)
        #   [5] iteration 5  = 40  (would be iteration 5 if reached)
        #   [6] post-loop check (final overflow for best-state restore)
        # With patience=2, the early termination should fire well before
        # iteration 10.  We provide enough overflow values for max_iterations=10.
        overflow_seq = [81, 18, 25, 30, 35, 40, 45, 50, 55, 60, 65, 65]
        two_phase, grid = self._build_two_phase(overflow_seq)
        # Issue #2597: Disable rip-up cohort stagnation detection so this
        # test exercises ``should_terminate_early()`` exclusively.  The
        # FakeNegotiatedRouter always rips up the same single net, so the
        # new detector would fire on iter 2's regression (18 → 25) before
        # the should_terminate_early heuristic gets a chance to evaluate
        # the longer history.  Setting an impossibly-strict delta
        # threshold (-1.0) means only a >100 % regression would trip
        # stagnation detection, which never happens in this fixture.
        two_phase.rules.stagnation_overflow_delta_threshold = -1.0

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
                max_iterations=10,
                patience=2,
            )

        captured = capsys.readouterr()
        # Early stop message should appear
        assert "Early stop: overflow not improving" in captured.out
        assert "best=18" in captured.out
        # Best-state restore should occur since final overflow > best
        assert "Restoring" in captured.out
        # Should have returned routes
        assert len(routes) > 0

    def test_no_early_stop_when_converging(self, capsys):
        """No early stop when overflow monotonically decreases to zero."""
        # Overflow sequence:
        #   [0] initial pass = 81
        #   [1] iteration 1  = 18
        #   [2] iteration 2  = 10
        #   [3] iteration 3  = 5
        #   [4] iteration 4  = 0  <-- converged
        #   [5] post-loop check = 0
        two_phase, grid = self._build_two_phase([81, 18, 10, 5, 0, 0])

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
                max_iterations=10,
                patience=2,
            )

        captured = capsys.readouterr()
        # Should converge normally, no early stop
        assert "Converged at iteration 4" in captured.out
        assert "Early stop" not in captured.out
        assert len(routes) > 0

    def test_best_state_restored_after_early_stop(self):
        """Best-state restore still works correctly after early-stop fires."""
        # Overflow sequence:
        #   [0] initial pass = 81
        #   [1] iteration 1  = 18  <-- best
        #   [2] iteration 2  = 25
        #   [3] iteration 3  = 30
        #   [4] iteration 4  = 35
        #   [5] iteration 5  = 40
        #   [6] post-loop check (for best-state restore)
        overflow_seq = [81, 18, 25, 30, 35, 40, 40]
        two_phase, grid = self._build_two_phase(overflow_seq)

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
                max_iterations=10,
                patience=2,
            )

        # Routes should be returned and grid state should be consistent
        assert len(routes) > 0
        assert len(two_phase.routes) == len(routes)
        assert len(grid._marked_routes) == len(routes)

    def test_patience_parameter_respected(self, capsys):
        """Higher patience value delays early termination."""
        # With a high patience (e.g. 8), early stop should not fire for
        # a sequence that regresses only a few times.
        # Overflow sequence that would trigger early stop with patience=2
        # but should NOT trigger with patience=8:
        #   [0] initial = 81, [1] = 18, [2] = 25, [3] = 30, [4] = 0
        overflow_seq = [81, 18, 25, 30, 0, 0]
        two_phase, grid = self._build_two_phase(overflow_seq)
        # Issue #2597: Disable rip-up cohort stagnation detection so this
        # test exercises ``patience`` exclusively.  See comment in
        # ``test_early_stop_on_regression`` for details.
        two_phase.rules.stagnation_overflow_delta_threshold = -1.0

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            two_phase._detailed_negotiated(
                net_order=[1],
                corridor_penalty=5.0,
                max_iterations=10,
                patience=8,  # High patience -- early stop should not fire
            )

        captured = capsys.readouterr()
        # Should converge at iteration 4 (overflow=0), not early stop
        assert "Converged at iteration 4" in captured.out
        assert "Early stop" not in captured.out
