"""Power/ground net classification for ``kct detect-mistakes`` (issue #5939).

``is_power_net`` used to be a substring match, so ``USB_D+`` ("+"),
``/PG_3V3`` ("3V3") and ``TRIGGER_5V`` ("5V") were power rails and board 03
reported false "Missing decoupling capacitor" errors on its USB data nets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.explain.checks.decoupling import MissingDecouplingCapCheck
from kicad_tools.explain.mistakes import (
    MistakeCategory,
    detect_mistakes,
    is_ground_net,
    is_power_net,
    is_supply_net,
    power_pin_nets,
)
from kicad_tools.optim.fom_electrical import _looks_like_power_net
from kicad_tools.router.net_class import (
    is_power_rail_name,
    is_power_rail_pin,
    is_switch_node_name,
)
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"

# The five names from the issue: signals that merely *contain* "+" or a
# voltage fragment, and a KiCad auto-named no-connect net.
NOT_POWER = [
    # Rail-ish names carrying a derived-signal qualifier, and the Kelvin
    # sense pair the old "VIN" substring matched inside "KELVIN".
    "VIN_SENSE",
    "VOUT_DET",
    "3V3_PG",
    "5V_EN",
    "KELVIN_P",
    "KELVIN_N",
    "/USB_D+",
    "ISENSE_A+",
    "/PG_3V3",
    "TRIGGER_5V",
    "unconnected-(U11-D+-Pad2)",
]

POWER_RAILS = [
    "+3V3",
    "VBUS",
    "/+5V",
    "+5V",
    "3.3V",
    "-12V",
    "+1V8",
    "VCC",
    "VDD_CORE",
    # Leading voltage token plus a qualifier (PR #5971 review)
    "3V3_MCU",
    "5V_USB",
    "12V_IN",
    "3V3_LDO",
    # P-/V-prefixed voltages (Intel/server style)
    "P3V3",
    "P5V",
    "P12V",
    "V3V3",
    "V5V0",
    "V3P3",
    # Named rails, including the old FOM hint VAA and the VBUS alias VUSB
    "VAA",
    "VUSB",
    "VIN",
    "VIN_12V",
    "VIN_FILT",
    "VBAT",
    "VSYS",
    "VCC_3V3",
    "VDD_1V8",
    "VDDA",
    "AVDD",
    "+12V",
    "+3.3V",
    # VCC/VDD supplies named for what they feed (PR #5971 re-review): the
    # ADC/REF/MON word is the load, not a derived signal.
    "VDD_ADC",
    "AVDD_ADC",
    "VDDA_ADC",
    "VCC_REF",
    "VDD_MON",
    "VDDIO",
    "AVCC",
    "DVDD",
    "PVIN",
    # Board 09's buck output and power-path rails
    "VOUT_PRE",
    "VBUS_FUSED",
    "VBUS_RAW",
]

# More signals next to rail-ish words: a derived-signal qualifier
# (SENSE/DET/EN/FB/PG ...), a voltage fragment that does not lead the name,
# a P/V prefix with no voltage, or a regulator compensation pin.
NOT_RAILS = [
    "PV_EN",
    "PWR_LED",
    "POWER_GOOD",
    "VBUS_SENSE",
    "VOUT_FB",
    "VIN_EN2",
    "VBUS_OVP",
    "3V3_EN",
    "5V_PG",
    "VREF",
    # A non-supply-family rail word with a telemetry qualifier stays a signal,
    # and supply-family names still drop out on control qualifiers.
    "VBAT_ADC",
    "VIN_REF",
    "VDD_EN",
    "VCC_PG",
    "AVDD_SENSE",
    "VCAP1",
    "VREG_1V2",
    "Net-(U1-VDD)",
]


@pytest.mark.parametrize("name", NOT_POWER)
def test_signal_names_are_not_power(name: str) -> None:
    assert not is_power_net(name)
    assert not is_supply_net(name)
    assert not is_ground_net(name)


@pytest.mark.parametrize("name", POWER_RAILS)
def test_real_rails_are_power(name: str) -> None:
    assert is_power_net(name)
    assert is_supply_net(name)


@pytest.mark.parametrize("name", NOT_RAILS)
def test_rail_adjacent_signal_names_are_not_power(name: str) -> None:
    assert not is_power_rail_name(name)
    assert not is_power_net(name)


@pytest.mark.parametrize("name", ["GND", "/GND", "AGND", "GNDA", "SHIELD_GND", "VSS"])
def test_ground_rails_are_supply_but_not_power(name: str) -> None:
    """GND is a rail (``is_supply_net``) but stays out of ``is_power_net``.

    The decoupling checks look for a *power* pin paired with a ground return;
    counting GND as power would make every IC's ground pin need its own cap.
    """
    assert is_ground_net(name)
    assert is_supply_net(name)
    assert not is_power_net(name)


@pytest.mark.parametrize(
    "name",
    [
        "VBUS_DET",
        "VIN_SENSE",
        "PWR_EN",
        "Net-(U1-VDD)",
        "unconnected-(J1-GND-Pad3)",
        "unconnected-(U1-VCC-Pad1)",
        "",
    ],
)
def test_rail_keyword_inside_signal_name_is_not_a_rail(name: str) -> None:
    assert not is_power_net(name)
    assert not is_ground_net(name)


def test_fom_electrical_shares_the_classifier() -> None:
    for name in NOT_POWER + ["PWR_EN", "GND"]:
        assert not _looks_like_power_net(name)
    for name in POWER_RAILS:
        assert _looks_like_power_net(name) == is_power_rail_name(name)


# ---------------------------------------------------------------------------
# Pin-type evidence
# ---------------------------------------------------------------------------

_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "MAIN")
  (net 2 "GND")
  (net 3 "USB_D+")
  (net 4 "SIG")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "MAIN") (pintype "power_in"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "GND") (pintype "power_in"))
    (pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "USB_D+") (pintype "bidirectional"))
    (pad "4" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "SIG"))
  )
)
"""


