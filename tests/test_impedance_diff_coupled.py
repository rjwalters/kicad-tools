"""Diff-pair impedance is evaluated on coupled segments only (Issue #7293)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.physics import Stackup
from kicad_tools.router.diffpair import detect_differential_pairs
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.impedance_specs import (
    default_diff_impedance_specs,
    derive_diff_impedance_specs,
)
from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec

STACKUP = Stackup.jlcpcb_named("JLC04161H-7628")
BOARD_03 = (
    Path(__file__).parent.parent / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"
)


def _seg(net, y, layer="F.Cu", width=0.15, x1=10.0, x2=30.0):
    return SimpleNamespace(net_name=net, width=width, layer=layer, start=(x1, y), end=(x2, y))


def _rule(specs, names=None):
    pairs = detect_differential_pairs(names or {1: "USB_D+", 2: "USB_D-"})
    return ImpedanceRule(specs=specs, stackup=STACKUP, detected_pairs=pairs)


def _run(rule, segments):
    return rule.check(SimpleNamespace(segments=segments), None).violations


COUPLED = [_seg("USB_D+", 10.0), _seg("USB_D-", 10.45)]  # 0.30 mm gap


def test_defaults_alongside_map_are_capped_at_warning():
    # (a) 150 ohm target is far off the real result: >20 % but advisory.
    rule = _rule([*default_diff_impedance_specs()])
    rule.specs[0] = NetImpedanceSpec(r"USB.*D[PM\+\-]?", target_zdiff=200.0, advisory=True)
    found = _run(rule, COUPLED)
    assert found and {v.severity for v in found} == {"warning"}


def test_declared_target_over_20_percent_is_error():
    # (b)
    spec = NetImpedanceSpec(r"USB_D[+-]", target_zdiff=200.0)
    found = _run(_rule([spec]), COUPLED)
    assert found and {v.severity for v in found} == {"error"}


def test_breakout_only_pair_is_unmeasurable():
    # (c) gap 1.0 mm > coupling window.
    rule = _rule([NetImpedanceSpec(r"USB_D[+-]", target_zdiff=90.0)])
    assert not _run(rule, [_seg("USB_D+", 10.0), _seg("USB_D-", 11.15)])
    assert rule.unmeasured_pairs == [("USB_D+", "USB_D-")]


def test_stripline_pair_is_judged():
    # (d) inner layer takes the edge_coupled_stripline path.
    rule = _rule([NetImpedanceSpec(r"USB_D[+-]", target_zdiff=300.0)])
    segs = [_seg("USB_D+", 10.0, layer="In2.Cu"), _seg("USB_D-", 10.45, layer="In2.Cu")]
    found = _run(rule, segs)
    assert found and found[0].layer == "In2.Cu"
    assert rule.unmeasured_pairs == []


def test_unrouted_partner_is_unmeasurable():
    # (e)
    rule = _rule([NetImpedanceSpec(r"USB_D[+-]", target_zdiff=90.0)])
    assert not _run(rule, [_seg("USB_D+", 10.0)])
    assert rule.unmeasured_pairs == [("USB_D+", "USB_D-")]


def test_measured_pair_is_not_reported_unmeasurable():
    rule = _rule([NetImpedanceSpec(r"USB_D[+-]", target_zdiff=90.0)])
    _run(rule, COUPLED)
    assert rule.unmeasured_pairs == []


def test_derive_diff_specs_uses_declared_targets():
    from kicad_tools.router.rules import NetClassRouting

    nc = NetClassRouting(name="HS", target_diff_impedance=85.0, impedance_tolerance_percent=5.0)
    none = NetClassRouting(name="X")
    specs = derive_diff_impedance_specs({"A+": nc, "B": none})
    assert [(s.target_zdiff, s.tolerance_percent, s.advisory) for s in specs] == [
        (85.0, 5.0, False)
    ]
    assert all(s.target_z0 is None and s.advisory for s in default_diff_impedance_specs())


def _board_03_checker(with_map, usb_width=None):
    pcb = PCB.load(str(BOARD_03))
    if usb_width is not None:
        # Synthetic off-target copy (in memory only): re-width the In2.Cu USB pair.
        for seg in pcb.segments:
            if seg.layer == "In2.Cu" and pcb.get_net(seg.net_number).name.startswith("USB_D"):
                seg.width = usb_width
    # (f) name-based fallback detection; the map branch keeps the defaults.
    return DRCChecker(pcb, net_class_map={} if with_map else None)


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 artifact missing")
@pytest.mark.parametrize("with_map", [False, True])
def test_checker_judges_board_03_usb_pair_on_both_branches(with_map):
    # Board 03's In2.Cu USB pair is 0.20 mm at a 0.15 mm gap (the #7713 arm C
    # re-derive; #7324 had hand-narrowed it to 0.22 mm) with a declared 90 ohm
    # target, i.e. in band: judged (not unmeasured) with no findings.
    checker = _board_03_checker(with_map)
    assert checker.check_impedance().violations == []
    assert checker.impedance_unmeasured_pairs == []

    # Off-target witness so the in-band result is not vacuous: the same board
    # with a much wider pair must be judged and flagged on both branches.
    off = _board_03_checker(with_map, usb_width=0.12)
    found = off.check_impedance().violations
    assert {i for v in found for i in v.items} == {"USB_D+", "USB_D-"}
    assert {v.severity for v in found} == {"warning"}
    assert {v.layer for v in found} == {"In2.Cu"}
    assert off.impedance_unmeasured_pairs == []
