"""Fan-via terminal gateways for lattice diff pairs (issue #6444, slice 2a).

A pair segment whose planar coupled search is exhausted on its pads' layer
gets one retry on another layer: each net fans out to ONE through via per
end, and the unchanged pair agent couples the run between the via pairs.

Synthetic boards only (the four-board bench lives in
``scripts/research/diffpair_bench.py``):

* a pair walled off on F.Cu couples on B.Cu through gateways, P and N with
  equal via counts, fans on the pad layer, the coupled body on B.Cu;
* on a 2-layer board with no legal gateway the segment declines with a named
  reason, quickly (no hang);
* a pair that couples planar is byte-identical with the retry enabled;
* the per-terminal candidate cap is honoured, and the enumeration is
  deterministic and handedness-filtered.
"""

from __future__ import annotations

import time

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.gateway import (
    Gateway,
    GatewaySite,
    enumerate_gateways,
    handedness_consistent,
    propose_gateway_sites,
    rank_gateway_combos,
)
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.lattice.pathfinder import GATEWAY_RETRY_REASONS, LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
_HS = NetClassRouting(
    name="HS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.1,
    coupled_routing=True,
)
_PITCH = _HS.trace_width + _HS.effective_intra_pair_clearance()
_OUTLINE = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]


def _pad(x: float, y: float, net: int, name: str, ref: str, **kw: float) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=kw.get("width", 0.5),
        height=kw.get("height", 0.5),
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _board(
    extra: list[Pad] | None = None, rules: DesignRules | None = None
) -> tuple[LatticePathfinder, CoupledConnection]:
    p_a = _pad(3.0, 5.0, 1, "D+", "J1")
    n_a = _pad(3.0, 6.0, 2, "D-", "J1")
    p_b = _pad(27.0, 5.0, 1, "D+", "U1")
    n_b = _pad(27.0, 6.0, 2, "D-", "U1")
    pads = [p_a, n_a, p_b, n_b] + list(extra or [])
    pf = LatticePathfinder(_OUTLINE, pads, rules or _RULES, LayerStack.two_layer())
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=p_a,
        pad_n_a=n_a,
        pad_p_b=p_b,
        pad_n_b=n_b,
        net_class=_HS,
        pitch=_PITCH,
    )
    return pf, pc


def _f_wall() -> Pad:
    """A full-height F.Cu-only wall between the ends: no planar corridor."""
    return _pad(15.0, 6.0, 9, "WALL", "WF", width=0.6, height=13.0)


def _signature(routes: dict) -> list:
    out = []
    for key in sorted(routes, key=repr):
        r = routes[key]
        out.append(
            (
                repr(key),
                [(s.x1, s.y1, s.x2, s.y2, s.width, s.layer) for s in r.segments],
                [(v.x, v.y) for v in r.vias],
            )
        )
    return out


def test_walled_pair_couples_through_fan_via_gateways() -> None:
    pf, pc = _board([_f_wall()])
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_gateway_log
    assert "B.Cu" in pf.pair_gateways[pc.key]
    assert "vias P/N=2/2" in pf.pair_gateways[pc.key]
    assert stats.routed == stats.total == 1
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    # Equal via counts: one fan via per net per end.
    assert len(route_p.vias) == len(route_n.vias) == 2
    for route in (route_p, route_n):
        layers = {s.layer for s in route.segments}
        assert layers == {Layer.F_CU, Layer.B_CU}
        # Every F.Cu segment is a short fan leg (nothing crosses the wall).
        for s in route.segments:
            if s.layer == Layer.F_CU:
                assert max(s.x1, s.x2) < 15.0 - 0.3 or min(s.x1, s.x2) > 15.0 + 0.3
    # Never ships a short: P and N copper keep the full gap on each layer.
    copper_gap = _RULES.trace_width + _RULES.trace_clearance
    for s1 in route_p.segments:
        for s2 in route_n.segments:
            if s1.layer == s2.layer:
                d = seg_seg_dist((s1.x1, s1.y1), (s1.x2, s1.y2), (s2.x1, s2.y1), (s2.x2, s2.y2))
                assert d >= min(copper_gap, _PITCH) - 1e-6


