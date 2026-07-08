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


# =============================================================================
# Issue #3989 — rip-up-loop backstop integrity
# =============================================================================
#
# ``route_all_negotiated`` takes a stage ``timeout`` (the backstop) and
# enforces it via ``check_timeout()``, which fires only BETWEEN nets.  When a
# net drops to the pure-Python A* fallback on a first-time geometric failure
# (``FAILURE_NO_PATH`` / ``FAILURE_CLEARANCE`` -- NOT covered by #3956's
# resume-exhaustion fast-path), one unbounded call can run ~200 s and overshoot
# a 360 s backstop before the next between-net check runs.
#
# The fix (Option A) derives a per-net A* cap from the budget REMAINING at each
# iteration entry and threads it to every reroute call site.  The cap is
# generous early (a fraction of the plentiful remaining budget) and tightens
# toward ``PER_NET_CAP_FLOOR_S`` as the deadline nears, so the loop can overshoot
# by at most one final net's bounded cap plus per-iteration overhead.

from kicad_tools.router.algorithms import (  # noqa: E402
    PER_NET_CAP_FLOOR_S,
    PER_NET_CAP_STAGE_FRACTION,
    derive_iter_per_net_cap,
    derive_per_net_cap,
)
from kicad_tools.router.core import Autorouter  # noqa: E402


class TestDeriveIterPerNetCap:
    """Unit coverage for the remaining-budget per-net cap derivation."""

    def test_no_stage_budget_returns_standing_cap(self):
        # ``remaining_budget=None`` => legacy unbounded stage: honour only the
        # standing cap (explicit --per-net-timeout, or None for unbounded).
        assert derive_iter_per_net_cap(None, None) is None
        assert derive_iter_per_net_cap(30.0, None) == 30.0

    def test_generous_early_when_budget_plentiful(self):
        # Early in a 600 s stage: 10% of remaining = 60 s, well above floor.
        assert derive_iter_per_net_cap(None, 600.0) == (PER_NET_CAP_STAGE_FRACTION * 600.0)

    def test_curation_worked_example(self):
        # The acceptance-criteria example: timeout=60 => derive_per_net_cap
        # gives the standing cap of 6.0; at iteration entry with the full 60 s
        # still remaining, the iter cap is also 6.0.
        standing = derive_per_net_cap(None, 60.0)
        assert standing == 6.0
        assert derive_iter_per_net_cap(standing, 60.0) == 6.0

    def test_tightens_toward_floor_late_in_stage(self):
        # As the deadline nears, the remaining-budget cap shrinks; below the
        # floor it clamps to PER_NET_CAP_FLOOR_S so a late net still gets a
        # fair (but bounded) share.
        assert derive_iter_per_net_cap(None, 1.0) == PER_NET_CAP_FLOOR_S
        assert derive_iter_per_net_cap(None, 0.0) == PER_NET_CAP_FLOOR_S
        # Already past the deadline (negative remaining) also clamps to floor.
        assert derive_iter_per_net_cap(None, -50.0) == PER_NET_CAP_FLOOR_S

    def test_explicit_cap_binds_when_tighter(self):
        # An operator's explicit --per-net-timeout is never LOOSENED: when it is
        # tighter than the remaining-budget derivation, it binds.
        # 10% of 600 = 60; explicit 30 < 60 => 30 binds.
        assert derive_iter_per_net_cap(30.0, 600.0) == 30.0

    def test_remaining_budget_tightens_explicit_cap_late(self):
        # Late in the stage the remaining-budget cap can drop BELOW an explicit
        # cap -- that is the whole point: the backstop must stay honest even
        # when the operator set a generous per-net budget.
        # 10% of 20 = 2 -> clamps to floor 5.0; explicit 36 > 5 => 5 binds.
        assert derive_iter_per_net_cap(36.0, 20.0) == PER_NET_CAP_FLOOR_S


def _build_slow_net_router() -> Autorouter:
    """Two nets on a 20x20 board.  Net 1 is trivial; net 2 is the
    pathological net whose A* fallback we mock as slow.  Geometry keeps the
    loop iterating via the mocked grid overflow below.
    """
    ar = Autorouter(width=20.0, height=20.0)
    ar.add_component(
        "R1",
        [
            {"number": "1", "x": 2.0, "y": 2.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 18.0, "y": 2.0, "net": 1, "net_name": "NET1"},
        ],
    )
    ar.add_component(
        "R2",
        [
            {"number": "1", "x": 2.0, "y": 18.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 18.0, "y": 18.0, "net": 2, "net_name": "NET2"},
        ],
    )
    return ar


