"""Tests for ``scripts/release_tag.py`` (issue #5777, epic #5774 Phase 3).

The tag this script pushes is what publishes to PyPI -- the irrevocable step --
so the release-merge detection, the pyproject cross-check, the idempotency
guards and the PyPI verification are pinned here against real git repos.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "release_tag.py"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load_module():
    spec = importlib.util.spec_from_file_location("release_tag", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_tag"] = module
    spec.loader.exec_module(module)
    return module


rt = _load_module()
rp = sys.modules["release_plan"]


# --- subject parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "version", "pr", "shape"),
    [
        ("chore(release): v0.23.0 (#5801)", "0.23.0", 5801, "automated"),
        ("Merge pull request #5772 from rjwalters/release/v0.22.0", "0.22.0", 5772, "manual"),
        ("Merge pull request #9 from someone/release/v1.0.0\n", "1.0.0", 9, "manual"),
    ],
)
def test_release_subjects_match(subject: str, version: str, pr: int, shape: str) -> None:
    merge = rt.parse_release_subject(subject)
    assert merge == rt.ReleaseMerge(version, pr, shape)
    assert merge.tag == f"v{version}"


@pytest.mark.parametrize(
    "subject",
    [
        "Merge pull request #5773 from rjwalters/ci/drop-cpuset-pins",
        "Merge pull request #5779 from rjwalters/feature/issue-5776",
        "chore(release): bump version to 0.22.0",  # the branch commit, not the merge
        "chore(release): v0.23.0",  # no PR suffix: not an API merge
        "chore(release): v0.23.0 (#5801) and more",
        "Merge pull request #1 from o/release/v0.23",
        "Merge pull request #1 from o/release/vBogus",
        "Merge pull request #1 from o/release/v0.23.0-rc1",
        "fix: mention release/v0.23.0 (#5)",
    ],
)
def test_other_subjects_do_not_match(subject: str) -> None:
    assert rt.parse_release_subject(subject) is None


# --- release notes ----------------------------------------------------------------

_CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-02-01

### Summary

Two things.

### Fixed

- **thing** (Issue #2).

## [0.1.0] - 2026-01-01

### Fixed

- first

[0.2.0]: https://github.com/o/r/releases/tag/v0.2.0
[0.1.0]: https://github.com/o/r/releases/tag/v0.1.0
"""


def test_release_notes_extracts_the_section_body() -> None:
    notes = rt.release_notes(_CHANGELOG, "0.2.0")
    assert notes.startswith("### Summary\n\nTwo things.")
    assert "- **thing** (Issue #2)." in notes
    assert "0.1.0" not in notes and "## [" not in notes


def test_release_notes_last_section_drops_footer_links() -> None:
    notes = rt.release_notes(_CHANGELOG, "0.1.0")
    assert notes == "### Fixed\n\n- first\n"


def test_release_notes_missing_or_empty_section_errors() -> None:
    with pytest.raises(rp.ReleaseError, match="no `## \\[9.9.9\\]`"):
        rt.release_notes(_CHANGELOG, "9.9.9")
    with pytest.raises(rp.ReleaseError, match="empty"):
        rt.release_notes("## [1.0.0] - x\n\n## [0.9.0]\n- y\n", "1.0.0")


