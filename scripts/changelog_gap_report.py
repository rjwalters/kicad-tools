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
4. Prints the user-visible issue numbers that are cited neither by a
   ``changelog.d/`` fragment (issue #5775) nor anywhere in the CHANGELOG's
   ``[Unreleased]`` section, and exits non-zero if that set is non-empty.

Per-PR mode (issue #5775)
-------------------------
``--pr N`` / ``--pr-event $GITHUB_EVENT_PATH`` evaluates **one pull request**
before it merges (see :func:`evaluate_pr`).  Unsquashed branch commits rarely
carry ``Closes #N``, so the PR is judged the way its squash commit will be: the
conventional-commit type comes from the **PR title** (the squash subject) and
the issue from the **PR body** (closing keyword), then the head branch
(``feature/issue-<N>``), then ``Part of #N``.  A PR fails only when it is
user-visible, not classified internal, not labelled ``changelog:skip``, and no
fragment / ``[Unreleased]`` entry at the PR head cites its issue (or, for a PR
with no resolvable issue, it adds no fragment at all).

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
    uv run python scripts/changelog_gap_report.py --pr 5801      # one PR, via gh
    python scripts/changelog_gap_report.py --pr-event "$GITHUB_EVENT_PATH" --base HEAD^1

`--offline` is lossy: without the tier-2 branch lookup, a commit whose body
carries only a `Part of #<epic>` trailer resolves to the epic rather than to its
own issue, so it can report a spurious gap. Prefer the default (networked) mode
when gating a release.

Exit codes
----------
    0 -- no gaps (or no commits since the tag).
    1 -- one or more user-visible issues are undocumented.
         (per-PR mode: the PR fails the fragment rule, or a fragment is malformed.)
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import changelog_assemble  # noqa: E402  (sibling script, not a package)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAGMENTS_DIR = REPO_ROOT / "changelog.d"

#: Label that exempts one PR from the per-PR check.  Legitimate uses: a change
#: that is user-visible by its title but deliberately undocumented (e.g. a fix
#: to a feature that has not shipped yet, already covered by the feature's own
#: fragment).  Prefer a fragment, or an ``INTERNAL_ISSUES`` entry with a
#: rationale, when the exemption should be auditable.
SKIP_LABEL = "changelog:skip"

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

#: Structural internal rules (issue #5775): classes of change that are internal
#: by construction, so they need no per-commit ledger entry.  Both already use
#: ``chore`` subjects today; these rules keep them internal even if a future
#: Loom or Dependabot release changes its subject style.
#:
#: - Loom's installed-surface resyncs (``chore: resync installed Loom surfaces``)
#:   and Loom installs/upgrades touch only vendored orchestration files.
#: - Dependabot bumps only move dev/CI/site pins or the lockfile.
STRUCTURAL_INTERNAL_SUBJECTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bresync installed Loom surfaces\b", re.IGNORECASE),
    re.compile(r"^(?:chore(?:\([^)]*\))?:\s*)?(?:install|update|upgrade) loom\b", re.IGNORECASE),
)
#: PR authors whose PRs are internal by construction.
STRUCTURAL_INTERNAL_AUTHORS = frozenset({"dependabot[bot]", "app/dependabot"})
#: PR head-branch prefixes that are internal by construction.
STRUCTURAL_INTERNAL_BRANCH_PREFIXES: tuple[str, ...] = ("dependabot/",)


def is_internal_subject(subject: str) -> bool:
    """Classify a commit subject / PR title as internal (never user-visible)."""
    if any(p.search(subject) for p in STRUCTURAL_INTERNAL_SUBJECTS):
        return True
    match = _CONVENTIONAL_SUBJECT.match(subject)
    if match is None:
        # Non-conventional subjects ("route/complete Phase 3: ...",
        # "board-05 Phase 3: ...") are real feature work often enough that the
        # safe default is user-visible; the override ledgers demote the rest.
        return False
    return match.group("type") in INTERNAL_TYPES


#: Issues whose commits carry a user-visible-looking subject but change nothing a
#: package consumer can observe.  This is the auditable ledger backing the
#: "classified internal" half of #4638's acceptance criteria -- add an entry (with
#: the rationale) rather than padding the CHANGELOG with a non-user-visible bullet.
INTERNAL_ISSUES: dict[int, str] = {
    4479: "board-05 CI closeout: AST guard test + CI blocking bound, no shipped surface",
    4895: "CI unblock: reformat ruff-formatted markdown code blocks; no shipped surface",
    4925: "test-harness path resolution for boards/external/; dev-only, no shipped surface",
    4938: "CI ruff-format exclusion for Loom-vendored .claude/.loom markdown; repo tooling",
    4940: "CI ruff-format exclusion for Loom-vendored .claude/.loom markdown; repo tooling",
    4950: "CI ruff-format exclusion for Loom-vendored .claude/.loom markdown; repo tooling",
    4962: "mypy 2.3.1 baseline diagnostic normalization; type-checker UX only",
    4963: "Loom curator heartbeat self-perpetuation fix; orchestration tooling",
    5027: "Cloudflare Pages deploy-guard scoping; release/deploy tooling only",
    5044: "main-CI red repair (readiness status, ruff drift, E2E cascades); repo CI only",
    5084: "CI fleet angle-census artifact discovery glob; repo CI only",
    5130: "CI workflow recognizes verified Loom App issue authors; repo CI only",
    5247: "local-gate manifest exemption for the CI changes detector; repo tooling",
    5249: "CI content-contract behavior on docs-only PRs; repo CI only",
    5271: "test-only stale rule-count floor replaced with a rule-name assertion; dev tests",
    5331: "Loom publish-side yield-exclusion/lease-thrash fix; orchestration tooling",
    5366: "CI Detect Changes nested content-only path-exclusion repair; repo CI only",
    5535: "Loom judge draft-PR fallback exclusion; orchestration tooling",
    5552: "restore #5535's judge exclusion after installed-surface resync; orchestration tooling",
    5566: "CI caching of kicad-cli capability probes; repo CI runtime only",
    5572: "CI native-slot wait credited to the waiting test's timeout; repo CI only",
    5580: "CI determinism gate compares whole copper nodes; repo CI gate mechanics",
    5584: "test-only finite-mode route-budget composition; dev test flake fix",
    5586: "CI board06 smoke copper hash delegates to normalize_copper.py; repo CI only",
    5587: "CI board02/04 determinism-smoke flag drift + timeout raise; repo CI only",
    5597: "CI board06 determinism-smoke explicit output dir; repo CI only",
    5685: "dead-code cleanup of unused router/mesh helpers; no behaviour change",
    5690: "vendored Loom merge-pr.sh trailer warning; repo tooling only",
    5697: "CI ruff exclude for vendored .agents/ surface; repo CI only",
    5741: "vendored Loom merge-pr.sh freshness-guard credential routing; repo tooling only",
    5746: "board-05 export test copies to tmp + regenerated committed demo DRU; tests/boards only",
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
        return is_internal_subject(self.subject)

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


def fragment_documented_issues(fragments_dir: Path | None) -> set[int]:
    """Every issue a ``changelog.d/`` fragment documents (name or body citation).

    Raises :class:`changelog_assemble.FragmentError` for a malformed fragment.
    """
    if fragments_dir is None:
        return set()
    return changelog_assemble.fragment_issue_numbers(
        changelog_assemble.collect_fragments(fragments_dir)
    )


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
    fragments_dir: Path | None = None,
) -> Report:
    commits = read_commits(since, head)
    section_text = extract_section(changelog_path.read_text(encoding="utf-8"), section)
    documented = documented_issue_numbers(section_text) | fragment_documented_issues(fragments_dir)

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
        f"  documented:    {len(report.documented)} issue reference(s) in "
        "changelog.d/ + [Unreleased]",
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
        else f"RESULT: {len(report.gaps)} undocumented issue(s) -- add changelog.d/ fragments"
    )
    return "\n".join(lines)