def _load(tmp_path: Path) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_BOARD)
    return PCB.load(str(path))


def test_pad_pintype_is_parsed(tmp_path: Path) -> None:
    pcb = _load(tmp_path)
    fp = pcb.get_footprint("U1")
    assert fp is not None
    types = {pad.number: pad.pintype for pad in fp.pads}
    assert types == {"1": "power_in", "2": "power_in", "3": "bidirectional", "4": ""}


def test_power_pin_evidence_overrides_an_unconventional_name(tmp_path: Path) -> None:
    """A rail named ``MAIN`` is power because a power_in pin sits on it."""
    pcb = _load(tmp_path)
    evidence = power_pin_nets(pcb)
    assert evidence == {"MAIN"}  # GND excluded: ground, not power
    assert is_power_net("MAIN", evidence)
    assert not is_power_net("MAIN")  # name alone says nothing
    assert not is_power_net("USB_D+", evidence)

    mistakes = MissingDecouplingCapCheck().check(pcb)
    assert [m.components for m in mistakes] == [["U1"]]
    assert "MAIN" in mistakes[0].explanation


# ---------------------------------------------------------------------------
# Board 03 regression (the issue's repro)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BOARD_03.exists(), reason="board 03 output not present")
def test_board03_has_no_findings_on_usb_data_nets() -> None:
    mistakes = detect_mistakes(PCB.load(str(BOARD_03)))
    usb_data = {"USB_D+", "USB_MCU_D+", "USB_D-", "USB_MCU_D-"}

    for m in mistakes:
        if m.category in (MistakeCategory.DECOUPLING, MistakeCategory.POWER_TRACE):
            assert not usb_data & set(m.components), m
            assert not any(net in m.explanation for net in usb_data), m

    assert not [m for m in mistakes if m.category == MistakeCategory.DECOUPLING]


# ---------------------------------------------------------------------------
# Fleet regressions (PR #5971 review)
# ---------------------------------------------------------------------------

BOARD_05 = REPO_ROOT / "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
BOARD_07 = REPO_ROOT / "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"
BOARD_09 = REPO_ROOT / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"


def _power_trace_nets(mistakes: list) -> set[str]:
    nets = set()
    for m in mistakes:
        if m.category == MistakeCategory.POWER_TRACE:
            nets.add(m.explanation.split("Power trace on ", 1)[1].split(" ", 1)[0])
    return nets


@pytest.mark.skipif(not BOARD_09.exists(), reason="board 09 output not present")
def test_board09_buck_output_keeps_its_trace_width_warning() -> None:
    """VOUT_PRE (L1 -> C8/C9 22u -> shunt) is a rail; KELVIN_P/N are not."""
    mistakes = detect_mistakes(PCB.load(str(BOARD_09)))
    nets = _power_trace_nets(mistakes)
    assert {"VOUT_PRE", "VBUS_RAW"} <= nets
    assert not {"KELVIN_P", "KELVIN_N", "VBUS_SENSE"} & nets
    # Q1 (P-MOSFET on VBUS_FUSED) is not an IC that needs decoupling.
    assert not [m for m in mistakes if m.category == MistakeCategory.DECOUPLING]


@pytest.mark.parametrize("board", [BOARD_05, BOARD_07], ids=["board05", "board07"])
def test_decoupled_regulator_pins_are_not_missing_decoupling(board: Path) -> None:
    """Board 05 V3P3 has C6 470nF and board 07 VCAP1/2 have 2.2uF caps.

    Neither value is in ``is_bypass_cap``'s fixed list, so the decoupling
    check must recognise them by parsed capacitance.
    """
    if not board.exists():
        pytest.skip("board output not present")
    mistakes = detect_mistakes(PCB.load(str(board)))
    assert not [m for m in mistakes if m.category == MistakeCategory.DECOUPLING]


@pytest.mark.parametrize(
    ("value", "counts"),
    [
        ("100nF", True),
        ("470nF", True),
        ("2.2uF", True),
        ("22u 25V", True),
        ("4u7", True),
        ("10nF", True),
        ("75p C0G", False),
        ("22pF", False),
        ("DNP", False),
    ],
)
def test_decoupling_cap_value_threshold(value: str, counts: bool) -> None:
    from kicad_tools.explain.checks.decoupling import _is_decoupling_cap

    assert _is_decoupling_cap("C1", value) is counts
    assert not _is_decoupling_cap("R1", value)


