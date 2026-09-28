"""Tests for ``scripts/release_plan.py`` (issue #5776, epic #5774 Phase 2).

The daily release workflow runs this script unattended and its last step is an
irreversible merge to ``main``, so the version-level rule, the CI gate, the
since-tag merge detection and the single-line ``uv.lock`` edit are pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "release_plan.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-pr.yml"


def _load_module():
    spec = importlib.util.spec_from_file_location("release_plan", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_plan"] = module
    spec.loader.exec_module(module)
    return module


rp = _load_module()
ca = sys.modules["changelog_assemble"]


def _fragment(name: str, text: str = "- entry") -> Any:
    issue, kind, slug = ca.parse_fragment_name(name)
    return ca.Fragment(
        path=Path("changelog.d") / name, issue=issue, kind=kind, slug=slug, text=text
    )


def _commit(subject: str, body: str = "", sha: str = "a" * 40) -> Any:
    return rp.Commit(sha=sha, subject=subject, body=body)


# --- versions -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "level", "expected"),
    [
        ("0.22.0", "patch", "0.22.1"),
        ("0.22.3", "minor", "0.23.0"),
        ("0.22.3", "major", "1.0.0"),
        ("v1.2.3", "patch", "1.2.4"),
    ],
)
def test_bump_version(version: str, level: str, expected: str) -> None:
    assert rp.bump_version(version, level) == expected


@pytest.mark.parametrize("bad", ["0.22", "0.22.0rc1", "latest", ""])
def test_parse_version_rejects_non_xyz(bad: str) -> None:
    with pytest.raises(rp.ReleaseError):
        rp.parse_version(bad)


# --- version level --------------------------------------------------------------


def test_level_defaults_to_patch() -> None:
    decision = rp.decide_level(
        "0.22.0", [_fragment("5001.fixed.md")], [_commit("fix(router): x (#1)")]
    )
    assert decision.level == "patch"


@pytest.mark.parametrize("kind", ["added", "upgrade"])
def test_added_or_upgrade_fragment_is_minor(kind: str) -> None:
    decision = rp.decide_level("0.22.0", [_fragment(f"5001.{kind}.md")], [])
    assert decision.level == "minor"
    assert any(f"5001.{kind}.md" in r for r in decision.reasons)


@pytest.mark.parametrize("kind", ["fixed", "changed", "performance"])
def test_other_fragment_kinds_stay_patch(kind: str) -> None:
    assert rp.decide_level("0.22.0", [_fragment(f"5001.{kind}.md")], []).level == "patch"


def test_feat_commit_is_minor() -> None:
    decision = rp.decide_level("0.22.0", [], [_commit("feat(check): new rule (#9)")])
    assert decision.level == "minor"


def test_feat_pr_title_in_merge_commit_body_is_minor() -> None:
    merge = _commit("Merge pull request #9 from o/feature/issue-1", "feat: new thing\n\nbody")
    assert merge.is_feat
    assert rp.decide_level("0.22.0", [], [merge]).level == "minor"


def test_feat_word_elsewhere_is_not_feat() -> None:
    assert not _commit("fix: feat: flag parsing").is_feat
    assert not _commit("docs: describe feat: prefixes").is_feat


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("fix(api)!: drop the old flag", ""),
        ("refactor: rename", "details\n\nBREAKING CHANGE: `--old` removed"),
        ("Merge pull request #4 from o/b", "feat!: remove legacy engine"),
    ],
)
def test_breaking_is_minor_below_one(subject: str, body: str) -> None:
    commit = _commit(subject, body)
    assert commit.is_breaking
    assert rp.decide_level("0.22.0", [], [commit]).level == "minor"


def test_breaking_is_major_from_one() -> None:
    assert rp.decide_level("1.4.2", [], [_commit("fix!: change default")]).level == "major"


@pytest.mark.parametrize("override", ["patch", "minor", "major"])
def test_override_wins(override: str) -> None:
    decision = rp.decide_level(
        "0.22.0", [_fragment("1.added.md")], [_commit("feat!: x")], override=override
    )
    assert decision.level == override


def test_auto_override_applies_rule() -> None:
    assert rp.decide_level("0.22.0", [], [], override="auto").level == "patch"


def test_decide_level_accepts_a_generator() -> None:
    commits = (c for c in [_commit("feat: x"), _commit("fix!: y")])
    assert rp.decide_level("0.22.0", [], commits).level == "minor"


# --- since-tag merge detection -------------------------------------------------


def test_merged_prs_reads_merge_and_squash_subjects_and_skips_direct_pushes() -> None:
    commits = [
        _commit("Merge pull request #5773 from rjwalters/ci/drop-cpuset-pins"),
        _commit("chore: resync installed Loom surfaces"),
        _commit("fix(router): halo marking (#5668)"),
        _commit("Merge pull request #5773 from rjwalters/ci/drop-cpuset-pins"),
    ]
    assert rp.merged_prs(commits) == [5773, 5668]


def test_direct_pushes_alone_are_not_merges() -> None:
    assert rp.merged_prs([_commit("chore: resync installed Loom surfaces")]) == []


# --- CI gate --------------------------------------------------------------------


def _job(name: str, conclusion: str | None = "success", status: str = "completed", **kw: Any):
    return {"name": name, "status": status, "conclusion": conclusion, **kw}


def _green_jobs() -> list[dict[str, Any]]:
    jobs = [_job(n) for n in rp.RELEASE_JOBS if n != "Diff-Pair Routing Regression"]
    jobs.append(_job("Diff-Pair Routing Regression (boards/06-diffpair-test)"))
    jobs.append(_job("Detect Changes", "skipped"))
    return jobs


def test_all_green_passes_and_suffixed_names_match() -> None:
    assert rp.evaluate_jobs(_green_jobs()) == []


def test_skipped_counts_as_not_red_unconditionally() -> None:
    jobs = [j for j in _green_jobs() if j["name"] != "Match-Group Routing Regression"]
    jobs.append(_job("Match-Group Routing Regression", "skipped"))
    assert rp.evaluate_jobs(jobs) == []


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_red_conclusions_fail(conclusion: str) -> None:
    jobs = [j for j in _green_jobs() if j["name"] != "Test"] + [_job("Test", conclusion)]
    assert rp.evaluate_jobs(jobs) == [f"CI job `Test` concluded {conclusion}"]


def test_missing_job_fails() -> None:
    jobs = [j for j in _green_jobs() if j["name"] != "Type Check"]
    assert rp.evaluate_jobs(jobs) == ["CI job `Type Check` did not run"]


def test_running_job_fails() -> None:
    jobs = [j for j in _green_jobs() if j["name"] != "Test"]
    jobs.append(_job("Test", None, status="in_progress"))
    assert rp.evaluate_jobs(jobs) == ["CI job `Test` is in_progress"]


def test_unrelated_red_job_is_ignored_by_main_gate() -> None:
    jobs = _green_jobs() + [_job("Board 05 Routing Regression (blocking <= 7)", "failure")]
    assert rp.evaluate_jobs(jobs) == []


def test_rerun_keeps_latest_attempt() -> None:
    jobs = [j for j in _green_jobs() if j["name"] != "Test"]
    jobs += [_job("Test", "failure", run_attempt=1), _job("Test", "success", run_attempt=2)]
    assert rp.evaluate_jobs(jobs) == []


def test_newest_run_filters_event() -> None:
    runs = [
        {"id": 1, "event": "push", "created_at": "2026-09-28T10:00:00Z"},
        {"id": 2, "event": "pull_request", "created_at": "2026-09-28T11:00:00Z"},
        {"id": 3, "event": "push", "created_at": "2026-09-28T09:00:00Z"},
    ]
    assert rp.newest_run(runs, "push")["id"] == 1
    assert rp.newest_run([], "push") is None


def test_wait_for_polls_until_settled_and_bounds() -> None:
    states = iter([("pending", ["a"]), ("pending", ["b"]), ("green", [])])
    slept: list[float] = []
    assert rp.wait_for(lambda: next(states), 100, 5, sleep=slept.append) == ("green", [])
    assert slept == [5, 5]

    clock = iter([0, 0, 50, 101])
    result = rp.wait_for(
        lambda: ("pending", ["x"]), 100, 50, sleep=lambda _s: None, clock=lambda: next(clock)
    )
    assert result == ("pending", ["x"])


# --- file edits ---------------------------------------------------------------


def test_set_lock_version_changes_exactly_one_line_of_the_real_lockfile() -> None:
    before = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    after = rp.set_lock_version(before, "9.9.9")
    changed = [
        (a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b
    ]
    assert changed == [(changed[0][0], 'version = "9.9.9"')]
    assert changed[0][0].startswith('version = "')


def test_set_lock_version_requires_one_anchor() -> None:
    with pytest.raises(rp.ReleaseError, match="0 `name"):
        rp.set_lock_version('[[package]]\nname = "other"\nversion = "1"\n', "2.0.0")
    doubled = '[[package]]\nname = "kicad-tools"\nversion = "1"\n' * 2
    with pytest.raises(rp.ReleaseError, match="2 `name"):
        rp.set_lock_version(doubled, "2.0.0")
    with pytest.raises(rp.ReleaseError, match="no `version"):
        rp.set_lock_version('name = "kicad-tools"\nsource = { editable = "." }\n', "2.0.0")


def test_set_pyproject_version_touches_only_project_table() -> None:
    text = (
        '[build-system]\nversion = "keep"\n\n[project]\nname = "kicad-tools"\n'
        'version = "0.22.0"\n\n[tool.x]\nversion = "keep"\n'
    )
    out = rp.set_pyproject_version(text, "0.23.0")
    assert out.count('version = "keep"') == 2
    assert 'version = "0.23.0"' in out
    assert rp.pyproject_version(out) == "0.23.0"


def test_real_pyproject_round_trips() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    current = rp.pyproject_version(text)
    out = rp.set_pyproject_version(text, "9.9.9")
    assert rp.pyproject_version(out) == "9.9.9"
    assert rp.set_pyproject_version(out, current) == text


def test_work_log_entry_appends() -> None:
    out = rp.add_work_log_entry("# Work Log\n\n## old\n", "0.23.0", "2026-09-29", [5, 7])
    assert out.endswith(
        "## 2026-09-29 — v0.23.0 release cut\n\n- v0.23.0 release PR opened "
        "by the automated daily release workflow (`release-pr.yml`, #5776); "
        "PRs merged since the previous tag: #5, #7.\n"
    )


# --- mode -----------------------------------------------------------------------

MODE_CASES = [
    (None, False, "dry-run"),
    ("", False, "dry-run"),
    ("off", False, "off"),
    ("OFF", True, "off"),
    ("on", False, "on"),
    ("on", True, "dry-run"),
    (" On ", False, "on"),
    ("dry-run", False, "dry-run"),
    ("yes", False, "dry-run"),
]


@pytest.mark.parametrize(("variable", "dispatch_dry_run", "expected"), MODE_CASES)
def test_resolve_mode(variable: str | None, dispatch_dry_run: bool, expected: str) -> None:
    assert rp.resolve_mode(variable, dispatch_dry_run)[0] == expected


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("variable", "dispatch_dry_run", "expected"), MODE_CASES)
def test_workflow_mode_step_matches_resolve_mode(
    tmp_path: Path, variable: str | None, dispatch_dry_run: bool, expected: str
) -> None:
    """The YAML resolves the mode in shell (it gates the token step); keep it in
    lockstep with :func:`resolve_mode`."""
    step = next(s for s in _workflow()["jobs"]["release-pr"]["steps"] if s.get("id") == "mode")
    out, summary = tmp_path / "out", tmp_path / "summary"
    env = {
        "PATH": os.environ["PATH"],
        "AUTO_RELEASE": variable or "",
        "DISPATCH_DRY_RUN": "true" if dispatch_dry_run else "false",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    subprocess.run(["bash", "-e", "-c", step["run"]], env=env, check=True, capture_output=True)
    assert out.read_text().strip() == f"mode={expected}"


def test_workflow_shape() -> None:
    wf = _workflow()
    triggers = wf[True]  # PyYAML parses the bare `on:` key as True
    assert triggers["schedule"] == [{"cron": "0 14 * * *"}]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"level", "dry_run"}
    assert wf["permissions"]["actions"] == "read"
    assert "write" not in wf["permissions"].values()  # writes go through the App token
    assert wf["concurrency"]["cancel-in-progress"] is False
    steps = wf["jobs"]["release-pr"]["steps"]
    token = next(s for s in steps if s.get("id") == "app-token")
    assert token["uses"].startswith("actions/create-github-app-token@")
    assert len(token["uses"].split("@")[1].split()[0]) == 40  # pinned by SHA
    assert "LOOM_FLEET_DISPATCH_APP_ID" in token["with"]["app-id"]
    final = steps[-1]
    assert final["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "app-token" in final["env"]["RELEASE_TOKEN"]


# --- git fixtures -----------------------------------------------------------------

_CHANGELOG = """# Changelog

