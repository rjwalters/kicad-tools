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