class _ControllableClock:
    """``time.time()`` returns ``self.now`` WITHOUT auto-advancing.  The mocked
    slow net advances ``self.now`` explicitly by however long its (capped) A*
    'runs'.  This drives ``check_timeout()`` deterministically with no real
    sleeping, so the test is instant and load-independent."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class TestRipupLoopBackstopHonored:
    """Issue #3989: a single slow Python-fallback A* call must not let the
    negotiated rip-up loop overshoot its stage backstop unboundedly."""

    def _run(self, timeout: float, per_net_timeout, unbounded_call_cost: float):
        """Drive ``route_all_negotiated`` with net 2's ``_route_net_negotiated``
        mocked to 'run' for ``unbounded_call_cost`` seconds UNLESS a
        ``per_net_timeout`` bounds it -- exactly how a deadline-respecting A*
        behaves.  Returns ``(final_now, start, clock)``.
        """
        ar = _build_slow_net_router()
        clock = _ControllableClock(start=1000.0)

        # Force overflow to persist so the rip-up loop keeps iterating and the
        # slow net is re-attempted each iteration (the #3413/#3448 recovery
        # paths that this fix threads the cap into).
        ar.grid.get_total_overflow = lambda: 5  # type: ignore[method-assign]
        # ``find_overused_cells`` yields (gx, gy, layer, overflow) 4-tuples.
        ar.grid.find_overused_cells = lambda: [(10, 10, 0, 5)]  # type: ignore[method-assign]

        orig = ar._route_net_negotiated

        def slow_fake(net, present_factor, per_net_timeout=None):
            if net != 2:
                return orig(net, present_factor, per_net_timeout=per_net_timeout)
            # A deadline-respecting A*: it 'runs' for its cost, but never past
            # the per-net cap it was handed.  A ``None`` cap (the bug) means an
            # UNBOUNDED run that eats ``unbounded_call_cost`` whole.
            cost = unbounded_call_cost
            if per_net_timeout is not None:
                cost = min(cost, per_net_timeout)
            clock.now += cost
            # Return a partial/empty result so the net stays 'unrouted' and the
            # recovery paths keep re-attempting it (drives repeated slow calls).
            return []

        ar._route_net_negotiated = slow_fake  # type: ignore[method-assign]

        with patch("kicad_tools.router.core.time.time", clock):
            ar.route_all_negotiated(
                max_iterations=8,
                timeout=timeout,
                per_net_timeout=per_net_timeout,
                adaptive=False,
                perturbation=False,
            )
        return clock.now, 1000.0, clock

    def test_single_slow_call_does_not_blow_past_backstop(self):
        """With ``timeout=60`` and ``per_net_timeout=None`` (board-06's recipe),
        a net whose fallback would run 200 s must be capped so the loop's total
        overshoot is bounded to ~one final net's derived cap.

        Acceptance criterion: elapsed <= timeout + derived_cap + epsilon, where
        derived_cap = derive_per_net_cap(None, 60.0) = 6.0.
        """
        final_now, start, _ = self._run(
            timeout=60.0, per_net_timeout=None, unbounded_call_cost=200.0
        )
        elapsed = final_now - start
        derived_cap = derive_per_net_cap(None, 60.0)
        assert derived_cap == 6.0
        # Overshoot bounded to one final net's cap plus a small overhead grace.
        # WITHOUT the fix, one uncapped 200 s call alone lands elapsed >= 200.
        assert elapsed <= 60.0 + derived_cap + 1.0, (
            f"loop overshot backstop: elapsed={elapsed:.1f}s (budget 60 + cap {derived_cap} + eps)"
        )
        # Sanity: it must actually have consumed most of the budget (the slow
        # net really did run), not exit trivially early.
        assert elapsed >= 60.0 - 12.0

    def test_no_stage_budget_leaves_slow_call_unbounded(self):
        """Edge case: ``timeout=None, per_net_timeout=None`` (legacy unbounded)
        derives no cap -- the slow call is NOT bounded.  Confirms the fix does
        not silently impose a cap when no budget exists."""
        final_now, start, _ = self._run(
            timeout=None, per_net_timeout=None, unbounded_call_cost=200.0
        )
        elapsed = final_now - start
        # No backstop => the full 200 s call ran unbounded at least once.
        assert elapsed >= 200.0

    def test_explicit_per_net_timeout_is_respected(self):
        """Edge case: an explicit ``per_net_timeout`` that is TIGHTER than the
        remaining-budget derivation must bind, not be loosened to the derived
        stage fraction."""
        # timeout=360 => standing derived cap would be 36; but the caller passes
        # an explicit 8.0, which is tighter and must bind every call.
        final_now, start, _ = self._run(
            timeout=360.0, per_net_timeout=8.0, unbounded_call_cost=200.0
        )
        elapsed = final_now - start
        # Each slow call is capped at 8s (< 36 derived, < 200 unbounded).  With
        # a 360 s budget the loop runs several iterations but each net call is
        # bounded at 8 s, so it never overshoots by a whole 200 s call.
        assert elapsed <= 360.0 + 8.0 + 1.0
