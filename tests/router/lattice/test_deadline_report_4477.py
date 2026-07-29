"""Bounded per-link termination + reporting stats (Issue #4477, epic #4465).

Phase 4 of ``kct route --complete``.  Phase 2 (#4472) added a per-link
wall-clock ``deadline`` to :meth:`LatticePathfinder.route_netset`; this phase
adds two properties on top of it that a completion report can consume:

1. A connection that is never reached before the deadline is declined with
   the honest reason ``"deadline-exceeded"`` in :attr:`failure_reasons` --
   never silently omitted (which would make an unroutable link
   undiagnosable).  This covers BOTH corners: the deadline expiring mid-pass
   (some connections attempted, the rest stamped) and expiring before the
   very first pass ever runs (every connection stamped).
2. :class:`LatticeNegotiationStats` carries ``deadline_hit`` / ``elapsed_s`` /
   ``budget_s`` so a caller can report "elapsed Xs of a Ys budget" without
   re-deriving timing from scratch.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _pad(x: float, y: float, net: int, *, ref: str, pin: str = "1") -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _pf(*pads: Pad) -> LatticePathfinder:
    return LatticePathfinder(
        [(0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0)],
        list(pads),
        DesignRules(),
        LayerStack.two_layer(),
    )


class TestStatsCarryTiming:
    """LatticeNegotiationStats.deadline_hit / elapsed_s / budget_s."""

    def test_unbudgeted_run_has_no_budget(self):
        a1, a2 = _pad(10, 10, 1, ref="A1"), _pad(20, 10, 1, ref="A2")
        pf = _pf(a1, a2)
        _routes, stats = pf.route_netset([((1, 0), a1, a2, None)], max_iterations=4)
        assert stats.deadline_hit is False
        assert stats.budget_s is None
        assert stats.elapsed_s >= 0.0

    def test_budgeted_run_reports_budget_and_elapsed(self):
        a1, a2 = _pad(10, 10, 1, ref="A1"), _pad(20, 10, 1, ref="A2")
        pf = _pf(a1, a2)
        deadline = time.monotonic() + 3600.0
        _routes, stats = pf.route_netset(
            [((1, 0), a1, a2, None)], max_iterations=4, deadline=deadline
        )
        assert stats.deadline_hit is False
        assert stats.budget_s is not None and stats.budget_s > 3000.0
        assert stats.elapsed_s >= 0.0


class TestDeadlineExceededNeverSilent:
    """Every connection the deadline cut off gets an honest reason."""

    def test_already_expired_stamps_every_connection(self):
        # Case 1: the budget is spent before the FIRST pass ever runs -- no
        # pass computed a single ``reasons`` entry, so the (pre-#4477)
        # behaviour would silently return an empty ``failure_reasons``.
        a1, a2 = _pad(10, 10, 1, ref="A1"), _pad(20, 10, 1, ref="A2")
        b1, b2 = _pad(50, 10, 2, ref="B1"), _pad(60, 10, 2, ref="B2")
        pf = _pf(a1, a2, b1, b2)
        connections = [((1, 0), a1, a2, None), ((2, 0), b1, b2, None)]
        routes, stats = pf.route_netset(
            connections, max_iterations=8, deadline=time.monotonic() - 1.0
        )
        assert routes == {}
        assert stats.routed == 0
        assert stats.converged is False
        assert stats.deadline_hit is True
        # BOTH connections are diagnosable -- neither is silently omitted.
        assert pf.failure_reasons == {(1, 0): "deadline-exceeded", (2, 0): "deadline-exceeded"}

    def test_mid_pass_expiry_stamps_only_the_unattempted_tail(self):
        # Case 2: the deadline is hit PARTWAY through the first pass.  Item 0
        # is attempted (real decline/success reason); every later item is
        # stamped "deadline-exceeded" rather than silently dropped.
        a1, a2 = _pad(10, 10, 1, ref="A1"), _pad(20, 10, 1, ref="A2")
        b1, b2 = _pad(50, 10, 2, ref="B1"), _pad(60, 10, 2, ref="B2")
        pf = _pf(a1, a2, b1, b2)
        connections = [((1, 0), a1, a2, None), ((2, 0), b1, b2, None)]

        # Deterministic clock: call #1 is ``start_time``; call #2 is the
        # top-of-loop deadline check (before pass 0 -- not yet expired);
        # call #3 is the mid-pass check before item 0 (not yet expired, so
        # item 0 is attempted for real); call #4 is the mid-pass check
        # before item 1 (now past the deadline); call #5 is the trailing
        # ``elapsed_s`` computation.  ``_route_impl`` makes no ``monotonic``
        # calls of its own (grep-verified), so this sequence is exact.
        clock = iter([0.0, 1.0, 2.0, 100.0, 150.0])
        with patch("kicad_tools.router.lattice.pathfinder.time.monotonic", lambda: next(clock)):
            routes, stats = pf.route_netset(connections, max_iterations=1, deadline=50.0)

        assert stats.deadline_hit is True
        assert stats.converged is False
        # Item 0 got a REAL attempt (present in either routes or with a
        # genuine decline reason -- either way, NOT "deadline-exceeded").
        assert pf.failure_reasons.get((1, 0)) != "deadline-exceeded" or (1, 0) in routes
        # Item 1 was never reached: honestly stamped, not silently dropped.
        assert pf.failure_reasons[(2, 0)] == "deadline-exceeded"
        assert (2, 0) not in routes
