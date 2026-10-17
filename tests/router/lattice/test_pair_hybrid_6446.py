"""One-ended hybrid trunk + legs for lattice diff pairs (issue #6446, phase 2d).

A segment the planar search and the 2a fan-via gateway both decline can have
exactly ONE terminal that cannot couple (no legal via site, no corridor for the
pair envelope).  The retry couples the rest: a trunk from the easy end to a
start outside the hard part's pad field, joined to the pads by two single-ended
legs.

Synthetic boards only (the four-board bench lives in
``scripts/research/diffpair_bench.py``).  The hard terminal is U1: a pair pad
pair walled in by a ring of GND balls whose gaps pass one trace but not the
pair envelope, with vias forbidden around it.  The easy end, J1, has legal
gateway sites.
"""

from __future__ import annotations

import re

from kicad_tools.router.lattice.coupled import CoupledConnection, seg_rect_dist
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairplan.hybrid import HYBRID_MAX_LEG_MM, MIN_TRUNK_PITCHES
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
_CX, _CY = 24.0, 6.0
_RING = 1.65  # ring half-size; pads 0.5 mm square on a 1.1 mm pitch => 0.6 mm gaps


def _pad(x: float, y: float, net: int, name: str, ref: str, size: tuple[float, float]) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=size[0],
        height=size[1],
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _board(
    *, crossed: bool = False, block_vias: bool = True
) -> tuple[LatticePathfinder, CoupledConnection, list[Pad]]:
    p_a = _pad(3.0, 5.7, 1, "D+", "J1", (0.3, 0.3))
    n_a = _pad(3.0, 6.3, 2, "D-", "J1", (0.3, 0.3))
    dy = 0.3 if crossed else -0.3
    p_b = _pad(_CX, _CY + dy, 1, "D+", "U1", (0.3, 0.3))
    n_b = _pad(_CX, _CY - dy, 2, "D-", "U1", (0.3, 0.3))
    ring: list[tuple[float, float]] = []
    for x in (-_RING, -0.55, 0.55, _RING):
        ring += [(_CX + x, _CY - _RING), (_CX + x, _CY + _RING)]
    for y in (-0.55, 0.55):
        ring += [(_CX - _RING, _CY + y), (_CX + _RING, _CY + y)]
    balls = [_pad(x, y, 9, "GND", "U1", (0.5, 0.5)) for x, y in ring]
    pads = [p_a, n_a, p_b, n_b, *balls]
    if crossed:
        # J1 can only be left eastward: no hooking round it to flip handedness.
        pads += [
            _pad(1.9, 6.0, 9, "GND", "J1", (0.5, 6.0)),
            _pad(3.6, 3.9, 9, "GND", "J1", (3.0, 0.5)),
            _pad(3.6, 8.1, 9, "GND", "J1", (3.0, 0.5)),
        ]
    pf = LatticePathfinder(_OUTLINE, pads, _RULES, LayerStack.two_layer())
    if block_vias:
        poly = ((_CX - 5, _CY - 5), (_CX + 5, _CY - 5), (_CX + 5, _CY + 5), (_CX - 5, _CY + 5))
        pf.set_keepouts(
            LatticeKeepoutMask(
                [
                    KeepoutArea(
                        polygon=poly,
                        layers=frozenset({0, 1}),
                        blocks_tracks=False,
                        blocks_vias=True,
                    )
                ]
            )
        )
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
    return pf, pc, balls


def _route(pf: LatticePathfinder, pc: CoupledConnection, **kw: object):
    return pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True, **kw)


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


def _coupled_trunk_mm(routes: dict, key: object) -> float:
    """Length of P copper that runs parallel to N copper at exactly the pitch."""
    total = 0.0
    for s1 in routes[(key, "P")].segments:
        a, b = (s1.x1, s1.y1), (s1.x2, s1.y2)
        for s2 in routes[(key, "N")].segments:
            if s1.layer != s2.layer:
                continue
            c, d = (s2.x1, s2.y1), (s2.x2, s2.y2)
            if abs(seg_seg_dist(a, b, c, d) - _PITCH) < 1e-3:
                total += ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
                break
    return total


