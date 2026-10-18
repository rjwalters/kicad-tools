"""Length-aware choice among via-parity fallback leg pairs (issue #7093).

#7255's mirrored partner-leg search gave board 03's ``USB_D`` J1->U2 leg equal
via counts, and #7291 bounded what parity may cost.  Two things still made
the pair longer than it had to be:

* the mirrored leg was searched under a partner the router laid with no
  regard for it, so the partner could stay the long way round.  A second
  round re-searches that partner under the mirrored leg (once per
  first-round parity candidate);
* among the parity candidates that pass #7291's gate, the skew-first ranking
  preferred the lowest skew even when every one of them was inside the pair's
  skew budget.  Within the budget the shortest pair now wins.

These are stubbed-search tests in the #6703 ``_candidate_winner`` style.
"""

from __future__ import annotations

from kicad_tools.router.lattice import pathfinder as pfmod
from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
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


def _pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _pair() -> tuple[LatticePathfinder, CoupledConnection]:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    p_a = _pad(3.0, 5.0, 1, "D+", "J1")
    n_a = _pad(3.0, 6.0, 2, "D-", "J1")
    p_b = _pad(27.0, 5.0, 1, "D+", "U1")
    n_b = _pad(27.0, 6.0, 2, "D-", "U1")
    pf = LatticePathfinder(outline, [p_a, n_a, p_b, n_b], _RULES, LayerStack.two_layer())
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=p_a,
        pad_n_a=n_a,
        pad_p_b=p_b,
        pad_n_b=n_b,
        net_class=_HS,
        pitch=0.3,
    )
    return pf, pc


class _Leg:
    """A routed leg reduced to what the candidate ranking reads."""

    def __init__(self, length: float, vias: int) -> None:
        self.length = length
        self.route = self
        self.vias = [object()] * vias
        self.runs = [(0, [(0.0, 0.0), (length, 0.0)], [0.2])]


def _winner(monkeypatch, legs: list[tuple[float, int]]) -> tuple[float, float, int, int]:
    """``(len_P, len_N, vias_P, vias_N)`` kept by ``_partner_aware_legs``.

    ``legs`` are the (length, vias) search results in call order: P, then N
    plain, N guided; N, then P plain, P guided; then the mirrored searches
    (guided first, unguided only when the guided one finds nothing).  Once
    the list runs out every search finds nothing.
    """
    pf, pc = _pair()
    pf.pair_length_measure = lambda route: route.length
    results = iter(_Leg(*lg) for lg in legs)
    monkeypatch.setattr(
        LatticePathfinder, "_search_single", lambda self, item, **kw: (next(results, None), "")
    )
    monkeypatch.setattr(LatticePathfinder, "_partner_guide", lambda self, res, pc: object())
    monkeypatch.setattr(LatticePathfinder, "_legs_shortfalls", lambda self, pc, lg: 0)
    monkeypatch.setattr(LatticePathfinder, "_commit_single", lambda self, *a, **k: None)
    monkeypatch.setattr(pfmod, "_mirror_of", lambda res: object())
    got, _ = pf._partner_aware_legs(
        pc, committed=pf._fresh_committed(), history={}, present=0.0, escape_plan={}
    )
    p_r, n_r = got["P"][0], got["N"][0]
    return p_r.length, n_r.length, len(p_r.vias), len(n_r.vias)


# Board 03 arm C's ``USB_D[0]`` (J1.A6 -> U2.3), as measured on every flush
# (lengths include the via barrels): the plain legs are (0, 1) / (1, 0) vias.
_ARM_C_PLAIN = [(4.077, 0), (6.025, 1), (5.41, 1), (4.077, 0), (6.025, 1), (5.41, 1)]
# First mirrored round, in search order (see ``_mirrored_candidates``).
_ARM_C_ROUND_1 = [(5.742, 1), (14.249, 0), (7.342, 1), (14.249, 0), (5.742, 1), (7.342, 1)]


def test_second_round_re_searches_the_partner_under_the_mirrored_leg(monkeypatch) -> None:
    # Without the second round the best pair is P 5.742 / N 6.025 (0.283 mm
    # skew; its 6.025 leg fails #7291's length gate, so it wins on skew).
    # Re-searching N under the mirrored P leg finds 5.41: inside the gate, so
    # the parity pair is both shorter and kept.  Fails on main (N 6.025).
    round_2 = [(5.41, 1), (4.077, 0), (5.41, 1), (4.077, 0), (5.41, 1), (5.41, 1)]
    got = _winner(monkeypatch, _ARM_C_PLAIN + _ARM_C_ROUND_1 + round_2)
    assert got == (5.742, 5.41, 1, 1)


def test_without_a_second_round_hit_the_first_round_pick_stands(monkeypatch) -> None:
    # The second round finds nothing: the #7291 choice is unchanged.
    got = _winner(monkeypatch, _ARM_C_PLAIN + _ARM_C_ROUND_1)
    assert got == (5.742, 6.025, 1, 1)


def test_shortest_parity_pair_within_the_skew_budget_wins(monkeypatch) -> None:
    # Reference (2, 3): 10.0 / 10.4, skew 0.4.  Both mirrored parity pairs
    # pass the gate inside the 0.5 mm budget: P mirrored 10.35 / 10.4 (skew
    # 0.05, 20.75 mm) and N mirrored 10.0 / 10.3 (skew 0.3, 20.3 mm).  The
    # shorter wins.  Fails on main, which keeps the 0.05 mm-skew pair.
    legs = [(10.0, 2), (10.4, 3), (10.4, 3), (10.4, 3), (10.0, 2), (10.0, 2)]
    legs += [(10.35, 3), (10.3, 2)]
    assert _winner(monkeypatch, legs) == (10.0, 10.3, 2, 2)


def test_skew_still_decides_past_the_budget(monkeypatch) -> None:
    # Reference (2, 3): 10.0 / 11.0, skew 1.0, already over the 0.5 mm
    # budget.  Both parity pairs are admitted as no worse than it (0.6 and
    # 0.9 mm), but neither is inside the budget, so skew still decides and
    # the longer, better-matched pair is kept.
    legs = [(10.0, 2), (11.0, 3), (11.0, 3), (11.0, 3), (10.0, 2), (10.0, 2)]
    legs += [(10.4, 3), (10.9, 2)]
    assert _winner(monkeypatch, legs) == (10.4, 11.0, 3, 3)


def test_second_round_is_bounded_by_the_first_round_parity_hits(monkeypatch) -> None:
    pf, pc = _pair()
    monkeypatch.setattr(LatticePathfinder, "_commit_single", lambda self, *a, **k: None)
    monkeypatch.setattr(LatticePathfinder, "_partner_guide", lambda self, res, pc: object())
    monkeypatch.setattr(pfmod, "_mirror_of", lambda res: object())
    calls: list[str] = []

    def search(pol, guide=None, mirror=None):
        calls.append(pol)
        return _Leg(5.0, 1), ""

    unequal = {"P": (_Leg(5.0, 0), ""), "N": (_Leg(5.0, 1), "")}
    got = pf._mirrored_candidates(
        pc, [unequal], pf._fallback_items(pc), search, pf._fresh_committed()
    )
    # Round 1: P under N -> (1, 1) parity; N under P -> (0, 1).  Round 2: only
    # the parity hit re-searches its partner (N, under the new P leg).
    assert calls == ["P", "N", "N"]
    assert [pfmod.LatticePathfinder._pair_via_counts(c) for c in got] == [(1, 1), (0, 1), (1, 1)]
