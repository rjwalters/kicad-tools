#!/usr/bin/env python3
"""Select modified routed PCBs for the `routed-pcb-drc-check` CI job (issue #5349).

Replaces the inline-bash file-selection logic that previously diffed the
event's *stale* base SHA (``github.event.pull_request.base.sha``, fixed at
webhook-delivery time) against the checked-out synthetic merge commit. When
the target branch (``main``) advances between the webhook firing and the job
actually running, that stale SHA is still an ancestor of the merge commit, so
a three-dot diff (``git diff base...HEAD``) silently widens to include every
commit merged into ``main`` in between -- enumerating unrelated routed boards
for a PR that never touched them (observed on PR #4955, a compiler-only PR
that spuriously selected all eight routed boards).

Correct provenance instead:

* ``pull_request`` events -- the checked-out ref is (by default) the
  synthetic merge of the PR head onto the *current* base tip. That merge
  commit has exactly two parents: parent 1 is the current base tip (always
  fresh, no drift possible), parent 2 is the PR head commit. We verify parent
  2 matches the event's reported head SHA (defending against an unexpected
  checkout topology, e.g. a merge conflict that leaves the direct head
  checked out instead) and then diff against parent 1. We deliberately do
  NOT fall back to the stale ``base.sha`` -- an unsupported topology is a
  hard failure, not a guess.
* ``push`` events -- diff the explicit before/current commit pair GitHub
  provides. A missing/all-zero ``before`` (first push to a fresh ref, or a
  force-push GitHub could not resolve) means the *full range of commits the
  push introduced* is unknowable, so we diff against the **empty tree**:
  every file in the pushed commit's tree counts as changed. That is
  deliberately conservative (it over-selects) because every cheaper
  approximation silently *under*-selects. In particular we do NOT fall back
  to the pushed commit's own parent (``head^``): a push can introduce many
  commits, and ``head^..head`` inspects only the last one -- a routed board
  added by any earlier commit in the same push would be silently skipped.
  Nor do we fall back to a ref like ``origin/main``, which immediately after
  a push to that same ref already IS ``head`` -- a self-diff that always
  yields an empty, falsely-reassuring result.

Git failures (unresolvable revision, non-zero ``git diff`` exit code) are
distinct from a genuinely empty match: the former raises and the CLI exits
non-zero; the latter returns an empty result and the CLI exits 0. The caller
(the CI workflow step) must not swallow a non-zero exit with ``|| true``.

Infrastructure self-coverage: a PR that touches this job's own selection or
native-execution machinery (see ``INFRA_TRIGGER_PATHS``) automatically adds
Board04's canonical routed PCB to the *native* selection, even when that PCB
itself is unmodified -- so a CI-infrastructure PR exercises the real native
path it changes, instead of silently taking the cheap no-files shortcut.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors the pattern the workflow used inline before this refactor.
ROUTED_PCB_PATTERN = re.compile(r"^boards/[^/]+/output/[^/]+_routed\.kicad_pcb$")

# Board04's committed routed PCB is dispatched through the reviewed paid
# mechanical-drill validator (scripts/ci/check_routed_drc.py's
# ``PAID_DRILL_BOARD``), which requires a working native ``kicad-cli`` for
# its strict ERC step. Kept as a literal (rather than importing
# ``check_routed_drc`` here) so this module stays dependency-free and
# importable/runnable with the stdlib interpreter alone, before ``uv sync``
# has installed third-party packages like PyYAML that ``check_routed_drc``
# needs at import time. ``tests/test_select_routed_pcbs.py`` pins that this
# stays in sync with ``check_routed_drc.PAID_DRILL_BOARD``.
NATIVE_BOARD_RELATIVE_PATH = "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"

# Paths whose modification makes this job's OWN selection/native-execution
# behavior part of the diff. Touching any of these forces a real Board04
# native smoke run (see module docstring) so the infrastructure fix under
# test in a given PR is actually exercised end-to-end by that PR's own CI
# run, rather than relying on a future, unrelated PR to touch Board04.
INFRA_TRIGGER_PATHS = frozenset(
    {
        ".github/workflows/ci.yml",
        "scripts/ci/check_routed_drc.py",
        "scripts/ci/select_routed_pcbs.py",
        "scripts/ci/run_native_routed_drc.sh",
        "scripts/ci/init_kicad_libraries.py",
        "boards/04-stm32-devboard/check_manufacturing.py",
        "boards/04-stm32-devboard/manufacturing_process.py",
    }
)

ZERO_SHA = "0" * 40


class SelectionError(RuntimeError):
    """Base class for a failure that must NOT be silently treated as
    "no files selected". The CLI maps any subclass to a non-zero exit."""


class GitCommandError(SelectionError):
    """A git subprocess (rev-parse/rev-list/diff) failed or produced
    unusable output."""


class UnsupportedTopologyError(SelectionError):
    """The checked-out ref's parentage did not match what a `pull_request`
    event is expected to produce. Refusing to guess rather than silently
    falling back to a possibly-stale base."""


@dataclass(frozen=True)
class SelectionResult:
    base: str
    all_files: list[str] = field(default_factory=list)
    native_files: list[str] = field(default_factory=list)
    ordinary_files: list[str] = field(default_factory=list)
    infra_triggered: bool = False


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _revision_exists(repo_root: Path, revision: str) -> bool:
    proc = _run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
    return proc.returncode == 0


def resolve_empty_tree(repo_root: Path) -> str:
    """Return this repository's empty-tree object id.

    Asked of ``git`` rather than hardcoding the familiar SHA-1 constant so
    the value is also correct in a SHA-256 repository. Git resolves the
    empty tree even when that object was never written to the object
    database, so ``git diff <empty-tree> <commit>`` works in a brand-new
    repository too.
    """
    proc = _run_git(repo_root, ["hash-object", "-t", "tree", os.devnull])
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise GitCommandError(
            "failed to resolve the empty-tree object id "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return sha


def resolve_push_base(repo_root: Path, head_sha: str, before: str | None) -> str:
    """Resolve the diff base for a ``push`` event.

    Explicit zero/missing-``before`` policy: **diff against the empty
    tree**, so every file in ``head_sha``'s tree is treated as changed.

    A push can introduce an arbitrary number of commits, and a zero/missing
    ``before`` means GitHub could not tell us where that range starts. Every
    cheaper approximation under-selects silently:

    * ``head^..head`` inspects only the push's *final* commit -- a routed
      board added by an earlier commit of the same push is missed.
    * a fallback ref such as ``origin/main`` already points AT ``head_sha``
      immediately after a push to that ref, so the diff is a self-comparison
      that is empty by construction.

    Over-selection (a full scan of the pushed tree) is the only failure mode
    that cannot hide a modified board, so that is the one we take. The
    explicit valid before/current pair is returned verbatim and is entirely
    unaffected by this branch.
    """
    if before and before != ZERO_SHA:
        return before

    if not _revision_exists(repo_root, head_sha):
        raise GitCommandError(
            f"push event has no usable 'before' commit (got {before!r}) and "
            f"the pushed commit {head_sha!r} does not resolve; cannot "
            "compute a diff base."
        )

    return resolve_empty_tree(repo_root)


def resolve_pr_base(repo_root: Path, head_sha: str, pr_head_sha: str | None) -> str:
    """Resolve the diff base for a ``pull_request`` event.

    Validates that ``head_sha`` (the checked-out ref, ``github.sha``) is a
    two-parent merge commit whose second parent matches the PR's actual head
    commit, then returns the first parent (the base branch tip at merge
    time -- always fresh, never the stale event-payload ``base.sha``).

    Raises :class:`UnsupportedTopologyError` if the checkout is not the
    expected merge-commit shape (e.g. the PR could not be auto-merged due to
    conflicts, or an unexpected explicit ``ref:`` override was used) rather
    than guessing at a base that might silently mis-scope the diff.
    """
    proc = _run_git(repo_root, ["rev-list", "--parents", "-n", "1", head_sha])
    if proc.returncode != 0:
        raise GitCommandError(f"failed to resolve parents of {head_sha!r}: {proc.stderr.strip()}")
    parts = proc.stdout.split()
    if not parts:
        raise GitCommandError(f"'git rev-list --parents' returned no output for {head_sha!r}")

    parents = parts[1:]  # parts[0] is head_sha itself
    if len(parents) != 2:
        raise UnsupportedTopologyError(
            f"expected {head_sha!r} to be a 2-parent merge commit (the "
            "standard pull_request checkout of PR-head-merged-onto-base), "
            f"but it has {len(parents)} parent(s): {parents}. This usually "
            "means the PR could not be auto-merged (conflicts) or the "
            "checkout ref was overridden. Refusing to guess a base; "
            "resolve the conflict (or the checkout configuration) and "
            "re-run."
        )

    base, second_parent = parents
    if pr_head_sha and second_parent != pr_head_sha:
        raise UnsupportedTopologyError(
            f"merge commit {head_sha!r}'s second parent {second_parent!r} "
            f"does not match the expected PR head {pr_head_sha!r}. Refusing "
            "to guess the correct base rather than risk silently diffing "
            "against the wrong ancestor."
        )
    return base


def run_git_diff(repo_root: Path, base: str, head: str) -> list[str]:
    """Return the list of added/modified files between ``base`` and ``head``.

    Raises :class:`GitCommandError` on a non-zero ``git diff`` exit code --
    this must be distinguishable from a legitimate empty result (an empty
    list with exit 0) so a broken revision can never masquerade as "nothing
    changed".
    """
    proc = _run_git(repo_root, ["diff", "--name-only", "--diff-filter=AM", base, head])
    if proc.returncode != 0:
        raise GitCommandError(
            f"'git diff' failed (base={base!r} head={head!r}, exit "
            f"{proc.returncode}): {proc.stderr.strip()}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def select_routed_pcbs(
    repo_root: Path,
    event_name: str,
    head_sha: str,
    *,
    push_before: str | None = None,
    pr_head_sha: str | None = None,
    fallback_ref: str = "origin/main",
) -> SelectionResult:
    """Compute the routed-PCB selection for the current CI event.

    Raises :class:`SelectionError` (or a subclass) on any failure to
    resolve a base revision or run ``git diff`` -- callers must propagate
    this as a job failure, never as an empty-success selection.
    """
    if event_name == "push":
        base = resolve_push_base(repo_root, head_sha, push_before)
    elif event_name == "pull_request":
        base = resolve_pr_base(repo_root, head_sha, pr_head_sha)
    else:
        # This job only triggers on push/pull_request today (see the `on:`
        # block in ci.yml). Handled explicitly (rather than left to error
        # out deep inside git) so an unexpected future trigger fails with an
        # attributable message instead of a confusing git error.
        base = fallback_ref

    files = run_git_diff(repo_root, base, head_sha)
    infra_triggered = any(f in INFRA_TRIGGER_PATHS for f in files)

    matched = {f for f in files if ROUTED_PCB_PATTERN.match(f)}
    native_files = {f for f in matched if f == NATIVE_BOARD_RELATIVE_PATH}
    if infra_triggered:
        native_files.add(NATIVE_BOARD_RELATIVE_PATH)
    ordinary_files = matched - native_files
    all_files = native_files | ordinary_files

    return SelectionResult(
        base=base,
        all_files=sorted(all_files),
        native_files=sorted(native_files),
        ordinary_files=sorted(ordinary_files),
        infra_triggered=infra_triggered,
    )


def _write_multiline_output(fh, name: str, values: list[str]) -> None:
    """Append a `$GITHUB_OUTPUT`-format multi-line value (matches the
    `EOF`-delimited heredoc form the workflow previously wrote by hand)."""
    fh.write(f"{name}<<SELECT_ROUTED_PCBS_EOF\n")
    for value in values:
        fh.write(f"{value}\n")
    fh.write("SELECT_ROUTED_PCBS_EOF\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="select_routed_pcbs",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--event-name", required=True, help="GitHub Actions `github.event_name`.")
    parser.add_argument(
        "--sha",
        required=True,
        help="The checked-out commit (`github.sha`) -- the PR merge commit "
        "for pull_request events, or the pushed commit for push events.",
    )
    parser.add_argument(
        "--push-before",
        default=None,
        help="`github.event.before` for push events. Ignored otherwise.",
    )
    parser.add_argument(
        "--pr-head-sha",
        default=None,
        help="`github.event.pull_request.head.sha` for pull_request events. Ignored otherwise.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to run git commands in (default: this repo).",
    )
    parser.add_argument(
        "--fallback-ref",
        default="origin/main",
        help="Ref used as the diff base for event types other than "
        "push/pull_request (default: origin/main). Push events with a "
        "missing/zero 'before' deliberately do NOT use this -- they diff "
        "against the empty tree instead; see the module docstring.",
    )
    parser.add_argument(
        "--github-output",
        default=None,
        help="Path to append `$GITHUB_OUTPUT`-format `files`/`native_files`/"
        "`ordinary_files` entries to. Typically `$GITHUB_OUTPUT` in CI.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    try:
        result = select_routed_pcbs(
            repo_root,
            args.event_name,
            args.sha,
            push_before=args.push_before,
            pr_head_sha=args.pr_head_sha,
            fallback_ref=args.fallback_ref,
        )
    except SelectionError as e:
        print(f"::error::routed PCB selection failed: {e}", flush=True)
        return 1

    print(f"Diffing against base: {result.base}")
    if result.infra_triggered:
        print(
            "Validation-infrastructure path changed -- forcing a real "
            f"native smoke run against {NATIVE_BOARD_RELATIVE_PATH}."
        )
    if not result.all_files:
        print("No routed PCBs modified -- skipping DRC check.")
    else:
        print("Selected routed PCBs:")
        for f in result.all_files:
            tag = "native" if f in result.native_files else "ordinary"
            print(f"  [{tag}] {f}")

    if args.github_output:
        with open(args.github_output, "a") as fh:
            _write_multiline_output(fh, "files", result.all_files)
            _write_multiline_output(fh, "native_files", result.native_files)
            _write_multiline_output(fh, "ordinary_files", result.ordinary_files)

    return 0


if __name__ == "__main__":
    sys.exit(main())
