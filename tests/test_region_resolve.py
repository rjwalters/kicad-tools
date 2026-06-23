"""Tests for the joint region re-solve (Issue #3864, M2).

``NegotiatedRouter.region_resolve`` replaces the sequential one-at-a-time
reroute of a ripped pocket with a bounded inner negotiated loop over the
pocket nets, guarded by a net-positive rollback so it can never regress a
board.  These tests verify, with mocked dependencies:

1. A pocket of <2 routable nets is a no-op (no swap/rotation possible).
2. The net-positive guard COMMITS only when the pocket's strict
   (fully-connected) net count strictly increases.
3. On no strict gain the exact pre-rip-up routes are restored verbatim
   (regression impossible by construction).
4. ``neighborhood_ripup(joint_resolve=True)`` routes the pocket through
   ``region_resolve`` and falls through to the legacy sequential reroute
   when the joint re-solve rolls back.
"""

from unittest.mock import MagicMock

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter


def _make_neg_router() -> NegotiatedRouter:
    neg = NegotiatedRouter(MagicMock(), MagicMock(), MagicMock(), {})
    return neg


def _route(net_id: int = 0) -> MagicMock:
    """A mock Route with one segment (enough for the rip-up machinery)."""
    seg = MagicMock()
    seg.x1, seg.y1, seg.x2, seg.y2 = 0.0, 0.0, 1.0, 1.0
    r = MagicMock(name=f"route-net{net_id}")
    r.segments = [seg]
    return r


