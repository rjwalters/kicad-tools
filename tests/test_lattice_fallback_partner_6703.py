"""Partner-aware uncoupled diff-pair fallback legs on the lattice (issue #6703).

When a pair's coupled search declines, the #6373 fallback routes its two legs
single-ended.  Pre-#6703 each leg was an unrelated net, so an obstacle wall
between the pads sent P and N round opposite ends of the wall and the pair's
skew grew by mm (board 07rf TMDS_D0/D1: 0.6/1.6 -> 7.2/8.4 mm).  The second
leg is now pulled toward the first leg's corridor and the better-matched of
the leg orders is kept.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.guide import PartnerGuide
from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route
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

# A wall on both layers from y = 4 to past the board's top edge: every route
# from the J1 pads (P at y = 5, N at y = 6) to the U1 pads must pass BELOW it,
# so P (nearer the wall's end) hugs it and N has to get round P.
_WALL_Y0, _WALL_Y1 = 4.0, 12.5
_WALL_X = 15.0


def _pad(x: float, y: float, net: int, name: str, ref: str, layer: Layer = Layer.F_CU, **kw):
    return Pad(
        x=x,
        y=y,
        width=kw.get("width", 0.5),
        height=kw.get("height", 0.5),
        net=net,
        net_name=name,
        layer=layer,
        ref=ref,
        pin="1",
    )


def _walled_pair(extra: tuple[Pad, ...] = ()) -> tuple[LatticePathfinder, CoupledConnection]:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    p_a = _pad(3.0, 5.0, 1, "D+", "J1")
    n_a = _pad(3.0, 6.0, 2, "D-", "J1")
    p_b = _pad(27.0, 5.0, 1, "D+", "U1")
    n_b = _pad(27.0, 6.0, 2, "D-", "U1")
    mid, height = (_WALL_Y0 + _WALL_Y1) / 2.0, _WALL_Y1 - _WALL_Y0
    walls = [
        _pad(_WALL_X, mid, 9, "WALL", "WF", Layer.F_CU, width=0.6, height=height),
        _pad(_WALL_X, mid, 9, "WALL", "WB", Layer.B_CU, width=0.6, height=height),
    ]
    pads = [p_a, n_a, p_b, n_b, *walls, *extra]
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


def _decline_coupled(pf: LatticePathfinder, monkeypatch: pytest.MonkeyPatch) -> None:
    # The coupled search is declined outright, as it is on 07rf where the
    # +1V8 pour-link keep-outs leave no planar corridor out of U4's row B.
    monkeypatch.setattr(pf, "_route_pair_impl", lambda *a, **k: (None, "pair-no-path"))


def _length(route: Route) -> float:
    """The skew checker's measure: tracks plus a 1.6 mm barrel per via."""
    tracks = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)
    return tracks + 1.6 * len(route.vias)


def _past_wall(route: Route) -> tuple[Layer, str]:
    """``(layer, side)`` of the leg where it passes the wall's x."""
    crossing = [s for s in route.segments if min(s.x1, s.x2) <= _WALL_X <= max(s.x1, s.x2)]
    assert len(crossing) == 1, crossing
    seg = crossing[0]
    y = (seg.y1 + seg.y2) / 2.0
    assert not (_WALL_Y0 < y < _WALL_Y1), "a leg cannot pass through the wall"
    return seg.layer, "below" if y <= _WALL_Y0 else "above"


def test_fallback_legs_take_the_same_way_past_the_wall(monkeypatch) -> None:
    pf, pc = _walled_pair()
    _decline_coupled(pf, monkeypatch)
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key] == "uncoupled-fallback:pair-no-path"
    assert stats.routed == 2 and stats.total == 2, stats
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    # Same layer and same side of the wall, same via count...
    assert _past_wall(route_p) == _past_wall(route_n)
    assert len(route_p.vias) == len(route_n.vias)
    # ...so the pair is length-matched by construction (pre-#6703: 4.03 mm).
    assert abs(_length(route_p) - _length(route_n)) < 0.5
    # Never a short: the legs keep the full copper gap from each other.
    gap = _RULES.trace_width + _RULES.trace_clearance
    for a in route_p.segments:
        for b in route_n.segments:
            if a.layer == b.layer:
                d = seg_seg_dist((a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2))
                assert d >= gap - 1e-6


