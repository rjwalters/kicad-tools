#!/usr/bin/env python3
"""Plan, gate and open the daily automated release PR (issue #5776, epic #5774).

``.github/workflows/release-pr.yml`` runs ``release_plan.py run`` once a day
(14:00 UTC) and on ``workflow_dispatch``.  All the decisions live here so they
can be unit-tested; the workflow YAML only mints tokens and calls this script.

What one run does
-----------------
1. **Mode.**  The repo variable ``AUTO_RELEASE`` is ``on`` | ``off`` |
   ``dry-run``; unset (or anything else) means ``dry-run``.  A dispatch with
   ``dry_run: true`` forces ``dry-run``.  ``off`` exits at once.
2. **No-op rule.**  Find the latest ``v*`` tag reachable from ``origin/main``
   and list the first-parent commits since it.  If none of them is a merged PR
   (``Merge pull request #N`` or a ``(#N)`` squash suffix), exit quietly --
   direct pushes such as Loom resyncs alone never cut a release.
3. **Plan.**  Current version from ``pyproject.toml`` (which must equal the
   tag, otherwise a merged release is still waiting for its tag and the run
   stops).  Level by :func:`decide_level`: patch by default; minor if any
   ``added``/``upgrade`` fragment exists or a ``feat`` commit landed; a
   breaking commit (``type!:`` or ``BREAKING CHANGE``) is minor below 1.0 and
   major from 1.0; a dispatch ``level`` overrides the rule.  Nothing to
   assemble (no fragments, empty ``[Unreleased]``) means no user-visible
   change, so the run exits quietly too.
4. **Gate** (all on the exact ``origin/main`` SHA; :func:`evaluate_gate`):
   the newest ``CI`` push run on the SHA completed with every release-topic
   job in :data:`RELEASE_JOBS` green or skipped (skipped is never red, whatever
   the reason -- #5776 curator note §1); ``changelog_gap_report.py`` reports no
   gaps; ``uv lock --check`` passes.  A CI run still in progress is waited for
   (bounded by ``--wait-ci``).  A red gate never releases: in ``on`` mode it
   comments its reasons on the "Automated release status" tracking issue.
5. **Release PR** (``on`` only): build ``release/vX.Y.Z`` from the gated SHA
   (:func:`build_release_tree` -- assembled CHANGELOG, fragments deleted,
   ``pyproject.toml`` bumped, only the kicad-tools ``version`` line of
   ``uv.lock`` changed, a WORK_LOG line), check ``uv lock --check`` again,
   push, and open the PR -- or update the open automated one (at most one open
   at a time; a hand-opened ``release/v*`` PR pauses automation).
6. **Merge.**  Wait (bounded by ``--wait-pr``) for the release PR's own checks
   and merge it through the API with the head SHA pinned.  Red or timed-out
   checks leave the PR open and are reported on the tracking issue.

Tokens
------
Reads (Actions runs/jobs, PR lists, the gap report's PR lookups) use
``GH_TOKEN`` -- the workflow's ``GITHUB_TOKEN`` with ``actions: read``.
Writes (PR create/edit/close/merge, branch delete, tracking-issue comments)
use ``RELEASE_TOKEN`` -- the loom-fleet-dispatch App token, so the push and PR
trigger ``ci.yml`` (a ``GITHUB_TOKEN`` push/PR would not; curator note §3).
The ``git push`` itself uses the credentials ``actions/checkout`` persisted,
which the workflow sets to the App token.

Interface for Phase 3 (#5777)
-----------------------------
- Automated release PRs: head ``release/vX.Y.Z``, base ``main``, title
  ``chore(release): vX.Y.Z``, body contains :data:`AUTO_MARKER`; merged with
  the first method the repo allows (merge commit > squash > rebase; today only
  merge commits are enabled), commit title ``chore(release): vX.Y.Z (#N)``.
- :func:`parse_version`, :func:`bump_version`, :func:`release_branch_name`.

Usage
-----
    uv run python scripts/release_plan.py plan                 # offline: tag, merges, level, section
    uv run python scripts/release_plan.py plan --json
    uv run python scripts/release_plan.py run --mode dry-run --wait-ci 0

Exit codes
----------
    0 -- done (released, dry-run reported, no-op, or ``off``).
    1 -- ``on`` mode: the gate is red, a hand-opened release PR is in flight, or the
         release PR's checks failed / timed out.  (``dry-run`` reports a red gate
         and exits 0.)
    2 -- usage / environment error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import changelog_assemble  # noqa: E402  (sibling script, not a package)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO_SLUG = "rjwalters/kicad-tools"
PACKAGE_NAME = "kicad-tools"
BASE_BRANCH = "main"
RELEASE_BRANCH_PREFIX = "release/v"
TRACKING_ISSUE_TITLE = "Automated release status"
#: Marks a PR body as opened by this workflow (a hand-opened release PR lacks it).
AUTO_MARKER = "<!-- kct-auto-release -->"
CI_WORKFLOW_FILE = "ci.yml"

#: The release-topic CI jobs (check-run / job ``name:`` in ci.yml).  A job whose
#: name is ``<name> (<suffix>)`` (e.g. ``Diff-Pair Routing Regression
#: (boards/06-diffpair-test)``, or a matrix leg) counts as that job.
RELEASE_JOBS: tuple[str, ...] = (
    "Lint & Format",
    "Type Check",
    "Test",
    "C++ Build Check",
    "kicad-cli Round-trip Smoke",
    "Routed PCB DRC Check",
    "Diff-Pair Routing Regression",
    "Match-Group Routing Regression",
    "Board 00 End-to-End",
)

#: Job conclusions that are not red.  ``skipped`` is included unconditionally:
#: Match-Group is skipped on every run while ``vars.BOARD_07_CI_ENABLED`` is
#: unset, so a path-filter-only exemption would make the gate unreachable.
NOT_RED = frozenset({"success", "skipped", "neutral"})

MODES = ("on", "off", "dry-run")
LEVELS = ("patch", "minor", "major")

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:")
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
_MERGE_PR = re.compile(r"^Merge pull request #(\d+)\b")
_SQUASH_PR = re.compile(r"\(#(\d+)\)\s*$")


class ReleaseError(RuntimeError):
    """An environment or repository state the release cannot proceed from."""


# --- versions -----------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int]:
    """``"0.22.0"`` (or ``"v0.22.0"``) -> ``(0, 22, 0)``; plain X.Y.Z only."""
    match = _VERSION.match(text.strip().removeprefix("v"))
    if match is None:
        raise ReleaseError(f"not an X.Y.Z version: {text!r}")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def bump_version(version: str, level: str) -> str:
    major, minor, patch = parse_version(version)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"unknown level {level!r}; expected one of {LEVELS}")


def release_branch_name(version: str) -> str:
    return f"{RELEASE_BRANCH_PREFIX}{version}"


# --- commits ------------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str = ""

    @property
    def pr_number(self) -> int | None:
        """The PR a first-parent commit on ``main`` merged, if it names one."""
        match = _MERGE_PR.match(self.subject) or _SQUASH_PR.search(self.subject)
        return int(match.group(1)) if match else None

    def titles(self) -> list[str]:
        """Conventional-commit candidates: the subject, and for a merge commit
        the PR title GitHub writes as the first body line."""
        out = [self.subject]
        if _MERGE_PR.match(self.subject):
            first = next((ln.strip() for ln in self.body.splitlines() if ln.strip()), "")
            if first:
                out.append(first)
        return out

    @property
    def is_feat(self) -> bool:
        return any(
            (m := _CONVENTIONAL.match(t)) is not None and m.group("type") == "feat"
            for t in self.titles()
        )

    @property
    def is_breaking(self) -> bool:
        if _BREAKING_FOOTER.search(self.body):
            return True
        return any(
            (m := _CONVENTIONAL.match(t)) is not None and m.group("bang") for t in self.titles()
        )


def merged_prs(first_parent_commits: Iterable[Commit]) -> list[int]:
    """PR numbers merged by first-parent commits, oldest first, de-duplicated."""
    seen: list[int] = []
    for commit in first_parent_commits:
        number = commit.pr_number
        if number is not None and number not in seen:
            seen.append(number)
    return seen


# --- version level ------------------------------------------------------------


@dataclass
class LevelDecision:
    level: str
    reasons: list[str]


def decide_level(
    current: str,
    fragments: Iterable[changelog_assemble.Fragment],
    commits: Iterable[Commit],
    override: str | None = None,
) -> LevelDecision:
    """The version-level rule (see the module docstring, step 3)."""
    if override and override != "auto":
        if override not in LEVELS:
            raise ReleaseError(f"unknown level override {override!r}")
        return LevelDecision(override, [f"level overridden to {override} by workflow input"])

    major = parse_version(current)[0]
    commits = list(commits)
    reasons: list[str] = []
    level = "patch"
    breaking = [c for c in commits if c.is_breaking]
    for fragment in fragments:
        if fragment.kind in ("added", "upgrade"):
            level = "minor"
            reasons.append(f"{fragment.kind} fragment {fragment.path.name}")
    for commit in commits:
        if commit.is_feat:
            level = "minor"
            reasons.append(f"feat commit {commit.sha[:8]} {commit.subject}")
    for commit in breaking:
        if major == 0:
            level = "minor"
            reasons.append(f"breaking commit {commit.sha[:8]} (minor while below 1.0)")
        else:
            level = "major"
            reasons.append(f"breaking commit {commit.sha[:8]} {commit.subject}")
    if not reasons:
        reasons.append("no added/upgrade fragment and no feat commit: patch")
    return LevelDecision(level, reasons)


# --- CI gate --------------------------------------------------------------------


def job_matches(job_name: str, required: str) -> bool:
    return job_name == required or job_name.startswith(required + " (")


def latest_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest job per name (a re-run leaves older attempts behind)."""
    newest: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("name", "")
        key = (job.get("run_attempt") or 0, job.get("started_at") or "", job.get("id") or 0)
        prior = newest.get(name)
        if prior is None or key > (
            prior.get("run_attempt") or 0,
            prior.get("started_at") or "",
            prior.get("id") or 0,
        ):
            newest[name] = job
    return list(newest.values())


