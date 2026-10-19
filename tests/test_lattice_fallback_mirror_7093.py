"""Mirrored partner-leg search for uncoupled diff-pair fallback legs (issue #7093).

Board 03's ``USB_D`` J1->U2 leg falls back to two single-ended legs.  J1's
pads are through-hole, so one leg leaves on F.Cu and reaches the F.Cu ESD pad
with no via, while the other has to leave on another layer and change once:
every candidate the #6703 / #7103 ranking saw was ``(0, 1)`` or ``(1, 0)``
vias, so ranking alone could not give the pair equal via counts.  The fallback
now also searches each leg constrained to its partner's layer sequence
(:class:`LayerMirror`), which gives the ranking a via-parity candidate.

The fixture reproduces that shape: through-hole start pads, F.Cu end pads, and
an F.Cu-only wall with a gap that one leg (but not two) can pass on F.Cu.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.geometry import seg_seg_dist
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

_WALL_X = 15.0
# Gap in the F.Cu wall, centred between the legs: one leg fits through it on
# F.Cu, the other is pushed onto B.Cu (measured: main commits (0, 1) vias).
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


def _gapped_pair() -> tuple[LatticePathfinder, CoupledConnection]:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    tht = {"through_hole": True, "width": 0.7, "height": 0.7}
    p_a = _pad(3.0, 5.0, 1, "D+", "J1", **tht)
    n_a = _pad(3.0, 6.0, 2, "D-", "J1", **tht)
    p_b = _pad(27.0, 5.0, 1, "D+", "U1")
    n_b = _pad(27.0, 6.0, 2, "D-", "U1")
    walls = [
        _pad(_WALL_X, _GAP_LO / 2.0, 9, "WALL", "WA", width=0.6, height=_GAP_LO),
        _pad(_WALL_X, (_GAP_HI + 12.0) / 2.0, 9, "WALL", "WB", width=0.6, height=12.0 - _GAP_HI),
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


def _decline_coupled(pf: LatticePathfinder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "_route_pair_impl", lambda *a, **k: (None, "pair-no-path"))


def _ungate_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let via parity win at any skew / length cost (the pre-#7284 ranking)."""
    from kicad_tools.router.lattice import pathfinder

    monkeypatch.setattr(pathfinder, "_PARITY_SKEW_TOLERANCE_MM", float("inf"))
    monkeypatch.setattr(pathfinder, "_PARITY_LENGTH_TOLERANCE_MM", float("inf"))
    monkeypatch.setattr(LatticePathfinder, "_pair_skew_budget", lambda self, pc: float("inf"))


def test_fallback_legs_with_asymmetric_layer_changes_get_equal_vias(monkeypatch) -> None:
    # Fails without the mirrored search: every plain / guided candidate is
    # (0, 1) or (1, 0) vias, and the pair commits (0, 1).  The parity gate
    # (#7284) is lifted here because this fixture's cheapest mirrored pair
    # costs skew and length (see the next test); this one checks the mirror.
    pf, pc = _gapped_pair()
    _decline_coupled(pf, monkeypatch)
    _ungate_parity(monkeypatch)
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key] == "uncoupled-fallback:pair-no-path"
    assert stats.routed == 2 and stats.total == 2, stats
    route_p, route_n = routes[(pc.key, "P")], routes[(pc.key, "N")]
    assert len(route_p.vias) == len(route_n.vias) == 1
    assert pf.pair_via_mismatch == {}
    # Never a short: the legs keep the full copper gap from each other.
    gap = _RULES.trace_width + _RULES.trace_clearance
    for a in route_p.segments:
        for b in route_n.segments:
            if a.layer == b.layer:
                d = seg_seg_dist((a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2))
                assert d >= gap - 1e-6


def test_mirrored_parity_that_costs_skew_and_length_is_not_kept(monkeypatch) -> None:
    # Issue #7284.  The fixture's best mirrored (1, 1) pair measures 26.760 /
    # 28.720 mm (skew 1.960) against the (0, 1) pair's 25.325 / 25.931 mm
    # (skew 0.606): 1.35 mm more skew and 2.8 mm on the longer leg, past the
    # gate's tolerances and the class's 0.5 mm skew budget, so the
    # asymmetric pair stays.
    pf, pc = _gapped_pair()
    _decline_coupled(pf, monkeypatch)
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert stats.routed == 2 and stats.total == 2, stats
    assert sorted(pf.pair_via_mismatch[pc.key]) == [0, 1]


