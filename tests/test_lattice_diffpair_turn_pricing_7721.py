"""The fat (coupled-pair) lattice search prices net signed turning (Issue #7721).

A pair's legs sit ``pitch`` apart, so the outer leg is longer by about
``pitch x net signed turning``.  Two same-direction U-turns (06b's LVDS pairs)
therefore build skew that a balanced pair of turns does not.  The fat search
carries ``(heading, winding)`` in its state and charges ``|winding|`` at the
goal; single-net searches keep the plain ``(node, layer)`` state.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import (
    _NO_HEADING,
    LatticePathfinder,
    _heading_index,
    _signed_turn,
)
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
        x=x, y=y, width=0.5, height=0.5, net=net, net_name=name, layer=Layer.F_CU,
        ref=ref, pin="1",
    )  # fmt: skip


def _pair(*, wall: bool = False) -> tuple[LatticePathfinder, CoupledConnection]:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    p_a, n_a = _pad(3.0, 5.0, 1, "D+", "J1"), _pad(3.0, 6.0, 2, "D-", "J1")
    p_b, n_b = _pad(27.0, 5.0, 1, "D+", "U1"), _pad(27.0, 6.0, 2, "D-", "U1")
    pads = [p_a, n_a, p_b, n_b]
    if wall:
        pads.append(
            Pad(
                x=15.0,
                y=5.5,
                width=1.0,
                height=7.0,
                net=9,
                net_name="WALL",
                layer=Layer.F_CU,
                ref="X1",
                pin="1",
            )  # fmt: skip
        )
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


def _net_turning(points: list[tuple[float, float]]) -> int:
    """Net signed turning of a polyline, in 45-degree units."""
    total = 0
    prev = None
    for a, b in zip(points, points[1:], strict=False):
        hd = _heading_index(a, b)
        if hd is None:
            continue
        if prev is not None:
            total += _signed_turn(prev, hd)
        prev = hd
    return total


def test_heading_index_is_octilinear_and_signed_turn_is_signed() -> None:
    assert _heading_index((0, 0), (1, 0)) == 0
    assert _heading_index((0, 0), (1, 1)) == 1
    assert _heading_index((0, 0), (0, 1)) == 2
    assert _heading_index((0, 0), (-1, 0)) == 4
    assert _heading_index((0, 0), (1, -1)) == 7
    assert _heading_index((0, 0), (0, 0)) is None
    # Left turns are positive, right turns negative, a reversal is +4.
    assert _signed_turn(0, 2) == 2
    assert _signed_turn(0, 6) == -2
    assert _signed_turn(7, 1) == 2
    assert _signed_turn(0, 4) == 4
    assert _signed_turn(_NO_HEADING, 3) == 0


def test_net_turning_separates_same_direction_from_balanced_turns() -> None:
    same_direction = [(0, 0), (4, 0), (4, 2), (0, 2)]  # east, north, west: two left turns
    balanced = [(0, 0), (4, 0), (4, 2), (8, 2)]  # east, north, east: left then right
    assert _net_turning(same_direction) == 4
    assert _net_turning(balanced) == 0


@pytest.mark.parametrize("wall", [False, True])
def test_fat_search_returns_balanced_centerline(wall: bool) -> None:
    pf, pc = _pair(wall=wall)
    res, reason = pf._route_pair_impl(pc, committed=pf._fresh_committed(), history={}, present=0.0)
    assert reason == "ok" and res is not None
    # The leg leaves its pad northward and enters the far pad southward, so
    # the endpoints fix a net half turn (4 units); a full extra turn (8) would
    # be the same-direction U-turn pattern the pricing exists to avoid.
    pts = res.runs[0][2]
    assert abs(_net_turning(pts)) <= 4


def test_single_net_search_is_unaffected() -> None:
    pf, _pc = _pair()
    pads = [p for p in pf.pads if p.net == 1]
    res, reason = pf._route_impl(
        pads[0], pads[1], None, committed=pf._fresh_committed(), history={}, present=0.0
    )
    assert reason == "ok" and res is not None
