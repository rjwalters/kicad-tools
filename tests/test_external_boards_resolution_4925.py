"""Unit tests for the worktree-depth-safe ``boards/external/`` resolver (#4925).

``boards/external/softstart`` (and any similarly-built local hardware-fixture
symlink) is a *relative* symlink calibrated for the primary checkout's own
location on disk. Loom's ``.loom/worktrees/issue-N/`` convention nests a
worktree root *inside* the primary checkout, so the same relative symlink
resolves to a path that does not exist from a worktree -- silently mistaken,
in practice, for "the fixture genuinely isn't provisioned on this host" (see
PRs #4908, #4911 on issue #4507).

These tests exercise ``tests.conftest.resolve_external_boards_dir`` against a
scratch git repository (with a real linked worktree, mirroring Loom's own
nesting) rather than the real ``boards/external/softstart`` fixture, which may
or may not be checked out on the host running this suite -- the fix under
test is the *resolution logic*, independent of whether any given host has the
sibling fixture repo checked out.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import EXTERNAL_BOARDS_ENV_VAR, resolve_external_boards_dir

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available on PATH")


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def scratch_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a scratch git repo with a linked worktree nested two levels in.

    Mirrors Loom's own ``<primary>/.loom/worktrees/issue-N`` layout: the
    worktree sits two directories *inside* the primary checkout, exactly the
    layout that broke the fixed-depth ``../../../`` symlink hop count.

    Returns ``(primary_root, worktree_root)``.
    """
    primary = tmp_path / "kicad-tools"
    primary.mkdir()
    _run_git(["init", "-q", "-b", "main"], cwd=primary)
    _run_git(["config", "user.email", "test@example.com"], cwd=primary)
    _run_git(["config", "user.name", "Test"], cwd=primary)
    (primary / "README.md").write_text("scratch repo for #4925 resolution test\n")
    _run_git(["add", "README.md"], cwd=primary)
    _run_git(["commit", "-q", "-m", "init"], cwd=primary)

    # The fixture-holding directory lives only in the primary checkout, just
    # like the real boards/external/softstart symlink is only ever checked
    # in / present relative to the primary checkout's own location.
    (primary / "boards" / "external").mkdir(parents=True)

    worktree_root = primary / ".loom" / "worktrees" / "issue-4925"
    worktree_root.parent.mkdir(parents=True)
    _run_git(
        ["worktree", "add", "-b", "feature/issue-4925", str(worktree_root), "main"],
        cwd=primary,
    )

    return primary, worktree_root


class TestResolveExternalBoardsDir:
    def test_env_override_takes_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        override = tmp_path / "somewhere-else"
        monkeypatch.setenv(EXTERNAL_BOARDS_ENV_VAR, str(override))

        result = resolve_external_boards_dir(start_dir=tmp_path)

        assert result == override

    def test_resolves_correctly_from_the_primary_checkout(
        self, scratch_repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv(EXTERNAL_BOARDS_ENV_VAR, raising=False)
        primary, _worktree = scratch_repo_with_worktree

        result = resolve_external_boards_dir(start_dir=primary)

        assert result == primary / "boards" / "external"
        assert result.is_dir()

    def test_resolves_correctly_from_a_nested_worktree(
        self, scratch_repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ):
        """The regression this issue fixes: a worktree two levels deep must
        still land on the *primary* checkout's boards/external/, not on a
        nonexistent path derived from the worktree's own nesting depth."""
        monkeypatch.delenv(EXTERNAL_BOARDS_ENV_VAR, raising=False)
        primary, worktree = scratch_repo_with_worktree

        result = resolve_external_boards_dir(start_dir=worktree)

        assert result == primary / "boards" / "external"
        assert result.is_dir()
        # And it must NOT be the (nonexistent) path a fixed-depth `../../../`
        # symlink resolved from inside the worktree would have produced.
        broken_guess = worktree / "boards" / "external"
        assert result != broken_guess

    def test_falls_back_to_start_dir_when_git_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv(EXTERNAL_BOARDS_ENV_VAR, raising=False)
        non_git_dir = tmp_path / "not-a-git-repo"
        non_git_dir.mkdir()

        result = resolve_external_boards_dir(start_dir=non_git_dir)

        assert result == non_git_dir / "boards" / "external"

    def test_defaults_start_dir_to_repo_root(self, monkeypatch: pytest.MonkeyPatch):
        """With no explicit start_dir, resolution runs from this repo's own
        REPO_ROOT -- exercised end to end against the real kicad-tools repo
        (whatever checkout/worktree is actually running this test)."""
        monkeypatch.delenv(EXTERNAL_BOARDS_ENV_VAR, raising=False)

        result = resolve_external_boards_dir()

        assert result.name == "external"
        assert result.parent.name == "boards"
