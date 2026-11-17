"""Via parity must not buy unbounded skew or length (issue #7284).

#7103 ranked the uncoupled-fallback leg candidates via mismatch first, then
skew, then length, and #7255 added mirrored partner-leg searches that produce
equal-via candidates.  On board 03 under ``jlcpcb-tier1`` that let a 3/3-via
USB_D pair win with USB_D- 15.9 -> 20.1 mm and skew 0.54 -> 4.57 mm.  A
candidate's via-count advantage now counts only when, against the candidate
the ranking would pick without the via term, it adds at most
``_PARITY_SKEW_TOLERANCE_MM`` of skew and ``_PARITY_LENGTH_TOLERANCE_MM`` to
its longer leg, and stays within the pair's skew budget.
"""

from __future__ import annotations

import dataclasses

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


def _pair(net_class: object = _HS) -> tuple[LatticePathfinder, CoupledConnection]:
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
        net_class=net_class,
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


def _winner(monkeypatch, legs: list[tuple[float, int]], net_class: object = _HS) -> tuple[int, int]:
    """(vias_P, vias_N) kept by ``_partner_aware_legs``.

    ``legs`` are the (length, vias) search results in call order: P, then N
    plain, N guided; N, then P plain, P guided.  Later (mirrored) searches
    return each remaining entry in turn, then nothing.
    """
    pf, pc = _pair(net_class)
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
    return len(got["P"][0].route.vias), len(got["N"][0].route.vias)


def test_mirrored_parity_leg_that_blows_the_skew_is_rejected(monkeypatch) -> None:
    # Board 03 tier-1's shape: every plain / guided candidate is (2, 3), 0.5 mm
    # skew; the mirrored P leg gets N's 3 vias but detours 4.5 mm.  Fails on
    # main, which commits the (3, 3) pair at 4 mm skew.
    legs = [(15.5, 2), (16.0, 3), (16.0, 3), (16.0, 3), (15.5, 2), (15.5, 2)]
    legs += [(20.0, 3)]  # the first mirrored search: P under N's layer sequence
    assert _winner(monkeypatch, legs) == (2, 3)


def test_mirrored_parity_leg_within_tolerance_wins(monkeypatch) -> None:
    # The same search order, but the mirrored P leg costs 0.3 mm: parity wins.
    legs = [(15.5, 2), (16.0, 3), (16.0, 3), (16.0, 3), (15.5, 2), (15.5, 2)]
    legs += [(15.8, 3)]
    assert _winner(monkeypatch, legs) == (3, 3)


def test_parity_that_only_lengthens_both_legs_is_rejected(monkeypatch) -> None:
    # The N-led candidate matches vias at zero skew, but both legs are 3 mm
    # longer than the skew-best (2, 3) pair.  Fails on main.
    legs = [(10.0, 2), (10.0, 3), (10.0, 3), (13.0, 2), (13.0, 2), (13.0, 2)]
    assert _winner(monkeypatch, legs) == (2, 3)


def test_parity_past_the_pair_skew_budget_is_rejected(monkeypatch) -> None:
    # Within the skew tolerance of the (2, 3) reference (0.4 -> 0.8 mm), but
    # past the class's 0.5 mm generic budget.  Fails on main.
    legs = [(10.0, 2), (10.4, 3), (10.4, 3), (10.8, 2), (10.0, 2), (10.0, 2)]
    assert _winner(monkeypatch, legs) == (2, 3)


def test_the_skew_budget_comes_from_the_net_class(monkeypatch) -> None:
    # The same legs under a class whose protocol allows 2.5 mm (USB2 FS): the
    # 0.8 mm parity pair is inside its budget and keeps the parity win.
    usb = dataclasses.replace(_HS, pair_protocol="usb2_fs")
    assert usb.effective_skew_tolerance() == 2.5
    legs = [(10.0, 2), (10.4, 3), (10.4, 3), (10.8, 2), (10.0, 2), (10.0, 2)]
    assert _winner(monkeypatch, legs, usb) == (2, 2)


def test_free_parity_still_wins(monkeypatch) -> None:
    # Equal vias for 0.3 mm of skew and no extra length: the #7093 win stays.
    legs = [(10.0, 2), (10.0, 3), (9.7, 2), (10.0, 2), (10.0, 3), (9.7, 2)]
    assert _winner(monkeypatch, legs) == (2, 2)


def test_fallback_budget_without_a_skew_resolver() -> None:
    pf, pc = _pair(net_class=object())
    assert pf._pair_skew_budget(pc) == pfmod._PARITY_FALLBACK_SKEW_BUDGET_MM
