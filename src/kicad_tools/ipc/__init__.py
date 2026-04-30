"""KiCad IPC API client for live instance interaction.

This module provides a Python client for KiCad's IPC API (protobuf over NNG),
enabling two-phase workflows: compute offline with kicad-tools, then push
results into a running KiCad instance via IPC.

Requires KiCad 9.0+ and the optional ``ipc`` dependency group::

    pip install 'kicad-tools[ipc]'

Example usage::

    from kicad_tools.ipc import IPCClient, discover_socket

    socket_path = discover_socket()
    with IPCClient(socket_path) as client:
        version = client.get_version()
        print(f"Connected to KiCad {version}")

The IPC client is separate from the MCP server (``kicad_tools.mcp``).
The MCP server provides tools for AI agents; the IPC client communicates
with KiCad itself.
"""

from kicad_tools.ipc.board import BoardOperations
from kicad_tools.ipc.client import IPCClient
from kicad_tools.ipc.discovery import (
    KiCadInstance,
    discover_instances,
    discover_socket,
)
from kicad_tools.ipc.transactions import Transaction

__all__ = [
    "BoardOperations",
    "IPCClient",
    "KiCadInstance",
    "Transaction",
    "discover_instances",
    "discover_socket",
]
