"""Tests for ``NegotiatedRouter.targeted_ripup`` geometric fast-fail
(Issue #2814) and transactional rollback (Issue #3470).

Background
----------

``targeted_ripup`` rips up all sibling routes first, then re-routes the
failed net, then re-routes every sibling.  When the failed net's A*
returns ``None`` even with the siblings already removed from the grid,
the blocker is geometric (pad-clearance keepout, board-edge constraint,
fixed escape route, component body) rather than a sibling trace --
re-routing all N siblings at the full ``per_net_timeout`` cannot help
and just wastes ``N * per_net_timeout`` of wall-clock.

Issue #3470 promoted the whole method to a TRANSACTION: the pre-rip-up
routes of the failed net and every sibling are snapshotted, and on any
non-converging outcome (failed net cannot fully route, OR a displaced
sibling fails / degrades on reroute) the EXACT original Route objects
are restored -- the old "restore siblings via fresh A* at a 10s probe
timeout" repair routinely failed and stranded previously-routed siblings
(board-05 HALL/GATE collateral), and a partial failed-net reroute used
to leave stranded stub copper (board-05 ISENSE_A-/ISENSE_B- overlap).

These tests verify:

1. The fast-fail branch returns ``False`` when the failed net's reroute
   produces no routes.
2. On fast-fail, sibling routes are restored VERBATIM (same objects,
   zero sibling A* invocations).
3. Wall-clock on fast-fail is bounded by the failed net's
   ``per_net_timeout`` (no sibling searches at all).
4. The convergence path (failed net DOES route fully and all siblings
   re-land) still passes the full ``per_net_timeout`` to siblings and
   commits.
5. A PARTIAL failed-net reroute (some RSMT edges failed) is rolled back
   -- no stranded stub copper is committed.
6. A sibling reroute failure rolls the whole transaction back, restoring
   the failed net's pre-rip-up state too.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def _make_router_with_four_nets() -> Autorouter:
    """Construct a 4-net autorouter for fast-fail rip-up tests.

    Net layout (mirrors the chorus-test PHASE_B/PHASE_C topology where a
    pad-clearance keepout blocks the only direct path between two pads
    while sibling traces sit elsewhere):

    - Net 1 (A): the failed net.  Two pads with a clearance keepout in
      between (modelled by a component placed mid-path in real boards;
      here the keepout is a no-op because we mock ``route_net_negotiated``).
    - Nets 2-4 (B, C, D): sibling nets that we pre-route, occupying the
      grid space around the failed net's keepout.
    """
    router = Autorouter(width=80.0, height=40.0)
    # Failed net (A).
    pads_a = [
        {
            "number": "1", "x": 5.0, "y": 20.0,
            "width": 0.5, "height": 0.5, "net": 1, "net_name": "A",
        },
        {
            "number": "2", "x": 60.0, "y": 20.0,
            "width": 0.5, "height": 0.5, "net": 1, "net_name": "A",
        },
    ]
    router.add_component("U_A", pads_a)
    # Sibling nets B, C, D.
    for idx, net_id, name in [(0, 2, "B"), (1, 3, "C"), (2, 4, "D")]:
        pads = [
            {
                "number": "1", "x": 15.0 + idx * 12.0, "y": 12.0,
                "width": 0.5, "height": 0.5, "net": net_id, "net_name": name,
            },
            {
                "number": "2", "x": 15.0 + idx * 12.0, "y": 28.0,
                "width": 0.5, "height": 0.5, "net": net_id, "net_name": name,
            },
        ]
        router.add_component(f"U_{name}", pads)
    return router


def _build_pads_by_net(router: Autorouter) -> dict[int, list]:
    """Group router pads by net id (mirrors the runtime structure)."""
    pads_by_net: dict[int, list] = {}
    for net_id, pad_ids in router.nets.items():
        if net_id == 0:
            continue
        pads_by_net[net_id] = [router.pads[p] for p in pad_ids]
    return pads_by_net


def _stub_route(net: int, offset: float = 0.0) -> Route:
    """Build a single-segment Route stub for net restoration.

    ``offset`` shifts the stub geometry so two stubs for the same net can
    be distinguished by value -- ``Route`` is a dataclass with content
    equality, and the Issue #3470 rollback tests need to tell "original"
    and "new" copper apart in ``routes_list`` membership checks.
    """
    return Route(
        net=net,
        net_name=f"Net{net}",
        segments=[
            Segment(
                x1=0.0 + offset, y1=0.0, x2=1.0 + offset, y2=1.0,
                width=0.2, layer=Layer.F_CU, net=net,
            ),
        ],
    )


class TestTargetedRipupGeometricFastFail:
    """Issue #2814: fast-fail when the failed net's A* still cannot find
    a path with siblings already removed from the grid.
    """

    def test_fast_fail_returns_false_when_failed_net_unreachable(self):
        """``targeted_ripup`` returns ``False`` (not ``True``) when the failed
        net cannot be routed even with siblings cleared.
        """
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [_stub_route(2)], 3: [_stub_route(3)], 4: [_stub_route(4)]}
        routes_list: list = list(net_routes[2]) + list(net_routes[3]) + list(net_routes[4])

        def fake_route_net(self_neg, pads, *args, **kwargs):
            # Failed net (net 1, pads 0/1) -> no path (geometric blocker).
            if pads[0].net == 1:
                return []
            # Siblings restore successfully.
            return [_stub_route(pads[0].net)]

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
            )

        assert success is False, (
            "Expected fast-fail return value of False when failed net is unreachable"
        )

    def test_siblings_restored_verbatim_on_fast_fail(self):
        """Issue #3470: sibling routes are restored as the EXACT original
        Route objects, with zero sibling A* invocations on the fast-fail
        path (the old probe-reroute restoration could fail and strand a
        previously-routed sibling)."""
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        # Start with siblings having routes; remember the exact objects.
        original = {2: _stub_route(2), 3: _stub_route(3), 4: _stub_route(4)}
        net_routes: dict[int, list] = {n: [r] for n, r in original.items()}
        routes_list: list = list(original.values())

        # Track which nets had route_net_negotiated invoked.
        invocations: list[int] = []

        def fake_route_net(self_neg, pads, *args, **kwargs):
            net_id = pads[0].net
            invocations.append(net_id)
            if net_id == 1:
                # Failed net -- geometric blocker.
                return []
            # Sibling restoration via A* must NOT happen any more.
            return [_stub_route(net_id)]

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
        ):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
            )

        assert success is False
        # Only the failed net (1) is invoked -- siblings are restored from
        # the snapshot, not rerouted.
        assert invocations == [1], (
            f"Expected only the failed net's A* to run on fast-fail; "
            f"got invocations for {invocations}"
        )
        # Each sibling should have its ORIGINAL route object back.
        for net_id in (2, 3, 4):
            assert net_routes[net_id] == [original[net_id]], (
                f"Net {net_id} should have been restored verbatim; "
                f"net_routes[{net_id}] = {net_routes[net_id]}"
            )
            assert original[net_id] in routes_list, (
                f"Net {net_id}'s original route should be back in routes_list"
            )

    def test_no_sibling_searches_on_fast_fail(self):
        """Issue #3470: on fast-fail the siblings are NOT rerouted at any
        timeout -- the snapshot restore replaces the old 10s probe entirely.

        The failed net still receives the full ``per_net_timeout``.
        """
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [_stub_route(2)], 3: [_stub_route(3)], 4: [_stub_route(4)]}

        sibling_timeouts: list[float | None] = []
        failed_timeouts: list[float | None] = []

        def fake_route_net(self_neg, pads, *args, per_net_timeout=None, **kwargs):
            net_id = pads[0].net
            if net_id == 1:
                failed_timeouts.append(per_net_timeout)
                return []
            sibling_timeouts.append(per_net_timeout)
            return [_stub_route(net_id)]

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
        ):
            neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=30.0,
            )

        assert failed_timeouts == [30.0], (
            f"Failed net should receive full per_net_timeout=30.0; got {failed_timeouts}"
        )
        assert sibling_timeouts == [], (
            f"Issue #3470: siblings must not be rerouted on fast-fail "
            f"(snapshot restore instead); got searches at {sibling_timeouts}"
        )

    def test_wall_clock_bounded_on_fast_fail(self):
        """Fast-fail wall-clock <= per_net_timeout + N * min(per_net_timeout, 10s).

        Simulates a realistic failure where the failed net A* takes the full
        ``per_net_timeout`` to give up, and each sibling probe takes the full
        probe timeout.  Asserts that the total wall-clock is bounded.
        """
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [_stub_route(2)], 3: [_stub_route(3)], 4: [_stub_route(4)]}

        per_net_timeout = 1.0  # Keep it small so tests run quickly.

        def fake_route_net(self_neg, pads, *args, per_net_timeout=None, **kwargs):
            # Sleep proportional to the budget the caller granted so the
            # wall-clock check is meaningful.  Failed net consumes full budget;
            # siblings consume the probe budget.
            sleep_for = min(per_net_timeout or 1.0, 1.0) * 0.2  # 20% of budget
            time.sleep(sleep_for)
            if pads[0].net == 1:
                return []
            return [_stub_route(pads[0].net)]

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            start = time.time()
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=per_net_timeout,
            )
            elapsed = time.time() - start

        assert success is False
        # Bound: per_net_timeout + 3 * min(per_net_timeout, 10s) = 1 + 3*1 = 4s.
        # Our fake only consumes 20% of each budget so the actual elapsed will
        # be much less, but the upper bound is what matters.  Allow a generous
        # buffer (8s) for the curator-specified test bound.
        assert elapsed <= 8.0, (
            f"Fast-fail wall-clock exceeded curator bound (8s); got {elapsed:.2f}s"
        )

    def test_convergence_path_still_uses_full_timeout(self):
        """When the failed net IS rescued, siblings still get the FULL
        ``per_net_timeout`` (no regression to the success path).
        """
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [_stub_route(2)], 3: [_stub_route(3)], 4: [_stub_route(4)]}

        sibling_timeouts: list[float | None] = []

        def fake_route_net(self_neg, pads, *args, per_net_timeout=None, **kwargs):
            net_id = pads[0].net
            if net_id == 1:
                # Failed net succeeds this time.
                return [_stub_route(net_id)]
            sibling_timeouts.append(per_net_timeout)
            return [_stub_route(net_id)]

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=30.0,
            )

        assert success is True, "Convergence path should report success"
        # Siblings should each get the FULL 30s budget on the success path
        # (no clamping to 10s).
        assert sibling_timeouts == [30.0, 30.0, 30.0], (
            f"On convergence path siblings must receive full per_net_timeout=30.0; "
            f"got {sibling_timeouts}"
        )

    def test_fast_fail_emits_only_failed_net_progress(self):
        """Issue #3470: the fast-fail path restores siblings from the
        snapshot (no A*), so only the failed-net progress event fires.
        The convergence path still emits per-sibling events (covered by
        the convergence test above).
        """
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [_stub_route(2)], 3: [_stub_route(3)], 4: [_stub_route(4)]}

        def fake_route_net(self_neg, pads, *args, **kwargs):
            if pads[0].net == 1:
                return []
            return [_stub_route(pads[0].net)]

        callback = MagicMock()
        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
        ):
            neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
                progress_callback=callback,
                net_names={1: "A", 2: "B", 3: "C", 4: "D"},
            )

        # Only the failed-net event -- siblings are restored, not rerouted.
        assert callback.call_count == 1, (
            f"Expected 1 progress callback (failed net only) on fast-fail, "
            f"got {callback.call_count}"
        )
        phases = [c.args[1]["phase"] for c in callback.call_args_list]
        assert phases == ["failed_net"]

    def test_no_fast_fail_when_no_siblings_eligible(self):
        """When ``nets_to_ripup`` is empty (all blocking nets hit the
        per-net rip-up cap), the existing early return at the top of
        ``targeted_ripup`` still fires -- we do NOT reach the new fast-fail
        branch.
        """
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [], 3: [], 4: []}

        call_count = [0]

        def fake_route_net(self_neg, pads, *args, **kwargs):
            call_count[0] += 1
            return []

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                # Every sibling already at its cap.
                ripup_history={2: 3, 3: 3, 4: 3},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
            )

        assert success is False
        # Early-return path should not invoke route_net_negotiated at all.
        assert call_count[0] == 0, (
            f"When no siblings are eligible, route_net_negotiated should not "
            f"be invoked; got {call_count[0]} calls"
        )


class TestTargetedRipupTransactionalRollback:
    """Issue #3470: ``targeted_ripup`` is a transaction.

    A rip-up that does not converge (failed net partial / sibling failed /
    sibling degraded) must restore the EXACT pre-rip-up routing state --
    never leave partial stub copper for the failed net (board-05
    ISENSE_A-/ISENSE_B- overlap-stub failure mode) and never strand a
    previously-routed sibling (board-05 HALL/GATE collateral-damage
    failure mode).
    """

    def _make_neg_router(self, router: Autorouter) -> NegotiatedRouter:
        return NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )

    def test_partial_failed_net_reroute_is_rolled_back(self):
        """A failed-net reroute that records edge failures (partial route)
        must NOT be committed: a partial route is exactly the stranded-stub
        copper this transaction exists to prevent."""
        router = _make_router_with_four_nets()
        neg_router = self._make_neg_router(router)
        pads_by_net = _build_pads_by_net(router)

        # Failed net 1 enters with a pre-existing partial stub; siblings
        # 2/3 are routed.
        stale_partial = _stub_route(1)
        original = {2: _stub_route(2), 3: _stub_route(3)}
        net_routes: dict[int, list] = {
            1: [stale_partial],
            2: [original[2]],
            3: [original[3]],
        }
        routes_list: list = [stale_partial, original[2], original[3]]

        new_partial = _stub_route(1, offset=10.0)
        invocations: list[int] = []

        def fake_route_net(
            self_neg, pads, *args, failure_callback=None, **kwargs
        ):
            net_id = pads[0].net
            invocations.append(net_id)
            if net_id == 1:
                # PARTIAL reroute: one route placed, one edge failed.
                if failure_callback is not None:
                    failure_callback(pads[0], pads[1])
                return [new_partial]
            return [_stub_route(net_id)]

        with patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3},
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
            )

        assert success is False, "Partial failed-net reroute must not converge"
        # Only the failed net's A* ran (partial => fast-fail, no sibling A*).
        assert invocations == [1]
        # The failed net's PRE-rip-up partial stub is restored verbatim;
        # the new partial copper is gone.
        assert net_routes[1] == [stale_partial], (
            f"Failed net's original state must be restored; got {net_routes[1]}"
        )
        assert new_partial not in routes_list, (
            "The non-converged partial reroute must not leave copper behind"
        )
        # Siblings restored verbatim.
        for net_id in (2, 3):
            assert net_routes[net_id] == [original[net_id]]
            assert original[net_id] in routes_list

    def test_sibling_failure_rolls_back_whole_transaction(self):
        """When a displaced sibling fails to re-route, the rescue commit is
        rolled back: the failed net's new routes are removed and every
        sibling gets its original routes back (no HALL/GATE collateral)."""
        router = _make_router_with_four_nets()
        neg_router = self._make_neg_router(router)
        pads_by_net = _build_pads_by_net(router)

        original = {2: _stub_route(2), 3: _stub_route(3), 4: _stub_route(4)}
        net_routes: dict[int, list] = {n: [r] for n, r in original.items()}
        routes_list: list = list(original.values())

        rescued_route = _stub_route(1, offset=10.0)

        def fake_route_net(self_neg, pads, *args, **kwargs):
            net_id = pads[0].net
            if net_id == 1:
                return [rescued_route]  # full success for the failed net
            if net_id == 3:
                return []  # sibling 3 cannot re-land
            return [_stub_route(net_id)]

        with patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
            )

        assert success is False, "Sibling failure must abort the transaction"
        # The failed net's rescue is rolled back -- pre-rip-up it had no
        # routes, so it must have none now.
        assert net_routes.get(1, []) == [], (
            f"Failed net's rescue must be rolled back; got {net_routes.get(1)}"
        )
        assert rescued_route not in routes_list
        # Every sibling has its exact original route back -- including
        # net 4, whose reroute never ran (transaction aborted at net 3).
        for net_id in (2, 3, 4):
            assert net_routes[net_id] == [original[net_id]], (
                f"Sibling {net_id} must be restored verbatim; "
                f"got {net_routes[net_id]}"
            )
            assert original[net_id] in routes_list

    def test_sibling_degradation_rolls_back(self):
        """A sibling that re-routes with FEWER routes than it had before
        the rip-up counts as degradation and aborts the transaction."""
        router = _make_router_with_four_nets()
        neg_router = self._make_neg_router(router)
        pads_by_net = _build_pads_by_net(router)

        # Sibling 2 starts with TWO routes (multi-edge net).
        sib2_a, sib2_b = _stub_route(2), _stub_route(2, offset=5.0)
        original3 = _stub_route(3)
        net_routes: dict[int, list] = {2: [sib2_a, sib2_b], 3: [original3]}
        routes_list: list = [sib2_a, sib2_b, original3]

        def fake_route_net(self_neg, pads, *args, **kwargs):
            net_id = pads[0].net
            if net_id == 1:
                return [_stub_route(1, offset=10.0)]
            if net_id == 2:
                return [_stub_route(2, offset=10.0)]  # 1 route < 2 originals: degraded
            return [_stub_route(net_id)]

        with patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3},
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
            )

        assert success is False, "Sibling degradation must abort the transaction"
        assert net_routes[2] == [sib2_a, sib2_b], (
            f"Degraded sibling must get BOTH original routes back; "
            f"got {len(net_routes[2])}"
        )
        assert net_routes[3] == [original3]
        assert net_routes.get(1, []) == []

    def test_successful_transaction_commits_and_clears_stale_partial(self):
        """On full convergence the transaction commits: the failed net's
        stale pre-rip-up partial copper is replaced by the new full route
        and the siblings carry their re-landed routes."""
        router = _make_router_with_four_nets()
        neg_router = self._make_neg_router(router)
        pads_by_net = _build_pads_by_net(router)

        stale_partial = _stub_route(1)
        original = {2: _stub_route(2), 3: _stub_route(3)}
        net_routes: dict[int, list] = {
            1: [stale_partial],
            2: [original[2]],
            3: [original[3]],
        }
        routes_list: list = [stale_partial, original[2], original[3]]

        rescued_route = _stub_route(1, offset=10.0)
        relanded = {2: _stub_route(2, offset=10.0), 3: _stub_route(3, offset=10.0)}

        def fake_route_net(self_neg, pads, *args, **kwargs):
            net_id = pads[0].net
            if net_id == 1:
                return [rescued_route]
            return [relanded[net_id]]

        with patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net):
            success = neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3},
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                per_net_timeout=2.0,
            )

        assert success is True, "Full convergence must commit"
        assert net_routes[1] == [rescued_route]
        assert rescued_route in routes_list
        # The stale partial stub was ripped inside the transaction and must
        # NOT come back on the commit path.
        assert stale_partial not in routes_list, (
            "Stale pre-rip-up partial copper must be cleared on commit"
        )
        for net_id in (2, 3):
            assert net_routes[net_id] == [relanded[net_id]]
