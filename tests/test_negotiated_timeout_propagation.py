"""Tests for wall-clock timeout propagation in negotiated routing (Issue #2518).

The issue: ``--timeout`` was only enforced at iteration boundaries on the
two-phase / hierarchical negotiated path.  When ``check_timeout()`` fired
inside the per-net inner loop it only ``break``-ed the inner loop, allowing
one more full iteration's worth of overflow recompute, history snapshot, and
(crucially) a brand-new outer iteration to run before the iteration-boundary
check finally caught it.  In the chorus-test repro, ``--timeout 900`` produced
a 1756.6s wall-clock run (1.95x overrun).

These tests verify the fix: a ``timed_out`` flag set inside the inner break
propagates to the outer iteration loop so the next iteration is never started
and the overflow / history bookkeeping is skipped.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kicad_tools.router.algorithms.hierarchical import HierarchicalRouter
from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter
from kicad_tools.router.primitives import Route, Segment

# =============================================================================
# Helpers — mirror those in test_best_state_tracking.py so the fixture
# wiring stays consistent.
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
    """Minimal grid mock; overflow is forced positive every call so the
    rip-up loop runs until the outer iteration limit (or timeout) trips."""

    def __init__(self, persistent_overflow: int = 5):
        self._overflow = persistent_overflow
        self._marked_routes: list[Route] = []
        self.width = 20.0
        self.height = 20.0
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.num_layers = 1

    def get_total_overflow(self) -> int:
        return self._overflow

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
        return {(5, 5, 0)}

    def set_corridor_preference(self, corridor, net, penalty) -> None:
        pass

    def clear_all_corridor_preferences(self) -> None:
        pass


class FakeNegotiatedRouter:
    """Minimal NegotiatedRouter mock — every iteration rips up every net."""

    def __init__(self, grid, router, rules, net_class_map):
        self.grid = grid

    def find_nets_through_overused_cells(self, net_routes, overused):
        return list(net_routes.keys())

    # Clearance-matrix violation finders (issues #3002/#3020/#3433): the
    # two-phase loop's best-state comparator queries these after every
    # iteration. The fake reports a clean board so the timeout machinery
    # alone drives the loop (issue #3436 stale-fake fix).
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


class _FakeClock:
    """Monotonic-style fake clock that advances by a fixed step on each
    call to ``time.time()`` (or ``time.monotonic()``).  This lets us drive
    the wall-clock check deterministically without sleeping in tests."""

    def __init__(self, step: float = 1.0, start: float = 1000.0):
        self.step = step
        self.now = start
        self.calls = 0

    def __call__(self) -> float:
        # Return the *current* time, then advance.  This way the first
        # call returns ``start`` (used as ``start_time``), and subsequent
        # ``check_timeout()`` calls see monotonically increasing values.
        t = self.now
        self.now += self.step
        self.calls += 1
        return t


# =============================================================================
# TwoPhaseRouter._detailed_negotiated tests
# =============================================================================


class TestTwoPhaseTimeoutPropagation:
    """Verify TwoPhaseRouter respects ``timeout`` immediately, not at the
    next iteration boundary (Issue #2518)."""

    def _build(
        self,
        net_count: int = 6,
        per_net_step: float = 5.0,
    ) -> tuple[TwoPhaseRouter, FakeGrid, list[float]]:
        """Build a TwoPhaseRouter that consumes ``per_net_step`` "seconds"
        of fake time per ``_route_net_with_corridor`` call.

        The fake clock advances by 0.0 per ``time.time()`` call by default;
        each per-net route call burns ``per_net_step`` extra seconds.
        """
        grid = FakeGrid(persistent_overflow=5)  # never converges
        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        rules.corridor_decay_rate = 0.1
        rules.corridor_decay_floor = 0.1
        # Issue #2597: Set explicit numeric defaults for stagnation thresholds
        # so the rip-up cohort detector can compare them against floats.
        rules.stagnation_overflow_delta_threshold = 0.20
        rules.stagnation_jaccard_threshold = 0.8

        # Track timestamps when each net is routed (after the burn).
        burn_log: list[float] = []
        clock = _FakeClock(step=0.0, start=1000.0)

        def fake_route_net_with_corridor(net, present_factor, per_net_timeout=None):
            # Burn fake time without advancing on the time.time() call itself.
            clock.now += per_net_step
            burn_log.append(clock.now)
            return [_make_route(net, tag=f"call{len(burn_log)}")]

        nets = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in range(1, net_count + 1)}
        net_names = {n: f"Net{n}" for n in nets}

        two_phase = TwoPhaseRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets=nets,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=fake_route_net_with_corridor,
            mark_route=lambda r: None,
        )

        # Patch the module-level ``time.time`` used by ``check_timeout`` /
        # ``elapsed_str`` inside two_phase.py.  We attach the clock to the
        # router so the test can inspect it.
        two_phase._test_clock = clock  # type: ignore[attr-defined]
        return two_phase, grid, burn_log

    def test_timeout_breaks_out_of_initial_pass(self, capsys):
        """When the wall-clock budget is exhausted during the initial pass,
        no rip-up iterations should be entered (was: one full iteration's
        tail of work would still run)."""
        two_phase, grid, burn_log = self._build(net_count=6, per_net_step=5.0)
        clock = two_phase._test_clock  # type: ignore[attr-defined]

        with (
            patch("kicad_tools.router.algorithms.two_phase.time.time", clock),
            patch(
                "kicad_tools.router.algorithms.NegotiatedRouter",
                FakeNegotiatedRouter,
            ),
        ):
            net_order = list(two_phase.nets.keys())
            two_phase._detailed_negotiated(
                net_order=net_order,
                corridor_penalty=5.0,
                timeout=10.0,
                start_time=1000.0,
                max_iterations=20,
                patience=2,
            )

        captured = capsys.readouterr()
        # The initial-pass timeout fires at net 3/6 (after 3 * 5.0 = 15s
        # > 10s budget).  The fix must skip the rip-up section entirely.
        assert "Timeout during detailed routing" in captured.out
        assert "Iteration 1: ripping up" not in captured.out
        # No "Restoring iteration N state" output because no iteration ran.
        assert "Iteration 1 complete" not in captured.out

    def test_timeout_in_inner_reroute_loop_skips_iteration_tail(self, capsys):
        """When the timeout fires inside the per-net rip-up reroute loop,
        the iteration body's overflow recompute, history append, and the
        next iteration must NOT run."""
        # 3 nets, each costs 2s.  Initial pass = 6s.
        # Budget = 10s.  Iteration 1 starts at 6s and routes net 1 (8s).
        # Net 2's check_timeout sees elapsed=10s -> >= budget -> timeout.
        # Without the fix, control would fall through the rest of the
        # iteration body (overflow recompute, history append) and start
        # iteration 2.  With the fix, the iteration loop exits immediately.
        two_phase, grid, burn_log = self._build(net_count=3, per_net_step=2.0)
        clock = two_phase._test_clock  # type: ignore[attr-defined]

        with (
            patch("kicad_tools.router.algorithms.two_phase.time.time", clock),
            patch(
                "kicad_tools.router.algorithms.NegotiatedRouter",
                FakeNegotiatedRouter,
            ),
        ):
            net_order = list(two_phase.nets.keys())
            two_phase._detailed_negotiated(
                net_order=net_order,
                corridor_penalty=5.0,
                timeout=10.0,
                start_time=1000.0,
                max_iterations=5,
                patience=99,
            )

        captured = capsys.readouterr()
        # Initial pass should complete (6s < 10s budget).
        assert "Initial pass: 3/3 nets" in captured.out
        # Iteration 1 should start...
        assert "Iteration 1: ripping up" in captured.out
        # ...and timeout in the per-net inner loop.
        assert "Timeout during reroute at net" in captured.out
        # Iteration 2 must NOT start (this is the regression we are
        # guarding against — pre-fix it would).
        assert "Iteration 2: ripping up" not in captured.out
        # Bookkeeping that runs *after* the inner loop must be skipped:
        # "Iteration N complete" comes from the overflow recompute
        # following the inner loop.
        assert "Iteration 1 complete" not in captured.out

    def test_no_timeout_runs_all_iterations(self, capsys):
        """When ``timeout=None``, the loop runs to ``max_iterations`` (or
        early-stop) — confirms the new flag does not regress the no-timeout
        path."""
        two_phase, grid, burn_log = self._build(net_count=2, per_net_step=0.0)
        clock = two_phase._test_clock  # type: ignore[attr-defined]
        # Issue #2597: Disable rip-up cohort stagnation detection for this
        # test.  The fake grid pins overflow at 5 across every iteration
        # which would naturally trip the new detector on iter 2 (same
        # cohort, 0 % overflow improvement).  We want the test to exercise
        # the no-timeout / max_iterations path, so set the overflow-delta
        # threshold to ``0.0`` — only an outright regression will fire the
        # detector, and the fake grid never regresses.  (Setting the
        # Jaccard threshold to >1.0 would not help because the cohort is a
        # subset of itself, which always satisfies cohort_stable.)
        two_phase.rules.stagnation_overflow_delta_threshold = 0.0

        with (
            patch("kicad_tools.router.algorithms.two_phase.time.time", clock),
            patch(
                "kicad_tools.router.algorithms.NegotiatedRouter",
                FakeNegotiatedRouter,
            ),
        ):
            net_order = list(two_phase.nets.keys())
            two_phase._detailed_negotiated(
                net_order=net_order,
                corridor_penalty=5.0,
                timeout=None,
                start_time=1000.0,
                max_iterations=3,
                patience=99,  # disable early stop
            )

        captured = capsys.readouterr()
        # No timeout messages should appear.
        assert "Timeout during" not in captured.out
        assert "Timeout at iteration" not in captured.out
        # All 3 iterations should have run (overflow stays at 5 forever).
        assert "Iteration 3 complete" in captured.out

    def test_partial_routes_preserved_on_timeout(self):
        """Verify that nets routed before the timeout are preserved as
        partial output (the acceptance criterion in the issue)."""
        two_phase, grid, burn_log = self._build(net_count=4, per_net_step=2.0)
        clock = two_phase._test_clock  # type: ignore[attr-defined]

        with (
            patch("kicad_tools.router.algorithms.two_phase.time.time", clock),
            patch(
                "kicad_tools.router.algorithms.NegotiatedRouter",
                FakeNegotiatedRouter,
            ),
        ):
            net_order = list(two_phase.nets.keys())
            routes = two_phase._detailed_negotiated(
                net_order=net_order,
                corridor_penalty=5.0,
                timeout=5.0,
                start_time=1000.0,
                max_iterations=5,
                patience=2,
            )

        # We must have *some* partial routes preserved — at least the
        # initial pass nets that completed before timeout (2 of 4 at 2s each).
        assert len(routes) > 0


