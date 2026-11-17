"""Layer-substituted mirrors for crossed uncoupled fallback legs (issue #7093).

Board 03 tier-1's ``USB_D`` J1.A6 -> U2.3 leg falls back to two single-ended
legs whose pads are polarity-crossed: D+ is the right-hand J1 pad and the
left-hand U2 pad.  The legs must cross, so one of them changes layer.  The
fallback measured, on every flush:

* P 5.573 mm leaving the J1 through-hole pad on In2.Cu, one via up to F.Cu;
  N 4.077 mm on F.Cu with no via -- ``(1, 0)`` vias, the committed pick;
* a ``(1, 1)`` pair whose guided N leg copies P's In2.Cu -> F.Cu sequence but
  has to go round P on In2.Cu: 8.939 mm, so #7284's gate ranks it with the
  asymmetric pair's mismatch.

Two things kept the pair at ``(1, 0)``:

* that inadmissible ``(1, 1)`` pair made :meth:`_mirrored_candidates` return
  early, as if parity were already settled, so no mirrored leg was searched;
* the exact mirror keeps the moved leg on its partner's layers, which is
  exactly the 8.939 mm detour.  Copying the partner's single layer change
  from another layer (B.Cu -> F.Cu) crosses under P instead: N 5.657 mm,
  ``(1, 1)`` vias, skew 0.084 mm.

These are stubbed-search tests in the #6703 ``_candidate_winner`` style; the
stub answers by (polarity, guided, mirror), not by call order.
"""

from __future__ import annotations

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import LatticePathfinder, LayerMirror
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
_F, _IN1, _IN2, _B = 0, 1, 2, 3


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
    p_b = _pad(27.0, 6.0, 1, "D+", "U1")
    n_b = _pad(27.0, 5.0, 2, "D-", "U1")
    pf = LatticePathfinder(
        outline, [p_a, n_a, p_b, n_b], _RULES, LayerStack.four_layer_all_signal()
    )
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
    """A routed leg reduced to what the ranking and the mirror read."""

    def __init__(self, length: float, layers: tuple[int, ...]) -> None:
        self.length = length
        self.route = self
        self.vias = [object()] * (len(layers) - 1)
        self.runs = [(layer, [(0.0, 0.0), (1.0, 0.0)], [0.2]) for layer in layers]


# Board 03 tier-1 ``USB_D[0]``, measured (lengths include the via barrels).
_P_VIA = _Leg(5.573, (_IN2, _F))
_N_FLAT = _Leg(4.077, (_F,))
_P_LONG = _Leg(8.939, (_IN2, _F))
_N_LONG = _Leg(8.939, (_IN2, _F))  # the exact mirror of _P_VIA: round P on In2.Cu
_P_ROUND = _Leg(13.352, (_F,))  # P mirrored to N's planar F.Cu
_N_UNDER = _Leg(5.657, (_B, _F))  # B.Cu -> F.Cu: crosses under P's In2.Cu run


def _stub(
    monkeypatch,
    table: dict[tuple[str, bool, LayerMirror | None], _Leg],
) -> list[tuple[str, bool, LayerMirror | None]]:
    calls: list[tuple[str, bool, LayerMirror | None]] = []

    def search(self, item, guide=None, mirror=None, **kw):
        key = (item[0][1], guide is not None, mirror)
        calls.append(key)
        return table.get(key), "no-path"

    monkeypatch.setattr(LatticePathfinder, "_search_single", search)
    monkeypatch.setattr(LatticePathfinder, "_partner_guide", lambda self, res, pc: object())
    monkeypatch.setattr(LatticePathfinder, "_legs_shortfalls", lambda self, pc, lg: 0)
    monkeypatch.setattr(LatticePathfinder, "_commit_single", lambda self, *a, **k: None)
    return calls


def _winner(table) -> tuple[float, float, int, int]:
    pf, pc = _pair()
    pf.pair_length_measure = lambda route: route.length
    got, _ = pf._partner_aware_legs(
        pc, committed=pf._fresh_committed(), history={}, present=0.0, escape_plan={}
    )
    p_r, n_r = got["P"][0], got["N"][0]
    return p_r.length, n_r.length, len(p_r.vias), len(n_r.vias)


