"""Tests for scripts/ci/check_copper_safety_drc.py (issue #6738)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/check_copper_safety_drc.py"
_spec = importlib.util.spec_from_file_location("check_copper_safety_drc", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _violation(vtype: str) -> dict:
    return {
        "type": vtype,
        "description": f"{vtype} (0.19 mm)",
        "items": [{"description": "Track [+1V2]", "pos": {"x": 1.5, "y": 2.5}}],
    }


def _run(tmp_path: Path, report: str | None, floor: int, capsys) -> tuple[int, str]:
    floor_file = tmp_path / "floor.yml"
    floor_file.write_text(f"floors:\n  board-07-fresh-regen: {floor}\n")
    path = tmp_path / "drc.json"
    if report is not None:
        path.write_text(report)
    code = mod.main([str(path), "--floor-file", str(floor_file)])
    return code, capsys.readouterr().out


def test_clean_report_passes(tmp_path, capsys):
    code, _ = _run(tmp_path, json.dumps({"violations": []}), 0, capsys)
    assert code == 0


def test_clearance_fails_at_floor_zero_and_prints_details(tmp_path, capsys):
    report = json.dumps({"violations": [_violation("clearance")]})
    code, out = _run(tmp_path, report, 0, capsys)
    assert code == 2
    assert "Track [+1V2]" in out and "1.5" in out and "2.5" in out


def test_clearance_passes_at_floor_one(tmp_path, capsys):
    report = json.dumps({"violations": [_violation("clearance")]})
    code, _ = _run(tmp_path, report, 1, capsys)
    assert code == 0


def test_skew_is_ignored(tmp_path, capsys):
    report = json.dumps({"violations": [_violation("skew_out_of_range")] * 5})
    code, _ = _run(tmp_path, report, 0, capsys)
    assert code == 0


@pytest.mark.parametrize("report", [None, "", "   \n", '{"violations": [', "{}", "[]"])
def test_untrustworthy_report_is_tool_failure(tmp_path, capsys, report):
    code, _ = _run(tmp_path, report, 99, capsys)
    assert code == 1


def test_bad_floor_is_tool_failure(tmp_path, capsys):
    path = tmp_path / "drc.json"
    path.write_text(json.dumps({"violations": []}))
    bad = tmp_path / "floor.yml"
    bad.write_text("floors: {}\n")
    assert mod.main([str(path), "--floor-file", str(bad)]) == 1


def _floor_file(tmp_path: Path, name: str, floor: int) -> Path:
    path = tmp_path / name
    path.write_text(f"floors:\n  board-07-fresh-regen: {floor}\n")
    return path


@pytest.mark.parametrize(("previous", "current", "expected"), [(1, 1, 0), (1, 0, 0), (0, 1, 3)])
def test_floor_may_only_shrink(tmp_path, capsys, previous, current, expected):
    report = tmp_path / "drc.json"
    report.write_text(json.dumps({"violations": []}))
    argv = [
        str(report),
        "--floor-file",
        str(_floor_file(tmp_path, "floor.yml", current)),
        "--previous-floor-file",
        str(_floor_file(tmp_path, "prev.yml", previous)),
    ]
    assert mod.main(argv) == expected
    if expected == 3:
        assert "may only go down" in capsys.readouterr().out


def test_unreadable_previous_floor_is_tool_failure(tmp_path, capsys):
    report = tmp_path / "drc.json"
    report.write_text(json.dumps({"violations": []}))
    argv = [
        str(report),
        "--floor-file",
        str(_floor_file(tmp_path, "floor.yml", 1)),
        "--previous-floor-file",
        str(tmp_path / "missing.yml"),
    ]
    assert mod.main(argv) == 1