# --- per-PR mode (issue #5775) ----------------------------------------------


@dataclass
class PullRequest:
    """The parts of one pull request the per-PR check reads."""

    number: int | None
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    author: str = ""
    head_ref: str = ""

    @classmethod
    def from_event(cls, event: dict) -> PullRequest:
        """Build from a GitHub ``pull_request`` event payload (or its ``pull_request`` key)."""
        pr = event.get("pull_request", event)
        return cls(
            number=pr.get("number"),
            title=pr.get("title") or "",
            body=pr.get("body") or "",
            labels=[label["name"] for label in pr.get("labels") or []],
            author=(pr.get("user") or {}).get("login", ""),
            head_ref=(pr.get("head") or {}).get("ref", ""),
        )

    @classmethod
    def from_gh(cls, data: dict) -> PullRequest:
        """Build from ``gh pr view --json number,title,body,labels,author,headRefName``."""
        return cls(
            number=data.get("number"),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=[label["name"] for label in data.get("labels") or []],
            author=(data.get("author") or {}).get("login", ""),
            head_ref=data.get("headRefName") or "",
        )


@dataclass
class PrVerdict:
    """The per-PR check's decision and the reason for it."""

    ok: bool
    reason: str
    issues: list[int]
    tier: str


def resolve_pr_issues(pr: PullRequest) -> tuple[list[int], str]:
    """Resolve a PR to its issue(s): body closing ref > head branch > ``Part of``.

    The same precedence as :func:`resolve_issue_numbers`, applied to the PR body
    and head branch (what the squash commit will carry) instead of to a merged
    commit, because unsquashed branch commits rarely carry ``Closes #N``.
    """
    closing = sorted({int(n) for n in _CLOSING_REF.findall(pr.body)})
    if closing:
        return closing, "closing"
    match = _ISSUE_IN_BRANCH.search(pr.head_ref)
    if match:
        return [int(match.group(1))], "branch"
    partial = sorted({int(n) for n in _PARTIAL_REF.findall(pr.body)})
    if partial:
        return partial, "partial"
    return [], "none"


