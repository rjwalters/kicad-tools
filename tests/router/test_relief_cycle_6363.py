"""Relief-rescue rip-up cycling (issue #6363).

Issue #6342's :class:`RipupCycleGuard` bounded the targeted rip-up sites, but
the relief rescue (``Autorouter._relief_rescue``) went around it.  On board 05
the relief for EN_C ripped DRIVER_SLEEP / PWM_C / RUN / STATUS_LED in three
successive iterations and rolled back every time (~1050 s).  Each attempt
ripped a *different* blocker set, so a set-keyed count would never have
repeated.  Pinned here:

* relief is counted per ``(victim, blocker)`` member and refuses a probe that
  needs an exhausted blocker, even when the sets differ but overlap;
* nested depth-1 rescues go through the same per-net relief cap;
* a targeted rip-up that ripped nothing does not charge its pair;
* the victim's first-attempt partial copper is cleared on the C++ grid before
  the targeted rip-up's retry re-lands the blockers;
* the guard's summary line reaches the negotiated log.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.router.access_ripup import MAX_PAIR_RIPUPS, RipupCycleGuard
from kicad_tools.router.algorithms.negotiated import (
    RIPUP_BUDGET_EXHAUSTED,
    RIPUP_COMMITTED,
    NegotiatedRouter,
    TargetedRipupOutcome,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment

VICTIM = 1
BLOCKERS = (2, 3, 4, 5)


def _router(num_nets: int = 5) -> Autorouter:
    router = Autorouter(width=40.0, height=30.0)
    for net in range(1, num_nets + 1):
        y = 3.0 + net * 4.0
        router.add_component(
            f"U{net}",
            [
                {
                    "number": str(pin),
                    "x": x,
                    "y": y,
                    "width": 0.5,
                    "height": 0.5,
                    "net": net,
                    "net_name": f"N{net}",
                }
                for pin, x in ((1, 5.0), (2, 35.0))
            ],
        )
    return router


def _seg_route(net: int, y: float, x1: float = 8.0, x2: float = 32.0) -> Route:
    return Route(
        net=net,
        net_name=f"N{net}",
        segments=[Segment(x1=x1, y1=y, x2=x2, y2=y, width=0.2, layer=Layer.F_CU, net=net)],
    )


def _fixture():
    """Victim 1 unrouted; blockers 2..5 committed."""
    router = _router()
    neg = NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )
    net_routes: dict[int, list[Route]] = {VICTIM: []}
    for net in BLOCKERS:
        route = _seg_route(net, 3.0 + net * 4.0)
        router._mark_route(route)
        router.grid.mark_route_usage(route)
        router.routes.append(route)
        net_routes[net] = [route]
    pads_by_net = {n: [router.pads[p] for p in keys] for n, keys in router.nets.items() if n}
    return router, neg, net_routes, pads_by_net


def _relief(router, neg, net_routes, pads_by_net, guard, log, **kwargs):
    return router._relief_rescue(
        failed_net=VICTIM,
        neg_router=neg,
        net_routes=net_routes,
        pads_by_net=pads_by_net,
        present_factor=1.0,
        per_net_timeout=2.0,
        flush_print_fn=log.append,
        elapsed_fn=lambda: "0s",
        guard=guard,
        **kwargs,
    )


def _rip_then_no_path(victims: set[int]):
    """Round 1 crosses ``victims`` (ripped); round 2 finds no path (rollback)."""
    calls: list[int] = []

    def probe(net, present_factor, per_net_timeout=None, **_kw):
        calls.append(net)
        if len(calls) == 1:
            return [_seg_route(VICTIM, 3.0 + VICTIM * 4.0)], set(victims)
        return [], set()

    return probe


# ---------------------------------------------------------------------------
# Relief is guarded
# ---------------------------------------------------------------------------


def test_relief_refuses_a_blocker_rolled_back_reliefs_already_ripped_twice():
    """Board-05 shape: three different, overlapping blocker sets, all with net 2."""
    router, neg, net_routes, pads_by_net = _fixture()
    originals = {n: list(net_routes[n]) for n in BLOCKERS}
    guard = RipupCycleGuard()
    log: list[str] = []

    for victims in ({2, 3}, {2, 4}):
        with patch.object(Autorouter, "_relief_probe", side_effect=_rip_then_no_path(victims)):
            assert _relief(router, neg, net_routes, pads_by_net, guard, log) is False
        # Each attempt rolled back verbatim.
        assert {n: net_routes[n] for n in BLOCKERS} == originals

    assert guard.relief_member_counts == {(1, 2): 2, (1, 3): 1, (1, 4): 1}
    assert not guard.relief_refusals

    # Third relief: a different set again, still holding the exhausted net 2.
    log.clear()
    rip_spy = MagicMock(wraps=neg.rip_up_nets)
    with (
        patch.object(Autorouter, "_relief_probe", side_effect=_rip_then_no_path({2, 5})),
        patch.object(neg, "rip_up_nets", rip_spy),
    ):
        assert _relief(router, neg, net_routes, pads_by_net, guard, log) is False

    rip_spy.assert_not_called()  # nothing was ripped on the refused attempt
    assert {n: net_routes[n] for n in BLOCKERS} == originals
    for net in BLOCKERS:
        assert originals[net][0] in router.routes
    refused = [line for line in log if "refused" in line]
    assert len(refused) == 1
    assert "issue #6363" in refused[0] and "issue #6363, #6342" in refused[0]
    assert "N2" in refused[0] and "N5" not in refused[0]
    assert guard.relief_refusals == [(1, frozenset({2}))]
    # A refusal rips nothing, so it charges nothing.
    assert guard.relief_member_counts == {(1, 2): 2, (1, 3): 1, (1, 4): 1}

    # A relief that does not need the exhausted blocker is still allowed.
    with patch.object(Autorouter, "_relief_probe", side_effect=_rip_then_no_path({3, 5})):
        _relief(router, neg, net_routes, pads_by_net, guard, log)
    assert guard.relief_member_counts[(1, 3)] == 2
    assert guard.relief_member_counts[(1, 5)] == 1


def test_a_relief_that_ripped_nothing_charges_nothing():
    router, neg, net_routes, pads_by_net = _fixture()
    guard = RipupCycleGuard()
    for _ in range(3):
        with patch.object(Autorouter, "_relief_probe", return_value=([], set())):
            assert _relief(router, neg, net_routes, pads_by_net, guard, []) is False
    assert guard.relief_member_counts == {}


def test_relief_without_a_guard_is_unguarded():
    """The two-phase stall-relief hook passes no guard: pre-#6363 behaviour."""
    router, neg, net_routes, pads_by_net = _fixture()
    for _ in range(MAX_PAIR_RIPUPS + 1):
        rip_spy = MagicMock(wraps=neg.rip_up_nets)
        with (
            patch.object(Autorouter, "_relief_probe", side_effect=_rip_then_no_path({2})),
            patch.object(neg, "rip_up_nets", rip_spy),
        ):
            _relief(router, neg, net_routes, pads_by_net, None, [])
        rip_spy.assert_called()


