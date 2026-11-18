"""Read-only replay of the merge-queue admission condition (issue #9413).

The merge-group run of ``Changelog Fragment Check`` only certifies that the
COMBINED tree's fragments assemble; the per-PR gap report is a pull_request-only
verdict.  A queue is therefore safe only if every proposed member has a
*successful* ``Changelog Fragment Check`` at its current head and with its
current metadata.  These tests replay that rule over fixtures: no network, no
GitHub state.  See ``changelog.d/README.md`` ("Merge-queue admission").
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_NAME = "Changelog Fragment Check"
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/changelog.yml").read_text())
JOB = WORKFLOW["jobs"]["fragment-check"]


def _load_gap_report() -> Any:
    path = ROOT / "scripts" / "changelog_gap_report.py"
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("changelog_gap_report_9413", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gap = _load_gap_report()


# --- check-run replay -------------------------------------------------------


@dataclass
class CheckRun:
    """One check run as the branch check rollup sees it (latest per name wins)."""

    name: str
    conclusion: str  # success | failure | skipped | cancelled


def rollup(runs: list[CheckRun]) -> dict[str, str]:
    """Latest run per check name, regardless of conclusion (issue #8434)."""
    latest: dict[str, str] = {}
    for run in runs:
        latest[run.name] = run.conclusion
    return latest


def admitted(runs: list[CheckRun]) -> bool:
    """The queue's required-status-check condition for one member at its head."""
    return rollup(runs).get(REQUIRED_NAME) == "success"


def _evaluate(expression: str, context: dict[str, Any]) -> Any:
    from tests.test_changelog_workflow import _evaluate as evaluate

    return evaluate(expression, context)


def _name_for_label_event(label: str) -> str:
    """Check-run name GitHub records for a label event (unevaluated if skipped)."""
    event = {"action": "labeled", "label": {"name": label}}
    ctx = {"github": {"event": event}}
    job_if = JOB["if"][3:-2].strip()
    name_expr = JOB["name"]
    if _evaluate(job_if, ctx):
        return _evaluate(name_expr[3:-2].strip(), ctx)
    return name_expr  # `if:`-skipped: the expression text is left unevaluated


def _verdict(title: str, body: str, labels: list[str], documented: set[int], frags: list[str]):
    pr = gap.PullRequest(number=1, title=title, body=body, labels=labels, head_ref="feature/x")
    return gap.evaluate_pr(pr, documented, frags)


# --- fixtures: a passing member and a member missing its fragment -----------

PASSING = {
    "title": "fix: a thing",
    "body": "Closes #100",
    "labels": [],
    "documented": {100},
    "frags": [],
}
FAILING = {
    "title": "fix: other thing",
    "body": "Closes #200",
    "labels": [],
    "documented": set(),
    "frags": [],
}


def test_passing_member_is_admitted() -> None:
    assert _verdict(**PASSING).ok
    assert admitted([CheckRun(REQUIRED_NAME, "success")])


def test_member_missing_fragment_fails_even_if_combined_tree_assembles() -> None:
    verdict = _verdict(**FAILING)
    assert not verdict.ok and "no changelog fragment" in verdict.reason
    # The merge-group run for a group holding this member would still be green
    # (tree-level validation only), so the PR-level check is what blocks it.
    group_run = CheckRun(REQUIRED_NAME, "success")
    pr_run = CheckRun(REQUIRED_NAME, "failure")
    assert admitted([group_run]) is True  # the gap: group run alone proves nothing
    assert admitted([pr_run]) is False  # the member's own PR check blocks admission


def test_group_with_both_members_admits_only_the_passing_one() -> None:
    members = {"pr-passing": PASSING, "pr-failing": FAILING}
    admission = {name: _verdict(**kw).ok for name, kw in members.items()}
    assert admission == {"pr-passing": True, "pr-failing": False}
    assert not all(admission.values())  # the group cannot be formed as a whole


# --- metadata edits at an unchanged head -----------------------------------


def test_title_edit_without_moving_head_changes_the_verdict() -> None:
    # `edited` re-runs the job (it is in `on.pull_request.types` and not a label
    # event), so a title flip from internal to user-visible must flip the check.
    assert "edited" in WORKFLOW[True]["pull_request"]["types"]
    internal = dict(FAILING, title="chore: tidy")
    assert _verdict(**internal).ok
    assert not _verdict(**FAILING).ok


def test_body_edit_changes_the_resolved_issue() -> None:
    cited = dict(FAILING, body="Closes #100", documented={100})
    assert _verdict(**cited).ok
    assert not _verdict(**dict(cited, body="Closes #200")).ok


def test_skip_toggle_without_moving_head_reruns_and_flips_the_verdict() -> None:
    assert _name_for_label_event("changelog:skip") == REQUIRED_NAME
    assert _verdict(**dict(FAILING, labels=["changelog:skip"])).ok
    assert not _verdict(**FAILING).ok  # label removed -> `unlabeled` re-run fails
    # Replay: fail, then skip applied (pass), then skip removed (fail again).
    runs = [CheckRun(REQUIRED_NAME, "failure")]
    assert not admitted(runs)
    runs.append(CheckRun(REQUIRED_NAME, "success"))
    assert admitted(runs)
    runs.append(CheckRun(REQUIRED_NAME, "failure"))
    assert not admitted(runs)


def test_unrelated_skipped_label_event_cannot_satisfy_the_gate() -> None:
    name = _name_for_label_event("loom:review-requested")
    assert name != REQUIRED_NAME
    runs = [CheckRun(REQUIRED_NAME, "failure"), CheckRun(name, "skipped")]
    assert not admitted(runs)
    # A skipped run under the required name WOULD supersede the failure and is
    # not "successful"; neither may admit a member.
    assert not admitted([CheckRun(REQUIRED_NAME, "skipped")])
    assert not admitted([CheckRun(REQUIRED_NAME, "cancelled")])
    # With no run at all (e.g. a head with no applicable check) nothing admits.
    assert not admitted([])


def test_documented_acceptance_probe_matches_the_workflow() -> None:
    text = (ROOT / "changelog.d" / "README.md").read_text()
    assert "Merge-queue admission" in text
    assert f"`{REQUIRED_NAME}`" in text
    assert "merge-group-ci audit" in text
