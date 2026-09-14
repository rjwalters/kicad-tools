"""Tests for `scripts/ci/select_routed_pcbs.py` (issue #5349).

These tests build small, disposable git repositories to exercise the real
`git` provenance logic end-to-end rather than mocking subprocess calls --
the bug this module fixes (PR #4955: a stale event-base SHA silently
widening a three-dot diff to include unrelated main history) is a property
of actual git topology, not of the Python glue around it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "select_routed_pcbs.py"
CHECK_ROUTED_DRC_PATH = REPO_ROOT / "scripts" / "ci" / "check_routed_drc.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def srp():
    return _load_module(HELPER_SCRIPT_PATH, "select_routed_pcbs")


# ---------------------------------------------------------------------------
# Git repo test harness.
# ---------------------------------------------------------------------------


class GitRepo:
    """Minimal git repo builder for provenance tests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._run(["init", "-q", "-b", "main"])
        self._run(["config", "user.email", "test@example.com"])
        self._run(["config", "user.name", "Test"])

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        )

    def write(self, rel_path: str, content: str = "x\n") -> None:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, message: str) -> str:
        self._run(["add", "."])
        self._run(["commit", "-q", "-m", message, "--allow-empty"])
        return self.head()

    def head(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def checkout(self, ref: str, new_branch: str | None = None) -> None:
        if new_branch:
            self._run(["checkout", "-q", "-b", new_branch, ref])
        else:
            self._run(["checkout", "-q", ref])

    def merge(self, ref: str, message: str = "merge") -> str:
        self._run(["merge", "-q", "--no-ff", ref, "-m", message])
        return self.head()


@pytest.fixture()
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path)


# ---------------------------------------------------------------------------
# resolve_push_base
# ---------------------------------------------------------------------------