def test_partner_blind_legs_diverge_past_the_wall() -> None:
    # The fixture really exercises the defect: the pre-#6703 fallback (P,
    # then N against P's committed copper, neither aware of the other) sends
    # N under P on the other layer, two vias and ~4 mm longer.
    pf, pc = _walled_pair()
    committed = pf._fresh_committed()
    legs = []
    for pol, a, b in (("P", pc.pad_p_a, pc.pad_p_b), ("N", pc.pad_n_a, pc.pad_n_b)):
        item = ((pc.key, pol), a, b, _HS)
        result, reason = pf._search_single(
            item, committed=committed, history={}, present=0.0, escape_plan={}
        )
        assert result is not None, reason
        pf._commit_single(item, result, {}, committed)
        legs.append(result.route)
    route_p, route_n = legs
    assert _past_wall(route_p)[0] != _past_wall(route_n)[0]
    assert len(route_p.vias) != len(route_n.vias)
    assert abs(_length(route_p) - _length(route_n)) > 3.0


def test_fallback_is_deterministic(monkeypatch) -> None:
    out = []
    for _ in range(2):
        pf, pc = _walled_pair()
        _decline_coupled(pf, monkeypatch)
        routes, _stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
        out.append(
            [
                (s.x1, s.y1, s.x2, s.y2, s.layer)
                for pol in ("P", "N")
                for s in routes[(pc.key, pol)].segments
            ]
        )
    assert out[0] == out[1]


def test_partner_guide_cost_is_zero_near_and_capped_far() -> None:
    guide = PartnerGuide([(0, [(0.0, 0.0), (10.0, 0.0)], [0.2])], near=0.6, cap=2.0, weight=1.0)
    # Alongside the partner, within ``near``: free.
    assert guide.edge_cost((1.0, 0.4), (2.0, 0.4), 0) == 0.0
    # 1 mm out: 0.4 mm of excess on a 1 mm edge.
    assert guide.edge_cost((1.0, 1.0), (2.0, 1.0), 0) == pytest.approx(0.4)
    # Far away, or on a layer the partner does not use: saturates at ``cap``.
    assert guide.edge_cost((1.0, 9.0), (2.0, 9.0), 0) == pytest.approx(2.0)
    assert guide.edge_cost((1.0, 0.0), (2.0, 0.0), 1) == pytest.approx(2.0)


def test_committed_trial_rolls_back_exactly() -> None:
    committed = CommittedCopper(
        2, trace_half=0.1, clearance=0.15, via_radius=0.3, via_via_gap=0.8, same_net_via_gap=0.5
    )
    committed.add_run(0, [(0.0, 0.0), (5.0, 0.0)], 1, 0.1)
    before = (
        {k: list(v) for k, v in committed.copper[0].buckets.items() if v},
        list(committed.vias),
    )
    mark = committed.begin_trial()
    committed.add_run_widths(0, [(0.0, 1.0), (5.0, 1.0), (6.0, 2.0)], 2, [0.1, 0.05])
    committed.add_via((6.0, 2.0), 2)
    assert not committed.seg_clear((0.0, 1.0), (5.0, 1.0), 0, 3)
    committed.rollback_trial(mark)
    after = (
        {k: list(v) for k, v in committed.copper[0].buckets.items() if v},
        list(committed.vias),
    )
    assert after == before
    assert committed.seg_clear((0.0, 1.0), (5.0, 1.0), 0, 3)
    assert not committed.via_copper[1:]


# -- review follow-ups (PR #6735) ---------------------------------------------


def test_one_very_strict_pad_class_does_not_disable_the_partner_aware_legs(
    monkeypatch,
) -> None:
    # A board with ONE very strict pad class (a 1.5 mm HV-style pad, ten
    # times the legs' own 0.15 mm), well away from the legs.  Only that pad's
    # keep-out is widened while the legs are searched, so the walled pair
    # still gets the matched legs -- a board-wide floor at 1.5 mm (the first
    # cut of #6703) held the legs, the wall and each other to 1.5 mm too, and
    # the partner-aware candidates could no longer pass below the wall.
    pf, pc = _walled_pair((_pad(8.0, 10.5, 7, "HV", "HV1"),))
    _decline_coupled(pf, monkeypatch)
    pf.set_pad_class_clearances([0.15] * (len(pf.pads) - 1) + [1.5])

    strict = pf._strict_pad_pairwise(pc, pf._fallback_items(pc))
    assert strict is not None
    # Only the strict pad's net is projected, for each leg.
    assert dict(strict.required_by_pair) == {(1, 7): 1.5, (2, 7): 1.5}

    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert stats.routed == 2 and stats.total == 2, stats
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    assert _past_wall(route_p) == _past_wall(route_n)
    assert abs(_length(route_p) - _length(route_n)) < 0.5


