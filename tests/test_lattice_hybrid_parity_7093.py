"""Via-parity search for the single-ended legs of a lattice hybrid (issue #7093).

A #6446 hybrid couples a trunk and reaches it through two single-ended legs.
Those legs had no equal-via search: board 07rf's ``TMDS_D2`` committed P/N =
0/2 vias and the end-of-run audit (#7421) reported a pair-rule failure.  Each
leg is now also re-searched under its partner's layer sequence (the #7255
:class:`LayerMirror` search), and a via-equal result replaces the pick only
when it is no worse on skew AND on length.

The fixture reuses the shape of ``test_lattice_fallback_mirror_7093``:
through-hole start pads, F.Cu end "sites", and an F.Cu wall with a gap that
one leg (but not two) can pass on F.Cu, so the plain legs come back 0/1 vias.
"""

from __future__ import annotations

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import (
    LatticePathfinder,
    _PairResult,
    _RouteResult,
)
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
_GAP_LO, _GAP_HI = 5.1, 5.9


def _pad(x: float, y: float, net: int, name: str, ref: str, **kw) -> Pad:
    th = kw.get("through_hole", False)
    return Pad(
        x=x,
        y=y,
        width=kw.get("width", 0.5),
        height=kw.get("height", 0.5),
        net=net,
        net_name=name,
        layer=kw.get("layer", Layer.F_CU),
        ref=ref,
        pin="1",
        through_hole=th,
        drill=0.3 if th else 0.0,
    )


def _fixture() -> tuple[LatticePathfinder, CoupledConnection]:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    tht = {"through_hole": True, "width": 0.7, "height": 0.7}
    p_a = _pad(3.0, 5.0, 1, "D+", "J1", **tht)
    n_a = _pad(3.0, 6.0, 2, "D-", "J1", **tht)
    p_b = _pad(27.0, 5.0, 1, "D+", "U1")
    n_b = _pad(27.0, 6.0, 2, "D-", "U1")
    walls = [
        _pad(15.0, _GAP_LO / 2.0, 9, "WALL", "WA", width=0.6, height=_GAP_LO),
        _pad(15.0, (_GAP_HI + 12.0) / 2.0, 9, "WALL", "WB", width=0.6, height=12.0 - _GAP_HI),
    ]
    pf = LatticePathfinder(outline, [p_a, n_a, p_b, n_b, *walls], _RULES, LayerStack.two_layer())
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


def _plain_legs(pf: LatticePathfinder, pc: CoupledConnection, committed):
    """The legs as :meth:`_try_hybrid_start` lays them: P alone, N against P."""
    items = {
        "P": ((pc.key, "P"), pc.pad_p_a, pc.pad_p_b, _HS),
        "N": ((pc.key, "N"), pc.pad_n_a, pc.pad_n_b, _HS),
    }
    kw = {"committed": committed, "history": {}, "present": 0.0, "escape_plan": {}}
    legs: dict[str, _RouteResult] = {}
    mark = committed.begin_trial()
    try:
        for pol in ("P", "N"):
            res, why = pf._search_hybrid_leg(items[pol], None, 0.0, **kw)
            assert res is not None, why
            legs[pol] = res
            pf._commit_single(items[pol], res, {}, committed)
    finally:
        committed.rollback_trial(mark)
    return legs


def _empty_trunk() -> _PairResult:
    return _PairResult(routes=[], runs=[], resources=set())


def test_fixture_hybrid_legs_have_unequal_vias() -> None:
    # The shape the search exists for: one leg passes the F.Cu gap with no
    # via, the other is pushed onto B.Cu and changes layer.
    pf, pc = _fixture()
    legs = _plain_legs(pf, pc, pf._fresh_committed())
    assert sorted(len(legs[p].route.vias) for p in ("P", "N")) == [0, 1]


def test_parity_legs_offer_a_via_equal_pair_and_commit_nothing() -> None:
    pf, pc = _fixture()
    committed = pf._fresh_committed()
    legs = _plain_legs(pf, pc, committed)
    before = (len(committed.vias), len(committed.via_copper))
    alts = pf._hybrid_parity_legs(
        pc,
        items={"P": (pc.pad_p_a, pc.pad_p_b), "N": (pc.pad_n_a, pc.pad_n_b)},
        trunk=_empty_trunk(),
        legs=legs,
        committed=committed,
        history={},
        present=0.0,
        escape_plan={},
        strict=None,
        floor=0.0,
        half=_HS.trace_width / 2.0,
        clr=_HS.clearance,
    )
    assert alts, "no via-equal alternative was found"
    for alt in alts:
        assert len(alt["P"].route.vias) == len(alt["N"].route.vias)
        # One leg is the original, kept as committed copper for the search.
        assert alt["P"] is legs["P"] or alt["N"] is legs["N"]
    assert (len(committed.vias), len(committed.via_copper)) == before
    assert committed._trial_depth == 0