def test_release_notes_for_this_repos_latest_release() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = rp.pyproject_version((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if f"## [{version}]" not in text:
        pytest.skip("pyproject version has no CHANGELOG section on this branch")
    assert rt.release_notes(text, version).strip()


# --- PyPI -------------------------------------------------------------------------


def _pypi(*types: str) -> dict[str, Any]:
    return {"urls": [{"packagetype": t, "filename": f"x.{t}"} for t in types]}


def test_missing_dists() -> None:
    assert rt.missing_dists(_pypi("bdist_wheel", "sdist")) == set()
    assert rt.missing_dists(_pypi("bdist_wheel")) == {"sdist"}
    assert rt.missing_dists(None) == {"bdist_wheel", "sdist"}


def test_wait_for_pypi_polls_until_both_files_and_is_bounded() -> None:
    answers = iter([None, _pypi("bdist_wheel"), _pypi("bdist_wheel", "sdist")])
    slept: list[float] = []
    assert rt.wait_for_pypi("1.0.0", 5, 7, lambda v: next(answers), slept.append) == set()
    assert slept == [7, 7]

    slept.clear()
    missing = rt.wait_for_pypi("1.0.0", 3, 1, lambda v: _pypi("bdist_wheel"), slept.append)
    assert missing == {"sdist"} and slept == [1, 1]  # no sleep after the last attempt


# --- git fixtures -----------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


def _pyproject(version: str) -> str:
    return f'[project]\nname = "kicad-tools"\nversion = "{version}"\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@example.com")
    (work / "pyproject.toml").write_text(_pyproject("0.1.0"))
    (work / "CHANGELOG.md").write_text(_CHANGELOG.replace("0.2.0", "0.0.9"))
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "tag", "-a", "v0.1.0", "-m", "Release 0.1.0")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main", "--tags")
    return work


def _merge(repo: Path, branch: str, subject: str, files: dict[str, str]) -> str:
    """Merge a branch into main with a merge commit titled ``subject``; push."""
    _git(repo, "checkout", "-q", "-b", branch)
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"change on {branch}")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", branch, "-m", subject)
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    return _git(repo, "rev-parse", "HEAD").strip()


def _head(repo: Path, merge_sha: str) -> str:
    """The merge's second parent: the release PR head the tag must land on."""
    return _git(repo, "rev-parse", f"{merge_sha}^2").strip()


def _release(repo: Path, version: str = "0.2.0", shape: str = "automated") -> str:
    subject = (
        f"chore(release): v{version} (#100)"
        if shape == "automated"
        else f"Merge pull request #100 from o/release/v{version}"
    )
    return _merge(
        repo,
        f"release/v{version}",
        subject,
        {"pyproject.toml": _pyproject(version), "CHANGELOG.md": _CHANGELOG},
    )


def _remote_tags(repo: Path) -> str:
    return _git(repo, "ls-remote", "--tags", "origin")


@pytest.mark.parametrize("shape", ["automated", "manual"])
def test_detect_release_merge_both_shapes_targets_the_second_parent(repo: Path, shape: str) -> None:
    sha = _release(repo, shape=shape)
    found = rt.detect(rp.Git(repo), sha)
    assert found.stop is None
    assert found.merge == rt.ReleaseMerge("0.2.0", 100, shape)
    assert found.sha == sha
    assert found.target == _head(repo, sha) != sha


def test_detect_ordinary_merge_is_quiet(repo: Path) -> None:
    sha = _merge(
        repo,
        "feature/issue-7",
        "Merge pull request #7 from o/feature/issue-7",
        {"a.txt": "x"},
    )
    found = rt.detect(rp.Git(repo), sha)
    assert found.merge is None and "not a release merge" in (found.stop or "")
    assert not found.loud


def test_detect_subject_without_pyproject_change_does_not_fire(repo: Path) -> None:
    """A subject that says release/v0.1.0 while pyproject stays at 0.1.0 (the
    version did not change in this merge) must never tag."""
    sha = _merge(
        repo, "release/v0.1.0", "Merge pull request #8 from o/release/v0.1.0", {"a.txt": "x"}
    )
    found = rt.detect(rp.Git(repo), sha)
    assert found.merge is None and "did not change" in (found.stop or "")


def test_detect_subject_version_disagreeing_with_pyproject_does_not_fire(repo: Path) -> None:
    sha = _merge(
        repo,
        "release/v0.3.0",
        "Merge pull request #9 from o/release/v0.3.0",
        {"pyproject.toml": _pyproject("0.2.0")},
    )
    found = rt.detect(rp.Git(repo), sha)
    assert found.merge is None and "pyproject.toml" in (found.stop or "")


@pytest.mark.parametrize(
    "subject", ["chore(release): v0.2.0 (#5)", "Merge pull request #5 from o/release/v0.2.0"]
)
def test_single_parent_release_commit_is_refused_loudly(repo: Path, subject: str) -> None:
    """A squash/rebase-merged release has no PR head on main's history: the
    automated path must refuse, and fail the run so a human tags by hand."""
    (repo / "pyproject.toml").write_text(_pyproject("0.2.0"))
    _git(repo, "commit", "-q", "-am", subject)
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    found = rt.detect(rp.Git(repo), "HEAD")
    assert found.merge is None and found.loud
    assert "not a merge commit" in (found.stop or "")
    outcome = _tag(repo, "on")
    assert outcome.code == 1 and "not a merge commit" in outcome.summary
    assert "v0.2.0" not in _remote_tags(repo)