def test_strict_pad_projection_is_off_when_no_pad_is_stricter() -> None:
    pf, pc = _walled_pair()
    assert pf._strict_pad_pairwise(pc, pf._fallback_items(pc)) is None  # no classes
    pf.set_pad_class_clearances([0.15] * len(pf.pads))
    assert pf._strict_pad_pairwise(pc, pf._fallback_items(pc)) is None


def _open_pairs(n: int) -> tuple[LatticePathfinder, list[CoupledConnection]]:
    """``n`` straight, independent diff pairs on an open two-layer board."""
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 3.0 * n + 3.0), (0.0, 3.0 * n + 3.0)]
    pads: list[Pad] = []
    pairs: list[CoupledConnection] = []
    for k in range(n):
        y = 1.5 + 3.0 * k
        pn, nn = 10 + 2 * k, 11 + 2 * k
        p_a = _pad(2.0, y, pn, f"D{k}+", f"J{k}")
        n_a = _pad(2.0, y + 1.0, nn, f"D{k}-", f"J{k}")
        p_b = _pad(18.0, y, pn, f"D{k}+", f"U{k}")
        n_b = _pad(18.0, y + 1.0, nn, f"D{k}-", f"U{k}")
        pads += [p_a, n_a, p_b, n_b]
        pairs.append(
            CoupledConnection(
                key=("pair", pn, nn),
                pair_name=f"D{k}",
                pad_p_a=p_a,
                pad_n_a=n_a,
                pad_p_b=p_b,
                pad_n_b=n_b,
                net_class=_HS,
                pitch=_PITCH,
            )
        )
    pf = LatticePathfinder(outline, pads, _RULES, LayerStack.two_layer())
    # One pad class stricter than the legs' own, so no pair is trivially
    # partner-blind and every pair would be weighed without the cap.
    pf.set_pad_class_clearances([0.3] * len(pads))
    return pf, pairs


def _count_calls(monkeypatch, name: str) -> list[int]:
    calls = [0]
    original = getattr(LatticePathfinder, name)

    def counted(self, *a, **k):
        calls[0] += 1
        return original(self, *a, **k)

    monkeypatch.setattr(LatticePathfinder, name, counted)
    return calls


def test_flush_cost_is_capped_linear_in_the_fallback_pairs(monkeypatch) -> None:
    from kicad_tools.router.lattice import pathfinder as pfmod

    n = pfmod._MAX_PARTNER_AWARE_PAIRS + 3
    pf, pairs = _open_pairs(n)
    aware = _count_calls(monkeypatch, "_partner_aware_legs")
    searches = _count_calls(monkeypatch, "_search_single")
    committed = pf._fresh_committed()
    out = pf._route_fallback_pairs_into(
        pairs, {}, committed=committed, history={}, present=0.0, escape_plan={}
    )
    assert len(out) == n
    assert all(per[pol][1] is None for per in out for pol in ("P", "N"))
    k = pfmod._MAX_PARTNER_AWARE_PAIRS
    assert aware[0] == k
    # 2n partner-blind searches up front, then per weighed pair at most six
    # candidate searches and a two-search rollout of every later pair.
    assert searches[0] <= 2 * n + k * (6 + 2 * n)


def test_expired_deadline_keeps_the_partner_blind_flush(monkeypatch) -> None:
    import time as _time

    pf, pairs = _open_pairs(3)
    aware = _count_calls(monkeypatch, "_partner_aware_legs")
    searches = _count_calls(monkeypatch, "_search_single")
    routes: dict = {}
    out = pf._route_fallback_pairs_into(
        pairs,
        routes,
        committed=pf._fresh_committed(),
        history={},
        present=0.0,
        escape_plan={},
        deadline=_time.monotonic() - 1.0,
    )
    assert aware[0] == 0
    assert searches[0] == 2 * len(pairs)  # the pre-#6703 flush's own cost
    assert all(per[pol][1] is None for per in out for pol in ("P", "N"))

    # Same copper as the partner-blind legs routed one after another.
    pf2, pairs2 = _open_pairs(3)
    committed = pf2._fresh_committed()
    for pc in pairs2:
        legs = pf2._partner_blind_legs(
            pc, committed=committed, history={}, present=0.0, escape_plan={}
        )
        for pol in ("P", "N"):
            got = routes[(pc.key, pol)].segments
            want = legs[pol][0].route.segments
            assert [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in got] == [
                (s.x1, s.y1, s.x2, s.y2, s.layer) for s in want
            ]