def test_gateway_retry_is_off_by_default() -> None:
    pf, pc = _board([_f_wall()])
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:pair-no-path")
    assert pf.pair_gateways == {}


def test_two_layer_board_without_legal_gateway_declines_quickly() -> None:
    # Vias are forbidden everywhere: B.Cu exists, but no gateway is legal.
    no_vias = LatticeKeepoutMask(
        [
            KeepoutArea(
                polygon=tuple(_OUTLINE),
                layers=frozenset({0, 1}),
                blocks_tracks=False,
                blocks_vias=True,
            )
        ]
    )
    pf, pc = _board([_f_wall()])
    pf.set_keepouts(no_vias)
    t0 = time.monotonic()
    routes, _stats = pf.route_netset([], coupled=[pc], pair_gateways=True)
    assert time.monotonic() - t0 < 60.0
    assert routes == {}
    reason = pf.pair_outcomes[pc.key]
    assert reason in GATEWAY_RETRY_REASONS, reason
    assert pf.failure_reasons[pc.key] == reason
    assert "pair-gateway-no-candidates" in pf.pair_gateway_log[pc.key]
    assert "candidates A=0/0 B=0/0" in pf.pair_gateway_log[pc.key]


def test_two_layer_board_with_no_other_routable_layer_declines() -> None:
    # B.Cu is outside ``allowed_layers``: a through via would land copper on
    # it, so no gateway layer exists at all.
    rules = DesignRules(
        trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb", allowed_layers=["F.Cu"]
    )
    pf, pc = _board([_f_wall()], rules=rules)
    t0 = time.monotonic()
    routes, _stats = pf.route_netset([], coupled=[pc], pair_gateways=True)
    assert time.monotonic() - t0 < 60.0
    assert routes == {}
    assert pf.pair_outcomes[pc.key] in GATEWAY_RETRY_REASONS
    assert pf.pair_gateway_log[pc.key] == "gateway: no other routable layer"
    assert pf._route_pair_gateway(pc, committed=pf._fresh_committed(), history={}, present=1.0)[
        1
    ] == ("pair-gateway-no-layer")


def test_planar_coupled_pair_is_byte_identical_with_gateway_retry() -> None:
    pf_off, pc_off = _board()
    routes_off, _ = pf_off.route_netset([], coupled=[pc_off], uncoupled_fallback=True)
    pf_on, pc_on = _board()
    routes_on, _ = pf_on.route_netset(
        [], coupled=[pc_on], uncoupled_fallback=True, pair_gateways=True
    )
    assert pf_on.pair_outcomes[pc_on.key] == "coupled"
    assert pf_on.pair_gateways == {}
    assert _signature(routes_on) == _signature(routes_off)
    assert all(not r.vias for r in routes_on.values())


def test_gateway_routing_is_deterministic() -> None:
    sigs = []
    for _ in range(2):
        pf, pc = _board([_f_wall()])
        routes, _ = pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
        sigs.append((_signature(routes), dict(pf.pair_gateways)))
    assert sigs[0] == sigs[1]


def test_via_limit_prunes_gateways_before_search() -> None:
    pf, pc = _board([_f_wall()])
    pf.pair_gateway_limits = {pc.key: (None, 1)}  # both ends need a via
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert "pair-gateway-via-limit" in pf.pair_gateway_log[pc.key]


def test_candidate_cap_is_honoured() -> None:
    def permissive(site: GatewaySite):
        return [(0.0, 0.0), site.via_p], [(1.0, 0.0), site.via_n]

    found, legal = enumerate_gateways(
        (0.0, 0.0), (1.0, 0.0), (10.0, 0.0), 1, permissive, pitch=0.3, via_gap=0.8, cap=5
    )
    assert len(found) == 5
    assert legal > 5
    costs = [g.cost for g in found]
    assert costs == sorted(costs)
    # Deterministic: a second enumeration is identical.
    again, _ = enumerate_gateways(
        (0.0, 0.0), (1.0, 0.0), (10.0, 0.0), 1, permissive, pitch=0.3, via_gap=0.8, cap=5
    )
    assert again == found