def _legs(log: str) -> tuple[float, float]:
    m = re.search(r"legs P/N=([\d.]+)/([\d.]+) mm", log)
    assert m, log
    return float(m.group(1)), float(m.group(2))


def _assert_clear_of_balls(routes: dict, key: object, balls: list[Pad]) -> None:
    half = _HS.trace_width / 2.0
    for pol in ("P", "N"):
        for s in routes[(key, pol)].segments:
            if s.layer != Layer.F_CU:
                continue
            for ball in balls:
                rect = (ball.x - 0.25, ball.y - 0.25, ball.x + 0.25, ball.y + 0.25)
                d = seg_rect_dist((s.x1, s.y1), (s.x2, s.y2), rect)
                assert d >= half + _HS.clearance - 1e-6, (pol, d)


def test_ring_fixture_declines_planar_and_gateway_then_couples_as_hybrid() -> None:
    pf, pc, balls = _board()
    routes, stats = _route(pf, pc)
    assert pf.pair_outcomes[pc.key] == "hybrid:U1.1", (
        pf.pair_gateway_log.get(pc.key),
        pf.pair_hybrid_log.get(pc.key),
    )
    # The gateway retry ran and named U1 (end B) as the end with no site.
    assert pf.pair_gateway_ends[pc.key][1] == 0
    assert pf.pair_gateway_ends[pc.key][0] != 0
    assert "pair-gateway-no-candidates" in pf.pair_gateway_log[pc.key]
    # A hybrid is a trunk plus two legs: two units, like a fallback pair.
    assert stats.routed == stats.total == 2
    # Trunk >= 2 x pitch at exactly the pitch; both legs within the cap.
    assert _coupled_trunk_mm(routes, pc.key) >= MIN_TRUNK_PITCHES * _PITCH
    lp, ln = _legs(pf.pair_hybrid_log[pc.key])
    assert 0.0 < max(lp, ln) <= HYBRID_MAX_LEG_MM
    assert "engine bound" in pf.pair_hybrid_log[pc.key]
    _assert_clear_of_balls(routes, pc.key, balls)
    # The pair is hybrid, not coupled: the gateway table stays empty.
    assert pf.pair_gateways == {}
    record = pf.pair_outcomes.record(pc.key)
    assert record is not None and record.status == "hybrid"


def test_declared_budget_below_the_shortest_leg_declines_to_the_fallback() -> None:
    pf, pc, _balls = _board()
    pf.pair_gateway_limits[pc.key] = (5.0, None)  # shortest feasible legs are ~6.5 mm
    routes, _stats = _route(pf, pc)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:"), pf.pair_hybrid_log
    log = pf.pair_hybrid_log[pc.key]
    # Names the terminal, the shortest legs found and the cap.
    assert "U1.1" in log and "shortest legs found" in log and "cap 5.0 mm, declared" in log
    # No hybrid copper: nothing runs coupled at the pitch.
    assert _coupled_trunk_mm(routes, pc.key) == 0.0


def test_crossed_polarity_with_no_swap_resolves_in_the_legs() -> None:
    pf, pc, balls = _board(crossed=True)
    routes, _stats = _route(pf, pc)
    assert pf.pair_outcomes[pc.key] == "hybrid:U1.1", pf.pair_hybrid_log.get(pc.key)
    assert _coupled_trunk_mm(routes, pc.key) >= MIN_TRUNK_PITCHES * _PITCH
    lp, ln = _legs(pf.pair_hybrid_log[pc.key])
    assert max(lp, ln) <= HYBRID_MAX_LEG_MM
    _assert_clear_of_balls(routes, pc.key, balls)
    # Each net's copper is one connected chain from its J1 pad to its U1 pad.
    for pol, (ax, ay), (bx, by) in (
        ("P", (3.0, 5.7), (_CX, _CY + 0.3)),
        ("N", (3.0, 6.3), (_CX, _CY - 0.3)),
    ):
        ends = {(round(s.x1, 3), round(s.y1, 3)) for s in routes[(pc.key, pol)].segments} | {
            (round(s.x2, 3), round(s.y2, 3)) for s in routes[(pc.key, pol)].segments
        }
        assert (ax, ay) in ends and (bx, by) in ends


