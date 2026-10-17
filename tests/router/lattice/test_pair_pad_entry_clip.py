"""Coupled-pair legs terminate where they enter their own pad (issue #6959).

The lattice used to run every coupled leg on to the pad *centre*, which on
board 03 spent most of the 4.0 mm ``usb_geometry()`` launch budget inside the
pad copper.  ``clip_to_pad_entry`` trims the in-pad tail instead.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.coupled import clip_to_pad_entry
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad

from .test_coupled_pairs import _pair_board

_TW = 0.2


def _pad(width: float = 0.5, height: float = 0.5, **kw) -> Pad:
    return Pad(
        x=10.0, y=5.0, width=width, height=height, net=1, net_name="D+", layer=Layer.F_CU, **kw
    )


def test_clip_stops_at_inset_boundary() -> None:
    # Pad half-extent 0.25, inset by trace_w/2 = 0.1 -> track ends at x = 9.85.
    out = clip_to_pad_entry([(5.0, 5.0), (10.0, 5.0)], _pad(), _TW)
    assert out == [(5.0, 5.0), pytest.approx((9.85, 5.0))]


def test_clip_drops_in_pad_tail_and_dogleg() -> None:
    # Leg already runs into the pad and doglegs to the centre inside it.
    pts = [(5.0, 4.9), (9.95, 4.9), (10.0, 5.0)]
    out = clip_to_pad_entry(pts, _pad(), _TW)
    assert out[-1] == pytest.approx((9.85, 4.9))
    assert len(out) == 2


def test_clip_diagonal_entry() -> None:
    out = clip_to_pad_entry([(5.0, 0.0), (10.0, 5.0)], _pad(), _TW)
    assert out[-1] == pytest.approx((9.85, 4.85))


def test_clip_leaves_tiny_and_gateway_pads_unchanged() -> None:
    pts = [(5.0, 5.0), (10.0, 5.0)]
    assert clip_to_pad_entry(pts, _pad(width=0.15, height=0.15), _TW) == pts
    assert clip_to_pad_entry(pts, _pad(width=0.0, height=0.0), _TW) == pts


def test_clip_skips_non_cardinal_rotated_pad() -> None:
    # The AABB of a 45-degree 0.3 x 1.5 pad is far larger than its copper; a
    # cut on it would land off the pad (open circuit), so the leg is kept.
    pts = [(5.0, 5.0), (10.0, 5.0)]
    pad = _pad(width=0.3, height=1.5, rotation=45.0)
    assert clip_to_pad_entry(pts, pad, 0.2) == pts


def test_clip_off_axis_circle_entry_stays_in_copper() -> None:
    # Off-axis entry into a 0.6 mm circle pad: the AABB cut lies outside the
    # round copper, so the polyline is left unchanged.
    pts = [(5.0, 4.75), (10.0, 4.75)]
    pad = _pad(width=0.6, height=0.6, shape="circle")
    out = clip_to_pad_entry(pts, pad, 0.2)
    assert out == pts


def test_clip_leaves_leg_that_starts_inside_pad() -> None:
    pts = [(9.95, 5.0), (10.0, 5.0)]
    assert clip_to_pad_entry(pts, _pad(), _TW) == pts


def test_routed_pair_legs_end_inside_their_pads() -> None:
    pf, pc = _pair_board()
    routes, _stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_outcomes
    ends = {
        1: (pc.pad_p_a, pc.pad_p_b),
        2: (pc.pad_n_a, pc.pad_n_b),
    }
    for net, (pad_a, pad_b) in ends.items():
        segs = [s for r in routes.values() for s in r.segments if s.net == net]
        assert segs
        for pad in (pad_a, pad_b):
            inset = 0.25 - _TW / 2.0 + 1e-6
            # Exactly one segment endpoint lies in this pad's inset copper (the
            # connection), and none reaches the pad centre.
            inside = [
                p
                for s in segs
                for p in ((s.x1, s.y1), (s.x2, s.y2))
                if abs(p[0] - pad.x) <= inset and abs(p[1] - pad.y) <= inset
            ]
            assert inside, f"net {net} never reaches {pad.ref}"
            assert all(p != (pad.x, pad.y) for p in inside)


def test_clip_narrow_pad_uses_full_width_but_insets_along_leg() -> None:
    # 0.3 x 1.475 QFN pad under a 0.3 mm trace (board 03 U1.4): the leg is
    # off-centre by 0.075 and must still stop 0.15 inside the pad's far edge.
    pad = _pad(width=0.3, height=1.475)
    out = clip_to_pad_entry([(10.075, 0.0), (10.075, 5.0)], pad, 0.3)
    assert out[-1] == pytest.approx((10.075, 5.0 - 1.475 / 2.0 + 0.15))


def test_clip_leg_beside_pad_centre_line_still_cut_at_far_edge() -> None:
    # Board 03 U1.4: the leg runs 0.175 right of the pad centre (inside the
    # copper, outside the all-round inset) and must still stop short of the
    # pad centre row instead of running the full pad height.
    pad = _pad(width=0.55, height=1.475)
    out = clip_to_pad_entry([(10.175, 0.0), (10.175, 5.0), (10.0, 5.0)], pad, 0.3)
    assert out == [(10.175, 0.0), pytest.approx((10.175, 5.0 - 1.475 / 2.0 + 0.15))]
