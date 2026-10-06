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
from kicad_tools.router.net_class import is_power_rail_name
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"

# The five names from the issue: signals that merely *contain* "+" or a
# voltage fragment, and a KiCad auto-named no-connect net.
NOT_POWER = [
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
    # Voltage token plus qualifier, P-/V-prefixed voltages, aliases (review of #5971)
    "3V3_MCU",
    "5V_USB",
    "12V_IN",
    "3V3_LDO",
    "P3V3",
    "P5V",
    "P12V",
    "V3V3",
    "V5V0",
    "VAA",
    "VUSB",
    "VOUT_PRE",
    "VIN_FILT",
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
