#!/usr/bin/env python3
"""Pre-merge gate: tell "no checks ran" apart from "checks passed" (issue #5701).

``.github/workflows/ci.yml`` only triggers ``pull_request`` for PRs whose
*base* is ``main``.  A stacked PR (base = ``feature/issue-N``) therefore gets
no CI at all, and GitHub reports ``mergeStateStatus: CLEAN`` -- which means
"never ran", not "passed".  This script classifies a PR's check rollup into
four states so a merge path can refuse the first one:

    absent   -- zero checks reported (the stacked-PR blind spot)   -> exit 3
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


def classify(rollup: list[dict] | None) -> str:
    """Classify a ``statusCheckRollup`` list as absent/failing/pending/passing."""
    if not rollup:
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

    state = classify(rollup)
    stacked = bool(base) and base != "main"
    if state == "absent":
        msg = (
            "NO CHECKS RAN for this PR"
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