def test_both_ends_hard_keeps_the_fallback_and_says_so() -> None:
    pf, pc, _balls = _board()
    # Forbid vias around J1 as well: neither end has a legal gateway site.
    poly = ((0.0, 0.0), (8.0, 0.0), (8.0, 12.0), (0.0, 12.0))
    ring_poly = ((_CX - 5, _CY - 5), (_CX + 5, _CY - 5), (_CX + 5, _CY + 5), (_CX - 5, _CY + 5))
    pf.set_keepouts(
        LatticeKeepoutMask(
            [
                KeepoutArea(
                    polygon=p, layers=frozenset({0, 1}), blocks_tracks=False, blocks_vias=True
                )
                for p in (poly, ring_poly)
            ]
        )
    )
    _routes, _stats = _route(pf, pc)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert "both ends hard" in pf.pair_hybrid_log[pc.key]


def test_planar_coupled_pair_is_byte_identical_with_the_hybrid_retry() -> None:
    def run(hybrid: bool) -> tuple[list, str]:
        p_a = _pad(3.0, 5.0, 1, "D+", "J1", (0.5, 0.5))
        n_a = _pad(3.0, 6.0, 2, "D-", "J1", (0.5, 0.5))
        p_b = _pad(27.0, 5.0, 1, "D+", "U1", (0.5, 0.5))
        n_b = _pad(27.0, 6.0, 2, "D-", "U1", (0.5, 0.5))
        pf = LatticePathfinder(_OUTLINE, [p_a, n_a, p_b, n_b], _RULES, LayerStack.two_layer())
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
        if not hybrid:
            pf._route_pair_hybrid = lambda *a, **k: (None, "pair-hybrid-disabled")  # type: ignore[method-assign]
        routes, _ = _route(pf, pc)
        assert not pf.pair_hybrid_log
        return _signature(routes), pf.pair_outcomes[pc.key]

    on, outcome_on = run(True)
    off, outcome_off = run(False)
    assert outcome_on == outcome_off == "coupled"
    assert on == off


def test_gateway_coupled_pair_is_byte_identical_with_the_hybrid_retry() -> None:
    def run(hybrid: bool) -> tuple[list, str]:
        p_a = _pad(3.0, 5.0, 1, "D+", "J1", (0.5, 0.5))
        n_a = _pad(3.0, 6.0, 2, "D-", "J1", (0.5, 0.5))
        p_b = _pad(27.0, 5.0, 1, "D+", "U1", (0.5, 0.5))
        n_b = _pad(27.0, 6.0, 2, "D-", "U1", (0.5, 0.5))
        wall = _pad(15.0, 6.0, 9, "WALL", "WF", (0.6, 13.0))
        pf = LatticePathfinder(_OUTLINE, [p_a, n_a, p_b, n_b, wall], _RULES, LayerStack.two_layer())
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
        if not hybrid:
            pf._route_pair_hybrid = lambda *a, **k: (None, "pair-hybrid-disabled")  # type: ignore[method-assign]
        routes, _ = _route(pf, pc)
        assert not pf.pair_hybrid_log
        assert pf.pair_gateways
        return _signature(routes), pf.pair_outcomes[pc.key]

    on, outcome_on = run(True)
    off, outcome_off = run(False)
    assert outcome_on == outcome_off == "coupled"
    assert on == off


def test_hybrid_routing_is_deterministic() -> None:
    sigs = []
    for _ in range(2):
        pf, pc, _balls = _board()
        routes, _ = _route(pf, pc)
        sigs.append((_signature(routes), dict(pf.pair_hybrid_log)))
    assert sigs[0] == sigs[1]


def test_hybrid_is_not_offered_when_the_gateway_retry_is_off() -> None:
    pf, pc, _balls = _board()
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert pf.pair_hybrid_log == {}
