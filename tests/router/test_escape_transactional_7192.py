"""Escape rollback flushes the C++ grid; per-strategy budgets; cut flag (Issue #7192).

* A rolled-back escape strategy inside an open corridor-reservation window
  must not leave its copper on the C++ grid for the next strategy.
* Each strategy has its own reroute allowance; the full reorder's covers
  every routable net.
* ``EscapeAttempts.cut`` is set only when a routable (>= 2 pads) net is left
  without budget.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.router.algorithms.negotiated import EscapeAttempts, NegotiatedRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def _router() -> Autorouter:
    router = Autorouter(width=40.0, height=30.0)
    for net in (1, 2):
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


def _escape(neg, net_routes, routes_list, mark, pads_by_net=None, **extra):
    return neg.escape_local_minimum(
        overflow_history=[2, 2, 2],
        net_routes=net_routes,
        routes_list=routes_list,
        pads_by_net=pads_by_net or {},
        net_order=[],
        present_cost_factor=0.5,
        mark_route_callback=mark,
        **extra,
    )


def _cpp_net_at(router, x, y):
    gx, gy = router.grid.world_to_grid(x, y)
    return int(router._cpp_grid._impl.at(gx, gy, 0).net)


def _setup_window(strategy2_probe=None):
    router = _router()
    neg = NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )
    kept = _seg_route(1, 7.0)
    router._mark_route(kept)
    router.grid.mark_route_usage(kept)
    router.routes.append(kept)
    net_routes = {1: [kept]}
    return router, neg, kept, net_routes


@pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
def test_failed_strategy_copper_flushed_from_cpp_grid():
    router, neg, kept, net_routes = _setup_window()
    fresh = _seg_route(1, 19.0)
    seen_in_second: list[int] = []

    def first(**kw):
        neg.rip_up_nets([1], net_routes, router.routes)
        net_routes[1] = [fresh]
        router._mark_route(fresh)
        router.grid.mark_route_usage(fresh)
        router.routes.append(fresh)
        return False, 1

    def second(**kw):
        seen_in_second.append(_cpp_net_at(router, 20.0, 19.0))
        return False, 1

    neg._escape_shuffle_order = first
    neg._escape_reverse_order = second
    neg._escape_random_subset = second
    neg._escape_full_reorder = second

    router.grid.begin_cpp_unmark_deferral()
    success, _, tried = _escape(neg, net_routes, router.routes, router._mark_route)

    assert success is False and tried == 4
    assert seen_in_second and all(n == 0 for n in seen_in_second), "discarded copper still on C++"
    assert _cpp_net_at(router, 20.0, 7.0) == 1, "restored copper still set"
    assert net_routes[1] == [kept]
    router.grid.flush_cpp_unmark_deferral()


def test_rollback_without_window_does_not_flush():
    router, neg, kept, net_routes = _setup_window()
    neg._escape_shuffle_order = lambda **kw: (False, 1)
    neg._escape_reverse_order = lambda **kw: (False, 1)
    neg._escape_random_subset = lambda **kw: (False, 1)
    neg._escape_full_reorder = lambda **kw: (False, 1)
    with patch.object(
        router.grid, "flush_discarded_cpp_unmarks", wraps=router.grid.flush_discarded_cpp_unmarks
    ) as flush:
        success, _, _ = _escape(neg, net_routes, router.routes, router._mark_route)
    assert success is False
    assert all(call.args == (0,) for call in flush.call_args_list)
    assert router.grid.pending_cpp_unmark_count() == 0


def _fake_pads(n):
    return [MagicMock(), MagicMock()] if n >= 2 else [MagicMock()][:n]


class _FakeGrid:
    def __init__(self):
        self.overflow = 3

    def find_overused_cells(self):
        return {(0, 0)}

    def get_total_overflow(self):
        return self.overflow

    def mark_route_usage(self, route):
        pass

    def unmark_route_usage(self, route):
        pass

    def unmark_route(self, route):
        pass


def _neg_with_nets(n_nets):
    neg = NegotiatedRouter(_FakeGrid(), MagicMock(), MagicMock(), {})
    nets = list(range(1, n_nets + 1))
    net_routes = {n: [object()] for n in nets}
    routes_list = [r for rs in net_routes.values() for r in rs]
    pads_by_net = {n: _fake_pads(2) for n in nets}
    neg.find_nets_through_overused_cells = lambda nr, overused: list(nets)
    return neg, net_routes, routes_list, pads_by_net


def test_full_reorder_budget_covers_scope():
    neg, net_routes, routes_list, pads_by_net = _neg_with_nets(6)
    calls: list[int] = []

    def reroute(pads, cost, cb, per_net_timeout=None):
        calls.append(1)
        neg.grid.overflow = 0  # every reroute "fixes" things
        return [object()]

    # Strategies 1-3 fail without consuming the full-reorder allowance.
    neg._escape_shuffle_order = lambda **kw: (False, 3)
    neg._escape_reverse_order = lambda **kw: (False, 3)
    neg._escape_random_subset = lambda **kw: (False, 3)
    with patch.object(NegotiatedRouter, "route_net_negotiated", side_effect=reroute):
        success, _, tried = _escape(
            neg, net_routes, routes_list, lambda r: None, pads_by_net=pads_by_net, reroute_cap=2
        )
    assert success is True, "full reorder (6 nets, cap 2) must run to completion and be kept"
    assert tried == 4
    assert len(calls) == 6
    assert neg.last_escape_summary.reroute_cap == 6


def test_earlier_strategies_do_not_starve_later_ones():
    neg, net_routes, routes_list, pads_by_net = _neg_with_nets(4)
    neg.grid.overflow = 3

    def reroute(pads, cost, cb, per_net_timeout=None):
        return [object()]

    with patch.object(NegotiatedRouter, "route_net_negotiated", side_effect=reroute):
        # cap 4 -> shuffle spends all 4 yet is not cut (no net left); failure
        # (overflow unchanged) must still let reverse/subset/full run.
        success, _, tried = _escape(
            neg, net_routes, routes_list, lambda r: None, pads_by_net=pads_by_net, reroute_cap=4
        )
    assert success is False
    assert tried == 4
    assert neg.last_escape_summary.reroutes > 4


def _run_strategy(strategy_name, pads_by_net, cap):
    neg, net_routes, routes_list, _ = _neg_with_nets(len(pads_by_net))
    attempts = EscapeAttempts(cap)

    def reroute(pads, cost, cb, per_net_timeout=None):
        neg.grid.overflow = 0
        return [object()]

    with patch.object(NegotiatedRouter, "route_net_negotiated", side_effect=reroute):
        result = getattr(neg, strategy_name)(
            overflow_history=[3],
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net=pads_by_net,
            net_order=list(pads_by_net),
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            attempts=attempts,
        )
    return result, attempts


@pytest.mark.parametrize(
    "strategy",
    [
        "_escape_shuffle_order",
        "_escape_reverse_order",
        "_escape_random_subset",
        "_escape_full_reorder",
    ],
)
def test_cut_flag_ignores_unroutable_nets(strategy, monkeypatch):
    # Nets 1-2 routable, net 3 has a single pad.  Whatever order the strategy
    # visits them in, the cap of 2 is spent on the routable pair and the
    # 1-pad net must not mark the strategy as cut.
    pads = {1: _fake_pads(2), 2: _fake_pads(2), 3: _fake_pads(1)}
    cap = 2
    # Make the random subset take every conflicting net.
    monkeypatch.setattr("random.sample", lambda population, k: list(population))
    (success, _), attempts = _run_strategy(strategy, pads, cap)
    assert attempts.cut is False
    assert attempts.used == cap
    assert success is True


@pytest.mark.parametrize("strategy", ["_escape_shuffle_order", "_escape_full_reorder"])
def test_cut_flag_set_when_routable_net_remains(strategy):
    pads = {1: _fake_pads(2), 2: _fake_pads(2), 3: _fake_pads(2)}
    _, attempts = _run_strategy(strategy, pads, 2)
    assert attempts.cut is True
    assert attempts.used == 2
