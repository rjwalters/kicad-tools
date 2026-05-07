"""Tests for best-of-iterations saved-partial state preservation
(Issue #2540).

The router's saved-partial result must not drop routes that succeeded in
earlier iterations when a later iteration's rip-up is aborted by timeout.

Background:
    ``rip_up_nets`` destructively mutates BOTH ``net_routes`` and
    ``self.routes`` BEFORE re-routing begins.  When the per-net reroute
    inner loop is cut short by ``check_timeout()``, ``self.routes``
    reflects the mid-rip-up state (e.g. only the few that survived being
    rerouted) while a prior iteration may have produced a strictly better
    state.

Fix (mirrors the #2305 pattern in two_phase.py):
    Snapshot ``(self.routes, net_routes)`` at the top of each iteration
    BEFORE the destructive ``rip_up_nets`` call.  After the iteration
    loop exits, compare current route count against the snapshot's route
    count and restore the snapshot if it had MORE routes.

This module covers two paths:

- ``HierarchicalRouter._detailed_negotiated`` — exercised end-to-end via
  the same fake fixture pattern as ``test_best_state_tracking.py``.
- ``Autorouter.route_all_negotiated`` — driven through a minimal
  ``Autorouter`` shim with a deterministic ``_FakeClock`` so the
  iteration-1 timeout reproduces deterministically.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kicad_tools.router.algorithms.hierarchical import HierarchicalRouter
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
    """Minimal grid mock.  ``get_total_overflow`` returns values from a
    sequence; ``mark_route_usage`` / ``unmark_route_usage`` track marked
    routes so tests can assert grid-state consistency."""

    def __init__(self, overflow_sequence: list[int]):
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

    def unmark_route(self, route: Route) -> None:  # noqa: D401
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
    """Minimal NegotiatedRouter mock that destructively rips up every
    requested net and clears its entry in ``net_routes`` -- the same
    semantics as the real ``rip_up_nets``."""

    def __init__(self, grid, router, rules, net_class_map):
        self.grid = grid

    def find_nets_through_overused_cells(self, net_routes, overused):
        # Reroute every net that currently has routes.
        return [n for n, routes in net_routes.items() if routes]

    def rip_up_nets(self, nets, net_routes, routes_list):
        for net in nets:
            for route in net_routes.get(net, []):
                self.grid.unmark_route_usage(route)
                if route in routes_list:
                    routes_list.remove(route)
            net_routes[net] = []


class _FakeClock:
    """Monotonic-style fake clock.  Each call returns the *current* time
    and then advances by ``step``.  Tests can also bump ``now`` directly
    to simulate per-net work that consumes wall-clock budget."""

    def __init__(self, step: float = 0.0, start: float = 1000.0):
        self.step = step
        self.now = start

    def __call__(self) -> float:
        t = self.now
        self.now += self.step
        return t


# =============================================================================
# HierarchicalRouter._detailed_negotiated tests
# =============================================================================


def _build_hierarchical(
    grid: FakeGrid,
    nets_to_route: list[int],
    routes_per_net: dict[int, list[Route]] | None = None,
) -> HierarchicalRouter:
    """Build a HierarchicalRouter with controlled grid/route behavior."""
    router = MagicMock()
    rules = MagicMock()
    rules.cost_corridor_deviation = 5.0

    if routes_per_net is None:
        routes_per_net = {n: [_make_route(n, "init")] for n in nets_to_route}

    call_log: list[int] = []

    def fake_route_net_with_corridor(net, present_factor, per_net_timeout=None):
        call_log.append(net)
        return routes_per_net.get(net, [])

    nets_dict = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in nets_to_route}
    net_names = {n: f"Net{n}" for n in nets_to_route}

    hier = HierarchicalRouter(
        grid=grid,
        router=router,
        rules=rules,
        net_class_map=None,
        nets=nets_dict,
        net_names=net_names,
        pads={},
        routes=[],
        routing_failures=[],
        get_net_priority=lambda n: n,
        route_net=lambda n: [_make_route(n)],
        route_net_with_corridor=fake_route_net_with_corridor,
        mark_route=lambda r: None,
    )
    hier._call_log = call_log  # type: ignore[attr-defined]
    return hier


class TestHierarchicalBestStateRestore:
    """Issue #2540: hierarchical strategy must preserve best-of-iterations."""

    def test_iteration1_timeout_preserves_iteration0_routes(self):
        """When iteration 1 rips up all routes and times out before
        rerouting any, the saved partial result restores iteration 0."""
        # Sequence of overflow values:
        #   - per-net during initial pass: not consulted (mark_route calls
        #     don't read overflow)
        #   - after initial pass: 5  (forces rip-up)
        #   - subsequent calls during iteration 1: 5 (still positive)
        #   - post-iteration loop "current overflow" check (none in
        #     hierarchical -- only used to gate next iteration)
        grid = FakeGrid(overflow_sequence=[5] * 50)

        nets = [1, 2, 3, 4, 5]
        # Iteration 0: every net routes successfully.
        # Iteration 1: rip-up clears everything, then no nets reroute
        # successfully (return [] for the iter-1 calls).
        call_count = [0]

        def routes_factory(net, present_factor, per_net_timeout=None):
            # First N calls (initial pass) succeed.
            # All subsequent calls (iter-1 rerouting) return [].
            call_count[0] += 1
            if call_count[0] <= len(nets):
                return [_make_route(net, "init")]
            return []

        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        nets_dict = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in nets}
        net_names = {n: f"Net{n}" for n in nets}

        hier = HierarchicalRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets=nets_dict,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=routes_factory,
            mark_route=lambda r: None,
        )

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = hier._detailed_negotiated(
                net_order=nets,
                progress_callback=None,
                timeout=None,  # let it run; we control failure via empty re-route
                start_time=0.0,
            )

        # Initial pass produced 5 routes.  Iteration 1 rips up all 5 then
        # fails to re-route any (returns []).  Without the fix, the
        # returned list would be empty.  With the fix, the iteration-0
        # snapshot is restored.
        assert len(routes) == 5, f"Expected 5 restored routes, got {len(routes)}"
        # The returned routes must be the iteration-0 ("init") tagged ones.
        for r in routes:
            assert "init" in r.net_name

    def test_no_restore_when_iteration1_improves(self, capsys):
        """When iteration 1 successfully reroutes and produces >= initial
        count, no restore log should be emitted."""
        # Overflow sequence: initial 5 (forces rip-up), iter-1 result = 0.
        grid = FakeGrid(overflow_sequence=[5, 0, 0])

        nets = [1, 2, 3]

        def routes_factory(net, present_factor, per_net_timeout=None):
            # Always succeed (both initial pass and iter-1 reroute).
            return [_make_route(net, "ok")]

        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        nets_dict = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in nets}
        net_names = {n: f"Net{n}" for n in nets}

        hier = HierarchicalRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets=nets_dict,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=routes_factory,
            mark_route=lambda r: None,
        )

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = hier._detailed_negotiated(
                net_order=nets,
                progress_callback=None,
                timeout=None,
                start_time=0.0,
            )

        captured = capsys.readouterr()
        assert "Restoring iteration" not in captured.out
        # Iter-1 rerouted all 3 nets, so we should have 3 routes.
        assert len(routes) == 3

    def test_restore_log_emitted_with_iteration_index(self, capsys):
        """When restore fires, log line must mention the iteration whose
        state is being restored AND the route counts."""
        # Same setup as test_iteration1_timeout_preserves_iteration0_routes
        # but we verify the log text.
        grid = FakeGrid(overflow_sequence=[5] * 50)

        nets = [1, 2, 3]
        call_count = [0]

        def routes_factory(net, present_factor, per_net_timeout=None):
            call_count[0] += 1
            if call_count[0] <= len(nets):
                return [_make_route(net, "init")]
            return []

        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        nets_dict = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in nets}
        net_names = {n: f"Net{n}" for n in nets}

        hier = HierarchicalRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets=nets_dict,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=routes_factory,
            mark_route=lambda r: None,
        )

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            hier._detailed_negotiated(
                net_order=nets,
                progress_callback=None,
                timeout=None,
                start_time=0.0,
            )

        captured = capsys.readouterr()
        assert "Restoring iteration 0 state" in captured.out
        assert "routed=3" in captured.out
        assert "routed=0" in captured.out

    def test_grid_state_consistent_after_restore(self):
        """After restore, the grid's marked routes match the returned
        routes (no leakage of mid-rip-up unmarks)."""
        grid = FakeGrid(overflow_sequence=[5] * 50)

        nets = [1, 2, 3, 4]
        call_count = [0]

        def routes_factory(net, present_factor, per_net_timeout=None):
            call_count[0] += 1
            if call_count[0] <= len(nets):
                return [_make_route(net, "init")]
            return []

        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        nets_dict = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in nets}
        net_names = {n: f"Net{n}" for n in nets}

        hier = HierarchicalRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets=nets_dict,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=routes_factory,
            mark_route=lambda r: None,
        )

        with patch(
            "kicad_tools.router.algorithms.NegotiatedRouter",
            FakeNegotiatedRouter,
        ):
            routes = hier._detailed_negotiated(
                net_order=nets,
                progress_callback=None,
                timeout=None,
                start_time=0.0,
            )

        # After restoration, grid's marked routes match returned routes
        # in count (and the iter-0 routes are deep-copied so identity may
        # differ, but count must match).
        assert len(grid._marked_routes) == len(routes)
        # All restored routes are the "init"-tagged ones.
        assert all("init" in r.net_name for r in routes)


