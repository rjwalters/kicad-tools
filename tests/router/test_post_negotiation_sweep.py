"""Tests for the post-negotiation rescue sweep (Issue #4159).

Measured failure (rjwalters/softstart PR #28, sparse 160x100 4-layer,
103 parts / 76 nets, C++ backend): ``kct route --strategy negotiated``
plateaus at ~24-30/76 complete, dominated by cross-board power/long-haul
nets.  Each such net then routes SOLO in <1s on the identical copper
(``kct route-auto --net /FUSED_LINE`` -> instant success), so the geometry
is trivial -- the batch negotiation exhausts the long net's per-net search
budget under negotiation pressure and never commits it.

The fix (suggested direction 1, curator-confirmed MVP): after the
negotiated batch loop converges/stalls/times out and the demote safety
nets have run, re-attempt every still-stranded net SOLO on the LIVE grid
via ``Autorouter.route_net`` (every committed route is already an obstacle,
so the solo attempt settles into free space the batch loop never used).
The pass is bounded (one attempt per net, per-net cap, overall wall-clock
ceiling) and strictly additive: a failed attempt rolls back verbatim, so
it can only ever raise the routed count.

The softstart repro fixture is local-only (not in CI), so these tests use
synthetic boards.  The starvation is reproduced by patching the batch's
internal per-net path (``_route_net_negotiated``) to hard-fail a specific
net while leaving the sweep's ``route_net`` path real -- the exact split
the curator identified (two structurally different single-net code paths).
"""

from __future__ import annotations

import time

