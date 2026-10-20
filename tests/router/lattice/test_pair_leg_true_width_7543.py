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

from kicad_tools.router.lattice.coupled import seg_rect_dist
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


def test_attach_rejects_foreign_pad_inside_true_width_clearance() -> None:
    # Issue #7597 (was the #7543 known gap): a pad 0.126 mm from the wide
    # leg's edge is beyond the board-default keep-out but inside the 0.150 mm
    # class clearance, so the attach must now decline it -- and still accept
    # one that is clear at the true width.
    def attach(gap: float):
        foreign = Pad(
            x=3.0,
            y=5.0 + _USB3_HALF + gap + 0.2,
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
        return pf, pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15)

    pf, out = attach(0.126)
    assert out is None
    assert "pad" in pf._attach_blocker
    assert attach(0.16)[1] is not None


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


@pytest.mark.parametrize("pad_at", [(3.8, 4.85), (3.8, 6.15)])
def test_emitted_pair_legs_keep_true_width_clearance_to_foreign_pad(
    pad_at: tuple[float, float],
) -> None:
    # Issue #7597: the finish() leg loop measures foreign PADS at the leg's
    # true width and class too.  Without the surcharge these fixtures ship a
    # 0.275 mm leg 0.125 mm from the pad (0.150 required); with it the pair
    # re-routes clear or declines.  Asserts the emitted copper's edge gap, so
    # a legitimate re-route still passes.
    foreign = Pad(
        x=pad_at[0],
        y=pad_at[1],
        width=0.4,
        height=0.4,
        net=7,
        net_name="OTHER",
        layer=Layer.F_CU,
        ref="U2",
        pin="1",
    )
    pf, pc = _pair_board([foreign])
    pc = replace(pc, net_class=_WIDE, pitch=0.375)
    pf.build()
    res, _reason = pf._route_pair_impl(pc, committed=pf._fresh_committed(), history={}, present=1.0)
    if res is None:
        # A decline (pair-leg-blocked) ships no copper, so nothing to violate.
        return
    for route in res.routes:
        for seg in route.segments:
            rect = (pad_at[0] - 0.2, pad_at[1] - 0.2, pad_at[0] + 0.2, pad_at[1] + 0.2)
            gap = seg_rect_dist((seg.x1, seg.y1), (seg.x2, seg.y2), rect) - seg.width / 2.0
            assert gap >= 0.15 - 1e-6, f"leg edge {gap:.3f} mm from the foreign pad"


@pytest.mark.parametrize("pad_y", [4.80, 4.75, 6.25])
def test_emitted_pair_legs_keep_the_foreign_pads_own_class_clearance(pad_y: float) -> None:
    # Issue #7597 review: a foreign pad whose router net class (0.25 mm) is
    # above the leg's (0.15 mm) must be held to ``max(leg, pad class)``.  The
    # static pad rects deliberately omit router pad classes (#6449) and
    # ``class_pad_repair`` never moves pair legs, so without the
    # ``_pair_pad_class_clear`` call these fixtures ship legs 0.175-0.225 mm
    # from the pad.  A decline ships no copper, so it also passes.
    foreign = Pad(
        x=3.8,
        y=pad_y,
        width=0.4,
        height=0.4,
        net=7,
        net_name="OTHER",
        layer=Layer.F_CU,
        ref="U2",
        pin="1",
    )
    pf, pc = _pair_board([foreign])
    pc = replace(pc, net_class=_WIDE, pitch=0.375)
    pf.build()
    pf.set_pad_class_clearances([0.25 if p.net == 7 else 0.15 for p in pf.pads])
    res, _reason = pf._route_pair_impl(pc, committed=pf._fresh_committed(), history={}, present=1.0)
    if res is None:
        return
    rect = (3.6, pad_y - 0.2, 4.0, pad_y + 0.2)
    for route in res.routes:
        for seg in route.segments:
            gap = seg_rect_dist((seg.x1, seg.y1), (seg.x2, seg.y2), rect) - seg.width / 2.0
            assert gap >= 0.25 - 1e-6, (
                f"leg edge {gap:.3f} mm from the foreign pad (0.250 required)"
            )


def test_attach_rejects_foreign_pad_inside_its_own_class_clearance() -> None:
    # The attach holds the pad side too: a pad 0.2 mm from the wide leg's edge
    # clears the leg's 0.150 mm class but not its own 0.25 mm class.
    foreign = Pad(
        x=3.0,
        y=5.0 + _USB3_HALF + 0.2 + 0.2,
        width=0.4,
        height=0.4,
        net=7,
        net_name="OTHER",
        layer=Layer.F_CU,
        ref="U2",
        pin="B2",
    )
    pf, pad = _board([foreign])
    committed = pf._fresh_committed()
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15) is not None
    pf.set_pad_class_clearances([0.25 if p.net == 7 else 0.15 for p in pf.pads])
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15) is None
    assert "pad" in pf._attach_blocker


