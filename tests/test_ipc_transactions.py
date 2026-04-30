"""Tests for KiCad IPC transaction lifecycle."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kicad_tools.ipc.client import IPCClient
from kicad_tools.ipc.transactions import Transaction, TransactionError


@pytest.fixture
def mock_client():
    """Provide a mocked IPCClient that returns success responses."""
    client = MagicMock(spec=IPCClient)
    client.connected = True

    # Default: all commands succeed
    response = MagicMock()
    response.ok = True
    response.result = {}
    client.send_command.return_value = response
    return client


class TestTransactionInit:
    """Tests for Transaction initialization."""

    def test_init_defaults(self, mock_client):
        txn = Transaction(mock_client)
        assert not txn.active
        assert not txn.committed
        assert "inactive" in repr(txn)

    def test_init_custom_description(self, mock_client):
        txn = Transaction(mock_client, description="test op")
        assert "test op" in repr(txn)


class TestTransactionLifecycle:
    """Tests for begin/commit/rollback sequence."""

    def test_begin(self, mock_client):
        txn = Transaction(mock_client)
        txn.begin()
        assert txn.active
        mock_client.send_command.assert_called_once_with(
            "BeginCommit",
            params={"description": "kicad-tools operation"},
        )

    def test_begin_already_active(self, mock_client):
        txn = Transaction(mock_client)
        txn.begin()
        with pytest.raises(TransactionError, match="already active"):
            txn.begin()

    def test_commit(self, mock_client):
        txn = Transaction(mock_client)
        txn.begin()
        txn.commit()
        assert not txn.active
        assert txn.committed

        calls = [c[0][0] for c in mock_client.send_command.call_args_list]
        assert "BeginCommit" in calls
        assert "PushCommit" in calls

    def test_commit_not_active(self, mock_client):
        txn = Transaction(mock_client)
        with pytest.raises(TransactionError, match="No active transaction"):
            txn.commit()

    def test_rollback(self, mock_client):
        txn = Transaction(mock_client)
        txn.begin()
        txn.rollback()
        assert not txn.active
        assert not txn.committed

        calls = [c[0][0] for c in mock_client.send_command.call_args_list]
        assert "DropCommit" in calls

    def test_rollback_not_active(self, mock_client):
        txn = Transaction(mock_client)
        with pytest.raises(TransactionError, match="No active transaction"):
            txn.rollback()

    def test_begin_fails(self, mock_client):
        from kicad_tools.ipc.client import IPCError

        mock_client.send_command.side_effect = IPCError("failed")
        txn = Transaction(mock_client)
        with pytest.raises(TransactionError, match="Failed to begin"):
            txn.begin()
        assert not txn.active


class TestTransactionContextManager:
    """Tests for context manager usage."""

    def test_success_auto_commits(self, mock_client):
        with Transaction(mock_client) as txn:
            pass

        assert txn.committed
        calls = [c[0][0] for c in mock_client.send_command.call_args_list]
        assert "BeginCommit" in calls
        assert "PushCommit" in calls

    def test_exception_auto_rollbacks(self, mock_client):
        with pytest.raises(ValueError):
            with Transaction(mock_client) as txn:
                raise ValueError("boom")

        assert not txn.committed
        calls = [c[0][0] for c in mock_client.send_command.call_args_list]
        assert "BeginCommit" in calls
        assert "DropCommit" in calls

    def test_context_manager_repr(self, mock_client):
        with Transaction(mock_client, description="test") as txn:
            assert "active" in repr(txn)
        assert "committed" in repr(txn)


class TestTransactionCreateItems:
    """Tests for item creation within transactions."""

    def test_create_items(self, mock_client):
        response = MagicMock()
        response.ok = True
        response.result = {"created_ids": ["id1", "id2"]}
        mock_client.send_command.return_value = response

        txn = Transaction(mock_client)
        txn.begin()
        ids = txn.create_items([{"type": "track"}, {"type": "via"}])
        assert ids == ["id1", "id2"]

    def test_create_items_not_active(self, mock_client):
        txn = Transaction(mock_client)
        with pytest.raises(TransactionError, match="No active transaction"):
            txn.create_items([{"type": "track"}])

    def test_create_items_fails(self, mock_client):
        from kicad_tools.ipc.client import IPCError

        # begin succeeds, CreateItems fails
        responses = [MagicMock(ok=True, result={})]
        mock_client.send_command.side_effect = [
            responses[0],
            IPCError("create failed"),
        ]

        txn = Transaction(mock_client)
        txn.begin()
        with pytest.raises(TransactionError, match="Failed to create"):
            txn.create_items([{"type": "track"}])


class TestTransactionDeleteItems:
    """Tests for item deletion within transactions."""

    def test_delete_items(self, mock_client):
        txn = Transaction(mock_client)
        txn.begin()
        txn.delete_items(["id1", "id2"])

        # Should have called DeleteItems
        delete_calls = [
            c for c in mock_client.send_command.call_args_list
            if c[0][0] == "DeleteItems"
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][1]["params"]["item_ids"] == ["id1", "id2"]

    def test_delete_items_not_active(self, mock_client):
        txn = Transaction(mock_client)
        with pytest.raises(TransactionError, match="No active transaction"):
            txn.delete_items(["id1"])
