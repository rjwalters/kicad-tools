"""Pass-under gateway orientation for lattice diff pairs (issue #6836).

The #6444 gateway model fixes the coupled body's direction at a via pair:
it leaves end A along the gateway heading and arrives at end B against it.
On a dual-row SMD end part whose matching row sits BEHIND the facing row
(board 06rf J1: USB2 D+/D- on A6/A7, with the opposite-polarity B7/B6 in
front of them), every legal gateway sits behind the matching row, away from
the other end, and the only twist-free coupled body reaches it from the pad
side -- under both rows, on a layer those pads do not own.  That
orientation is ``through`` (pass-under).

Pass-under combinations form a second, strictly later group: they are tried
only after every pre-#6836 combination declined, so a pair that couples
today keeps its combination and copper.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairplan import gateway as pp_gateway
from kicad_tools.router.pairplan.gateway import (
    THROUGH_ORIENTATIONS,
    Gateway,
    GatewayCombo,
    GatewaySite,
    gateway_anchor_point,
    gateway_side,
    handedness_consistent,
    rank_gateway_combos,
    rank_through_combos,
)
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


def _gw(heading: int, via_p, via_n, cost: float) -> Gateway:
    site = GatewaySite(heading, "parallel", 0.6, via_p, via_n)
    return Gateway(1, site, (via_p,), (via_n,), cost)


# -- pure model ---------------------------------------------------------------


@pytest.mark.parametrize("heading", range(8))
@pytest.mark.parametrize("arriving", [False, True])
def test_pass_under_side_is_the_reverse_of_today(heading: int, arriving: bool) -> None:
    g = _gw(heading, (0.0, 0.0), (0.7, 0.2), 1.0)  # never along a heading
    today = gateway_side(g, arriving=arriving)
    assert gateway_side(g, arriving=arriving, through=False) == today
    assert gateway_side(g, arriving=arriving, through=True) is (not today)


def test_06rf_like_gateways_are_consistent_only_pass_under() -> None:
    # 06rf geometry (seed-42 probe): U1 at y=66 fans -y toward J1; J1's
    # only gateways sit behind its A row at y=54.9, also heading -y (away
    # from U1).  D+ is the smaller x at both ends.
    u1_toward = _gw(6, (111.5, 65.3), (112.3, 65.3), 1.4)
    j1_behind = _gw(6, (113.0, 54.9), (114.0, 54.9), 3.2)
    assert not handedness_consistent(u1_toward, j1_behind)
    assert handedness_consistent(u1_toward, j1_behind, through_b=True)
    assert rank_gateway_combos([u1_toward], [j1_behind]) == []
    # Under J1's rows first (the orientation order breaks the cost tie);
    # back over U1's own pads second.  Never both: that re-twists.
    assert rank_through_combos([u1_toward], [j1_behind]) == [
        GatewayCombo(u1_toward, j1_behind, False, True),
        GatewayCombo(u1_toward, j1_behind, True, False),
    ]


def test_through_group_never_repeats_an_against_combo_and_keeps_today_unchanged() -> None:
    a = [
        _gw(0, (1.0, -0.4), (1.0, 0.4), 1.0),
        _gw(4, (-1.0, 0.4), (-1.0, -0.4), 1.5),
        None,
    ]
    b = [
        _gw(4, (9.0, -0.4), (9.0, 0.4), 1.0),
        _gw(0, (11.0, -0.4), (11.0, 0.4), 0.5),
        _gw(4, (9.0, 0.4), (9.0, -0.4), 0.7),
    ]
    today = rank_gateway_combos(a, b)
    # The pre-#6836 list: an independent recomputation of its rule.
    expected = sorted(
        (
            (round((ga.cost if ga else 0.0) + gb.cost, 9), i, j, ga, gb)
            for i, ga in enumerate(a)
            for j, gb in enumerate(b)
            if ga is None or gateway_side(ga, arriving=False) == gateway_side(gb, arriving=True)
        ),
        key=lambda c: (c[0], c[1], c[2]),
    )
    assert today == [(ga, gb) for *_k, ga, gb in expected[:6]]

    through = rank_through_combos(a, b)
    assert through
    for combo in through:
        assert combo.through_a or combo.through_b
        # A gatewayless end has no orientation to flip.
        assert not (combo.through_a and combo.ga is None)
        assert not (combo.through_b and combo.gb is None)
        assert handedness_consistent(
            combo.ga, combo.gb, through_a=combo.through_a, through_b=combo.through_b
        )
    costs = [(c.ga.cost if c.ga else 0.0) + c.gb.cost for c in through]
    assert costs == sorted(costs)
    assert len(through) <= 6
    # Deterministic.
    assert rank_through_combos(a, b) == through


def test_through_combos_respect_the_cap_and_orientation_order() -> None:
    a = [_gw(0, (1.0, -0.4), (1.0, 0.4), 1.0)]
    b = [_gw(h, (9.0, -0.4), (9.0, 0.4), 1.0) for h in range(8)]
    out = rank_through_combos(a, b, top_n=3)
    assert len(out) == 3
    for combo in out:
        assert (combo.through_a, combo.through_b) in THROUGH_ORIENTATIONS


def test_pass_under_anchor_sits_on_the_pad_side() -> None:
    # Vias 0.6 mm behind pads at y=0, heading +y (index 2).
    site = GatewaySite(2, "parallel", 0.6, (0.0, 0.6), (1.0, 0.6))
    out = gateway_anchor_point(site, 0.7)
    back = gateway_anchor_point(site, 0.7, through=True)
    assert out is not None and back is not None
    assert out[1] > 0.6 and back[1] < 0.6
    assert abs(out[0] - 0.5) < 1e-9 and abs(back[0] - 0.5) < 1e-9


# -- synthetic 06rf-J1-like board ---------------------------------------------


def _pad(x: float, y: float, net: int, name: str, ref: str, pin: str, w: float = 0.5) -> Pad:
    return Pad(
        x=x, y=y, width=w, height=0.5, net=net, net_name=name, layer=Layer.F_CU, ref=ref, pin=pin
    )


def _dual_row_board() -> tuple[LatticePathfinder, CoupledConnection]:
    """U1 (SMD) -> J1 (SMD, F.Cu only), matching row BEHIND the facing row.

    J1's B row (x=24.0) faces U1 with D- (B7) in front of D+ (A6) and D+
    (B6) in front of D- (A7); the A row (x=25.2) has U1's handedness.  The
    rows are too close for the pair to enter between them, foreign pads fill
    both rows above and below, and the board edge sits right behind the A
    row, so the A row's gateway vias have no room for a body beyond them.
    The only twist-free coupled route is U1 -> under both rows on B.Cu.
    """
    outline = [(1.5, 0.0), (26.8, 0.0), (26.8, 11.0), (1.5, 11.0)]
    p_a = _pad(3.0, 5.0, 1, "D+", "U1", "10")
    n_a = _pad(3.0, 5.8, 2, "D-", "U1", "11")
    pads = [p_a, n_a]
    net = 10
    for y in (2.0, 3.0, 4.0, 7.0, 8.0, 9.0):
        for x, row in ((24.0, "B"), (25.2, "A")):
            pads.append(_pad(x, y, net, f"N{net}", "J1", f"{row}{int(y)}", w=0.6))
            net += 1
    p_b = _pad(25.2, 5.0, 1, "D+", "J1", "A6", w=0.6)
    n_b = _pad(25.2, 6.0, 2, "D-", "J1", "A7", w=0.6)
    pads += [
        p_b,
        n_b,
        _pad(24.0, 6.0, 1, "D+", "J1", "B6", w=0.6),
        _pad(24.0, 5.0, 2, "D-", "J1", "B7", w=0.6),
    ]
    pf = LatticePathfinder(outline, pads, _RULES, LayerStack.two_layer())
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


def test_dual_row_end_couples_through_pass_under() -> None:
    pf, pc = _dual_row_board()
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_gateway_log
    log = pf.pair_gateways[pc.key]
    assert "gateway B.Cu" in log
    assert "B: heading=0deg" in log and log.count("pass-under") == 1, log
    assert "vias P/N=2/2" in log
    assert stats.routed == stats.total == 1
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    assert len(route_p.vias) == len(route_n.vias) == 2
    # The body passes UNDER J1's rows: B.Cu copper spans x=24.0..25.2.
    for route in (route_p, route_n):
        b_cu = [s for s in route.segments if s.layer == Layer.B_CU]
        assert min(min(s.x1, s.x2) for s in b_cu) < 24.0
        assert max(max(s.x1, s.x2) for s in b_cu) > 25.2
        # F.Cu holds only the two short fans.
        for s in route.segments:
            if s.layer == Layer.F_CU:
                assert max(s.x1, s.x2) < 4.0 or min(s.x1, s.x2) >= 25.2 - 1e-6


def test_dual_row_end_does_not_couple_without_pass_under(monkeypatch) -> None:
    # The revert guard: with the pass-under group emptied, today's combos
    # all decline and the pair falls back to uncoupled legs.
    monkeypatch.setattr(pp_gateway, "rank_through_combos", lambda *a, **k: [])
    pf, pc = _dual_row_board()
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert "gateway declined" in pf.pair_gateway_log[pc.key]


def test_pass_under_routing_is_deterministic() -> None:
    sigs = []
    for _ in range(2):
        pf, pc = _dual_row_board()
        routes, _ = pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
        sigs.append(
            (
                sorted(
                    (repr(k), [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in r.segments])
                    for k, r in routes.items()
                ),
                dict(pf.pair_gateways),
            )
        )
    assert sigs[0] == sigs[1]


def test_against_gateway_pair_never_tries_pass_under(monkeypatch) -> None:
    # A pair today's combinations couple (#6444's walled pair) never reaches
    # the pass-under group, so its copper cannot change.
    from tests.router.lattice.test_pair_gateway_6444 import _board, _f_wall

    calls: list[int] = []

    def spy(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(pp_gateway, "rank_through_combos", spy)
    pf, pc = _board([_f_wall()])
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    assert pf.pair_outcomes[pc.key] == "coupled"
    assert "pass-under" not in pf.pair_gateways[pc.key]
    assert calls == []