# ---------------------------------------------------------------------------
# Nested rescues count against the per-net relief cap
# ---------------------------------------------------------------------------


def _nested_scenario(guard: RipupCycleGuard):
    """Outer relief for net 1 rips net 2, commits; net 2 cannot re-land plainly."""
    router, neg, net_routes, pads_by_net = _fixture()
    probes: list[int] = []

    def probe(net, present_factor, per_net_timeout=None, **_kw):
        probes.append(net)
        if net == VICTIM and probes.count(VICTIM) == 1:
            return [_seg_route(VICTIM, 7.0)], {2}
        if net == VICTIM:
            return [_seg_route(VICTIM, 7.0)], set()
        return [], set()  # nested rescue: no relief path

    def route_net(net, present_cost_factor, per_net_timeout=None, **_kw):
        return [_seg_route(VICTIM, 7.0)] if net == VICTIM else []

    log: list[str] = []
    with (
        patch.object(Autorouter, "_relief_probe", side_effect=probe),
        patch.object(Autorouter, "_route_net_negotiated", side_effect=route_net),
    ):
        ok = _relief(router, neg, net_routes, pads_by_net, guard, log)
    return ok, probes, log


def test_nested_rescue_is_charged_against_the_per_net_cap():
    guard = RipupCycleGuard(max_relief_attempts=3)
    ok, probes, _log = _nested_scenario(guard)
    assert ok is False
    assert 2 in probes, "the nested rescue for the stranded victim ran"
    assert guard.relief_attempts == {2: 1}


