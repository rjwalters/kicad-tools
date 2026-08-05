#!/usr/bin/env python3
"""Report user-visible commits since a release tag that no CHANGELOG entry cites.

Motivation (issue #4638): between ``v0.19.0`` (2026-07-20) and 2026-08-05 the
``[Unreleased]`` section documented 6 of 87 user-visible commits.  Nothing
surfaced the drift, so it was only discoverable by hand-walking two weeks of
``git log``.  This script turns that walk into one command, so the
CHANGELOG-reconciliation step in ``RELEASING.md`` is mechanical rather than
archaeological.

What it does
------------
1. Walks ``git log <tag>..<head>``.
2. Resolves each commit to the **issue** number(s) it addresses, using a
   three-tier recipe (see :func:`resolve_issue_numbers`).
3. Classifies each commit user-visible vs. internal from its conventional-commit
   subject prefix.
4. Prints the user-visible issue numbers that are **not** mentioned anywhere in
   the CHANGELOG's ``[Unreleased]`` section, and exits non-zero if that set is
   non-empty.

Note that the trailing ``(#NNNN)`` in a squash-merge subject is the **PR**
number, never the issue number -- the CHANGELOG convention in this repo is to
cite issues.  That is exactly why this script exists rather than a one-line
grep.

Usage
-----
    uv run python scripts/changelog_gap_report.py                # since latest v* tag
    uv run python scripts/changelog_gap_report.py --since v0.19.0
    uv run python scripts/changelog_gap_report.py --json
    uv run python scripts/changelog_gap_report.py --offline      # no gh API calls

`--offline` is lossy: without the tier-2 branch lookup, a commit whose body
carries only a `Part of #<epic>` trailer resolves to the epic rather than to its
own issue, so it can report a spurious gap. Prefer the default (networked) mode
when gating a release.

Exit codes
----------
    0 -- no gaps (or no commits since the tag).
    1 -- one or more user-visible issues are undocumented.
    2 -- usage / environment error (bad tag, missing CHANGELOG, git failure).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- classification ---------------------------------------------------------

#: Conventional-commit types whose changes never reach a package consumer.
INTERNAL_TYPES = frozenset(
    {
        "bench",
        "build",
        "chore",
        "ci",
        "config",
        "docs",
        "refactor",
        "revert",
        "style",
        "test",
        "tests",
    }
)

_CONVENTIONAL_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?:")

#: Issues whose commits carry a user-visible-looking subject but change nothing a
#: package consumer can observe.  This is the auditable ledger backing the
#: "classified internal" half of #4638's acceptance criteria -- add an entry (with
#: the rationale) rather than padding the CHANGELOG with a non-user-visible bullet.
INTERNAL_ISSUES: dict[int, str] = {
    4479: "board-05 CI closeout: AST guard test + CI blocking bound, no shipped surface",
}

#: Commits (by SHA prefix) that carry a user-visible-looking subject, resolve to no
#: issue at all, and are repo tooling rather than product.  Keyed by SHA because
#: there is no issue number to key on.
INTERNAL_COMMITS: dict[str, str] = {
    "fcbe6660": "Claude Code permission-rule fix in .claude/settings.json",
    "09f0545c": "Loom 0.18.0 orchestration resync",
}


# --- issue-number resolution ------------------------------------------------

#: GitHub closing keywords.  Anchored to the start of a line so prose such as
#: "...resolve #4506 attach zones in sheet-absolute space" is not mistaken for a
#: closing reference (that exact sentence misattributes commit 771caf16).
_CLOSING_REF = re.compile(
    r"^[ \t]*[-*]?[ \t]*(?:close[sd]?|fixe[sd]?|fix|resolve[sd]?)[ \t:]+#(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)

#: Non-closing references ("Part of #N") -- used only when nothing better exists,
#: since they usually name an epic rather than the commit's own issue.
_PARTIAL_REF = re.compile(
    r"^[ \t]*[-*]?[ \t]*(?:part of|contributes to)[ \t:]+#(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)

#: Trailing "(#NNNN)" in a squash-merge subject -- the PR number.
_PR_IN_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")

#: Loom builders always branch ``feature/issue-<N>``.
_ISSUE_IN_BRANCH = re.compile(r"issue-(\d+)")


@dataclass
class Commit:
    """One commit in the range under review."""

    sha: str
    subject: str
    body: str
    issues: list[int] = field(default_factory=list)
    tier: str = "none"

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    @property
    def is_internal(self) -> bool:
        match = _CONVENTIONAL_SUBJECT.match(self.subject)
        if match is None:
            # Non-conventional subjects ("route/complete Phase 3: ...",
            # "board-05 Phase 3: ...") are real feature work often enough that the
            # safe default is user-visible; the override ledgers demote the rest.
            return False
        return match.group("type") in INTERNAL_TYPES

    @property
    def pr_number(self) -> int | None:
        match = _PR_IN_SUBJECT.search(self.subject)
        return int(match.group(1)) if match else None


class _BranchResolver:
    """Tier-2 resolver: read the issue number out of a merged PR's branch name."""

    def __init__(self, repo: str | None, offline: bool) -> None:
        self._repo = repo
        self._offline = offline
        self._cache: dict[int, int | None] = {}
        self.warned = False

    def issue_for_pr(self, pr_number: int) -> int | None:
        if self._offline or not self._repo:
            return None
        if pr_number in self._cache:
            return self._cache[pr_number]
        result = _run(
            ["gh", "api", f"repos/{self._repo}/pulls/{pr_number}", "--jq", ".head.ref"],
            check=False,
        )
        issue: int | None = None
        if result is not None:
            match = _ISSUE_IN_BRANCH.search(result.strip())
            if match:
                issue = int(match.group(1))
        elif not self.warned:
            self.warned = True
            print(
                "warning: `gh api` lookups failed; tier-2 (branch-name) resolution is "
                "unavailable, so some commits may report as unattributed",
                file=sys.stderr,
            )
        self._cache[pr_number] = issue
        return issue


