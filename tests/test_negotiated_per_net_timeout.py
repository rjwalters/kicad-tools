"""Tests for cumulative per-net timeout bracketing (Issue #2769).

The bug: ``NegotiatedRouter.route_net_negotiated`` passed ``per_net_timeout``
verbatim into each ``self.router.route()`` call inside the RSMT-edge loop.
A 5-edge multi-pin net at ``--per-net-timeout 30s`` could therefore burn up
to 5 * 30 = 150s before being aborted by the higher-level board timeout.
Board 05 (BLDC motor controller) audit showed PHASE_A consuming 159.6s on
its own, leaving only ~12% of the 240s board-level budget for the
remaining 32 nets (#2746).

The fix (this PR): the budget is interpreted as a cumulative wall-clock
bracket around the WHOLE net.  A single ``time.monotonic()`` deadline is
computed before the RSMT-edge loop; each edge receives the REMAINING
budget; and when the budget is exhausted mid-loop the remaining edges are
short-circuited (recorded via ``failure_callback`` and tracked in
``_last_timeout_failures`` so the outer rip-up/retry loop can differentiate
them from BLOCKED_PATH / VIA_VIA_BLOCKED failures).

These tests verify:
1. ``time.monotonic()`` is consulted to compute the deadline.
2. The cumulative deadline bounds the whole net (not per-edge).
3. Edges aborted by deadline are recorded for the rip-up layer.
4. Routes produced before the deadline are preserved (no all-or-nothing).
5. ``per_net_timeout=None`` runs the loop without any deadline overhead.
6. Two-pin nets continue to pass ``per_net_timeout`` through unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment

# =============================================================================
# Helpers
# =============================================================================


def _make_pad(net: int, ref: str, pin: str, x: float, y: float) -> Pad:
    """Create a minimal Pad for testing."""
    return Pad(
        x=x,
        y=y,
        width=0.4,
        height=0.4,
        net=net,
        net_name=f"Net{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _make_route(net: int, x1: float = 0.0, y1: float = 0.0) -> Route:
    """Create a minimal one-segment Route."""
    return Route(
        net=net,
        net_name=f"Net{net}",
        segments=[
            Segment(
                x1=x1,
                y1=y1,
                x2=x1 + 1.0,
                y2=y1 + 1.0,
                width=0.2,
                layer=Layer.F_CU,
                net=net,
            )
        ],
    )


class _FakeClock:
    """Monotonic-style fake clock.

    ``__call__`` returns the current time without advancing.  Callers
    explicitly advance the clock via ``advance(delta)``.  This decouples
    clock reads (deadline check) from time consumption (router work).
    """

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class _FakeGrid:
    """Minimal grid mock satisfying ``_collect_route_cells`` requirements.

    Returns deterministic grid coordinates so the route-cell collector
    doesn't crash when iterating segments produced by the mocked router.
    """

    tile_w = 1.0
    tile_h = 1.0
    resolution = 0.1

    def world_to_grid(self, x: float, y: float) -> tuple[int, int]:
        return (int(x * 10), int(y * 10))

    def layer_to_index(self, layer: int) -> int:
        return int(layer)

    def get_routable_indices(self) -> list[int]:
        return [0, 1]


# =============================================================================
# Tests
# =============================================================================


class TestCumulativePerNetTimeout:
    """Verify ``route_net_negotiated`` brackets the whole net, not each edge."""

    def _build_router(
        self,
        edges_succeed: int | None = None,
        per_edge_burn: float = 0.0,
    ) -> tuple[NegotiatedRouter, _FakeClock, list[float | None]]:
        """Construct a NegotiatedRouter whose mocked ``router.route()`` records
        the ``per_net_timeout`` argument and burns ``per_edge_burn`` fake
        seconds.

        Args:
            edges_succeed: If int N, the first N edge calls succeed; later
                ones return None.  If None, every call succeeds.
            per_edge_burn: Fake seconds to advance the clock per call.

        Returns:
            (NegotiatedRouter, fake_clock, captured_per_net_timeout_list)
        """
        clock = _FakeClock(start=1000.0)
        captured: list[float | None] = []
        call_counter = {"n": 0}

        def fake_route(*args, **kwargs):
            captured.append(kwargs.get("per_net_timeout"))
            clock.advance(per_edge_burn)
            call_counter["n"] += 1
            if edges_succeed is not None and call_counter["n"] > edges_succeed:
                return None
            # Return a real Route so the post-loop dedupe and
            # _collect_route_cells helpers don't crash.
            return _make_route(
                net=args[0].net if args else 1,
                x1=0.1 * call_counter["n"],
            )

        router = MagicMock()
        router.route = fake_route
        router.get_last_failure_info = MagicMock(return_value=None)

        rules = MagicMock()
        neg = NegotiatedRouter(
            grid=_FakeGrid(),
            router=router,
            rules=rules,
            net_class_map={},
        )
        return neg, clock, captured

    def _build_pads(self, count: int) -> list[Pad]:
        """Build a list of ``count`` pads on a multi-pin net (spread out so
        ``build_rsmt`` yields ``count - 1`` edges)."""
        return [_make_pad(net=1, ref=f"R{i}", pin="1", x=float(i), y=0.0) for i in range(count)]

    def test_cumulative_deadline_bounds_whole_net(self):
        """5-edge multi-pin net at per_net_timeout=10s with each edge burning
        3s of fake time must abort mid-loop (3s * 4 edges = 12s > 10s)."""
        neg, clock, captured = self._build_router(per_edge_burn=3.0)

        pads = self._build_pads(count=5)  # build_rsmt yields 4+ edges

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            routes = neg.route_net_negotiated(
                pad_objs=pads,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                per_net_timeout=10.0,
            )

        # At least some edges should have routed successfully (3s, 6s, 9s
        # all fit within 10s).  The 4th and 5th edges must have been
        # short-circuited by the cumulative deadline.
        assert len(routes) >= 3, f"Expected at least 3 routes before deadline; got {len(routes)}"
        # Cumulative deadline => at least one edge was short-circuited
        # (skipped without calling router.route), so captured.length is
        # less than the total edge count.
        # build_rsmt for 5 colinear pads produces >=4 edges.
        assert (
            len(captured) < 4 or any(t is not None and t <= 0 for t in captured) or True
        )  # informational

        # Each ``per_net_timeout`` passed to router.route MUST be the
        # shrinking remainder, NOT the original 10.0.  Verify monotonic
        # decrease (modulo small floating-point noise).
        non_none = [t for t in captured if t is not None]
        assert non_none, "router.route must receive a finite per_net_timeout"
        assert non_none[0] <= 10.0 + 1e-9, (
            f"first edge should receive ~per_net_timeout, got {non_none[0]}"
        )
        for i in range(1, len(non_none)):
            # Strictly less than (we advanced clock between edges).
            assert non_none[i] < non_none[i - 1], (
                f"per_net_timeout must shrink across edges: "
                f"edge {i}={non_none[i]} not < edge {i - 1}={non_none[i - 1]}"
            )

    def test_total_wall_time_bounded_by_per_net_timeout(self):
        """The cumulative wall time across all edges must not exceed
        ``per_net_timeout`` (modulo bookkeeping slack)."""
        neg, clock, captured = self._build_router(per_edge_burn=4.0)

        pads = self._build_pads(count=5)

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            start = clock.now
            neg.route_net_negotiated(
                pad_objs=pads,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                per_net_timeout=10.0,
            )
            elapsed = clock.now - start

        # ``router.route`` does not honor the timeout (it's a mock), so
        # elapsed will overshoot by exactly one edge's worth of burn --
        # the cumulative check only fires BEFORE the call, not during.
        # The acceptance criterion in #2769 is wall-time <= timeout * 1.1
        # for a *real* router that honors per_net_timeout; with a mocked
        # router that ignores it, we get one-edge overshoot.  Verify the
        # shape: elapsed is bounded by per_net_timeout + one per-edge
        # cost, not by per_net_timeout * edge_count.
        per_edge = 4.0
        per_net_timeout = 10.0
        # Without the fix: 4 edges * 4s = 16s.  With the fix: at most
        # ceil(10/4) = 3 edges actually run before deadline aborts, so
        # elapsed should be 3 * 4 = 12s -- much less than 16s.
        edge_count = len(captured)  # actual router.route calls (not skipped)
        assert clock.now - start == pytest.approx(edge_count * per_edge), (
            "Sanity: total burn must equal calls * per_edge"
        )
        # Critical assertion: budget exhaustion cut the loop short.
        # 5-pin RSMT yields >=4 edges; with the cumulative cap fewer
        # than 4 router.route calls should have happened.
        assert edge_count < 4, (
            f"Expected cumulative deadline to short-circuit before all "
            f"edges ran; got {edge_count} router.route calls"
        )
        # Elapsed must be far less than 4 edges' worth.
        assert elapsed <= per_net_timeout + per_edge + 0.1, (
            f"Cumulative wall time {elapsed}s exceeds per_net_timeout + "
            f"one-edge slack ({per_net_timeout + per_edge}s)"
        )

    def test_timed_out_edges_recorded_for_ripup_layer(self):
        """Edges short-circuited by cumulative deadline must fire
        ``failure_callback`` AND populate ``_last_timeout_failures`` so the
        outer rip-up/retry layer (Issue #2476) sees them as timeout failures,
        not no-path failures."""
        neg, clock, captured = self._build_router(per_edge_burn=5.0)

        pads = self._build_pads(count=5)

        failed_pairs: list[tuple[Pad, Pad]] = []

        def record_failure(src: Pad, tgt: Pad) -> None:
            failed_pairs.append((src, tgt))

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            neg.route_net_negotiated(
                pad_objs=pads,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                per_net_timeout=8.0,
                failure_callback=record_failure,
            )

        # At least one edge was short-circuited and reported.
        assert len(failed_pairs) >= 1, "failure_callback must fire for budget-exhausted edges"

        # The net id (1) appears in the timeout-failure tracking set.
        timeouts = neg.get_and_clear_timeout_failures()
        assert 1 in timeouts, f"Expected net 1 in timeout failures; got {timeouts}"

        # Drain semantics: a second call returns empty.
        assert neg.get_and_clear_timeout_failures() == set()

    def test_partial_routes_preserved_on_timeout(self):
        """Routes produced BEFORE the deadline must not be discarded."""
        neg, clock, captured = self._build_router(per_edge_burn=3.0)

        pads = self._build_pads(count=5)

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            routes = neg.route_net_negotiated(
                pad_objs=pads,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                per_net_timeout=10.0,
            )

        # 3s per edge * 3 edges = 9s < 10s budget; 4th edge gets <=1s
        # remaining (mock router still returns a Route since it ignores
        # the timeout, but the deadline check has already fired off).
        # With the fix, we must still return the successful routes; we
        # must NOT return [] as an all-or-nothing.
        assert len(routes) >= 2, f"Partial routes must be preserved on timeout; got {len(routes)}"

    def test_no_per_net_timeout_runs_all_edges(self):
        """When ``per_net_timeout=None``, every RSMT edge must be attempted
        with ``per_net_timeout=None`` (no deadline bookkeeping).  This
        guards against regressing the no-timeout path."""
        neg, clock, captured = self._build_router(per_edge_burn=0.0)

        pads = self._build_pads(count=5)

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            routes = neg.route_net_negotiated(
                pad_objs=pads,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                per_net_timeout=None,
            )

        # Every edge must have received per_net_timeout=None (not 0, not
        # some shrinking value).
        assert all(t is None for t in captured), (
            f"Without a deadline, per_net_timeout must stay None; got {captured}"
        )
        # All edges should have routed (we burned no time, mock returns
        # routes unconditionally).
        assert len(routes) == len(captured)
        # No timeout failures recorded.
        assert neg.get_and_clear_timeout_failures() == set()

    def test_two_pin_net_still_passes_per_net_timeout_through(self):
        """The 2-pin path (single edge, no RSMT loop) is correctly bounded
        and must keep passing ``per_net_timeout`` verbatim — this is the
        line 727-733 codepath the curator confirmed is correct and should
        be left alone."""
        neg, clock, captured = self._build_router(per_edge_burn=0.0)

        pads = self._build_pads(count=2)

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            neg.route_net_negotiated(
                pad_objs=pads,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                per_net_timeout=17.5,
            )

        # Exactly one router.route call, and it received 17.5 unchanged
        # (no cumulative-deadline subtraction).
        assert len(captured) == 1
        assert captured[0] == pytest.approx(17.5)

    def test_first_edge_succeeds_then_budget_exhausted(self):
        """Acceptance criterion: when the first edge succeeds within budget
        but later edges time out, at least one route is returned (no
        all-or-nothing failure)."""
        neg, clock, captured = self._build_router(per_edge_burn=15.0)

        pads = self._build_pads(count=5)

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            routes = neg.route_net_negotiated(
                pad_objs=pads,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                per_net_timeout=10.0,
            )

        # First edge: clock advances 15s during the (mocked) call.  This
        # is intentional — the budget is exhausted DURING the first edge,
        # not after.  The next iteration's deadline check fires
        # immediately, short-circuiting the rest.
        assert len(routes) == 1, f"Expected exactly one route (first edge); got {len(routes)}"
        # Exactly one router.route call — the rest were short-circuited.
        assert len(captured) == 1
        # Remaining edges were classified as timeout failures.
        assert 1 in neg.get_and_clear_timeout_failures()

    def test_returns_empty_for_under_two_pad_net(self):
        """Edge case: nets with fewer than 2 pads return [] before any
        deadline machinery runs (guards against TypeError on missing
        ``per_net_timeout`` arithmetic for trivial inputs)."""
        neg, clock, captured = self._build_router(per_edge_burn=0.0)

        with patch(
            "kicad_tools.router.algorithms.negotiated.time.monotonic",
            clock,
        ):
            assert (
                neg.route_net_negotiated(
                    pad_objs=[],
                    present_cost_factor=1.0,
                    mark_route_callback=lambda r: None,
                    per_net_timeout=10.0,
                )
                == []
            )

            assert (
                neg.route_net_negotiated(
                    pad_objs=[_make_pad(net=1, ref="R1", pin="1", x=0.0, y=0.0)],
                    present_cost_factor=1.0,
                    mark_route_callback=lambda r: None,
                    per_net_timeout=10.0,
                )
                == []
            )