def evaluate_jobs(
    jobs: Iterable[dict[str, Any]], required: Iterable[str] = RELEASE_JOBS
) -> list[str]:
    """Reasons the jobs are not green (empty list = green).

    Every ``required`` name must match at least one job, every matching job
    must be completed with a conclusion in :data:`NOT_RED`.  Jobs outside
    ``required`` are ignored.
    """
    jobs = latest_jobs(jobs)
    problems: list[str] = []
    for name in required:
        matching = [j for j in jobs if job_matches(j.get("name", ""), name)]
        if not matching:
            problems.append(f"CI job `{name}` did not run")
            continue
        for job in matching:
            if job.get("status") != "completed":
                problems.append(f"CI job `{job['name']}` is {job.get('status')}")
            elif job.get("conclusion") not in NOT_RED:
                problems.append(f"CI job `{job['name']}` concluded {job.get('conclusion')}")
    return problems


def newest_run(runs: Iterable[dict[str, Any]], event: str | None = None) -> dict[str, Any] | None:
    candidates = [r for r in runs if event is None or r.get("event") == event]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))


@dataclass
class GateResult:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# --- file edits ---------------------------------------------------------------


def set_pyproject_version(text: str, version: str) -> str:
    """Rewrite ``version = "..."`` inside ``[project]`` only."""
    match = re.search(r"^\[project\]\s*$", text, re.MULTILINE)
    if match is None:
        raise ReleaseError("pyproject.toml has no [project] table")
    end_match = re.search(r"^\[", text[match.end() :], re.MULTILINE)
    end = match.end() + end_match.start() if end_match else len(text)
    table = text[match.end() : end]
    new_table, count = re.subn(
        r'^version\s*=\s*"[^"]*"', f'version = "{version}"', table, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise ReleaseError("pyproject.toml [project] has no version line")
    return text[: match.end()] + new_table + text[end:]


def pyproject_version(text: str) -> str:
    match = re.search(r"^\[project\]\s*$", text, re.MULTILINE)
    if match is None:
        raise ReleaseError("pyproject.toml has no [project] table")
    found = re.search(r'^version\s*=\s*"([^"]*)"', text[match.end() :], re.MULTILINE)
    if found is None:
        raise ReleaseError("pyproject.toml [project] has no version line")
    return found.group(1)


def set_lock_version(text: str, version: str, package: str = PACKAGE_NAME) -> str:
    """Change only the ``version`` line of ``package``'s ``uv.lock`` stanza.

    A full ``uv lock`` is avoided on purpose: different uv versions rewrite
    hundreds of marker lines (#5776 curator note §4).
    """
    anchor = f'name = "{package}"'
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == anchor]
    if len(hits) != 1:
        raise ReleaseError(f"uv.lock has {len(hits)} `{anchor}` lines, expected exactly 1")
    i = hits[0] + 1
    if i >= len(lines) or not re.match(r'^version = "[^"]*"\s*$', lines[i]):
        raise ReleaseError(f"uv.lock: no `version = ...` line right after `{anchor}`")
    eol = lines[i][len(lines[i].rstrip("\r\n")) :]
    lines[i] = f'version = "{version}"{eol}'
    return "".join(lines)


