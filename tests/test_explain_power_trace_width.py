"""PowerTraceWidthCheck: deterministic worst-case reporting and no-load nets.

Issue #6028: the check kept the *first* narrow segment per net (so the
reported width followed file order) and flagged IC internal-regulator
outputs that only feed a decoupling capacitor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.explain.checks.power import (
    PowerTraceWidthCheck,
    decoupling_only_output_nets,
)
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_09 = REPO_ROOT / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "+5V")
  (net 2 "VREG_INT")
  (net 3 "LDO_OUT")
  (net 4 "VCAP_TP")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "PDCTRL")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "+5V") (pinfunction "VDD") (pintype "power_in"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "VREG_INT") (pinfunction "VREG") (pintype "power_out"))
    (pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "LDO_OUT") (pinfunction "OUT") (pintype "power_out"))
    (pad "4" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "VCAP_TP") (pinfunction "VCAP") (pintype "power_out"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "C1")
    (property "Value" "1u")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "VREG_INT") (pintype "passive"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (at 20 20)
    (property "Reference" "C2")
    (property "Value" "1u")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "LDO_OUT") (pintype "passive"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "LDO_OUT") (pintype "passive"))
  )
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 30 20)
    (property "Reference" "U2")
    (property "Value" "LOAD")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "LDO_OUT") (pinfunction "VDD") (pintype "power_in"))
  )
  (footprint "TestPoint:TestPoint_Pad_D1.0mm"
    (layer "F.Cu")
    (at 40 20)
    (property "Reference" "TP1")
    (property "Value" "TP")
    (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu") (net 4 "VCAP_TP") (pintype "passive"))
  )
"""

# Three narrow +5V segments: 0.25, 0.15 (narrowest) and 0.20 mm.
_PLUS5_SEGMENTS = [
    '  (segment (start 0 0) (end 1 0) (width 0.25) (layer "F.Cu") (net 1))\n',
    '  (segment (start 2 0) (end 3 0) (width 0.15) (layer "F.Cu") (net 1))\n',
    '  (segment (start 4 0) (end 5 0) (width 0.2) (layer "F.Cu") (net 1))\n',
    '  (segment (start 6 0) (end 7 0) (width 0.5) (layer "F.Cu") (net 1))\n',
]
_OTHER_SEGMENTS = [
    '  (segment (start 0 5) (end 1 5) (width 0.2) (layer "F.Cu") (net 2))\n',
    '  (segment (start 0 6) (end 1 6) (width 0.2) (layer "F.Cu") (net 3))\n',
    '  (segment (start 0 7) (end 1 7) (width 0.2) (layer "F.Cu") (net 4))\n',
]


def _write(tmp_path: Path, segments: list[str], name: str = "board.kicad_pcb") -> PCB:
    path = tmp_path / name
    path.write_text(_HEADER + "".join(segments) + ")\n")
    return PCB.load(str(path))


def _by_net(pcb: PCB) -> dict[str, object]:
    return {m.components[0]: m for m in PowerTraceWidthCheck().check(pcb)}


@pytest.mark.parametrize(
    "order",
    [[0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1], [0, 2, 3, 1]],
    ids=["file", "reversed", "shuffled-a", "shuffled-b"],
)
def test_reports_narrowest_segment_regardless_of_order(tmp_path: Path, order: list[int]) -> None:
    segments = [_PLUS5_SEGMENTS[i] for i in order] + _OTHER_SEGMENTS
    finding = _by_net(_write(tmp_path, segments))["+5V"]
    assert "only 0.15mm wide" in finding.explanation
    assert "(3 segments below 0.3mm)" in finding.explanation
    assert finding.location == (2.0, 0.0)


def test_output_is_identical_across_segment_orders(tmp_path: Path) -> None:
    forward = _write(tmp_path, _PLUS5_SEGMENTS + _OTHER_SEGMENTS, "a.kicad_pcb")
    backward = _write(tmp_path, list(reversed(_PLUS5_SEGMENTS + _OTHER_SEGMENTS)), "b.kicad_pcb")
    a = PowerTraceWidthCheck().check(forward)
    b = PowerTraceWidthCheck().check(backward)
    assert [(m.components, m.explanation, m.location) for m in a] == [
        (m.components, m.explanation, m.location) for m in b
    ]
    # One finding per net, sorted by net name.
    names = [m.components[0] for m in a]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_decoupling_only_regulator_output_is_not_flagged(tmp_path: Path) -> None:
    """VREG_INT: power_out pin + one capacitor -> no load, no warning.

    LDO_OUT also feeds U2's power_in pin and VCAP_TP reaches a test point,
    so both still count as loaded rails.
    """
    pcb = _write(tmp_path, _PLUS5_SEGMENTS + _OTHER_SEGMENTS)
    assert decoupling_only_output_nets(pcb) == {"VREG_INT"}
    flagged = set(_by_net(pcb))
    assert "VREG_INT" not in flagged
    assert {"+5V", "LDO_OUT", "VCAP_TP"} <= flagged


def test_board_without_pintypes_suppresses_nothing(tmp_path: Path) -> None:
    path = tmp_path / "bare.kicad_pcb"
    text = _HEADER.replace('(pintype "power_out")', "").replace('(pintype "power_in")', "")
    path.write_text(text + "".join(_OTHER_SEGMENTS) + ")\n")
    assert decoupling_only_output_nets(PCB.load(str(path))) == set()


@pytest.mark.skipif(not BOARD_09.exists(), reason="board 09 output not present")
def test_board09_stusb4500_vreg_pins_are_not_power_traces() -> None:
    pcb = PCB.load(str(BOARD_09))
    assert decoupling_only_output_nets(pcb) == {"VREG_1V2", "VREG_2V7"}
    findings = _by_net(pcb)
    assert not {"VREG_1V2", "VREG_2V7"} & set(findings)
    # +5V_OUT has 0.15mm segments; the first-in-file one is 0.25mm.
    assert "only 0.15mm wide" in findings["+5V_OUT"].explanation
