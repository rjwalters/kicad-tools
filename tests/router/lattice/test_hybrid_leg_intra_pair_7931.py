"""Hybrid legs escape the virtual site at the intra-pair gap (issue #7931).

The hybrid trunk commits both nets' coupled runs, one pitch apart, before the
legs are searched.  A leg's virtual end sits ON its net's trunk start, so the
partner's trunk is one pitch away from every stub the leg can try -- closer
than the leg's full class clearance whenever the pair's intra-pair gap is the
tighter of the two.  Without the relaxation every stub is rejected and the leg
declines ``pad-escape-end`` (board 03 USB_D J1.A6->U2.3).
"""

from __future__ import annotations

from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pathfinder import LatticePathfinder, PhysicalHistory
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
_HALF = _HS.trace_width / 2.0
_PITCH = _HS.trace_width + _HS.effective_intra_pair_clearance()
_OUTLINE = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
_SITE = (10.0, 6.0)


def _pad(x: float, y: float, net: int, size: float) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=size,
        height=size,
        net=net,
        net_name="D+" if net == 1 else "D-",
        layer=Layer.F_CU,
        ref="U1",
        pin="1",
    )


def _committed() -> CommittedCopper:
    c = CommittedCopper(
        2,
        trace_half=_HALF,
        clearance=0.15,
        via_radius=0.3,
        via_via_gap=0.5,
        same_net_via_gap=0.25,
    )
    return c


def test_partner_is_relaxed_only_near_the_site_and_only_for_the_partner() -> None:
    gap = _PITCH - 2 * _HALF
    c = _committed()
    c.add_run(0, [(10.0, 6.0 + _PITCH), (14.0, 6.0 + _PITCH)], 2, _HALF, 0.15)  # partner
    c.add_run(0, [(10.0, 6.0 - _PITCH), (14.0, 6.0 - _PITCH)], 3, _HALF, 0.15)  # foreign
    a, b = (10.2, 6.0), (10.6, 6.0)
    assert not c.seg_clear(a, b, 0, 1, _HALF, 0.15)
    c.intra_pair = (2, _SITE, 1.0, gap)
    # The partner trace is now held to the intra-pair gap, but the foreign
    # net one pitch on the other side is still held to the full clearance.
    assert not c.seg_clear(a, b, 0, 1, _HALF, 0.15)
    c2 = _committed()
    c2.add_run(0, [(10.0, 6.0 + _PITCH), (14.0, 6.0 + _PITCH)], 2, _HALF, 0.15)
    c2.intra_pair = (2, _SITE, 1.0, gap)
    assert c2.seg_clear(a, b, 0, 1, _HALF, 0.15)
    assert c2.node_clear((10.4, 6.0), 0, 1, _HALF, 0.15)
    # Beyond the attach reach of the site the full clearance applies again.
    assert not c2.seg_clear((12.0, 6.0), (12.4, 6.0), 0, 1, _HALF, 0.15)
    # A closer-than-intra-pair approach is still refused.
    assert not c2.seg_clear((10.2, 6.1), (10.6, 6.1), 0, 1, _HALF, 0.15)


def test_leg_escapes_a_virtual_site_one_pitch_from_the_partner_trunk() -> None:
    real = _pad(20.0, 6.0, 1, 0.4)
    virt = Pad(
        x=_SITE[0],
        y=_SITE[1],
        width=0.0,
        height=0.0,
        net=1,
        net_name="D+",
        layer=Layer.F_CU,
        ref="U1",
        pin="1",
    )
    pf = LatticePathfinder(
        _OUTLINE, [real, _pad(25.0, 9.0, 3, 0.4)], _RULES, LayerStack.two_layer()
    )
    item = (("pair", "P"), real, virt, _HS)

    def leg(relax: bool):
        committed = pf._fresh_committed()
        # The partner's trunk body, one pitch from the site, running away.
        committed.add_run(0, [(10.0, 6.0 + _PITCH), (16.0, 6.0 + _PITCH)], 2, _HALF, 0.15)
        if relax:
            committed.intra_pair = (2, _SITE, 2.0, _PITCH - 2 * _HALF)
        return pf._search_hybrid_leg(
            item,
            None,
            0.0,
            committed=committed,
            history=PhysicalHistory(),
            present=1.0,
            escape_plan={},
        )

    res, why = leg(False)
    assert res is None and "escape" in why, why
    res, why = leg(True)
    assert res is not None, why
