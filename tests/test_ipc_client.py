"""Tests for KiCad IPC client with mocked NNG transport."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.ipc.client import (
    IPCApiError,
    IPCClient,
    IPCConnectionError,
    IPCProtocolError,
)
from kicad_tools.ipc.proto.messages import StatusCode


@pytest.fixture
def mock_pynng():
    """Provide a mocked pynng module."""
    mock_module = MagicMock()
    mock_socket = MagicMock()
    mock_module.Req0.return_value = mock_socket
    mock_module.exceptions = MagicMock()
    mock_module.exceptions.ConnectionRefused = type("ConnectionRefused", (Exception,), {})
    mock_module.exceptions.AddressInUse = type("AddressInUse", (Exception,), {})
    return mock_module, mock_socket


class TestIPCClientInit:
    """Tests for IPCClient initialization."""

    def test_init_defaults(self):
        client = IPCClient("/tmp/kicad/test.sock")
        assert not client.connected
        assert client.socket_path.name == "test.sock"
        assert client.token == ""

    def test_init_custom_timeout(self):
        client = IPCClient("/tmp/test.sock", timeout_ms=10000)
        assert not client.connected

    def test_repr_disconnected(self):
        client = IPCClient("/tmp/test.sock")
        assert "disconnected" in repr(client)


class TestIPCClientConnect:
    """Tests for connection lifecycle."""

    def test_connect_success(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        client = IPCClient("/tmp/kicad/test.sock")

        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()

        assert client.connected
        mock_module.Req0.assert_called_once()

    def test_connect_already_connected(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        client = IPCClient("/tmp/kicad/test.sock")

        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            # Second connect should be a no-op
            client.connect()

        # Req0 should only be called once
        assert mock_module.Req0.call_count == 1

    def test_connect_refused(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        mock_module.Req0.side_effect = mock_module.exceptions.ConnectionRefused()
        client = IPCClient("/tmp/kicad/test.sock")

        with patch.dict("sys.modules", {"pynng": mock_module}):
            with pytest.raises(IPCConnectionError, match="Connection refused"):
                client.connect()

    def test_connect_address_in_use(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        mock_module.Req0.side_effect = mock_module.exceptions.AddressInUse()
        client = IPCClient("/tmp/kicad/test.sock")

        with patch.dict("sys.modules", {"pynng": mock_module}):
            with pytest.raises(IPCConnectionError, match="already in use"):
                client.connect()

    def test_connect_import_error(self):
        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": None}):
            with pytest.raises(ImportError, match="pynng"):
                client.connect()

    def test_disconnect(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        client = IPCClient("/tmp/kicad/test.sock")

        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            client.disconnect()

        assert not client.connected
        mock_socket.close.assert_called_once()

    def test_disconnect_when_not_connected(self):
        client = IPCClient("/tmp/kicad/test.sock")
        # Should not raise
        client.disconnect()
        assert not client.connected

    def test_reconnect(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        client = IPCClient("/tmp/kicad/test.sock")

        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            client.reconnect()

        assert client.connected
        assert mock_module.Req0.call_count == 2


class TestIPCClientSendCommand:
    """Tests for command send/receive."""

    def test_send_command_success(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        response_data = {"status": 0, "message": "", "result": {"version": "9.0.1"}}
        mock_socket.recv.return_value = json.dumps(response_data).encode()

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            response = client.send_command("GetVersion")

        assert response.ok
        assert response.result["version"] == "9.0.1"

    def test_send_command_with_params(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        response_data = {"status": 0, "message": "", "result": {}}
        mock_socket.recv.return_value = json.dumps(response_data).encode()

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            client.send_command("CreateItems", params={"items": [{"type": "track"}]})

        sent_bytes = mock_socket.send.call_args[0][0]
        sent_data = json.loads(sent_bytes)
        assert sent_data["command"] == "CreateItems"
        assert sent_data["params"]["items"] == [{"type": "track"}]

    def test_send_command_not_connected(self):
        client = IPCClient("/tmp/kicad/test.sock")
        with pytest.raises(IPCConnectionError, match="Not connected"):
            client.send_command("GetVersion")

    def test_send_command_api_error(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        response_data = {"status": 3, "message": "Board not loaded", "result": {}}
        mock_socket.recv.return_value = json.dumps(response_data).encode()

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            with pytest.raises(IPCApiError) as exc_info:
                client.send_command("GetNets")

        assert exc_info.value.status == StatusCode.AS_ERROR
        assert "Board not loaded" in exc_info.value.api_message

    def test_send_command_invalid_json_response(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        mock_socket.recv.return_value = b"not json"

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            with pytest.raises(IPCProtocolError, match="Invalid response"):
                client.send_command("GetVersion")

    def test_send_command_captures_token(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        response_data = {"status": 0, "message": "", "result": {"token": "abc123"}}
        mock_socket.recv.return_value = json.dumps(response_data).encode()

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            client.send_command("Connect")

        assert client.token == "abc123"

    def test_send_command_includes_token(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        # First response sets token
        response_with_token = {"status": 0, "result": {"token": "tok123"}}
        # Second response is normal
        response_normal = {"status": 0, "result": {"version": "9.0"}}
        mock_socket.recv.side_effect = [
            json.dumps(response_with_token).encode(),
            json.dumps(response_normal).encode(),
        ]

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            client.send_command("Connect")
            client.send_command("GetVersion")

        # Second call should include the token
        second_call_bytes = mock_socket.send.call_args_list[1][0][0]
        second_call_data = json.loads(second_call_bytes)
        assert second_call_data["token"] == "tok123"


class TestIPCClientPing:
    """Tests for health check."""

    def test_ping_success(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        response_data = {"status": 0, "result": {}}
        mock_socket.recv.return_value = json.dumps(response_data).encode()

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            assert client.ping() is True

    def test_ping_not_connected(self):
        client = IPCClient("/tmp/kicad/test.sock")
        assert client.ping() is False

    def test_ping_failure(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        mock_socket.recv.side_effect = Exception("timeout")

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            assert client.ping() is False


class TestIPCClientContextManager:
    """Tests for context manager usage."""

    def test_context_manager(self, mock_pynng):
        mock_module, mock_socket = mock_pynng

        with patch.dict("sys.modules", {"pynng": mock_module}):
            with IPCClient("/tmp/kicad/test.sock") as client:
                assert client.connected

        assert not client.connected
        mock_socket.close.assert_called_once()

    def test_context_manager_exception(self, mock_pynng):
        mock_module, mock_socket = mock_pynng

        with patch.dict("sys.modules", {"pynng": mock_module}):
            with pytest.raises(ValueError):
                with IPCClient("/tmp/kicad/test.sock"):
                    raise ValueError("test error")

        # Socket should still be closed
        mock_socket.close.assert_called_once()


class TestIPCClientGetVersion:
    """Tests for get_version helper."""

    def test_get_version(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        response_data = {"status": 0, "result": {"version": "9.0.2"}}
        mock_socket.recv.return_value = json.dumps(response_data).encode()

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            assert client.get_version() == "9.0.2"

    def test_get_version_missing_key(self, mock_pynng):
        mock_module, mock_socket = mock_pynng
        response_data = {"status": 0, "result": {}}
        mock_socket.recv.return_value = json.dumps(response_data).encode()

        client = IPCClient("/tmp/kicad/test.sock")
        with patch.dict("sys.modules", {"pynng": mock_module}):
            client.connect()
            assert client.get_version() == "unknown"
