"""Tests for scripts/ci/check_pr_checks_present.py (issue #5701).

Covers the three merge-gate states: absent (refuse), failing (refuse),
passing (allow), plus pending and the --allow-absent override.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ci" / "check_pr_checks_present.py"
_spec = importlib.util.spec_from_file_location("check_pr_checks_present", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

OK = {"status": "COMPLETED", "conclusion": "SUCCESS"}
BAD = {"status": "COMPLETED", "conclusion": "FAILURE"}
RUN = {"status": "IN_PROGRESS", "conclusion": ""}


def _run(tmp_path, payload, *extra):
    f = tmp_path / "r.json"
    f.write_text(json.dumps(payload))
    return mod.main(["--json", str(f), *extra])


@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        ([], "absent"),
        (None, "absent"),
        ([OK, BAD], "failing"),
        ([OK, RUN], "pending"),
        ([OK, {"status": "COMPLETED", "conclusion": "SKIPPED"}], "passing"),
        ([{"state": "SUCCESS"}], "passing"),
        ([{"state": "FAILURE"}], "failing"),
    ],
)
def test_classify(rollup, expected):
    assert mod.classify(rollup) == expected


def test_absent_on_stacked_pr_refuses(tmp_path, capsys):
    rc = _run(tmp_path, {"baseRefName": "feature/issue-1", "statusCheckRollup": []})
    assert rc == 3
    assert "never ran" in capsys.readouterr().out


def test_absent_allowed_with_override(tmp_path, capsys):
    rc = _run(
        tmp_path, {"baseRefName": "feature/issue-1", "statusCheckRollup": []}, "--allow-absent"
    )
    assert rc == 0
    assert "::warning::" in capsys.readouterr().out


CI_OK = {**OK, "workflowName": "CI"}
DRIFT_OK = {**OK, "workflowName": "Ecosystem Drift"}


def test_classify_requires_workflow():
    assert mod.classify([DRIFT_OK], "CI") == "absent"
    assert mod.classify([DRIFT_OK, CI_OK], "CI") == "passing"
    assert mod.classify([DRIFT_OK], None) == "passing"


def test_stacked_pr_with_only_path_filtered_check_refuses(tmp_path, capsys):
    """ecosystem-drift.yml runs on stacked PRs; it must not count as CI (#5701)."""
    rc = _run(tmp_path, {"baseRefName": "feature/issue-1", "statusCheckRollup": [DRIFT_OK]})
    assert rc == 3
    out = capsys.readouterr().out
    assert "'CI' WORKFLOW" in out
    assert "never triggered" in out


def test_stacked_pr_with_ci_checks_passes(tmp_path):
    payload = {"baseRefName": "feature/issue-1", "statusCheckRollup": [DRIFT_OK, CI_OK]}
    assert _run(tmp_path, payload) == 0


def test_main_base_does_not_require_ci_workflow(tmp_path):
    assert _run(tmp_path, {"baseRefName": "main", "statusCheckRollup": [DRIFT_OK]}) == 0


def test_required_workflow_can_be_disabled(tmp_path):
    payload = {"baseRefName": "feature/issue-1", "statusCheckRollup": [DRIFT_OK]}
    assert _run(tmp_path, payload, "--required-workflow", "") == 0


def test_failing_refuses(tmp_path):
    assert _run(tmp_path, {"baseRefName": "main", "statusCheckRollup": [OK, BAD]}) == 2


def test_passing_allows(tmp_path):
    assert _run(tmp_path, {"baseRefName": "main", "statusCheckRollup": [OK]}) == 0


def test_missing_input_is_usage_error(tmp_path):
    assert mod.main(["--json", str(tmp_path / "nope.json")]) == 1
    assert mod.main([]) == 1
