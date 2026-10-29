"""Tests for the CHANGELOG reconciliation gate (issue #4638).

``scripts/changelog_gap_report.py`` backs `RELEASING.md` step (0): it walks
``git log <tag>..HEAD``, resolves each commit to the **issue** number it
addresses, classifies user-visible vs. internal, and reports the user-visible
issues the ``[Unreleased]`` section does not cite.

These tests exercise the pure parsing/classification helpers against synthetic
inputs -- no git range, no network, no ``gh`` calls. The three resolution tiers
and the changelog-section slice are the parts that can silently mis-report a
gap, which is why they are pinned here rather than left to a manual run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "changelog_gap_report.py"


def _load_helper_module():
    """Import ``scripts/changelog_gap_report.py`` as a module."""
    spec = importlib.util.spec_from_file_location("changelog_gap_report", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["changelog_gap_report"] = module
    spec.loader.exec_module(module)
    return module


gap_report = _load_helper_module()


class _StubResolver:
    """Tier-2 stand-in: a fixed PR -> issue map, with a call counter."""

    def __init__(self, mapping: dict[int, int] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[int] = []

    def issue_for_pr(self, pr_number: int) -> int | None:
        self.calls.append(pr_number)
        return self.mapping.get(pr_number)


def _commit(subject: str, body: str = "") -> object:
    return gap_report.Commit(sha="0123456789abcdef", subject=subject, body=body)


# --- tier 1: closing keywords ----------------------------------------------


def test_tier1_closing_keyword_resolves_the_issue() -> None:
    commit = _commit("fix(router): x (#4440)", "Some prose.\n\nCloses #4413\n")
    issues, tier = gap_report.resolve_issue_numbers(commit, _StubResolver())
    assert (issues, tier) == ([4413], "closing")


def test_tier1_handles_multiple_closing_keywords_as_a_set() -> None:
    commit = _commit("feat: x (#1)", "Closes #100\nFixes #100\nResolves #101\n")
    issues, tier = gap_report.resolve_issue_numbers(commit, _StubResolver())
    assert (issues, tier) == ([100, 101], "closing")


def test_prose_mentioning_resolve_is_not_a_closing_reference() -> None:
    """Line-anchoring guards the real misattribution of commit 771caf16.

    Its body contains "* fix(route): resolve #4506 attach zones in
    sheet-absolute space", which an unanchored keyword regex reads as a closing
    reference to #4506 -- the wrong issue (the commit closes #4588).
    """
    body = (
        "Close the hole with a post-route audit.\n"
        "\n"
        "The gate would otherwise fire on every #4506-exempt rated connector,\n"
        "so we resolve #4506 attach zones in sheet-absolute space.\n"
    )
    commit = _commit("feat(route): gate every engine (#4603)", body)
    resolver = _StubResolver({4603: 4588})
    issues, tier = gap_report.resolve_issue_numbers(commit, resolver)
    assert (issues, tier) == ([4588], "branch")


# --- tier 2: PR branch name -------------------------------------------------


def test_tier2_uses_the_pr_branch_when_no_closing_keyword() -> None:
    commit = _commit("fix(export): key manifest files (#4608)", "No trailer here.\n")
    resolver = _StubResolver({4608: 4590})
    issues, tier = gap_report.resolve_issue_numbers(commit, resolver)
    assert (issues, tier) == ([4590], "branch")
    assert resolver.calls == [4608]


def test_tier2_is_not_consulted_when_a_closing_keyword_exists() -> None:
    commit = _commit("fix: x (#4608)", "Closes #4590\n")
    resolver = _StubResolver({4608: 9999})
    issues, _ = gap_report.resolve_issue_numbers(commit, resolver)
    assert issues == [4590]
    assert resolver.calls == []


# --- tier 3: partial reference, then nothing --------------------------------


def test_tier3_falls_back_to_part_of_when_nothing_better_exists() -> None:
    commit = _commit("Wire the feedback loop (#4556)", "Part of #3438\n")
    issues, tier = gap_report.resolve_issue_numbers(commit, _StubResolver())
    assert (issues, tier) == ([3438], "partial")


def test_a_commit_with_no_pr_and_no_issue_resolves_to_nothing() -> None:
    """Edge case: a direct-to-main chore commit must classify, not crash."""
    commit = _commit("chore: update loom installation to v0.16.0", "Refreshed surfaces.\n")
    issues, tier = gap_report.resolve_issue_numbers(commit, _StubResolver())
    assert (issues, tier) == ([], "none")


# --- classification ---------------------------------------------------------


def test_conventional_internal_prefixes_classify_internal() -> None:
    for subject in (
        "chore(loom): Install Loom 0.15.0 orchestration framework (#4495)",
        "docs: add READMEs for benchmarks (#4618)",
        "test: re-baseline board-07 match-group family delta (#4598)",
        "ci: gate board end-to-end jobs on plane-net completion (#4535)",
        "build(dev-env): pin local venv to Python 3.12 (#4452)",
        "refactor(boards): migrate remaining 5 recipes (#4458)",
    ):
        assert _commit(subject).is_internal, subject


def test_feat_fix_and_non_conventional_subjects_classify_user_visible() -> None:
    for subject in (
        "feat(check): general .kct_waivers.json waiver mechanism (#4445)",
        "fix(lvs): derive unnamed-net names per component (#4625)",
        "perf(router): speed up the A* loop (#1)",
        "route/complete Phase 3: via-in-pad as tier-gated last resort (#4503)",
        "board-05 Phase 3: Kelvin/current-sense topology model (#4499)",
        "drc: align copper sliver detection with KiCad (#4517)",
    ):
        assert not _commit(subject).is_internal, subject


def test_pr_number_comes_from_the_subject_suffix_only() -> None:
    assert _commit("feat(diffpair): census (#4580) (#4611)").pr_number == 4611
    assert _commit("chore: no pr here").pr_number is None


# --- changelog section slicing ---------------------------------------------


_CHANGELOG = """# Changelog