# ---------------------------------------------------------------------------
# Switching-regulator switch node is not a rail (issue #5998)
# ---------------------------------------------------------------------------

_BUCK_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu") (31 "B.Cu") (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SW")
  (net 3 "VIN")
  (net 4 "BOOT")
  (net 5 "NODE_A")
  (net 6 "MCU_PH0")
  (footprint "Package_TO_SOT_SMD:SOT-23-6"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U2")
    (property "Value" "TPS54302")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "GND") (pinfunction "GND") (pintype "power_in"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "SW") (pinfunction "SW") (pintype "power_out"))
    (pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "VIN") (pinfunction "VIN") (pintype "power_in"))
    (pad "6" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "BOOT") (pinfunction "BOOT") (pintype "power_in"))
  )
  (footprint "Package_TO_SOT_SMD:SOT-23-5"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "U5")
    (property "Value" "BOOST")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 5 "NODE_A") (pinfunction "LX") (pintype "power_out"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 3 "VIN") (pinfunction "VIN") (pintype "power_in"))
  )
  (footprint "Package_QFP:LQFP-48"
    (layer "F.Cu")
    (at 30 10)
    (property "Reference" "U9")
    (property "Value" "MCU")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 6 "MCU_PH0") (pinfunction "PH0") (pintype "power_in"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 5 "NODE_A") (pinfunction "VDD") (pintype "power_in"))
  )
)
"""


def test_switch_node_is_not_power_pin_evidence(tmp_path: Path) -> None:
    """TPS54302 ``SW`` (power_out) and a boost ``LX`` are not rails.

    ``NODE_A`` carries a VDD ``power_in`` pin as well as the ``LX`` pin, but
    a net that carries a switch node is never a rail.  ``BOOT`` is a
    bootstrap pin; ``PH0`` is an MCU port pin and still counts.
    """
    path = tmp_path / "buck.kicad_pcb"
    path.write_text(_BUCK_BOARD)
    pcb = PCB.load(str(path))
    u2 = pcb.get_footprint("U2")
    assert u2 is not None
    assert {p.number: p.pinfunction for p in u2.pads}["2"] == "SW"

    assert power_pin_nets(pcb) == {"VIN", "MCU_PH0"}
    decoupling = MissingDecouplingCapCheck().check(pcb)
    assert not any("SW" in m.explanation.split("'") for m in decoupling), decoupling


def test_switch_node_net_name_disqualifies_power_out_without_pinfunction(
    tmp_path: Path,
) -> None:
    """Older annotations without ``pinfunction`` fall back to the net name."""
    path = tmp_path / "buck.kicad_pcb"
    path.write_text(_BUCK_BOARD.replace('(pinfunction "SW") ', ""))
    assert "SW" not in power_pin_nets(PCB.load(str(path)))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SW", True),
        ("SW1", True),
        ("SW_2", True),
        ("LX", True),
        ("LX2", True),
        ("PH", True),
        ("PHASE", True),
        ("/power/SW", True),
        ("BUCK_SW", True),
        ("SW_NODE", True),
        ("SWN", True),
        ("BOOST_LX", True),
        ("U2_SW1", True),
        ("PH0", False),
        ("SWD", False),
        ("SWA", False),
        ("SWC", False),
        ("SWO", False),
        ("3V3_SW", False),
        ("+5V_SW", False),
        ("VBUS_SW", False),
        ("BTN_SW", False),
        ("KEY_SW", False),
        ("MODE_SW", False),
        ("LED_SW", False),
        ("EN_SW", False),
        ("SWDIO", False),
        ("SWCLK", False),
        ("SW_3V3", False),
        ("+3V3", False),
        ("", False),
    ],
)
def test_is_switch_node_name(name: str, expected: bool) -> None:
    assert is_switch_node_name(name) is expected


@pytest.mark.parametrize(
    ("pintype", "pin_name", "expected"),
    [
        ("power_out", "VO", True),
        ("power_in", "VDD", True),
        ("power_out", "SW", False),
        ("power_out", "LX1", False),
        ("power_in", "BOOT", False),
        ("passive", "BST", False),
        ("power_in", "BOOT0", True),  # MCU strap pin name, not a bootstrap
        ("input", "VDD", False),
    ],
)
def test_is_power_rail_pin(pintype: str, pin_name: str, expected: bool) -> None:
    assert is_power_rail_pin(pintype, pin_name) is expected


def test_classify_net_switch_node_is_not_power() -> None:
    from kicad_tools.router.net_class import NetClass, classify_net
    from kicad_tools.schematic.models.pin import Pin

    sw = Pin(name="SW", number="2", x=0, y=0, angle=0, length=2.54, pin_type="power_out")
    vo = Pin(name="VO", number="1", x=0, y=0, angle=0, length=2.54, pin_type="power_out")
    assert classify_net("SW", [("U2", sw)]).net_class is NetClass.HIGH_CURRENT_SIGNAL
    assert classify_net("+3V3", [("U4", vo)]).net_class is NetClass.POWER
