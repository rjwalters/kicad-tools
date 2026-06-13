"""Tests for Issue #3433: segment-vs-segment violators in the negotiated
quality tuple + the demote-to-partial safety net.

Structural hazard (board-04 SWCLK/SWO, ``clearance_segment_segment``
actuals to -0.200 mm): the best-iteration lex tuple
(:class:`IterationMetrics`) counted only seg-via (#3002) and via-seg
(#3020) violators.  With no seg-seg finder, an iteration holding four
cross-net trace FULL OVERLAPS scored identically to a DRC-clean one,
so ``routed_count`` dominance let the overlapping snapshot win the
post-loop restore and physically-overlapping copper reached the saved
board.

The reported violation set is environment-sensitive (wall-clock rip-up
trajectories; C++ vs Python A*) and does not reproduce everywhere, so
these tests target the STRUCTURE with synthetic fixtures that FORCE the
overlapping-commit scenario:

1. ``NegotiatedRouter.find_segment_segment_violation_pairs`` /
   ``find_nets_with_segment_segment_violations`` — detection engine.
2. ``IterationMetrics`` — a clean snapshot must beat an overlapping
   snapshot with equal routed count.
3. ``_select_seg_seg_demotion_nets`` — greedy cover that picks which
   nets to strip when overlaps survive to the final result.
4. ``Autorouter._demote_seg_seg_overlap_nets`` — end-to-end safety
   net: overlapping copper is demoted to unrouted, never committed.
"""

