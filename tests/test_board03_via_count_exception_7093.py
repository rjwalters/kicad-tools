"""The #7093 diff-pair exception covers exactly one finding (Issue #6394).

``tests/board03_via_count_exception_7093.py`` lets four committed-board tests
tolerate board 03's USB_D via-count-mismatch warning while router bug #7093 is
open.  These tests pin that the exception is that narrow: any variation of the
finding -- another board, pair, rule, sub-check, severity or count -- is NOT
excepted, and ``diffpair_uncoupled_length`` is not on any allowlist.  The
first test fails once the router stops producing the finding, which is the
signal to delete the exception (and this file).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from kicad_tools.router.rules import net_class_map_from_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.diffpair_uncoupled_length import (
    PER_TERMINAL_PHRASE,
    VIA_OVER_LIMIT_PHRASE,
    is_via_count_finding,
)
from tests.board03_via_count_exception_7093 import (
    BOARD,
    MESSAGE,
    TRACKING_ISSUE,
    is_excepted,
    without_exception,
)

REPO = Path(__file__).resolve().parent.parent
BOARD06 = "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb"


def _board03_findings() -> list:
    ncm = net_class_map_from_dict(
        json.loads((REPO / "boards/03-usb-joystick/output/net_class_map.json").read_text())
    )
    checker = DRCChecker(
        PCB.load(str(REPO / BOARD)), manufacturer="jlcpcb", layers=4, net_class_map=ncm
    )
    return [
        v
        for v in checker.check_diffpair_uncoupled_length().violations
        if v.severity in ("error", "warning")
    ]


@pytest.fixture(scope="module")
def finding():
    findings = _board03_findings()
    excepted = [v for v in findings if is_excepted(BOARD, v)]
    assert len(excepted) == 1, (
        f"board 03 no longer has the USB_D via-count warning (#{TRACKING_ISSUE} landed?): "
        "delete tests/board03_via_count_exception_7093.py, its callers' filters and this file"
    )
    return excepted[0]


def test_committed_board03_has_exactly_the_one_excepted_finding(finding) -> None:
    assert finding.message == MESSAGE
    assert is_via_count_finding(finding)
    assert without_exception(BOARD, _board03_findings()) == []


@pytest.mark.parametrize(
    "board",
    [BOARD06, "boards/03-usb-joystick/regression-fixture/usb_joystick_routed.kicad_pcb"],
)
def test_other_boards_are_not_excepted(finding, board) -> None:
    assert not is_excepted(board, finding)
    assert without_exception(board, [finding]) == [finding]


@pytest.mark.parametrize(
    "change",
    [
        {"severity": "error"},
        {"rule_id": "diffpair_length_skew"},
        {"rule_id": "diffpair_routing_continuity"},
        # Other sub-checks of the same rule on the same pair.
        {"message": f"Differential pair USB_D+/USB_D-: {PER_TERMINAL_PHRASE} 7.0 mm"},
        {"message": f"Differential pair USB_D+/USB_D-: net USB_D- has 3 {VIA_OVER_LIMIT_PHRASE} 2"},
        {"message": "Differential pair USB_D+/USB_D- uncoupled length 20.0 mm exceeds 19.05 mm"},
        # Any other via counts.
        {"message": MESSAGE.replace("USB_D-: 3", "USB_D-: 4")},
        {"message": MESSAGE.replace("USB_D+: 2", "USB_D+: 3").replace("USB_D-: 3", "USB_D-: 2")},
        # Another pair.
        {
            "message": MESSAGE.replace("USB_D", "USB_MCU_D"),
            "nets": ("USB_MCU_D+", "USB_MCU_D-"),
            "items": ("USB_MCU_D+", "USB_MCU_D-"),
        },
        {"nets": ("USB_MCU_D+", "USB_MCU_D-")},
        {"items": ("USB_MCU_D+", "USB_MCU_D-")},
    ],
)
def test_any_variation_of_the_finding_is_not_excepted(finding, change) -> None:
    variant = dataclasses.replace(finding, **change)
    assert not is_excepted(BOARD, variant)
    assert without_exception(BOARD, [finding, variant]) == [variant]


def test_rule_stays_off_the_board03_allowlist() -> None:
    from tests.router.test_board03_routing_baseline import EXPECTED_COMMITTED_DRC_RULES

    assert "diffpair_uncoupled_length" not in EXPECTED_COMMITTED_DRC_RULES
