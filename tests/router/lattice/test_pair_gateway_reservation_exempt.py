"""A pair's own reservations exempt the pair's nets (issue #6956).

Board 03's ``USB_D[1]`` (U2.4 -> R4.1) could not couple through its In2.Cu
gateway: two static keep-outs that block every net -- the pair's own nets
included -- sat on the only routes its fans and attaches can take:

* ``pair-escape-via:VBUS:U2.5`` (#6697), reserved between the pair's own
  pads for the sealed VBUS pad;
* ``plane-via-site:GND:U2.2`` (#6401), the GND pour-pad via site under U2's
  body.

Policy: a reservation inside a declared pair's gateway envelope at that
pair's own footprint (and every #6697 escape site the pair caused) exempts
the pair's two nets and stays a hard keep-out for every other net.  The
gateway decline line also counts every combination's reason, so the VBUS
blocker would have been named from the start.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairplan.gateway import MAX_GATEWAY_SETBACK_MM
from kicad_tools.router.plane_via_sites import (
    _capsule_polygon,
    _regular_polygon,
    exempt_pair_in_gateway_envelope,
    pair_gateway_envelope_contains,
)
from kicad_tools.router.primitives import Pad
from tests.router.lattice.test_coupled_pairs import (
    _chain_autorouter,
    _chain_pads,
    _sealed_vbus_pads,
)
from tests.router.lattice.test_pair_gateway_pass_under_6836 import _dual_row_board

_ALL = frozenset({0, 1})


def _sot(x: float, y: float, net: int, pin: str, ref: str = "U2") -> Pad:
    # Board 03 U2 (SOT-23-6 at (146.5, 68.0), rotation 270): the east row.
    return Pad(
        x=x, y=y, width=1.325, height=0.6, net=net, net_name=str(net), layer=Layer.F_CU, ref=ref,
        pin=pin,
    )  # fmt: skip


def _disc(x: float, y: float, r: float, name: str) -> KeepoutArea:
    return KeepoutArea(
        polygon=_regular_polygon(x, y, r),
        layers=_ALL,
        blocks_tracks=True,
        blocks_vias=True,
        name=name,
    )


# -- the envelope --------------------------------------------------------------

_U2_4 = _sot(145.55, 69.1375, 1, "4")  # USB_D+
_U2_6 = _sot(147.45, 69.1375, 2, "6")  # USB_D-


def test_board03_reservations_lie_in_the_pair_envelope() -> None:
    kw = {"setback": MAX_GATEWAY_SETBACK_MM}
    # The #6401 GND U2.2 via (under U2's body) and the #6697 VBUS U2.5 site.
    assert pair_gateway_envelope_contains(_U2_4, _U2_6, (146.47, 67.85), **kw)
    assert pair_gateway_envelope_contains(_U2_4, _U2_6, (146.5, 70.3), **kw)
    # Beyond the largest gateway setback, or past the pads along the axis.
    assert not pair_gateway_envelope_contains(_U2_4, _U2_6, (146.5, 72.5), **kw)
    assert not pair_gateway_envelope_contains(_U2_4, _U2_6, (149.0, 69.1375), **kw)
    # Not the pair's own footprint: no envelope.
    other = _sot(147.45, 69.1375, 2, "1", ref="R4")
    assert not pair_gateway_envelope_contains(_U2_4, other, (146.5, 70.3), **kw)


def test_envelope_exempts_the_pair_and_still_blocks_every_other_net() -> None:
    site = _disc(146.47, 67.85, 0.45, "plane-via-site:GND:U2.2")
    fill = _disc(146.47, 67.85, 0.6, "plane-via-fill:GND:U2.2")
    stub = KeepoutArea(
        polygon=_capsule_polygon(146.5, 66.86, 146.47, 67.85, 0.25),
        layers=frozenset({0}),
        blocks_tracks=True,
        blocks_vias=True,
        name="plane-via-stub:GND:U2.2",
    )
    far = _disc(120.0, 60.0, 0.45, "plane-via-site:GND:U9.1")
    areas, names = exempt_pair_in_gateway_envelope(
        [site, fill, stub, far], _U2_4, _U2_6, {1, 2}, setback=MAX_GATEWAY_SETBACK_MM
    )
    assert names == ["plane-via-site:GND:U2.2", "plane-via-fill:GND:U2.2"]
    assert areas[0].exempt == areas[1].exempt == frozenset({1, 2})
    # The stub is the pad's own way to its via; a far site is another part's.
    assert areas[2] is stub and areas[3] is far
    mask = LatticeKeepoutMask(areas)
    a, b = (146.0, 67.85), (147.0, 67.85)
    # In2-style layer 1 (no stub): the pair crosses the exempted discs.
    assert not mask.segment_blocked(a, b, 1, 1, 0.1)
    assert not mask.segment_blocked(a, b, 1, 2, 0.1)
    # The pad layer still carries the un-exempted stub.
    assert mask.segment_blocked(a, b, 0, 1, 0.1)
    # A foreign signal (net 7) next to the exempted site is still kept out.
    assert mask.segment_blocked(a, b, 1, 7, 0.1)
    assert mask.via_blocked((146.47, 67.85), 7, 0.3)
    # Idempotent.
    again, names2 = exempt_pair_in_gateway_envelope(
        areas, _U2_4, _U2_6, {1, 2}, setback=MAX_GATEWAY_SETBACK_MM
    )
    assert names2 == [] and all(x is y for x, y in zip(again, areas, strict=True))


# -- the gateway, end to end ---------------------------------------------------


def _blocked_dual_row(exempt: bool):
    # A reserved site under J1's rows, on the pass-under body's attach.
    pf, pc = _dual_row_board()
    areas = [_disc(24.6, 5.5, 0.5, "plane-via-site:GND:J1.X")]
    if exempt:
        areas, names = exempt_pair_in_gateway_envelope(
            areas, pc.pad_p_b, pc.pad_n_b, {1, 2}, setback=MAX_GATEWAY_SETBACK_MM
        )
        assert names == ["plane-via-site:GND:J1.X"]
    pf.set_keepouts(LatticeKeepoutMask(areas))
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    return pf, pc


def test_reservation_in_the_envelope_no_longer_blocks_the_pair_gateway() -> None:
    pf, pc = _blocked_dual_row(exempt=True)
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_gateway_log
    assert "gateway B.Cu" in pf.pair_gateway_log[pc.key]
    assert "pass-under" in pf.pair_gateway_log[pc.key]
    # The same site still keeps a foreign net out.
    assert pf._keepouts.segment_blocked((24.3, 5.5), (24.9, 5.5), 1, 10, 0.1)


def test_decline_line_names_every_groups_blocker() -> None:
    pf, pc = _blocked_dual_row(exempt=False)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    log = pf.pair_gateway_log[pc.key]
    # The LAST combination declined ``escape-b``; the attach blocker that
    # closed the pass-under group is named anyway (before #6956 it was not).
    assert log.startswith("gateway declined (pair-gateway-pass-under-escape-b)"), log
    assert "tried: against " in log and "; pass-under " in log, log
    assert "pad-attach x" in log, log
    assert "keepout plane-via-site:GND:J1.X x" in log, log


def test_decline_summary_counts_per_group() -> None:
    from kicad_tools.router.lattice.pathfinder import _gateway_decline_summary

    tally = {
        "against": {"pad-attach": 16, "gateway-leg-via": 8},
        "pass-under": {"offset-degenerate": 4, "pad-attach": 20},
    }
    blockers = {"U2.4 on layer 2: keepout pair-escape-via:VBUS:U2.5": 28, "U2.4: x": 8}
    assert _gateway_decline_summary(tally, blockers) == (
        "tried: against pad-attach x16, gateway-leg-via x8; "
        "pass-under pad-attach x20, offset-degenerate x4; "
        "attach blockers: U2.4 on layer 2: keepout pair-escape-via:VBUS:U2.5 x28, U2.4: x x8"
    )
    assert _gateway_decline_summary({}, {}) == ""


# -- the Autorouter wiring -----------------------------------------------------


def test_core_exempts_the_pair_from_its_escape_site_and_envelope_sites(capsys) -> None:
    router = _chain_autorouter(_sealed_vbus_pads())
    pads = _chain_pads()
    # A #6401 GND site between U2's rows (inside both segments' envelopes),
    # and one well outside every envelope.
    inner = _disc(15.0, 5.0, 0.4, "plane-via-site:GND:U2.2")
    outer = _disc(15.0, 10.5, 0.4, "plane-via-site:GND:J9.9")
    router._plane_via_keepout_areas = (inner, outer)
    router.route_net(1)
    out = capsys.readouterr().out
    mask = router._lattice_pathfinder._keepouts
    by_name = {a.name: a for a in mask.areas}
    pair = frozenset({1, 2})
    assert pair <= by_name["plane-via-site:GND:U2.2"].exempt
    assert by_name["plane-via-site:GND:J9.9"].exempt == frozenset()
    escape = by_name["pair-escape-via:VBUS:U2.5"]
    assert escape.exempt == frozenset({10}) | pair
    # Every other net is still kept out of both.
    for name in ("plane-via-site:GND:U2.2", "pair-escape-via:VBUS:U2.5"):
        assert by_name[name].applies_to(30) and by_name[name].applies_to(9)
    assert "Pair reservation exemption (#6956): plane-via-site:GND:U2.2" in out
    assert "Pair reservation exemption (#6956): pair-escape-via:VBUS:U2.5" in out
    # The planar anchor relocation still sees the un-exempted reservations.
    plain = router._lattice_keepouts_without_pair_exemptions(mask)
    by_plain = {a.name: a for a in plain.areas}
    assert by_plain["plane-via-site:GND:U2.2"].exempt == frozenset()
    assert by_plain["pair-escape-via:VBUS:U2.5"].exempt == frozenset({10})
    assert pads["U2.5"].net == 10


def test_core_exemption_leaves_a_pairless_reservation_alone() -> None:
    router = _chain_autorouter()
    router._lattice_exempt_pair_reservations([])
    assert getattr(router, "_lattice_pair_exempt_added", {}) == {}
    assert router._lattice_keepouts_without_pair_exemptions(None) is None


@pytest.mark.parametrize("exempt", [False, True])
def test_dual_row_outcome_depends_only_on_the_exemption(exempt: bool) -> None:
    pf, pc = _blocked_dual_row(exempt=exempt)
    assert (pf.pair_outcomes[pc.key] == "coupled") is exempt