def _pair(lp: float, ln: float, vias_p: int, vias_n: int) -> _PairResult:
    from kicad_tools.router.primitives import Segment, Via

    routes = []
    for net, length, nv in ((1, lp, vias_p), (2, ln, vias_n)):
        r = Route(net=net, net_name=str(net))
        r.segments.append(Segment(0.0, 0.0, length, 0.0, 0.2, Layer.F_CU, net, str(net)))
        for _ in range(nv):
            r.vias.append(Via(0.0, 0.0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net, str(net)))
        routes.append(r)
    return _PairResult(routes=routes, runs=[], resources=set())


def _run_selection(monkeypatch, original: _PairResult, alternative: _PairResult):
    """Drive :meth:`_try_hybrid_start` with every search stubbed out."""
    pf, pc = _fixture()
    leg = _RouteResult(
        Route(net=1, net_name="1"), [(0, [(0.0, 0.0), (1.0, 0.0)], [0.2])], [], set()
    )
    trunk = _PairResult(
        routes=[Route(net=1, net_name="1"), Route(net=2, net_name="2")],
        runs=[(1, 0, [(0.0, 0.0), (5.0, 0.0)]), (2, 0, [(0.0, 1.0), (5.0, 1.0)])],
        resources=set(),
    )

    def leg_search(self, item, *a, **k):
        # P and N end at their (virtual) sites, with 0 and 1 vias.
        virt = item[2]
        route = Route(net=item[1].net, net_name=item[1].net_name)
        if item[0][1] == "N":
            route.vias.append(object())
        return (
            _RouteResult(route, [(0, [(0.0, 0.0), (virt.x, virt.y)], [0.2])], [], set()),
            "ok",
        )

    results = iter([original, alternative])
    monkeypatch.setattr(
        LatticePathfinder, "_hybrid_anchor", lambda self, s, r: ((0, 0), (0, 0), (0, 1))
    )
    monkeypatch.setattr(LatticePathfinder, "_route_pair_impl", lambda self, *a, **k: (trunk, "ok"))
    monkeypatch.setattr(LatticePathfinder, "_gateway_pad_class_clear", lambda self, *a: True)
    monkeypatch.setattr(LatticePathfinder, "_search_hybrid_leg", leg_search)
    monkeypatch.setattr(LatticePathfinder, "_commit_single", lambda self, *a, **k: None)
    monkeypatch.setattr(
        LatticePathfinder, "_hybrid_result", lambda self, *a, **k: (next(results), "ok", (1.0, 1.0))
    )
    monkeypatch.setattr(
        LatticePathfinder, "_hybrid_parity_legs", lambda self, *a, **k: [{"P": leg, "N": leg}]
    )

    class _Start:
        site_p, site_n = (0.0, 0.0), (0.0, 1.0)

    return pf._try_hybrid_start(
        pc,
        hard_a=True,
        layer=0,
        start=_Start(),
        committed=pf._fresh_committed(),
        history={},
        present=0.0,
        escape_plan={},
        strict=None,
        floor=0.0,
        reqs=None,
        cap=100.0,
        max_vias=None,
        vias_used=0,
        half=0.1,
        clr=0.15,
    )


def test_via_equal_alternative_no_worse_on_skew_and_length_is_kept(monkeypatch) -> None:
    original = _pair(10.0, 11.0, 0, 1)
    # Measured with each 1.6 mm via: 10.0 / 12.6 (skew 2.6, total 22.6)
    # against 11.1 / 11.1 (skew 0, total 22.2).
    alternative = _pair(9.5, 9.5, 1, 1)
    pres, why, _lens, _trunk = _run_selection(monkeypatch, original, alternative)
    assert pres is alternative and why == "ok-mirrored"


def test_via_equal_alternative_trading_length_for_skew_is_not_kept(monkeypatch) -> None:
    # Element-wise, never lexicographic: lower skew does not excuse a longer pair.
    original = _pair(10.0, 11.0, 0, 1)
    alternative = _pair(11.0, 11.5, 1, 1)  # skew 0.5 < 2.6, but total 25.7 > 22.6
    pres, why, _lens, _trunk = _run_selection(monkeypatch, original, alternative)
    assert pres is original and why == "ok"


def test_via_equal_alternative_with_more_skew_is_not_kept(monkeypatch) -> None:
    original = _pair(10.0, 11.0, 0, 1)
    alternative = _pair(5.0, 9.0, 1, 1)  # total 17.2 < 22.6, but skew 4.0 > 2.6
    pres, why, _lens, _trunk = _run_selection(monkeypatch, original, alternative)
    assert pres is original and why == "ok"
