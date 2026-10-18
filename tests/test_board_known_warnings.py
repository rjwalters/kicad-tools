"""Issue #7298: board 04/05 gates accept ONLY their exact known warnings.

#7220's ``thermal_asymmetry`` / ``via_at_pad_edge`` advisories turned main CI
red on boards 04 and 05, whose gates fail on any warning.  Each board pins the
exact ``(rule, ref, pad)`` findings its source fix (#7295 / #7296) removes.
These tests prove that the pinned set passes and that any new warning, a
different ref or pad, a repeat (higher count) or any error still fails.
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

BOARD04_REFS = ("C1", "C2", "C3", "C12", "C13", "C14", "C15", "C16", "R1")
BOARD05_PADS = ("R11-1", "R9-1", "D1-1")


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
def test_board04_known_set_is_exact(board04):
    assert set(board04.BOARD04_KNOWN_WARNINGS) == {
        ("thermal_asymmetry", ref, f"{ref}-1") for ref in BOARD04_REFS
    }
    assert set(board04.BOARD04_KNOWN_WARNINGS.values()) == {"#7295"}


def test_board04_known_warnings_pass(board04):
    verdict = board04.drc_verdict([_thermal(ref) for ref in BOARD04_REFS])
    assert verdict.status == "PASSED", verdict.detail
    assert verdict.detail == "0 errors, 9 warnings (9 known, #7295)"
    # A partial source fix (fewer findings) still passes; none at all is clean.
    assert board04.drc_verdict([_thermal("C1")]).status == "PASSED"
    assert board04.drc_verdict([]).detail == "0 errors, 0 warnings"


@pytest.mark.parametrize(
    "extra",
    [
        _thermal("C4"),  # a different ref
        _thermal("C1", pad=2),  # a different pad of a known ref
        _thermal("C1"),  # a repeat: higher count than pinned
        DRCViolation(
            rule_id="via_at_pad_edge", severity="warning", message="m", items=("v", "C1-1")
        ),
        DRCViolation(rule_id="silk_over_pad", severity="warning", message="m", items=("C1",)),
    ],
)
def test_board04_any_other_warning_fails(board04, extra):
    verdict = board04.drc_verdict([_thermal(ref) for ref in BOARD04_REFS] + [extra])
    assert verdict.status == "FAILED", verdict.detail
    assert "unaccepted" in verdict.detail


def test_board04_error_still_fails(board04):
    findings = [_thermal(ref) for ref in BOARD04_REFS]
    findings.append(DRCViolation(rule_id="clearance", severity="error", message="m"))
    assert board04.drc_verdict(findings).status == "FAILED"


# --------------------------------------------------------------------- board 05
def test_board05_known_set_is_exact(board05):
    assert set(board05.BOARD05_KNOWN_WARNINGS) == {
        ("via_at_pad_edge", pad.rsplit("-", 1)[0], pad) for pad in BOARD05_PADS
    }
    assert set(board05.BOARD05_KNOWN_WARNINGS.values()) == {"#7296"}


def test_board05_known_warnings_pass(board05):
    assert board05.manufacturer_check_failures(_report([_via_edge(p) for p in BOARD05_PADS])) == []
    assert board05.manufacturer_check_failures(_report([_via_edge("D1-1")])) == []
    assert board05.manufacturer_check_failures(_report([])) == []


@pytest.mark.parametrize(
    "extra",
    [
        _via_edge("R10-1"),  # a different ref
        _via_edge("R11-2"),  # a different pad of a known ref
        _via_edge("R11-1", x=9.0),  # a repeat: higher count than pinned
        {"rule_id": "thermal_asymmetry", "severity": "warning", "items": ["R11", "R11-1"]},
        {"rule_id": "silk_over_pad", "severity": "warning", "items": ["R11"]},
    ],
)
def test_board05_any_other_warning_fails(board05, extra):
    report = _report([_via_edge(p) for p in BOARD05_PADS] + [extra])
    assert board05.manufacturer_check_failures(report)


def test_board05_error_still_fails(board05):
    report = _report([_via_edge(p) for p in BOARD05_PADS], errors=1)
    assert board05.manufacturer_check_failures(report) == ["1 error(s)"]


def test_board05_summary_disagreeing_with_findings_fails(board05):
    report = _report([_via_edge(p) for p in BOARD05_PADS])
    report["summary"]["warnings"] = 4  # a warning the finding list does not show
    assert board05.manufacturer_check_failures(report)
    assert unaccepted_report_warnings(report, board05.BOARD05_KNOWN_WARNINGS)