def test_fan_budget_prunes_long_setbacks() -> None:
    def permissive(site: GatewaySite):
        return [(0.0, 0.0), site.via_p], [(1.0, 0.0), site.via_n]

    found, _ = enumerate_gateways(
        (0.0, 0.0),
        (1.0, 0.0),
        (10.0, 0.0),
        1,
        permissive,
        pitch=0.3,
        via_gap=0.8,
        max_fan_mm=0.35,
    )
    assert found
    assert all(max(g.fan_len_p, g.fan_len_n) <= 0.35 + 1e-9 for g in found)


def test_proposed_sites_keep_p_via_on_p_side() -> None:
    # P above N: every site's P via must sit on P's side of the pair axis
    # (fan legs never cross), and the two vias at least the via gap apart.
    for ladder in propose_gateway_sites((0.0, 0.0), (0.0, 1.0), pitch=0.3, via_gap=0.8):
        for site in ladder:
            if site.heading in (0, 4):  # across the pair axis
                assert site.via_p[1] < site.via_n[1]
            dx = site.via_p[0] - site.via_n[0]
            dy = site.via_p[1] - site.via_n[1]
            assert (dx * dx + dy * dy) ** 0.5 >= 0.8 - 1e-6


def test_combos_skip_inconsistent_handedness() -> None:
    def gw(heading: int, via_p, via_n, cost: float) -> Gateway:
        site = GatewaySite(heading, "centred", 0.5, via_p, via_n)
        return Gateway(1, site, (via_p,), (via_n,), cost)

    # End A heads +x with P above (y smaller); end B arrives travelling +x
    # (heading -x) and must also have P above to avoid a twist.
    a = gw(0, (1.0, -0.4), (1.0, 0.4), 1.0)
    b_ok = gw(4, (9.0, -0.4), (9.0, 0.4), 1.0)
    b_twist = gw(4, (9.0, 0.4), (9.0, -0.4), 0.5)
    assert handedness_consistent(a, b_ok)
    assert not handedness_consistent(a, b_twist)
    assert rank_gateway_combos([a], [b_twist, b_ok]) == [(a, b_ok)]
    assert handedness_consistent(None, b_twist)


def _fake_grown(pf: LatticePathfinder, runs: dict[bool, tuple[int, int]], calls: list):
    """Stand-in for ``_route_netset_grown``: ``runs[gateways] = (routed, left)``."""
    from types import SimpleNamespace

    from kicad_tools.router.lattice.pathfinder import LatticeNegotiationStats

    def fake(connections, **kwargs):
        gw = bool(kwargs.get("pair_gateways"))
        calls.append(gw)
        routed, left = runs[gw]
        pf.class_pad_repair_stats = SimpleNamespace(total_remaining=left)
        pf.pair_gateways = {("pair", 1, 2): "gateway B.Cu"} if gw else {}
        pf.pair_outcomes = {("pair", 1, 2): "coupled" if gw else "uncoupled-fallback:x"}
        stats = LatticeNegotiationStats(
            iterations=1, converged=True, routed=routed, total=2, lattice_builds=1
        )
        return {"run": gw}, stats

    return fake


def test_unclean_gateway_run_is_renegotiated_without_gateways(monkeypatch) -> None:
    pf, _pc = _board()
    calls: list = []
    monkeypatch.setattr(
        pf, "_route_netset_grown", _fake_grown(pf, {True: (2, 1), False: (2, 0)}, calls)
    )
    routes, _stats = pf.route_netset([], pair_gateways=True)
    assert calls == [True, False]
    assert routes == {"run": False}
    assert pf.pair_gateways == {}
    assert "kept" in (pf.pair_gateway_rollback or "")


