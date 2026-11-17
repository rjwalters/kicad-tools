#!/usr/bin/env python3
"""Pre-merge gate: tell "no checks ran" apart from "checks passed" (issue #5701).

``.github/workflows/ci.yml`` (and ``changelog.yml``) only trigger
``pull_request`` for PRs whose *base* is ``main``.  A stacked PR (base =
``feature/issue-N``) therefore gets none of the ``ci.yml`` gates, and GitHub
reports ``mergeStateStatus: CLEAN`` -- which means "never ran", not "passed".

The rollup is not necessarily empty, though: ``ecosystem-drift.yml`` has a
``paths:``-only ``pull_request`` trigger (no ``branches:`` filter), so a
stacked PR touching the ecosystem registry gets that one check.  An "any
check passed" test would wrongly report ``passing`` there.  So when the base
is not ``main`` the script also requires at least one rollup entry from the
``CI`` workflow (``--required-workflow``); without one the rollup counts as
absent.

This script classifies a PR's check rollup into four states so a merge path
can refuse the first one:

    absent   -- zero checks reported, or (base != main) no check from
                the required ``CI`` workflow (the stacked-PR blind spot) -> exit 3
    failing  -- at least one check concluded unsuccessfully        -> exit 2
    pending  -- checks exist but some have not finished            -> exit 2
    passing  -- every check succeeded / was skipped / neutral      -> exit 0

Exit 1 is a usage / ``gh`` error.  ``--allow-absent`` downgrades ``absent``
to a loud warning (exit 0) for an operator who has verified the change
locally (record that evidence in the PR, as #5699 did).

Usage::

    scripts/ci/check_pr_checks_present.py <pr-number> [--repo OWNER/REPO]
    scripts/ci/check_pr_checks_present.py --json rollup.json   # offline

Why this lives here and not in ``.loom/scripts/merge-pr.sh``: that script is
Loom-managed (shipped from Loom's ``defaults/`` and overwritten on every
resync), so a local edit would be silently lost.  Run this before
``merge-pr.sh`` on any PR whose base is not ``main``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

OK_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
DEFAULT_REQUIRED_WORKFLOW = "CI"  # ``name:`` of .github/workflows/ci.yml
PENDING_STATES = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}


def _outcome(item: dict) -> str:
    """Return 'ok', 'pending' or 'fail' for one rollup entry (check run or status)."""
    # CheckRun: status + conclusion.  StatusContext: state only.
    status = (item.get("status") or "").upper()
    conclusion = (item.get("conclusion") or "").upper()
    state = (item.get("state") or "").upper()
    if status and status != "COMPLETED":
        return "pending"
    if conclusion:
        return "ok" if conclusion in OK_CONCLUSIONS else "fail"
    if state:
        if state == "SUCCESS":
            return "ok"
        return "pending" if state in PENDING_STATES else "fail"
    return "pending"


def has_workflow(rollup: list[dict] | None, workflow: str) -> bool:
    """True if any rollup entry came from the GitHub Actions workflow *workflow*."""
    return any((i.get("workflowName") or "") == workflow for i in rollup or [])


def classify(rollup: list[dict] | None, required_workflow: str | None = None) -> str:
    """Classify a ``statusCheckRollup`` list as absent/failing/pending/passing.

    With *required_workflow*, a rollup containing no entry from that workflow
    is ``absent`` even if other (e.g. path-filtered) checks ran.
    """
    if not rollup:
        return "absent"
    if required_workflow and not has_workflow(rollup, required_workflow):
        return "absent"
    outcomes = {_outcome(i) for i in rollup}
    if "fail" in outcomes:
        return "failing"
    if "pending" in outcomes:
        return "pending"
    return "passing"


def _load(args: argparse.Namespace) -> tuple[list[dict], str]:
    if args.json:
        with open(args.json) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data, ""
        return data.get("statusCheckRollup") or [], data.get("baseRefName", "")
    cmd = ["gh", "pr", "view", str(args.pr), "--json", "statusCheckRollup,baseRefName"]
    if args.repo:
        cmd += ["--repo", args.repo]
    data = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    return data.get("statusCheckRollup") or [], data.get("baseRefName", "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("pr", nargs="?", help="PR number")
    p.add_argument("--repo")
    p.add_argument("--json", help="read a statusCheckRollup JSON file instead of calling gh")
    p.add_argument(
        "--required-workflow",
        default=DEFAULT_REQUIRED_WORKFLOW,
        help="when base != main, require a check from this workflow name "
        f"(default: {DEFAULT_REQUIRED_WORKFLOW!r}; '' disables)",
    )
    p.add_argument(
        "--allow-absent", action="store_true", help="warn instead of failing when no checks ran"
    )
    args = p.parse_args(argv)
    if not args.pr and not args.json:
        p.print_usage(sys.stderr)
        return 1
    try:
        rollup, base = _load(args)
    except (OSError, ValueError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"::error::could not read check rollup: {exc}", file=sys.stderr)
        return 1

    stacked = bool(base) and base != "main"
    required = args.required_workflow if stacked else None
    state = classify(rollup, required)
    if state == "absent":
        if rollup:
            what = (
                f"NO '{required}' WORKFLOW CHECKS RAN for this PR ({len(rollup)} other "
                "check(s) did, e.g. a path-filtered workflow, but none of ci.yml's gates)"
            )
        else:
            what = "NO CHECKS RAN for this PR"
        msg = (
            what
            + (f" (base '{base}' is not 'main', so ci.yml never triggered)" if stacked else "")
            + ". mergeStateStatus CLEAN here means 'never ran', not 'passed'. "
            "Verify locally and record the evidence, or merge via the parent PR."
        )
        if args.allow_absent:
            print(f"::warning::{msg} (--allow-absent set)")
            return 0
        print(f"::error::{msg}")
        return 3
    if state in ("failing", "pending"):
        print(f"::error::checks are {state}")
        return 2
    print("checks passing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