def is_structurally_internal_pr(pr: PullRequest) -> bool:
    """Dependabot PRs and Loom resyncs/installs are internal by construction."""
    if pr.author in STRUCTURAL_INTERNAL_AUTHORS:
        return True
    if pr.head_ref.startswith(STRUCTURAL_INTERNAL_BRANCH_PREFIXES):
        return True
    return any(p.search(pr.title) for p in STRUCTURAL_INTERNAL_SUBJECTS)


def evaluate_pr(
    pr: PullRequest,
    documented: set[int],
    changed_fragments: list[str],
) -> PrVerdict:
    """Decide whether one PR satisfies the changelog-fragment rule.

    ``documented`` is every issue cited at the PR head by a ``changelog.d/``
    fragment or by ``[Unreleased]``; ``changed_fragments`` is the fragment
    files the PR itself adds or modifies.  Rules, first match wins:

    1. ``changelog:skip`` label -> pass.
    2. Structurally internal (Dependabot author/branch, Loom resync title) -> pass.
    3. Internal conventional-commit type in the PR **title** -> pass.
    4. Every resolved issue is in ``INTERNAL_ISSUES`` -> pass.
    5. A resolved issue is documented -> pass.
    6. No issue resolves, but the PR adds/modifies a fragment -> pass.
    7. Otherwise -> fail.
    """
    issues, tier = resolve_pr_issues(pr)
    if SKIP_LABEL in pr.labels:
        return PrVerdict(True, f"labelled {SKIP_LABEL}", issues, tier)
    if is_structurally_internal_pr(pr):
        return PrVerdict(True, "internal by construction (Dependabot / Loom resync)", issues, tier)
    if is_internal_subject(pr.title):
        return PrVerdict(True, "internal conventional-commit type in PR title", issues, tier)
    if issues and all(i in INTERNAL_ISSUES for i in issues):
        return PrVerdict(True, "issue classified internal in INTERNAL_ISSUES", issues, tier)
    cited = [i for i in issues if i in documented]
    if cited:
        return PrVerdict(True, "documented: " + ", ".join(f"#{i}" for i in cited), issues, tier)
    if not issues and changed_fragments:
        return PrVerdict(
            True,
            "no issue resolved; PR adds fragment(s): " + ", ".join(changed_fragments),
            issues,
            tier,
        )
    want = f"changelog.d/{issues[0]}.<kind>.md" if issues else "changelog.d/<issue>.<kind>.md"
    detail = (
        f" (the PR's fragment(s) {', '.join(changed_fragments)} cite none of "
        + ", ".join(f"#{i}" for i in issues)
        + ")"
        if changed_fragments
        else ""
    )
    return PrVerdict(
        False,
        f"user-visible PR with no changelog fragment{detail}: add {want} "
        f"(kinds: {', '.join(changelog_assemble.KINDS)}), use an internal "
        f"conventional-commit title, or apply the {SKIP_LABEL} label",
        issues,
        tier,
    )


