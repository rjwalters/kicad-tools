"""Per-net timeout enforcement in the MST / RSMT edge loop (Issue #3485).

The bug: ``--per-net-timeout`` was plumbed into
``NegotiatedRouter.route_net_negotiated`` (Issue #2769) but NOT into the
sibling ``MSTRouter.route_net`` / ``route_net_star`` paths nor the
``Autorouter._route_net_with_mst_edges`` edge loop.  Those call
``self.router.route()`` WITHOUT a ``per_net_timeout`` argument, so a single
pathological multi-terminal Steiner net (softstart's VGATE, 8 pads) could
grind for 20-30 minutes inside the negotiated rip-up reroute loop despite an
explicit ``--per-net-timeout 60`` -- the per-edge A* searches never received
the deadline.

The fix (this PR): the per-net budget is interpreted as a CUMULATIVE
wall-clock bracket around the WHOLE net (matching #2769).  A single
``time.monotonic()`` deadline is computed before the edge loop; each edge's
``self.router.route()`` receives the REMAINING budget; and edges that arrive
after the budget is exhausted are short-circuited (recorded via
``failure_callback`` so the rip-up/retry layer still sees them).

These tests verify, against a mocked router so they are fast and
deterministic:

1. ``per_net_timeout`` reaches each per-edge ``router.route()`` call.
2. The cumulative deadline bounds the whole net (not per-edge), so a grindy
   net is abandoned within a bounded multiple of the budget rather than
   ``per_net_timeout * len(edges)``.
3. Edges short-circuited by the deadline still fire ``failure_callback``.
4. Routes produced before the deadline are preserved (no all-or-nothing).
5. ``per_net_timeout=None`` runs every edge unbudgeted (no regression).
6. The star-topology path honours the same bracketing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kicad_tools.router.algorithms.mst import MSTRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment

# =============================================================================
# Helpers
# =============================================================================


def _make_pad(net: int, ref: str, pin: str, x: float, y: float) -> Pad:
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


def _make_route(net: int, x1: float = 0.0) -> Route:
    return Route(
        net=net,
        net_name=f"Net{net}",
        segments=[
            Segment(
                x1=x1,
                y1=0.0,
                x2=x1 + 1.0,
                y2=1.0,
                width=0.2,
                layer=Layer.F_CU,
                net=net,
            )
        ],
    )


class _FakeClock:
    """Monotonic-style fake clock advanced explicitly by the test."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def _build_mst_router(
    clock: _FakeClock,
    per_edge_burn: float = 0.0,
    edges_succeed: int | None = None,
) -> tuple[MSTRouter, list[float | None]]:
    """Construct an MSTRouter whose mocked ``router.route()`` records the
    ``per_net_timeout`` it received and burns ``per_edge_burn`` fake seconds.

    Args:
        clock: shared fake clock; ``router.route`` advances it per call.
        per_edge_burn: fake seconds consumed by each router.route call.
        edges_succeed: if int N, the first N calls succeed; later calls
            return None.  If None, every call succeeds.

    Returns:
        (MSTRouter, captured_per_net_timeout_list)
    """
    captured: list[float | None] = []
    call_counter = {"n": 0}

    def fake_route(*args, **kwargs):
        captured.append(kwargs.get("per_net_timeout"))
        clock.advance(per_edge_burn)
        call_counter["n"] += 1
        if edges_succeed is not None and call_counter["n"] > edges_succeed:
            return None
        return _make_route(net=args[0].net if args else 1, x1=0.1 * call_counter["n"])

    router = MagicMock()
    router.route = fake_route

    return (
        MSTRouter(grid=MagicMock(), router=router, rules=MagicMock(), net_class_map={}),
        captured,
    )


def _colinear_pads(count: int) -> list[Pad]:
    """``count`` colinear pads -> ``build_mst`` yields ``count - 1`` edges."""
    return [_make_pad(net=1, ref=f"R{i}", pin="1", x=float(i), y=0.0) for i in range(count)]


# =============================================================================
# route_net (MST topology) tests
# =============================================================================