class TestResolvePushBase:
    def test_explicit_before_used_verbatim(self, srp, repo: GitRepo) -> None:
        c1 = repo.commit("c1")
        c2 = repo.commit("c2")
        base = srp.resolve_push_base(repo.root, c2, c1, "origin/main")
        assert base == c1

    def test_zero_before_falls_back_to_parent(self, srp, repo: GitRepo) -> None:
        repo.commit("c1")
        c2 = repo.commit("c2")
        base = srp.resolve_push_base(repo.root, c2, "0" * 40, "origin/main")
        assert base == f"{c2}^"
        # And that ref must actually resolve to the c1 commit.
        resolved = subprocess.run(
            ["git", "rev-parse", base], cwd=repo.root, capture_output=True, text=True, check=True
        ).stdout.strip()
        c1_sha = subprocess.run(
            ["git", "rev-parse", "HEAD^"], cwd=repo.root, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert resolved == c1_sha

    def test_missing_before_falls_back_to_parent(self, srp, repo: GitRepo) -> None:
        repo.commit("c1")
        c2 = repo.commit("c2")
        base = srp.resolve_push_base(repo.root, c2, None, "origin/main")
        assert base == f"{c2}^"

    def test_empty_string_before_falls_back_to_parent(self, srp, repo: GitRepo) -> None:
        repo.commit("c1")
        c2 = repo.commit("c2")
        base = srp.resolve_push_base(repo.root, c2, "", "origin/main")
        assert base == f"{c2}^"

    def test_root_commit_with_zero_before_raises(self, srp, repo: GitRepo) -> None:
        """A repo's very first commit has no parent AND (in this test) no
        fallback ref either -- must fail loudly, not silently."""
        c1 = repo.commit("only commit")
        with pytest.raises(srp.GitCommandError):
            srp.resolve_push_base(repo.root, c1, "0" * 40, "origin/nonexistent-ref")

    def test_root_commit_with_zero_before_uses_fallback_ref_if_resolvable(
        self, srp, repo: GitRepo
    ) -> None:
        c1 = repo.commit("only commit")
        repo._run(["branch", "some-fallback", c1])
        base = srp.resolve_push_base(repo.root, c1, "0" * 40, "some-fallback")
        assert base == "some-fallback"


# ---------------------------------------------------------------------------
# resolve_pr_base
# ---------------------------------------------------------------------------


class TestResolvePrBase:
    def test_two_parent_merge_uses_first_parent(self, srp, repo: GitRepo) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        head_sha = repo.commit("feature work")
        repo.checkout("main")
        # Simulate main advancing past base_sha before the merge is built.
        main_tip = repo.commit("main advances")
        repo.checkout("main")
        merge_sha = repo.merge("feature")

        resolved_base = srp.resolve_pr_base(repo.root, merge_sha, head_sha)
        assert resolved_base == main_tip

    def test_second_parent_mismatch_raises(self, srp, repo: GitRepo) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.commit("feature work")
        repo.checkout("main")
        merge_sha = repo.merge("feature")

        with pytest.raises(srp.UnsupportedTopologyError):
            srp.resolve_pr_base(repo.root, merge_sha, "0" * 40)

    def test_single_parent_head_raises_unsupported_topology(self, srp, repo: GitRepo) -> None:
        """A non-merge checkout (e.g. PR could not be auto-merged) must be
        an explicit failure, never a silent guess."""
        c1 = repo.commit("c1")
        c2 = repo.commit("c2")
        with pytest.raises(srp.UnsupportedTopologyError):
            srp.resolve_pr_base(repo.root, c2, c1)

    def test_unresolvable_head_raises_git_command_error(self, srp, repo: GitRepo) -> None:
        repo.commit("c1")
        with pytest.raises(srp.GitCommandError):
            srp.resolve_pr_base(repo.root, "0" * 40, "0" * 40)

    def test_pr_head_sha_none_skips_second_parent_check(self, srp, repo: GitRepo) -> None:
        """When the caller doesn't supply an expected head (defensive --
        should not normally happen in CI), the topology check on the second
        parent is skipped but the two-parent shape is still required."""
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.commit("feature work")
        repo.checkout("main")
        main_tip = repo.commit("main advances")
        merge_sha = repo.merge("feature")

        resolved_base = srp.resolve_pr_base(repo.root, merge_sha, None)
        assert resolved_base == main_tip


# ---------------------------------------------------------------------------
# run_git_diff: failure vs empty-match distinction
# ---------------------------------------------------------------------------


class TestRunGitDiff:
    def test_empty_diff_is_empty_list_not_error(self, srp, repo: GitRepo) -> None:
        c1 = repo.commit("c1")
        c2 = repo.commit("c2 (no file changes)")
        assert srp.run_git_diff(repo.root, c1, c2) == []

    def test_real_diff_lists_files(self, srp, repo: GitRepo) -> None:
        c1 = repo.commit("c1")
        repo.write("boards/04-x/output/x_routed.kicad_pcb")
        c2 = repo.commit("add routed pcb")
        files = srp.run_git_diff(repo.root, c1, c2)
        assert files == ["boards/04-x/output/x_routed.kicad_pcb"]

    def test_invalid_revision_raises_git_command_error(self, srp, repo: GitRepo) -> None:
        c1 = repo.commit("c1")
        with pytest.raises(srp.GitCommandError):
            srp.run_git_diff(repo.root, "not-a-real-revision", c1)

    def test_git_failure_is_distinguishable_from_empty_match(self, srp, repo: GitRepo) -> None:
        """The load-bearing regression test for the `|| true` bug: a failed
        git invocation must raise, never silently return `[]` the same way
        a genuine no-match does."""
        c1 = repo.commit("c1")
        c2 = repo.commit("c2")
        empty_result = srp.run_git_diff(repo.root, c1, c2)
        assert empty_result == []
        with pytest.raises(srp.GitCommandError):
            srp.run_git_diff(repo.root, "totally-bogus-ref", c2)


# ---------------------------------------------------------------------------
# select_routed_pcbs: end-to-end selection semantics.
# ---------------------------------------------------------------------------


class TestSelectRoutedPcbs:
    def test_compiler_only_pr_selects_nothing_even_with_main_drift(
        self, srp, repo: GitRepo
    ) -> None:
        """Regression test for PR #4955: main advances with an unrelated
        routed-board change between the PR's original base and the actual
        merge-checkout base; a compiler-only PR must select NO boards."""
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.write("src/kicad_tools/compiler.py")
        head_sha = repo.commit("compiler-only change")
        repo.checkout("main")
        repo.write("boards/05-y/output/y_routed.kicad_pcb")
        repo.commit("main advances: unrelated board 05 change")
        merge_sha = repo.merge("feature")

        result = srp.select_routed_pcbs(repo.root, "pull_request", merge_sha, pr_head_sha=head_sha)
        assert result.all_files == []
        assert result.native_files == []
        assert result.ordinary_files == []

    def test_feature_side_board_change_is_selected(self, srp, repo: GitRepo) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.write("boards/05-y/output/y_routed.kicad_pcb")
        head_sha = repo.commit("board 05 change")
        repo.checkout("main")
        repo.merge("feature")
        merge_sha = repo.head()

        result = srp.select_routed_pcbs(repo.root, "pull_request", merge_sha, pr_head_sha=head_sha)
        assert result.all_files == ["boards/05-y/output/y_routed.kicad_pcb"]
        assert result.ordinary_files == ["boards/05-y/output/y_routed.kicad_pcb"]
        assert result.native_files == []

    def test_board04_change_is_selected_as_native(self, srp, repo: GitRepo) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.write(srp.NATIVE_BOARD_RELATIVE_PATH)
        head_sha = repo.commit("board 04 change")
        repo.checkout("main")
        repo.merge("feature")
        merge_sha = repo.head()

        result = srp.select_routed_pcbs(repo.root, "pull_request", merge_sha, pr_head_sha=head_sha)
        assert result.native_files == [srp.NATIVE_BOARD_RELATIVE_PATH]
        assert result.ordinary_files == []
        assert result.all_files == [srp.NATIVE_BOARD_RELATIVE_PATH]

    def test_infra_change_forces_native_board04_selection(self, srp, repo: GitRepo) -> None:
        """A PR touching this job's own selection machinery must exercise a
        real Board04 native run, even if Board04 itself is unmodified."""
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.write("scripts/ci/select_routed_pcbs.py")
        head_sha = repo.commit("touch the selection helper")
        repo.checkout("main")
        repo.merge("feature")
        merge_sha = repo.head()

        result = srp.select_routed_pcbs(repo.root, "pull_request", merge_sha, pr_head_sha=head_sha)
        assert result.infra_triggered is True
        assert result.native_files == [srp.NATIVE_BOARD_RELATIVE_PATH]

    def test_unrelated_pr_selects_no_files_and_no_native(self, srp, repo: GitRepo) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.write("README.md")
        head_sha = repo.commit("docs only")
        repo.checkout("main")
        repo.merge("feature")
        merge_sha = repo.head()

        result = srp.select_routed_pcbs(repo.root, "pull_request", merge_sha, pr_head_sha=head_sha)
        assert result.all_files == []
        assert result.native_files == []
        assert result.infra_triggered is False

    def test_push_event_uses_before_current_pair(self, srp, repo: GitRepo) -> None:
        c1 = repo.commit("c1")
        repo.write("boards/03-x/output/x_routed.kicad_pcb")
        c2 = repo.commit("c2: board 03 change")
        result = srp.select_routed_pcbs(repo.root, "push", c2, push_before=c1)
        assert result.ordinary_files == ["boards/03-x/output/x_routed.kicad_pcb"]

    def test_push_event_zero_before_still_selects_pushed_commit_diff(
        self, srp, repo: GitRepo
    ) -> None:
        repo.commit("c1")
        repo.write("boards/03-x/output/x_routed.kicad_pcb")
        c2 = repo.commit("c2: board 03 change")
        result = srp.select_routed_pcbs(repo.root, "push", c2, push_before="0" * 40)
        assert result.ordinary_files == ["boards/03-x/output/x_routed.kicad_pcb"]

    def test_unresolvable_topology_propagates_as_selection_error(self, srp, repo: GitRepo) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.commit("feature work")
        repo.checkout("main")
        merge_sha = repo.merge("feature")

        with pytest.raises(srp.SelectionError):
            srp.select_routed_pcbs(repo.root, "pull_request", merge_sha, pr_head_sha="0" * 40)


# ---------------------------------------------------------------------------
# CLI (main()) behavior.
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_selection_failure_exits_nonzero_with_error_annotation(
        self, srp, repo: GitRepo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.commit("feature work")
        repo.checkout("main")
        merge_sha = repo.merge("feature")

        rc = srp.main(
            [
                "--event-name",
                "pull_request",
                "--sha",
                merge_sha,
                "--pr-head-sha",
                "0" * 40,
                "--repo-root",
                str(repo.root),
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "::error::" in captured.out

    def test_success_writes_github_output_format(self, srp, repo: GitRepo, tmp_path: Path) -> None:
        base_sha = repo.commit("base")
        repo.checkout(base_sha, new_branch="feature")
        repo.write("boards/05-y/output/y_routed.kicad_pcb")
        head_sha = repo.commit("board 05 change")
        repo.checkout("main")
        repo.merge("feature")
        merge_sha = repo.head()

        output_path = tmp_path / "github_output"
        rc = srp.main(
            [
                "--event-name",
                "pull_request",
                "--sha",
                merge_sha,
                "--pr-head-sha",
                head_sha,
                "--repo-root",
                str(repo.root),
                "--github-output",
                str(output_path),
            ]
        )
        assert rc == 0
        content = output_path.read_text()
        assert "files<<SELECT_ROUTED_PCBS_EOF" in content
        assert "boards/05-y/output/y_routed.kicad_pcb" in content
        assert "native_files<<SELECT_ROUTED_PCBS_EOF" in content
        assert "ordinary_files<<SELECT_ROUTED_PCBS_EOF" in content

    def test_no_op_on_empty_selection_exits_zero(self, srp, repo: GitRepo) -> None:
        c1 = repo.commit("c1")
        c2 = repo.commit("c2")
        rc = srp.main(
            [
                "--event-name",
                "push",
                "--sha",
                c2,
                "--push-before",
                c1,
                "--repo-root",
                str(repo.root),
            ]
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Cross-module consistency: the native board path must stay in sync with
# check_routed_drc.py's canonical dispatch constant.
# ---------------------------------------------------------------------------


class TestNativeBoardPathMatchesCheckRoutedDrc:
    def test_native_board_relative_path_matches_paid_drill_board(self, srp) -> None:
        check_routed_drc = _load_module(CHECK_ROUTED_DRC_PATH, "check_routed_drc_xref")
        expected = str(check_routed_drc.PAID_DRILL_BOARD.relative_to(check_routed_drc.REPO_ROOT))
        assert expected == srp.NATIVE_BOARD_RELATIVE_PATH
