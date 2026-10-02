"""Unit tests for the sequential N+1 rip-up prototype (Issue #5894 step 3).

Four layers, mirroring ``tests/test_router_crossing_order.py`` (step 2's
sibling):

1. Pure combinatorics in :mod:`kicad_tools.router.sequential_ripup` --
   ``select_ripup_targets`` (including the slot-0 guard) and
   ``is_improvement`` (the whole-pass gate predicate) -- against
   hand-constructed inputs with a known answer.
2. Frontier-cell blocker attribution: that ``_frontier_blockers`` delegates to
   the existing relaxed-A* helper ``NegotiatedRouter.find_blocking_nets_relaxed``
   (Issue #2274) rather than re-deriving a straight-line guess.
3. The per-net N+1 escalation transaction against a scripted fake router:
   soft-cost pricing of the ripped corridor, escalation when N=1 is not
   enough, and the all-or-nothing rollback when a displaced sibling cannot
   re-land.
4. The ``--ripup-strategy sequential-n1`` plumbing (outer parser, inner
   parser, routing shim, ``_apply_ripup_strategy``) and the two dispatch
   sites the flag needs to be a no-op nowhere -- ``route_all_negotiated``
   *and* ``route_all_two_phase``.  Layer 4 exists because the first #5894
   fleet run measured the flag as a silent no-op on board 03, which routes
   through ``TwoPhaseRouter`` and never reaches ``route_all_negotiated``
   (the #5908 shape, hit once already by step 2's ``--order-method``).
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.router import sequential_ripup as sr

# ---------------------------------------------------------------------------
# Layer 1: select_ripup_targets / is_improvement (pure)
# ---------------------------------------------------------------------------


def test_targets_are_ordered_by_blocker_score_descending():
    targets = sr.select_ripup_targets({7: 1, 8: 3, 9: 2}, {7, 8, 9}, escalation=3)
    assert targets == [8, 9, 7]


def test_target_ties_break_on_net_id_for_determinism():
    targets = sr.select_ripup_targets({9: 2, 4: 2, 6: 2}, {4, 6, 9}, escalation=3)
    assert targets == [4, 6, 9]


def test_escalation_caps_the_number_of_targets():
    scores = {1: 5, 2: 4, 3: 3, 4: 2}
    assert sr.select_ripup_targets(scores, {1, 2, 3, 4}, escalation=1) == [1]
    assert sr.select_ripup_targets(scores, {1, 2, 3, 4}, escalation=2) == [1, 2]
    assert sr.select_ripup_targets(scores, {1, 2, 3, 4}, escalation=4) == [1, 2, 3, 4]


def test_slot_zero_guard_excludes_a_net_this_pass_has_not_placed():
    """The top-scoring blocker is skipped when nothing has been committed for it.

    Ripping an unplaced net is a no-op that would silently burn an escalation
    slot without displacing any copper -- the "slot-0 guard" of #5894.
    """
    targets = sr.select_ripup_targets({42: 99, 7: 1}, committed_nets={7}, escalation=2)
    assert targets == [7]


def test_slot_zero_guard_can_empty_the_target_list_entirely():
    assert sr.select_ripup_targets({42: 99}, committed_nets=set(), escalation=3) == []


def test_improvement_gate_prefers_more_connections_even_with_more_vias():
    assert sr.is_improvement((10, 50), (9, 0)) is True


def test_improvement_gate_rejects_fewer_connections_even_with_fewer_vias():
    assert sr.is_improvement((8, 0), (9, 50)) is False


def test_improvement_gate_breaks_a_connection_tie_on_vias():
    assert sr.is_improvement((9, 4), (9, 5)) is True
    assert sr.is_improvement((9, 6), (9, 5)) is False


def test_improvement_gate_keeps_an_exact_tie():
    """An identical pass is "at least as good", so the rip-up arm is kept."""
    assert sr.is_improvement((9, 5), (9, 5)) is True


# ---------------------------------------------------------------------------
# Layer 2: frontier-cell blocker attribution
# ---------------------------------------------------------------------------


def test_frontier_blockers_delegates_to_the_relaxed_search():
    """Attribution must reuse the #2274 relaxed-A* helper, not re-derive it.

    ``find_blocking_nets`` (the other helper in ``negotiated.py``) walks a
    Bresenham straight line between pads; ``find_blocking_nets_relaxed``
    re-runs the failing net's OWN search with routed cells unblocked, so the
    cells it reports are the real frontier.  This test pins which one the
    prototype calls, and that it scopes the call to the single failing net.
    """
    router = SimpleNamespace(grid=object(), router=object(), rules=object(), net_class_map={})
    pads = [object(), object()]
    seen: dict = {}

    def fake_relaxed(self, failed_nets, pads_by_net, per_net_timeout=None):
        seen["failed_nets"] = failed_nets
        seen["pads_by_net"] = pads_by_net
        seen["per_net_timeout"] = per_net_timeout
        return {5: 2, 6: 1}

    with patch.object(sr.NegotiatedRouter, "find_blocking_nets_relaxed", fake_relaxed):
        scores = sr._frontier_blockers(router, 11, pads, per_net_timeout=7.5)

    assert scores == {5: 2, 6: 1}
    assert seen["failed_nets"] == [11]
    assert seen["pads_by_net"] == {11: pads}
    assert seen["per_net_timeout"] == 7.5


def test_frontier_blockers_is_not_the_straight_line_helper():
    """Guard against a future "simplification" back to the Bresenham variant."""
    import inspect

    source = inspect.getsource(sr._frontier_blockers)
    assert "find_blocking_nets_relaxed" in source
    assert "find_blocking_nets(" not in source


# ---------------------------------------------------------------------------
# Scripted fake router for layer 3
# ---------------------------------------------------------------------------


class _FakeRoute:
    """A route is just the set of grid cells it occupies, plus its vias."""

    def __init__(self, net: int, cells, vias=()) -> None:
        self.net = net
        self.cells = frozenset(cells)
        self.vias = list(vias)
        self.is_escape = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeRoute net={self.net} cells={sorted(self.cells)}>"


class _FakeGrid:
    """Tracks the two independent grid ledgers the real code must keep in sync.

    ``marked`` is the blocking ledger written by ``_mark_route`` /
    ``unmark_route``; ``usage`` is the congestion-count ledger written by
    ``mark_route_usage`` / ``unmark_route_usage``.  The real rip-up helpers
    must touch exactly one of them for a discarded partial result and both for
    a committed net (see ``sequential_ripup``'s commit/undo section), so the
    tests assert against both.
    """

    def __init__(self) -> None:
        self.marked: dict[str, int] = {}
        self.usage: dict[str, int] = {}

    def mark_route(self, route: _FakeRoute) -> bool:
        for cell in route.cells:
            self.marked[cell] = route.net
        return True

    def unmark_route(self, route: _FakeRoute) -> None:
        for cell in route.cells:
            if self.marked.get(cell) == route.net:
                del self.marked[cell]

    def mark_route_usage(self, route: _FakeRoute) -> None:
        for cell in route.cells:
            self.usage[cell] = self.usage.get(cell, 0) + 1

    def unmark_route_usage(self, route: _FakeRoute) -> None:
        for cell in route.cells:
            self.usage[cell] = self.usage.get(cell, 0) - 1
            if self.usage[cell] <= 0:
                del self.usage[cell]


class _ScriptRouter:
    """Minimal ``Autorouter`` stand-in exposing only what the prototype reads.

    Routing model: each net wants a fixed set of cells.  At a HIGH
    ``present_cost_factor`` (the prototype's ``hard_cost``) a net cannot share
    a cell another net already marked, so it fails; at a LOW factor (the
    ``soft_ripup_cost``) it can -- which is exactly the "soft cost on the
    ripped corridor" distinction the strategy relies on, and makes the
    sibling-reroute pricing observable.
    """

    def __init__(
        self,
        cells_by_net: dict[int, set[str]],
        *,
        share_above_factor: float = 10.0,
        pads_per_net: int = 2,
        vias_when_sharing: int = 0,
        never_routable: frozenset[int] = frozenset(),
        partial_nets: frozenset[int] = frozenset(),
    ) -> None:
        self.cells_by_net = cells_by_net
        self.share_above_factor = share_above_factor
        self.vias_when_sharing = vias_when_sharing
        self.never_routable = never_routable
        self.partial_nets = partial_nets
        self.grid = _FakeGrid()
        self.routes: list[_FakeRoute] = []
        self.route_calls: list[tuple[int, float]] = []
        self._forced_net_order: list[int] | None = None
        self._escape_pad_overrides: dict = {}
        self.nets = {net: [("U1", str(i)) for i in range(pads_per_net)] for net in cells_by_net}
        self.pads = {
            (ref, pad): SimpleNamespace(x=0.0, y=0.0)
            for net in cells_by_net
            for (ref, pad) in self.nets[net]
        }

    # -- net-order plumbing read by _build_net_order / _build_pads_by_net ---
    def _get_net_priority(self, net: int) -> int:
        return net

    def _filter_pour_nets(self, order: list[int]) -> list[int]:
        return list(order)

    def _interleave_match_groups(self, order: list[int]) -> list[int]:
        return list(order)

    def _region_filtered_pad_keys(self, net: int, netlist) -> list:
        return list(netlist)

    def _stub_target_pads(self, net: int) -> list:
        return []

    # -- the one commit path ------------------------------------------------
    def _mark_route(self, route: _FakeRoute, *, enforce_pad_access: bool = False) -> bool:
        return self.grid.mark_route(route)

    def _route_net_negotiated(
        self,
        net: int,
        present_cost_factor: float,
        per_net_timeout=None,
        failure_callback=None,
    ) -> list[_FakeRoute]:
        self.route_calls.append((net, present_cost_factor))
        pad_pair = (object(), object())
        if net in self.never_routable:
            if failure_callback is not None:
                failure_callback(*pad_pair)
            return []
        want = self.cells_by_net[net]
        occupied = {c for c, owner in self.grid.marked.items() if owner != net}
        sharing = bool(want & occupied)
        if sharing and present_cost_factor > self.share_above_factor:
            # Hard cost: the cell is effectively blocked.  Report it through
            # the #2425 failure callback, which is the prototype's per-net
            # success predicate (see ``sequential_ripup._route_net``).
            if failure_callback is not None:
                failure_callback(*pad_pair)
            return []
        vias = [object()] * (self.vias_when_sharing if sharing else 0)
        route = _FakeRoute(net, want, vias=vias)
        self._mark_route(route, enforce_pad_access=True)
        if self.partial_nets and net in self.partial_nets and failure_callback is not None:
            # A net that placed SOME copper but left an edge unrouted -- the
            # case the route-count proxy mis-reads as success.
            failure_callback(*pad_pair)
        return [route]


def _cfg(**kwargs) -> sr._Config:
    base = {
        "per_net_timeout": None,
        "max_escalation": sr.DEFAULT_MAX_ESCALATION,
        "hard_cost": sr.DEFAULT_HARD_COST,
        "soft_ripup_cost": sr.DEFAULT_SOFT_RIPUP_COST,
    }
    base.update(kwargs)
    return sr._Config(**base)


def _pads(router: _ScriptRouter, net: int) -> list:
    return [router.pads[key] for key in router.nets[net]]


# ---------------------------------------------------------------------------
# Layer 3: per-net N+1 escalation transaction
# ---------------------------------------------------------------------------


def test_a_net_that_routes_directly_never_reaches_attribution(monkeypatch):
    router = _ScriptRouter({1: {"a"}})
    called = []
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: called.append(1) or {})

    net_routes: dict[int, list] = {}
    assert sr._attempt_net(router, 1, _pads(router, 1), net_routes, _cfg()) is True

    assert called == []
    assert [r.net for r in router.routes] == [1]
    assert router.grid.usage == {"a": 1}
    assert router.route_calls == [(1, sr.DEFAULT_HARD_COST)]


def test_n_equals_one_ripup_places_the_failed_net_and_reroutes_the_sibling(monkeypatch):
    """The core loop: rip the top blocker, land the failed net, re-land the sibling."""
    router = _ScriptRouter({1: {"a"}, 2: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 1})

    net_routes: dict[int, list] = {}
    sr._attempt_net(router, 1, _pads(router, 1), net_routes, _cfg())
    assert sr._attempt_net(router, 2, _pads(router, 2), net_routes, _cfg()) is True

    assert sorted(r.net for r in router.routes) == [1, 2]
    assert net_routes[1] and net_routes[2]
    # Both ledgers stay consistent: the shared cell is used by two nets.
    assert router.grid.usage == {"a": 2}


def test_the_displaced_sibling_is_rerouted_at_the_soft_cost(monkeypatch):
    """Soft-cost pricing (module docstring point 4), not a flat prohibition."""
    router = _ScriptRouter({1: {"a"}, 2: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 1})

    net_routes: dict[int, list] = {}
    sr._attempt_net(router, 1, _pads(router, 1), net_routes, _cfg())
    router.route_calls.clear()
    sr._attempt_net(router, 2, _pads(router, 2), net_routes, _cfg())

    # The failed net is always retried at the hard cost; only the displaced
    # sibling gets the soft one.
    assert (2, sr.DEFAULT_HARD_COST) in router.route_calls
    assert (1, sr.DEFAULT_SOFT_RIPUP_COST) in router.route_calls
    assert (1, sr.DEFAULT_HARD_COST) not in router.route_calls


def test_escalation_grows_to_n_plus_one_when_one_rip_is_not_enough(monkeypatch):
    """Net 3 needs BOTH blockers out of the way; N=1 must escalate to N=2."""
    router = _ScriptRouter({1: {"a"}, 2: {"b"}, 3: {"a", "b"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 2, 2: 1})

    net_routes: dict[int, list] = {}
    sr._attempt_net(router, 1, _pads(router, 1), net_routes, _cfg())
    sr._attempt_net(router, 2, _pads(router, 2), net_routes, _cfg())
    router.route_calls.clear()
    assert sr._attempt_net(router, 3, _pads(router, 3), net_routes, _cfg()) is True

    # Escalation 1 ripped only net 1 (higher score) -> net 3 still blocked by
    # net 2's cell; escalation 2 ripped both and succeeded.
    assert sorted(r.net for r in router.routes) == [1, 2, 3]
    assert (1, sr.DEFAULT_SOFT_RIPUP_COST) in router.route_calls
    assert (2, sr.DEFAULT_SOFT_RIPUP_COST) in router.route_calls


def test_escalation_is_capped_by_max_escalation(monkeypatch):
    """With the cap at 1, the two-blocker net can never land."""
    router = _ScriptRouter({1: {"a"}, 2: {"b"}, 3: {"a", "b"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 2, 2: 1})

    net_routes: dict[int, list] = {}
    cfg = _cfg(max_escalation=1)
    sr._attempt_net(router, 1, _pads(router, 1), net_routes, cfg)
    sr._attempt_net(router, 2, _pads(router, 2), net_routes, cfg)
    assert sr._attempt_net(router, 3, _pads(router, 3), net_routes, cfg) is False

    # Both blockers are restored byte-for-byte and net 3 placed nothing.
    assert sorted(r.net for r in router.routes) == [1, 2]
    assert not net_routes.get(3)


def test_a_sibling_that_cannot_reland_rolls_the_whole_attempt_back(monkeypatch):
    """The improvement-gate's per-attempt sibling: all-or-nothing revert.

    Net 2 is scripted as unroutable on its RETRY (``never_routable``), so the
    attempt must restore net 2's original Route objects verbatim -- same
    ``id()``, both grid ledgers back to their pre-attempt values -- and leave
    net 3 unrouted rather than keeping a half-applied result.
    """
    router = _ScriptRouter({2: {"a"}, 3: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {2: 1})

    net_routes: dict[int, list] = {}
    sr._attempt_net(router, 2, _pads(router, 2), net_routes, _cfg())
    original = net_routes[2][0]
    marked_before = dict(router.grid.marked)
    usage_before = dict(router.grid.usage)

    router.never_routable = frozenset({2})
    assert sr._attempt_net(router, 3, _pads(router, 3), net_routes, _cfg()) is False

    assert net_routes[2] == [original]
    assert router.routes == [original]
    assert router.grid.marked == marked_before
    assert router.grid.usage == usage_before
    assert not net_routes.get(3)


def test_a_partial_result_is_discarded_without_touching_the_usage_ledger(monkeypatch):
    """A below-``expected`` result never reached ``_commit``, so usage must be 0.

    Decrementing ``unmark_route_usage`` for copper that never incremented it
    is the #3501 shape; the prototype's ``_discard_partial`` must only call
    ``unmark_route``.
    """
    router = _ScriptRouter({1: {"a"}}, pads_per_net=3, partial_nets=frozenset({1}))
    # Nothing is committed yet, so the slot-0 guard would empty the target list
    # anyway; stub the attribution so the test does not need a real grid.
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {})
    net_routes: dict[int, list] = {}

    # The scripted router places copper AND reports one failed RSMT edge, i.e.
    # a partial result.
    assert sr._attempt_net(router, 1, _pads(router, 1), net_routes, _cfg()) is False
    assert router.grid.marked == {}
    assert router.grid.usage == {}
    assert router.routes == []


def test_success_is_zero_failed_edges_not_a_route_count(monkeypatch):
    """Regression guard for the measurement bug the #5894 fleet run exposed.

    A net that placed copper but left an RSMT edge unrouted used to clear the
    ``len(routes) >= len(pads) - 1`` proxy -- intra-IC / block-internal routes
    count toward it -- so on board 02 all 10 nets were declared successful, the
    escalation never engaged, and the "rip-up vs. no-rip-up" comparison was
    vacuous (``0 escalation(s)``).  Success must be "zero failed edges".
    """
    router = _ScriptRouter({1: {"a"}, 2: {"b"}}, partial_nets=frozenset({2}))
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {})
    stats = sr._PassStats()
    net_routes: dict[int, list] = {}

    # Net 1 reports no failed edge -> success; net 2 places copper but reports
    # one -> NOT success, even though its route count matches net 1's.
    assert sr._attempt_net(router, 1, _pads(router, 1), net_routes, _cfg(), stats) is True
    assert sr._attempt_net(router, 2, _pads(router, 2), net_routes, _cfg(), stats) is False
    assert stats.direct_success == 1
    assert [r.net for r in router.routes] == [1]


def test_escape_only_copper_does_not_mark_a_net_pre_routed():
    """Board 03's net universe went 24 -> 6 before this (see the helper's docstring)."""
    router = _ScriptRouter({1: {"a"}, 2: {"b"}})
    key = router.nets[1][0]
    escape_stub = _FakeRoute(1, {"zz"})
    router.routes.append(escape_stub)  # NOT flagged is_escape, like a prephase stub
    router._escape_pad_overrides = {key: router.pads[key]}
    router.pads[key].net = 1

    assert sr._escape_infrastructure_nets(router) == {1}
    assert sr._build_net_order(router) == [1, 2]


def test_a_genuinely_pre_routed_net_is_still_skipped():
    router = _ScriptRouter({1: {"a"}, 2: {"b"}})
    router.routes.append(_FakeRoute(1, {"zz"}))
    assert sr._build_net_order(router) == [2]


def test_a_pose_coupled_claimed_net_is_never_re_routed():
    """#5786's coupled trunk must not come back as two independent legs."""
    router = _ScriptRouter({1: {"a"}, 2: {"b"}})
    router._pose_coupled_nets = {2}
    assert sr._build_net_order(router) == [1]


def test_route_net_forwards_a_failure_callback_to_the_core_helper():
    """The predicate depends on ``_route_net_negotiated`` accepting the kwarg."""
    import inspect

    from kicad_tools.router import core

    signature = inspect.signature(core.Autorouter._route_net_negotiated)
    assert "failure_callback" in signature.parameters
    assert signature.parameters["failure_callback"].default is None
    body = inspect.getsource(core.Autorouter._route_net_negotiated)
    assert "failure_callback=failure_callback" in body


def test_pass_stats_distinguish_a_tie_with_no_escalation_from_a_rolled_back_one(monkeypatch):
    """The counters the fleet verdict rests on, exercised on both outcomes.

    The #5894 comparison found the rip-up pass and the no-rip-up baseline tied
    on every board.  ``escalations == 0`` (no net ever failed) and
    ``escalations > 0, ripup_commits == 0`` (every escalation was rolled back)
    both produce that tie but mean opposite things, so the counters have to
    separate them.
    """
    # (a) Nothing ever fails: no escalation at all.
    quiet = _ScriptRouter({1: {"a"}, 2: {"b"}})
    stats = sr._PassStats()
    net_routes: dict[int, list] = {}
    for net in (1, 2):
        sr._attempt_net(quiet, net, _pads(quiet, net), net_routes, _cfg(), stats)
    assert (stats.direct_success, stats.escalations, stats.ripup_commits) == (2, 0, 0)
    assert "0 escalation(s)" in stats.summary()

    # (b) A net fails and every escalation is rolled back by its sibling.
    busy = _ScriptRouter({2: {"a"}, 3: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {2: 1})
    stats = sr._PassStats()
    net_routes = {}
    sr._attempt_net(busy, 2, _pads(busy, 2), net_routes, _cfg(), stats)
    busy.never_routable = frozenset({2})
    sr._attempt_net(busy, 3, _pads(busy, 3), net_routes, _cfg(max_escalation=2), stats)
    assert stats.escalations > 0
    assert stats.ripup_commits == 0
    assert stats.sibling_rollbacks > 0


def test_a_committed_ripup_is_counted_as_such(monkeypatch):
    router = _ScriptRouter({1: {"a"}, 2: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 1})
    stats = sr._PassStats()
    net_routes: dict[int, list] = {}
    sr._attempt_net(router, 1, _pads(router, 1), net_routes, _cfg(), stats)
    sr._attempt_net(router, 2, _pads(router, 2), net_routes, _cfg(), stats)
    assert (stats.direct_success, stats.escalations, stats.ripup_commits) == (1, 1, 1)
    assert stats.sibling_rollbacks == 0


# ---------------------------------------------------------------------------
# Layer 3b: whole-pass improvement gate
# ---------------------------------------------------------------------------


def test_the_gate_keeps_the_ripup_pass_when_it_connects_more_nets(monkeypatch):
    """Rip-up lands 2 nets on a contended cell; the no-rip-up baseline lands 1."""
    router = _ScriptRouter({1: {"a"}, 2: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 1})

    result = sr.route_all_sequential_ripup(router, max_escalation=2)

    assert sorted(r.net for r in result) == [1, 2]
    assert sorted(r.net for r in router.routes) == [1, 2]
    # Exactly one ledger entry per restored route -- the keep branch must not
    # double-mark the treatment copper it re-applies.
    assert router.grid.usage == {"a": 2}


def test_the_gate_reverts_to_the_baseline_when_the_ripup_pass_is_worse(monkeypatch):
    """Revert-on-regression: the treatment copper is dropped, baseline kept.

    The transaction rules make a genuinely worse rip-up pass hard to provoke
    from real geometry (rip-up only ever ADDS connections), so the regression
    is injected at the metric boundary -- which is precisely the comparison
    the gate is responsible for acting on.
    """
    router = _ScriptRouter({1: {"a"}, 2: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 1})

    verdicts = iter([sr._RunMetrics(1, 99), sr._RunMetrics(1, 0)])
    monkeypatch.setattr(sr, "_metrics", lambda net_routes: next(verdicts))

    result = sr.route_all_sequential_ripup(router, max_escalation=2)

    # Only the no-rip-up baseline's copper survives: net 1 placed, net 2 not.
    assert [r.net for r in result] == [1]
    assert [r.net for r in router.routes] == [1]
    assert router.grid.usage == {"a": 1}
    assert router.grid.marked == {"a": 1}


@pytest.mark.parametrize("gate,expected_passes", [(True, 2), (False, 1)])
def test_the_gate_costs_exactly_one_extra_whole_pass(monkeypatch, gate, expected_passes):
    """``enable_improvement_gate=False`` must skip the baseline pass entirely.

    The gate doubles the per-net routing work in the worst case, which is the
    headline cost the research doc has to report -- so the "off" switch has to
    genuinely not run the second pass, not merely ignore its result.
    """
    router = _ScriptRouter({1: {"a"}, 2: {"a"}})
    monkeypatch.setattr(sr, "_frontier_blockers", lambda *a, **k: {1: 1})

    real_run_pass = sr._run_pass
    labels: list[str] = []

    def counting_run_pass(*args, **kwargs):
        labels.append(kwargs.get("label", "?"))
        return real_run_pass(*args, **kwargs)

    monkeypatch.setattr(sr, "_run_pass", counting_run_pass)
    sr.route_all_sequential_ripup(router, max_escalation=2, enable_improvement_gate=gate)

    assert len(labels) == expected_passes
    assert labels[0] == "rip-up"
    if gate:
        assert labels[1] == "no-rip-up baseline"


def test_the_pass_skips_nets_with_fewer_than_two_pads():
    router = _ScriptRouter({1: {"a"}}, pads_per_net=1)
    assert sr.route_all_sequential_ripup(router) == []
    assert router.route_calls == []


def test_a_cancelling_progress_callback_stops_the_pass():
    router = _ScriptRouter({1: {"a"}, 2: {"b"}, 3: {"c"}})
    seen: list[str] = []

    def cancel_after_first(fraction, message, cancelable):
        seen.append(message)
        return False

    sr.route_all_sequential_ripup(
        router, progress_callback=cancel_after_first, enable_improvement_gate=False
    )
    assert len(seen) == 1
    assert [r.net for r in router.routes] == [1]


# ---------------------------------------------------------------------------
# Layer 4: CLI plumbing + the two dispatch sites
# ---------------------------------------------------------------------------


def _flags_choices(parser: argparse.ArgumentParser, flag: str) -> list[str]:
    for action in parser._actions:
        if flag in action.option_strings:
            return list(action.choices or [])
    raise AssertionError(f"{flag} not declared on {parser.prog!r}")


def _inner_parser() -> argparse.ArgumentParser:
    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            route_main([])
    return captured["parser"]


def test_outer_parser_defaults_to_negotiated():
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["route", "in.kicad_pcb"])
    assert args.ripup_strategy == "negotiated"


def test_outer_parser_accepts_sequential_n1():
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["route", "in.kicad_pcb", "--ripup-strategy", "sequential-n1"]
    )
    assert args.ripup_strategy == "sequential-n1"