def add_work_log_entry(text: str, version: str, date: str, prs: list[int]) -> str:
    """Append a ``## <date> — vX.Y.Z release cut`` entry (the file is chronological)."""
    pr_list = ", ".join(f"#{n}" for n in prs) if prs else "none"
    entry = (
        f"## {date} — v{version} release cut\n\n"
        f"- v{version} release PR opened by the automated daily release workflow "
        f"(`release-pr.yml`, #5776); PRs merged since the previous tag: {pr_list}.\n"
    )
    return text.rstrip("\n") + "\n\n" + entry


def build_release_tree(
    repo: Path,
    version: str,
    date: str,
    prs: list[int],
    fragments: list[changelog_assemble.Fragment] | None = None,
) -> list[Path]:
    """Apply every release-PR file change in ``repo``; return the touched paths.

    CHANGELOG assembled from ``changelog.d/`` (fragments deleted),
    ``pyproject.toml`` + the kicad-tools ``uv.lock`` line bumped, WORK_LOG entry.
    """
    fragments_dir = repo / "changelog.d"
    if fragments is None:
        fragments = changelog_assemble.collect_fragments(fragments_dir)
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        changelog_assemble.assemble(
            changelog.read_text(encoding="utf-8"), fragments, version, date
        ),
        encoding="utf-8",
    )
    for fragment in fragments:
        fragment.path.unlink()

    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        set_pyproject_version(pyproject.read_text(encoding="utf-8"), version), encoding="utf-8"
    )
    lock = repo / "uv.lock"
    lock.write_text(set_lock_version(lock.read_text(encoding="utf-8"), version), encoding="utf-8")
    work_log = repo / "WORK_LOG.md"
    if work_log.is_file():
        work_log.write_text(
            add_work_log_entry(work_log.read_text(encoding="utf-8"), version, date, prs),
            encoding="utf-8",
        )
    touched = [changelog, pyproject, lock, work_log]
    touched += [f.path for f in fragments]
    return touched


