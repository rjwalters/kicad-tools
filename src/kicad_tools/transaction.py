"""File-scoped snapshot/rollback transactions for in-place board mutations.

``kct`` commands that mutate a ``.kicad_pcb`` / ``.kicad_sch`` **in place**
historically had no uniform failure-safety: a crash or partial failure
mid-mutation could leave the user's only copy of a board corrupted or
half-written.  :class:`BoardTransaction` (usually constructed via the
:func:`board_transaction` factory) closes that gap with a *file-scoped*
snapshot/rollback context manager (issue #4541).

Guarantees
----------

* **Byte-identical restore** — on an exception escaping the ``with`` block,
  or on an explicit :meth:`BoardTransaction.rollback` call, every wrapped
  path is restored to the exact bytes it held on entry.  Paths that did not
  exist on entry are removed again on rollback.
* **Atomic restore** — the restore write goes through a sibling temp file +
  ``os.fsync`` + ``os.replace`` (same pattern as the router's atomic save),
  so a crash *mid-restore* cannot leave a half-written board either.
* **Forensic sidecar** — the failed attempt is preserved next to the input
  as ``<file>.failed-<UTC-timestamp><suffix>`` before restoring, so a
  failed mutation can be inspected after the fact.  Pass
  ``keep_failed=False`` to suppress the sidecar.
* **No litter on success** — when the ``with`` block exits cleanly without
  a rollback, no temp files or sidecars remain on disk.
* **Ctrl-C coverage** — ``KeyboardInterrupt`` (and any other
  ``BaseException``) propagates through the ``with`` block, so an interrupt
  mid-mutation triggers the same rollback before re-raising.
* **Loud restore failures** — if the rollback itself cannot restore a file
  (e.g. read-only directory), :class:`TransactionRestoreError` is raised;
  a failed restore is never silently swallowed.

Because most ``kct`` commands report failure via non-zero exit codes rather
than exceptions, adopting commands should also call
:meth:`BoardTransaction.rollback` explicitly on their failure exit paths.
Rollback is cheap and idempotent: paths whose current bytes already match
the entry snapshot are skipped entirely (no restore, no sidecar), so it is
safe to call on exit paths where no mutation actually happened.

Snapshots are held in memory (boards are single-digit MB).  Because the
snapshot/restore operates on the *file*, mutations performed by
subprocesses (e.g. ``kicad-cli`` zone refills) are covered too.

Example::

    from kicad_tools.transaction import board_transaction

    with board_transaction(pcb_path, enabled=args.transactional) as txn:
        exit_code = do_mutation(pcb_path)
        if exit_code != 0:
            txn.rollback()
        return exit_code
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Literal

__all__ = [
    "BoardTransaction",
    "TransactionRestoreError",
    "board_transaction",
]


class TransactionRestoreError(RuntimeError):
    """Raised when a transactional rollback could not fully restore a file.

    This is deliberately loud: if the rollback cannot put the original
    bytes back (or write the forensic sidecar), the user must know their
    file may be in the failed post-mutation state.
    """


class BoardTransaction:
    """File-scoped snapshot/rollback transaction (see module docstring).

    Prefer the :func:`board_transaction` factory for construction.

    Attributes:
        paths: The wrapped file paths.
        enabled: When ``False`` the transaction is a complete no-op
            (no snapshot, no rollback) — this lets callers wire an opt-in
            ``--transactional`` flag without branching around the ``with``.
        keep_failed: Whether rollback preserves the failed attempt as a
            forensic sidecar before restoring.
        restored: Paths actually restored by :meth:`rollback` (paths whose
            bytes were unchanged are skipped and never appear here).
        sidecars: Forensic sidecar paths written by :meth:`rollback`.
    """

    def __init__(
        self,
        *paths: str | Path,
        enabled: bool = True,
        keep_failed: bool = True,
    ) -> None:
        if not paths:
            raise ValueError("BoardTransaction requires at least one path")
        self.paths: list[Path] = [Path(p) for p in paths]
        self.enabled = enabled
        self.keep_failed = keep_failed
        self.restored: list[Path] = []
        self.sidecars: list[Path] = []
        self._snapshots: dict[Path, bytes | None] = {}
        self._entered = False

    def __enter__(self) -> BoardTransaction:
        if self.enabled:
            for path in self.paths:
                # None means "did not exist on entry" -> rollback unlinks.
                self._snapshots[path] = path.read_bytes() if path.exists() else None
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        # Any escaping BaseException (including KeyboardInterrupt from a
        # Ctrl-C mid-mutation) triggers rollback; the exception always
        # propagates.  A TransactionRestoreError raised here chains onto
        # the original exception rather than replacing it.
        if exc_type is not None:
            self.rollback()
        return False

    def rollback(self) -> None:
        """Restore every wrapped path to its entry snapshot.

        Paths whose current bytes already equal the entry snapshot are
        skipped (no restore, no sidecar), which makes this method cheap
        and idempotent — safe to call on failure exit paths even when no
        mutation actually happened.

        Raises:
            TransactionRestoreError: If any restore (or sidecar write)
                failed.  All paths are attempted before raising so one
                bad path cannot block restoring the others.
        """
        if not self.enabled or not self._entered:
            return

        errors: list[str] = []
        for path in self.paths:
            snapshot = self._snapshots.get(path)
            try:
                current: bytes | None = path.read_bytes() if path.exists() else None
            except OSError as e:
                errors.append(f"could not read current state of {path}: {e}")
                continue

            if current == snapshot:
                continue  # unchanged (or already rolled back) -> nothing to do

            # Preserve the failed attempt for forensics BEFORE restoring,
            # so even a failed restore leaves the sidecar behind.
            if self.keep_failed and current is not None:
                sidecar = self._sidecar_path(path)
                try:
                    sidecar.write_bytes(current)
                    self.sidecars.append(sidecar)
                except OSError as e:
                    errors.append(f"could not write forensic sidecar for {path}: {e}")

            try:
                self._restore(path, snapshot)
                self.restored.append(path)
            except OSError as e:
                errors.append(f"could not restore {path}: {e}")

        if errors:
            raise TransactionRestoreError(
                "transactional rollback failed to fully restore the pre-command "
                "state:\n  " + "\n  ".join(errors)
            )

    @staticmethod
    def _restore(path: Path, snapshot: bytes | None) -> None:
        """Atomically restore *path* to *snapshot* (or remove it if None)."""
        if snapshot is None:
            path.unlink(missing_ok=True)
            return
        # Atomic write: sibling tmp file -> fsync -> os.replace.  The
        # sibling-in-same-dir placement makes os.replace a same-filesystem
        # rename (atomic on POSIX), mirroring route_cmd's atomic save, so a
        # crash mid-restore cannot leave a half-written board.
        tmp_path = path.with_name(path.name + ".txn-restore.tmp")
        try:
            tmp_path.write_bytes(snapshot)
            with open(tmp_path, "rb") as f:
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _sidecar_path(path: Path) -> Path:
        """Pick a non-clobbering ``<file>.failed-<UTC-timestamp><suffix>`` name."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = path.with_name(f"{path.name}.failed-{stamp}{path.suffix}")
        counter = 1
        while candidate.exists():
            candidate = path.with_name(f"{path.name}.failed-{stamp}-{counter}{path.suffix}")
            counter += 1
        return candidate

    def report_lines(self) -> list[str]:
        """Human-readable summary of the last rollback (for CLI stderr)."""
        lines: list[str] = []
        for path in self.restored:
            lines.append(f"--transactional: rolled back {path} to its pre-command state")
        for sidecar in self.sidecars:
            lines.append(f"--transactional: failed attempt preserved at {sidecar}")
        return lines


def board_transaction(
    *paths: str | Path,
    enabled: bool = True,
    keep_failed: bool = True,
) -> BoardTransaction:
    """Create a file-scoped snapshot/rollback transaction over *paths*.

    See the module docstring for the full guarantees.  With
    ``enabled=False`` the returned transaction is a complete no-op, which
    lets CLI commands wire an opt-in ``--transactional`` flag without
    branching around the ``with`` block.

    Args:
        paths: One or more files to protect.  Paths that do not exist yet
            are supported — rollback removes them again.
        enabled: Master switch; ``False`` disables snapshotting entirely.
        keep_failed: Preserve the failed attempt as a
            ``<file>.failed-<UTC-timestamp><suffix>`` sidecar on rollback.

    Returns:
        A :class:`BoardTransaction` context manager.
    """
    return BoardTransaction(*paths, enabled=enabled, keep_failed=keep_failed)
