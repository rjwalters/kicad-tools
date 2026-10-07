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


# --- Issue #5995: non-``U`` active parts ------------------------------------


@pytest.mark.parametrize(
    ("ref", "name", "pads", "expected"),
    [
        # Oscillators (powered) are ICs; passive crystals are not.
        ("X1", "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm", 4, True),
        ("Y2", "Oscillator:Oscillator_SMD_SiT_PQFN-6Pin_3.2x2.5mm", 6, True),
        ("OSC1", "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm", 4, True),
        ("Y1", "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm", 4, False),
        ("Y3", "Crystal:Crystal_HC49-4H_Vertical", 2, False),
        ("X2", "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm", 2, False),
        # Regulators in power packages.
        ("VR1", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", 4, True),
        ("REG1", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", 4, True),
        ("VR3", "Package_TO_SOT_SMD:TO-252-2", 3, True),
        ("VREG2", "Package_TO_SOT_SMD:SOT-89-3", 3, True),
        ("LDO1", "Package_TO_SOT_SMD:SOT-23", 3, True),
        ("PS1", "Converter_DCDC:Converter_DCDC_RECOM_R-78E-0.5_THT", 3, True),
        # ``VR`` used for a trimmer must not be promoted.
        ("VR2", "Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical", 3, False),
        ("VR4", "custom:Trimmer_Potentiometer_3mm", 3, False),
        ("VR5", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", 2, False),
        # Sensor / converter libraries.
        ("S1", "Sensor:Sensirion_SCD4x-1EP_10.1x10.1mm_P1.25mm_EP4.8x4.8mm", 21, True),
        ("S2", "Sensor_Humidity:Sensirion_DFN-4-1EP_2x2mm_P1mm_EP0.7x1.6mm", 5, True),
        ("PSU1", "Converter_DCDC:Converter_DCDC_TRACO_TSR-1_THT", 3, True),
        # Project-local module with many pads.
        ("MOD2", "myproj:RPi_CM4", 100, True),
        ("MOD3", "myproj:Small_Thing", 6, False),
        # False positives that must stay out.
        ("S3", "Button_Switch_THT:SW_DIP_SPSTx04_Slide_9.78x12.34mm_W7.62mm_P2.54mm", 8, False),
        ("SW4", "Button_Switch_SMD:SW_DIP_SPSTx04_Slide_6.7x11.72mm_W8.61mm_P2.54mm", 8, False),
        ("Q2", "Package_TO_SOT_SMD:TO-252-2", 3, False),
        ("Q3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", 4, False),
        ("A2", "Connector_PinHeader_2.54mm:PinHeader_2x20_P2.54mm_Vertical", 40, False),
        ("H1", "Package_TO_SOT_THT:TO-220-3_Vertical", 3, False),
        ("U7", "Connector_USB:USB_C_Receptacle_GCT_USB4085", 20, False),
    ],
)
def test_is_ic_footprint_non_u_active_parts(ref: str, name: str, pads: int, expected: bool) -> None:
    assert is_ic_footprint(_fp(ref, name, pads)) is expected


def test_power_in_pintype_promotes_unconventional_reference() -> None:
    """A symbol-derived ``power_in`` pad marks an active part (issue #5995)."""
    fp = _fp("Y5", "custom:SMD_Clock_5032", 4)
    assert is_ic_footprint(fp) is False
    fp.pads[3].pintype = "power_in"
    assert is_ic_footprint(fp) is True
    # A connector stays excluded even with a power_in pad.
    conn = _fp("J9", "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", 4)
    conn.pads[0].pintype = "power_in"
    assert is_ic_footprint(conn) is False


_OSCILLATOR_NEAR_CAP = """
  (footprint "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm"
    (layer "F.Cu")
    (at 30 10)
    (property "Reference" "X1")
    (property "Value" "25MHz")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))
    (pad "2" smd rect (at -0.5 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at 0.5 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))
    (pad "4" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "+3V3"))
  )
"""


def test_oscillator_is_a_bypass_partner(tmp_path: Path) -> None:
    """``X1`` in ``Oscillator:`` now pairs with a far bypass cap (issue #5995)."""
    body = _two_pad("Capacitor_SMD:C_0402", "C1", "100nF", 10) + _OSCILLATOR_NEAR_CAP
    mistakes = BypassCapDistanceCheck().check(_load(tmp_path, body))
    assert {m.components[1] for m in mistakes} == {"X1"}