def test_detect_refuses_a_merge_not_on_main(repo: Path) -> None:
    """A release-shaped merge on a side branch: neither it nor its second
    parent is reachable from origin/main, so nothing is tagged."""
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "checkout", "-q", "-b", "release/v0.2.0")
    (repo / "pyproject.toml").write_text(_pyproject("0.2.0"))
    _git(repo, "commit", "-q", "-am", "chore(release): bump")
    _git(repo, "checkout", "-q", "side")
    _git(repo, "merge", "-q", "--no-ff", "release/v0.2.0", "-m", "chore(release): v0.2.0 (#5)")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    found = rt.detect(rp.Git(repo), sha)
    assert found.merge is None and "not on origin/main" in (found.stop or "")


def test_detect_refuses_a_second_parent_not_on_the_base(repo: Path) -> None:
    """The ancestry check is on the second parent itself: against a base that
    does not contain the release PR head, nothing is tagged."""
    sha = _release(repo)
    _git(repo, "update-ref", "refs/remotes/origin/old-main", f"{sha}^1")
    found = rt.detect(rp.Git(repo), sha, base="origin/old-main")
    assert found.merge is None and "not on origin/old-main" in (found.stop or "")


def test_detect_existing_tag_is_a_quiet_noop(repo: Path) -> None:
    sha = _release(repo)
    head = _head(repo, sha)
    _git(repo, "tag", "-a", "v0.2.0", "-m", "by hand", head)
    _git(repo, "push", "-q", "origin", "v0.2.0")
    found = rt.detect(rp.Git(repo), sha)
    assert found.merge is None and "already exists" in (found.stop or "")
    assert "this commit" in (found.stop or "")


def _tag(repo: Path, mode: str, sha: str = "HEAD", exact: bool = False):
    return rt.run_tag(mode=mode, mode_note="test", git=rp.Git(repo), sha=sha, exact=exact)


@pytest.mark.parametrize("shape", ["automated", "manual"])
def test_run_tag_on_pushes_annotated_tag_on_the_release_pr_head(repo: Path, shape: str) -> None:
    sha = _release(repo, shape=shape)
    head = _head(repo, sha)
    outcome = _tag(repo, "on", sha)
    assert outcome.code == 0, outcome.summary
    assert "pushed v0.2.0" in outcome.summary
    tags = _remote_tags(repo)
    assert f"{head}\trefs/tags/v0.2.0^{{}}" in tags and sha not in tags
    assert _git(repo, "cat-file", "-t", "v0.2.0").strip() == "tag"  # annotated
    message = _git(repo, "tag", "-l", "--format=%(contents)", "v0.2.0")
    assert "Head of release PR #100" in message and sha[:12] in message


def test_prs_merged_while_the_release_pr_waited_are_not_in_the_tag(repo: Path) -> None:
    """The operator's reason for tagging the PR head: a PR merged to main
    after the release branch was cut is in the merge commit, not in the tag,
    and the next release plan counts it (and not the release merge)."""
    _git(repo, "checkout", "-q", "-b", "release/v0.2.0")
    (repo / "pyproject.toml").write_text(_pyproject("0.2.0"))
    (repo / "CHANGELOG.md").write_text(_CHANGELOG)
    _git(repo, "commit", "-q", "-am", "chore(release): bump version to 0.2.0")
    _git(repo, "checkout", "-q", "main")
    late = _merge(repo, "feature/late", "Merge pull request #7 from o/feature/late", {"late": "x"})
    _git(repo, "merge", "-q", "--no-ff", "release/v0.2.0", "-m", "chore(release): v0.2.0 (#100)")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    assert _tag(repo, "on", sha).code == 0
    git = rp.Git(repo)
    assert not _is_ancestor(repo, late, "v0.2.0")
    tip_commits = git.commits(f"v0.2.0..{sha}", first_parent=True)
    assert rp.merged_prs(tip_commits) == [7]


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=repo, check=False)
    return proc.returncode == 0


def test_run_tag_rerun_cannot_double_tag(repo: Path) -> None:
    sha = _release(repo)
    assert _tag(repo, "on", sha).code == 0
    again = _tag(repo, "on", sha)
    assert again.code == 0 and "already exists" in again.summary


