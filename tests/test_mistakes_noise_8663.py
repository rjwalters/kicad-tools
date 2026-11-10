"""Noise reduction in detect-mistakes checks (issue #8663)."""

from __future__ import annotations

import math
from pathlib import Path

from kicad_tools.explain.checks.acid_trap import AcidTrapCheck
from kicad_tools.explain.checks.bypass import BypassCapDistanceCheck
from kicad_tools.explain.checks.connectivity import PullUpResistorCheck
from kicad_tools.explain.checks.crystal import CrystalTraceLengthCheck
from kicad_tools.explain.checks.power import PowerTraceWidthCheck
from kicad_tools.schema.pcb import PCB

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "GND")
  (net 3 "SIG")
  (net 4 "XIN")
  (net 5 "NRST")
"""


def _load(tmp_path: Path, body: str) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEADER + body + ")\n")
    return PCB.load(str(path))


def _cap(ref: str, x: float) -> str:
    return f"""
  (footprint "Capacitor_SMD:C_0402" (layer "F.Cu") (at {x} 10)
    (property "Reference" "{ref}") (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "+3V3"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND")))
"""


def _ic(ref: str, x: float) -> str:
    return f"""
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" (layer "F.Cu") (at {x} 10)
    (property "Reference" "{ref}") (property "Value" "IC")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "+3V3"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))
    (pad "4" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 "SIG")))
"""


def test_bypass_one_finding_per_cap_and_pairs_nearest_ic(tmp_path: Path) -> None:
    # C1 sits beside U1; U2 is far away on the same rail.
    pcb = _load(tmp_path, _ic("U1", 20) + _ic("U2", 80) + _cap("C1", 17.5))
    assert BypassCapDistanceCheck().check(pcb) == []


def test_bypass_far_cap_warns_once_even_with_two_ics(tmp_path: Path) -> None:
    pcb = _load(tmp_path, _ic("U1", 20) + _ic("U2", 80) + _cap("C1", 50))
    mistakes = BypassCapDistanceCheck().check(pcb)
    assert len(mistakes) == 1
    assert mistakes[0].components[0] == "C1"


def _seg(x1, y1, x2, y2, w=0.25, net=3, layer="F.Cu") -> str:
    return (
        f'(segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) (layer "{layer}") (net {net}))\n'
    )


def _vertex(deg: float) -> str:
    r = math.radians(deg)
    return _seg(0, 0, 5, 0) + _seg(0, 0, 5 * math.cos(r), 5 * math.sin(r))


def test_acid_trap_tolerates_rounded_right_angle(tmp_path: Path) -> None:
    assert AcidTrapCheck().check(_load(tmp_path, _vertex(89.7))) == []


def test_acid_trap_still_flags_acute_and_prints_true_angle(tmp_path: Path) -> None:
    mistakes = AcidTrapCheck().check(_load(tmp_path, _vertex(45)))
    assert len(mistakes) == 1
    assert "45.0°" in mistakes[0].explanation


def test_acid_trap_skips_collinear_overlap(tmp_path: Path) -> None:
    assert AcidTrapCheck().check(_load(tmp_path, _seg(0, 0, 5, 0) + _seg(0, 0, 3, 0))) == []


def _xtal(ref: str, name: str, pads: str) -> str:
    return f"""
  (footprint "{name}" (layer "F.Cu") (at 10 10)
    (property "Reference" "{ref}") (property "Value" "8MHz")
    {pads})
"""


_XTAL_PADS = """
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 4 "XIN"))
    (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "XIN"))
"""
_LONG_XIN = _seg(0, 0, 15, 0, net=4)


def test_crystal_long_trace_warns_once_despite_two_pads_on_net(tmp_path: Path) -> None:
    body = _xtal("Y1", "Crystal:Crystal_SMD_3225-4Pin", _XTAL_PADS) + _LONG_XIN
    mistakes = CrystalTraceLengthCheck().check(_load(tmp_path, body))
    assert len(mistakes) == 1


def test_crystal_ignores_supply_nets(tmp_path: Path) -> None:
    pads = _XTAL_PADS + '(pad "4" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))'
    body = _xtal("Y1", "Crystal:Crystal_SMD_3225-4Pin", pads) + _seg(0, 0, 30, 0, net=2)
    assert CrystalTraceLengthCheck().check(_load(tmp_path, body)) == []


def test_active_oscillator_is_not_a_crystal_node(tmp_path: Path) -> None:
    pads = """
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "+3V3"))
    (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "XIN"))
    """
    body = _xtal("X1", "Oscillator:Oscillator_SMD_4Pin", pads) + _LONG_XIN
    assert CrystalTraceLengthCheck().check(_load(tmp_path, body)) == []


_VIA = '(via (at 0 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
_ZONE = """(zone (net 1) (net_name "+3V3") (layer "B.Cu")
  (connect_pads (clearance 0.2)) (min_thickness 0.2)
  (polygon (pts (xy -5 -5) (xy 50 -5) (xy 50 50) (xy -5 50))))
"""


def test_power_via_stub_into_plane_is_skipped(tmp_path: Path) -> None:
    body = _ZONE + _VIA + _seg(0, 0, 1.5, 0, w=0.2, net=1)
    assert PowerTraceWidthCheck().check(_load(tmp_path, body)) == []


def test_power_long_narrow_trace_still_warns_with_plane(tmp_path: Path) -> None:
    body = _ZONE + _VIA + _seg(0, 0, 20, 0, w=0.2, net=1)
    assert len(PowerTraceWidthCheck().check(_load(tmp_path, body))) == 1


def test_power_narrow_stub_without_plane_still_warns(tmp_path: Path) -> None:
    body = _VIA + _seg(0, 0, 1.5, 0, w=0.2, net=1)
    assert len(PowerTraceWidthCheck().check(_load(tmp_path, body))) == 1


_RESET = """
  (footprint "Package_SO:SOIC-8" (layer "F.Cu") (at 10 10)
    (property "Reference" "U1") (property "Value" "MCU")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 5 "NRST")))
  (footprint "Button_Switch_SMD:SW" (layer "F.Cu") (at 20 10)
    (property "Reference" "SW1") (property "Value" "RST")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 5 "NRST")))
"""


def test_reset_without_pullup_is_info_not_warning(tmp_path: Path) -> None:
    mistakes = PullUpResistorCheck().check(_load(tmp_path, _RESET))
    assert [m.severity for m in mistakes] == ["info"]
