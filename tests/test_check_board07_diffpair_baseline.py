"""Tests for scripts/ci/check_board07_diffpair_baseline.py (issue #7795)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/check_board07_diffpair_baseline.py"
_spec = importlib.util.spec_from_file_location("check_board07_diffpair_baseline", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _report(tmp_path: Path, clearance=0, opens=0, skew=0) -> Path:
    data = {
        "violations": (
            [{"type": "clearance", "description": "c", "items": []}] * clearance
            + [{"type": "skew_out_of_range", "description": "DQS 4.655 mm", "items": []}] * skew
        ),
        "unconnected_items": [{"type": "unconnected_items"}] * opens,
    }
    path = tmp_path / "drc.json"
    path.write_text(json.dumps(data))
    return path


def _baseline(tmp_path: Path, name: str, **values) -> Path:
    vals = {"copper_safety": 0, "opens": 0, "skew_out_of_range": 0} | values
    path = tmp_path / name
    lines = "\n".join(f"  {k}: {'null' if v is None else v}" for k, v in vals.items())
    path.write_text(f"baseline:\n{lines}\n")
    return path


def _run(report: Path, baseline: Path, previous: Path | None = None) -> int:
    argv = [str(report), "--baseline-file", str(baseline)]
    if previous is not None:
        argv += ["--previous-baseline-file", str(previous)]
    return mod.main(argv)


def test_within_baseline_passes(tmp_path):
    base = _baseline(tmp_path, "b.yml", copper_safety=1, opens=2, skew_out_of_range=6)
    assert _run(_report(tmp_path, clearance=1, opens=2, skew=6), base) == 0


@pytest.mark.parametrize("metric", ["clearance", "opens", "skew"])
def test_any_metric_above_baseline_fails(tmp_path, metric):
    base = _baseline(tmp_path, "b.yml")
    assert _run(_report(tmp_path, **{metric: 1}), base) == 2


def test_null_baseline_reports_but_does_not_gate(tmp_path, capsys):
    base = _baseline(tmp_path, "b.yml", copper_safety=None, opens=None, skew_out_of_range=None)
    assert _run(_report(tmp_path, clearance=3, opens=4, skew=5), base) == 0
    assert "opens: 4 (no baseline yet" in capsys.readouterr().out


def test_skew_text_is_printed(tmp_path, capsys):
    base = _baseline(tmp_path, "b.yml", skew_out_of_range=1)
    _run(_report(tmp_path, skew=1), base)
    assert "4.655 mm" in capsys.readouterr().out


@pytest.mark.parametrize("text", [None, "", "{not json", "{}", '{"violations": 3}'])
def test_untrustworthy_report_is_tool_failure(tmp_path, text):
    path = tmp_path / "drc.json"
    if text is not None:
        path.write_text(text)
    assert _run(path, _baseline(tmp_path, "b.yml")) == 1


def test_bad_baseline_is_tool_failure(tmp_path):
    bad = tmp_path / "b.yml"
    bad.write_text("baseline:\n  copper_safety: -1\n")
    assert _run(_report(tmp_path), bad) == 1


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [(2, 1, 0), (2, 2, 0), (1, 2, 3), (1, None, 3), (None, 5, 0)],
)
def test_baseline_may_only_shrink(tmp_path, previous, current, expected):
    prev = _baseline(tmp_path, "prev.yml", opens=previous)
    cur = _baseline(tmp_path, "cur.yml", opens=current)
    assert _run(_report(tmp_path, opens=0), cur, prev) == expected