def changed_fragment_paths(base: str, head: str = "HEAD") -> list[str]:
    """Fragment files added or modified in ``base...head``."""
    out = _run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=AMR",
            f"{base}...{head}",
            "--",
            "changelog.d/",
        ]
    )
    assert out is not None
    names = []
    for line in out.splitlines():
        name = Path(line.strip()).name
        if line.strip() and changelog_assemble.parse_fragment_name(name) is not None:
            names.append(line.strip())
    return sorted(names)


def run_pr_mode(
    pr: PullRequest,
    base: str,
    changelog_path: Path,
    section: str,
    fragments_dir: Path,
    as_json: bool,
) -> int:
    """CLI driver for per-PR mode.  Reads the working tree as the PR head."""
    try:
        documented = documented_issue_numbers(
            extract_section(changelog_path.read_text(encoding="utf-8"), section)
        ) | fragment_documented_issues(fragments_dir)
        changed = changed_fragment_paths(base)
    except (changelog_assemble.FragmentError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, changelog_assemble.FragmentError) else 2
    verdict = evaluate_pr(pr, documented, changed)
    if as_json:
        print(
            json.dumps(
                {
                    "pr": pr.number,
                    "title": pr.title,
                    "ok": verdict.ok,
                    "reason": verdict.reason,
                    "issues": verdict.issues,
                    "tier": verdict.tier,
                    "changed_fragments": changed,
                },
                indent=2,
            )
        )
    else:
        label = f"PR #{pr.number}" if pr.number else "PR"
        print(f"CHANGELOG fragment check: {label}: {pr.title}")
        resolved = ", ".join(f"#{i}" for i in verdict.issues) or "none"
        print(f"  issue(s):  {resolved} (via {verdict.tier})")
        print(f"  fragments: {', '.join(changed) or 'none added/modified'}")
        print(f"RESULT: {'PASS' if verdict.ok else 'FAIL'} -- {verdict.reason}")
    return 0 if verdict.ok else 1


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
    parser.add_argument(
        "--fragments-dir",
        type=Path,
        default=DEFAULT_FRAGMENTS_DIR,
        help="changelog fragment directory (default: changelog.d/)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    pr_mode = parser.add_mutually_exclusive_group()
    pr_mode.add_argument(
        "--pr", type=int, metavar="N", help="per-PR mode: evaluate PR N (reads it via gh)"
    )
    pr_mode.add_argument(
        "--pr-event",
        type=Path,
        metavar="PATH",
        help="per-PR mode: read the PR from a GitHub pull_request event JSON "
        "($GITHUB_EVENT_PATH); no gh or network needed",
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        metavar="REF",
        help="per-PR mode: base ref the PR's fragment changes are diffed against "
        "(default: origin/main; CI's merge checkout uses HEAD^1)",
    )
    args = parser.parse_args(argv)

    if not args.changelog.is_file():
        print(f"error: no such CHANGELOG: {args.changelog}", file=sys.stderr)
        return 2

    if args.pr is not None or args.pr_event is not None:
        if args.pr_event is not None:
            try:
                pr = PullRequest.from_event(json.loads(args.pr_event.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError) as exc:
                print(f"error: cannot read PR event {args.pr_event}: {exc}", file=sys.stderr)
                return 2
        else:
            fields = "number,title,body,labels,author,headRefName"
            cmd = ["gh", "pr", "view", str(args.pr), "--json", fields]
            if args.repo:
                cmd += ["--repo", args.repo]
            out = _run(cmd, check=False)
            if out is None:
                print(f"error: `gh pr view {args.pr}` failed", file=sys.stderr)
                return 2
            pr = PullRequest.from_gh(json.loads(out))
        return run_pr_mode(
            pr, args.base, args.changelog, args.section, args.fragments_dir, args.json
        )

    since = args.since or latest_release_tag()
    if not since:
        print("error: no v* tag found; pass --since <tag>", file=sys.stderr)
        return 2

    resolver = _BranchResolver(args.repo or default_repo_slug(), args.offline)
    try:
        report = build_report(
            since, args.head, args.changelog, args.section, resolver, args.fragments_dir
        )
    except (RuntimeError, changelog_assemble.FragmentError) as exc:
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
