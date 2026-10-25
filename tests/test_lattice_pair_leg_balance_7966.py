"""P/N leg balance of a lattice coupled pair at construction (issue #7966).

A coupled pair is emitted as two offsets of one centerline.  When the pad
orientations at the two ends force a net turn -- board 07's MIPI/TMDS pairs
leave side-by-side connector pads heading one way and must arrive at stacked
IC pads heading 90 degrees round -- the outside leg is longer by at least the
45-degree-step floor ``4 * p * tan(22.5 deg)`` (``1.66 p``, #7649), however
the bends are shaped.  On board 07 that was 0.597 mm, N short, on all six
pairs, which kicad-cli's ``skew_out_of_range`` rule flagged.

The lattice pair finish now moves the end stubs sideways toward the long
leg's pad, which shortens that leg's attach dogleg and lengthens the
partner's: the legs come out balanced before any skew tuner runs.  These
tests pin that on the board-07 pad pattern, with no tuner in the loop.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice import pathfinder as pathfinder_mod
from kicad_tools.router.lattice.coupled import CoupledConnection, seg_rect_dist
from kicad_tools.router.lattice.pathfinder import (
    LatticePathfinder,
    _end_balanced_centerline,
    _shift_centerline_end,
)
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import pad_half_extents

from .router.lattice.test_coupled_pairs import _HS, _PITCH, _RULES, _pad

#: The floor a 90-degree net turn taken in two 45-degree joints leaves.
_TURN_FLOOR = 4.0 * _PITCH * math.tan(math.pi / 8.0)


def _heading(a, b) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0])


# ---------------------------------------------------------------------------
# Geometry (pure)
# ---------------------------------------------------------------------------


def test_shift_centerline_end_moves_the_anchor_and_keeps_every_direction() -> None:
    # North off the pads, a 45-degree joint, then east.
    center = [(5.5, 15.0), (5.5, 14.0), (7.5, 12.0), (12.0, 12.0)]
    out = _shift_centerline_end(center, 0.15)  # left of north (+y-down frame)
    assert out is not None
    assert len(out) == len(center)
    # The anchor moved 0.15 mm along the first segment's left normal.
    assert math.dist(out[0], center[0]) == pytest.approx(0.15)
    for (a, b), (c, d) in zip(
        zip(out, out[1:], strict=False), zip(center, center[1:], strict=False), strict=True
    ):
        assert _heading(a, b) == pytest.approx(_heading(c, d))
    # Only the first joint moved; the body from the second joint on is the same.
    assert out[2:] == center[2:]


def test_shift_centerline_end_refuses_what_cannot_close() -> None:
    assert _shift_centerline_end([(0.0, 0.0), (5.0, 0.0)], 0.1) is None  # straight
    # The stub is only 0.1 mm long: a 0.2 mm shift into the 45-degree joint
    # would reverse it.
    assert _shift_centerline_end([(0.0, 0.0), (0.1, 0.0), (1.1, 1.0)], -0.2) is None


def _pads_side_by_side():
    return (
        _pad(5.0, 15.0, 1, "D+", ref="J1", width=0.4, height=1.0),
        _pad(6.0, 15.0, 2, "D-", ref="J1", width=0.4, height=1.0),
    )


def test_end_balanced_centerline_moves_toward_the_p_pad_within_the_attach_room() -> None:
    p, n = _pads_side_by_side()
    far = (_pad(20.0, 5.0, 1, "D+", ref="U1"), _pad(20.0, 6.27, 2, "D-", ref="U1"))
    center = [(5.5, 15.0), (5.5, 14.0), (7.5, 12.0), (12.0, 12.0)]
    out = _end_balanced_centerline(center, 0.2, 0.0, (p, n), far, _PITCH)
    assert out is not None
    # Toward P (x = 5.0) by the full 0.2 mm, the rest untouched.
    assert out[0] == pytest.approx((5.3, 15.0))
    assert out[2:] == center[2:]
    # (1.0 - 0.3) / 2 = 0.35 mm of room: one more micron and the P leg would
    # pass its own pad and the dogleg would have to double back.
    assert _end_balanced_centerline(center, 0.35, 0.0, (p, n), far, _PITCH) is not None
    assert _end_balanced_centerline(center, 0.351, 0.0, (p, n), far, _PITCH) is None


def test_end_balanced_centerline_leaves_a_stub_along_its_pad_axis() -> None:
    # The stub runs east, parallel to the P-N axis: a sideways shift would
    # lengthen or shorten both attach doglegs alike, so nothing is offered.
    p, n = _pads_side_by_side()
    far = (_pad(20.0, 5.0, 1, "D+", ref="U1"), _pad(20.0, 6.27, 2, "D-", ref="U1"))
    center = [(5.5, 15.0), (7.0, 15.0), (9.0, 13.0), (12.0, 13.0)]
    assert _end_balanced_centerline(center, 0.1, 0.0, (p, n), far, _PITCH) is None


# ---------------------------------------------------------------------------
# The board-07 pattern, routed by the lattice engine (no tuner)
# ---------------------------------------------------------------------------


def _board07_pattern(*, swap: bool):
    """Side-by-side connector pads -> stacked BGA balls: a 90-degree net turn.

    ``swap`` trades the P and N pad positions at both ends, which makes N the
    outside leg of the turn instead of P.
    """
    a = [(5.0, 15.0), (6.0, 15.0)]
    b = [(20.0, 5.0), (20.0, 6.27)]
    if swap:
        a.reverse()
        b.reverse()
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]
    p_a = _pad(*a[0], 1, "D+", ref="J1", width=0.4, height=1.0)
    n_a = _pad(*a[1], 2, "D-", ref="J1", width=0.4, height=1.0)
    p_b = _pad(*b[0], 1, "D+", ref="U1", width=0.45, height=0.45)
    n_b = _pad(*b[1], 2, "D-", ref="U1", width=0.45, height=0.45)
    pads = [p_a, n_a, p_b, n_b]
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
    return pf, pc, pads


def _length(route) -> float:
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)


def _route(monkeypatch: pytest.MonkeyPatch, *, swap: bool, balance: bool):
    with monkeypatch.context() as m:
        if not balance:
            m.setattr(pathfinder_mod, "_end_balanced_centerline", lambda *_a, **_k: None)
        pf, pc, pads = _board07_pattern(swap=swap)
        routes, _stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_outcomes
    return routes[(pc.key, "P")], routes[(pc.key, "N")], pads


@pytest.mark.parametrize("swap", [False, True], ids=["p-outside", "n-outside"])
def test_turn_skew_is_balanced_at_construction(monkeypatch, swap: bool) -> None:
    p_off, n_off, _pads = _route(monkeypatch, swap=swap, balance=False)
    skew_off = _length(p_off) - _length(n_off)
    # Without the end balancing the pair carries the whole turn floor (the
    # board-07 defect), on whichever leg is outside.
    assert abs(skew_off) == pytest.approx(_TURN_FLOOR, abs=1e-3)
    assert (skew_off < 0.0) == swap

    route_p, route_n, pads = _route(monkeypatch, swap=swap, balance=True)
    assert abs(_length(route_p) - _length(route_n)) < 1e-3


@pytest.mark.parametrize("swap", [False, True], ids=["p-outside", "n-outside"])
def test_balanced_legs_keep_full_clearance_to_the_partner_pads(monkeypatch, swap: bool) -> None:
    # The shift moves the short leg toward its partner's pad.  It must keep
    # the leg's full class clearance there, not just the intra-pair gap the
    # finish gate holds for the searched legs.
    route_p, route_n, pads = _route(monkeypatch, swap=swap, balance=True)
    need = _HS.clearance + _HS.trace_width / 2.0 - 1e-4
    for route, other in ((route_p, 2), (route_n, 1)):
        for pad in pads:
            if pad.net != other:
                continue
            hw, hh = pad_half_extents(pad)
            rect = (pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh)
            for s in route.segments:
                assert seg_rect_dist((s.x1, s.y1), (s.x2, s.y2), rect) >= need, (pad.ref, s)