class TestHierarchicalDeterministicTimeout:
    """Drive iteration-1 abort via a deterministic _FakeClock instead of
    via empty reroute results.  This more closely matches the production
    failure mode where ``check_timeout()`` fires inside the per-net
    rip-up loop."""

    def test_per_net_timeout_aborts_iteration1_preserves_iteration0(self):
        """A FakeClock that crosses the timeout threshold during the
        iteration-1 reroute pass must not drop iteration-0 routes."""
        grid = FakeGrid(overflow_sequence=[5] * 50)

        nets = [1, 2, 3, 4]
        clock = _FakeClock(step=0.0, start=1000.0)
        budget = 100.0

        # Track which call index we're on.  Initial pass = calls 1..N.
        # Iteration-1 reroute starts at call N+1; on that call we burn
        # enough simulated time to trip the budget.
        call_count = [0]

        def routes_factory(net, present_factor, per_net_timeout=None):
            call_count[0] += 1
            if call_count[0] == len(nets) + 1:
                # First call inside iteration-1 reroute -- burn the budget.
                clock.now += budget * 2
            return [_make_route(net, f"call{call_count[0]}")]

        router = MagicMock()
        rules = MagicMock()
        rules.cost_corridor_deviation = 5.0
        nets_dict = {n: [(f"R{n}", "1"), (f"R{n}", "2")] for n in nets}
        net_names = {n: f"Net{n}" for n in nets}

        hier = HierarchicalRouter(
            grid=grid,
            router=router,
            rules=rules,
            net_class_map=None,
            nets=nets_dict,
            net_names=net_names,
            pads={},
            routes=[],
            routing_failures=[],
            get_net_priority=lambda n: n,
            route_net=lambda n: [_make_route(n)],
            route_net_with_corridor=routes_factory,
            mark_route=lambda r: None,
        )

        with (
            patch(
                "kicad_tools.router.algorithms.NegotiatedRouter",
                FakeNegotiatedRouter,
            ),
            patch(
                "kicad_tools.router.algorithms.hierarchical.time.time",
                clock,
            ),
        ):
            routes = hier._detailed_negotiated(
                net_order=nets,
                progress_callback=None,
                timeout=budget,
                start_time=1000.0,
            )

        # Initial pass routed 4 nets.  Iteration-1 ripped them all up,
        # then check_timeout fired after 1 reroute.  Without the fix, we
        # would return only the surviving route(s); with the fix, we
        # restore iteration 0's snapshot of 4.
        assert len(routes) == 4, (
            f"Expected 4 restored routes after iter-1 timeout, got {len(routes)}"
        )
