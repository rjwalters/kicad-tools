"""Access-loss rip-up cycles: rip blocker, blocker reseals, repeat (issue #6342).

On board 05 the negotiated loop named the right blocker for a sealed-in net,
ripped it, and the net still failed -- every round, until the no-progress stop.
Four pieces are pinned here:

* ``NegotiatedRouter.targeted_ripup`` records *why* it failed
  (:class:`TargetedRipupOutcome`) instead of returning a bare ``False``.
* Inside the negotiated loop's corridor-reservation window the ripped blockers'
  copper stayed phantom-blocked on the C++ grid, so the victim was rerouted
  against the very copper the rip-up had just removed.  ``targeted_ripup`` now
  retries such a failure once with its own rip-ups mirrored to the C++ grid.
* :class:`RipupCycleGuard` rips one ``(victim, blocker set)`` pair at most twice
  per run; the second attempt may *trade* (keep the victim, leave the blocker to
  route around it).
* The witness log line no longer says "ripping X" for a net the rip-up budget
  already filtered out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.router.access_ripup import (
    MAX_PAIR_RIPUPS,
    PLAN_NOTHING,
    PLAN_REFUSE,
    PLAN_RIP,
    PLAN_TRADE,
    AccessLossWitness,
    RipupCycleGuard,
)
from kicad_tools.router.algorithms.negotiated import (
    RIPUP_BUDGET_EXHAUSTED,
    RIPUP_COMMITTED,
    RIPUP_SIBLING_FAILED,
    RIPUP_TRADED,
    RIPUP_VICTIM_GEOMETRIC,
    NegotiatedRouter,
    TargetedRipupOutcome,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from tests.router.test_negotiated_ripup import (
    _build_pads_by_net,
    _make_router_with_four_nets,
    _stub_route,
)

# ---------------------------------------------------------------------------
# Structured outcome of one targeted rip-up
# ---------------------------------------------------------------------------


def _neg(router: Autorouter) -> NegotiatedRouter:
    return NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )


def _run_ripup(fake_route_net, *, blocking=frozenset({2, 3}), history=None, **kwargs):
    router = _make_router_with_four_nets()
    neg = _neg(router)
    original = {2: _stub_route(2), 3: _stub_route(3)}
    net_routes: dict[int, list] = {n: [r] for n, r in original.items()}
    routes_list: list = list(original.values())
    with patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net):
        success = neg.targeted_ripup(
            failed_net=1,
            blocking_nets=set(blocking),
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net=_build_pads_by_net(router),
            present_cost_factor=1.0,
            mark_route_callback=lambda r: None,
            ripup_history={} if history is None else history,
            max_ripups_per_net=3,
            per_net_timeout=2.0,
            **kwargs,
        )
    return success, neg.last_ripup_outcome, net_routes, routes_list, original


def test_outcome_committed():
    def fake(self_neg, pads, *a, **k):
        return [_stub_route(pads[0].net, offset=10.0)]

    success, outcome, *_ = _run_ripup(fake)

    assert success is True
    assert isinstance(outcome, TargetedRipupOutcome)
    assert outcome.reason == RIPUP_COMMITTED
    assert outcome.ripped == (2, 3)
    assert bool(outcome) is True


def test_outcome_victim_geometric_when_the_victim_has_no_path_with_blockers_gone():
    def fake(self_neg, pads, *a, **k):
        return [] if pads[0].net == 1 else [_stub_route(pads[0].net)]

    success, outcome, net_routes, _routes, original = _run_ripup(fake)

    assert success is False
    assert outcome.reason == RIPUP_VICTIM_GEOMETRIC
    assert bool(outcome) is False
    # Still the #3470 verbatim rollback.
    assert net_routes[2] == [original[2]]


def test_outcome_sibling_failed_names_the_blocker_that_could_not_re_land():
    def fake(self_neg, pads, *a, **k):
        net = pads[0].net
        if net == 3:
            return []
        return [_stub_route(net, offset=10.0)]

    success, outcome, net_routes, _routes, original = _run_ripup(fake)

    assert success is False
    assert outcome.reason == RIPUP_SIBLING_FAILED
    assert outcome.failed_sibling == 3
    assert net_routes.get(1, []) == []
    assert net_routes[3] == [original[3]]
    assert "could not re-land" in outcome.describe({3: "PHASE_B"})


def test_outcome_budget_exhausted_reports_the_nets_it_did_not_rip():
    def fake(self_neg, pads, *a, **k):  # pragma: no cover - must not run
        raise AssertionError("nothing may be rerouted when nothing was ripped")

    success, outcome, *_ = _run_ripup(fake, history={2: 3, 3: 3})

    assert success is False
    assert outcome.reason == RIPUP_BUDGET_EXHAUSTED
    assert outcome.ripped == ()
    assert outcome.dropped == (2, 3)
    assert "rip-up budget spent" in outcome.describe({2: "GND_SENSE", 3: "VM"})


def test_trade_keeps_the_victim_and_leaves_the_stuck_blocker_unrouted():
    """Second-attempt semantics: the victim keeps its copper (issue #6342)."""
    rescued = _stub_route(1, offset=10.0)

    def fake(self_neg, pads, *a, **k):
        net = pads[0].net
        if net == 1:
            return [rescued]
        if net == 3:
            return []
        return [_stub_route(net, offset=10.0)]

    success, outcome, net_routes, routes_list, original = _run_ripup(fake, allow_trade=True)

    assert success is True
    assert outcome.reason == RIPUP_TRADED
    assert outcome.traded == (3,)
    assert outcome.failed_sibling == 3
    assert net_routes[1] == [rescued]
    assert rescued in routes_list
    # The blocker that could not re-land is left for ordinary recovery -- no
    # stale copper of it survives anywhere.
    assert net_routes[3] == []
    assert original[3] not in routes_list
    # The blocker that did re-land keeps its new copper.
    assert net_routes[2] and net_routes[2] != [original[2]]


def test_trade_never_keeps_a_victim_that_did_not_fully_route():
    def fake(self_neg, pads, *a, **k):
        return [] if pads[0].net == 1 else [_stub_route(pads[0].net)]

    success, outcome, net_routes, _routes, original = _run_ripup(fake, allow_trade=True)

    assert success is False
    assert outcome.reason == RIPUP_VICTIM_GEOMETRIC
    assert net_routes[2] == [original[2]]
    assert net_routes[3] == [original[3]]


# ---------------------------------------------------------------------------
# The cycle guard
# ---------------------------------------------------------------------------


def test_guard_rips_a_pair_at_most_twice_then_refuses():
    guard = RipupCycleGuard()
    modes = []
    for _ in range(4):
        plan = guard.plan(1, {2, 3})
        modes.append(plan.mode)
        if plan.rips:
            guard.charge(plan)
        else:
            guard.refuse(plan)

    assert modes == [PLAN_RIP, PLAN_TRADE, PLAN_REFUSE, PLAN_REFUSE]
    assert guard.pair_counts[(1, frozenset({2, 3}))] == MAX_PAIR_RIPUPS == 2
    assert len(guard.refusals) == 2


def test_guard_counts_a_different_blocker_set_as_a_different_pair():
    guard = RipupCycleGuard()
    for _ in range(2):
        guard.charge(guard.plan(1, {2}))

    assert guard.plan(1, {2}).mode == PLAN_REFUSE
    assert guard.plan(1, {2, 3}).mode == PLAN_RIP


def test_a_trade_protects_the_victim_from_the_blocker_it_displaced():
    guard = RipupCycleGuard()
    guard.record(
        TargetedRipupOutcome(
            reason=RIPUP_TRADED, failed_net=1, ripped=(2, 3), failed_sibling=3, traded=(3,)
        )
    )

    # The displaced blocker may not rip the victim straight back ...
    plan = guard.plan(3, {1})
    assert plan.mode == PLAN_NOTHING
    assert plan.held_back == {1: "protected victim"}
    # ... but can still rip anything else it names.
    plan = guard.plan(3, {1, 4})
    assert plan.mode == PLAN_RIP
    assert plan.blockers == frozenset({4})
    # A blocker that re-landed is not barred from the victim.
    assert guard.plan(2, {1}).mode == PLAN_RIP


def test_a_plain_commit_protects_nobody():
    guard = RipupCycleGuard()
    guard.record(TargetedRipupOutcome(reason=RIPUP_COMMITTED, failed_net=1, ripped=(2,)))

    assert guard.protected == {}
    assert guard.plan(2, {1}).mode == PLAN_RIP


def _fake_neg_router(outcome_reason: str):
    neg = MagicMock()

    def targeted_ripup(**kwargs):
        neg.last_ripup_outcome = TargetedRipupOutcome(
            reason=outcome_reason,
            failed_net=kwargs["failed_net"],
            ripped=tuple(sorted(kwargs["blocking_nets"])),
        )
        return outcome_reason == RIPUP_COMMITTED

    neg.targeted_ripup.side_effect = targeted_ripup
    return neg


def test_same_victim_and_blocker_set_is_not_ripped_more_than_twice_per_run():
    """AC: the same (victim, blocker set) is ripped at most twice in one run."""
    router = _make_router_with_four_nets()
    guard = RipupCycleGuard()
    neg = _fake_neg_router(RIPUP_VICTIM_GEOMETRIC)
    lines: list[str] = []

    for _ in range(6):
        ok = router._guarded_targeted_ripup(
            neg,
            guard,
            failed_net=1,
            blocking_nets={2, 3},
            net_routes={},
            pads_by_net={},
            present_cost_factor=1.0,
            mark_route_callback=lambda r: None,
            ripup_history={},
            max_ripups_per_net=3,
            per_net_timeout=1.0,
            log=lines.append,
        )
        assert ok is False

    assert neg.targeted_ripup.call_count == 2
    first, second = (call.kwargs for call in neg.targeted_ripup.call_args_list)
    assert first["allow_trade"] is False
    assert second["allow_trade"] is True
    assert guard.max_pair_count() == 2
    assert len(guard.refusals) == 4
    # The geometric reason is recorded, and the refusals say why.
    assert [o.reason for o in guard.outcomes] == [RIPUP_VICTIM_GEOMETRIC] * 2
    assert sum("victim_geometric" in line for line in lines) == 2
    assert sum("refused" in line for line in lines) == 4


def test_negotiated_run_never_rips_a_pair_more_than_twice():
    """End to end on a reliably sealed pad (ring pads no rip-up can move).

    ``B.1`` sits inside four foreign ring pads: ripping ``BOX``'s copper can
    never free it, which is exactly the shape that used to be ripped every
    round.  The run must charge the pair at most twice and say the victim is
    geometrically stuck.
    """
    from tests.router.test_access_ripup_5913 import _boxed_in_router

    router = _boxed_in_router()
    router.route_all_negotiated(
        max_iterations=8,
        timeout=120,
        per_net_timeout=15,
        use_targeted_ripup=True,
        best_stall_patience=None,
    )

    guard = router._ripup_cycle_guard
    assert guard is not None
    assert guard.pair_counts, "the sealed net should have been sent to rip-up"
    assert guard.max_pair_count() <= MAX_PAIR_RIPUPS
    assert RIPUP_VICTIM_GEOMETRIC in {o.reason for o in guard.outcomes}


# ---------------------------------------------------------------------------
# The C++ phantom: targeted rip-up inside the corridor-reservation window
# ---------------------------------------------------------------------------

VICTIM, SEAL = 1, 2
SEAL_HALF = 0.8


def _pad(router, ref, pin, x, y, net, name):
    router.add_component(
        ref,
        [
            {
                "number": pin,
                "x": x,
                "y": y,
                "width": 0.5,
                "height": 0.5,
                "net": net,
                "net_name": name,
                "layer": Layer.F_CU,
            }
        ],
    )


def _seal_route() -> Route:
    """A ring of ``SEAL`` copper around ``B.1`` on both layers, tied to SEAL's pads."""
    c = 10.0
    d = SEAL_HALF
    ring = [(c - d, c - d), (c + d, c - d), (c + d, c + d), (c - d, c + d), (c - d, c - d)]
    segs = [
        Segment(x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=0.2, layer=layer, net=SEAL)
        for layer in (Layer.F_CU, Layer.B_CU)
        for a, b in zip(ring, ring[1:], strict=False)
    ]
    segs.append(Segment(x1=c + d, y1=c - d, x2=15.0, y2=3.0, width=0.2, layer=Layer.F_CU, net=SEAL))
    segs.append(
        Segment(x1=c + d, y1=c + d, x2=15.0, y2=17.0, width=0.2, layer=Layer.F_CU, net=SEAL)
    )
    return Route(net=SEAL, net_name="SEAL", segments=segs)


def _sealed_board() -> Autorouter:
    """``SEAL`` routed first walls ``VICTIM``'s pad in; the other order routes both."""
    router = Autorouter(20, 20, per_net_iterations=200_000)
    _pad(router, "B", "1", 10.0, 10.0, VICTIM, "VICTIM")
    _pad(router, "B", "2", 3.0, 14.3, VICTIM, "VICTIM")
    _pad(router, "A", "1", 15.0, 3.0, SEAL, "SEAL")
    _pad(router, "A", "2", 15.0, 17.0, SEAL, "SEAL")
    return router


def _seal_probe(router: Autorouter) -> tuple[int, int, int]:
    """A C++ cell under the seal ring's east side on B.Cu, far from any re-route."""
    gx, gy = router.grid.world_to_grid(10.0 + SEAL_HALF, 10.0)
    return gx, gy, router.grid.layer_to_index(Layer.B_CU.value)


@pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
@pytest.mark.parametrize("transaction", ["targeted_ripup", "_targeted_ripup_txn"])
def test_victim_is_retried_with_the_ripped_copper_freed_on_the_cpp_grid(transaction, monkeypatch):
    """The board-05 mechanism: the rip-up must free the copper it rips.

    ``route_all_negotiated`` opens a corridor-reservation window before its
    stall fallbacks, which defers every C++-grid unmark.  Before issue #6342 a
    targeted rip-up inside that window removed the blocker from the Python grid
    only; the C++ A* still saw it, so the victim failed against copper that was
    no longer there and the transaction rolled back.  ``_targeted_ripup_txn``
    is the bare transaction (the pre-#6342 behaviour) and is the control.

    The pure-Python A* fallback is disabled so the outcome depends on the C++
    grid alone, as it does on board 05 under the per-net iteration cap.
    """
    router = _sealed_board()
    cpp = router._cpp_grid
    assert cpp is not None
    monkeypatch.setattr(router.router, "_try_python_fallback", lambda *a, **k: None)
    seal = _seal_route()
    assert router._mark_route(seal)
    router.routes.append(seal)
    gx, gy, layer = _seal_probe(router)
    assert cpp._impl.at(gx, gy, layer).blocked

    net_routes: dict[int, list[Route]] = {SEAL: [seal]}
    history: dict[int, int] = {}
    router.grid.begin_cpp_unmark_deferral()
    neg = _neg(router)
    getattr(neg, transaction)(
        VICTIM,
        {SEAL},
        net_routes,
        router.routes,
        {n: [router.pads[k] for k in keys] for n, keys in router.nets.items() if n},
        1.0,
        router._mark_route,
        ripup_history=history,
        per_net_timeout=10.0,
    )

    outcome = neg.last_ripup_outcome
    if transaction == "targeted_ripup":
        assert outcome.reason == RIPUP_COMMITTED
        assert outcome.cpp_retry is True
        assert "retry" in outcome.describe()
        assert net_routes[VICTIM] and net_routes[SEAL]
        assert not cpp._impl.at(gx, gy, layer).blocked, "ripped copper is still phantom"
        # The retry's budget charge replaced the first attempt's.
        assert history == {SEAL: 1}
        # The window itself is still open for every other net.
        assert router.grid._cpp_unmark_deferred is True
    else:
        assert outcome.reason == RIPUP_VICTIM_GEOMETRIC
        assert net_routes[SEAL] == [seal]
        assert cpp._impl.at(gx, gy, layer).blocked


def test_no_retry_without_an_open_reservation_window():
    """Outside a window the rip-up was already mirrored; a retry cannot help."""

    def fake(self_neg, pads, *a, **k):
        return [] if pads[0].net == 1 else [_stub_route(pads[0].net)]

    calls: list[int] = []
    original = NegotiatedRouter._targeted_ripup_txn

    def counting(self_neg, *args, **kwargs):
        calls.append(1)
        return original(self_neg, *args, **kwargs)

    with patch.object(NegotiatedRouter, "_targeted_ripup_txn", counting):
        success, outcome, *_ = _run_ripup(fake)

    assert success is False
    assert outcome.reason == RIPUP_VICTIM_GEOMETRIC
    assert outcome.cpp_retry is False
    assert calls == [1]


@pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
def test_negotiated_run_recovers_when_the_first_net_seals_the_second():
    """AC: two nets that fail in one order and route in the other both land.

    ``SEAL`` is routed first and its copper walls ``VICTIM``'s pad in on both
    layers (forced, so the test does not depend on A* tie-breaking).  The
    router is not told the order: the access-loss witness names ``SEAL``, the
    targeted rip-up routes ``VICTIM`` first and re-lands ``SEAL`` around it.
    """
    router = _sealed_board()
    original = router._route_net_negotiated
    forced = {"done": False}

    def route_net(net, *args, **kwargs):
        if net == SEAL and not forced["done"]:
            forced["done"] = True
            seal = _seal_route()
            router._mark_route(seal)
            return [seal]
        return original(net, *args, **kwargs)

    router._route_net_negotiated = route_net  # type: ignore[method-assign]
    router._forced_net_order = [SEAL, VICTIM]
    router.route_all_negotiated(
        max_iterations=6, timeout=120, per_net_timeout=15, use_targeted_ripup=True
    )

    routed = {route.net for route in router.routes}
    assert routed == {VICTIM, SEAL}
    assert router.grid.get_total_overflow() == 0
    guard = router._ripup_cycle_guard
    assert guard is not None and guard.max_pair_count() <= MAX_PAIR_RIPUPS


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------


def test_cpp_unmark_immediate_restores_the_window_even_on_error():
    router = _sealed_board()
    grid = router.grid
    grid.begin_cpp_unmark_deferral()
    with pytest.raises(RuntimeError), grid.cpp_unmark_immediate():
        assert grid._cpp_unmark_deferred is False
        raise RuntimeError("boom")
    assert grid._cpp_unmark_deferred is True
    grid.flush_cpp_unmark_deferral()


# ---------------------------------------------------------------------------
# The witness log line
# ---------------------------------------------------------------------------


def _witness() -> AccessLossWitness:
    return AccessLossWitness(
        failed_net=30,
        failed_net_name="PHASE_C",
        pass_name="iteration",
        iteration=4,
        stranded_pads=(("U2", "9"),),
        closing_refs=("GND_SENSE", "VM"),
        closing_kinds=("route_segment",),
        blocking_nets=(7, 9),
        blocking_net_names=("GND_SENSE", "VM"),
        held_nets=(),
    )


def test_witness_line_no_longer_claims_a_rip_the_budget_filtered_out():
    line = _witness().one_line({7: "rip-up budget spent"})

    assert "-- ripping VM;" in line
    assert "ripping GND_SENSE," not in line
    assert "not ripping GND_SENSE (rip-up budget spent)" in line


def test_witness_line_when_nothing_named_will_be_ripped():
    line = _witness().one_line({7: "rip-up budget spent", 9: "protected victim"})

    assert "-- nothing ripped: " in line
    assert "not ripping VM (protected victim)" in line
    assert "not ripping GND_SENSE (rip-up budget spent)" in line


def test_witness_line_without_held_back_is_unchanged():
    assert _witness().one_line().endswith("-- ripping GND_SENSE, VM")
