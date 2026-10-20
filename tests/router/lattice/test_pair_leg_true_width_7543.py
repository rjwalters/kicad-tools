"""Coupled pair legs are verified at their true width and class (#7543).

Board 06rf: once exact pad outlines (#7517) opened corridors, the coupled
search shipped a 0.275 mm USB3_RX1+ leg 0.126 mm from USB3_TX1+'s gateway via
(0.150 mm required; kicad-cli ``clearance``).  The leg attach doglegs and the
never-ship-a-short re-verification measured each leg as a board-default
0.2 mm track, 0.0375 mm narrower than the copper actually emitted.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting
from tests.router.lattice.test_coupled_pairs import _pair_board

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, via_diameter=0.6, manufacturer="jlcpcb")
_OUTLINE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
_USB3_HALF = 0.275 / 2.0
_VIA_R = 0.3


def _board(extra: list[Pad] | None = None) -> tuple[LatticePathfinder, Pad]:
    pad = Pad(
        x=2.0,
        y=5.0,
        width=0.4,
        height=0.4,
        net=1,
        net_name="USB3_RX1+",
        layer=Layer.F_CU,
        ref="J1",
        pin="B11",
    )
    pads = [pad, *(extra or [])]
    return LatticePathfinder(_OUTLINE, pads, _RULES, LayerStack.two_layer()), pad


def _via_at(gap_to_wide_leg: float) -> tuple[float, float]:
    # A foreign via beside the straight attach (y = 5.0) whose copper is
    # ``gap_to_wide_leg`` from a 0.275 mm leg's edge.
    return (3.0, 5.0 + _VIA_R + _USB3_HALF + gap_to_wide_leg)


def test_attach_rejects_foreign_via_inside_true_width_clearance() -> None:
    pf, pad = _board()
    committed = pf._fresh_committed()
    # 0.126 mm from the wide leg -- 0.1635 mm from a 0.2 mm track's edge, so
    # the old board-default probe accepted it.
    committed.add_via(_via_at(0.126), 7, 0.15)
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, 0.0) is not None
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15) is None
    assert "committed-copper" in pf._attach_blocker


def test_attach_accepts_via_clear_at_true_width() -> None:
    pf, pad = _board()
    committed = pf._fresh_committed()
    committed.add_via(_via_at(0.16), 7, 0.15)
    out = pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15)
    assert out is not None


def test_attach_foreign_pad_check_is_unchanged() -> None:
    # KNOWN GAP (deferred, not a principled exclusion): foreign pads keep the
    # static-mask check with no width surcharge, so a wide-class leg is still
    # under-measured against other nets' pads by trace_w/2 - default/2
    # (0.0375 mm on 06rf USB3).  The post-route gate and kicad-cli DRC still
    # see it.  This pins today's behaviour: a pad 0.126 mm from the wide leg's
    # edge is beyond the board-default keep-out, so it still attaches.
    edge = 5.0 + _USB3_HALF + 0.126
    foreign = Pad(
        x=3.0,
        y=edge + 0.2,
        width=0.4,
        height=0.4,
        net=7,
        net_name="USB3_TX1+",
        layer=Layer.F_CU,
        ref="U2",
        pin="B2",
    )
    pf, pad = _board([foreign])
    committed = pf._fresh_committed()
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15) is not None


def test_class_clearance_raises_the_attach_requirement() -> None:
    pf, pad = _board()
    committed = pf._fresh_committed()
    committed.add_via(_via_at(0.18), 7, 0.15)
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15) is not None
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.2) is None


def test_partner_copper_keeps_the_board_default_gate() -> None:
    # The pair's own partner copper (its gateway via) is held to the intra-pair
    # gap, not the net clearance: the true-width check skips it, so a partner
    # via that the board-default gate accepts still attaches (board 06rf
    # USB2_D's pass-under gateway).
    pf, pad = _board()
    committed = pf._fresh_committed()
    committed.add_via(_via_at(0.126), 2, 0.15)
    out = pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15)
    assert out is not None


_WIDE = NetClassRouting(
    name="USB3",
    trace_width=0.275,
    clearance=0.15,
    intra_pair_clearance=0.1,
    coupled_routing=True,
)


@pytest.mark.parametrize(
    ("via_at", "must_route"),
    [((4.025, 4.9), True), ((3.8, 6.025), False)],
)
def test_emitted_pair_legs_keep_true_width_clearance_to_foreign_via(
    via_at: tuple[float, float], must_route: bool
) -> None:
    # Goes through the finish() re-verification, which is what removes the
    # violations on the real routing path: removing its ``_pair_leg_clear``
    # call ships these fixtures at 0.113-0.121 mm (0.150 required).  Asserts the
    # emitted copper's edge gap rather than a decline reason, so a legitimate
    # re-route still passes.
    pf, pc = _pair_board()
    pc = replace(pc, net_class=_WIDE, pitch=0.375)
    pf.build()
    committed = pf._fresh_committed()
    committed.add_via(via_at, 7, 0.15)
    res, _reason = pf._route_pair_impl(pc, committed=committed, history={}, present=1.0)
    if res is None:
        # A decline (pair-leg-blocked) ships no copper, so nothing to violate.
        assert not must_route, _reason
        return
    via_r = pf.rules.via_diameter / 2.0
    for route in res.routes:
        for seg in route.segments:
            centre_dist = seg_seg_dist(via_at, via_at, (seg.x1, seg.y1), (seg.x2, seg.y2))
            gap = centre_dist - via_r - seg.width / 2.0
            assert gap >= 0.15 - 1e-6, (
                f"leg edge {gap:.3f} mm from the foreign via (0.150 required)"
            )