def test_gateway_run_kept_when_the_rerun_is_no_better(monkeypatch) -> None:
    pf, _pc = _board()
    calls: list = []
    monkeypatch.setattr(
        pf, "_route_netset_grown", _fake_grown(pf, {True: (2, 1), False: (2, 1)}, calls)
    )
    routes, _stats = pf.route_netset([], pair_gateways=True)
    assert calls == [True, False]
    assert routes == {"run": True}
    assert pf.pair_gateways == {("pair", 1, 2): "gateway B.Cu"}
    assert pf.pair_outcomes == {("pair", 1, 2): "coupled"}
    assert "gateways kept" in (pf.pair_gateway_rollback or "")


def test_clean_gateway_run_is_not_renegotiated(monkeypatch) -> None:
    pf, _pc = _board()
    calls: list = []
    monkeypatch.setattr(
        pf, "_route_netset_grown", _fake_grown(pf, {True: (2, 0), False: (2, 0)}, calls)
    )
    routes, _stats = pf.route_netset([], pair_gateways=True)
    assert calls == [True]
    assert routes == {"run": True}
    assert pf.pair_gateway_rollback is None


# ---------------------------------------------------------------------------
# Issue #6842: a trunk skipped for a tap chain that never served it is offered
# the gateway retry
# ---------------------------------------------------------------------------


def test_gateway_offer_keys_cover_unserved_skipped_trunks() -> None:
    from types import SimpleNamespace

    from kicad_tools.router.core import _lattice_gateway_offer_keys

    def pc(key, name):
        return SimpleNamespace(key=key, pair_name=name)

    coupled = [
        pc(("pair", 1, 2), "A"),  # skipped, declined pair-no-path (tap retry discarded)
        pc(("pair", 3, 4), "B"),  # skipped, declined pair-leg-keepout (tap retry never runs)
        pc(("pair", 5, 6), "C"),  # skipped but coupled: nothing to offer
        pc(("pair", 7, 8), "D"),  # not skipped (no tap alternative): never gated
        pc(("pair", 9, 10, 0), "E"),  # tap chain kept: chain key, no trunk left
        pc(("pair", 11, 12), "F"),  # skipped, declined for a non-gateway reason
    ]
    outcomes = {
        ("pair", 1, 2): "uncoupled-fallback:pair-no-path",
        ("pair", 3, 4): "uncoupled-fallback:pair-leg-keepout",
        ("pair", 5, 6): "coupled",
        ("pair", 7, 8): "uncoupled-fallback:pair-no-path",
        ("pair", 9, 10, 0): "coupled",
        ("pair", 11, 12): "uncoupled-fallback:pair-no-parity",
    }
    got = _lattice_gateway_offer_keys(coupled, outcomes, {"A", "B", "C", "E", "F"})
    assert got == {("pair", 1, 2), ("pair", 3, 4)}


def _spy_negotiations(monkeypatch, *, deadline_first: bool = False) -> list[frozenset]:
    """Record ``pair_gateway_skip`` as each lattice negotiation starts."""
    import dataclasses

    skips: list[frozenset] = []
    real = LatticePathfinder.route_netset

    def spy(self, connections, **kwargs):
        skips.append(self.pair_gateway_skip)
        routes, stats = real(self, connections, **kwargs)
        if deadline_first and len(skips) == 1:
            stats = dataclasses.replace(stats, deadline_hit=True)
        return routes, stats

    monkeypatch.setattr(LatticePathfinder, "route_netset", spy)
    return skips


def _tap_router():
    from tests.router.lattice.test_coupled_pairs import _tap_pads, _tap_walls, _trunk_autorouter

    return _trunk_autorouter(_tap_pads() + _tap_walls())