def test_partner_pads_are_exempt_from_the_pad_class_check() -> None:
    # Both pair nets are "self" for the pad-class check: the partner's pads sit
    # inside the pair envelope and are held to the intra-pair gap by the
    # partner-pad floor instead.  A partner pad 0.2 mm from the leg's edge with
    # a 0.25 mm class must still attach; holding it to its class (exempting
    # only the leg's own net) would decline every tightly pitched pair.
    partner = Pad(
        x=3.0,
        y=5.0 + _USB3_HALF + 0.2 + 0.2,
        width=0.4,
        height=0.4,
        net=2,
        net_name="USB3_RX1-",
        layer=Layer.F_CU,
        ref="J1",
        pin="B10",
    )
    pf, pad = _board([partner])
    pf.set_pad_class_clearances([0.25 if p.net == 2 else 0.15 for p in pf.pads])
    committed = pf._fresh_committed()
    out = pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1, 2}, committed, _USB3_HALF, 0.15)
    assert out is not None, pf._attach_blocker


def test_shift_centerline_segment_preserves_directions() -> None:
    from kicad_tools.router.lattice.pathfinder import _shift_centerline_segment

    center = [(0.0, 0.0), (0.0, 1.0), (1.0, 2.0), (5.0, 2.0), (6.0, 1.0), (6.0, 0.0)]
    out = _shift_centerline_segment(center, 2, -0.1)  # body (1,2)-(5,2) moves down
    assert out is not None
    assert out[0] == center[0] and out[-1] == center[-1]
    assert out[2] == pytest.approx((0.9, 1.9)) and out[3] == pytest.approx((5.1, 1.9))
    for (a, b), (c, d) in zip(
        zip(center, center[1:], strict=False), zip(out, out[1:], strict=False), strict=False
    ):
        u = (b[0] - a[0], b[1] - a[1])
        v = (d[0] - c[0], d[1] - c[1])
        assert u[0] * v[1] - u[1] * v[0] == pytest.approx(0.0, abs=1e-9)  # parallel
        assert u[0] * v[0] + u[1] * v[1] > 0.0  # same sense
    # End segments carry the midpoint stubs and never move.
    assert _shift_centerline_segment(center, 0, 0.1) is None
    assert _shift_centerline_segment(center, len(center) - 2, 0.1) is None
    # A shift that reverses a neighbouring segment is refused.
    assert _shift_centerline_segment(center, 2, -1.5) is None


def test_nudged_centerline_rescues_a_pair_a_hair_inside_a_pad_class(monkeypatch) -> None:
    # Issue #7597 (board 06 USB2_D): the lattice body row put the D- leg
    # 0.187 mm from J1.B8/B9/B12 (0.200 mm class) although the corridor had
    # room.  Here the body row puts the N leg 0.18 mm from a 0.25 mm-class
    # pad.  Without the sideways nudge the pair declines; with it the body
    # shifts 0.1 mm and ships clear of the class, still at exactly the pitch.
    from kicad_tools.router.lattice import pathfinder as pfm
    from tests.router.lattice.test_coupled_pairs import _PITCH, _pad

    default_steps = pfm._PAIR_NUDGE_STEPS

    def route(steps: tuple[float, ...]):
        monkeypatch.setattr(pfm, "_PAIR_NUDGE_STEPS", steps)
        foreign = _pad(15.0, 7.08, 7, "OTHER", ref="U9")
        pf, pc = _pair_board([foreign])
        pf.build()
        pf.set_pad_class_clearances([0.25 if p.net == 7 else 0.15 for p in pf.pads])
        return pf._route_pair_impl(pc, committed=pf._fresh_committed(), history={}, present=1.0)

    res, reason = route(())
    assert res is None and reason == "pair-leg-blocked"
    res, reason = route(default_steps)
    assert res is not None, reason
    rect = (14.75, 7.08 - 0.25, 15.25, 7.08 + 0.25)
    segs = [s for r in res.routes for s in r.segments]
    gap = min(seg_rect_dist((s.x1, s.y1), (s.x2, s.y2), rect) - s.width / 2.0 for s in segs)
    assert gap >= 0.25 - 1e-6
    sep = min(
        seg_seg_dist((a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2))
        for a in res.routes[0].segments
        for b in res.routes[1].segments
    )
    assert sep == pytest.approx(_PITCH, abs=1e-6)