def _board03_table() -> dict:
    return {
        ("P", False, None): _P_VIA,
        ("N", False, None): _N_FLAT,
        ("P", True, None): _P_LONG,
        ("N", True, None): _N_LONG,
        ("P", True, LayerMirror(_F, _F, frozenset({_F}), True)): _P_ROUND,
        ("N", True, LayerMirror(_IN2, _F, frozenset({_IN2, _F}), False)): _N_LONG,
        ("N", True, LayerMirror(_B, _F, frozenset({_B, _F}), False)): _N_UNDER,
    }


def test_crossed_legs_get_equal_vias_from_a_layer_substituted_mirror(monkeypatch) -> None:
    # Fails on main: the inadmissible (1, 1) base pair (P 5.573 / N 8.939)
    # skips the mirror, and main commits P 5.573 / N 4.077, (1, 0) vias.
    _stub(monkeypatch, _board03_table())
    assert _winner(_board03_table()) == (5.573, 5.657, 1, 1)


def test_an_inadmissible_parity_pair_no_longer_skips_the_exact_mirror(monkeypatch) -> None:
    # Only the early return is at stake: the exact mirror itself finds an
    # admissible N leg, so no substituted search is needed or made.
    table = _board03_table()
    table[("N", True, LayerMirror(_IN2, _F, frozenset({_IN2, _F}), False))] = _Leg(5.6, (_IN2, _F))
    calls = _stub(monkeypatch, table)
    assert _winner(table) == (5.573, 5.6, 1, 1)
    substituted = [c for c in calls if c[2] is not None and c[2] not in _exact_mirrors()]
    assert substituted == []


def _exact_mirrors() -> set[LayerMirror]:
    return {
        LayerMirror(_F, _F, frozenset({_F}), True),
        LayerMirror(_IN2, _F, frozenset({_IN2, _F}), False),
    }


def test_substituted_mirrors_copy_one_layer_change_onto_each_other_layer(monkeypatch) -> None:
    # The partner's In2.Cu -> F.Cu change is re-offered as L -> F.Cu and
    # In2.Cu -> L for every other copper layer L (In1.Cu, B.Cu), guided
    # first and unguided on a miss; a planar partner offers none.
    calls = _stub(monkeypatch, {k: v for k, v in _board03_table().items() if k[2] is None})
    _winner({})
    subs = {c[2] for c in calls if c[2] is not None and c[2] not in _exact_mirrors()}
    assert subs == {
        LayerMirror(_IN1, _F, frozenset({_IN1, _F}), False),
        LayerMirror(_B, _F, frozenset({_B, _F}), False),
        LayerMirror(_IN2, _IN1, frozenset({_IN2, _IN1}), False),
        LayerMirror(_IN2, _B, frozenset({_IN2, _B}), False),
    }
    # Only the N leg is moved: the P leg's partners are planar (N 4.077 mm),
    # or (P-led guided) already equal.
    assert {c[0] for c in calls if c[2] in subs} == {"N"}


def test_substituted_parity_is_still_gated_on_skew_and_length(monkeypatch) -> None:
    # A substituted (1, 1) pair that costs too much skew is ranked with the
    # asymmetric pair's mismatch, exactly as an exact-mirror one is (#7284).
    # (No guided P leg here: a P 8.939 / N 8.939 exact-mirror pair would win
    # on skew alone, with or without the substitution.)
    table = _board03_table()
    del table[("P", True, None)]
    table[("N", True, LayerMirror(_B, _F, frozenset({_B, _F}), False))] = _Leg(9.5, (_B, _F))
    _stub(monkeypatch, table)
    assert _winner(table) == (5.573, 4.077, 1, 0)


def test_an_admissible_base_parity_pair_still_skips_every_mirror(monkeypatch) -> None:
    table = _board03_table()
    table[("N", True, None)] = _Leg(5.6, (_IN2, _F))
    calls = _stub(monkeypatch, table)
    assert _winner(table) == (5.573, 5.6, 1, 1)
    assert all(c[2] is None for c in calls)
