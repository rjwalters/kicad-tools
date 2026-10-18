"""ImpedanceRule measures the routed intra-pair gap (Issue #7079)."""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.physics import Stackup
from kicad_tools.physics.coupled_lines import CoupledLines
from kicad_tools.router.diffpair import detect_differential_pairs
from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec

STACKUP = Stackup.jlcpcb_named("JLC04161H-7628")


def _seg(net, y, layer="F.Cu", width=0.15, x1=10.0, x2=30.0):
    return SimpleNamespace(net_name=net, width=width, layer=layer, start=(x1, y), end=(x2, y))


def _rule(target):
    pairs = detect_differential_pairs({1: "USB_D+", 2: "USB_D-"})
    return ImpedanceRule(
        specs=[NetImpedanceSpec(r"USB_D[+-]", target_zdiff=target)],
        stackup=STACKUP,
        detected_pairs=pairs,
    )


def _check(rule, segments):
    return rule.check(SimpleNamespace(segments=segments), None).violations


def test_gap_measured_from_partner():
    cl = CoupledLines(STACKUP)
    z_board = cl.edge_coupled_microstrip(0.15, 0.30, "F.Cu").zdiff
    z_default = cl.edge_coupled_microstrip(0.15, 0.15, "F.Cu").zdiff
    assert z_board > z_default * 1.02  # the gap must matter for this test
    segs = [_seg("USB_D+", 10.0), _seg("USB_D-", 10.45)]
    # Target the 0.30 mm result: passes only if the real gap is used.
    assert not _check(_rule(z_board), segs)
    # Target 20 % below it: outside the 10 % window at the real gap, but
    # within it at the old hard-coded 0.15 mm gap.
    low = z_board * 0.8
    assert _check(_rule(low), segs)
    assert not _check(_rule(z_default), [_seg("USB_D+", 10.0), _seg("USB_D-", 10.30)])


def test_pair_violation_has_no_width_hint():
    msgs = [v.message for v in _check(_rule(120.0), [_seg("USB_D+", 10.0), _seg("USB_D-", 10.45)])]
    assert msgs and all("requires" not in m for m in msgs)


def test_unrouted_partner_is_not_judged():
    assert not _check(_rule(120.0), [_seg("USB_D+", 10.0)])


def test_partner_on_other_layer_is_not_judged():
    segs = [_seg("USB_D+", 10.0), _seg("USB_D-", 10.45, layer="B.Cu")]
    assert not _check(_rule(120.0), segs)


def test_zero_width_single_ended_is_not_passed_as_50_ohm():
    rule = ImpedanceRule(specs=[NetImpedanceSpec(r"CLK", target_z0=50.0)], stackup=STACKUP)
    assert not _check(rule, [_seg("CLK", 5.0, width=0.0)])