class TestRegionResolveGuards:
    def test_pocket_with_fewer_than_two_routable_nets_is_noop(self):
        neg = _make_neg_router()
        neg.rip_up_nets = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[_route()])

        improved, strict = neg.region_resolve(
            pocket_nets=[10],
            net_routes={10: [_route(10)]},
            routes_list=[],
            pads_by_net={10: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        assert improved is False
        assert strict == 0
        # No work attempted on a degenerate pocket.
        neg.rip_up_nets.assert_not_called()
        neg.route_net_negotiated.assert_not_called()

    def test_commits_on_strict_increase(self):
        """One partial net finishes while the strict net stays strict."""
        neg = _make_neg_router()

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)
        neg.grid.mark_route_usage = MagicMock()

        # Net 100 starts strict (has copper); net 200 starts partial (no
        # copper -- stub stripped before entry).  The inner loop fully
        # routes BOTH: strict goes 1 -> 2.
        net_routes = {100: [_route(100)], 200: []}

        def route_net(pads, factor, cb, per_net_timeout=None, failure_callback=None):
            # Zero failure_callback invocations => fully connected.
            return [_route()]

        neg.route_net_negotiated = MagicMock(side_effect=route_net)

        improved, strict = neg.region_resolve(
            pocket_nets=[100, 200],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={
                100: [MagicMock(), MagicMock()],
                200: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        assert improved is True
        assert strict == 2
        assert net_routes[100]
        assert net_routes[200]

    def test_rolls_back_verbatim_on_no_strict_gain(self):
        """A 1:1 trade (one finishes, one strands) must NOT commit."""
        neg = _make_neg_router()

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                for r in net_routes.get(n, []):
                    if r in routes_list:
                        routes_list.remove(r)
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)
        neg.grid.mark_route_usage = MagicMock()
        neg.grid.unmark_route_usage = MagicMock()
        neg.grid.unmark_route = MagicMock()

        original_100 = _route(100)
        original_200 = _route(200)
        net_routes = {100: [original_100], 200: [original_200]}
        routes_list = [original_100, original_200]

        # Every inner reroute reports an edge failure => never fully
        # connected => strict count can never rise above the baseline (2).
        def route_net(pads, factor, cb, per_net_timeout=None, failure_callback=None):
            if failure_callback is not None:
                failure_callback(MagicMock(), MagicMock())
            return [_route()]

        neg.route_net_negotiated = MagicMock(side_effect=route_net)

        improved, strict = neg.region_resolve(
            pocket_nets=[100, 200],
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net={
                100: [MagicMock(), MagicMock()],
                200: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
        )

        assert improved is False
        # Exact original Route objects restored verbatim.
        assert net_routes[100] == [original_100]
        assert net_routes[200] == [original_200]
        assert original_100 in routes_list
        assert original_200 in routes_list


class TestJointEscapesOneToOneTrade:
    """The decisive synthetic congestion test (Issue #3864).

    A 2-net pocket sharing one scarce track.  Under a SEQUENTIAL reroute
    (route in a fixed order, first-come-first-served) the first net grabs
    the only cheap track and the second strands -> a 1:1 trade, net-zero.
    The JOINT inner loop re-derives the pocket several times at an
    escalating present-cost and with a rotated order; on the escalated
    pass the present-cost penalty pushes the first net onto a detour,
    leaving room for BOTH -> a strict gain the sequential reroute can
    never find.  This is the local minimum #3862 names as the lever.
    """

    def test_joint_escapes_where_sequential_is_net_zero(self):
        neg = _make_neg_router()

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                for r in net_routes.get(n, []):
                    if r in routes_list:
                        routes_list.remove(r)
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)
        neg.grid.mark_route_usage = MagicMock()
        neg.grid.unmark_route_usage = MagicMock()
        neg.grid.unmark_route = MagicMock()

        # Net 1 starts STRICT (already has copper); net 2 is the stuck
        # near-complete partial (no copper).  strict_before = 1.  The only
        # win that counts is connecting BOTH -> strict 2.  A sequential
        # reroute that re-derives "net 1 keeps the track, net 2 strands"
        # stays at strict 1 (a 1:1 trade -- net-zero, rolled back).
        net_routes: dict[int, list] = {1: [_route(1)], 2: []}

        # Model the scarce shared track: at the BASE present-cost only the
        # net routed FIRST in a pass connects fully; the second sees the
        # track occupied and strands (an edge failure).  Because every pass
        # rips the pocket clean and re-routes, base-cost passes always
        # reproduce the 1:1 trade (exactly ONE strict) regardless of order
        # -- the sequential minimum.  Only at an ESCALATED present-cost
        # (pass_index >= 1) is the first net pushed onto a detour so BOTH
        # connect fully -> strict 2.
        base_factor = 0.5
        occupied: dict[int, int] = {}

        def route_net(pads, factor, cb, per_net_timeout=None, failure_callback=None):
            escalated = factor > base_factor + 1e-9
            if escalated:
                return [_route()]  # detour available: full route
            # Base cost: first-routed net in THIS pass wins the track.
            pass_key = round(factor, 6)
            occupied[pass_key] = occupied.get(pass_key, 0) + 1
            if occupied[pass_key] == 1:
                return [_route()]  # fully connected
            if failure_callback is not None:
                failure_callback(MagicMock(), MagicMock())  # strands
            return [_route()]  # partial copper, NOT fully connected

        neg.route_net_negotiated = MagicMock(side_effect=route_net)

        improved, strict = neg.region_resolve(
            pocket_nets=[1, 2],
            net_routes=net_routes,
            routes_list=[net_routes[1][0]],
            pads_by_net={1: [MagicMock(), MagicMock()], 2: [MagicMock(), MagicMock()]},
            present_cost_factor=base_factor,
            mark_route_callback=lambda r: None,
            inner_passes=4,
            present_cost_escalation=1.8,
        )

        # Pass 0 (base cost) reproduces the 1:1 trade: strict 1 == before,
        # no commit.  Pass 1 (escalated) connects both -> strict 1 -> 2,
        # committed.  This is the escape the sequential reroute cannot find.
        assert improved is True
        assert strict == 2


class TestRegionResolveBudget:
    def test_oversized_pocket_is_skipped(self):
        neg = _make_neg_router()
        neg.rip_up_nets = MagicMock()
        neg.route_net_negotiated = MagicMock(return_value=[_route()])

        pocket = list(range(1, 12))  # 11 nets > default max_pocket_nets=8
        pads = {n: [MagicMock(), MagicMock()] for n in pocket}

        improved, strict = neg.region_resolve(
            pocket_nets=pocket,
            net_routes={n: [] for n in pocket},
            routes_list=[],
            pads_by_net=pads,
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            max_pocket_nets=8,
        )

        assert improved is False
        # Oversized -> no work attempted at all.
        neg.rip_up_nets.assert_not_called()
        neg.route_net_negotiated.assert_not_called()

    def test_wall_budget_rolls_back_without_strict_gain(self):
        neg = _make_neg_router()

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                for r in net_routes.get(n, []):
                    if r in routes_list:
                        routes_list.remove(r)
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)
        neg.grid.mark_route_usage = MagicMock()
        neg.grid.unmark_route_usage = MagicMock()
        neg.grid.unmark_route = MagicMock()

        orig1, orig2 = _route(1), _route(2)
        net_routes = {1: [orig1], 2: [orig2]}
        routes_list = [orig1, orig2]

        # Every reroute strands (edge failure) AND is slow enough that the
        # tiny wall budget trips immediately -- the guard must roll back to
        # the exact originals.
        def route_net(pads, factor, cb, per_net_timeout=None, failure_callback=None):
            if failure_callback is not None:
                failure_callback(MagicMock(), MagicMock())
            return [_route()]

        neg.route_net_negotiated = MagicMock(side_effect=route_net)

        improved, strict = neg.region_resolve(
            pocket_nets=[1, 2],
            net_routes=net_routes,
            routes_list=routes_list,
            pads_by_net={1: [MagicMock(), MagicMock()], 2: [MagicMock(), MagicMock()]},
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            wall_budget_s=0.0,  # exhausted on entry to the first pass
        )

        assert improved is False
        assert net_routes[1] == [orig1]
        assert net_routes[2] == [orig2]


class TestNeighborhoodJointResolve:
    def test_joint_resolve_routes_through_region_resolve(self):
        neg = _make_neg_router()
        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 1})
        neg.grid.world_to_grid.return_value = (5, 5)

        # Intercept region_resolve to confirm the flag path is taken.
        neg.region_resolve = MagicMock(return_value=(True, 2))

        net_routes = {100: [_route(100)]}
        improved, _count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes=net_routes,
            routes_list=[],
            pads_by_net={
                10: [MagicMock(), MagicMock()],
                100: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            joint_resolve=True,
        )

        neg.region_resolve.assert_called_once()
        assert improved is True

    def test_flag_off_does_not_call_region_resolve(self):
        neg = _make_neg_router()
        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 1})
        neg.grid.world_to_grid.return_value = (5, 5)
        neg.region_resolve = MagicMock(return_value=(True, 2))

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)
        neg.route_net_negotiated = MagicMock(return_value=[])
        neg.grid.mark_route_usage = MagicMock()

        neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes={100: [_route(100)]},
            routes_list=[],
            pads_by_net={
                10: [MagicMock(), MagicMock()],
                100: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            joint_resolve=False,
        )

        neg.region_resolve.assert_not_called()

    def test_joint_resolve_falls_through_to_sequential_on_rollback(self):
        """When region_resolve rolls back, the legacy sequential reroute runs."""
        neg = _make_neg_router()
        neg.find_blocking_nets_relaxed = MagicMock(return_value={100: 1})
        neg.grid.world_to_grid.return_value = (5, 5)
        # region_resolve returns no improvement (rolled back).
        neg.region_resolve = MagicMock(return_value=(False, 1))

        def mock_rip_up(nets, net_routes, routes_list):
            for n in nets:
                net_routes[n] = []

        neg.rip_up_nets = MagicMock(side_effect=mock_rip_up)
        # Sequential reroute then succeeds for the failed net.
        neg.route_net_negotiated = MagicMock(return_value=[_route()])
        neg.grid.mark_route_usage = MagicMock()

        improved, _count = neg.neighborhood_ripup(
            failed_nets=[10],
            net_routes={100: [_route(100)]},
            routes_list=[],
            pads_by_net={
                10: [MagicMock(), MagicMock()],
                100: [MagicMock(), MagicMock()],
            },
            present_cost_factor=0.5,
            mark_route_callback=lambda r: None,
            joint_resolve=True,
        )

        neg.region_resolve.assert_called_once()
        # Sequential path engaged after rollback -> failed net 10 routed.
        assert neg.route_net_negotiated.called
        assert improved is True
