"""Tests for scripts/ci/compare_model_coverage.py (issue #7689)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ci" / "compare_model_coverage.py"
_spec = importlib.util.spec_from_file_location("compare_model_coverage", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

REPORT = {
    "boards": [
        {
            "board": "03-usb",
            "model_check": {
                "status": "partial",
                "coverage": {
                    "env": "host",
                    "covered": 9,
                    "total": 10,
                    "percent": 90.0,
                    "counts": {"covered": 9, "no_ref": 1},
                },
                "uncovered_models": [
                    {"reference": "J2", "class": "no_ref", "path": "/host/models/j2.step"}
                ],
                "unresolved_models": [],
            },
        }
    ]
}


def _run(tmp_path, host, pinned):
    h, p = tmp_path / "h.json", tmp_path / "p.json"
    h.write_text(json.dumps(host))
    p.write_text(json.dumps(pinned))
    return mod.main([str(h), str(p)])


def test_identical_ignoring_env_and_paths(tmp_path):
    pinned = copy.deepcopy(REPORT)
    check = pinned["boards"][0]["model_check"]
    check["coverage"]["env"] = "pinned"
    check["uncovered_models"][0]["path"] = "/pinned/models/j2.step"
    assert _run(tmp_path, REPORT, pinned) == 0


def test_percent_difference_fails(tmp_path, capsys):
    pinned = copy.deepcopy(REPORT)
    pinned["boards"][0]["model_check"]["coverage"]["percent"] = 100.0
    assert _run(tmp_path, REPORT, pinned) == 1
    assert "03-usb: coverage differs" in capsys.readouterr().out


def test_uncovered_ref_mismatch_fails(tmp_path, capsys):
    pinned = copy.deepcopy(REPORT)
    pinned["boards"][0]["model_check"]["uncovered_models"][0]["reference"] = "J1"
    assert _run(tmp_path, REPORT, pinned) == 1
    assert "uncovered differs" in capsys.readouterr().out


def test_board_missing_from_one_side_fails(tmp_path, capsys):
    assert _run(tmp_path, REPORT, {"boards": []}) == 1
    assert "present only in host" in capsys.readouterr().out


def test_unreadable_report_is_usage_error(tmp_path):
    assert mod.main([str(tmp_path / "nope.json"), str(tmp_path / "nope2.json")]) == 2