def test_gateway_offered_when_the_tap_retry_does_not_run(monkeypatch, capsys) -> None:
    import kicad_tools.router.core as core

    monkeypatch.setattr(core, "_lattice_tap_chain_retry_pairs", lambda *a: frozenset())
    skips = _spy_negotiations(monkeypatch)
    router = _tap_router()
    router.route_net(1)
    out = capsys.readouterr().out
    assert "tap-chain retry" not in out
    assert "gateway offer for 1 pair(s)" in out
    assert len(skips) == 2
    assert skips[0] == {("pair", 1, 2)}  # first run: held back for the tap chain
    assert skips[1] == frozenset()  # offer run: lifted


def test_gateway_offered_when_the_tap_retry_is_discarded(monkeypatch, capsys) -> None:
    import kicad_tools.router.core as core

    monkeypatch.setattr(core, "_lattice_tap_chain_retry_keeps", lambda *a: (False, []))
    skips = _spy_negotiations(monkeypatch)
    router = _tap_router()
    router.route_net(1)
    out = capsys.readouterr().out
    assert "tap-chain retry for 1 pair(s)" in out and "discarded" in out
    assert "gateway offer for 1 pair(s)" in out
    assert [bool(s) for s in skips] == [True, False, False]  # first, tap retry, offer


def test_gateway_not_offered_when_the_tap_retry_is_kept(monkeypatch, capsys) -> None:
    skips = _spy_negotiations(monkeypatch)
    router = _tap_router()
    router.route_net(1)
    out = capsys.readouterr().out
    assert "tap-chain retry for 1 pair(s)" in out and ": kept" in out
    assert "gateway offer" not in out
    assert len(skips) == 2  # first run + tap retry, nothing more


def test_gateway_not_offered_after_the_deadline(monkeypatch, capsys) -> None:
    import kicad_tools.router.core as core

    monkeypatch.setattr(core, "_lattice_tap_chain_retry_pairs", lambda *a: frozenset())
    skips = _spy_negotiations(monkeypatch, deadline_first=True)
    router = _tap_router()
    router.route_net(1)
    assert "gateway offer" not in capsys.readouterr().out
    assert len(skips) == 1  # the first result is returned as is


def test_discarded_gateway_offer_restores_the_first_result(monkeypatch, capsys) -> None:
    """A gateway that buys no coupling leaves exactly today's copper."""
    import kicad_tools.router.core as core

    def run():
        router = _tap_router()
        routes = {
            net: [
                [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in r.segments]
                for r in router.route_net(net)
            ]
            for net in (1, 2)
        }
        return router, routes

    monkeypatch.setattr(core, "_lattice_tap_chain_retry_pairs", lambda *a: frozenset())
    monkeypatch.setattr(core, "_lattice_gateway_offer_keys", lambda *a: frozenset())
    base, base_routes = run()
    capsys.readouterr()
    monkeypatch.undo()

    monkeypatch.setattr(core, "_lattice_tap_chain_retry_pairs", lambda *a: frozenset())
    monkeypatch.setattr(core, "_lattice_tap_chain_retry_keeps", lambda *a: (False, []))
    forced, forced_routes = run()
    out = capsys.readouterr().out
    assert "gateway offer" in out and "discarded" in out
    assert forced_routes == base_routes
    assert forced._lattice_failure_reasons == base._lattice_failure_reasons


# -- issue #6834: review follow-ups ------------------------------------------