# =============================================================================
# HierarchicalRouter._detailed_negotiated tests
# =============================================================================


class TestHierarchicalTimeoutPropagation:
    """Verify HierarchicalRouter respects ``timeout`` immediately too."""

    def _build(
        self,
        net_count: int = 6,
        per_net_step: float = 5.0,
    ) -> tuple[HierarchicalRouter, FakeGrid, list[float]]:
        grid = FakeGrid(persistent_overflow=5)
        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0

        burn_log: list[float] = []
        clock = _FakeClock(step=0.0, start=1000.0)

        def fake_route_net_with_corridor(net, present_factor, per_net_timeout=None):
            clock.now += per_net_step
            burn_log.append(clock.now)
            return [_make_route(net, tag=f"call{len(burn_log)}")]

        nets = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in range(1, net_count + 1)}
        net_names = {n: f"Net{n}" for n in nets}

        h_router = HierarchicalRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets=nets,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=fake_route_net_with_corridor,
            mark_route=lambda r: None,
        )
        h_router._test_clock = clock  # type: ignore[attr-defined]
        return h_router, grid, burn_log

    def test_timeout_in_inner_reroute_skips_next_iteration(self, capsys):
        # Same setup as the two-phase test: 3 nets * 2s, budget = 10s.
        h_router, grid, burn_log = self._build(net_count=3, per_net_step=2.0)
        clock = h_router._test_clock  # type: ignore[attr-defined]

        with (
            patch("kicad_tools.router.algorithms.hierarchical.time.time", clock),
            patch(
                "kicad_tools.router.algorithms.NegotiatedRouter",
                FakeNegotiatedRouter,
            ),
        ):
            net_order = list(h_router.nets.keys())
            h_router._detailed_negotiated(
                net_order=net_order,
                progress_callback=None,
                timeout=10.0,
                start_time=1000.0,
                per_net_timeout=None,
            )

        captured = capsys.readouterr()
        # Initial pass should complete.
        assert "Initial pass: 3/3 nets" in captured.out
        # Iteration 1 should start, then timeout in the inner loop.
        assert "Iteration 1: ripping up" in captured.out
        assert "Timeout during reroute at net" in captured.out
        # Iteration 2 must NOT start.
        assert "Iteration 2: ripping up" not in captured.out

    def test_per_net_timeout_forwarded(self):
        """Verify ``per_net_timeout`` reaches ``_route_net_with_corridor``.

        Echo of #2307 (which fixed this for two-phase) — same gap existed in
        the hierarchical path.
        """
        captured_per_net: list[float | None] = []

        grid = FakeGrid(persistent_overflow=0)  # converge immediately
        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0

        def fake_route_net_with_corridor(net, present_factor, per_net_timeout=None):
            captured_per_net.append(per_net_timeout)
            return [_make_route(net)]

        h_router = HierarchicalRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets={1: [("R1", "1"), ("R1", "2")]},
            net_names={1: "Net1"},
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=fake_route_net_with_corridor,
            mark_route=lambda r: None,
        )

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            h_router._detailed_negotiated(
                net_order=[1],
                progress_callback=None,
                timeout=None,
                start_time=0.0,
                per_net_timeout=42.0,
            )

        # All initial-pass calls must have received ``per_net_timeout=42.0``.
        assert captured_per_net == [42.0]
