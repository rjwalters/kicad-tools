"""NNG connection management for KiCad IPC API.

This module provides the low-level transport layer for communicating with
KiCad over NNG (nanomsg next generation) sockets. It handles:

- Connection lifecycle (connect, disconnect, reconnect)
- Request/response serialization (JSON over NNG)
- Health checks and keepalive
- Timeout and error handling

The client uses the NNG ``Req0`` (request) socket pattern, which matches
KiCad's ``Rep0`` (reply) server pattern.

Requires ``pynng`` package::

    pip install 'kicad-tools[ipc]'
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kicad_tools.ipc.proto.messages import ApiRequest, ApiResponse, StatusCode

logger = logging.getLogger(__name__)

# Default timeout for NNG operations in milliseconds
DEFAULT_TIMEOUT_MS = 5000

# Timeout for health check operations (shorter than normal requests)
HEALTH_CHECK_TIMEOUT_MS = 2000


class IPCError(Exception):
    """Base exception for IPC communication errors."""


class IPCConnectionError(IPCError):
    """Raised when connection to KiCad cannot be established."""


class IPCTimeoutError(IPCError):
    """Raised when a request times out waiting for a response."""


class IPCProtocolError(IPCError):
    """Raised when KiCad returns an unexpected response format."""


class IPCApiError(IPCError):
    """Raised when KiCad returns an API-level error.

    Attributes:
        status: The KiCad API status code.
        api_message: The error message from KiCad.
    """

    def __init__(self, status: StatusCode, api_message: str) -> None:
        self.status = status
        self.api_message = api_message
        super().__init__(f"KiCad API error ({status.name}): {api_message}")


class IPCClient:
    """Client for KiCad's IPC API over NNG.

    This client manages a single NNG request socket connected to a
    running KiCad instance. KiCad's IPC API is single-client, so only
    one ``IPCClient`` can be connected at a time.

    Usage::

        client = IPCClient("/tmp/kicad/kicad.sock")
        client.connect()
        try:
            response = client.send_command("GetVersion")
            print(response.result)
        finally:
            client.disconnect()

    Or as a context manager::

        with IPCClient("/tmp/kicad/kicad.sock") as client:
            response = client.send_command("GetVersion")

    Args:
        socket_path: Path to the KiCad NNG socket.
        timeout_ms: Default timeout for requests in milliseconds.
    """

    def __init__(
        self,
        socket_path: str | Path,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._timeout_ms = timeout_ms
        self._socket: Any = None  # pynng.Req0 when connected
        self._connected = False
        self._token: str = ""

    @property
    def socket_path(self) -> Path:
        """The NNG socket path this client connects to."""
        return self._socket_path

    @property
    def connected(self) -> bool:
        """Whether the client is currently connected."""
        return self._connected

    @property
    def token(self) -> str:
        """The API token from the last successful connection."""
        return self._token

    def connect(self) -> None:
        """Establish connection to KiCad IPC socket.

        Raises:
            IPCConnectionError: If the socket cannot be reached.
            ImportError: If ``pynng`` is not installed.
        """
        if self._connected:
            logger.debug("Already connected to %s", self._socket_path)
            return

        try:
            import pynng
        except ImportError as exc:
            raise ImportError(
                "pynng is required for KiCad IPC communication. "
                "Install it with: pip install 'kicad-tools[ipc]'"
            ) from exc

        address = f"ipc://{self._socket_path}"
        logger.info("Connecting to KiCad IPC at %s", address)

        try:
            self._socket = pynng.Req0(
                dial=address,
                recv_timeout=self._timeout_ms,
                send_timeout=self._timeout_ms,
            )
            self._connected = True
            logger.info("Connected to KiCad IPC at %s", address)
        except pynng.exceptions.ConnectionRefused:
            raise IPCConnectionError(
                f"Connection refused at {address}. "
                "Is KiCad running with IPC enabled?"
            )
        except pynng.exceptions.AddressInUse:
            raise IPCConnectionError(
                f"Socket at {address} is already in use. "
                "KiCad's IPC API supports only one client at a time."
            )
        except Exception as exc:
            raise IPCConnectionError(
                f"Failed to connect to {address}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        """Close the NNG socket connection."""
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                logger.debug("Error closing socket", exc_info=True)
            finally:
                self._socket = None
                self._connected = False
                self._token = ""
                logger.info("Disconnected from KiCad IPC")

    def reconnect(self) -> None:
        """Disconnect and reconnect to the KiCad IPC socket."""
        self.disconnect()
        self.connect()

    def send_command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> ApiResponse:
        """Send a command to KiCad and wait for the response.

        Args:
            command: The API command name (e.g., ``"GetVersion"``).
            params: Optional command parameters.
            timeout_ms: Override the default timeout for this request.

        Returns:
            The parsed API response.

        Raises:
            IPCConnectionError: If not connected.
            IPCTimeoutError: If the request times out.
            IPCProtocolError: If the response cannot be parsed.
            IPCApiError: If KiCad returns a non-OK status.
        """
        if not self._connected or self._socket is None:
            raise IPCConnectionError("Not connected. Call connect() first.")

        request = ApiRequest(
            command=command,
            token=self._token,
            params=params or {},
        )

        request_bytes = json.dumps(request.to_dict()).encode("utf-8")
        logger.debug("Sending command: %s", command)

        try:
            # Apply per-request timeout if specified
            if timeout_ms is not None:
                self._socket.recv_timeout = timeout_ms

            self._socket.send(request_bytes)
            response_bytes = self._socket.recv()

            # Restore default timeout
            if timeout_ms is not None:
                self._socket.recv_timeout = self._timeout_ms

        except Exception as exc:
            exc_name = type(exc).__name__
            # Check for timeout specifically
            if "Timeout" in exc_name or "TimedOut" in exc_name:
                raise IPCTimeoutError(
                    f"Request '{command}' timed out after "
                    f"{timeout_ms or self._timeout_ms}ms. "
                    "KiCad may be busy with user interaction."
                ) from exc
            raise IPCConnectionError(
                f"Communication error during '{command}': {exc}"
            ) from exc

        try:
            response_data = json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IPCProtocolError(
                f"Invalid response from KiCad for '{command}': {exc}"
            ) from exc

        response = ApiResponse.from_dict(response_data)

        if not response.ok:
            raise IPCApiError(response.status, response.message)

        # Capture token from initial handshake if present
        if "token" in response.result:
            self._token = response.result["token"]
            logger.debug("Received API token")

        return response

    def ping(self) -> bool:
        """Check if KiCad is responsive.

        Sends a lightweight health check command.

        Returns:
            True if KiCad responded successfully, False otherwise.
        """
        if not self._connected:
            return False

        try:
            self.send_command("Ping", timeout_ms=HEALTH_CHECK_TIMEOUT_MS)
            return True
        except IPCError:
            return False

    def get_version(self) -> str:
        """Query the KiCad version string.

        Returns:
            Version string (e.g., ``"9.0.1"``).

        Raises:
            IPCError: If the request fails.
        """
        response = self.send_command("GetVersion")
        return response.result.get("version", "unknown")

    def get_open_documents(self) -> list[dict[str, Any]]:
        """List currently open documents in KiCad.

        Returns:
            List of document info dicts with keys like ``path``, ``type``.

        Raises:
            IPCError: If the request fails.
        """
        response = self.send_command("GetOpenDocuments")
        return response.result.get("documents", [])

    def __enter__(self) -> IPCClient:
        """Connect on context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Disconnect on context manager exit."""
        self.disconnect()

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return f"IPCClient({self._socket_path!s}, {state})"