class TestMstRouteNetPerNetTimeout:
    def test_per_net_timeout_reaches_each_edge(self, monkeypatch):
        """With a budget set, every per-edge ``router.route`` receives a finite
        ``per_net_timeout`` (the shrinking remainder), not ``None``."""
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=0.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(4)  # 3 MST edges
        mst.route_net(
            pads,
            mark_route_callback=lambda r: None,
            use_steiner=False,
            per_net_timeout=30.0,
        )

        assert len(captured) == 3, f"expected 3 edge attempts, got {len(captured)}"
        assert all(t is not None for t in captured), (
            f"every edge must receive a finite budget; got {captured}"
        )
        assert captured[0] == pytest.approx(30.0)

    def test_cumulative_deadline_bounds_whole_net(self, monkeypatch):
        """A grindy net (each edge burns 25s, budget 30s) must be abandoned
        after ~2 edges, NOT run all edges for 25s each."""
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=25.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(6)  # 5 MST edges
        start = clock.now
        mst.route_net(
            pads,
            mark_route_callback=lambda r: None,
            use_steiner=False,
            per_net_timeout=30.0,
        )
        elapsed = clock.now - start

        # First edge runs (budget full).  After it the clock is at 25s; the
        # second edge sees remaining=5s and runs (mock ignores the deadline,
        # burns 25s more -> 50s).  The third edge sees remaining<0 and is
        # short-circuited.  So exactly 2 router.route calls happen.
        assert len(captured) == 2, (
            f"cumulative deadline must short-circuit after the budget is "
            f"exhausted; got {len(captured)} router.route calls"
        )
        # Bounded by budget + one grindy edge's overrun -- NOT 5 * 25 = 125s.
        assert elapsed <= 30.0 + 25.0 + 0.1, (
            f"total wall time {elapsed}s exceeds budget + one-edge slack; "
            f"per-net timeout was not enforced cumulatively"
        )

    def test_timed_out_edges_fire_failure_callback(self, monkeypatch):
        """Edges short-circuited by the deadline must fire failure_callback so
        the rip-up/retry layer still sees them."""
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=40.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(5)  # 4 MST edges
        failed: list[tuple[Pad, Pad]] = []

        mst.route_net(
            pads,
            mark_route_callback=lambda r: None,
            failure_callback=lambda s, t: failed.append((s, t)),
            use_steiner=False,
            per_net_timeout=10.0,
        )

        # First edge burns 40s > 10s budget -> subsequent edges all
        # short-circuited and reported as failures.
        assert len(captured) == 1, f"only the first edge should run; got {len(captured)}"
        assert len(failed) >= 1, "short-circuited edges must fire failure_callback"

    def test_partial_routes_preserved(self, monkeypatch):
        """Routes produced before the deadline are not discarded."""
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=4.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(6)  # 5 MST edges
        routes = mst.route_net(
            pads,
            mark_route_callback=lambda r: None,
            use_steiner=False,
            per_net_timeout=10.0,
        )

        # 4s per edge: edges at 0s, 4s, 8s run (3 routes); the 4th sees
        # remaining<=0 and is short-circuited.  Partial routes survive.
        assert len(routes) >= 2, f"partial routes must be preserved; got {len(routes)}"

    def test_no_timeout_runs_all_edges_unbudgeted(self, monkeypatch):
        """``per_net_timeout=None`` => every edge receives None (no regression,
        no deadline bookkeeping)."""
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=100.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(5)  # 4 MST edges
        routes = mst.route_net(
            pads,
            mark_route_callback=lambda r: None,
            use_steiner=False,
            per_net_timeout=None,
        )

        assert len(captured) == 4, "all edges must run when unbudgeted"
        assert all(t is None for t in captured), (
            f"unbudgeted edges must receive per_net_timeout=None; got {captured}"
        )
        assert len(routes) == 4

    def test_two_pin_net_passes_budget_through(self, monkeypatch):
        """The 2-pin path forwards the full budget verbatim to its single A*."""
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=0.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(2)
        mst.route_net(
            pads,
            mark_route_callback=lambda r: None,
            use_steiner=False,
            per_net_timeout=17.5,
        )

        assert len(captured) == 1
        assert captured[0] == pytest.approx(17.5)


# =============================================================================
# route_net_star tests
# =============================================================================


class TestMstRouteNetStarPerNetTimeout:
    def test_star_cumulative_deadline_bounds_whole_net(self, monkeypatch):
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=25.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(6)  # star: 5 spokes from pad 0
        mst.route_net_star(
            pads,
            mark_route_callback=lambda r: None,
            per_net_timeout=30.0,
        )

        # Same shape as the MST test: exactly 2 router.route calls before the
        # cumulative deadline short-circuits the rest.
        assert len(captured) == 2, (
            f"star path must enforce the cumulative deadline; "
            f"got {len(captured)} router.route calls"
        )

    def test_star_no_timeout_runs_all_spokes(self, monkeypatch):
        clock = _FakeClock()
        mst, captured = _build_mst_router(clock, per_edge_burn=0.0)
        monkeypatch.setattr(
            "kicad_tools.router.algorithms.mst.time.monotonic", clock
        )

        pads = _colinear_pads(4)  # 3 spokes
        mst.route_net_star(
            pads,
            mark_route_callback=lambda r: None,
            per_net_timeout=None,
        )

        assert len(captured) == 3
        assert all(t is None for t in captured)