def test_run_tag_dry_run_logs_and_pushes_nothing(repo: Path) -> None:
    sha = _release(repo)
    outcome = _tag(repo, "dry-run", sha)
    assert outcome.code == 0
    assert f"would push annotated tag v0.2.0 -> {_head(repo, sha)}" in outcome.summary
    assert "v0.2.0" not in _remote_tags(repo)
    assert _git(repo, "tag", "-l", "v0.2.0") == ""


def test_run_tag_off_does_nothing(repo: Path) -> None:
    _release(repo)
    outcome = _tag(repo, "off")
    assert outcome.code == 0 and "AUTO_RELEASE=off" in outcome.summary
    assert "v0.2.0" not in _remote_tags(repo)


def test_run_tag_ordinary_push_is_quiet(repo: Path) -> None:
    (repo / "a.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: resync installed Loom surfaces")
    _git(repo, "push", "-q", "origin", "main")
    outcome = _tag(repo, "on")
    assert outcome.code == 0 and "not a release merge" in outcome.summary


def test_phase2_guard_clears_once_tagged(repo: Path) -> None:
    """Phase 2 stops while pyproject is ahead of the latest tag; the tag this
    phase pushes on the second parent is reachable from main's tip, so it is
    exactly what clears it -- and the release merge is not a PR to re-release."""
    sha = _release(repo)
    git = rp.Git(repo)
    assert git.latest_tag(sha) == "v0.1.0"
    assert _tag(repo, "on", sha).code == 0
    assert git.latest_tag(sha) == "v0.2.0"
    assert rp.parse_version(git.latest_tag(sha)) == rp.parse_version(
        rp.pyproject_version(git.show(sha, "pyproject.toml"))
    )
    assert rp.merged_prs(git.commits(f"v0.2.0..{sha}", first_parent=True)) == []


# --- manual `sha` input (--exact) ------------------------------------------------------


def test_exact_tags_exactly_the_given_sha(repo: Path) -> None:
    sha = _release(repo)
    head = _head(repo, sha)
    outcome = _tag(repo, "on", head, exact=True)
    assert outcome.code == 0, outcome.summary
    assert f"{head}\trefs/tags/v0.2.0^{{}}" in _remote_tags(repo)
    # Given the merge commit instead, --exact tags the merge commit (in a fresh repo state).
    again = _tag(repo, "on", sha, exact=True)
    assert again.code == 0 and "not ahead of the latest tag v0.2.0" in again.summary


def test_exact_can_tag_the_merge_commit_when_asked(repo: Path) -> None:
    sha = _release(repo)
    outcome = _tag(repo, "on", sha, exact=True)
    assert outcome.code == 0, outcome.summary
    assert f"{sha}\trefs/tags/v0.2.0^{{}}" in _remote_tags(repo)


def test_exact_refuses_a_version_that_is_not_ahead(repo: Path) -> None:
    outcome = _tag(repo, "on", "HEAD", exact=True)
    assert outcome.code == 0 and "not ahead of the latest tag v0.1.0" in outcome.summary
    assert "v0.1.0" in _remote_tags(repo) and "v0.2.0" not in _remote_tags(repo)


def test_exact_refuses_a_sha_not_on_main(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "pyproject.toml").write_text(_pyproject("0.2.0"))
    _git(repo, "commit", "-q", "-am", "chore(release): bump version to 0.2.0")
    outcome = _tag(repo, "on", "HEAD", exact=True)
    assert outcome.code == 0 and "not on origin/main" in outcome.summary
    assert "v0.2.0" not in _remote_tags(repo)


def test_exact_existing_tag_is_a_quiet_noop(repo: Path) -> None:
    sha = _release(repo)
    head = _head(repo, sha)
    _git(repo, "tag", "-a", "v0.2.0", "-m", "by hand", sha)  # tagged elsewhere
    _git(repo, "push", "-q", "origin", "v0.2.0")
    found = rt.detect_exact(rp.Git(repo), head)
    assert found.merge is None and "already exists" in (found.stop or "")


# --- finalize -----------------------------------------------------------------------


