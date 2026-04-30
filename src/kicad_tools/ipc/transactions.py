"""Transaction support for atomic KiCad IPC operations.

Wraps multi-step operations in ``begin_commit()`` / ``push_commit()`` /
``drop_commit()`` to ensure atomic undo. When pushing a full routing
solution (potentially hundreds of tracks and vias), transactions ensure
that either all items are applied or none are.

Usage as a context manager::

    from kicad_tools.ipc import IPCClient, Transaction

    with IPCClient(socket_path) as client:
        with Transaction(client) as txn:
            # All operations in this block are grouped
            txn.create_items([track1, track2, via1])
            # On success: push_commit() is called automatically
            # On exception: drop_commit() reverts all changes
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.ipc.client import IPCClient

logger = logging.getLogger(__name__)


class TransactionError(Exception):
    """Raised when a transaction operation fails."""


class Transaction:
    """Context manager for atomic KiCad board modifications.

    A transaction groups multiple board modifications into a single
    undoable operation. If any step fails, the entire transaction
    is rolled back.

    Args:
        client: Connected IPCClient instance.
        description: Optional human-readable description for the undo stack.
    """

    def __init__(
        self,
        client: IPCClient,
        description: str = "kicad-tools operation",
    ) -> None:
        self._client = client
        self._description = description
        self._active = False
        self._committed = False

    @property
    def active(self) -> bool:
        """Whether this transaction is currently open."""
        return self._active

    @property
    def committed(self) -> bool:
        """Whether this transaction was successfully committed."""
        return self._committed

    def begin(self) -> None:
        """Start the transaction.

        Sends ``BeginCommit`` to KiCad to open an undo group.

        Raises:
            TransactionError: If a transaction is already active.
        """
        if self._active:
            raise TransactionError("Transaction already active")

        from kicad_tools.ipc.client import IPCError

        try:
            self._client.send_command(
                "BeginCommit",
                params={"description": self._description},
            )
            self._active = True
            logger.info("Transaction started: %s", self._description)
        except IPCError as exc:
            raise TransactionError(f"Failed to begin transaction: {exc}") from exc

    def commit(self) -> None:
        """Commit the transaction.

        Sends ``PushCommit`` to KiCad to finalize all changes as a
        single undo step.

        Raises:
            TransactionError: If no transaction is active.
        """
        if not self._active:
            raise TransactionError("No active transaction to commit")

        from kicad_tools.ipc.client import IPCError

        try:
            self._client.send_command("PushCommit")
            self._active = False
            self._committed = True
            logger.info("Transaction committed: %s", self._description)
        except IPCError as exc:
            # Try to roll back on commit failure
            logger.error("Commit failed, attempting rollback: %s", exc)
            self.rollback()
            raise TransactionError(f"Failed to commit transaction: {exc}") from exc

    def rollback(self) -> None:
        """Roll back the transaction.

        Sends ``DropCommit`` to KiCad to discard all changes made
        since ``begin()``.

        Raises:
            TransactionError: If no transaction is active.
        """
        if not self._active:
            raise TransactionError("No active transaction to rollback")

        from kicad_tools.ipc.client import IPCError

        try:
            self._client.send_command("DropCommit")
            logger.info("Transaction rolled back: %s", self._description)
        except IPCError as exc:
            logger.error("Rollback failed: %s", exc)
            raise TransactionError(f"Failed to rollback transaction: {exc}") from exc
        finally:
            self._active = False

    def create_items(self, items: list[dict[str, Any]]) -> list[str]:
        """Create board items within this transaction.

        Args:
            items: List of item dicts (tracks, vias, etc.) in KiCad
                API format.

        Returns:
            List of created item IDs (KIIDs).

        Raises:
            TransactionError: If no transaction is active.
        """
        if not self._active:
            raise TransactionError("No active transaction")

        from kicad_tools.ipc.client import IPCError

        try:
            response = self._client.send_command(
                "CreateItems",
                params={"items": items},
            )
            created_ids = response.result.get("created_ids", [])
            logger.debug("Created %d items", len(created_ids))
            return created_ids
        except IPCError as exc:
            raise TransactionError(f"Failed to create items: {exc}") from exc

    def delete_items(self, item_ids: list[str]) -> None:
        """Delete board items within this transaction.

        Args:
            item_ids: List of KIIDs to delete.

        Raises:
            TransactionError: If no transaction is active.
        """
        if not self._active:
            raise TransactionError("No active transaction")

        from kicad_tools.ipc.client import IPCError

        try:
            self._client.send_command(
                "DeleteItems",
                params={"item_ids": item_ids},
            )
            logger.debug("Deleted %d items", len(item_ids))
        except IPCError as exc:
            raise TransactionError(f"Failed to delete items: {exc}") from exc

    def __enter__(self) -> Transaction:
        """Begin transaction on context manager entry."""
        self.begin()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Commit or rollback on context manager exit.

        If an exception occurred, the transaction is rolled back.
        Otherwise, it is committed.
        """
        if not self._active:
            return

        if exc_type is not None:
            logger.info(
                "Exception during transaction, rolling back: %s",
                exc_val,
            )
            try:
                self.rollback()
            except TransactionError:
                logger.error("Rollback failed after exception", exc_info=True)
        else:
            self.commit()

    def __repr__(self) -> str:
        state = "active" if self._active else "committed" if self._committed else "inactive"
        return f"Transaction({self._description!r}, {state})"
