"""The fat (coupled-pair) lattice search prices net signed turning (Issue #7721).

A pair's legs sit ``pitch`` apart, so the outer leg is longer by about
``pitch x net signed turning``.  Two same-direction U-turns (06b's LVDS pairs)
therefore build skew that a balanced pair of turns does not.  The fat search
carries ``(heading, winding)`` in its state and charges ``|winding|`` at the
goal; single-net searches keep the plain ``(node, layer)`` state.

The pricing is opt-in (``LatticePathfinder.pair_turn_pricing`` /
``KCT_LATTICE_PAIR_TURN_PRICING=1``) and off by default, because on the
regression boards it still makes some pairs worse (PR #7726).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import (
    _NO_HEADING,
    PAIR_TURN_PRICING_ENV,
    LatticePathfinder,
    _heading_index,
    _poly_turning,
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


def _wall(x0: float, y0: float, x1: float, y1: float, net: int) -> Pad:
    return Pad(
        x=(x0 + x1) / 2.0, y=(y0 + y1) / 2.0, width=x1 - x0, height=y1 - y0,
        net=net, net_name=f"WALL{net}", layer=Layer.F_CU, ref=f"X{net}", pin="1",
    )  # fmt: skip


def _pair(
    *,
    wall: bool = False,
    a: tuple[float, float] = (3.0, 5.0),
    b: tuple[float, float] = (27.0, 5.0),
    walls: tuple[tuple[float, float, float, float], ...] = (),
    height: float = 12.0,
    pricing: bool = True,
) -> tuple[LatticePathfinder, CoupledConnection]:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, height), (0.0, height)]
    p_a, n_a = _pad(a[0], a[1], 1, "D+", "J1"), _pad(a[0], a[1] + 1.0, 2, "D-", "J1")
    p_b, n_b = _pad(b[0], b[1], 1, "D+", "U1"), _pad(b[0], b[1] + 1.0, 2, "D-", "U1")
    pads = [p_a, n_a, p_b, n_b]
    if wall:
        pads.append(_wall(14.5, 2.0, 15.5, 9.0, 9))
    pads.extend(_wall(*w, 20 + i) for i, w in enumerate(walls))
    pf = LatticePathfinder(outline, pads, _RULES, LayerStack.two_layer())
    pf.pair_turn_pricing = pricing
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
    return _poly_turning(points)


def _leg_skew(res) -> float:
    """``|L_p - L_n|`` of an emitted pair."""

    def length(pts: list[tuple[float, float]]) -> float:
        return sum(
            ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            for a, b in zip(pts, pts[1:], strict=False)
        )

    return abs(length(res.runs[0][2]) - length(res.runs[1][2]))


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


def test_turn_pricing_is_off_by_default_and_opted_in_by_env(monkeypatch) -> None:
    outline = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    monkeypatch.delenv(PAIR_TURN_PRICING_ENV, raising=False)
    assert LatticePathfinder(outline, [], _RULES).pair_turn_pricing is False
    monkeypatch.setenv(PAIR_TURN_PRICING_ENV, "1")
    assert LatticePathfinder(outline, [], _RULES).pair_turn_pricing is True


def test_poly_turning_negates_on_reversal() -> None:
    poly = [(0.0, 0.0), (1.0, 0.0), (2.0, 1.0), (2.0, 2.0)]
    assert _poly_turning(poly) == 2
    assert _poly_turning(list(reversed(poly))) == -2


# The discriminating case: wall W1 forces a U-turn (+4 of winding) and a short
# fence just east of U1 forces the pair to enter U1 heading east, so the pair
# must wrap U1's fence.  W2 makes the pair crest just under U1's level, which
# puts the wrap OVER the fence (a second same-direction U-turn, winding 8) a
# little shorter than the balanced wrap UNDER it (winding 0).  Length alone
# picks the spiral, and its outer leg is ~pitch x 2*pi longer.
_SPIRAL = (
    (0.0, 4.0, 22.0, 4.6),  # W1
    (9.0, 9.5, 9.5, 11.5),  # fence east of U1
    (13.0, 4.6, 13.6, 9.8),  # W2
)


@pytest.mark.parametrize("pricing", [False, True])
def test_turn_pricing_picks_the_balanced_wrap_over_the_shorter_spiral(pricing: bool) -> None:
    pf, pc = _pair(a=(3.0, 1.0), b=(8.0, 10.0), walls=_SPIRAL, height=20.0, pricing=pricing)
    res, reason = pf._route_pair_impl(pc, committed=pf._fresh_committed(), history={}, present=0.0)
    assert reason == "ok" and res is not None
    if pricing:
        assert _leg_skew(res) < 0.5
    else:
        # Without the pricing the shortest centerline is the spiral.
        assert _leg_skew(res) > 2.0


@pytest.mark.parametrize(
    "name",
    [
        "tests.router.lattice.test_coupled_pairs:"
        "test_pair_declines_when_emitted_leg_violates_pairwise_pad_requirement",
        "tests.router.lattice.test_coupled_pairs:"
        "test_relocate_pair_anchor_moves_blocked_midpoint_off_the_part",
        "tests.router.lattice.test_pair_leg_true_width_7543:"
        "test_nudged_centerline_rescues_a_pair_a_hair_inside_a_pad_class",
    ],
)
def test_pricing_keeps_offsettable_centerlines(name: str, monkeypatch) -> None:
    # PR #7726 review: with heading in the state the search re-entered nodes
    # (180-degree spurs, 135-degree hooks, closed loops) to launder winding,
    # and the emitted centerlines failed with ``pair-offset-degenerate``.
    # These pair scenarios must keep their outcome with the pricing on.
    import importlib
    import inspect

    module, func = name.split(":")
    test = getattr(importlib.import_module(module), func)
    monkeypatch.setenv(PAIR_TURN_PRICING_ENV, "1")
    if "monkeypatch" in inspect.signature(test).parameters:
        test(monkeypatch)
    else:
        test()