def resolve_issue_numbers(commit: Commit, resolver: _BranchResolver) -> tuple[list[int], str]:
    """Resolve one commit to the issue number(s) it addresses.

    Three tiers, most authoritative first:

    1. ``closing`` -- a ``Closes/Fixes/Resolves #N`` line in the commit body.
    2. ``branch`` -- the merged PR's ``feature/issue-<N>`` head branch.
    3. ``partial`` -- a ``Part of #N`` line (usually an epic, so it is the last
       resort rather than the first).

    Returns ``([], "none")`` when nothing resolves -- e.g. a direct-to-main chore
    commit with no PR.
    """
    closing = sorted({int(n) for n in _CLOSING_REF.findall(commit.body)})
    if closing:
        return closing, "closing"

    pr_number = commit.pr_number
    if pr_number is not None:
        issue = resolver.issue_for_pr(pr_number)
        if issue is not None:
            return [issue], "branch"

    partial = sorted({int(n) for n in _PARTIAL_REF.findall(commit.body)})
    if partial:
        return partial, "partial"

    return [], "none"


# --- git / changelog plumbing ----------------------------------------------


def _run(cmd: list[str], check: bool = True) -> str | None:
    """Run ``cmd`` in the repo root, returning stdout (or ``None`` on failure)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        if check:
            raise
        return None
    if proc.returncode != 0:
        if check:
            raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
        return None
    return proc.stdout


def latest_release_tag() -> str | None:
    """Return the most recent ``v*`` tag reachable from HEAD, if any."""
    out = _run(["git", "describe", "--tags", "--abbrev=0", "--match", "v*"], check=False)
    return out.strip() if out and out.strip() else None


def default_repo_slug() -> str | None:
    """Derive ``owner/name`` from the ``origin`` remote, for ``gh api`` calls."""
    out = _run(["git", "remote", "get-url", "origin"], check=False)
    if not out:
        return None
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?\s*$", out.strip())
    return match.group(1) if match else None


def read_commits(since: str, head: str) -> list[Commit]:
    """Return the commits in ``since..head``, oldest first."""
    out = _run(["git", "log", "--reverse", "--format=%x00%H%x01%s%x01%b", f"{since}..{head}"])
    assert out is not None
    commits: list[Commit] = []
    for record in out.split("\x00"):
        if not record.strip():
            continue
        sha, subject, body = record.split("\x01", 2)
        commits.append(Commit(sha=sha.strip(), subject=subject.strip(), body=body))
    return commits


def extract_section(changelog_text: str, section: str) -> str:
    """Return the body of the ``## [<section>]`` block, exclusive of later blocks.

    An issue cited only in an *older* release section must not count as
    documented, which is why this slices rather than searching the whole file.
    """
    heading = re.compile(rf"^##\s*\[{re.escape(section)}\]", re.IGNORECASE | re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        return ""
    rest = changelog_text[match.end() :]
    next_heading = re.search(r"^##\s", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def documented_issue_numbers(section_text: str) -> set[int]:
    """Every ``#N`` mentioned in a CHANGELOG section."""
    return {int(n) for n in re.findall(r"#(\d+)\b", section_text)}


# --- report -----------------------------------------------------------------


