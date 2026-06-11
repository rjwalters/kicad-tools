"""Tests for the issue #3474 R1 budget-integrity fixes.

Measured failure (chorus-test-revA v21, jlcpcb-tier1, cpp, seed 42,
2026-06-10): the pinned recipe yielded 2/51 strict because three layers
below the per-net cap leaked wall-clock time:

1. **Un-budgeted failure diagnostics**: ``_record_routing_failure`` ran
   ``RootCauseAnalyzer.analyze_routing_failure`` per FAILED EDGE with a
   full-board ``CongestionMap`` scan and unbounded corridor scans --
   ~100-120s of pure Python per failure on a 1240x1240x4 grid.  SPI_SCK
   "routing" measured 247s of which only 10s was A*.
2. **cpp->python fallback double-spend**: when the C++ search consumed
   its whole per-net budget, ``_try_python_fallback`` ran the
   10-100x-slower Python A* with a FRESH copy of the same budget.
3. **Grace pass unproductive burn**: one 0.3s-capped grace attempt
   burned 101.7s inside leak (1), consuming the entire #3452 grace
   budget for 0 routed nets, 44+ skipped.

Fixes under test:

- ``derive_per_net_cap``: stage-budget-derived default per-net cap.
- ``run_initial_pass_grace``: ``budget_s`` fund parameter, single-call
  overrun abort, and no-progress tier exit.
- ``CongestionMap``: ``region`` window + ``stride`` subsampling.
- ``RootCauseAnalyzer._scan_stride`` / bounded corridor scans.
- ``Autorouter._analyze_failure_budgeted``: per-net analysis cache +
  cumulative analysis budget.
"""

from __future__ import annotations

import time

