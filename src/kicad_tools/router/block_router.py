"""
Per-block detail router with sub-Pathfinder (sub-block Phase 4).

This module provides the BlockRouter class that creates a confined sub-grid
for a PCBBlock's bounding box and routes block-internal nets using the
existing A* pathfinder. After block routing, results are transformed to
board coordinates and reported back to the Autorouter for integration.

The BlockRouter enables hierarchical routing where each block gets its own
routing pass within its physical space, then inter-block nets are routed
globally using block ports as endpoints.

Issue #1589: Per-block detail routing with sub-Pathfinder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.pcb.blocks.base import PCBBlock

from .cpp_backend import create_hybrid_router
from .grid import RoutingGrid
from .layers import LayerStack
from .primitives import Pad, Route
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting

logger = logging.getLogger(__name__)


@dataclass
class BlockRoutingResult:
    """Result of routing a single block's internal nets.

    Attributes:
        block_id: Identifier of the routed block.
        routes: Route objects in board (absolute) coordinates.
        routed_nets: Set of net IDs that were successfully routed internally.
        failed_nets: Set of net IDs that failed to route.
        connected_pad_keys: Set of (ref, pin) pad keys connected by internal routes.
            These pads can be removed from the global routing pool.
    """

    block_id: str = ""
    routes: list[Route] = field(default_factory=list)
    routed_nets: set[int] = field(default_factory=set)
    failed_nets: set[int] = field(default_factory=set)
    connected_pad_keys: set[tuple[str, str]] = field(default_factory=set)
    inter_block_nets: set[int] = field(default_factory=set)


class BlockRouter:
    """Per-block detail router using a confined sub-grid.

    Creates a localized RoutingGrid covering only the block's bounding box,
    populates it with block-internal component pads, and routes block-internal
    nets using the existing A* pathfinder. Routes are then transformed from
    sub-grid (block-local) coordinates to board (absolute) coordinates.

    Args:
        block: A placed PCBBlock with components and ports.
        rules: Design rules for routing.
        net_class_map: Net class to routing config mapping.
        layer_stack: Layer stack for routing (default: 2-layer).
        margin: Extra margin around bounding box in mm (default: 1.0).
        force_python: Force Python pathfinder backend (default: False).
    """

    def __init__(
        self,
        block: PCBBlock,
        rules: DesignRules,
        net_class_map: dict[str, NetClassRouting] | None = None,
        layer_stack: LayerStack | None = None,
        margin: float = 1.0,
        force_python: bool = False,
    ):
        if not block.placed:
            raise ValueError(
                f"Block '{block.block_id}' must be placed before creating a BlockRouter"
            )

        self.block = block
        self.rules = rules
        self.net_class_map = net_class_map or DEFAULT_NET_CLASS_MAP
        self.layer_stack = layer_stack or LayerStack.two_layer()
        self.margin = margin
        self._force_python = force_python

        # Compute absolute bounding box with margin
        bbox = block.bounding_box
        self._abs_min_x = bbox.min_x + block.origin.x - margin
        self._abs_min_y = bbox.min_y + block.origin.y - margin
        self._abs_max_x = bbox.max_x + block.origin.x + margin
        self._abs_max_y = bbox.max_y + block.origin.y + margin
        self._width = self._abs_max_x - self._abs_min_x
        self._height = self._abs_max_y - self._abs_min_y

        # Sub-grid and router are created lazily on first route call
        self._grid: RoutingGrid | None = None
        self._router = None

        # Pad tracking for the sub-grid
        self._pads: dict[tuple[str, str], Pad] = {}
        self._nets: dict[int, list[tuple[str, str]]] = {}
        self._net_names: dict[int, str] = {}

        # Full autorouter net map for inter-block classification
        self._autorouter_nets: dict[int, list[tuple[str, str]]] | None = None

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Absolute bounding box of the block sub-grid (min_x, min_y, max_x, max_y)."""
        return (self._abs_min_x, self._abs_min_y, self._abs_max_x, self._abs_max_y)

    @property
    def origin_x(self) -> float:
        """X origin of the sub-grid in board coordinates."""
        return self._abs_min_x

    @property
    def origin_y(self) -> float:
        """Y origin of the sub-grid in board coordinates."""
        return self._abs_min_y

    def _create_sub_grid(self) -> None:
        """Create the confined sub-grid covering the block's bounding box."""
        self._grid = RoutingGrid(
            self._width,
            self._height,
            self.rules,
            self._abs_min_x,
            self._abs_min_y,
            layer_stack=self.layer_stack,
        )
        self._router = create_hybrid_router(
            self._grid,
            self.rules,
            force_python=self._force_python,
            net_class_map=self.net_class_map,
        )

    def add_pad(self, pad: Pad) -> None:
        """Register a pad on the sub-grid.

        Only pads within the block's bounding box are accepted. Pads
        outside the bounding box are silently ignored.

        Args:
            pad: Pad to register.
        """
        # Check if pad is within the block's bounding box
        if not (
            self._abs_min_x <= pad.x <= self._abs_max_x
            and self._abs_min_y <= pad.y <= self._abs_max_y
        ):
            return

        key = (pad.ref, pad.pin)
        self._pads[key] = pad

        # Track nets
        if pad.net not in self._nets:
            self._nets[pad.net] = []
        self._nets[pad.net].append(key)

        if pad.net_name and pad.net not in self._net_names:
            self._net_names[pad.net] = pad.net_name

    def add_pads_from_autorouter(
        self,
        pads: dict[tuple[str, str], Pad],
        nets: dict[int, list[tuple[str, str]]],
        net_names: dict[int, str],
    ) -> None:
        """Bulk-register pads from the main Autorouter that fall within this block.

        Args:
            pads: Autorouter's pad dictionary.
            nets: Autorouter's net-to-pad mapping.
            net_names: Autorouter's net name mapping.
        """
        self._autorouter_nets = nets
        self._net_names.update(net_names)
        for key, pad in pads.items():
            self.add_pad(pad)

    def _classify_nets(self) -> tuple[list[int], list[int]]:
        """Classify nets as block-internal or inter-block.

        A net is block-internal if ALL of its pads in the main design
        are within this block's bounding box. A net is inter-block if
        it has pads both inside and outside the block.

        Returns:
            Tuple of (internal_net_ids, inter_block_net_ids).
        """
        internal: list[int] = []
        inter_block: list[int] = []

        for net_id, local_pad_keys in self._nets.items():
            if net_id == 0:
                continue

            # Compare against the full autorouter net map when available
            if self._autorouter_nets is not None:
                full_pad_keys = self._autorouter_nets.get(net_id, [])
                if len(local_pad_keys) < len(full_pad_keys):
                    # Net has pads outside this block -- inter-block
                    inter_block.append(net_id)
                elif len(local_pad_keys) >= 2:
                    # All pads are inside this block
                    internal.append(net_id)
            else:
                # No autorouter context -- treat all multi-pad nets as internal
                if len(local_pad_keys) >= 2:
                    internal.append(net_id)

        return internal, inter_block

    def _mark_boundary_blocked(self) -> None:
        """Mark boundary cells of the sub-grid as blocked except at port locations.

        This enforces that inter-block routing can only enter/exit through
        designated port positions.
        """
        if self._grid is None:
            return

        grid = self._grid
        port_positions: set[tuple[int, int]] = set()

        # Compute grid positions of all ports
        for port_name in self.block.ports:
            abs_pos = self.block.port(port_name)
            gx, gy = grid.world_to_grid(abs_pos.x, abs_pos.y)
            port_positions.add((gx, gy))

        # Mark top and bottom boundary rows
        for layer_idx in grid.get_routable_indices():
            for gx in range(grid.cols):
                # Top boundary (gy=0)
                if (gx, 0) not in port_positions:
                    grid.grid[layer_idx][0][gx].blocked = True
                # Bottom boundary (gy=rows-1)
                if (gx, grid.rows - 1) not in port_positions:
                    grid.grid[layer_idx][grid.rows - 1][gx].blocked = True

            for gy in range(grid.rows):
                # Left boundary (gx=0)
                if (0, gy) not in port_positions:
                    grid.grid[layer_idx][gy][0].blocked = True
                # Right boundary (gx=cols-1)
                if (grid.cols - 1, gy) not in port_positions:
                    grid.grid[layer_idx][gy][grid.cols - 1].blocked = True

    def route_block(self) -> BlockRoutingResult:
        """Route all block-internal nets on the sub-grid.

        Creates the sub-grid, registers pads, enforces boundary constraints,
        and routes each internal net. Results are in board (absolute)
        coordinates since the sub-grid's origin is already in board space.

        Returns:
            BlockRoutingResult with routes and connectivity information.
        """
        result = BlockRoutingResult(block_id=self.block.block_id)

        internal_nets, inter_block_nets = self._classify_nets()
        result.inter_block_nets = set(inter_block_nets)

        if not internal_nets:
            logger.info(
                "Block '%s': no internal nets to route", self.block.block_id
            )
            return result

        # Create sub-grid and register pads
        self._create_sub_grid()
        assert self._grid is not None
        assert self._router is not None

        for pad in self._pads.values():
            self._grid.add_pad(pad)

        # Enforce boundary constraints
        self._mark_boundary_blocked()

        # Route each internal net
        from .algorithms import MSTRouter

        mst_router = MSTRouter(
            self._grid, self._router, self.rules, self.net_class_map
        )

        for net_id in internal_nets:
            pad_keys = self._nets.get(net_id, [])
            if len(pad_keys) < 2:
                continue

            pad_objs = [self._pads[k] for k in pad_keys if k in self._pads]
            if len(pad_objs) < 2:
                continue

            net_name = self._net_names.get(net_id, f"Net {net_id}")
            logger.debug(
                "Block '%s': routing net '%s' (%d pads)",
                self.block.block_id,
                net_name,
                len(pad_objs),
            )

            try:
                def _mark_on_subgrid(route: Route) -> None:
                    assert self._grid is not None
                    self._grid.mark_route(route)

                routes = mst_router.route_net(
                    pad_objs, _mark_on_subgrid
                )
                if routes:
                    result.routes.extend(routes)
                    result.routed_nets.add(net_id)
                    for key in pad_keys:
                        result.connected_pad_keys.add(key)
                    logger.debug(
                        "Block '%s': net '%s' routed (%d segments)",
                        self.block.block_id,
                        net_name,
                        sum(len(r.segments) for r in routes),
                    )
                else:
                    result.failed_nets.add(net_id)
                    logger.warning(
                        "Block '%s': net '%s' failed to route",
                        self.block.block_id,
                        net_name,
                    )
            except Exception:
                result.failed_nets.add(net_id)
                logger.warning(
                    "Block '%s': net '%s' routing raised exception",
                    self.block.block_id,
                    net_name,
                    exc_info=True,
                )

        logger.info(
            "Block '%s': routed %d/%d internal nets (%d failed)",
            self.block.block_id,
            len(result.routed_nets),
            len(internal_nets),
            len(result.failed_nets),
        )

        return result

    def contains_point(self, x: float, y: float) -> bool:
        """Check whether a board-space point falls inside this block's sub-grid.

        Args:
            x: X coordinate in mm (board space).
            y: Y coordinate in mm (board space).

        Returns:
            True if the point is inside the block's bounding box (with margin).
        """
        return (
            self._abs_min_x <= x <= self._abs_max_x
            and self._abs_min_y <= y <= self._abs_max_y
        )


__all__ = [
    "BlockRouter",
    "BlockRoutingResult",
]