def test_inner_route_parser_accepts_sequential_n1():
    parser = _inner_parser()
    assert _flags_choices(parser, "--ripup-strategy") == ["negotiated", "sequential-n1"]
    assert parser.parse_args(["x.kicad_pcb"]).ripup_strategy == "negotiated"


def test_outer_and_inner_choice_lists_agree():
    """The shim forwards ``str(args.ripup_strategy)`` -- the lists must match."""
    from kicad_tools.cli.parser import create_parser

    outer = None
    for action in create_parser()._actions:
        if action.dest == "command":
            outer = action.choices["route"]
            break
    assert outer is not None
    assert _flags_choices(outer, "--ripup-strategy") == _flags_choices(
        _inner_parser(), "--ripup-strategy"
    )


def test_routing_shim_forwards_only_a_non_default_value():
    """``negotiated`` must not be forwarded -- the default stays byte-identical."""
    import inspect

    from kicad_tools.cli.commands import routing

    source = inspect.getsource(routing.run_route_command)
    assert 'getattr(args, "ripup_strategy", "negotiated") != "negotiated"' in source
    assert '"--ripup-strategy"' in source


def test_apply_ripup_strategy_is_a_no_op_at_the_default():
    from kicad_tools.cli import route_cmd

    router = SimpleNamespace(_ripup_strategy="negotiated")
    route_cmd._apply_ripup_strategy(router, SimpleNamespace(ripup_strategy="negotiated"))
    assert router._ripup_strategy == "negotiated"
    route_cmd._apply_ripup_strategy(router, SimpleNamespace())
    assert router._ripup_strategy == "negotiated"


