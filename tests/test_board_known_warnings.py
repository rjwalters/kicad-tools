"""Issue #7298: board 04/05 gates accept ONLY their exact known warnings.

#7220's ``thermal_asymmetry`` / ``via_at_pad_edge`` advisories turned main CI
red on boards 04 and 05, whose gates fail on any warning.  Each board pins the
exact ``(rule, ref, pad)`` findings its source fix (#7295 / #7296) removes.
Board 04's source fix (#7295) landed, so its pinned set is empty; board 05
still pins its #7296 findings.  These tests prove that the pinned set passes
and that any new warning, a different ref or pad, a repeat (higher count) or
any error still fails.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from kicad_tools.validate.known_warnings import (
    unaccepted_report_warnings,
    unaccepted_warnings,
    warning_key,
)
from kicad_tools.validate.violations import DRCViolation

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD04 = REPO_ROOT / "boards/04-stm32-devboard"
BOARD05_REDESIGN = REPO_ROOT / "boards/05-bldc-motor-controller/redesign"


def _load(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


@pytest.fixture(scope="module")
def board04():
    return _load(BOARD04 / "check_manufacturing.py", "_board04_check_manufacturing_7298")


@pytest.fixture(scope="module")
def board05():
    return _load(BOARD05_REDESIGN / "build.py", "_board05_build_7298")


# --------------------------------------------------------------------- helpers
def _thermal(ref: str, pad: int = 1, severity: str = "warning") -> DRCViolation:
    other = 2 if pad == 1 else 1
    return DRCViolation(
        rule_id="thermal_asymmetry",
        severity=severity,
        message="thermal asymmetry",
        items=(ref, f"{ref}-{pad}", f"{ref}-{other}"),
    )


def _via_edge(pad: str, x: float = 1.0) -> dict:
    return {
        "rule_id": "via_at_pad_edge",
        "severity": "warning",
        "waived": False,
        "items": [f"Via@{x}/2.0:F.Cu-B.Cu:d0.3/s0.6", pad],
    }


def _report(violations: list[dict], errors: int = 0) -> dict:
    warnings = sum(1 for v in violations if v["severity"] == "warning" and not v.get("waived"))
    return {"summary": {"errors": errors, "warnings": warnings}, "violations": violations}


def test_warning_key_layouts():
    assert warning_key(_thermal("C1")) == ("thermal_asymmetry", "C1", "C1-1")
    assert warning_key(_via_edge("R11-1")) == ("via_at_pad_edge", "R11", "R11-1")
    # A rule whose items layout is not modelled never matches an allowance.
    assert warning_key({"rule_id": "silk_over_pad", "items": ["C1", "C1-1"]}) is None


def test_waived_and_info_findings_are_ignored():
    known: dict = {}
    waived = {**_via_edge("R11-1"), "waived": True}
    info = {**_via_edge("R11-1"), "severity": "info"}
    assert unaccepted_warnings([waived, info], known) == []


# --------------------------------------------------------------------- board 04
def test_board04_known_set_is_empty(board04):
    # Issue #7295 removed the nine 0805 ``thermal_asymmetry`` findings at the
    # source (narrower thermal spokes), so nothing is accepted any more.
    assert board04.BOARD04_KNOWN_WARNINGS == {}


def test_board04_clean_board_passes(board04):
    verdict = board04.drc_verdict([])
    assert verdict.status == "PASSED", verdict.detail
    assert verdict.detail == "0 errors, 0 warnings"


@pytest.mark.parametrize(
    "finding",
    [
        _thermal("C1"),  # the formerly accepted findings now fail
        _thermal("R1"),
        _thermal("C4"),
        DRCViolation(
            rule_id="via_at_pad_edge", severity="warning", message="m", items=("v", "C1-1")
        ),
        DRCViolation(rule_id="silk_over_pad", severity="warning", message="m", items=("C1",)),
    ],
)
def test_board04_any_warning_fails(board04, finding):
    verdict = board04.drc_verdict([finding])
    assert verdict.status == "FAILED", verdict.detail
    assert "unaccepted" in verdict.detail


def test_board04_error_still_fails(board04):
    error = DRCViolation(rule_id="clearance", severity="error", message="m")
    assert board04.drc_verdict([error]).status == "FAILED"


# --------------------------------------------------------------------- board 05
def test_board05_accepts_no_warning(board05):
    assert board05.BOARD05_KNOWN_WARNINGS == {}
    assert board05.manufacturer_check_failures(_report([])) == []


@pytest.mark.parametrize(
    "extra",
    [
        _via_edge("R11-1"),  # the findings #7296 removed stay fatal
        _via_edge("R9-1"),
        _via_edge("D1-1"),
        _via_edge("R10-1"),
        {"rule_id": "thermal_asymmetry", "severity": "warning", "items": ["R11", "R11-1"]},
        {"rule_id": "silk_over_pad", "severity": "warning", "items": ["R11"]},
    ],
)
def test_board05_any_warning_fails(board05, extra):
    assert board05.manufacturer_check_failures(_report([extra]))


def test_board05_error_still_fails(board05):
    assert board05.manufacturer_check_failures(_report([], errors=1)) == ["1 error(s)"]


def test_board05_summary_disagreeing_with_findings_fails(board05):
    report = _report([])
    report["summary"]["warnings"] = 1  # a warning the finding list does not show
    assert board05.manufacturer_check_failures(report)
    assert unaccepted_report_warnings(report, board05.BOARD05_KNOWN_WARNINGS)