from kicad_tools.router.algorithms import (
    GRACE_PASS_TIER_CAPS_S,
    PER_NET_CAP_FLOOR_S,
    PER_NET_CAP_STAGE_FRACTION,
    derive_per_net_cap,
    run_initial_pass_grace,
)
from kicad_tools.router.algorithms.negotiated import (
    GRACE_CALL_OVERRUN_ABORT_FLOOR_S,
    GRACE_CALL_OVERRUN_ABORT_MULT,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.failure_analysis import (
    CongestionMap,
    Rectangle,
    RootCauseAnalyzer,
)

# ---------------------------------------------------------------------------
# derive_per_net_cap
# ---------------------------------------------------------------------------


class TestDerivePerNetCap:
    def test_explicit_cap_passes_through_unchanged(self):
        assert derive_per_net_cap(7.5, 300.0) == 7.5
        # Even when the explicit cap is larger than the derived value.
        assert derive_per_net_cap(500.0, 300.0) == 500.0

    def test_derives_fraction_of_stage_budget(self):
        assert derive_per_net_cap(None, 300.0) == 300.0 * PER_NET_CAP_STAGE_FRACTION

    def test_floor_applies_for_tiny_stage_budgets(self):
        assert derive_per_net_cap(None, 10.0) == PER_NET_CAP_FLOOR_S

    def test_unbudgeted_runs_stay_unbounded(self):
        assert derive_per_net_cap(None, None) is None
        assert derive_per_net_cap(None, 0.0) is None
        assert derive_per_net_cap(None, -5.0) is None

    def test_fraction_is_small(self):
        """No single net may claim more than ~10% of a stage by default."""
        assert 0.0 < PER_NET_CAP_STAGE_FRACTION <= 0.15


# ---------------------------------------------------------------------------
# run_initial_pass_grace: #3474 R1 productivity guards
# ---------------------------------------------------------------------------


class TestGraceOverrunAbort:
    def test_abort_constants_are_sane(self):
        """The abort threshold must tolerate the documented ~15x legal
        overrun of a multi-edge net but catch the measured 100x+ leak."""
        assert GRACE_CALL_OVERRUN_ABORT_MULT >= 15.0
        assert GRACE_CALL_OVERRUN_ABORT_FLOOR_S >= 5.0

    def test_abort_skips_remaining_nets(self, monkeypatch):
        import kicad_tools.router.algorithms.negotiated as neg

        # Shrink the abort floor so a 0.2s sleep counts as a gross overrun.
        monkeypatch.setattr(neg, "GRACE_CALL_OVERRUN_ABORT_FLOOR_S", 0.1)
        monkeypatch.setattr(neg, "GRACE_CALL_OVERRUN_ABORT_MULT", 1.0)

        attempted: list[int] = []

        def route_fn(net: int, cap: float) -> list:
            attempted.append(net)
            if net == 1:
                time.sleep(0.25)  # > max(1.0 * 0.05, 0.1)
            return []

        routed, n_attempted, skipped = neg.run_initial_pass_grace(
            [1, 2, 3, 4],
            route_fn,
            lambda net, routes: None,
            per_net_timeout=0.05,
        )
        assert routed == 0
        assert attempted == [1], "sweep must abort after the leaking call"
        assert n_attempted == 1
        assert skipped == 3

    def test_fast_failures_do_not_abort(self):
        """Fast (cap-honoring) failures sweep the whole tail."""
        attempted: list[int] = []

        def route_fn(net: int, cap: float) -> list:
            attempted.append(net)
            return [object()] if net % 2 == 0 else []

        committed: list[int] = []
        routed, n_attempted, skipped = run_initial_pass_grace(
            [1, 2, 3, 4],
            route_fn,
            lambda net, routes: committed.append(net),
            per_net_timeout=None,
        )
        assert routed == 2
        assert committed == [2, 4]
        assert skipped == 0


class TestGraceNoProgressTierExit:
    def test_tier2_skipped_when_tier1_routes_nothing(self):
        """Tier 1 attempting >= 3 nets with 0 routed skips tier 2."""
        caps_seen: list[float] = []

        def route_fn(net: int, cap: float) -> list:
            caps_seen.append(cap)
            return []

        routed, attempted, skipped = run_initial_pass_grace(
            [1, 2, 3, 4],
            route_fn,
            lambda net, routes: None,
            per_net_timeout=None,
        )
        assert routed == 0
        assert attempted == 4
        # Every call must have used the tier-1 cap only.
        assert set(caps_seen) == {GRACE_PASS_TIER_CAPS_S[0]}

    def test_tier2_runs_when_tier1_makes_progress(self):
        caps_seen: list[float] = []

        def route_fn(net: int, cap: float) -> list:
            caps_seen.append(cap)
            # Net 1 routes at tier 1; the rest keep failing.
            return [object()] if (net == 1 and cap == GRACE_PASS_TIER_CAPS_S[0]) else []

        run_initial_pass_grace(
            [1, 2, 3, 4],
            route_fn,
            lambda net, routes: None,
            per_net_timeout=None,
        )
        assert GRACE_PASS_TIER_CAPS_S[1] in set(caps_seen), (
            "tier 2 must re-attempt the tier-1 failures when tier 1 routed "
            "at least one net"
        )

    def test_budget_s_parameter_bounds_the_sweep(self):
        """A zero fund attempts nothing (callers skip in that case, but
        the function itself must also honor the bound)."""

        def route_fn(net: int, cap: float) -> list:
            time.sleep(0.01)
            return []

        routed, attempted, skipped = run_initial_pass_grace(
            [1, 2, 3],
            route_fn,
            lambda net, routes: None,
            per_net_timeout=None,
            budget_s=0.0,
        )
        assert routed == 0
        assert attempted == 0
        assert skipped == 3


# ---------------------------------------------------------------------------
# CongestionMap region window + stride
# ---------------------------------------------------------------------------


class _FakeCell:
    __slots__ = ("blocked", "usage_count", "is_zone", "net")

    def __init__(self, blocked=False, usage_count=0, is_zone=False, net=0):
        self.blocked = blocked
        self.usage_count = usage_count
        self.is_zone = is_zone
        self.net = net


class _CountingGrid:
    """Minimal RoutingGrid stand-in that counts cell visits."""

    def __init__(self, cols=100, rows=100, num_layers=2, resolution=0.1):
        self.cols = cols
        self.rows = rows
        self.num_layers = num_layers
        self.resolution = resolution
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.visits = 0
        outer = self

        class _Layer:
            def __getitem__(self_inner, gy):
                class _Row:
                    def __getitem__(self_row, gx):
                        outer.visits += 1
                        return _FakeCell()

                return _Row()

        self._layer = _Layer()
        self.grid = [self._layer] * num_layers

    def world_to_grid(self, x, y):
        return int(x / self.resolution), int(y / self.resolution)

    def grid_to_world(self, gx, gy):
        return gx * self.resolution, gy * self.resolution


class TestCongestionMapWindowAndStride:
    def test_region_limits_scan(self):
        grid = _CountingGrid()
        CongestionMap(grid, region=Rectangle(0.0, 0.0, 1.0, 1.0))
        windowed = grid.visits
        grid.visits = 0
        CongestionMap(grid)
        full = grid.visits
        assert windowed < full / 10, (
            f"region-scoped scan ({windowed} visits) must be far smaller "
            f"than the full-board scan ({full} visits)"
        )

    def test_stride_subsamples_scan(self):
        grid = _CountingGrid()
        CongestionMap(grid)
        full = grid.visits
        grid.visits = 0
        CongestionMap(grid, stride=4)
        strided = grid.visits
        assert strided <= full / 8

    def test_default_is_full_scan(self):
        """region=None, stride=1 preserves the legacy full-board scan."""
        grid = _CountingGrid(cols=10, rows=10, num_layers=1)
        CongestionMap(grid)
        assert grid.visits == 100


class TestScanStride:
    def test_small_corridor_uses_every_cell(self):
        analyzer = RootCauseAnalyzer()
        grid = _CountingGrid(cols=100, rows=100, num_layers=2, resolution=0.1)
        assert analyzer._scan_stride(grid, Rectangle(0, 0, 1.0, 1.0)) == 1

    def test_huge_fine_grid_corridor_is_subsampled(self):
        analyzer = RootCauseAnalyzer()
        # chorus-test shape: 0.0508mm resolution, 4 layers, ~63mm corridor.
        grid = _CountingGrid(cols=1240, rows=1240, num_layers=4, resolution=0.0508)
        stride = analyzer._scan_stride(grid, Rectangle(0, 0, 50.0, 40.0))
        assert stride >= 2
        # And the resulting scan stays within the cell budget.
        cells = (50.0 / 0.0508) * (40.0 / 0.0508) * 4 / stride**2
        assert cells <= RootCauseAnalyzer.MAX_SCAN_CELLS * 1.1


# ---------------------------------------------------------------------------
# Autorouter._analyze_failure_budgeted: cache + cumulative budget
# ---------------------------------------------------------------------------


class TestAnalyzeFailureBudgeted:
    def _make_router(self) -> Autorouter:
        router = Autorouter(width=20.0, height=20.0)
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )
        return router

    def test_same_net_analyzed_once(self, monkeypatch):
        router = self._make_router()
        calls: list[str] = []

        def fake_analyze(self, **kwargs):
            calls.append(kwargs["net"])
            return None

        monkeypatch.setattr(
            RootCauseAnalyzer, "analyze_routing_failure", fake_analyze
        )
        for _ in range(3):
            router._analyze_failure_budgeted(1, "NET1", (2.0, 10.0), (18.0, 10.0))
        assert len(calls) == 1, "repeat failures of one net must reuse the analysis"

    def test_budget_exhaustion_skips_deep_analysis(self, monkeypatch):
        router = self._make_router()
        calls: list[str] = []

        def fake_analyze(self, **kwargs):
            calls.append(kwargs["net"])
            return None

        monkeypatch.setattr(
            RootCauseAnalyzer, "analyze_routing_failure", fake_analyze
        )
        # Pre-exhaust the budget.
        router._failure_analysis_cache = {}
        router._failure_analysis_spent = Autorouter._FAILURE_ANALYSIS_BUDGET_S + 1.0
        result = router._analyze_failure_budgeted(2, "NET2", (0.0, 0.0), (1.0, 1.0))
        assert result is None
        assert calls == []

    def test_analyzer_exception_degrades_to_none(self, monkeypatch):
        router = self._make_router()

        def boom(self, **kwargs):
            raise RuntimeError("diagnostics must never break routing")

        monkeypatch.setattr(RootCauseAnalyzer, "analyze_routing_failure", boom)
        result = router._analyze_failure_budgeted(3, "NET3", (0.0, 0.0), (1.0, 1.0))
        assert result is None

    def test_record_routing_failure_still_appends(self, monkeypatch):
        """End-to-end: a recorded failure lands on routing_failures even
        when the deep analysis budget is exhausted."""
        router = self._make_router()
        router._failure_analysis_cache = {}
        router._failure_analysis_spent = Autorouter._FAILURE_ANALYSIS_BUDGET_S + 1.0
        src = router.pads[("R1", "1")]
        tgt = router.pads[("R1", "2")]
        router._record_routing_failure(1, src, tgt)
        assert len(router.routing_failures) == 1
        failure = router.routing_failures[0]
        assert failure.analysis is None
        assert failure.reason == "No path found"