@dataclass
class Report:
    """The outcome of one reconciliation pass."""

    since: str
    head: str
    total_commits: int
    user_visible_commits: int
    internal_commits: int
    documented: list[int]
    gaps: list[int]
    gap_subjects: dict[int, list[str]]
    unattributed: list[str]
    overridden: list[int]

    @property
    def ok(self) -> bool:
        return not self.gaps


def build_report(
    since: str,
    head: str,
    changelog_path: Path,
    section: str,
    resolver: _BranchResolver,
) -> Report:
    commits = read_commits(since, head)
    section_text = extract_section(changelog_path.read_text(encoding="utf-8"), section)
    documented = documented_issue_numbers(section_text)

    user_visible = 0
    internal = 0
    candidates: dict[int, list[str]] = {}
    unattributed: list[str] = []

    for commit in commits:
        commit.issues, commit.tier = resolve_issue_numbers(commit, resolver)
        if commit.is_internal or commit.short_sha in INTERNAL_COMMITS:
            internal += 1
            continue
        user_visible += 1
        if not commit.issues:
            unattributed.append(f"{commit.short_sha} {commit.subject}")
            continue
        for issue in commit.issues:
            candidates.setdefault(issue, []).append(f"{commit.short_sha} {commit.subject}")

    overridden = sorted(i for i in candidates if i in INTERNAL_ISSUES)
    gaps = sorted(i for i in candidates if i not in documented and i not in INTERNAL_ISSUES)

    return Report(
        since=since,
        head=head,
        total_commits=len(commits),
        user_visible_commits=user_visible,
        internal_commits=internal,
        documented=sorted(documented),
        gaps=gaps,
        gap_subjects={i: candidates[i] for i in gaps},
        unattributed=unattributed,
        overridden=overridden,
    )


def render_text(report: Report) -> str:
    lines = [
        f"CHANGELOG gap report: {report.since}..{report.head}",
        f"  commits:       {report.total_commits} "
        f"({report.user_visible_commits} user-visible, {report.internal_commits} internal)",
        f"  documented:    {len(report.documented)} issue reference(s) in [Unreleased]",
        f"  gaps:          {len(report.gaps)}",
    ]
    if report.gaps:
        lines.append("")
        lines.append("Undocumented user-visible issues:")
        for issue in report.gaps:
            lines.append(f"  #{issue}")
            for subject in report.gap_subjects[issue]:
                lines.append(f"      {subject}")
    if report.overridden:
        lines.append("")
        lines.append(
            "Classified internal via INTERNAL_ISSUES: "
            + ", ".join(f"#{i}" for i in report.overridden)
        )
    if report.unattributed:
        lines.append("")
        lines.append("Unattributed user-visible commits (no issue to cite -- advisory only):")
        lines.extend(f"  {entry}" for entry in report.unattributed)
    lines.append("")
    lines.append(
        "RESULT: gap set is empty"
        if report.ok
        else f"RESULT: {len(report.gaps)} undocumented issue(s) -- update CHANGELOG [Unreleased]"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report user-visible commits since a release tag with no CHANGELOG entry.",
    )
    parser.add_argument(
        "--since",
        metavar="TAG",
        help="release tag to diff against (default: most recent v* tag reachable from HEAD)",
    )
    parser.add_argument("--head", default="HEAD", metavar="REF", help="end of the range")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=REPO_ROOT / "CHANGELOG.md",
        help="path to CHANGELOG.md",
    )
    parser.add_argument(
        "--section",
        default="Unreleased",
        help="CHANGELOG section that must cite the issues (default: Unreleased)",
    )
    parser.add_argument("--repo", help="OWNER/NAME for gh API calls (default: from origin remote)")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip tier-2 (PR branch name) resolution instead of calling the gh API; "
        "lossy, may report spurious gaps for Part-of-only commits",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    since = args.since or latest_release_tag()
    if not since:
        print("error: no v* tag found; pass --since <tag>", file=sys.stderr)
        return 2
    if not args.changelog.is_file():
        print(f"error: no such CHANGELOG: {args.changelog}", file=sys.stderr)
        return 2

    resolver = _BranchResolver(args.repo or default_repo_slug(), args.offline)
    try:
        report = build_report(since, args.head, args.changelog, args.section, resolver)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "since": report.since,
                    "head": report.head,
                    "total_commits": report.total_commits,
                    "user_visible_commits": report.user_visible_commits,
                    "internal_commits": report.internal_commits,
                    "gaps": report.gaps,
                    "gap_subjects": report.gap_subjects,
                    "overridden": report.overridden,
                    "unattributed": report.unattributed,
                },
                indent=2,
            )
        )
    else:
        print(render_text(report))

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