class FakeForge(rt.ReleaseForge):
    def __init__(self, exists: bool = False) -> None:
        super().__init__("o/r", write_token="app")
        self.exists = exists
        self.calls: list[tuple] = []

    def release_exists(self, tag: str) -> bool:
        return self.exists

    def create_release(self, tag: str, notes: str) -> str:
        self.calls.append(("create_release", tag, notes))
        return f"https://github.com/o/r/releases/tag/{tag}"

    def report(self, body: str) -> None:
        self.calls.append(("report", body))


@pytest.fixture
def tagged(repo: Path) -> tuple[Path, str]:
    sha = _release(repo)
    assert _tag(repo, "on", sha).code == 0
    return repo, _head(repo, sha)  # the tagged commit publish.yml runs on


def _finalize(repo: Path, sha: str, forge: FakeForge, *, mode: str = "on", **kw: Any):
    return rt.run_finalize(
        mode=mode,
        mode_note="test",
        git=rp.Git(repo),
        forge=forge,
        tag=kw.pop("tag", "v0.2.0"),
        sha=sha,
        conclusion=kw.pop("conclusion", "success"),
        attempts=kw.pop("attempts", 2),
        interval=0,
        fetch=kw.pop("fetch", lambda v: _pypi("bdist_wheel", "sdist")),
        sleep=lambda _s: None,
        **kw,
    )


def test_finalize_creates_release_with_changelog_notes(tagged) -> None:
    repo, sha = tagged
    forge = FakeForge()
    outcome = _finalize(repo, sha, forge)
    assert outcome.code == 0, outcome.summary
    ((kind, tag, notes),) = forge.calls
    assert (kind, tag) == ("create_release", "v0.2.0")
    assert notes.startswith("### Summary") and "0.1.0" not in notes
    assert "wheel and sdist present" in outcome.summary


def test_finalize_existing_release_is_not_recreated(tagged) -> None:
    repo, sha = tagged
    forge = FakeForge(exists=True)
    outcome = _finalize(repo, sha, forge)
    assert outcome.code == 0 and forge.calls == []
    assert "already exists" in outcome.summary


def test_finalize_missing_sdist_fails_loudly_and_reports(tagged) -> None:
    repo, sha = tagged
    forge = FakeForge()
    outcome = _finalize(repo, sha, forge, fetch=lambda v: _pypi("bdist_wheel"))
    assert outcome.code == 1
    report = [c for c in forge.calls if c[0] == "report"]
    assert len(report) == 1 and "missing sdist" in report[0][1]


def test_finalize_failed_publish_reports_and_creates_nothing(tagged) -> None:
    repo, sha = tagged
    forge = FakeForge()
    outcome = _finalize(repo, sha, forge, conclusion="failure")
    assert outcome.code == 1
    assert [c[0] for c in forge.calls] == ["report"]
    assert "concluded `failure`" in forge.calls[0][1]


def test_finalize_dry_run_creates_and_reports_nothing(tagged) -> None:
    repo, sha = tagged
    forge = FakeForge()
    ok = _finalize(repo, sha, forge, mode="dry-run")
    assert ok.code == 0 and "would create GitHub Release v0.2.0" in ok.summary
    bad = _finalize(repo, sha, forge, mode="dry-run", fetch=lambda v: None)
    assert bad.code == 1
    assert forge.calls == []


def test_finalize_off_does_nothing(tagged) -> None:
    repo, sha = tagged
    forge = FakeForge()
    assert _finalize(repo, sha, forge, mode="off", conclusion="failure").code == 0
    assert forge.calls == []


@pytest.mark.parametrize("tag", ["main", "v0.2", "release/v0.2.0"])
def test_finalize_ignores_non_version_refs(tagged, tag: str) -> None:
    repo, sha = tagged
    forge = FakeForge()
    outcome = _finalize(repo, sha, forge, tag=tag)
    assert outcome.code == 0 and forge.calls == []


def test_finalize_ignores_a_tag_not_at_the_run_sha(tagged) -> None:
    repo, _sha = tagged
    forge = FakeForge()
    outcome = _finalize(repo, "0" * 40, forge)
    assert outcome.code == 0 and forge.calls == [] and "does not point at" in outcome.summary


