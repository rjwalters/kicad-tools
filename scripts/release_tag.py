#!/usr/bin/env python3
"""Tag a merged release, then create its GitHub Release once PyPI has it
(issue #5777, Phase 3 of epic #5774).

Two workflows call this script; all decisions live here so they can be
unit-tested (``tests/test_release_tag.py``).

``tag`` -- ``.github/workflows/release-tag.yml``, on every push to ``main``
--------------------------------------------------------------------------
1. **Detect a release merge** on the pushed SHA.  Two commit subjects count:

   - ``chore(release): vX.Y.Z (#N)`` -- the automated release PR, merged by
     ``release_plan.py`` through the API (Phase 2, #5776);
   - ``Merge pull request #N from <owner>/release/vX.Y.Z`` -- a hand-opened
     release PR merged by ``merge-pr.sh`` or the GitHub button (RELEASING.md's
     manual fallback).

   Anything else exits quietly -- that is almost every push.
2. **Cross-check** against the tree, so a coincidentally worded subject can
   never tag: ``pyproject.toml``'s ``[project].version`` at the SHA must equal
   the subject's version, and must differ from the version at the first
   parent (the merge really bumped it).  The SHA must be on ``origin/main``.
3. **Idempotency**: if ``vX.Y.Z`` already exists on ``origin``, exit quietly
   (a rerun, or a human who tagged first).
4. **Mode** (``AUTO_RELEASE``, same resolution as Phase 2): ``off`` exits at
   once; ``dry-run`` (and unset) logs the tag it would push; ``on`` creates an
   annotated ``vX.Y.Z`` on the SHA and pushes it.  The push authenticates as
   the loom-fleet-dispatch App, so ``publish.yml``'s ``push: tags`` trigger
   fires (a ``GITHUB_TOKEN`` push would not).

``finalize`` -- ``.github/workflows/release-finalize.yml``, on ``workflow_run``
------------------------------------------------------------------------------
Runs when ``Publish to PyPI`` completes.  For a tag-triggered run the
payload's ``head_branch`` is the tag name and ``head_sha`` the tagged commit.

1. Not a ``vX.Y.Z`` tag, or the tag no longer points at ``head_sha``: quiet
   exit.
2. Publish concluded anything but ``success``: report on the "Automated
   release status" issue (``on``) and fail.
3. Create the GitHub Release ``vX.Y.Z`` with the ``## [X.Y.Z]`` section of
   ``CHANGELOG.md`` (at the tag) as its notes -- unless it already exists.
4. Poll ``https://pypi.org/pypi/kicad-tools/X.Y.Z/json`` (bounded) until both
   a ``bdist_wheel`` and an ``sdist`` are listed; otherwise report and fail.

Usage
-----
    uv run python scripts/release_tag.py tag --sha HEAD --mode dry-run
    uv run python scripts/release_tag.py notes 0.22.0      # print release notes
    uv run python scripts/release_tag.py finalize --tag v0.22.0 --sha <sha> \\
        --conclusion success --mode dry-run

Exit codes
----------
    0 -- done (tagged, finalized, dry-run reported, not a release, or ``off``).
    1 -- a release that should have completed did not (publish failed, PyPI
         missing files, tag push failed).
    2 -- usage / environment error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_plan as rp  # noqa: E402  (sibling script, not a package)

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/{version}/json"
REQUIRED_DISTS = frozenset({"bdist_wheel", "sdist"})

_AUTO_SUBJECT = re.compile(r"^chore\(release\): v(?P<version>\d+\.\d+\.\d+) \(#(?P<pr>\d+)\)$")
_MANUAL_SUBJECT = re.compile(
    r"^Merge pull request #(?P<pr>\d+) from [^/\s]+/release/v(?P<version>\d+\.\d+\.\d+)$"
)
_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


@dataclass
class Outcome:
    code: int
    summary: str


# --- detection ----------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseMerge:
    version: str
    pr: int
    shape: str  # "automated" | "manual"

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def parse_release_subject(subject: str) -> ReleaseMerge | None:
    """The release a commit subject announces, or ``None``."""
    subject = subject.strip()
    for regex, shape in ((_AUTO_SUBJECT, "automated"), (_MANUAL_SUBJECT, "manual")):
        match = regex.match(subject)
        if match:
            return ReleaseMerge(match.group("version"), int(match.group("pr")), shape)
    return None


def remote_tag_sha(git: rp.Git, tag: str) -> str | None:
    """The commit ``origin``'s ``tag`` points at (peeled), or ``None``."""
    out = git("ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    refs: dict[str, str] = {}
    for line in out.splitlines():
        if "\t" in line:
            sha, ref = line.split("\t", 1)
            refs[ref.strip()] = sha.strip()
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


@dataclass
class Detection:
    sha: str
    merge: ReleaseMerge | None = None
    stop: str | None = None  # a reason not to tag (quiet unless it looks wrong)


def detect(git: rp.Git, sha: str, base: str = f"origin/{rp.BASE_BRANCH}") -> Detection:
    """Decide whether ``sha`` is a release merge that still needs its tag."""
    sha = git.rev_parse(f"{sha}^{{commit}}")
    found = Detection(sha)
    subject = git("log", "-1", "--format=%s", sha).strip()
    merge = parse_release_subject(subject)
    if merge is None:
        found.stop = f"not a release merge: {subject!r}"
        return found
    at_sha = rp.pyproject_version(git.show(sha, "pyproject.toml"))
    if at_sha != merge.version:
        found.stop = (
            f"subject names v{merge.version} but pyproject.toml at {sha[:12]} is {at_sha}: "
            "not tagging"
        )
        return found
    parent = git("rev-parse", "--verify", "--quiet", f"{sha}^1", check=False).strip()
    if parent and rp.pyproject_version(git.show(parent, "pyproject.toml")) == at_sha:
        found.stop = (
            f"subject names v{merge.version} but this commit did not change pyproject.toml's "
            "version: not tagging"
        )
        return found
    if not _is_ancestor(git, sha, base):
        found.stop = f"{sha[:12]} is not on {base}: only a merged main SHA is ever tagged"
        return found
    existing = remote_tag_sha(git, merge.tag)
    if existing is not None:
        where = "this commit" if existing == sha else f"{existing[:12]}, not {sha[:12]}"
        found.stop = f"{merge.tag} already exists on origin (at {where}): nothing to do"
        return found
    found.merge = merge
    return found


def _is_ancestor(git: rp.Git, sha: str, base: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, base],
        cwd=git.repo,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def tag_message(merge: ReleaseMerge) -> str:
    return f"Release {merge.version}\n\nRelease PR #{merge.pr} ({merge.shape})."


def run_tag(*, mode: str, mode_note: str, git: rp.Git, sha: str) -> Outcome:
    out = [f"mode: {mode} ({mode_note})"]
    if mode == "off":
        return Outcome(0, "\n".join(out + ["AUTO_RELEASE=off: not tagging"]))
    found = detect(git, sha)
    if found.merge is None:
        out.append(f"{found.sha[:12]}: {found.stop}")
        return Outcome(0, "\n".join(out))
    merge = found.merge
    out.append(
        f"{found.sha[:12]} is the {merge.shape} release merge of PR #{merge.pr} (v{merge.version})"
    )
    if mode != "on":
        out.append(f"dry-run: would push annotated tag {merge.tag} -> {found.sha}")
        return Outcome(0, "\n".join(out))
    git("tag", "-a", merge.tag, "-m", tag_message(merge), found.sha)
    try:
        git("push", "origin", f"refs/tags/{merge.tag}")
    except rp.ReleaseError as exc:
        # Lost a race with a human (or a concurrent rerun) who pushed the same tag.
        if remote_tag_sha(git, merge.tag) == found.sha:
            out.append(f"{merge.tag} appeared on origin at {found.sha[:12]} meanwhile: fine")
            return Outcome(0, "\n".join(out))
        out.append(f"tag push failed: {exc}")
        return Outcome(1, "\n".join(out))
    out.append(f"pushed {merge.tag} -> {found.sha} (publish.yml runs on this tag)")
    return Outcome(0, "\n".join(out))


# --- release notes --------------------------------------------------------------


def release_notes(changelog_text: str, version: str) -> str:
    """The body of ``## [version]`` (heading excluded), up to the next ``## `` heading.

    Footer link definitions (``[x.y.z]: https://...``) are dropped: the section
    body never contains them, but the last section runs to end of file.
    """
    heading = re.compile(rf"^##\s*\[{re.escape(version)}\][^\n]*\n?", re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        raise rp.ReleaseError(f"CHANGELOG.md has no `## [{version}]` section")
    nxt = re.compile(r"^##\s", re.MULTILINE).search(changelog_text, match.end())
    body = changelog_text[match.end() : nxt.start() if nxt else len(changelog_text)]
    lines = [ln for ln in body.splitlines() if not re.match(r"^\[[^\]]+\]:\s*\S", ln)]
    notes = "\n".join(lines).strip("\n")
    if not notes.strip():
        raise rp.ReleaseError(f"CHANGELOG.md `## [{version}]` section is empty")
    return notes + "\n"


# --- PyPI -------------------------------------------------------------------------


def fetch_pypi(version: str, package: str = rp.PACKAGE_NAME) -> dict[str, Any] | None:
    """PyPI's JSON for one release, or ``None`` while it is not there yet."""
    url = PYPI_JSON_URL.format(package=package, version=version)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https URL)
            data: dict[str, Any] = json.load(resp)
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def missing_dists(data: dict[str, Any] | None) -> set[str]:
    """Which of :data:`REQUIRED_DISTS` a PyPI release JSON lacks."""
    present = {u.get("packagetype") for u in (data or {}).get("urls", [])}
    return set(REQUIRED_DISTS - present)


def wait_for_pypi(
    version: str,
    attempts: int,
    interval: float,
    fetch: Callable[[str], dict[str, Any] | None] = fetch_pypi,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str]:
    """Poll until PyPI lists a wheel and an sdist; return what is still missing."""
    missing = set(REQUIRED_DISTS)
    for attempt in range(max(attempts, 1)):
        missing = missing_dists(fetch(version))
        if not missing:
            return missing
        if attempt + 1 < attempts:
            sleep(interval)
    return missing


# --- finalize -----------------------------------------------------------------------


class ReleaseForge(rp.Forge):
    """:class:`release_plan.Forge` plus GitHub Releases."""

    def release_exists(self, tag: str) -> bool:
        try:
            self.api(f"{{repo}}/releases/tags/{tag}")
        except rp.ReleaseError as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                return False
            raise
        return True

    def create_release(self, tag: str, notes: str) -> str:
        created: dict[str, Any] = self.api(
            "{repo}/releases",
            method="POST",
            body={"tag_name": tag, "name": tag, "body": notes, "make_latest": "true"},
            write=True,
        )
        return str(created.get("html_url", ""))


def run_finalize(
    *,
    mode: str,
    mode_note: str,
    git: rp.Git,
    forge: ReleaseForge,
    tag: str,
    sha: str,
    conclusion: str,
    run_url: str = "",
    attempts: int = 10,
    interval: float = 30,
    fetch: Callable[[str], dict[str, Any] | None] = fetch_pypi,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    out = [f"mode: {mode} ({mode_note})"]
    if mode == "off":
        return Outcome(0, "\n".join(out + ["AUTO_RELEASE=off: not finalizing"]))
    if not _TAG.match(tag):
        out.append(f"{tag!r} is not a vX.Y.Z tag: nothing to finalize")
        return Outcome(0, "\n".join(out))
    tagged = git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", check=False)
    if tagged.strip() != sha:
        out.append(f"{tag} does not point at {sha[:12]} (got {tagged.strip()[:12] or 'none'})")
        return Outcome(0, "\n".join(out))
    version = tag.removeprefix("v")
    where = f" ({run_url})" if run_url else ""

    def fail(problem: str) -> Outcome:
        out.append(problem)
        if mode == "on":
            forge.report(f"Release {tag} (`{sha[:12]}`): {problem}{where}")
        return Outcome(1, "\n".join(out))

    if conclusion != "success":
        return fail(
            f"`publish.yml` concluded `{conclusion}`; no GitHub Release created. Re-run the "
            "publish run (it skips files PyPI already has), or follow RELEASING.md's manual "
            "fallback."
        )
    notes = release_notes(git.show(sha, "CHANGELOG.md"), version)
    if forge.release_exists(tag):
        out.append(f"GitHub Release {tag} already exists: not recreating")
    elif mode == "on":
        url = forge.create_release(tag, notes)
        out.append(f"created GitHub Release {tag} {url}".rstrip())
    else:
        out.append(f"dry-run: would create GitHub Release {tag} ({len(notes)} chars of notes)")

    missing = wait_for_pypi(version, attempts, interval, fetch, sleep)
    if missing:
        return fail(
            f"PyPI `{rp.PACKAGE_NAME}` {version} is missing {', '.join(sorted(missing))} after "
            f"{attempts} checks; `publish.yml` reported success. Check "
            f"https://pypi.org/project/{rp.PACKAGE_NAME}/{version}/ and re-run the publish run."
        )
    out.append(f"PyPI {rp.PACKAGE_NAME} {version}: wheel and sdist present")
    return Outcome(0, "\n".join(out))


# --- CLI ------------------------------------------------------------------------------


def _write_summary(title: str, text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"## {title}\n\n```\n{text}\n```\n")


def _mode(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, str]:
    if args.mode is not None:
        if args.mode not in rp.MODES:
            parser.error(f"--mode must be one of {rp.MODES}")
        return args.mode, "--mode"
    mode, note = rp.resolve_mode(os.environ.get("AUTO_RELEASE"))
    return str(mode), str(note)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_tag = sub.add_parser("tag", help="tag the release merge at --sha (release-tag.yml)")
    p_tag.add_argument("--sha", default="HEAD")
    p_fin = sub.add_parser("finalize", help="GitHub Release + PyPI check (release-finalize.yml)")
    p_fin.add_argument("--tag", required=True)
    p_fin.add_argument("--sha", required=True)
    p_fin.add_argument("--conclusion", required=True)
    p_fin.add_argument("--run-url", default="")
    p_fin.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", rp.DEFAULT_REPO_SLUG))
    p_fin.add_argument("--attempts", type=int, default=10)
    p_fin.add_argument("--interval", type=float, default=30)
    for p in (p_tag, p_fin):
        p.add_argument(
            "--mode", help="on|off|dry-run (default: from AUTO_RELEASE; unset -> dry-run)"
        )
    p_notes = sub.add_parser("notes", help="print the release notes for a version")
    p_notes.add_argument("version")
    p_notes.add_argument("--ref", default="HEAD")
    args = parser.parse_args(argv)

    git = rp.Git(rp.REPO_ROOT)
    title = "Release tag" if args.command == "tag" else "Release finalize"
    try:
        if args.command == "notes":
            print(release_notes(git.show(args.ref, "CHANGELOG.md"), args.version), end="")
            return 0
        mode, note = _mode(args, parser)
        if args.command == "tag":
            outcome = run_tag(mode=mode, mode_note=note, git=git, sha=args.sha)
        else:
            forge = ReleaseForge(args.repo, write_token=os.environ.get("RELEASE_TOKEN") or None)
            outcome = run_finalize(
                mode=mode,
                mode_note=note,
                git=git,
                forge=forge,
                tag=args.tag,
                sha=args.sha,
                conclusion=args.conclusion,
                run_url=args.run_url,
                attempts=args.attempts,
                interval=args.interval,
            )
    except rp.ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _write_summary(title, f"error: {exc}")
        return 2
    print(outcome.summary)
    _write_summary(title, outcome.summary)
    return outcome.code


if __name__ == "__main__":
    sys.exit(main())