def test_nested_rescue_is_skipped_once_the_cap_is_spent():
    guard = RipupCycleGuard(max_relief_attempts=3)
    guard.relief_attempts[2] = 3
    ok, probes, log = _nested_scenario(guard)
    assert ok is False
    assert 2 not in probes, "a capped nested rescue must not probe"
    assert guard.relief_capped == [2]
    assert guard.relief_attempts == {2: 3}
    assert any("nested rescue of N2 skipped" in line for line in log)
    # The outer rescue still rolled back and charged its blocker.
    assert guard.relief_member_counts == {(1, 2): 1}


def test_admit_relief_counts_and_caps():
    guard = RipupCycleGuard(max_relief_attempts=2)
    assert guard.admit_relief(7) and guard.admit_relief(7)
    assert not guard.admit_relief(7)
    assert guard.relief_attempts == {7: 2}
    unbounded = RipupCycleGuard()
    assert all(unbounded.admit_relief(7) for _ in range(10))


# ---------------------------------------------------------------------------
# A no-op targeted rip-up does not burn a guard slot
# ---------------------------------------------------------------------------


def _neg_with(outcome: TargetedRipupOutcome):
    neg = MagicMock()

    def targeted_ripup(**kwargs):
        neg.last_ripup_outcome = outcome
        return bool(outcome)

    neg.targeted_ripup.side_effect = targeted_ripup
    return neg


def _guarded(router, neg, guard):
    return router._guarded_targeted_ripup(
        neg,
        guard,
        failed_net=1,
        blocking_nets={2},
        net_routes={},
        pads_by_net={},
        present_cost_factor=1.0,
        mark_route_callback=lambda r: None,
        ripup_history={},
        max_ripups_per_net=3,
        per_net_timeout=1.0,
        log=None,
    )


def test_budget_exhausted_rip_up_does_not_charge_the_pair():
    router = _router()
    guard = RipupCycleGuard()
    noop = _neg_with(TargetedRipupOutcome(RIPUP_BUDGET_EXHAUSTED, 1, ripped=(), dropped=(2,)))
    for _ in range(5):
        _guarded(router, noop, guard)
    assert guard.pair_counts == {}
    assert noop.targeted_ripup.call_count == 5  # never refused: nothing was spent
    # A real rip still charges.
    _guarded(router, _neg_with(TargetedRipupOutcome(RIPUP_COMMITTED, 1, ripped=(2,))), guard)
    assert guard.pair_counts == {(1, frozenset({2})): 1}