## [Unreleased]

## [0.1.0] - 2026-01-01

### Fixed

- first

[0.1.0]: https://github.com/o/r/releases/tag/v0.1.0
"""

_LOCK = """version = 1

[[package]]
name = "anyio"
version = "4.0.0"

[[package]]
name = "kicad-tools"
version = "0.1.0"
source = { editable = "." }
"""


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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@example.com")
    (work / "pyproject.toml").write_text('[project]\nname = "kicad-tools"\nversion = "0.1.0"\n')
    (work / "uv.lock").write_text(_LOCK)
    (work / "CHANGELOG.md").write_text(_CHANGELOG)
    (work / "WORK_LOG.md").write_text("# Work Log\n")
    (work / "changelog.d").mkdir()
    (work / "changelog.d" / "README.md").write_text("fragments\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "chore(release): v0.1.0")
    _git(work, "tag", "-a", "v0.1.0", "-m", "Release 0.1.0")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main", "--tags")
    return work


def _merge_pr(repo: Path, number: int, title: str, files: dict[str, str]) -> None:
    _git(repo, "checkout", "-q", "-b", f"pr-{number}")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", title)
    _git(repo, "checkout", "-q", "main")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        f"pr-{number}",
        "-m",
        f"Merge pull request #{number} from o/pr-{number}",
        "-m",
        title,
    )
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")


def test_plan_no_merges_is_a_quiet_noop(repo: Path) -> None:
    (repo / "note.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: resync installed Loom surfaces")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    plan = rp.make_plan(rp.Git(repo), "origin/main")
    assert plan.tag == "v0.1.0"
    assert plan.prs == []
    assert plan.stop == "no PRs merged since v0.1.0"
    assert plan.version is None


def test_plan_internal_only_merges_are_a_noop(repo: Path) -> None:
    _merge_pr(repo, 11, "ci: tune runners", {"ci.txt": "x"})
    plan = rp.make_plan(rp.Git(repo), "origin/main")
    assert plan.prs == [11]
    assert plan.stop is not None and "nothing user-visible" in plan.stop


def test_plan_fix_fragment_is_patch(repo: Path) -> None:
    _merge_pr(repo, 12, "fix(router): y", {"changelog.d/40.fixed.md": "- fixed y (Issue #40)."})
    plan = rp.make_plan(rp.Git(repo), "origin/main", date="2026-09-29")
    assert plan.stop is None
    assert (plan.decision.level, plan.version) == ("patch", "0.1.1")
    assert plan.section.startswith("## [0.1.1] - 2026-09-29\n\n### Fixed\n\n- fixed y")


def test_plan_feat_merge_is_minor(repo: Path) -> None:
    _merge_pr(repo, 13, "feat(check): z", {"changelog.d/41.fixed.md": "- z (Issue #41)."})
    plan = rp.make_plan(rp.Git(repo), "origin/main")
    assert (plan.decision.level, plan.version) == ("minor", "0.2.0")


def test_plan_reads_fragments_from_the_ref_not_the_working_tree(repo: Path) -> None:
    _merge_pr(repo, 12, "fix: y", {"changelog.d/40.fixed.md": "- y"})
    (repo / "changelog.d" / "99.added.md").write_text("- uncommitted")
    plan = rp.make_plan(rp.Git(repo), "origin/main")
    assert [f.path.name for f in plan.fragments] == ["40.fixed.md"]
    assert plan.decision.level == "patch"


def test_plan_stops_when_bump_merged_but_untagged(repo: Path) -> None:
    _merge_pr(
        repo,
        14,
        "chore(release): v0.1.1",
        {
            "pyproject.toml": '[project]\nname = "kicad-tools"\nversion = "0.1.1"\n',
            "changelog.d/40.fixed.md": "- y",
        },
    )
    plan = rp.make_plan(rp.Git(repo), "origin/main")
    assert plan.stop is not None and "waiting for its tag" in plan.stop


def test_build_release_tree(repo: Path) -> None:
    _merge_pr(
        repo,
        12,
        "feat: y",
        {"changelog.d/40.added.md": "- added y (Issue #40).", "changelog.d/41.fixed.md": "- z"},
    )
    plan = rp.make_plan(rp.Git(repo), "origin/main", date="2026-09-29")
    rp.build_release_tree(repo, plan.version, "2026-09-29", plan.prs, plan.fragments)
    assert sorted(p.name for p in (repo / "changelog.d").iterdir()) == ["README.md"]
    changelog = (repo / "CHANGELOG.md").read_text()
    assert "## [Unreleased]\n\n## [0.2.0] - 2026-09-29\n" in changelog
    assert "[0.2.0]: https://github.com/o/r/releases/tag/v0.2.0\n" in changelog
    assert rp.pyproject_version((repo / "pyproject.toml").read_text()) == "0.2.0"
    lock_diff = _git(repo, "diff", "-U0", "--", "uv.lock")
    assert [
        ln for ln in lock_diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")
    ] == [
        '-version = "0.1.0"',
        '+version = "0.2.0"',
    ]
    assert "v0.2.0 release cut" in (repo / "WORK_LOG.md").read_text()


# --- run() with a fake forge ------------------------------------------------------


class FakeForge(rp.Forge):
    def __init__(self, *, ci: str = "green", pr_checks: str = "green", open_prs=None) -> None:
        super().__init__("o/r", write_token="app-token")
        self.ci = ci
        self.pr_checks = pr_checks
        self.open_prs = open_prs or []
        self.calls: list[tuple] = []

    def runs_for_sha(self, sha: str, workflow: str | None = None) -> list[dict[str, Any]]:
        if workflow:  # main CI
            status = "in_progress" if self.ci == "pending" else "completed"
            return [{"id": 1, "event": "push", "status": status, "created_at": "t"}]
        status = "in_progress" if self.pr_checks == "pending" else "completed"
        return [
            {
                "id": 2,
                "event": "pull_request",
                "status": status,
                "path": ".github/workflows/ci.yml",
                "workflow_id": 7,
                "name": "CI",
                "created_at": "t",
            }
        ]

    def run_jobs(self, run_id: int) -> list[dict[str, Any]]:
        state = self.ci if run_id == 1 else self.pr_checks
        jobs = _green_jobs()
        if state == "red":
            jobs = [j for j in jobs if j["name"] != "Test"] + [_job("Test", "failure")]
        return jobs

    def open_release_prs(self) -> list[dict[str, Any]]:
        return self.open_prs

    def create_pr(self, head: str, title: str, body: str) -> dict[str, Any]:
        self.calls.append(("create_pr", head, title, body))
        return {"number": 100}

    def edit_pr(self, number: int, title: str, body: str) -> None:
        self.calls.append(("edit_pr", number, title))

    def close_pr(self, number: int, comment: str) -> None:
        self.calls.append(("close_pr", number))

    def delete_branch(self, branch: str) -> None:
        self.calls.append(("delete_branch", branch))

    def merge_pr(self, number: int, sha: str, title: str) -> None:
        self.calls.append(("merge_pr", number, sha, title))

    def report(self, body: str) -> None:
        self.calls.append(("report", body))


@pytest.fixture
def quiet_checks(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {"gap": [], "lock": []}
    monkeypatch.setattr(rp, "run_gap_report", lambda tag, sha: results["gap"])
    monkeypatch.setattr(rp, "run_uv_lock_check", lambda repo: results["lock"])
    return results


def _run(repo: Path, forge: FakeForge, mode: str = "on", **kw: Any):
    return rp.run(
        mode=mode,
        mode_note="test",
        git=rp.Git(repo),
        forge=forge,
        level=kw.pop("level", "auto"),
        wait_ci=kw.pop("wait_ci", 0),
        wait_pr=kw.pop("wait_pr", 0),
        interval=0,
        sleep=lambda _s: None,
        date="2026-09-29",
        **kw,
    )


def test_run_off_does_nothing(repo: Path, quiet_checks) -> None:
    forge = FakeForge()
    outcome = _run(repo, forge, mode="off")
    assert outcome.code == 0 and forge.calls == []


def test_run_no_merges_exits_quietly(repo: Path, quiet_checks) -> None:
    forge = FakeForge()
    outcome = _run(repo, forge)
    assert outcome.code == 0 and "no PRs merged" in outcome.summary
    assert forge.calls == []


def test_run_dry_run_reports_version_and_section_and_opens_nothing(
    repo: Path, quiet_checks
) -> None:
    _merge_pr(repo, 12, "feat: y", {"changelog.d/40.added.md": "- added y (Issue #40)."})
    head = _git(repo, "rev-parse", "HEAD")
    forge = FakeForge()
    outcome = _run(repo, forge, mode="dry-run")
    assert outcome.code == 0
    assert "level: minor -> v0.2.0" in outcome.summary
    assert "## [0.2.0] - 2026-09-29" in outcome.summary
    assert "dry-run: would open/update PR `release/v0.2.0`" in outcome.summary
    assert forge.calls == []
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / "changelog.d" / "40.added.md").exists()


@pytest.mark.parametrize(
    ("ci", "gap", "lock", "needle"),
    [
        ("red", [], [], "CI job `Test` concluded failure"),
        ("pending", [], [], "CI not finished after 0s"),
        ("green", ["changelog gap report: #9"], [], "changelog gap report: #9"),
        ("green", [], ["`uv lock --check` failed"], "uv lock --check"),
    ],
)
def test_run_red_gate_never_opens_a_pr_and_reports(
    repo: Path, quiet_checks, ci: str, gap: list[str], lock: list[str], needle: str
) -> None:
    _merge_pr(repo, 12, "fix: y", {"changelog.d/40.fixed.md": "- y"})
    quiet_checks["gap"], quiet_checks["lock"] = gap, lock
    forge = FakeForge(ci=ci)
    outcome = _run(repo, forge)
    assert outcome.code == 1
    assert [c[0] for c in forge.calls] == ["report"]
    assert needle in forge.calls[0][1]


def test_run_dry_run_red_gate_reports_without_commenting(repo: Path, quiet_checks) -> None:
    _merge_pr(repo, 12, "fix: y", {"changelog.d/40.fixed.md": "- y"})
    forge = FakeForge(ci="red")
    outcome = _run(repo, forge, mode="dry-run")
    assert outcome.code == 0 and "GATE RED" in outcome.summary
    assert forge.calls == []


def test_run_on_opens_pushes_waits_and_merges(repo: Path, quiet_checks) -> None:
    _merge_pr(repo, 12, "fix: y", {"changelog.d/40.fixed.md": "- y (Issue #40)."})
    forge = FakeForge()
    outcome = _run(repo, forge)
    assert outcome.code == 0, outcome.summary
    kinds = [c[0] for c in forge.calls]
    assert kinds == ["create_pr", "merge_pr"]
    _, head, title, body = forge.calls[0]
    assert (head, title) == ("release/v0.1.1", "chore(release): v0.1.1")
    assert rp.AUTO_MARKER in body and "#12" in body
    remote_head = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/release/v0.1.1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert forge.calls[1] == ("merge_pr", 100, remote_head, "chore(release): v0.1.1 (#100)")
    files = _git(repo, "show", "--name-status", "--format=", remote_head)
    assert "D\tchangelog.d/40.fixed.md" in files
    assert {"M\tCHANGELOG.md", "M\tpyproject.toml", "M\tuv.lock", "M\tWORK_LOG.md"} <= set(
        files.splitlines()
    )


def test_run_on_red_pr_checks_leaves_pr_open(repo: Path, quiet_checks) -> None:
    _merge_pr(repo, 12, "fix: y", {"changelog.d/40.fixed.md": "- y"})
    forge = FakeForge(pr_checks="red")
    outcome = _run(repo, forge)
    assert outcome.code == 1
    assert [c[0] for c in forge.calls] == ["create_pr", "report"]
    assert "left open: checks red" in forge.calls[1][1]


def test_run_on_updates_existing_auto_pr_and_supersedes_stale_version(
    repo: Path, quiet_checks
) -> None:
    _merge_pr(repo, 12, "feat: y", {"changelog.d/40.fixed.md": "- y"})
    forge = FakeForge(
        open_prs=[
            {"number": 90, "head": {"ref": "release/v0.1.1"}, "body": rp.AUTO_MARKER},
        ]
    )
    outcome = _run(repo, forge)
    assert outcome.code == 0, outcome.summary
    kinds = [c[0] for c in forge.calls]
    assert kinds == ["create_pr", "close_pr", "delete_branch", "merge_pr"]
    assert forge.calls[1] == ("close_pr", 90)
    assert forge.calls[2] == ("delete_branch", "release/v0.1.1")


def test_run_on_same_version_edits_in_place(repo: Path, quiet_checks) -> None:
    _merge_pr(repo, 12, "fix: y", {"changelog.d/40.fixed.md": "- y"})
    forge = FakeForge(
        open_prs=[{"number": 90, "head": {"ref": "release/v0.1.1"}, "body": rp.AUTO_MARKER}]
    )
    _run(repo, forge)
    assert [c[0] for c in forge.calls] == ["edit_pr", "merge_pr"]
    assert forge.calls[1][1] == 90


def test_run_on_hand_opened_release_pr_pauses(repo: Path, quiet_checks) -> None:
    _merge_pr(repo, 12, "fix: y", {"changelog.d/40.fixed.md": "- y"})
    forge = FakeForge(
        open_prs=[{"number": 91, "head": {"ref": "release/v0.1.1"}, "body": "Release 0.1.1"}]
    )
    outcome = _run(repo, forge)
    assert outcome.code == 1
    assert [c[0] for c in forge.calls] == ["report"]
    assert "hand-opened release PR" in forge.calls[0][1]


# --- PR checks & forge plumbing ---------------------------------------------------


def test_pr_checks_pending_until_ci_run_exists() -> None:
    class NoRuns(FakeForge):
        def runs_for_sha(self, sha, workflow=None):
            return []

    state, reasons = rp.pr_checks_state(NoRuns(), "f" * 40)
    assert state == "pending" and "no CI pull_request run" in reasons[0]


def test_pr_checks_red_on_any_red_job_in_any_workflow() -> None:
    class Extra(FakeForge):
        def runs_for_sha(self, sha, workflow=None):
            runs = super().runs_for_sha(sha, workflow)
            runs.append(
                {
                    "id": 3,
                    "event": "pull_request",
                    "status": "completed",
                    "path": ".github/workflows/changelog.yml",
                    "workflow_id": 8,
                    "name": "Changelog",
                    "created_at": "t",
                }
            )
            return runs

        def run_jobs(self, run_id):
            if run_id == 3:
                return [_job("Changelog Fragment Check", "failure")]
            return super().run_jobs(run_id)

    state, reasons = rp.pr_checks_state(Extra(), "f" * 40)
    assert state == "red"
    assert reasons == ["`Changelog` job `Changelog Fragment Check` concluded failure"]
    assert rp.pr_checks_state(FakeForge(), "f" * 40) == ("green", [])


def test_forge_writes_use_release_token_and_reads_do_not() -> None:
    seen: list[dict[str, Any]] = []

    def runner(cmd, *, input, capture_output, text, check, env):
        seen.append({"cmd": cmd, "input": input, "token": env.get("GH_TOKEN")})
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"number": 5}), stderr="")

    forge = rp.Forge("o/r", write_token="app", runner=runner)
    os.environ.pop("GH_TOKEN", None)
    forge.pull(5)
    forge.create_pr("release/v1.0.0", "t", "b")
    assert seen[0]["cmd"][-1] == "repos/o/r/pulls/5" and seen[0]["token"] is None
    assert seen[1]["token"] == "app" and json.loads(seen[1]["input"])["base"] == "main"

    with pytest.raises(rp.ReleaseError, match="no RELEASE_TOKEN"):
        rp.Forge("o/r", runner=runner).comment(1, "x")


def test_tracking_issue_find_or_create() -> None:
    created: list[dict[str, Any]] = []

    class F(rp.Forge):
        def __init__(self, items):
            super().__init__("o/r", write_token="app")
            self.items = items

        def api(self, path, *, method="GET", body=None, write=False):
            if path.startswith("search/issues"):
                return {"items": self.items}
            created.append(body)
            return {"number": 77}

    assert (
        F(
            [
                {"number": 5, "title": "Automated release status (old)"},
                {"number": 6, "title": "Automated release status"},
            ]
        ).tracking_issue()
        == 6
    )
    assert F([]).tracking_issue() == 77
    assert created[0]["title"] == "Automated release status"


def test_cli_plan_json_against_this_repo() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "plan", "--ref", "HEAD", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 2 and "no v* tag" in proc.stderr:
        pytest.skip("shallow clone without tags")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert set(data) >= {"tag", "merged_prs", "level", "version", "stop", "section"}