# --- mode ---------------------------------------------------------------------


def resolve_mode(variable: str | None, dispatch_dry_run: bool = False) -> tuple[str, str]:
    """``(mode, note)`` from ``AUTO_RELEASE`` and the dispatch ``dry_run`` input."""
    value = (variable or "").strip().lower()
    if value == "off":
        return "off", "AUTO_RELEASE=off"
    if value == "on":
        if dispatch_dry_run:
            return "dry-run", "AUTO_RELEASE=on, dry_run requested by workflow_dispatch"
        return "on", "AUTO_RELEASE=on"
    if value == "dry-run":
        return "dry-run", "AUTO_RELEASE=dry-run"
    if not value:
        return "dry-run", "AUTO_RELEASE unset: defaulting to dry-run"
    return "dry-run", f"AUTO_RELEASE={variable!r} is not on/off/dry-run: defaulting to dry-run"


# --- git ----------------------------------------------------------------------


class Git:
    def __init__(self, repo: Path = REPO_ROOT) -> None:
        self.repo = repo

    def __call__(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise ReleaseError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout if proc.returncode == 0 else ""

    def rev_parse(self, ref: str) -> str:
        return self("rev-parse", ref).strip()

    def latest_tag(self, ref: str) -> str | None:
        out = self("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", ref, check=False)
        return out.strip() or None

    def commits(self, rev_range: str, *, first_parent: bool) -> list[Commit]:
        args = ["log", "--format=%H%x1f%s%x1f%b%x1e"]
        if first_parent:
            args.append("--first-parent")
        out = self(*args, rev_range)
        commits = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, subject, body = (record.split("\x1f") + ["", ""])[:3]
            commits.append(Commit(sha=sha, subject=subject, body=body))
        return commits

    def show(self, ref: str, path: str) -> str:
        return self("show", f"{ref}:{path}")

    def ls_dir(self, ref: str, path: str) -> list[str]:
        out = self("ls-tree", "--name-only", f"{ref}:{path}", check=False)
        return [line for line in out.splitlines() if line]


def fragments_at(
    git: Git, ref: str, directory: str = "changelog.d"
) -> list[changelog_assemble.Fragment]:
    """The fragments committed at ``ref`` (not the working tree), validated by
    :func:`changelog_assemble.collect_fragments`; paths point into ``git.repo``."""
    names = git.ls_dir(ref, directory)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for name in names:
            (scratch / name).write_text(git.show(ref, f"{directory}/{name}"), encoding="utf-8")
        found = changelog_assemble.collect_fragments(scratch)
    return [replace(f, path=git.repo / directory / f.path.name) for f in found]


# --- forge (GitHub via gh) ----------------------------------------------------


class Forge:
    """GitHub access through ``gh api``: reads with GH_TOKEN, writes with RELEASE_TOKEN."""

    def __init__(
        self,
        slug: str,
        write_token: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.slug = slug
        self.write_token = write_token
        self._runner = runner

    def api(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        write: bool = False,
    ) -> Any:
        env = dict(os.environ)
        if write:
            if not self.write_token:
                raise ReleaseError(f"{method} {path}: no RELEASE_TOKEN for a write")
            env["GH_TOKEN"] = self.write_token
        cmd = ["gh", "api", "-X", method, path.format(repo=f"repos/{self.slug}")]
        if body is not None:
            cmd += ["--input", "-"]
        proc = self._runner(
            cmd,
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            raise ReleaseError(f"gh api {method} {path} failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    # reads
    def runs_for_sha(self, sha: str, workflow: str | None = None) -> list[dict[str, Any]]:
        base = f"{{repo}}/actions/workflows/{workflow}/runs" if workflow else "{repo}/actions/runs"
        runs: list[dict[str, Any]] = self.api(f"{base}?head_sha={sha}&per_page=100")[
            "workflow_runs"
        ]
        return runs

    def run_jobs(self, run_id: int) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = self.api(
            f"{{repo}}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
        )["jobs"]
        return jobs

    def open_release_prs(self) -> list[dict[str, Any]]:
        pulls = self.api(f"{{repo}}/pulls?state=open&base={BASE_BRANCH}&per_page=100")
        return [p for p in pulls if p["head"]["ref"].startswith(RELEASE_BRANCH_PREFIX)]

    def pull(self, number: int) -> dict[str, Any]:
        pull: dict[str, Any] = self.api(f"{{repo}}/pulls/{number}")
        return pull

    # writes
    def create_pr(self, head: str, title: str, body: str) -> dict[str, Any]:
        created: dict[str, Any] = self.api(
            "{repo}/pulls",
            method="POST",
            body={"head": head, "base": BASE_BRANCH, "title": title, "body": body},
            write=True,
        )
        return created

    def edit_pr(self, number: int, title: str, body: str) -> None:
        self.api(
            f"{{repo}}/pulls/{number}",
            method="PATCH",
            body={"title": title, "body": body},
            write=True,
        )

    def close_pr(self, number: int, comment: str) -> None:
        self.comment(number, comment)
        self.api(f"{{repo}}/pulls/{number}", method="PATCH", body={"state": "closed"}, write=True)

    def delete_branch(self, branch: str) -> None:
        try:
            self.api(f"{{repo}}/git/refs/heads/{branch}", method="DELETE", write=True)
        except ReleaseError as exc:  # already gone is fine
            print(f"warning: {exc}", file=sys.stderr)

    def merge_method(self) -> str:
        """The first merge method the repo allows (merge > squash > rebase)."""
        repo = self.api("{repo}")
        for method, key in (
            ("merge", "allow_merge_commit"),
            ("squash", "allow_squash_merge"),
            ("rebase", "allow_rebase_merge"),
        ):
            if repo.get(key):
                return method
        return "merge"

    def merge_pr(self, number: int, sha: str, title: str) -> None:
        """Merge with the head SHA pinned, so nothing pushed after the checks merges."""
        self.api(
            f"{{repo}}/pulls/{number}/merge",
            method="PUT",
            body={"merge_method": self.merge_method(), "sha": sha, "commit_title": title},
            write=True,
        )

    def comment(self, number: int, body: str) -> None:
        self.api(
            f"{{repo}}/issues/{number}/comments", method="POST", body={"body": body}, write=True
        )

    def tracking_issue(self) -> int:
        """Find (by exact title, open) or create the tracking issue."""
        query = f'repo:{self.slug} is:issue is:open in:title "{TRACKING_ISSUE_TITLE}"'
        found = self.api("search/issues?q=" + urllib.parse.quote(query) + "&sort=created&order=asc")
        for issue in sorted(found.get("items", []), key=lambda i: int(i["number"])):
            if issue.get("title") == TRACKING_ISSUE_TITLE and "pull_request" not in issue:
                return int(issue["number"])
        created = self.api(
            "{repo}/issues",
            method="POST",
            body={
                "title": TRACKING_ISSUE_TITLE,
                "body": (
                    "Status of the automated daily release workflow "
                    "(`.github/workflows/release-pr.yml`, #5776). The workflow comments "
                    "here whenever it skips a release or a release PR fails its checks. "
                    "Pin this issue once (GitHub has no REST pin endpoint)."
                ),
            },
            write=True,
        )
        return int(created["number"])

    def report(self, body: str) -> None:
        self.comment(self.tracking_issue(), body)


# --- plan -----------------------------------------------------------------------


@dataclass
class Plan:
    sha: str
    tag: str | None
    current: str
    prs: list[int]
    first_parent: list[Commit]
    fragments: list[changelog_assemble.Fragment]
    has_content: bool
    decision: LevelDecision | None = None
    version: str | None = None
    section: str = ""
    stop: str | None = None  # a reason to exit quietly (no-op)

    def to_json(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "tag": self.tag,
            "current_version": self.current,
            "merged_prs": self.prs,
            "commits_since_tag": len(self.first_parent),
            "fragments": [f.path.name for f in self.fragments],
            "level": self.decision.level if self.decision else None,
            "level_reasons": self.decision.reasons if self.decision else [],
            "version": self.version,
            "stop": self.stop,
            "section": self.section,
        }


def make_plan(
    git: Git, ref: str, level_override: str | None = None, date: str | None = None
) -> Plan:
    """Everything that can be decided from git alone (no network)."""
    sha = git.rev_parse(ref)
    tag = git.latest_tag(sha)
    current = pyproject_version(git.show(sha, "pyproject.toml"))
    if tag is None:
        raise ReleaseError(f"no v* tag reachable from {ref}")
    first_parent = git.commits(f"{tag}..{sha}", first_parent=True)
    prs = merged_prs(first_parent)
    fragments = fragments_at(git, sha)
    changelog_text = git.show(sha, "CHANGELOG.md")
    content = changelog_assemble.has_content(
        fragments, changelog_assemble.parse_unreleased(changelog_text)
    )
    plan = Plan(sha, tag, current, prs, first_parent, fragments, content)
    if not prs:
        plan.stop = f"no PRs merged since {tag}"
        return plan
    if parse_version(tag) != parse_version(current):
        plan.stop = (
            f"pyproject.toml is at {current} but the latest tag is {tag}: a merged release is "
            "still waiting for its tag"
        )
        return plan
    if not content:
        plan.stop = (
            f"{len(prs)} PR(s) merged since {tag}, but no changelog fragment and an empty "
            "[Unreleased]: nothing user-visible to release"
        )
        return plan
    all_commits = git.commits(f"{tag}..{sha}", first_parent=False)
    plan.decision = decide_level(current, fragments, all_commits, level_override)
    plan.version = bump_version(current, plan.decision.level)
    date = date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    plan.section = changelog_assemble.render_release_section(
        plan.version,
        date,
        fragments,
        unreleased=changelog_assemble.parse_unreleased(changelog_text),
    )
    return plan


def render_plan(plan: Plan) -> str:
    lines = [
        f"release plan for {plan.sha[:12]} (latest tag {plan.tag}, pyproject {plan.current})",
        f"  PRs merged since tag: {', '.join(f'#{n}' for n in plan.prs) or 'none'}"
        f" ({len(plan.first_parent)} first-parent commit(s))",
        f"  fragments: {len(plan.fragments)}",
    ]
    if plan.stop:
        lines.append(f"  no-op: {plan.stop}")
        return "\n".join(lines)
    assert plan.decision is not None
    lines.append(f"  level: {plan.decision.level} -> v{plan.version}")
    lines += [f"    - {r}" for r in plan.decision.reasons]
    lines += ["", plan.section.rstrip("\n")]
    return "\n".join(lines)


# --- gate -----------------------------------------------------------------------


def wait_for(
    probe: Callable[[], tuple[str, list[str]]],
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, list[str]]:
    """Poll ``probe`` (-> ``("green"|"red"|"pending", reasons)``) until not pending."""
    deadline = clock() + timeout
    while True:
        state, reasons = probe()
        if state != "pending" or clock() >= deadline:
            return state, reasons
        sleep(interval)


def main_ci_state(forge: Forge, sha: str) -> tuple[str, list[str]]:
    run = newest_run(forge.runs_for_sha(sha, CI_WORKFLOW_FILE), event="push")
    if run is None:
        return "pending", [f"no CI push run found for {sha[:12]}"]
    if run.get("status") != "completed":
        return "pending", [f"CI run {run['id']} on {sha[:12]} is {run.get('status')}"]
    problems = evaluate_jobs(forge.run_jobs(run["id"]))
    return ("red" if problems else "green"), problems


def pr_checks_state(forge: Forge, sha: str) -> tuple[str, list[str]]:
    """State of every ``pull_request`` workflow run on the release PR's head."""
    runs = [r for r in forge.runs_for_sha(sha) if r.get("event") == "pull_request"]
    latest: dict[Any, dict[str, Any]] = {}
    for run in runs:
        key = run.get("workflow_id") or run.get("path")
        if key not in latest or (run.get("created_at") or "") > (
            latest[key].get("created_at") or ""
        ):
            latest[key] = run
    ci = [r for r in latest.values() if (r.get("path") or "").endswith(CI_WORKFLOW_FILE)]
    if not ci:
        return "pending", [f"no CI pull_request run on {sha[:12]} yet"]
    pending = [r for r in latest.values() if r.get("status") != "completed"]
    if pending:
        return "pending", [f"{r.get('name')} run {r['id']} is {r.get('status')}" for r in pending]
    problems: list[str] = []
    for run in latest.values():
        jobs = forge.run_jobs(run["id"])
        required = RELEASE_JOBS if run in ci else ()
        problems += evaluate_jobs(jobs, required)
        problems += [
            f"`{run.get('name')}` job `{j['name']}` concluded {j.get('conclusion')}"
            for j in latest_jobs(jobs)
            if j.get("status") == "completed"
            and j.get("conclusion") not in NOT_RED
            and not any(job_matches(j["name"], r) for r in required)
        ]
    return ("red" if problems else "green"), problems


def run_gap_report(tag: str, sha: str) -> list[str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "changelog_gap_report.py"),
            "--since",
            tag,
            "--head",
            sha,
            "--json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        try:
            gaps = json.loads(proc.stdout).get("gaps", [])
        except ValueError:
            gaps = []
        listed = ", ".join(f"#{n}" for n in gaps) or "(unparsed)"
        return [f"changelog gap report: undocumented user-visible issue(s) {listed}"]
    return [f"changelog gap report errored (exit {proc.returncode}): {proc.stderr.strip()}"]


def run_uv_lock_check(repo: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["uv", "lock", "--check"], cwd=repo, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return ["`uv lock --check`: uv not installed"]
    if proc.returncode != 0:
        return [f"`uv lock --check` failed: {proc.stderr.strip().splitlines()[-1:]}"]
    return []


def evaluate_gate(
    forge: Forge,
    plan: Plan,
    repo: Path,
    wait_ci: float,
    interval: float = 60,
    sleep: Callable[[float], None] = time.sleep,
) -> GateResult:
    result = GateResult()
    state, reasons = wait_for(lambda: main_ci_state(forge, plan.sha), wait_ci, interval, sleep)
    if state == "green":
        result.notes.append(f"CI green on {plan.sha[:12]} (skipped jobs count as not red)")
    else:
        prefix = "" if state == "red" else f"CI not finished after {int(wait_ci)}s: "
        result.problems += [prefix + r for r in reasons]
    assert plan.tag is not None
    result.problems += run_gap_report(plan.tag, plan.sha)
    result.problems += run_uv_lock_check(repo)
    return result


# --- run ------------------------------------------------------------------------


def pr_title(version: str) -> str:
    return f"chore(release): v{version}"


def pr_body(plan: Plan, gate: GateResult) -> str:
    assert plan.decision is not None
    reasons = "\n".join(f"- {r}" for r in plan.decision.reasons)
    prs = ", ".join(f"#{n}" for n in plan.prs)
    return (
        f"{AUTO_MARKER}\n"
        f"Automated release **v{plan.version}** from `{plan.sha}` "
        f"(`.github/workflows/release-pr.yml`, #5776).\n\n"
        f"**Level: {plan.decision.level}**\n{reasons}\n\n"
        f"PRs merged since {plan.tag}: {prs}\n\n"
        f"Gate: {'; '.join(gate.notes) or 'green'}; changelog gap report empty; "
        f"`uv lock --check` passed.\n\n"
        "The workflow merges this PR once its own checks are green. The tag and "
        "PyPI publish follow on merge (Phase 3, #5777).\n\n"
        f"---\n\n{plan.section}"
    )


@dataclass
class Outcome:
    code: int
    summary: str


def open_or_update_release_pr(
    forge: Forge, git: Git, plan: Plan, gate: GateResult, date: str
) -> tuple[int, str]:
    """Build and push ``release/vX.Y.Z``; open or update the automated PR.

    Returns ``(pr_number, head_sha)``.  Raises :class:`ReleaseError` if a
    hand-opened release PR is in flight.
    """
    assert plan.version is not None
    existing = forge.open_release_prs()
    manual = [p for p in existing if AUTO_MARKER not in (p.get("body") or "")]
    if manual:
        raise ReleaseError(
            "a hand-opened release PR is in flight ("
            + ", ".join(f"#{p['number']}" for p in manual)
            + "); automation pauses until it is merged or closed"
        )
    branch = release_branch_name(plan.version)
    git("checkout", "--quiet", "-B", branch, plan.sha)
    touched = build_release_tree(git.repo, plan.version, date, plan.prs, plan.fragments)
    lock_problems = run_uv_lock_check(git.repo)
    if lock_problems:
        raise ReleaseError("release tree fails " + "; ".join(lock_problems))
    git("add", "-A", "--", *(str(p.relative_to(git.repo)) for p in touched))
    git("commit", "--quiet", "-m", f"chore(release): bump version to {plan.version}")
    git("push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    head_sha = git.rev_parse("HEAD")

    title, body = pr_title(plan.version), pr_body(plan, gate)
    same = [p for p in existing if p["head"]["ref"] == branch]
    stale = [p for p in existing if p["head"]["ref"] != branch]
    if same:
        number = int(same[0]["number"])
        forge.edit_pr(number, title, body)
    else:
        number = int(forge.create_pr(branch, title, body)["number"])
    for pr in stale:
        forge.close_pr(
            int(pr["number"]),
            f"Superseded by #{number} (v{plan.version}) -- one release PR at a time.",
        )
        forge.delete_branch(pr["head"]["ref"])
    return number, head_sha


def run(
    *,
    mode: str,
    mode_note: str,
    git: Git,
    forge: Forge,
    level: str | None,
    wait_ci: float,
    wait_pr: float,
    interval: float = 60,
    sleep: Callable[[float], None] = time.sleep,
    date: str | None = None,
    ref: str = f"origin/{BASE_BRANCH}",
) -> Outcome:
    out = [f"mode: {mode} ({mode_note})"]
    if mode == "off":
        return Outcome(0, "\n".join(out + ["AUTO_RELEASE=off: nothing to do"]))

    date = date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    plan = make_plan(git, ref, level, date)
    out.append(render_plan(plan))
    if plan.stop:
        return Outcome(0, "\n".join(out))

    gate = evaluate_gate(forge, plan, git.repo, wait_ci, interval, sleep)
    if not gate.ok:
        problems = "\n".join(f"- {p}" for p in gate.problems)
        out += ["", "GATE RED -- no release today:", problems]
        if mode != "on":
            return Outcome(0, "\n".join(out))
        forge.report(
            f"Skipped release v{plan.version} on `{plan.sha[:12]}` ({date}): the gate "
            f"is red.\n\n{problems}"
        )
        return Outcome(1, "\n".join(out))
    out += ["", "gate: green", *(f"  {n}" for n in gate.notes)]

    if mode == "dry-run":
        out.append(f"dry-run: would open/update PR `{release_branch_name(plan.version or '')}`")
        return Outcome(0, "\n".join(out))

    try:
        number, head_sha = open_or_update_release_pr(forge, git, plan, gate, date)
    except ReleaseError as exc:
        forge.report(f"Skipped release v{plan.version} ({date}): {exc}")
        out.append(f"skipped: {exc}")
        return Outcome(1, "\n".join(out))
    out.append(f"release PR #{number} at {head_sha[:12]}")

    state, reasons = wait_for(lambda: pr_checks_state(forge, head_sha), wait_pr, interval, sleep)
    if state != "green":
        why = "red" if state == "red" else f"not finished after {int(wait_pr)}s"
        listed = "\n".join(f"- {r}" for r in reasons)
        forge.report(f"Release PR #{number} (v{plan.version}) left open: checks {why}.\n\n{listed}")
        out += [f"release PR #{number} checks {why}; left open", listed]
        return Outcome(1, "\n".join(out))
    forge.merge_pr(number, head_sha, f"{pr_title(plan.version or '')} (#{number})")
    out.append(f"merged release PR #{number}")
    return Outcome(0, "\n".join(out))


# --- CLI ------------------------------------------------------------------------


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"## Automated release\n\n```\n{text}\n```\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="offline: tag, merges, level, assembled section")
    p_run = sub.add_parser("run", help="plan + gate + open/merge the release PR")
    for p in (p_plan, p_run):
        p.add_argument("--ref", default=f"origin/{BASE_BRANCH}", help="branch/SHA to release")
        p.add_argument("--level", choices=("auto", *LEVELS), default="auto")
        p.add_argument("--date", help="release date YYYY-MM-DD (default: today, UTC)")
    p_plan.add_argument("--json", action="store_true")
    p_run.add_argument(
        "--mode",
        help="on|off|dry-run (default: from AUTO_RELEASE; unset -> dry-run)",
    )
    p_run.add_argument("--dispatch-dry-run", action="store_true")
    p_run.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO_SLUG))
    p_run.add_argument("--wait-ci", type=float, default=5400, help="seconds (default 90 min)")
    p_run.add_argument("--wait-pr", type=float, default=9000, help="seconds (default 150 min)")
    p_run.add_argument("--interval", type=float, default=60)
    args = parser.parse_args(argv)

    git = Git(REPO_ROOT)
    try:
        if args.command == "plan":
            plan = make_plan(git, args.ref, args.level, args.date)
            print(json.dumps(plan.to_json(), indent=2) if args.json else render_plan(plan))
            return 0
        if args.mode is not None:
            if args.mode not in MODES:
                parser.error(f"--mode must be one of {MODES}")
            mode, note = args.mode, "--mode"
            if mode == "on" and args.dispatch_dry_run:
                mode, note = "dry-run", "dry_run requested by workflow_dispatch"
        else:
            mode, note = resolve_mode(os.environ.get("AUTO_RELEASE"), args.dispatch_dry_run)
        forge = Forge(args.repo, write_token=os.environ.get("RELEASE_TOKEN") or None)
        outcome = run(
            mode=mode,
            mode_note=note,
            git=git,
            forge=forge,
            level=args.level,
            wait_ci=args.wait_ci,
            wait_pr=args.wait_pr,
            interval=args.interval,
            date=args.date,
            ref=args.ref,
        )
    except (ReleaseError, changelog_assemble.FragmentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        _write_summary(f"error: {exc}")
        return 2
    print(outcome.summary)
    _write_summary(outcome.summary)
    return outcome.code


if __name__ == "__main__":
    sys.exit(main())