def test_apply_ripup_strategy_installs_the_prototype():
    from kicad_tools.cli import route_cmd

    router = SimpleNamespace(_ripup_strategy="negotiated")
    route_cmd._apply_ripup_strategy(router, SimpleNamespace(ripup_strategy="sequential-n1"))
    assert router._ripup_strategy == "sequential-n1"


def test_autorouter_defaults_to_the_negotiated_strategy():
    """The prototype is opt-in: no constructor default change (#5894 AC 1)."""
    import inspect

    from kicad_tools.router import core

    source = inspect.getsource(core.Autorouter.__init__)
    assert 'self._ripup_strategy: str = "negotiated"' in source


@pytest.mark.parametrize(
    "func_name",
    [
        "route_with_layer_escalation",
        "route_with_rule_relaxation",
        "route_with_combined_escalation",
        "_run_main_impl",
    ],
)
def test_every_route_entry_point_applies_the_strategy(func_name):
    """A flag wired into only one attempt helper is a silent no-op (#5908)."""
    import inspect

    from kicad_tools.cli import route_cmd

    source = inspect.getsource(getattr(route_cmd, func_name))
    assert "_apply_ripup_strategy(router, args)" in source, (
        f"{func_name} does not apply --ripup-strategy; the flag would be a "
        "silent no-op on this path (Issue #5894)"
    )


@pytest.mark.parametrize("method_name", ["route_all_negotiated", "route_all_two_phase"])
def test_both_detailed_routing_loops_dispatch_to_the_prototype(method_name):
    """``route_all_two_phase`` is a SEPARATE loop -- it needs its own dispatch.

    Every escape-routed board (``route_with_escape`` ->
    ``route_all_two_phase`` -> ``TwoPhaseRouter._detailed_negotiated``) never
    calls ``route_all_negotiated``, which is how the first #5894 fleet run
    measured the flag as inert on board 03.
    """
    import inspect

    from kicad_tools.router import core

    source = inspect.getsource(getattr(core.Autorouter, method_name))
    assert 'getattr(self, "_ripup_strategy", "negotiated") == "sequential-n1"' in source
    assert "route_all_sequential_ripup" in source
