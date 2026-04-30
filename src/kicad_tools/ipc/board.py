"""High-level board operations over KiCad IPC.

This module provides a convenient interface for common PCB board operations,
building on the low-level :class:`IPCClient` and :class:`Transaction` classes.

Operations include:
- Pushing routing solutions (tracks + vias)
- Reading design rules from the running board
- Querying board items by net or type
- Highlighting items in the KiCad GUI

Example::

    from kicad_tools.ipc import IPCClient, BoardOperations

    with IPCClient(socket_path) as client:
        board = BoardOperations(client)
        rules = board.get_design_rules()
        print(f"Min track width: {rules['min_track_width_nm']}nm")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kicad_tools.ipc.proto.messages import TrackSegment, Via
from kicad_tools.ipc.transactions import Transaction

if TYPE_CHECKING:
    from kicad_tools.ipc.client import IPCClient

logger = logging.getLogger(__name__)


class BoardOperations:
    """High-level board operations using KiCad IPC API.

    Args:
        client: Connected IPCClient instance.
    """

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def push_routes(
        self,
        tracks: list[TrackSegment],
        vias: list[Via] | None = None,
        description: str = "Push routes from kicad-tools",
    ) -> list[str]:
        """Push a routing solution to the running KiCad board.

        All tracks and vias are created in a single transaction so they
        appear as one undo step in KiCad.

        Args:
            tracks: List of track segments to create.
            vias: Optional list of vias to create.
            description: Description for the undo stack entry.

        Returns:
            List of created item IDs (KIIDs).
        """
        items: list[dict[str, Any]] = []
        for track in tracks:
            item = track.to_dict()
            item["type"] = "track"
            items.append(item)

        if vias:
            for via in vias:
                item = via.to_dict()
                item["type"] = "via"
                items.append(item)

        logger.info(
            "Pushing %d tracks and %d vias",
            len(tracks),
            len(vias) if vias else 0,
        )

        with Transaction(self._client, description=description) as txn:
            created_ids = txn.create_items(items)

        return created_ids

    def get_design_rules(self) -> dict[str, Any]:
        """Read the active design rules from the running board.

        Returns:
            Dict with design rule values, e.g.::

                {
                    "min_track_width_nm": 150000,
                    "min_clearance_nm": 150000,
                    "min_via_diameter_nm": 600000,
                    "min_via_drill_nm": 300000,
                    ...
                }
        """
        response = self._client.send_command("GetDesignRules")
        return response.result

    def get_nets(self) -> list[dict[str, Any]]:
        """List all nets in the board.

        Returns:
            List of net info dicts with keys like ``name``, ``code``.
        """
        response = self._client.send_command("GetNets")
        return response.result.get("nets", [])

    def get_items(
        self,
        item_type: str | None = None,
        net: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query board items by type and/or net.

        Args:
            item_type: Filter by item type (e.g., ``"track"``, ``"via"``).
            net: Filter by net code.

        Returns:
            List of item dicts.
        """
        params: dict[str, Any] = {}
        if item_type is not None:
            params["type"] = item_type
        if net is not None:
            params["net"] = net

        response = self._client.send_command("GetItems", params=params)
        return response.result.get("items", [])

    def highlight_net(self, net_code: int) -> None:
        """Highlight a net in the KiCad GUI.

        Args:
            net_code: The net code to highlight.
        """
        self._client.send_command(
            "HighlightNet",
            params={"net": net_code},
        )

    def clear_highlight(self) -> None:
        """Clear all highlights in the KiCad GUI."""
        self._client.send_command("ClearHighlight")

    def refresh_board(self) -> None:
        """Request KiCad to refresh/redraw the board view."""
        self._client.send_command("RefreshBoard")

    def get_board_bounding_box(self) -> dict[str, Any]:
        """Get the bounding box of the entire board.

        Returns:
            Dict with ``min_x``, ``min_y``, ``max_x``, ``max_y`` in nm.
        """
        response = self._client.send_command("GetBoardBoundingBox")
        return response.result

    def get_stackup(self) -> list[dict[str, Any]]:
        """Get the board layer stackup configuration.

        Returns:
            List of layer dicts with ``name``, ``type``, ``thickness_nm``, etc.
        """
        response = self._client.send_command("GetStackup")
        return response.result.get("layers", [])

    def delete_tracks_on_net(
        self,
        net_code: int,
        description: str = "Delete tracks from kicad-tools",
    ) -> int:
        """Delete all tracks on a given net.

        Args:
            net_code: The net code whose tracks to delete.
            description: Description for the undo stack entry.

        Returns:
            Number of deleted tracks.
        """
        items = self.get_items(item_type="track", net=net_code)
        if not items:
            return 0

        item_ids = [item["id"] for item in items if "id" in item]
        if not item_ids:
            return 0

        with Transaction(self._client, description=description) as txn:
            txn.delete_items(item_ids)

        return len(item_ids)

    def __repr__(self) -> str:
        return f"BoardOperations({self._client!r})"