from __future__ import annotations

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.core import (
    Autorouter,
    IterationMetrics,
    _select_seg_seg_demotion_nets,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP, DesignRules


def _make_rules() -> DesignRules:
    """DesignRules mirroring board-04's clearance regime (0.2/0.15)."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.two_layer(),
    )


def _make_neg_router(grid, rules) -> NegotiatedRouter:
    router = Router(grid, rules) if grid is not None else None
    return NegotiatedRouter(grid, router, rules, DEFAULT_NET_CLASS_MAP)


def _seg(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    net: int,
    layer: Layer = Layer.F_CU,
    width: float = 0.2,
) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer, net=net)


def _route(net: int, segments: list[Segment], name: str = "") -> Route:
    return Route(net=net, net_name=name or f"NET{net}", segments=segments, vias=[])


# ---------------------------------------------------------------------------
# Detection: find_nets_with_segment_segment_violations
# ---------------------------------------------------------------------------


class TestFindNetsWithSegmentSegmentViolations:
    """Unit tests for the third-quadrant clearance finder."""

    def setup_method(self):
        self.rules = _make_rules()
        self.neg = _make_neg_router(None, self.rules)

    def test_full_overlap_flags_both_nets(self):
        """Board-04 -0.200 mm case: coincident centerlines on the same
        layer flag BOTH nets (overlapping traces are rippable peers)."""
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1)], "SWCLK")],
            2: [_route(2, [_seg(2.0, 5.0, 8.0, 5.0, 2)], "SWO")],
        }
        assert self.neg.find_nets_with_segment_segment_violations(
            net_routes, trace_clearance=0.15
        ) == [1, 2]

    def test_near_miss_clearance_violation_detected(self):
        """Board-04 0.106 mm case: positive edge gap below the 0.2 mm
        requirement is still a violation (centerline dy = 0.2 width +
        0.106 gap = 0.306 < 0.35 required)."""
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1)])],
            2: [_route(2, [_seg(0.0, 5.306, 10.0, 5.306, 2)])],
        }
        assert self.neg.find_nets_with_segment_segment_violations(
            net_routes, trace_clearance=0.15
        ) == [1, 2]

    def test_exactly_at_required_pitch_is_clean(self):
        """Grid-snapped routes at exactly width + clearance pitch
        (centerline dy = 0.35) must NOT be flagged (float-noise guard)."""
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1)])],
            2: [_route(2, [_seg(0.0, 5.35, 10.0, 5.35, 2)])],
        }
        assert (
            self.neg.find_nets_with_segment_segment_violations(net_routes, trace_clearance=0.15)
            == []
        )

    def test_same_net_overlap_ignored(self):
        """Coincident same-net segments (stitching, re-traced spans)
        are never violations."""
        net_routes = {
            1: [
                _route(
                    1,
                    [_seg(0.0, 5.0, 10.0, 5.0, 1), _seg(0.0, 5.0, 10.0, 5.0, 1)],
                )
            ],
        }
        assert (
            self.neg.find_nets_with_segment_segment_violations(net_routes, trace_clearance=0.15)
            == []
        )

    def test_different_layer_overlap_ignored(self):
        """The committed-clean board resolves SWCLK/SWO by layer
        separation (F.Cu vs B.Cu) -- that geometry must score clean."""
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1, layer=Layer.F_CU)])],
            2: [_route(2, [_seg(0.0, 5.0, 10.0, 5.0, 2, layer=Layer.B_CU)])],
        }
        assert (
            self.neg.find_nets_with_segment_segment_violations(net_routes, trace_clearance=0.15)
            == []
        )

    def test_extra_routes_foreign_universe_only(self):
        """Issue #3077 parity: an escape-phase extra route overlapping a
        committed net surfaces the COMMITTED net only -- escape infra is
        non-rippable and must not enter the violator set."""
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1)])],
        }
        extra = [_route(9, [_seg(0.0, 5.0, 10.0, 5.0, 9)], "ESC")]
        assert self.neg.find_nets_with_segment_segment_violations(
            net_routes, trace_clearance=0.15, extra_routes=extra
        ) == [1]

    def test_extra_vs_extra_pair_skipped(self):
        """Two overlapping extra routes produce no violators and no
        pairs -- nothing the negotiated loop can rip up."""
        net_routes: dict[int, list[Route]] = {}
        extra = [
            _route(8, [_seg(0.0, 5.0, 10.0, 5.0, 8)]),
            _route(9, [_seg(0.0, 5.0, 10.0, 5.0, 9)]),
        ]
        assert (
            self.neg.find_nets_with_segment_segment_violations(
                net_routes, trace_clearance=0.15, extra_routes=extra
            )
            == []
        )
        assert (
            self.neg.find_segment_segment_violation_pairs(
                net_routes, trace_clearance=0.15, extra_routes=extra
            )
            == []
        )

    def test_memoization_same_cache_key_reuses_result(self):
        """Same cache_key -> cached result even after mutation; a new
        cache_key triggers a fresh walk (protocol parity with the
        seg-via / via-seg sibling hooks)."""
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1)])],
            2: [_route(2, [_seg(0.0, 5.0, 10.0, 5.0, 2)])],
        }
        first = self.neg.find_nets_with_segment_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("post", 1)
        )
        assert first == [1, 2]
        net_routes[2] = []  # Mutate without changing the key.
        stale = self.neg.find_nets_with_segment_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("post", 1)
        )
        assert stale == [1, 2]  # Memo hit.
        fresh = self.neg.find_nets_with_segment_segment_violations(
            net_routes, trace_clearance=0.15, cache_key=("post", 2)
        )
        assert fresh == []

    def test_four_overlap_corridor_counts_all_nets(self):
        """The board-04 corridor shape: multiple overlapping spans in
        the x 34-44 band between two nets count each net once."""
        net_routes = {
            1: [
                _route(
                    1,
                    [
                        _seg(34.0, 19.0, 36.0, 19.0, 1),
                        _seg(40.0, 20.5, 42.0, 20.5, 1),
                    ],
                    "SWCLK",
                )
            ],
            2: [
                _route(
                    2,
                    [
                        _seg(34.0, 19.0, 36.0, 19.0, 2),
                        _seg(40.0, 20.5, 42.0, 20.5, 2),
                    ],
                    "SWO",
                )
            ],
        }
        assert self.neg.find_nets_with_segment_segment_violations(
            net_routes, trace_clearance=0.15
        ) == [1, 2]


class TestFindSegmentSegmentViolationPairs:
    """copper_overlap_only discrimination for the demotion safety net."""

    def setup_method(self):
        self.rules = _make_rules()
        self.neg = _make_neg_router(None, self.rules)

    def test_hard_overlap_reported(self):
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1)])],
            2: [_route(2, [_seg(2.0, 5.0, 8.0, 5.0, 2)])],
        }
        assert self.neg.find_segment_segment_violation_pairs(
            net_routes, trace_clearance=0.15, copper_overlap_only=True
        ) == [(1, 2)]

    def test_positive_gap_near_miss_not_hard_overlap(self):
        """A nudgeable 0.106 mm near-miss is a clearance violation but
        NOT a copper overlap -- the safety net must not demote it."""
        net_routes = {
            1: [_route(1, [_seg(0.0, 5.0, 10.0, 5.0, 1)])],
            2: [_route(2, [_seg(0.0, 5.306, 10.0, 5.306, 2)])],
        }
        assert (
            self.neg.find_segment_segment_violation_pairs(
                net_routes, trace_clearance=0.15, copper_overlap_only=True
            )
            == []
        )
        # ...but it IS a default-mode violation.
        assert self.neg.find_segment_segment_violation_pairs(
            net_routes, trace_clearance=0.15, copper_overlap_only=False
        ) == [(1, 2)]


# ---------------------------------------------------------------------------
# Lex tuple: clean snapshot must beat overlapping snapshot
# ---------------------------------------------------------------------------


class TestIterationMetricsSegSegOrdering:
    """The #3433 blind spot expressed directly on the comparator."""

    def test_clean_beats_overlapping_at_equal_routed_count(self):
        """Iteration A: 9/9 routed, 4 seg-seg violators.  Iteration B:
        9/9 routed, clean.  B must win even with higher overflow."""
        overlapping = IterationMetrics(
            iteration=3,
            routed_count=9,
            overflow=0,
            clearance_violations=4,
            nets_fully_connected=9,
        )
        clean = IterationMetrics(
            iteration=1,
            routed_count=9,
            overflow=2,
            clearance_violations=0,
            nets_fully_connected=9,
        )
        assert clean.is_better_than(overlapping)
        assert not overlapping.is_better_than(clean)

    def test_pre_3433_tie_is_now_broken(self):
        """Pre-fix, both snapshots scored clearance_violations=0 and the
        later iteration won the tie.  With seg-seg counted, the
        overlapping later iteration loses."""
        clean_early = IterationMetrics(
            iteration=0,
            routed_count=9,
            overflow=0,
            clearance_violations=0,
            nets_fully_connected=9,
        )
        overlapping_late = IterationMetrics(
            iteration=5,
            routed_count=9,
            overflow=0,
            clearance_violations=2,  # Two seg-seg violator nets.
            nets_fully_connected=9,
        )
        assert clean_early.is_better_than(overlapping_late)


class TestIterationMetricsDemotableConnected:
    """Issue #3588: the comparator's primary key is SURVIVABLE
    connectivity (``effective_connected = nets_fully_connected -
    demotable_connected``), not raw ``nets_fully_connected``.

    Board-04's 2L SWDIO/NRST regression made the blind spot concrete:
    iter-3 closed 9/9 pad-to-pad paths but TWO of those nets
    (SWDIO, NRST) sat in hard copper overlaps the post-loop
    ``_demote_seg_seg_overlap_nets`` safety net strips, collapsing the
    saved board to 7/9.  iter-2 closed only 8/9 paths but carried ZERO
    overlaps, so it survives demotion at a clean 8/9.  The old #3117
    primary key (raw ``nets_fully_connected``) preferred iter-3 (9 > 8)
    and shipped the worse result.
    """

    def test_clean_lower_connected_beats_higher_but_demotable(self):
        """The exact board-04 trade: clean 8-connected must beat
        9-connected-with-2-demotable (effective 7)."""
        dirty_high = IterationMetrics(
            iteration=3,
            routed_count=9,
            overflow=3,
            clearance_violations=7,
            nets_fully_connected=9,
            demotable_connected=2,  # SWDIO + NRST will be stripped.
        )
        clean_low = IterationMetrics(
            iteration=2,
            routed_count=9,
            overflow=1,
            clearance_violations=0,
            nets_fully_connected=8,
            demotable_connected=0,
        )
        assert clean_low.effective_connected == 8
        assert dirty_high.effective_connected == 7
        assert clean_low.is_better_than(dirty_high)
        assert not dirty_high.is_better_than(clean_low)

    def test_equal_effective_prefers_more_raw_connected(self):
        """When survivable connectivity ties, the state that closed more
        pad-to-pad paths overall still wins (secondary key)."""
        more_raw = IterationMetrics(
            iteration=1,
            routed_count=9,
            overflow=0,
            clearance_violations=1,
            nets_fully_connected=9,
            demotable_connected=1,  # effective 8
        )
        fewer_raw = IterationMetrics(
            iteration=1,
            routed_count=8,
            overflow=0,
            clearance_violations=0,
            nets_fully_connected=8,
            demotable_connected=0,  # effective 8
        )
        assert more_raw.effective_connected == fewer_raw.effective_connected == 8
        assert more_raw.is_better_than(fewer_raw)

    def test_default_demotable_preserves_3117_ordering(self):
        """With ``demotable_connected`` defaulted (0), ``effective_connected``
        collapses to ``nets_fully_connected`` and the historical #3117
        ordering is unchanged: higher raw connectivity wins."""
        nine = IterationMetrics(
            iteration=1, routed_count=9, overflow=0, nets_fully_connected=9
        )
        eight = IterationMetrics(
            iteration=2, routed_count=9, overflow=0, nets_fully_connected=8
        )
        assert nine.effective_connected == 9
        assert eight.effective_connected == 8
        assert nine.is_better_than(eight)


# ---------------------------------------------------------------------------
# Demotion: greedy cover + Autorouter safety net
# ---------------------------------------------------------------------------


class TestSelectSegSegDemotionNets:
    """Pure greedy-cover selection logic."""

    def test_single_pair_demotes_one_net(self):
        assert _select_seg_seg_demotion_nets([(1, 2)], {1, 2}) == [1]

    def test_hub_net_preferred(self):
        """A net overlapping two others is demoted alone (vertex cover
        minimality) instead of demoting both leaves."""
        assert _select_seg_seg_demotion_nets([(1, 2), (2, 3)], {1, 2, 3}) == [2]

    def test_non_demotable_member_forces_partner(self):
        """Pair (5, 9) where 5 is escape infra (not demotable): the
        committed partner 9 is demoted."""
        assert _select_seg_seg_demotion_nets([(5, 9)], {9}) == [9]

    def test_no_demotable_members_yields_empty(self):
        assert _select_seg_seg_demotion_nets([(5, 9)], set()) == []

    def test_deterministic_tie_break(self):
        """Equal participation counts -> lowest net id, every time."""
        for _ in range(5):
            assert _select_seg_seg_demotion_nets([(3, 7)], {3, 7}) == [3]


class TestDemoteSegSegOverlapNets:
    """End-to-end safety net on a real Autorouter + grid: a forced
    overlapping commit is demoted to unrouted, never committed."""

    def _make_autorouter(self) -> Autorouter:
        rules = _make_rules()
        ar = Autorouter(
            width=20.0,
            height=20.0,
            origin_x=0.0,
            origin_y=0.0,
            rules=rules,
            layer_stack=LayerStack.two_layer(),
        )
        return ar

    def test_overlapping_net_demoted(self):
        ar = self._make_autorouter()
        neg = _make_neg_router(ar.grid, ar.rules)

        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "SWCLK")
        r2 = _route(2, [_seg(4.0, 5.0, 10.0, 5.0, 2)], "SWO")
        r3 = _route(3, [_seg(2.0, 10.0, 12.0, 10.0, 3)], "CLEAN")
        for r in (r1, r2, r3):
            ar.grid.mark_route(r)
            ar.grid.mark_route_usage(r)
            ar.routes.append(r)
        net_routes = {1: [r1], 2: [r2], 3: [r3]}

        demoted = ar._demote_seg_seg_overlap_nets(net_routes, neg)

        assert demoted == [1]
        assert net_routes[1] == []
        assert net_routes[2] == [r2]
        assert net_routes[3] == [r3]
        assert r1 not in ar.routes
        assert r2 in ar.routes and r3 in ar.routes
        # Post-demotion state contains no copper overlap.
        assert (
            neg.find_segment_segment_violation_pairs(
                net_routes,
                trace_clearance=ar.rules.trace_clearance,
                copper_overlap_only=True,
            )
            == []
        )

    def test_clean_board_untouched(self):
        ar = self._make_autorouter()
        neg = _make_neg_router(ar.grid, ar.rules)
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)])
        r2 = _route(2, [_seg(2.0, 10.0, 12.0, 10.0, 2)])
        for r in (r1, r2):
            ar.grid.mark_route(r)
            ar.grid.mark_route_usage(r)
            ar.routes.append(r)
        net_routes = {1: [r1], 2: [r2]}

        assert ar._demote_seg_seg_overlap_nets(net_routes, neg) == []
        assert net_routes == {1: [r1], 2: [r2]}
        assert r1 in ar.routes and r2 in ar.routes

    def test_near_miss_not_demoted(self):
        """Positive-gap near-miss (0.106 mm) stays committed -- it is
        repairable by the correction/nudge passes, and demotion must
        only fire for unmanufacturable physical overlap."""
        ar = self._make_autorouter()
        neg = _make_neg_router(ar.grid, ar.rules)
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)])
        r2 = _route(2, [_seg(2.0, 5.306, 12.0, 5.306, 2)])
        for r in (r1, r2):
            ar.grid.mark_route(r)
            ar.grid.mark_route_usage(r)
            ar.routes.append(r)
        net_routes = {1: [r1], 2: [r2]}

        assert ar._demote_seg_seg_overlap_nets(net_routes, neg) == []
        assert net_routes[1] == [r1] and net_routes[2] == [r2]