## [Unreleased]

### Added

- **A new thing** (#111).

### Fixed

- **Another thing** (#222).

## [0.19.0] - 2026-07-20

### Added

- **An old thing** (#333).

## [0.18.0] - 2026-07-20

- **Older still** (#444).
"""


def test_extract_section_stops_at_the_next_release_heading() -> None:
    section = gap_report.extract_section(_CHANGELOG, "Unreleased")
    assert "#111" in section and "#222" in section
    assert "#333" not in section and "#444" not in section


def test_an_issue_cited_only_in_an_older_section_is_not_documented() -> None:
    documented = gap_report.documented_issue_numbers(
        gap_report.extract_section(_CHANGELOG, "Unreleased")
    )
    assert documented == {111, 222}


def test_extract_section_returns_empty_for_a_missing_section() -> None:
    assert gap_report.extract_section(_CHANGELOG, "Nonexistent") == ""


# --- fragments count as documentation (issue #5775) --------------------------


def test_fragments_document_their_issue_and_body_citations(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# not a fragment\n")
    (tmp_path / "5001.fixed.md").write_text("- **A fix** (Issue #5001, see also #5002).\n")
    (tmp_path / "5003.upgrade.md").write_text("- **Heads up** (Issue #5003).\n")
    assert gap_report.fragment_documented_issues(tmp_path) == {5001, 5002, 5003}


def test_fragment_dir_none_or_missing_documents_nothing(tmp_path: Path) -> None:
    assert gap_report.fragment_documented_issues(None) == set()
    assert gap_report.fragment_documented_issues(tmp_path / "absent") == set()


def test_malformed_fragment_name_is_an_error(tmp_path: Path) -> None:
    import pytest

    (tmp_path / "5001.fix.md").write_text("- typo'd kind\n")
    with pytest.raises(gap_report.changelog_assemble.FragmentError, match="5001.fix.md"):
        gap_report.fragment_documented_issues(tmp_path)


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_build_report_counts_fragments_as_documented(tmp_path: Path, monkeypatch) -> None:
    """``--since <tag>`` with a fragment and no [Unreleased] bullet has no gap."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "chore: base")
    _git(tmp_path, "tag", "v0.0.1")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: a (#9)", "-m", "Closes #700")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feat: b (#10)", "-m", "Closes #701")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n- **B** (#701).\n")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    monkeypatch.setattr(gap_report, "REPO_ROOT", tmp_path)

    without = gap_report.build_report(
        "v0.0.1", "HEAD", changelog, "Unreleased", _StubResolver(), fragments
    )
    assert without.gaps == [700]

    (fragments / "700.fixed.md").write_text("- **A** (Issue #700).\n")
    with_fragment = gap_report.build_report(
        "v0.0.1", "HEAD", changelog, "Unreleased", _StubResolver(), fragments
    )
    assert with_fragment.ok and with_fragment.gaps == []


def test_build_report_any_resolved_issue_documents_commit(tmp_path: Path, monkeypatch) -> None:
    """Two ``Part of`` trailers with a fragment citing only one is not a gap (#8099)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "chore: base")
    _git(tmp_path, "tag", "v0.0.1")
    _git(
        tmp_path,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "feat: a (#9)",
        "-m",
        "Part of #7423\n\nPart of #5798",
    )
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: b (#10)", "-m", "Closes #800")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [Unreleased]\n")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "7423.added.md").write_text("- **A** (Issue #7423).\n")
    monkeypatch.setattr(gap_report, "REPO_ROOT", tmp_path)

    report = gap_report.build_report(
        "v0.0.1", "HEAD", changelog, "Unreleased", _StubResolver(), fragments
    )
    # #5798 is excused by its own commit's other citation; #800 (a different
    # commit) is not excused by #7423.
    assert report.gaps == [800]


# --- per-PR mode (issue #5775) ----------------------------------------------


def _pr(
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    author: str = "rjwalters",
    head_ref: str = "feature/issue-5800",
) -> object:
    return gap_report.PullRequest(
        number=1, title=title, body=body, labels=labels or [], author=author, head_ref=head_ref
    )


def test_user_visible_pr_without_fragment_fails() -> None:
    verdict = gap_report.evaluate_pr(_pr("fix(router): x", "Closes #5800\n"), set(), [])
    assert not verdict.ok
    assert verdict.issues == [5800]
    assert "changelog.d/5800.<kind>.md" in verdict.reason


def test_same_pr_with_a_fragment_passes() -> None:
    verdict = gap_report.evaluate_pr(
        _pr("fix(router): x", "Closes #5800\n"), {5800}, ["changelog.d/5800.fixed.md"]
    )
    assert verdict.ok and "#5800" in verdict.reason


def test_same_pr_with_skip_label_passes() -> None:
    verdict = gap_report.evaluate_pr(
        _pr("fix(router): x", "Closes #5800\n", labels=["changelog:skip"]), set(), []
    )
    assert verdict.ok and "changelog:skip" in verdict.reason


def test_fragment_citing_a_different_issue_does_not_count() -> None:
    verdict = gap_report.evaluate_pr(
        _pr("feat: x", "Closes #5800\n"), {1234}, ["changelog.d/1234.added.md"]
    )
    assert not verdict.ok
    assert "cite none of #5800" in verdict.reason


def test_issue_resolves_from_pr_body_when_branch_commits_carry_none() -> None:
    """Curator case (a): unsquashed commits lack ``Closes #N``; the PR body has it."""
    pr = _pr("fix: x", "Summary prose.\n\nCloses #5810\n", head_ref="some-branch")
    assert gap_report.resolve_pr_issues(pr) == ([5810], "closing")
    assert gap_report.evaluate_pr(pr, {5810}, []).ok


def test_issue_falls_back_to_branch_then_part_of() -> None:
    assert gap_report.resolve_pr_issues(_pr("fix: x", "", head_ref="feature/issue-42")) == (
        [42],
        "branch",
    )
    assert gap_report.resolve_pr_issues(_pr("fix: x", "Part of #7\n", head_ref="topic")) == (
        [7],
        "partial",
    )
    assert gap_report.resolve_pr_issues(_pr("fix: x", "", head_ref="topic")) == ([], "none")


def test_unattributed_user_visible_pr_passes_only_with_a_fragment() -> None:
    pr = _pr("fix: x", "", head_ref="topic")
    assert not gap_report.evaluate_pr(pr, set(), []).ok
    assert gap_report.evaluate_pr(pr, set(), ["changelog.d/99.fixed.md"]).ok


def test_internal_title_types_pass_without_a_fragment() -> None:
    for title in (
        "ci: add a job",
        "test(router): pin a case",
        "docs: fix a typo",
        "refactor: split module",
        "chore(deps): bump x",
    ):
        assert gap_report.evaluate_pr(_pr(title, "Closes #5800\n"), set(), []).ok, title


def test_loom_resync_passes_structurally() -> None:
    for title in (
        "chore: resync installed Loom surfaces",
        "Resync installed Loom surfaces",  # non-conventional variant
        "Install Loom 0.19.0",
    ):
        verdict = gap_report.evaluate_pr(_pr(title, head_ref="loom/resync"), set(), [])
        assert verdict.ok, title
    assert gap_report.Commit(
        sha="0" * 40, subject="Resync installed Loom surfaces", body=""
    ).is_internal


def test_dependabot_passes_structurally_even_with_a_non_conventional_title() -> None:
    """Curator case (b): classification is by author/branch, not by ledger."""
    for author, ref in (
        ("dependabot[bot]", "dependabot/uv/numpy-3.0"),
        ("app/dependabot", "dependabot/uv/numpy-3.0"),
        ("someone", "dependabot/github_actions/x"),
    ):
        verdict = gap_report.evaluate_pr(
            _pr("Bump numpy from 2.0 to 3.0", author=author, head_ref=ref), set(), []
        )
        assert verdict.ok and "construction" in verdict.reason, (author, ref)


def test_other_bots_are_not_internal() -> None:
    """Loom's fleet bot authors real feature PRs; only Dependabot is exempt."""
    verdict = gap_report.evaluate_pr(
        _pr("feat: x", "Closes #5800\n", author="loom-fleet-dispatch[bot]"), set(), []
    )
    assert not verdict.ok


def test_internal_issues_ledger_exempts_a_pr() -> None:
    issue = next(iter(gap_report.INTERNAL_ISSUES))
    verdict = gap_report.evaluate_pr(_pr("fix: x", f"Closes #{issue}\n"), set(), [])
    assert verdict.ok and "INTERNAL_ISSUES" in verdict.reason


def test_pull_request_from_event_payload() -> None:
    event = {
        "action": "labeled",
        "pull_request": {
            "number": 12,
            "title": "fix: y",
            "body": None,
            "labels": [{"name": "changelog:skip"}, {"name": "loom:pr"}],
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/uv/x"},
        },
    }
    pr = gap_report.PullRequest.from_event(event)
    assert (pr.number, pr.title, pr.body, pr.author, pr.head_ref) == (
        12,
        "fix: y",
        "",
        "dependabot[bot]",
        "dependabot/uv/x",
    )
    assert pr.labels == ["changelog:skip", "loom:pr"]


def test_pull_request_from_gh_json() -> None:
    pr = gap_report.PullRequest.from_gh(
        {
            "number": 3,
            "title": "feat: z",
            "body": "Closes #1",
            "labels": [{"name": "a"}],
            "author": {"login": "app/dependabot"},
            "headRefName": "dependabot/npm/x",
        }
    )
    assert (pr.author, pr.head_ref, pr.labels) == ("app/dependabot", "dependabot/npm/x", ["a"])


def test_cli_pr_event_mode_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    """A fragment-only diff on a user-visible PR passes; without it the PR fails."""
    import json

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "chore: base")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"pull_request": {"number": 5, "title": "fix: q", "body": "Closes #900", "labels": []}}
        )
    )
    monkeypatch.setattr(gap_report, "REPO_ROOT", tmp_path)
    args = [
        "--pr-event",
        str(event),
        "--base",
        "HEAD^1",
        "--changelog",
        str(tmp_path / "CHANGELOG.md"),
        "--fragments-dir",
        str(tmp_path / "changelog.d"),
    ]

    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: q")
    assert gap_report.main(args) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out

    _git(tmp_path, "reset", "-q", "--hard", "HEAD^1")
    (tmp_path / "changelog.d" / "900.fixed.md").write_text("- **Q** (Issue #900).\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fix: q")
    assert gap_report.main(args) == 0
    assert "RESULT: PASS" in capsys.readouterr().out


# --- per-PR mode: commit trailers (issue #8099) ------------------------------


def _resolved(subject: str, body: str, resolver: object | None = None) -> object:
    commit = _commit(subject, body)
    commit.issues, commit.tier = gap_report.resolve_issue_numbers(
        commit, resolver or _StubResolver()
    )
    return commit


#: The f409e6a8 shape: two ``Part of`` trailers on one user-visible commit.
_TWO_PART_OF = "Body.\n\nPart of #7423\nPart of #5798\n"


def test_pr_fails_when_a_commit_part_of_issue_is_uncited() -> None:
    """The f409e6a8 commit shape: an uncited ``Part of`` issue fails (#8099).

    This reproduces the commit shape, not the #5798 incident itself, which was
    the release-window race tracked in #8111.
    """
    commits = [_resolved("fix(router): hold meanders", _TWO_PART_OF)]
    verdict = gap_report.evaluate_pr(
        _pr("fix(router): hold meanders", "Closes #7423\n", head_ref="feature/issue-7423"),
        {7423},
        ["changelog.d/7423.added.md"],
        commits,
    )
    assert not verdict.ok
    assert "'Part of #5798'" in verdict.reason
    assert "add 'part of #5798' to changelog.d/7423.added.md" in verdict.reason
    assert "'Part of #7423'" not in verdict.reason  # the cited one is not reported


def test_pr_passes_once_the_fragment_cites_every_part_of_issue() -> None:
    commits = [_resolved("fix(router): hold meanders", _TWO_PART_OF)]
    verdict = gap_report.evaluate_pr(
        _pr("fix(router): hold meanders", "Closes #7423\n"),
        {7423, 5798},
        ["changelog.d/7423.added.md"],
        commits,
    )
    assert verdict.ok, verdict.reason


def test_uncited_part_of_without_a_pr_fragment_suggests_one() -> None:
    commits = [_resolved("feat: models sync", "Part of #7690.\n")]
    verdict = gap_report.evaluate_pr(
        _pr("feat: models sync", "Closes #7683\n"), {7683}, [], commits
    )
    assert not verdict.ok
    assert "add 'part of #7690' to changelog.d/7690.<kind>.md" in verdict.reason


def test_commit_trailers_skip_internal_title_prs() -> None:
    """An internal-title PR needs no fragment at all, trailers included."""
    commits = [_resolved("fix: x", _TWO_PART_OF)]
    verdict = gap_report.evaluate_pr(_pr("ci: x", "Closes #7423\n"), set(), [], commits)
    assert verdict.ok and "title" in verdict.reason


def test_commit_trailers_skip_internal_commits_and_internal_issues() -> None:
    internal_issue = next(iter(gap_report.INTERNAL_ISSUES))
    commits = [
        _resolved("test: pin a case", "Part of #5798\n"),
        _resolved("fix: y", f"Part of #{internal_issue}\n"),
    ]
    verdict = gap_report.evaluate_pr(
        _pr("fix: y", "Closes #7423\n"), {7423}, ["changelog.d/7423.fixed.md"], commits
    )
    assert verdict.ok, verdict.reason


def test_closing_trailer_wins_over_part_of_as_in_the_release_gate() -> None:
    """``Closes #A`` + ``Part of #B`` resolves to #A only, so #B needs no citation."""
    commits = [_resolved("fix: z", "Closes #7423\nPart of #5798\n")]
    verdict = gap_report.evaluate_pr(
        _pr("fix: z", "Closes #7423\n"), {7423}, ["changelog.d/7423.fixed.md"], commits
    )
    assert verdict.ok, verdict.reason


def test_pr_number_subject_suffix_resolves_to_its_branch_issue() -> None:
    """A ``(#PR)`` suffix is a PR number, resolved via the release gate's tier 2."""
    commits = [_resolved("fix: z (#8100)", "Part of #5798\n", _StubResolver({8100: 7423}))]
    assert commits[0].issues == [7423]
    verdict = gap_report.evaluate_pr(
        _pr("fix: z", "Closes #7423\n"), {7423}, ["changelog.d/7423.fixed.md"], commits
    )
    assert verdict.ok, verdict.reason


def test_cli_pr_event_mode_checks_the_pr_commits(tmp_path: Path, monkeypatch, capsys) -> None:
    """End to end: the PR's own commits (``base..HEAD``) are read and resolved."""
    import json

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "chore: base")
    _git(tmp_path, "tag", "base")
    fragment = tmp_path / "changelog.d" / "7423.added.md"
    fragment.write_text("- **A** (Issue #7423).\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fix(router): a", "-m", _TWO_PART_OF)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "number": 5,
                    "title": "fix(router): a",
                    "body": "Closes #7423",
                    "labels": [],
                    "head": {"ref": "feature/issue-7423"},
                }
            }
        )
    )
    monkeypatch.setattr(gap_report, "REPO_ROOT", tmp_path)
    args = [
        "--pr-event",
        str(event),
        "--base",
        "base",
        "--offline",
        "--changelog",
        str(tmp_path / "CHANGELOG.md"),
        "--fragments-dir",
        str(tmp_path / "changelog.d"),
    ]

    assert gap_report.main(args) == 1
    out = capsys.readouterr().out
    assert "RESULT: FAIL" in out
    assert "add 'part of #5798' to changelog.d/7423.added.md" in out

    fragment.write_text("- **A** (Issue #7423, part of #5798).\n")
    _git(tmp_path, "commit", "-q", "-am", "fix(router): cite the epic")
    assert gap_report.main(args) == 0
    assert "RESULT: PASS" in capsys.readouterr().out