def test_partner_aware_legs_keep_out_of_an_area_array_pour_neck(monkeypatch) -> None:
    # Review fix 1 (PR #6735): on 06rf the partner-aware legs, which need no
    # class-pad growth round, must still leave an area array's pour necks
    # alone.  A 5x5 BGA with a 3x3 same-pour cluster in the middle (the
    # #6673 links between its balls are keep-outs) and a declined pair whose
    # balls sit on rows A and E either side of the cluster: P's shortest leg
    # runs down a channel through the middle of the cluster, across its
    # necks (checked below by routing without the keep-outs).
    from kicad_tools.router.lattice.obstacles import LatticeKeepoutMask
    from kicad_tools.router.plane_via_sites import (
        PlaneFill,
        area_array_link_keepout_areas,
        area_array_pour_links,
        plane_fill_pullback,
    )

    pitch, cx, cy = 1.27, 15.0, 10.0
    pour, pour_net, gnd = "+1V8", 5, 3
    stack = LayerStack.two_layer()
    f_cu = stack.layer_enum_to_index(Layer.F_CU)
    legs = {(0, 2): (1, "D+"), (4, 2): (1, "D+"), (0, 3): (2, "D-"), (4, 3): (2, "D-")}
    cluster = {(r, c) for r in (1, 2, 3) for c in (1, 2, 3)}
    pads: list[Pad] = []
    for r in range(5):
        for c in range(5):
            net, name = legs.get((r, c), (pour_net, pour) if (r, c) in cluster else (gnd, "GND"))
            pads.append(
                Pad(
                    x=cx + (c - 2) * pitch,
                    y=cy + (r - 2) * pitch,
                    width=0.45,
                    height=0.45,
                    net=net,
                    net_name=name,
                    layer=Layer.F_CU,
                    ref="U1",
                    pin=f"{'ABCDE'[r]}{c + 1}",
                )
            )
    # One strict pad class far from the BGA, so the partner-aware search
    # (with its per-pad projection) really runs instead of short-cutting to
    # the partner-blind legs.
    pads.append(_pad(2.0, 2.0, 9, "HV", "HV1"))
    links, _released = area_array_pour_links(pads, {pour: [f_cu]}, stack.layer_enum_to_index)
    assert len(links) == 12
    pullback = plane_fill_pullback(PlaneFill(layer="F.Cu", clearance=0.3, min_thickness=0.25), 0.2)
    areas = area_array_link_keepout_areas(links, pullback={(pour, f_cu): pullback})

    from kicad_tools.router.lattice.geometry import seg_near_polygon

    def route(mask: LatticeKeepoutMask | None) -> list[str]:
        """Route the declined pair; return the keep-outs its legs enter."""
        pf = LatticePathfinder(
            [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)], pads, _RULES, stack
        )
        pf.set_keepouts(mask)
        pf.set_pad_class_clearances([0.15] * (len(pads) - 1) + [1.0])
        by_cell = {
            (round((p.y - cy) / pitch) + 2, round((p.x - cx) / pitch) + 2): p for p in pads[:25]
        }
        pc = CoupledConnection(
            key=("pair", 1, 2),
            pair_name="D",
            pad_p_a=by_cell[(0, 2)],
            pad_n_a=by_cell[(0, 3)],
            pad_p_b=by_cell[(4, 2)],
            pad_n_b=by_cell[(4, 3)],
            net_class=_HS,
            pitch=_PITCH,
        )
        _decline_coupled(pf, monkeypatch)
        assert pf._strict_pad_pairwise(pc, pf._fallback_items(pc)) is not None
        routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
        assert stats.routed == 2, stats
        hit: list[str] = []
        for pol in ("P", "N"):
            r = routes[(pc.key, pol)]
            for seg in r.segments:
                if seg.layer == Layer.F_CU:
                    hit += [
                        a.name
                        for a in areas
                        if seg_near_polygon(
                            (seg.x1, seg.y1), (seg.x2, seg.y2), a.polygon, seg.width / 2.0
                        )
                    ]
            for via in r.vias:
                hit += [
                    a.name
                    for a in areas
                    if seg_near_polygon(
                        (via.x, via.y), (via.x, via.y), a.polygon, via.diameter / 2.0
                    )
                ]
        return hit

    # Unguarded, the legs do cut the cluster's necks ...
    assert route(None)
    # ... and with the #6673 link keep-outs neither partner-aware leg enters one.
    assert route(LatticeKeepoutMask(areas)) == []