def test_discarded_tap_chain_retry_restores_gateway_state() -> None:
    from types import SimpleNamespace

    from kicad_tools.router.core import Autorouter

    pf, _pc = _board()
    pf.pair_gateways = {("pair", 1, 2): "first gateway"}
    pf.pair_gateway_log = {("pair", 1, 2): "first log"}
    pf.pair_gateway_rollback = "first rollback"
    router = SimpleNamespace(
        _lattice_pair_outcomes={},
        _lattice_pair_hops={},
        _lattice_tap_chain_pairs=set(),
        _lattice_connection_endpoints={},
        _lattice_pair_ties=[],
        _lattice_netset_connections=lambda retry_pairs: ([], []),
    )

    def negotiate(connections, coupled):
        pf.pair_gateways = {("pair", 1, 2): "retry gateway"}
        pf.pair_gateway_log = {("pair", 1, 2): "retry log"}
        pf.pair_gateway_rollback = "retry rollback"
        return {}, SimpleNamespace(deadline_hit=True)

    first = ({"first": 1}, SimpleNamespace(deadline_hit=False), [])
    out = Autorouter._lattice_tap_chain_retry(router, pf, negotiate, frozenset(), first)
    assert out[0] == {"first": 1}
    assert pf.pair_gateways == {("pair", 1, 2): "first gateway"}
    assert pf.pair_gateway_log == {("pair", 1, 2): "first log"}
    assert pf.pair_gateway_rollback == "first rollback"


def test_rejected_growth_attempt_restores_gateway_log(monkeypatch) -> None:
    from types import SimpleNamespace

    from kicad_tools.router.lattice.pathfinder import LatticeNegotiationStats

    pf, _pc = _board()
    key = ("pair", 1, 2)
    calls: list[int] = []

    def fake_once(connections, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            pf.pair_gateway_log = {key: "first run"}
            pf.class_pad_repair_stats = SimpleNamespace(total_remaining=1, unrepaired_pads={0: 0.5})
        else:
            pf.pair_gateway_log = {key: "rejected growth run"}
            pf.class_pad_repair_stats = SimpleNamespace(total_remaining=1, unrepaired_pads={})
        stats = LatticeNegotiationStats(
            iterations=1, converged=True, routed=1, total=1, lattice_builds=1
        )
        return {}, stats

    monkeypatch.setattr(pf, "_route_netset_once", fake_once)
    pf._route_netset_grown([])
    assert len(calls) >= 2  # a growth attempt ran and was rejected
    assert pf.pair_gateway_log == {key: "first run"}


def test_gateway_via_limit_counts_other_chain_segments(monkeypatch) -> None:
    pf, pc = _board([_f_wall()])
    sibling = CoupledConnection(
        key=("pair", 1, 2, 1),
        pair_name=pc.pair_name,
        pad_p_a=pc.pad_p_a,
        pad_n_a=pc.pad_n_a,
        pad_p_b=pc.pad_p_b,
        pad_n_b=pc.pad_n_b,
        net_class=_HS,
        pitch=_PITCH,
    )
    pf.pair_gateway_limits = {pc.key: (None, 2)}  # this segment alone fits (2 vias/net)
    committed = pf._fresh_committed()
    kw = {"committed": committed, "history": {}, "present": 1.0}
    res, why = pf._route_pair_gateway(pc, vias_used=1, **kw)
    assert res is None and why == "pair-gateway-via-limit"
    assert "other segments" in pf.pair_gateway_log[pc.key]
    res, _why = pf._route_pair_gateway(pc, vias_used=0, **kw)
    assert res is not None
    assert sibling.pair_name == pc.pair_name


def test_setback_plus_attach_dogleg_stays_under_tightest_protocol_budget() -> None:
    from kicad_tools.router.lattice.gateway import MAX_GATEWAY_SETBACK_MM
    from kicad_tools.router.pair_protocols import PROTOCOL_DEFAULTS

    budgets = [
        row.max_uncoupled_per_terminal_mm.value
        for row in PROTOCOL_DEFAULTS.values()
        if row.max_uncoupled_per_terminal_mm is not None
    ]
    tightest = min(budgets)
    # The attach dogleg runs from the via to the body anchor: at most the
    # anchor ``reach`` plus the 1 mm lattice-node snap of ``_gateway_anchor``.
    # Generous geometry (0.8 mm via, 0.3 mm trace, 0.5 mm pitch, 0.2 clearance).
    reach = 0.5 / 2.0 + 0.3 / 2.0 + 0.2 + 0.8 / 2.0
    attach = reach + 1.0
    assert MAX_GATEWAY_SETBACK_MM + attach < tightest
