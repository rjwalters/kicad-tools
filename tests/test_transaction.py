"""Tests for the file-scoped snapshot/rollback transaction helper (issue #4541)."""

import hashlib
import os
import re
from pathlib import Path

import pytest

from kicad_tools.transaction import (
    BoardTransaction,
    TransactionRestoreError,
    board_transaction,
)

ORIGINAL = b'(kicad_pcb (version 20240108) (net 0 "") (net 1 "GND"))\n'


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def board(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_bytes(ORIGINAL)
    return f


def _sidecars(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.failed-*"))


# ── Rollback on exception ───────────────────────────────────────────────


class TestExceptionRollback:
    def test_truncating_mutation_restored_byte_identical(self, board: Path):
        original_sha = _sha256(board)
        with pytest.raises(ValueError, match="boom"):
            with board_transaction(board):
                board.write_bytes(b"(kicad")  # truncated mid-write
                raise ValueError("boom")
        assert board.read_bytes() == ORIGINAL
        assert _sha256(board) == original_sha

    def test_appending_mutation_restored_byte_identical(self, board: Path):
        with pytest.raises(RuntimeError):
            with board_transaction(board):
                board.write_bytes(ORIGINAL + b"(segment ...)\n")
                raise RuntimeError("mid-mutation failure")
        assert board.read_bytes() == ORIGINAL

    def test_exception_propagates_unchanged(self, board: Path):
        """The original exception is re-raised, not swallowed by rollback."""

        class CustomError(Exception):
            pass

        with pytest.raises(CustomError):
            with board_transaction(board):
                board.write_bytes(b"garbage")
                raise CustomError()

    def test_keyboard_interrupt_rolls_back(self, board: Path):
        """Ctrl-C mid-mutation must restore the file and propagate."""
        with pytest.raises(KeyboardInterrupt):
            with board_transaction(board):
                board.write_bytes(b"half-written")
                raise KeyboardInterrupt()
        assert board.read_bytes() == ORIGINAL


# ── Explicit rollback API ───────────────────────────────────────────────


class TestExplicitRollback:
    def test_explicit_rollback_restores(self, board: Path):
        """Commands that fail via exit codes call rollback() directly."""
        with board_transaction(board) as txn:
            board.write_bytes(b"mutated")
            txn.rollback()
            assert board.read_bytes() == ORIGINAL
        assert board.read_bytes() == ORIGINAL

    def test_rollback_is_idempotent(self, board: Path):
        with board_transaction(board) as txn:
            board.write_bytes(b"mutated")
            txn.rollback()
            txn.rollback()  # second call is a no-op
        assert board.read_bytes() == ORIGINAL
        # only one sidecar despite two rollback calls
        assert len(_sidecars(board.parent)) == 1

    def test_rollback_noop_when_unchanged(self, board: Path):
        """Unchanged files: no restore, no sidecar (cheap failure exits)."""
        with board_transaction(board) as txn:
            txn.rollback()
            assert txn.restored == []
            assert txn.sidecars == []
        assert _sidecars(board.parent) == []
        assert board.read_bytes() == ORIGINAL

    def test_report_lines_mention_restore_and_sidecar(self, board: Path):
        with board_transaction(board) as txn:
            board.write_bytes(b"mutated")
            txn.rollback()
        lines = txn.report_lines()
        assert any(str(board) in line and "rolled back" in line for line in lines)
        assert any("failed attempt preserved" in line for line in lines)


# ── Forensic sidecar ────────────────────────────────────────────────────


class TestForensicSidecar:
    def test_sidecar_preserves_failed_attempt(self, board: Path):
        failed_bytes = b"the failed mutation attempt"
        with pytest.raises(RuntimeError):
            with board_transaction(board):
                board.write_bytes(failed_bytes)
                raise RuntimeError()
        sidecars = _sidecars(board.parent)
        assert len(sidecars) == 1
        assert sidecars[0].read_bytes() == failed_bytes

    def test_sidecar_name_format(self, board: Path):
        """Sidecar is <file>.failed-<UTC-timestamp><suffix>."""
        with pytest.raises(RuntimeError):
            with board_transaction(board):
                board.write_bytes(b"garbage")
                raise RuntimeError()
        (sidecar,) = _sidecars(board.parent)
        assert re.fullmatch(
            r"board\.kicad_pcb\.failed-\d{8}T\d{6}Z(-\d+)?\.kicad_pcb",
            sidecar.name,
        ), sidecar.name

    def test_keep_failed_false_suppresses_sidecar(self, board: Path):
        with pytest.raises(RuntimeError):
            with board_transaction(board, keep_failed=False):
                board.write_bytes(b"garbage")
                raise RuntimeError()
        assert board.read_bytes() == ORIGINAL
        assert _sidecars(board.parent) == []

    def test_sidecar_names_do_not_clobber(self, board: Path):
        """Two rollbacks in the same second get distinct sidecar names."""
        for attempt in (b"first failure", b"second failure"):
            with pytest.raises(RuntimeError):
                with board_transaction(board):
                    board.write_bytes(attempt)
                    raise RuntimeError()
        sidecars = _sidecars(board.parent)
        assert len(sidecars) == 2
        assert {s.read_bytes() for s in sidecars} == {b"first failure", b"second failure"}


# ── Success path ────────────────────────────────────────────────────────


class TestSuccessPath:
    def test_success_leaves_no_litter(self, board: Path):
        """Clean exit: mutation kept, no temp files, no sidecars."""
        before_listing = set(board.parent.iterdir())
        mutated = ORIGINAL + b"(via ...)\n"
        with board_transaction(board):
            board.write_bytes(mutated)
        assert board.read_bytes() == mutated
        assert set(board.parent.iterdir()) == before_listing

    def test_no_tmp_residue_after_rollback(self, board: Path):
        with pytest.raises(RuntimeError):
            with board_transaction(board):
                board.write_bytes(b"garbage")
                raise RuntimeError()
        assert list(board.parent.glob("*.tmp")) == []


# ── Multi-path and missing-file handling ────────────────────────────────


class TestMultiPath:
    def test_multiple_paths_all_restored(self, tmp_path: Path):
        a = tmp_path / "a.kicad_pcb"
        b = tmp_path / "b.kicad_pcb"
        a.write_bytes(b"contents of a")
        b.write_bytes(b"contents of b")
        with pytest.raises(RuntimeError):
            with board_transaction(a, b):
                a.write_bytes(b"mutated a")
                b.write_bytes(b"mutated b")
                raise RuntimeError()
        assert a.read_bytes() == b"contents of a"
        assert b.read_bytes() == b"contents of b"

    def test_only_changed_paths_get_sidecars(self, tmp_path: Path):
        a = tmp_path / "a.kicad_pcb"
        b = tmp_path / "b.kicad_pcb"
        a.write_bytes(b"contents of a")
        b.write_bytes(b"contents of b")
        with pytest.raises(RuntimeError):
            with board_transaction(a, b):
                a.write_bytes(b"mutated a")  # b untouched
                raise RuntimeError()
        sidecars = _sidecars(tmp_path)
        assert len(sidecars) == 1
        assert sidecars[0].name.startswith("a.kicad_pcb.failed-")

    def test_missing_file_on_entry_is_removed_on_rollback(self, tmp_path: Path):
        """A path created inside the transaction is unlinked by rollback."""
        out = tmp_path / "new-output.kicad_pcb"
        assert not out.exists()
        with pytest.raises(RuntimeError):
            with board_transaction(out):
                out.write_bytes(b"partial output")
                raise RuntimeError()
        assert not out.exists()
        # the partial output is still preserved for forensics
        (sidecar,) = _sidecars(tmp_path)
        assert sidecar.read_bytes() == b"partial output"

    def test_requires_at_least_one_path(self):
        with pytest.raises(ValueError):
            BoardTransaction()


# ── enabled=False (opt-in wiring) ───────────────────────────────────────


class TestDisabled:
    def test_disabled_is_complete_noop(self, board: Path):
        """enabled=False: no snapshot, no rollback, mutation survives failure."""
        with pytest.raises(RuntimeError):
            with board_transaction(board, enabled=False):
                board.write_bytes(b"mutated without protection")
                raise RuntimeError()
        assert board.read_bytes() == b"mutated without protection"
        assert _sidecars(board.parent) == []

    def test_disabled_explicit_rollback_is_noop(self, board: Path):
        with board_transaction(board, enabled=False) as txn:
            board.write_bytes(b"mutated")
            txn.rollback()
        assert board.read_bytes() == b"mutated"


# ── Restore failure is loud ─────────────────────────────────────────────


class TestRestoreFailure:
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses directory permission checks",
    )
    def test_read_only_directory_raises_loudly(self, tmp_path: Path):
        """A rollback that cannot restore must raise, never silently pass."""
        subdir = tmp_path / "boards"
        subdir.mkdir()
        board = subdir / "board.kicad_pcb"
        board.write_bytes(ORIGINAL)
        try:
            with pytest.raises(TransactionRestoreError):
                with board_transaction(board) as txn:
                    board.write_bytes(b"mutated")
                    # Read-only dir: sidecar + atomic-restore tmp writes fail.
                    subdir.chmod(0o555)
                    txn.rollback()
        finally:
            subdir.chmod(0o755)
        # file is left in the failed state -- but the user was told loudly
        assert board.read_bytes() == b"mutated"

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses directory permission checks",
    )
    def test_restore_error_chains_onto_original_exception(self, tmp_path: Path):
        """When rollback fails during exception handling, both surface."""
        subdir = tmp_path / "boards"
        subdir.mkdir()
        board = subdir / "board.kicad_pcb"
        board.write_bytes(ORIGINAL)
        try:
            with pytest.raises(TransactionRestoreError) as excinfo:
                with board_transaction(board):
                    board.write_bytes(b"mutated")
                    subdir.chmod(0o555)
                    raise RuntimeError("original failure")
            assert isinstance(excinfo.value.__context__, RuntimeError)
        finally:
            subdir.chmod(0o755)


# ── Atomic restore mechanics ────────────────────────────────────────────


class TestAtomicRestore:
    def test_restore_goes_through_os_replace(self, board: Path, monkeypatch):
        """Restore must use the tmp-sibling + os.replace pattern."""
        replace_calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy_replace(src, dst, **kwargs):
            replace_calls.append((str(src), str(dst)))
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr("kicad_tools.transaction.os.replace", spy_replace)
        with pytest.raises(RuntimeError):
            with board_transaction(board):
                board.write_bytes(b"garbage")
                raise RuntimeError()
        assert board.read_bytes() == ORIGINAL
        assert any(dst == str(board) and ".txn-restore.tmp" in src for src, dst in replace_calls)

    def test_subprocess_style_mutation_is_covered(self, board: Path):
        """External-process mutations (fresh file bytes) are rolled back too.

        Simulates a subprocess (e.g. kicad-cli zone refill) by replacing the
        file wholesale rather than writing through a Python handle.
        """
        replacement = board.parent / "external-write.tmp"
        replacement.write_bytes(b"externally rewritten")
        with pytest.raises(RuntimeError):
            with board_transaction(board):
                os.replace(replacement, board)
                raise RuntimeError()
        assert board.read_bytes() == ORIGINAL
