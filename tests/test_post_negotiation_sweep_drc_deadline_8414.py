"""Issue #8414: the sweep's DRC pre-check must not run past the sweep deadline."""

import time

from kicad_tools.router.core import Autorouter, DesignRules


def _router():
    router = Autorouter(width=20.0, height=20.0, rules=DesignRules(grid_resolution=0.1))
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "N1"},
            {"number": "2", "x": 10.0, "y": 5.0, "net": 1, "net_name": "N1"},
        ],
    )
    return router


def _run(router, deadline, calls):
    def stalled_drc(*a, **kw):
        calls.append(1)
        time.sleep(30)
        return False

    router._rescued_net_adds_blocking_drc = stalled_drc
    pads = {1: list(router.pads.values())}
    net_routes = {1: []}
    t0 = time.time()
    rescued = router._post_negotiation_sweep(
        stranded_nets=[1],
        net_routes=net_routes,
        pads_by_net=pads,
        per_net_timeout=10.0,
        deadline=deadline,
    )
    return rescued, net_routes, time.time() - t0


def test_drc_precheck_skipped_and_rescue_rolled_back_when_budget_spent():
    router = _router()
    real_route_net = router.route_net
    connected = []

    def slow_route_net(net, **kw):
        routes = real_route_net(net, **kw)
        connected.append(bool(routes))
        time.sleep(0.6)  # the sweep budget (0.4 s) is spent while routing
        return routes

    router.route_net = slow_route_net
    calls: list[int] = []
    rescued, net_routes, elapsed = _run(router, time.time() + 0.4, calls)
    assert connected == [True], "fixture must actually route the net"
    assert calls == [], "stalled DRC pre-check must be skipped past the deadline"
    assert elapsed < 10
    assert rescued == []
    assert net_routes[1] == []


def test_drc_precheck_still_runs_within_budget():
    router = _router()
    calls: list[int] = []

    def fast_drc(*a, **kw):
        calls.append(1)
        return False

    router._rescued_net_adds_blocking_drc = fast_drc
    pads = {1: list(router.pads.values())}
    rescued = router._post_negotiation_sweep(
        stranded_nets=[1],
        net_routes={1: []},
        pads_by_net=pads,
        per_net_timeout=10.0,
        deadline=time.time() + 600,
    )
    if rescued:
        assert calls == [1]
