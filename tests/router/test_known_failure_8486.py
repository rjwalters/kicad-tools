"""Known-failure memo: rescue skip and per-net search back-off (issue #8486).

On the reported 97-net board the same 15 nets failed from iteration 2 on.
#8248 already skipped the repeat *relief* for the nine sealed ones, but every
iteration still (1) searched all 15 at the full 30 s per-net cap and (2) re-ran
the overflow-silent rescue's targeted rip-up, which was most of its cost.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from kicad_tools.router.access_ripup import (
    KNOWN_FAILURE_BACKOFF_AFTER,
    KNOWN_FAILURE_BACKOFF_FLOOR_S,
    RipupCycleGuard,
)

from .test_relief_cycle_6363 import VICTIM, _fixture

SEAL = ("seal", ((("Q1", "2"),), ("/GATE", "Q1.1", "Q1.3")))
OTHER = ("seal", ((("Q1", "2"),), ("Q1.1", "Q1.3")))


def _fail_capped(g: RipupCycleGuard, it: int, cap: float, sig=SEAL, net: int = 7) -> float | None:
    """One iteration in which ``net`` enters under ``sig`` and spends its cap."""
    g.begin_iteration(it, {net: sig})
    used = g.search_cap(net, it, cap)
    g.note_search(net, it, used, used if used is not None else 0.0, routed=False)
    return used


# --- back-off rule ----------------------------------------------------------


def test_budget_halves_after_consecutive_capped_failures_under_one_seal():
    g = RipupCycleGuard()
    caps = [_fail_capped(g, it, 30.0) for it in range(1, 7)]
    assert KNOWN_FAILURE_BACKOFF_AFTER == 2
    assert caps == [30.0, 30.0, 15.0, 7.5, KNOWN_FAILURE_BACKOFF_FLOOR_S, 5.0]
    assert [it for it, _net, _cap in g.backoffs] == [3, 4, 5, 6]


def test_backoff_resets_when_the_seal_changes():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0)
    assert _fail_capped(g, 3, 30.0) == 15.0
    assert _fail_capped(g, 4, 30.0, sig=OTHER) == 30.0  # changed: full budget
    assert _fail_capped(g, 5, 30.0, sig=OTHER) == 30.0  # streak restarts at 1
    assert _fail_capped(g, 6, 30.0, sig=OTHER) == 15.0


def test_backoff_resets_on_route_fast_failure_gap_or_missing_signature():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0)
    # Routed: streak gone, and begin_iteration forgets a net no longer failed.
    g.begin_iteration(3, {7: SEAL})
    g.note_search(7, 3, g.search_cap(7, 3, 30.0), 1.0, routed=True)
    assert 7 not in g.capped_streak
    for it in (4, 5):
        _fail_capped(g, it, 30.0)
    # A fast, proven failure is not a budget failure.
    g.begin_iteration(6, {7: SEAL})
    assert g.search_cap(7, 6, 30.0) == 15.0
    g.note_search(7, 6, 15.0, 0.4, routed=False)
    assert 7 not in g.capped_streak
    # A gap (not searched in iteration N-1) restarts the count.
    for it in (7, 8):
        _fail_capped(g, it, 30.0)
    assert _fail_capped(g, 10, 30.0) == 30.0
    # No signature (no terminals, error) never backs off.
    for it in (11, 12, 13):
        assert _fail_capped(g, it, 30.0, sig=None) == 30.0


def test_routed_net_drops_every_memo_at_the_next_iteration():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0)
    g.note_futile_rescue(7, SEAL)
    g.begin_iteration(3, {})  # net 7 routed during iteration 2
    assert 7 not in g.capped_streak and 7 not in g.futile_rescue


def test_deterministic_arm_cap_none_is_untouched():
    g = RipupCycleGuard()
    for it in range(1, 6):
        assert _fail_capped(g, it, None) is None  # type: ignore[arg-type]
    assert g.backoffs == []


def test_known_fail_seconds_are_reported_per_iteration():
    g = RipupCycleGuard()
    for it in (1, 2, 3):
        _fail_capped(g, it, 30.0)
    # Iteration 1 was the first failure; 2 and 3 searched a known failure.
    assert g.known_fail_seconds == {2: 30.0, 3: 15.0}
    line = g.known_failure_line()
    assert "iter 2: 30.0s" in line and "iter 3: 15.0s" in line
    assert "1 search(es) backed off" in line


# --- the search wrapper -------------------------------------------------------


def test_reroute_wrapper_passes_the_backed_off_cap_and_feeds_the_outcome():
    router, _neg, _net_routes, _pads = _fixture()
    g = RipupCycleGuard()
    clock = [0.0]
    seen: list[float | None] = []

    def fake_route(net, pf, per_net_timeout=None):
        seen.append(per_net_timeout)
        clock[0] += per_net_timeout or 0.0  # runs to its cap
        return []

    fake_time = SimpleNamespace(time=lambda: clock[0])
    with (
        patch.object(router, "_route_net_negotiated", side_effect=fake_route),
        patch("kicad_tools.router.core.time", fake_time),
    ):
        for it in (1, 2, 3, 4):
            g.begin_iteration(it, {VICTIM: SEAL})
            assert router._route_net_known_failure_aware(VICTIM, 1.0, 30.0, g, it) == []
    assert seen == [30.0, 30.0, 15.0, 7.5]


# --- overflow-silent rescue memo ------------------------------------------------


def _memoized(router, neg, net_routes, pads_by_net, guard, log, iteration):
    return router._silent_rescue_memoized(
        VICTIM,
        iteration,
        neg,
        net_routes,
        pads_by_net,
        1.0,
        30.0,
        log.append,
        lambda: "0s",
        ripup_guard=guard,
        ripup_history={},
        max_ripups_per_net=3,
        relief_probe_attempts=guard.relief_attempts,
        max_relief_probes_per_net=3,
        relief_deadline=None,
        deterministic_rescue=False,
    )


def test_futile_rescue_is_skipped_until_the_seal_changes():
    router, neg, net_routes, pads_by_net = _fixture()
    g = RipupCycleGuard()
    sigs = iter([SEAL, SEAL, SEAL, SEAL, OTHER, OTHER])
    calls: list[int] = []
    log: list[str] = []

    def rescue(net, *a, **kw):
        calls.append(net)
        return False

    with (
        patch.object(router, "_silent_rescue_one", side_effect=rescue),
        patch.object(router, "_relief_witness_signature", side_effect=lambda *a: next(sigs)),
    ):
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 2) is False  # runs
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 3) is False  # skip
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 4) is False  # skip
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 5) is False  # changed
    assert calls == [VICTIM, VICTIM]
    assert g.futile_rescue_skips == {VICTIM: 2}
    assert sum("skipped" in m and "#8486" in m for m in log) == 1  # logged once
    assert g.futile_rescue[VICTIM] == OTHER


def test_resolved_rescue_clears_the_memo_and_none_never_skips():
    router, neg, net_routes, pads_by_net = _fixture()
    g = RipupCycleGuard()
    outcomes = iter([False, True])
    with (
        patch.object(router, "_silent_rescue_one", side_effect=lambda *a, **k: next(outcomes)),
        patch.object(router, "_relief_witness_signature", return_value=SEAL),
    ):
        _memoized(router, neg, net_routes, pads_by_net, g, [], 2)
        g.futile_rescue.clear()  # a neighbour changed meanwhile: re-run
        assert _memoized(router, neg, net_routes, pads_by_net, g, [], 3) is True
    assert VICTIM not in g.futile_rescue

    calls: list[int] = []
    with (
        patch.object(router, "_silent_rescue_one", side_effect=lambda n, *a, **k: calls.append(n)),
        patch.object(router, "_relief_witness_signature", return_value=None),
    ):
        for it in (4, 5, 6):
            _memoized(router, neg, net_routes, pads_by_net, g, [], it)
    assert calls == [VICTIM] * 3


def test_skipped_rescue_does_not_spend_relief_attempts():
    """The real step chain: the first rescue charges one relief attempt, the
    skipped repeats charge none, so a later changed seal still has budget."""
    router, neg, net_routes, pads_by_net = _fixture()
    g = RipupCycleGuard()
    with (
        patch.object(router, "_relief_probe", return_value=([], set())),
        patch.object(router, "_relief_witness_signature", return_value=SEAL),
    ):
        for it in (2, 3, 4):
            router._commit_journal.iteration = it
            assert _memoized(router, neg, net_routes, pads_by_net, g, [], it) is False
    assert g.relief_attempts.get(VICTIM, 0) == 1
    assert g.futile_rescue_skips == {VICTIM: 2}