# ---------------------------------------------------------------------------
# Item 2: the victim's partial copper is cleared on the C++ grid before retry
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
def test_victim_partial_copper_is_cleared_on_cpp_grid_before_the_retry():
    from tests.router.test_ripup_cycle_6342 import SEAL, _seal_route, _sealed_board

    router = _sealed_board()
    cpp = router._cpp_grid
    seal = _seal_route()
    assert router._mark_route(seal)
    router.routes.append(seal)
    net_routes: dict[int, list[Route]] = {SEAL: [seal]}

    # The victim's attempt-1 partial copper, on a cell no seal copper touches.
    partial = Route(
        net=VICTIM,
        net_name="VICTIM",
        segments=[
            Segment(x1=3.0, y1=14.3, x2=3.0, y2=18.0, width=0.2, layer=Layer.F_CU, net=VICTIM)
        ],
    )
    px, py = router.grid.world_to_grid(3.0, 17.0)
    player = router.grid.layer_to_index(Layer.F_CU.value)

    victim_calls: list[int] = []
    seen_at_blocker: list[int] = []

    def fake_route_net(self_neg, pads, pf, mark_cb, per_net_timeout=None, failure_callback=None):
        net = pads[0].net
        if net == VICTIM:
            victim_calls.append(1)
            if len(victim_calls) == 1:
                mark_cb(partial)  # lays partial copper, then fails an edge
                if failure_callback is not None:
                    failure_callback(pads[0], pads[1])
                return [partial]
            return [
                Route(
                    net=VICTIM,
                    net_name="VICTIM",
                    segments=[
                        Segment(
                            x1=10.0,
                            y1=10.0,
                            x2=3.0,
                            y2=14.3,
                            width=0.2,
                            layer=Layer.F_CU,
                            net=VICTIM,
                        )
                    ],
                )
            ]
        # A blocker re-landing during the retry: what does the C++ grid say
        # about the victim's discarded attempt-1 copper?
        seen_at_blocker.append(int(cpp._impl.at(px, py, player).net))
        return [_seal_route()]

    router.grid.begin_cpp_unmark_deferral()
    neg = NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )
    with patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net):
        neg.targeted_ripup(
            VICTIM,
            {SEAL},
            net_routes,
            router.routes,
            {n: [router.pads[k] for k in keys] for n, keys in router.nets.items() if n},
            1.0,
            router._mark_route,
            ripup_history={},
            per_net_timeout=10.0,
        )

    assert len(victim_calls) == 2, "the victim_geometric failure is retried"
    assert neg.last_ripup_outcome.cpp_retry is True
    assert seen_at_blocker, "the retry re-landed the blocker"
    assert seen_at_blocker[0] != VICTIM, "attempt-1 partial copper still on the C++ grid"
    queued = [route for route, _ in router.grid._deferred_cpp_unmarks]
    assert partial not in queued
    # The blocker's ripped-and-restored copper stays reserved until the flush.
    assert seal in queued
    assert router.grid._cpp_unmark_deferred is True
    router.grid.flush_cpp_unmark_deferral()


@pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
def test_flush_discarded_keeps_committed_routes_queued_and_remarks_same_net():
    router = _router(num_nets=2)
    grid = router.grid
    cpp = router._cpp_grid
    kept = _seg_route(2, 11.0)
    router._mark_route(kept)
    grid.begin_cpp_unmark_deferral()
    start = grid.pending_cpp_unmark_count()
    discarded = _seg_route(2, 11.0, x1=8.0, x2=20.0)  # overlaps ``kept``
    router._mark_route(discarded)
    grid.unmark_route(discarded)
    grid.unmark_route(kept)
    router._mark_route(kept)  # ripped and restored
    assert grid.pending_cpp_unmark_count() == 2

    flushed = grid.flush_discarded_cpp_unmarks(start)

    assert flushed == [discarded]
    assert [r for r, _ in grid._deferred_cpp_unmarks] == [kept]
    gx, gy = grid.world_to_grid(12.0, 11.0)
    assert cpp._impl.at(gx, gy, 0).net == 2, "same-net committed copper re-marked"
    gx, gy = grid.world_to_grid(25.0, 11.0)
    assert cpp._impl.at(gx, gy, 0).net == 2
    grid.flush_cpp_unmark_deferral()


def test_flush_discarded_without_window_is_noop():
    router = _router(num_nets=1)
    assert router.grid.pending_cpp_unmark_count() == 0
    assert router.grid.flush_discarded_cpp_unmarks(0) == []


# ---------------------------------------------------------------------------
# The summary line
# ---------------------------------------------------------------------------


def test_summary_line_reports_relief_activity():
    guard = RipupCycleGuard()
    guard.admit_relief(1)
    guard.charge_relief_rollback(1, [2, 3])
    guard.charge_relief_rollback(1, [2])
    guard.refuse_relief(1, [2])
    line = guard.summary_line()
    assert line.startswith("Rip-up cycle guard: ")
    assert "relief: 1 rescue(s), 2 (victim, blocker) member(s) rolled back" in line
    assert "max 2 per member, 1 refused" in line


def test_negotiated_log_prints_the_guard_summary(capsys):
    """A run that reaches the rip-up loop ends its summary with the guard line."""
    from tests.router.test_access_ripup_5913 import _boxed_in_router

    router = _boxed_in_router()
    router.route_all_negotiated(
        max_iterations=3, timeout=60, per_net_timeout=5, use_targeted_ripup=True
    )
    out = capsys.readouterr().out
    assert "=== Negotiated Routing Complete ===" in out
    assert "Rip-up cycle guard: " in out