def test_release_exists_distinguishes_404() -> None:
    class F(rt.ReleaseForge):
        def __init__(self, error: str | None) -> None:
            super().__init__("o/r")
            self.error = error

        def api(self, path, *, method="GET", body=None, write=False):
            if self.error:
                raise rp.ReleaseError(self.error)
            return {"id": 1}

    assert F(None).release_exists("v1.0.0") is True
    assert F("gh api GET x failed: gh: Not Found (HTTP 404)").release_exists("v1.0.0") is False
    with pytest.raises(rp.ReleaseError):
        F("HTTP 500").release_exists("v1.0.0")


# --- workflows ------------------------------------------------------------------------


def _wf(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


MODE_CASES = [
    (None, "dry-run"),
    ("", "dry-run"),
    ("on", "on"),
    ("ON ", "on"),
    ("off", "off"),
    ("dry-run", "dry-run"),
    ("bogus", "dry-run"),
]


@pytest.mark.parametrize("workflow", ["release-tag.yml", "release-finalize.yml"])
@pytest.mark.parametrize(("variable", "expected"), MODE_CASES)
def test_workflow_mode_step_matches_resolve_mode(
    tmp_path: Path, workflow: str, variable: str | None, expected: str
) -> None:
    assert rp.resolve_mode(variable)[0] == expected
    (job,) = _wf(workflow)["jobs"].values()
    step = next(s for s in job["steps"] if s.get("id") == "mode")
    out = tmp_path / "out"
    env = {
        "PATH": os.environ["PATH"],
        "AUTO_RELEASE": variable or "",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    subprocess.run(["bash", "-e", "-c", step["run"]], env=env, check=True, capture_output=True)
    assert out.read_text().strip() == f"mode={expected}"


def _app_token_step(job: dict[str, Any]) -> dict[str, Any]:
    token = next(s for s in job["steps"] if s.get("id") == "app-token")
    assert token["uses"].startswith("actions/create-github-app-token@")
    assert len(token["uses"].split("@")[1].split()[0]) == 40  # pinned by SHA
    assert "LOOM_FLEET_DISPATCH_APP_ID" in token["with"]["app-id"]
    assert "LOOM_FLEET_DISPATCH_APP_PRIVATE_KEY" in token["with"]["private-key"]
    return token


def test_release_tag_workflow_shape() -> None:
    wf = _wf("release-tag.yml")
    assert wf[True]["push"] == {"branches": ["main"]}  # PyYAML parses `on:` as True
    assert "write" not in wf["permissions"].values()  # the tag push uses the App token
    job = wf["jobs"]["release-tag"]
    assert _app_token_step(job)["with"]["permission-contents"] == "write"
    checkout = next(
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert "app-token" in checkout["with"]["token"]
    final = job["steps"][-1]
    assert "release_tag.py tag" in final["run"] and "github.sha" in final["env"]["SHA"]
    assert "inputs.sha" in final["env"]["SHA"] and "inputs.sha" in final["env"]["EXACT"]
    assert "--exact" in final["run"]


def test_release_finalize_workflow_shape() -> None:
    wf = _wf("release-finalize.yml")
    trigger = wf[True]["workflow_run"]
    publish = _wf("publish.yml")
    assert trigger["workflows"] == [publish["name"]]
    assert trigger["types"] == ["completed"]
    assert "write" not in wf["permissions"].values()
    job = wf["jobs"]["release-finalize"]
    assert "workflow_run.event == 'push'" in job["if"]
    _app_token_step(job)
    final = job["steps"][-1]
    assert "release_tag.py finalize" in final["run"]
    assert final["env"]["TAG"] == "${{ github.event.workflow_run.head_branch }}"
    assert "app-token" in final["env"]["RELEASE_TOKEN"]


def test_publish_workflow_still_triggers_on_tags_only() -> None:
    """Trusted publishing names publish.yml; Phase 3 relies on it unchanged."""
    assert _wf("publish.yml")[True] == {"push": {"tags": ["v*"]}}


def test_cli_notes_against_this_repo() -> None:
    version = rp.pyproject_version((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "notes", version, "--ref", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if "dubious ownership" in proc.stderr:
        # Local fallback only (see test_release_plan); CI sets safe.directory.
        pytest.skip("git refuses repo: dubious ownership (container uid)")
    if proc.returncode == 2 and "no `## [" in proc.stderr:
        pytest.skip("pyproject version has no CHANGELOG section on this branch")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()
