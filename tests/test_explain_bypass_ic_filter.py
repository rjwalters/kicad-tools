"""BypassCapDistanceCheck only pairs bypass caps with real ICs (issue #5970).

The check used to treat *any* footprint with a power-net pad as an "IC", so
resistors, other capacitors, connectors and fuses were reported as the part
a bypass cap should sit next to (65 of 90 findings on board 03).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.explain.checks.bypass import BypassCapDistanceCheck
from kicad_tools.explain.mistakes import is_ic_footprint
from kicad_tools.schema.pcb import PCB, Footprint, Pad

_REPO = Path(__file__).resolve().parents[1]

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "GND")
  (net 3 "SIG")
"""


def _two_pad(lib: str, ref: str, value: str, x: float) -> str:
    return f"""
  (footprint "{lib}"
    (layer "F.Cu")
    (at {x} 10)
    (property "Reference" "{ref}")
    (property "Value" "{value}")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "+3V3"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
"""


_CONNECTOR = """
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
    (layer "F.Cu")
    (at 60 10)
    (property "Reference" "J1")
    (property "Value" "PWR")
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (layers "F.Cu") (net 1 "+3V3"))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (layers "F.Cu") (net 2 "GND"))
    (pad "3" thru_hole oval (at 0 5.08) (size 1.7 1.7) (layers "F.Cu") (net 3 "SIG"))
    (pad "4" thru_hole oval (at 0 7.62) (size 1.7 1.7) (layers "F.Cu") (net 2 "GND"))
  )
"""

_FAR_IC = """
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (at 90 10)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "+3V3"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))
    (pad "4" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))
  )
"""

_PASSIVES = (
    _two_pad("Capacitor_SMD:C_0402", "C1", "100nF", 10)
    + _two_pad("Capacitor_SMD:C_0402", "C2", "100nF", 30)
    + _two_pad("Resistor_SMD:R_0402", "R1", "10k", 45)
    + _two_pad("Fuse:Fuse_1206_3216Metric", "F1", "500mA", 50)
    + _CONNECTOR
)


def _load(tmp_path: Path, body: str) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEADER + body + ")\n")
    return PCB.load(str(path))


def test_caps_connector_and_fuse_on_a_rail_without_an_ic_give_no_findings(
    tmp_path: Path,
) -> None:
    pcb = _load(tmp_path, _PASSIVES)
    assert BypassCapDistanceCheck().check(pcb) == []


def test_far_ic_is_still_reported_and_is_the_only_partner(tmp_path: Path) -> None:
    pcb = _load(tmp_path, _PASSIVES + _FAR_IC)
    mistakes = BypassCapDistanceCheck().check(pcb)
    assert mistakes, "a bypass cap far from a real IC must still be flagged"
    assert {m.components[1] for m in mistakes} == {"U1"}
    assert {m.components[0] for m in mistakes} == {"C1", "C2"}


def _fp(ref: str, name: str, pads: int) -> Footprint:
    return Footprint(
        name=name,
        layer="F.Cu",
        position=(0.0, 0.0),
        rotation=0.0,
        reference=ref,
        value="",
        pads=[
            Pad(
                number=str(i + 1),
                type="smd",
                shape="rect",
                position=(float(i), 0.0),
                size=(1.0, 1.0),
                layers=["F.Cu"],
            )
            for i in range(pads)
        ],
    )


@pytest.mark.parametrize(
    ("ref", "name", "pads", "expected"),
    [
        ("U1", "Package_QFP:LQFP-48_7x7mm_P0.5mm", 48, True),
        ("U4", "Package_TO_SOT_SMD:SOT-23", 3, True),  # SOT-23 LDO
        ("IC2", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 8, True),
        ("A1", "RF_Module:ESP32-WROOM-32", 39, True),
        ("MCU1", "custom:TQFP-32_7x7mm_P0.8mm", 32, True),
        ("VR1", "Package_TO_SOT_SMD:SOT-23-5", 5, True),
        ("U9", "Package_TO_SOT_SMD:SOT-23", 2, False),
        ("C12", "Capacitor_SMD:C_0402_1005Metric", 2, False),
        ("R3", "Resistor_SMD:R_0402_1005Metric", 2, False),
        ("F1", "Fuse:Fuse_1206_3216Metric", 2, False),
        ("RV1", "Varistor:RV_Disc_D7mm", 2, False),
        ("J1", "Connector_USB:USB_C_Receptacle_GCT_USB4085", 20, False),
        ("Q1", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 8, False),
        ("D1", "Diode_SMD:D_SMA", 2, False),
        ("K1", "Relay_THT:Relay_SPDT_Omron-G5LE-1", 5, False),
        ("X9", "Unknown:Thing", 6, False),
    ],
)
def test_is_ic_footprint(ref: str, name: str, pads: int, expected: bool) -> None:
    assert is_ic_footprint(_fp(ref, name, pads)) is expected


def test_board03_bypass_findings_name_only_ics() -> None:
    """Acceptance: on board 03 every bypass finding's IC is a ``U*`` part."""
    path = _REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"
    if not path.exists():
        pytest.skip("board 03 routed output not present")
    mistakes = BypassCapDistanceCheck().check(PCB.load(str(path)))
    assert mistakes, "board 03's real IC findings must not disappear"
    assert all(m.components[1].startswith("U") for m in mistakes), sorted(
        {m.components[1] for m in mistakes}
    )
