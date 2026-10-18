"""A fat-centerline turn inside a partner pad row is retried, not declined (#6839).

06b LVDS1[0] left a SOIC pad heading +x and started its 45-degree turn while
still inside the pad row, so the emitted offset leg ended 0.2156 mm from the
partner pad's copper -- under the ``intra_pair_clearance + trace_width/2``
floor that only ``finish()`` enforces.  The pair then fell back to uncoupled
with no retry, while LVDS2-4 (same geometry, turning ~1 mm later) coupled.
The search is now retried without the stubs through the offending turn.
"""

from __future__ import annotations

import kicad_tools.router.lattice.coupled as coupled
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
_PITCH = _HS.trace_width + _HS.effective_intra_pair_clearance()


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
    p_a, n_a = _pad(3.0, 5.0, 1, "D+", "J1"), _pad(3.0, 6.0, 2, "D-", "J1")
    p_b, n_b = _pad(27.0, 5.0, 1, "D+", "U1"), _pad(27.0, 6.0, 2, "D-", "U1")
    pf = LatticePathfinder(outline, [p_a, n_a, p_b, n_b], _RULES, LayerStack.two_layer())
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


def _route(pf: LatticePathfinder, pc: CoupledConnection):
    return pf._route_pair_impl(pc, committed=pf._fresh_committed(), history={}, present=0.0)


def test_unforced_pair_couples_on_first_search(monkeypatch) -> None:
    pf, pc = _pair()
    calls: list[int] = []
    real = pf._route_impl

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(pf, "_route_impl", counting)
    res, reason = _route(pf, pc)
    assert reason == "ok" and res is not None
    assert len(calls) == 1


def test_turn_inside_partner_pad_row_is_retried(monkeypatch) -> None:
    pf, pc = _pair()
    real_impl = pf._route_impl
    real_dist = coupled.seg_rect_dist
    searches: list[tuple[list, list]] = []
    bad: list[tuple[float, float]] = []

    def recording(*a, **k):
        stubs = k["stubs_override"]
        searches.append((list(stubs[0]), list(stubs[1])))
        result, why = real_impl(*a, **k)
        if result is not None and not bad:
            # The first search's centerline: poison its first turn off pad A.
            pts = result.runs[0][1]
            bad.append(next(p for p in pts[1:] if p != pts[0]))
        return result, why

    def poisoned(a, b, rect):
        # Stand-in for "this leg runs inside the partner pad row": any leg
        # segment touching the first search's turn breaks the floor.
        if bad and (abs(a[0] - bad[0][0]) < 0.4 and abs(a[1] - bad[0][1]) < 0.4):
            return 0.0
        return real_dist(a, b, rect)

    monkeypatch.setattr(pf, "_route_impl", recording)
    monkeypatch.setattr(coupled, "seg_rect_dist", poisoned)
    res, reason = _route(pf, pc)
    assert len(searches) >= 2, "the partner-pad decline must trigger a retry"
    assert reason == "ok" and res is not None
    # The retry searched strictly fewer stubs than the first search.
    assert len(searches[1][0]) + len(searches[1][1]) < len(searches[0][0]) + len(searches[0][1])
