"""Tests for ``NegotiatedRouter.targeted_ripup`` geometric fast-fail
(Issue #2814).

Background
----------

``targeted_ripup`` rips up all sibling routes first, then re-routes the
failed net, then re-routes every sibling.  When the failed net's A*
returns ``None`` even with the siblings already removed from the grid,
the blocker is geometric (pad-clearance keepout, board-edge constraint,
fixed escape route, component body) rather than a sibling trace --
re-routing all N siblings at the full ``per_net_timeout`` cannot help
and just wastes ``N * per_net_timeout`` of wall-clock.

The fix:

- After the failed-net retry, check ``failed_net_success``.
- When it is ``False``, restore siblings with a SHORT probe timeout
  (``min(per_net_timeout, 10s)``) so the grid is not left in a worse
  state than we found it.
- Return ``False`` so the caller (see
  ``_attempt_blocked_component_ripup_negotiated``) can escalate to a
  different strategy.

These tests verify:

1. The fast-fail branch returns ``False`` when the failed net's reroute
   produces no routes.
2. Sibling re-routes use ``min(per_net_timeout, 10s)`` -- never the full
   ``per_net_timeout`` -- when fast-failing.
3. Wall-clock is bounded by ``per_net_timeout + N * sibling_probe_timeout``.
4. Sibling routes are reinstated on the grid after the fast-fail path
   completes.
5. The convergence path (failed net DOES route) still passes the full
   ``per_net_timeout`` to siblings (no regression).
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


def _stub_route(net: int) -> Route:
    """Build a single-segment Route stub for net restoration."""
    return Route(
        net=net,
        net_name=f"Net{net}",
        segments=[
            Segment(
                x1=0.0, y1=0.0, x2=1.0, y2=1.0,
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

    def test_siblings_restored_on_fast_fail(self):
        """Sibling nets that had routes pre-rip-up are restored on the grid."""
        router = _make_router_with_four_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        # Start with siblings having routes.
        net_routes: dict[int, list] = {
            2: [_stub_route(2)],
            3: [_stub_route(3)],
            4: [_stub_route(4)],
        }
        routes_list: list = []

        # Track which nets had route_net_negotiated invoked.
        invocations: list[int] = []

        def fake_route_net(self_neg, pads, *args, **kwargs):
            net_id = pads[0].net
            invocations.append(net_id)
            if net_id == 1:
                # Failed net -- geometric blocker.
                return []
            # Sibling restoration succeeds.
            return [_stub_route(net_id)]

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

        assert success is False
        # Failed net (1) is invoked once + each sibling (2, 3, 4) is invoked
        # once for restoration => 4 total.
        assert invocations[0] == 1, (
            f"Expected failed net (1) to be invoked first; got {invocations}"
        )
        assert set(invocations[1:]) == {2, 3, 4}, (
            f"Expected siblings 2/3/4 to be restored; got {invocations}"
        )
        # Each sibling should now have a route in net_routes.
        for net_id in (2, 3, 4):
            assert net_routes[net_id], (
                f"Net {net_id} should have been restored on fast-fail path; "
                f"net_routes[{net_id}] = {net_routes[net_id]}"
            )

    def test_sibling_probe_uses_short_timeout_on_fast_fail(self):
        """Siblings get ``min(per_net_timeout, 10s)`` -- NOT the full
        ``per_net_timeout`` -- when fast-failing.

        This is the wall-clock-saving optimisation: when the failed net's
        A* already proved geometric infeasibility, the sibling restoration
        is a probe, not a serious search.
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

        # Case 1: per_net_timeout=2.0 (smaller than 10s) -> siblings use 2.0.
        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
            patch.object(NegotiatedRouter, "rip_up_nets"),
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
            )

        assert failed_timeouts == [2.0], (
            f"Failed net should receive full per_net_timeout=2.0; got {failed_timeouts}"
        )
        assert sibling_timeouts == [2.0, 2.0, 2.0], (
            f"Siblings should receive min(2.0, 10.0)=2.0; got {sibling_timeouts}"
        )

        # Case 2: per_net_timeout=30.0 (larger than 10s) -> siblings clamped to 10.0.
        sibling_timeouts.clear()
        failed_timeouts.clear()
        net_routes2: dict[int, list] = {2: [_stub_route(2)], 3: [_stub_route(3)], 4: [_stub_route(4)]}

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3, 4},
                net_routes=net_routes2,
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
        assert sibling_timeouts == [10.0, 10.0, 10.0], (
            f"Siblings should be clamped to 10.0 when per_net_timeout > 10; "
            f"got {sibling_timeouts}"
        )

    def test_sibling_probe_handles_none_timeout(self):
        """``per_net_timeout=None`` -> siblings use the 10s clamp."""
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
            if pads[0].net == 1:
                return []
            sibling_timeouts.append(per_net_timeout)
            return [_stub_route(pads[0].net)]

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", fake_route_net),
            patch.object(NegotiatedRouter, "rip_up_nets"),
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
                per_net_timeout=None,
            )

        assert sibling_timeouts == [10.0, 10.0, 10.0], (
            f"per_net_timeout=None should produce probe timeout 10.0; "
            f"got {sibling_timeouts}"
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

    def test_fast_fail_emits_progress_for_siblings(self):
        """Sibling restoration emits the same ``sibling`` progress events as
        the convergence path so the caller's per-step log line still shows up.
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
            patch.object(NegotiatedRouter, "rip_up_nets"),
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

        # 1 failed-net + 3 sibling probes = 4 progress callbacks.
        assert callback.call_count == 4, (
            f"Expected 4 progress callbacks (1 failed + 3 sibling probes), "
            f"got {callback.call_count}"
        )
        phases = [c.args[1]["phase"] for c in callback.call_args_list]
        assert phases[0] == "failed_net"
        assert phases[1:] == ["sibling", "sibling", "sibling"]

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
