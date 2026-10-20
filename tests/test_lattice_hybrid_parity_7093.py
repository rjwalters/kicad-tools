"""Via-parity search for the single-ended legs of a lattice hybrid (issue #7093).

A #6446 hybrid couples a trunk and reaches it through two single-ended legs.
Those legs had no equal-via search: board 07rf's ``TMDS_D2`` committed P/N =
0/2 vias and the end-of-run audit (#7421) reported a pair-rule failure.  Each
leg is now also re-searched under its partner's layer sequence (the #7255
:class:`LayerMirror` search), and a via-equal result replaces the pick only
when it is no worse on skew AND on length.

The re-search runs AFTER the board is finished (``_hybrid_parity_pass``),
against every other route as a hard obstacle: run mid-negotiation it moved
the copper board 06rf's later pairs routed around, so two pairs that had
equal vias lost them and a #6489 pad-growth re-negotiation fired.

The fixture reuses the shape of ``test_lattice_fallback_mirror_7093``:
through-hole start pads, F.Cu end "sites", and an F.Cu wall with a gap that
one leg (but not two) can pass on F.Cu, so the plain legs come back 0/1 vias.
"""

from __future__ import annotations

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import (
    LatticePathfinder,
    _HybridParityCtx,
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


def _ctx(pf, pc, pres: _PairResult, legs=None, trunk=None) -> _HybridParityCtx:
    return _HybridParityCtx(
        pc=pc,
        items={"P": (pc.pad_p_a, pc.pad_p_b), "N": (pc.pad_n_a, pc.pad_n_b)},
        trunk=trunk or _empty_trunk(),
        legs=legs or {},
        pres=pres,
        escape_plan={},
        strict=None,
        floor=0.0,
        reqs=None,
        cap=100.0,
        max_vias=None,
        vias_used=0,
        half=_HS.trace_width / 2.0,
        clr=_HS.clearance,
    )


def _run_selection(monkeypatch, original: _PairResult, alternative: _PairResult):
    """Drive :meth:`_hybrid_parity_pick` with the searches stubbed out."""
    pf, pc = _fixture()
    leg = _RouteResult(
        Route(net=1, net_name="1"), [(0, [(0.0, 0.0), (1.0, 0.0)], [0.2])], [], set()
    )
    monkeypatch.setattr(
        LatticePathfinder, "_hybrid_parity_legs", lambda self, *a, **k: [{"P": leg, "N": leg}]
    )
    monkeypatch.setattr(
        LatticePathfinder, "_hybrid_result", lambda self, *a, **k: (alternative, "ok", (1.0, 1.0))
    )
    return pf._hybrid_parity_pick(_ctx(pf, pc, original), committed=pf._fresh_committed())


def test_via_equal_alternative_no_worse_on_skew_and_length_is_kept(monkeypatch) -> None:
    original = _pair(10.0, 11.0, 0, 1)
    # Measured with each 1.6 mm via: 10.0 / 12.6 (skew 2.6, total 22.6)
    # against 11.1 / 11.1 (skew 0, total 22.2).
    alternative = _pair(9.5, 9.5, 1, 1)
    pres, why, _lens = _run_selection(monkeypatch, original, alternative)
    assert pres is alternative and why == "ok-mirrored"


def test_via_equal_alternative_trading_length_for_skew_is_not_kept(monkeypatch) -> None:
    # Element-wise, never lexicographic: lower skew does not excuse a longer pair.
    original = _pair(10.0, 11.0, 0, 1)
    alternative = _pair(11.0, 11.5, 1, 1)  # skew 0.5 < 2.6, but total 25.7 > 22.6
    pres, why, _lens = _run_selection(monkeypatch, original, alternative)
    assert pres is original and why == "ok"


def test_via_equal_alternative_with_more_skew_is_not_kept(monkeypatch) -> None:
    original = _pair(10.0, 11.0, 0, 1)
    alternative = _pair(5.0, 9.0, 1, 1)  # total 17.2 < 22.6, but skew 4.0 > 2.6
    pres, why, _lens = _run_selection(monkeypatch, original, alternative)
    assert pres is original and why == "ok"


# --- no replacement mid-negotiation -----------------------------------------


def test_negotiation_records_the_unequal_pick_and_never_mirrors(monkeypatch) -> None:
    # Board 06rf: a mirrored leg committed mid-negotiation is copper every
    # later pair routes around -- USB3_RX1/TX1 lost via parity and a #6489
    # pad-growth re-negotiation fired.  So ``_try_hybrid_start`` returns the
    # pick unchanged and only records it for the post-route pass.
    pf, pc = _fixture()
    original = _pair(10.0, 11.0, 0, 1)
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

    def no_mirror(self, *a, **k):
        raise AssertionError("mirrored legs searched mid-negotiation")

    monkeypatch.setattr(
        LatticePathfinder, "_hybrid_anchor", lambda self, s, r: ((0, 0), (0, 0), (0, 1))
    )
    monkeypatch.setattr(LatticePathfinder, "_route_pair_impl", lambda self, *a, **k: (trunk, "ok"))
    monkeypatch.setattr(LatticePathfinder, "_gateway_pad_class_clear", lambda self, *a: True)
    monkeypatch.setattr(LatticePathfinder, "_search_hybrid_leg", leg_search)
    monkeypatch.setattr(LatticePathfinder, "_commit_single", lambda self, *a, **k: None)
    monkeypatch.setattr(
        LatticePathfinder, "_hybrid_result", lambda self, *a, **k: (original, "ok", (1.0, 1.0))
    )
    monkeypatch.setattr(LatticePathfinder, "_hybrid_parity_legs", no_mirror)

    class _Start:
        site_p, site_n = (0.0, 0.0), (0.0, 1.0)

    pres, why, _lens, _trunk = pf._try_hybrid_start(
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
    assert pres is original and why == "ok"
    assert [c.pres for c in pf._hybrid_parity_ctx] == [original]


# --- the post-route pass ------------------------------------------------------


def _site_trunk() -> _PairResult:
    """A zero-length trunk: one degenerate run per net, no copper."""
    return _PairResult(
        routes=[Route(net=1, net_name="D+"), Route(net=2, net_name="D-")],
        runs=[(1, 0, [(29.0, 1.0), (29.0, 1.0)]), (2, 0, [(29.0, 11.0), (29.0, 11.0)])],
        resources=set(),
    )


def _finished_board(*, foreign: list | None = None):
    """A finished board: the fixture's 0/1-via hybrid plus one foreign route.

    Lengths are measured as a constant so the dominance gate always passes
    and only the board's copper decides whether a mirrored leg fits.
    """
    from kicad_tools.router.primitives import Segment

    pf, pc = _fixture()
    pf.pair_length_measure = lambda _route: 1.0
    legs = _plain_legs(pf, pc, pf._fresh_committed())
    trunk = _site_trunk()
    pres, why, _lens = pf._hybrid_result(
        pc,
        pc.pad_p_a,
        pc.pad_n_a,
        trunk,
        legs,
        cap=100.0,
        max_vias=None,
        vias_used=0,
        reqs=None,
        half=_HS.trace_width / 2.0,
        clr=_HS.clearance,
    )
    assert pres is not None, why
    assert [len(r.vias) for r in pres.routes] == [0, 1]
    other = Route(net=9, net_name="X")
    for x1, y1, x2, y2, layer in foreign or []:
        other.segments.append(Segment(x1, y1, x2, y2, 0.2, layer, 9, "X"))
    pf._hybrid_parity_ctx = [_ctx(pf, pc, pres, legs=legs, trunk=trunk)]
    routes = {(pc.key, "P"): pres.routes[0], (pc.key, "N"): pres.routes[1], "other": other}
    return pf, pc, routes, other


def test_pass_replaces_only_the_hybrids_own_legs() -> None:
    pf, pc, routes, other = _finished_board()
    other_before = list(other.segments)
    pf._hybrid_parity_pass(routes, [], [pc])
    assert [len(routes[(pc.key, p)].vias) for p in ("P", "N")] == [1, 1]
    assert routes["other"] is other and other.segments == other_before
    assert "0/1 -> 1/1" in pf.hybrid_parity_log[pc.key]
    assert pf._hybrid_parity_ctx == []


def _boxed_start() -> list:
    """Foreign B.Cu copper boxing P's through-hole start pad."""
    from kicad_tools.router.layers import Layer

    box = [(2.2, 4.2), (3.8, 4.2), (3.8, 5.55), (2.2, 5.55), (2.2, 4.2)]
    return [(a[0], a[1], b[0], b[1], Layer.B_CU) for a, b in zip(box, box[1:], strict=False)]


def test_pass_rejects_a_mirrored_leg_that_foreign_copper_blocks(monkeypatch) -> None:
    # The reject path: foreign B.Cu copper boxes P's through-hole start, so
    # the mirrored leg (B.Cu first, like N's) cannot leave it.  The finished
    # board's copper is a hard obstacle, never something the leg pushes.
    # (The #7580 joint search, which can leave on F.Cu first, is switched off
    # here; it has its own test below.)
    monkeypatch.setattr(LatticePathfinder, "_hybrid_joint_legs", lambda self, *a, **k: [])
    pf, pc, routes, other = _finished_board(foreign=_boxed_start())
    pick = (routes[(pc.key, "P")], routes[(pc.key, "N")])
    pf._hybrid_parity_pass(routes, [], [pc])
    assert (routes[(pc.key, "P")], routes[(pc.key, "N")]) == pick
    assert routes[(pc.key, "P")] is pick[0] and routes[(pc.key, "N")] is pick[1]
    assert "kept" in pf.hybrid_parity_log[pc.key]


def test_pass_skips_a_pick_that_is_not_the_emitted_copper(monkeypatch) -> None:
    # A pick from a rolled-back pass (a #6489 growth round, the #6444
    # gateway rollback) is not what the board carries: it is never offered.
    pf, pc, routes, _other = _finished_board()
    routes[(pc.key, "P")] = Route(net=1, net_name="D+")

    def no_search(self, *a, **k):
        raise AssertionError("searched a superseded pick")

    monkeypatch.setattr(LatticePathfinder, "_hybrid_parity_pick", no_search)
    pf._hybrid_parity_pass(routes, [], [pc])
    assert pf.hybrid_parity_log == {}


def test_pass_is_off_when_hybrid_parity_is_disabled() -> None:
    pf, pc, routes, _other = _finished_board()
    pick = routes[(pc.key, "P")]
    pf.hybrid_parity = False
    pf._hybrid_parity_pass(routes, [], [pc])
    assert routes[(pc.key, "P")] is pick and pf.hybrid_parity_log == {}


def _stub_negotiation(monkeypatch, pf, pc, routes, *, deadline_hit: bool):
    from kicad_tools.router.lattice.pathfinder import LatticeNegotiationStats

    ctxs = list(pf._hybrid_parity_ctx)

    def fake(self, connections, **_kw):
        self._hybrid_parity_ctx = list(ctxs)  # recorded during negotiation
        stats = LatticeNegotiationStats(
            iterations=1,
            converged=True,
            routed=2,
            total=2,
            lattice_builds=1,
            deadline_hit=deadline_hit,
        )
        return routes, stats

    monkeypatch.setattr(LatticePathfinder, "_route_netset_gateways", fake)


def test_route_netset_runs_the_pass_after_negotiation(monkeypatch) -> None:
    pf, pc, routes, _other = _finished_board()
    _stub_negotiation(monkeypatch, pf, pc, routes, deadline_hit=False)
    out, _stats = pf.route_netset([], coupled=[pc])
    assert [len(out[(pc.key, p)].vias) for p in ("P", "N")] == [1, 1]
    assert pf._hybrid_parity_ctx == []


def test_route_netset_skips_the_pass_on_a_deadline_hit(monkeypatch) -> None:
    pf, pc, routes, _other = _finished_board()
    pick = routes[(pc.key, "P")]
    _stub_negotiation(monkeypatch, pf, pc, routes, deadline_hit=True)
    out, _stats = pf.route_netset([], coupled=[pc])
    assert out[(pc.key, "P")] is pick and pf.hybrid_parity_log == {}
    assert pf._hybrid_parity_ctx == []


# --- issue #7580: both legs re-searched together, and the failure report -----


def test_joint_legs_find_a_via_equal_pair_the_mirror_cannot() -> None:
    # Foreign B.Cu copper boxes P's start, so P cannot copy N's B.Cu-first
    # sequence while N's leg stays put.  Lifting BOTH legs lets P leave on
    # F.Cu and drop to B.Cu past the box, and N follow on the same layers.
    pf, pc, routes, _other = _finished_board(foreign=_boxed_start())
    ctx = pf._hybrid_parity_ctx[0]
    committed = pf._fresh_committed()
    pf._commit_route_copper(routes["other"], None, committed)
    before = (len(committed.vias), len(committed.via_copper))
    mirrored = pf._hybrid_parity_legs(
        pc,
        items=ctx.items,
        trunk=ctx.trunk,
        legs=ctx.legs,
        committed=committed,
        history={},
        present=0.0,
        escape_plan={},
        strict=None,
        floor=0.0,
        half=ctx.half,
        clr=ctx.clr,
    )
    assert mirrored == []
    joint = pf._hybrid_joint_legs(ctx, committed=committed)
    assert joint, "no via-equal joint pair was found"
    for alt in joint:
        assert len(alt["P"].route.vias) == len(alt["N"].route.vias) > 0
        # Both legs are new copper: neither is the pick's.
        assert alt["P"] is not ctx.legs["P"] and alt["N"] is not ctx.legs["N"]
    assert (len(committed.vias), len(committed.via_copper)) == before
    assert committed._trial_depth == 0


def test_route_netset_commits_a_joint_pair_when_the_mirror_is_blocked(monkeypatch) -> None:
    # End to end through ``route_netset`` (negotiation stubbed to the
    # finished board): the boxed start defeats every mirrored leg, the joint
    # search clears the pair to equal vias, and no other copper moves.
    pf, pc, routes, other = _finished_board(foreign=_boxed_start())
    other_before = list(other.segments)
    _stub_negotiation(monkeypatch, pf, pc, routes, deadline_hit=False)
    out, _stats = pf.route_netset([], coupled=[pc])
    vias = [len(out[(pc.key, p)].vias) for p in ("P", "N")]
    assert vias[0] == vias[1] > 0
    assert "both legs re-searched together" in pf.hybrid_parity_log[pc.key]
    assert out["other"] is other and other.segments == other_before


def test_route_netset_reports_a_rejected_via_equal_pair_as_a_pair_rule_failure(
    monkeypatch,
) -> None:
    # Real lengths: every via-equal pair costs skew AND length against the
    # 0/1 pick, so the pick is kept -- and the log says it is a pair-rule
    # failure and how much worse the best via-equal pair was.
    pf, pc, routes, _other = _finished_board()
    pf.pair_length_measure = None
    pick = (routes[(pc.key, "P")], routes[(pc.key, "N")])
    _stub_negotiation(monkeypatch, pf, pc, routes, deadline_hit=False)
    out, _stats = pf.route_netset([], coupled=[pc])
    assert out[(pc.key, "P")] is pick[0] and out[(pc.key, "N")] is pick[1]
    note = pf.hybrid_parity_log[pc.key]
    assert note.startswith("vias P/N=0/1: pair-rule failure, kept")
    assert "best rejected: vias" in note
    assert "none exists against the trunk alone either" in note


def test_failure_note_names_the_nets_blocking_a_trunk_alone_pair() -> None:
    # Against the trunk alone a via-equal pair fits (the pick is measured
    # long, so it qualifies as no worse).  On the "finished board" a foreign
    # net X runs a bar across both layers that every such pair must cross,
    # and the report names X as what is in its way.
    from kicad_tools.router.lattice.pathfinder import _default_pair_length
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Segment

    pf, pc, routes, _other = _finished_board()
    ctx = pf._hybrid_parity_ctx[0]
    pick = (routes[(pc.key, "P")], routes[(pc.key, "N")])

    def measure(route):
        if route is pick[0] or route is pick[1]:
            return 1000.0 if route is pick[0] else 1010.0
        return _default_pair_length(route)

    pf.pair_length_measure = measure
    bar = Route(net=9, net_name="X")
    for layer in (Layer.F_CU, Layer.B_CU):
        bar.segments.append(Segment(10.0, -1.0, 10.0, 13.0, 0.2, layer, 9, "X"))
    committed = pf._fresh_committed()
    pf._commit_route_copper(bar, None, committed)
    found = pf._hybrid_parity_blockers(ctx, committed=committed, net_names={9: "X"})
    assert found is not None
    vias, (skew, total), names = found
    assert vias[0] == vias[1] and names == ["X"]
    assert (skew, total) <= (10.0, 2010.0)
    note = pf._hybrid_parity_failure_note(ctx, (0, 1), committed=committed, net_names={9: "X"})
    assert note.startswith("vias P/N=0/1: pair-rule failure, kept")
    assert note.endswith("blocked on the finished board by: X")