def test_fixture_candidates_are_all_asymmetric_without_the_mirror(monkeypatch) -> None:
    # The fixture really has board 03's shape: with no mirrored candidate the
    # best leg pair has unequal via counts.
    pf, pc = _gapped_pair()
    _decline_coupled(pf, monkeypatch)
    monkeypatch.setattr(LatticePathfinder, "_mirrored_candidates", lambda self, *a, **k: ([], []))
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert sorted(pf.pair_via_mismatch[pc.key]) == [0, 1]


def test_fallback_with_the_mirror_is_deterministic(monkeypatch) -> None:
    def run() -> list[tuple]:
        pf, pc = _gapped_pair()
        _decline_coupled(pf, monkeypatch)
        routes, _ = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
        return [
            (s.layer, round(s.x1, 4), round(s.y1, 4), round(s.x2, 4), round(s.y2, 4))
            for pol in ("P", "N")
            for s in routes[(pc.key, pol)].segments
        ]

    assert run() == run()


def _result(runs: list[tuple[int, list[tuple[float, float]]]]):
    from kicad_tools.router.lattice.pathfinder import _RouteResult

    return _RouteResult(
        Route(net=1, net_name="D+"),
        [(layer, pts, [0.2] * (len(pts) - 1)) for layer, pts in runs],
        [],
        set(),
    )


def test_layer_sequence_skips_a_stacked_via_landing_and_merges_repeats() -> None:
    from kicad_tools.router.lattice.pathfinder import LayerMirror, _layer_sequence, _mirror_of

    res = _result(
        [
            (0, [(0.0, 0.0), (1.0, 0.0)]),
            (1, [(1.0, 0.0)]),  # stacked via: no copper on layer 1
            (3, [(1.0, 0.0), (2.0, 0.0)]),
            (3, [(2.0, 0.0), (3.0, 0.0)]),
        ]
    )
    assert _layer_sequence(res) == (0, 3)
    assert _mirror_of(res) == LayerMirror(0, 3, frozenset({0, 3}), False)


def test_planar_partner_gives_a_planar_mirror() -> None:
    from kicad_tools.router.lattice.pathfinder import LayerMirror, _mirror_of

    res = _result([(1, [(0.0, 0.0), (1.0, 0.0)])])
    assert _mirror_of(res) == LayerMirror(1, 1, frozenset({1}), True)


def test_mirrored_search_follows_the_partner_layer_sequence() -> None:
    from kicad_tools.router.lattice.pathfinder import LayerMirror, _layer_sequence

    pf, pc = _gapped_pair()
    item = ((pc.key, "P"), pc.pad_p_a, pc.pad_p_b, _HS)
    kw = {"committed": pf._fresh_committed(), "history": {}, "present": 0.0, "escape_plan": {}}
    # Unconstrained, P takes the F.Cu gap with no via.
    plain, _ = pf._search_single(item, **kw)
    assert plain is not None and _layer_sequence(plain) == (0,)
    # Mirroring a B.Cu -> F.Cu partner: leave on B.Cu, one via, arrive on F.Cu.
    mirrored, _ = pf._search_single(item, mirror=LayerMirror(1, 0, frozenset({0, 1}), False), **kw)
    assert mirrored is not None
    assert _layer_sequence(mirrored) == (1, 0)
    assert len(mirrored.route.vias) == 1
    # A planar B.Cu mirror cannot reach the F.Cu end pad: declined, never a via.
    none, reason = pf._search_single(item, mirror=LayerMirror(1, 1, frozenset({1}), True), **kw)
    assert none is None and reason == "mirror-layer-end"


def test_no_mirrored_search_when_a_candidate_already_has_parity(monkeypatch) -> None:
    pf, pc = _gapped_pair()
    monkeypatch.setattr(LatticePathfinder, "_commit_single", lambda self, *a, **k: None)
    calls: list[object] = []

    def search(*a, **k):
        calls.append(a)
        return None, "x"

    class _Leg:
        def __init__(self, vias: int) -> None:
            self.route = self
            self.vias = [object()] * vias
            self.runs = [(0, [(0.0, 0.0), (1.0, 0.0)], [0.2])]

    equal = {"P": (_Leg(1), ""), "N": (_Leg(1), "")}
    unequal = {"P": (_Leg(0), ""), "N": (_Leg(1), "")}
    committed = pf._fresh_committed()
    items = pf._fallback_items(pc)
    got = pf._mirrored_candidates(pc, [unequal, equal], items, search, committed)
    assert got == ([], []) and calls == []
    # All asymmetric: both legs are re-searched (guided, then unguided).
    pf._mirrored_candidates(pc, [unequal], items, search, committed)
    assert len(calls) == 4