# ---------------------------------------------------------------------------
# Issue #3413: correction pass must run BEFORE demotion on the timeout exit
# ---------------------------------------------------------------------------


class TestCorrectionBeforeDemotionOnTimeout:
    """``route_all_negotiated``'s timeout exit must give the post-route
    clearance-correction pass a (bounded) chance to repair geometry
    BEFORE the #3433 overlap demotion strips nets.

    Board 06's USB2_D+ made the gap concrete: the negotiated loop exits
    via timeout, the old ``not timed_out`` gate skipped
    ``_post_route_clearance_correction`` entirely, and the demotion
    safety net was the FIRST consumer of a repairable overlap -- so the
    net was stripped every sweep instead of rerouted.
    """

    def _make_autorouter(self) -> Autorouter:
        ar = Autorouter(
            width=20.0,
            height=20.0,
            origin_x=0.0,
            origin_y=0.0,
            rules=_make_rules(),
            layer_stack=LayerStack.two_layer(),
        )
        pads1 = [
            {"number": "1", "x": 2.0, "y": 5.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 12.0, "y": 5.0, "net": 1, "net_name": "NET1"},
        ]
        pads2 = [
            {"number": "1", "x": 2.0, "y": 10.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 12.0, "y": 10.0, "net": 2, "net_name": "NET2"},
        ]
        ar.add_component("R1", pads1)
        ar.add_component("R2", pads2)
        return ar

    @staticmethod
    def _instrument(monkeypatch, calls):
        """Replace the correction + demotion passes with order recorders."""

        def _fake_correction(self, **kwargs):
            calls.append(("correction", kwargs.get("max_correction_passes")))
            return 0

        def _fake_demotion(self, net_routes, neg_router):
            calls.append(("demotion", None))
            return []

        monkeypatch.setattr(
            Autorouter, "_post_route_clearance_correction", _fake_correction
        )
        monkeypatch.setattr(
            Autorouter, "_demote_seg_seg_overlap_nets", _fake_demotion
        )

    def test_correction_runs_bounded_before_demotion_on_timeout(self, monkeypatch):
        ar = self._make_autorouter()

        # Fake clock: every _route_net_negotiated call costs 10 fake
        # seconds, so with timeout=10 the iteration-0 loop trips the
        # wall-clock check after the first net (elapsed 10 >= 10) and
        # exits via the ``timed_out`` path with successful_nets >= 1.
        # ``route_all_negotiated`` imports ``time`` function-locally, so
        # patch the global ``time.time``; monkeypatch restores it on
        # teardown and nothing in this test depends on real wall-clock.
        class _FakeClock:
            t = 1000.0

        import time as _time_mod

        monkeypatch.setattr(_time_mod, "time", lambda: _FakeClock.t)

        real_route = Autorouter._route_net_negotiated

        def _slow_route(self, net, present_factor, per_net_timeout=None):
            _FakeClock.t += 10.0
            return real_route(
                self, net, present_factor, per_net_timeout=per_net_timeout
            )

        monkeypatch.setattr(Autorouter, "_route_net_negotiated", _slow_route)

        calls: list[tuple[str, object]] = []
        self._instrument(monkeypatch, calls)

        ar.route_all_negotiated(timeout=10.0, per_net_timeout=5.0)

        names = [name for name, _ in calls]
        assert "correction" in names, (
            "Timeout exit must still run the bounded correction pass "
            "(successful_nets > 0)"
        )
        assert "demotion" in names, "The #3433 safety net must always run"
        assert names.index("correction") < names.index("demotion"), (
            "Correction must get first chance at the geometry before demotion"
        )
        correction_passes = dict(calls)["correction"]
        assert correction_passes == 1, (
            f"Timed-out exit must bound the correction to a single pass, "
            f"got max_correction_passes={correction_passes}"
        )

    def test_correction_unbounded_on_clean_exit(self, monkeypatch):
        """Non-timeout exits keep the historical 3-pass budget.

        The iteration loop must actually run (the zero-overflow
        early-return after iteration 0 skips the tail entirely), so
        force a phantom overflow reading: the loop spins without a
        cohort, exits without ``timed_out``, and the tail must call the
        correction pass with the historical 3-pass budget.
        """
        ar = self._make_autorouter()

        real_overflow = type(ar.grid).get_total_overflow
        overflow_calls = {"n": 0}

        def _phantom_overflow(self):
            overflow_calls["n"] += 1
            # First reading (post-iteration-0) reports phantom overflow so
            # the early "No conflicts" return is skipped; later readings
            # are truthful so the loop winds down naturally.
            if overflow_calls["n"] == 1:
                return 1
            return real_overflow(self)

        monkeypatch.setattr(type(ar.grid), "get_total_overflow", _phantom_overflow)

        calls: list[tuple[str, object]] = []
        self._instrument(monkeypatch, calls)

        ar.route_all_negotiated(timeout=600.0, per_net_timeout=5.0)

        assert calls, "Clean exit with routed nets must run the correction pass"
        assert calls[0] == ("correction", 3)
        names = [name for name, _ in calls]
        assert names.index("correction") < names.index("demotion")
