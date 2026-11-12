"""Tests for the ``missing_plated_hole`` rule (#8840)."""

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.missing_plated_hole import MissingPlatedHoleRule

pytest.importorskip("shapely")


def _fp(ref, x, y, pad):
    return (
        f'(footprint "Test:FP" (layer "F.Cu") (uuid "fp-{ref}") (at {x} {y})\n'
        f'  (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS"))\n'
        f"  {pad})\n"
    )


def _smd(layer):
    return f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "{layer}") (net 0 ""))'


THRU = '(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 ""))'


def _smd_net(layer, net, name):
    return f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "{layer}") (net {net} "{name}"))'


def _violations(tmp_path, *fps):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "A") (net 2 "B")\n' + "".join(fps) + ")\n"
    )
    pcb = PCB.load(str(p))
    return p, pcb, MissingPlatedHoleRule().check(pcb, None).violations


def test_colocated_top_bottom_pads_flagged(tmp_path):
    _, _, v = _violations(
        tmp_path, _fp("TP1", 10, 10, _smd("F.Cu")), _fp("TP2", 10, 10, _smd("B.Cu"))
    )
    assert [(x.rule_id, x.severity) for x in v] == [("missing_plated_hole", "warning")]
    assert v[0].items == ("TP1.1", "TP2.1")


def test_through_hole_pad_between_not_flagged(tmp_path):
    _, _, v = _violations(
        tmp_path,
        _fp("TP1", 10, 10, _smd("F.Cu")),
        _fp("TP2", 10, 10, _smd("B.Cu")),
        _fp("J1", 10, 10, THRU),
    )
    assert v == []


def test_via_between_not_flagged(tmp_path):
    via = '(via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0))'
    _, _, v = _violations(
        tmp_path, _fp("TP1", 10, 10, _smd("F.Cu")), _fp("TP2", 10, 10, _smd("B.Cu")), via
    )
    assert v == []


def test_offset_pads_and_lone_through_hole_not_flagged(tmp_path):
    _, _, v = _violations(
        tmp_path, _fp("TP1", 10, 10, _smd("F.Cu")), _fp("TP2", 14, 10, _smd("B.Cu"))
    )
    assert v == []
    _, _, v = _violations(tmp_path, _fp("J1", 10, 10, THRU))
    assert v == []


def test_checker_method_registered(tmp_path):
    assert "check_missing_plated_hole" in DRCChecker.CHECK_ALL_METHODS
    _, pcb, _ = _violations(
        tmp_path, _fp("TP1", 10, 10, _smd("F.Cu")), _fp("TP2", 10, 10, _smd("B.Cu"))
    )
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
    assert checker.check_missing_plated_hole().violations[0].rule_id == "missing_plated_hole"


def test_different_net_pads_not_flagged(tmp_path):
    _, _, v = _violations(
        tmp_path,
        _fp("J1", 10, 10, _smd_net("F.Cu", 1, "A")),
        _fp("J2", 10, 10, _smd_net("B.Cu", 2, "B")),
    )
    assert v == []
    _, _, v = _violations(
        tmp_path,
        _fp("J1", 10, 10, _smd_net("F.Cu", 1, "A")),
        _fp("J2", 10, 10, _smd_net("B.Cu", 1, "A")),
    )
    assert len(v) == 1