from kicad_tools.router.algorithms import (
    POST_NEGOTIATION_SWEEP_BUDGET_S,
    POST_NEGOTIATION_SWEEP_PER_NET_S,
)
from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def _build_two_net_router() -> Autorouter:
    """A long-haul 2-pad net (net 1, spans the board) plus an easy net 2.

    Both route trivially on the open 40x20 grid; the long net is the one
    the real softstart repro starves on under negotiation pressure.
    """
    router = Autorouter(width=40.0, height=20.0)
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "LONGHAUL"},
            {"number": "2", "x": 38.0, "y": 10.0, "net": 1, "net_name": "LONGHAUL"},
        ],
    )
    router.add_component(
        "R2",
        [
            {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
        ],
    )
    return router


def _snapshot_net_state(ar: Autorouter) -> tuple[dict[int, list[Route]], dict]:
    """Rebuild the ``net_routes`` / ``pads_by_net`` maps the loop hands the
    sweep, from the router's live state after ``route_all_negotiated``."""
    net_routes: dict[int, list[Route]] = {}
    for r in ar.routes:
        net_routes.setdefault(r.net, []).append(r)
    pads_by_net = {net: [ar.pads[k] for k in keys] for net, keys in ar.nets.items()}
    return net_routes, pads_by_net


# ---------------------------------------------------------------------------
# Bounds constants
# ---------------------------------------------------------------------------


class TestSweepConstants:
    def test_budget_is_bounded(self):
        assert 0 < POST_NEGOTIATION_SWEEP_BUDGET_S <= 120.0

    def test_per_net_cap_is_small(self):
        """A solo long-haul is sub-second per the issue's evidence; the
        per-net cap must stay small so one genuinely-impossible net cannot
        eat the whole sweep."""
        assert 0 < POST_NEGOTIATION_SWEEP_PER_NET_S <= 30.0
        assert POST_NEGOTIATION_SWEEP_PER_NET_S <= POST_NEGOTIATION_SWEEP_BUDGET_S


# ---------------------------------------------------------------------------
# Direct sweep-mechanism tests
# ---------------------------------------------------------------------------


class TestSweepMechanism:
    def test_rescues_a_stranded_routable_net(self):
        """Given a net that is stranded but routable solo on the live grid,
        the sweep commits it and clears its failure records."""
        ar = _build_two_net_router()
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        net_routes, pads_by_net = _snapshot_net_state(ar)

        # Strand net 1: rip it up so it is absent from the committed copper,
        # exactly as a budget-starved batch loop would leave it.
        neg = NegotiatedRouter(
            ar.grid,
            ar.router,
            ar.rules,
            ar.net_class_map,
            congestion_estimator=ar._ensure_congestion_estimator(),
        )
        neg.rip_up_nets([1], net_routes, ar.routes)
        assert net_routes[1] == []

        rescued = ar._post_negotiation_sweep(
            stranded_nets=[1],
            net_routes=net_routes,
            pads_by_net=pads_by_net,
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() + POST_NEGOTIATION_SWEEP_BUDGET_S,
        )
        assert rescued == [1]
        assert net_routes[1], "rescued net must have committed routes"
        assert not any(f.net == 1 for f in ar.routing_failures)

    def test_does_not_touch_already_routed_nets(self):
        """The sweep is additive-only: nets not in the stranded set keep
        their exact routes."""
        ar = _build_two_net_router()
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        net_routes, pads_by_net = _snapshot_net_state(ar)

        neg = NegotiatedRouter(
            ar.grid,
            ar.router,
            ar.rules,
            ar.net_class_map,
            congestion_estimator=ar._ensure_congestion_estimator(),
        )
        neg.rip_up_nets([1], net_routes, ar.routes)
        net2_before = list(net_routes[2])

        ar._post_negotiation_sweep(
            stranded_nets=[1],
            net_routes=net_routes,
            pads_by_net=pads_by_net,
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() + POST_NEGOTIATION_SWEEP_BUDGET_S,
        )
        assert net_routes[2] == net2_before, "untouched net must be byte-identical"

    def test_failed_attempt_rolls_back_without_regression(self, monkeypatch):
        """When a solo attempt cannot connect the net, the sweep rolls back
        verbatim: no partial copper committed, no orphan failure records,
        and other nets are untouched."""
        ar = _build_two_net_router()
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        net_routes, pads_by_net = _snapshot_net_state(ar)

        neg = NegotiatedRouter(
            ar.grid,
            ar.router,
            ar.rules,
            ar.net_class_map,
            congestion_estimator=ar._ensure_congestion_estimator(),
        )
        neg.rip_up_nets([1], net_routes, ar.routes)
        net2_before = list(net_routes[2])
        routes_len_before = len(ar.routes)
        failures_before = len(ar.routing_failures)

        # Simulate a route_net that marks a DISCONNECTED fragment (a stub
        # that does not connect the net's two pads) and records a failure --
        # the batch-starvation partial-route case.  The sweep must detect the
        # net is not fully connected and unwind everything.
        def fake_route_net(net, per_net_timeout=None):
            frag = Route(net=net, net_name="LONGHAUL", segments=[], vias=[])
            ar._mark_route(frag)
            ar.routes.append(frag)
            net_routes.setdefault(net, []).append(frag)
            ar.routing_failures.append(_make_failure(net))
            return [frag]

        monkeypatch.setattr(ar, "route_net", fake_route_net)

        rescued = ar._post_negotiation_sweep(
            stranded_nets=[1],
            net_routes=net_routes,
            pads_by_net=pads_by_net,
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() + POST_NEGOTIATION_SWEEP_BUDGET_S,
        )
        assert rescued == []
        assert net_routes[1] == [], "failed attempt must leave net stranded"
        assert net_routes[2] == net2_before, "other nets must be untouched"
        assert len(ar.routes) == routes_len_before, "no orphan copper committed"
        assert len(ar.routing_failures) == failures_before, "no orphan failure records"

    def test_drc_dirty_rescue_rolls_back(self, monkeypatch):
        """Issue #4159 (Judge #4192): a rescue that CONNECTS the net but
        commits copper physically overlapping a FOREIGN net's committed
        trace is DRC-dirty (a net-new blocking clearance violation) and must
        be rolled back verbatim -- the sweep is additive for the DRC-error
        count, not only the net-completion count.

        Repro: net 2 routes as a vertical trace at x=10 on F_CU.  Strand net
        1, then patch its solo ``route_net`` to return a horizontal trace at
        y=10 that CONNECTS net 1's two pads (2,10)->(38,10) but crosses net
        2's copper at (10,10) with zero edge-to-edge clearance -- a physical
        overlap the ``copper_overlap_only`` seg-seg gate flags as blocking.
        The sweep must detect the net-new violation and leave net 1 stranded.
        """
        ar = _build_two_net_router()
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        net_routes, pads_by_net = _snapshot_net_state(ar)

        neg = NegotiatedRouter(
            ar.grid,
            ar.router,
            ar.rules,
            ar.net_class_map,
            congestion_estimator=ar._ensure_congestion_estimator(),
        )
        neg.rip_up_nets([1], net_routes, ar.routes)
        assert net_routes[1] == []
        net2_before = list(net_routes[2])
        routes_len_before = len(ar.routes)

        # A connected-BUT-overlapping solo route for net 1: one horizontal
        # segment joining both net-1 pads that lies on top of net 2's
        # vertical trace at the (10,10) crossing (same layer, physical
        # overlap => net-new blocking seg-seg violation).
        def dirty_route_net(net, per_net_timeout=None):
            seg = Segment(
                x1=2.0,
                y1=10.0,
                x2=38.0,
                y2=10.0,
                width=0.2,
                layer=Layer.F_CU,
                net=net,
                net_name="LONGHAUL",
            )
            route = Route(net=net, net_name="LONGHAUL", segments=[seg], vias=[])
            ar._mark_route(route)
            ar.routes.append(route)
            net_routes.setdefault(net, []).append(route)
            return [route]

        monkeypatch.setattr(ar, "route_net", dirty_route_net)

        rescued = ar._post_negotiation_sweep(
            stranded_nets=[1],
            net_routes=net_routes,
            pads_by_net=pads_by_net,
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() + POST_NEGOTIATION_SWEEP_BUDGET_S,
        )
        assert rescued == [], "DRC-dirty rescue must NOT be committed"
        assert net_routes[1] == [], "DRC-dirty rescue must leave the net stranded"
        assert net_routes[2] == net2_before, "foreign net must be untouched"
        assert len(ar.routes) == routes_len_before, "no DRC-dirty copper committed"

    def test_drc_clean_rescue_is_committed(self, monkeypatch):
        """Control for ``test_drc_dirty_rescue_rolls_back``: a rescue that
        connects the net WITHOUT overlapping foreign copper is committed --
        the DRC gate must not reject a clean solo route.

        Net 1's solo route runs along y=19.5 (clear of net 2's x=10 vertical
        trace, which spans y=2..18), then drops to its pads: connected AND
        clean, so the sweep commits it.
        """
        ar = _build_two_net_router()
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        net_routes, pads_by_net = _snapshot_net_state(ar)

        neg = NegotiatedRouter(
            ar.grid,
            ar.router,
            ar.rules,
            ar.net_class_map,
            congestion_estimator=ar._ensure_congestion_estimator(),
        )
        neg.rip_up_nets([1], net_routes, ar.routes)

        def clean_route_net(net, per_net_timeout=None):
            # Detour below net 2's trace: (2,10)->(2,19.5)->(38,19.5)->(38,10).
            segs = [
                Segment(
                    x1=2.0,
                    y1=10.0,
                    x2=2.0,
                    y2=19.5,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=net,
                    net_name="LONGHAUL",
                ),
                Segment(
                    x1=2.0,
                    y1=19.5,
                    x2=38.0,
                    y2=19.5,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=net,
                    net_name="LONGHAUL",
                ),
                Segment(
                    x1=38.0,
                    y1=19.5,
                    x2=38.0,
                    y2=10.0,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=net,
                    net_name="LONGHAUL",
                ),
            ]
            route = Route(net=net, net_name="LONGHAUL", segments=segs, vias=[])
            ar._mark_route(route)
            ar.routes.append(route)
            net_routes.setdefault(net, []).append(route)
            return [route]

        monkeypatch.setattr(ar, "route_net", clean_route_net)

        rescued = ar._post_negotiation_sweep(
            stranded_nets=[1],
            net_routes=net_routes,
            pads_by_net=pads_by_net,
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() + POST_NEGOTIATION_SWEEP_BUDGET_S,
        )
        assert rescued == [1], "a clean, connected rescue must be committed"
        assert net_routes[1], "rescued net must have committed routes"

    def test_diffpair_leg_is_skipped(self, monkeypatch):
        """Issue #4159 (Judge #4192): a stranded DIFFERENTIAL-PAIR LEG must
        NOT be solo-rescued.  ``route_net`` is single-ended -- it ignores the
        pair's coupled length-matching / within-pair spacing -- so rescuing
        one leg produces the board-07 regression (a connected leg that then
        fails ``diffpair_length_skew`` / ``diffpair_routing_continuity``,
        raising the blocking-DRC count).  The sweep must skip the leg and
        never call ``route_net`` for it.
        """
        ar = Autorouter(width=40.0, height=20.0)
        # DAT_P / DAT_N are suffix-detected as a differential pair.
        ar.add_component(
            "U1",
            [
                {"number": "1", "x": 2.0, "y": 9.0, "net": 1, "net_name": "DAT_P"},
                {"number": "2", "x": 38.0, "y": 9.0, "net": 1, "net_name": "DAT_P"},
                {"number": "3", "x": 2.0, "y": 11.0, "net": 2, "net_name": "DAT_N"},
                {"number": "4", "x": 38.0, "y": 11.0, "net": 2, "net_name": "DAT_N"},
            ],
        )
        assert "DAT_P" in ar.get_diff_pair_map(), "test setup: DAT_P must be a diff-pair leg"

        attempts: list[int] = []
        orig = ar.route_net

        def counting_route_net(net, per_net_timeout=None):
            attempts.append(net)
            return orig(net, per_net_timeout=per_net_timeout)

        monkeypatch.setattr(ar, "route_net", counting_route_net)

        # Net 1 (DAT_P) is stranded (no committed copper) and is a diff-pair
        # leg; the sweep must skip it without ever invoking ``route_net``.
        rescued = ar._post_negotiation_sweep(
            stranded_nets=[1],
            net_routes={1: [], 2: []},
            pads_by_net={
                1: [ar.pads[k] for k in ar.nets[1]],
                2: [ar.pads[k] for k in ar.nets[2]],
            },
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() + POST_NEGOTIATION_SWEEP_BUDGET_S,
        )
        assert rescued == [], "a diff-pair leg must not be rescued"
        assert attempts == [], "the sweep must not solo-route a diff-pair leg"

    def test_bounded_by_deadline(self):
        """A deadline already in the past terminates the sweep immediately
        without attempting any net (the hard bound that keeps a genuinely-
        impossible stranded net from hanging the pass)."""
        ar = _build_two_net_router()
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        net_routes, pads_by_net = _snapshot_net_state(ar)

        neg = NegotiatedRouter(
            ar.grid,
            ar.router,
            ar.rules,
            ar.net_class_map,
            congestion_estimator=ar._ensure_congestion_estimator(),
        )
        neg.rip_up_nets([1], net_routes, ar.routes)

        attempts: list[int] = []
        orig = ar.route_net

        def counting_route_net(net, per_net_timeout=None):
            attempts.append(net)
            return orig(net, per_net_timeout=per_net_timeout)

        ar.route_net = counting_route_net
        rescued = ar._post_negotiation_sweep(
            stranded_nets=[1],
            net_routes=net_routes,
            pads_by_net=pads_by_net,
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() - 1.0,  # already expired
        )
        assert rescued == []
        assert attempts == [], "past deadline must skip all net attempts"

    def test_empty_stranded_list_is_noop(self):
        ar = _build_two_net_router()
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        net_routes, pads_by_net = _snapshot_net_state(ar)
        rescued = ar._post_negotiation_sweep(
            stranded_nets=[],
            net_routes=net_routes,
            pads_by_net=pads_by_net,
            per_net_timeout=POST_NEGOTIATION_SWEEP_PER_NET_S,
            deadline=time.time() + POST_NEGOTIATION_SWEEP_BUDGET_S,
        )
        assert rescued == []


def _make_failure(net: int):
    from kicad_tools.router.core import RoutingFailure

    return RoutingFailure(
        net=net,
        net_name="LONGHAUL",
        source_pad=("R1", "1"),
        target_pad=("R1", "2"),
        source_coords=(2.0, 10.0),
        target_coords=(38.0, 10.0),
        blocking_nets=set(),
        blocking_components=[],
        reason="synthetic",
    )


# ---------------------------------------------------------------------------
# End-to-end integration: batch starves a net, sweep rescues it
# ---------------------------------------------------------------------------


def _starve_batch_net(ar: Autorouter, starved_net: int) -> None:
    """Patch the batch's internal per-net path to hard-fail ``starved_net``.

    ``_route_net_negotiated`` is the fine-grained A* the batch loop calls
    (curator's path 1); the rescue sweep uses ``route_net`` (a separate
    MST/RSMT path).  Failing only the former reproduces the reported
    budget-starvation without breaking the sweep's ability to route the
    same net solo.
    """
    orig = ar._route_net_negotiated

    def patched(net, pf, per_net_timeout=None):
        if net == starved_net:
            return []
        return orig(net, pf, per_net_timeout=per_net_timeout)

    ar._route_net_negotiated = patched


class TestEndToEndStarvationRescue:
    def test_starved_long_haul_is_rescued(self):
        """The batch loop strands the long net (its internal path is starved),
        but the post-negotiation sweep recovers it -- final completion is 2/2."""
        ar = _build_two_net_router()
        _starve_batch_net(ar, starved_net=1)
        routes = ar.route_all_negotiated(
            max_iterations=2, timeout=30.0, adaptive=False, perturbation=False
        )
        routed_nets = {r.net for r in routes}
        assert routed_nets == {1, 2}, (
            f"post-negotiation sweep must rescue the starved long-haul; got {sorted(routed_nets)}"
        )

    def test_completion_count_rises_after_sweep(self, capsys):
        ar = _build_two_net_router()
        _starve_batch_net(ar, starved_net=1)
        ar.route_all_negotiated(max_iterations=2, timeout=30.0, adaptive=False, perturbation=False)
        out = capsys.readouterr().out
        # The batch summary reports 1/2 (long net stranded); the sweep line
        # reports the rise to 2/2.
        assert "Post-negotiation rescue recovered" in out
        assert "now 2/2 routed" in out

    def test_disabled_flag_leaves_net_stranded(self):
        """``--no-rescue-pass`` (``_post_negotiation_rescue=False``) gives the
        raw negotiated result: the starved net stays unrouted."""
        ar = _build_two_net_router()
        _starve_batch_net(ar, starved_net=1)
        ar._post_negotiation_rescue = False
        routes = ar.route_all_negotiated(
            max_iterations=2, timeout=30.0, adaptive=False, perturbation=False
        )
        routed_nets = {r.net for r in routes}
        assert routed_nets == {2}, "disabled sweep must not rescue the starved net"

    def test_no_starvation_is_byte_identical(self, capsys):
        """When nothing is stranded the sweep never engages (no rescue line)
        and the board routes exactly as before."""
        ar = _build_two_net_router()
        routes = ar.route_all_negotiated(
            max_iterations=2, timeout=30.0, adaptive=False, perturbation=False
        )
        routed_nets = {r.net for r in routes}
        assert routed_nets == {1, 2}
        out = capsys.readouterr().out
        assert "Post-negotiation rescue" not in out
