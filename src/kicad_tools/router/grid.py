"""
Routing grid for PCB autorouting.

This module provides:
- RoutingGrid: 3D grid for routing with obstacle tracking and congestion awareness
"""

from typing import Dict, List, Optional, Set, Tuple

from .layers import Layer, LayerStack
from .primitives import GridCell, Obstacle, Pad, Route, Segment, Via
from .rules import DesignRules


class RoutingGrid:
    """3D grid for routing with obstacle tracking and congestion awareness."""

    def __init__(
        self,
        width: float,
        height: float,
        rules: DesignRules,
        origin_x: float = 0,
        origin_y: float = 0,
        layer_stack: Optional[LayerStack] = None,
    ):
        self.width = width
        self.height = height
        self.rules = rules
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.resolution = rules.grid_resolution

        # Layer stack (default to 2-layer for backward compatibility)
        self.layer_stack = layer_stack or LayerStack.two_layer()
        self.num_layers = self.layer_stack.num_layers

        # Build layer enum to grid index mapping
        self._layer_to_index: Dict[int, int] = {}
        self._index_to_layer: Dict[int, int] = {}
        for layer_def in self.layer_stack.layers:
            for layer_enum in Layer:
                if layer_enum.kicad_name == layer_def.name:
                    self._layer_to_index[layer_enum.value] = layer_def.index
                    self._index_to_layer[layer_def.index] = layer_enum.value
                    break

        # Grid dimensions
        self.cols = int(width / self.resolution) + 1
        self.rows = int(height / self.resolution) + 1

        # 3D grid: [layer][y][x]
        self.grid: List[List[List[GridCell]]] = [
            [[GridCell(x, y, layer) for x in range(self.cols)] for y in range(self.rows)]
            for layer in range(self.num_layers)
        ]

        # Congestion tracking: coarser grid for density
        self.congestion_size = rules.congestion_grid_size
        self.congestion_cols = max(1, self.cols // self.congestion_size)
        self.congestion_rows = max(1, self.rows // self.congestion_size)

        # Congestion counts: [layer][cy][cx] = number of blocked cells
        self.congestion: List[List[List[int]]] = [
            [[0 for _ in range(self.congestion_cols)] for _ in range(self.congestion_rows)]
            for _ in range(self.num_layers)
        ]

        # Track placed routes for net assignment
        self.routes: List[Route] = []

        # Alias for backward compatibility
        self.layers = self.num_layers

    def layer_to_index(self, layer_enum_value: int) -> int:
        """Map Layer enum value to grid index."""
        if layer_enum_value in self._layer_to_index:
            return self._layer_to_index[layer_enum_value]
        raise ValueError(f"Layer value {layer_enum_value} not in stack")

    def index_to_layer(self, index: int) -> int:
        """Map grid index to Layer enum value."""
        if index in self._index_to_layer:
            return self._index_to_layer[index]
        raise ValueError(f"Grid index {index} not in stack")

    def get_routable_indices(self) -> List[int]:
        """Get grid indices of routable signal layers."""
        return self.layer_stack.get_routable_indices()

    def is_plane_layer(self, index: int) -> bool:
        """Check if grid index is a plane layer (no routing)."""
        return self.layer_stack.is_plane_layer(index)

    def _update_congestion(self, gx: int, gy: int, layer: int, delta: int = 1) -> None:
        """Update congestion count for the region containing (gx, gy)."""
        cx = min(gx // self.congestion_size, self.congestion_cols - 1)
        cy = min(gy // self.congestion_size, self.congestion_rows - 1)
        self.congestion[layer][cy][cx] += delta

    def get_congestion(self, gx: int, gy: int, layer: int) -> float:
        """Get congestion level [0, 1] for a grid cell's region."""
        cx = min(gx // self.congestion_size, self.congestion_cols - 1)
        cy = min(gy // self.congestion_size, self.congestion_rows - 1)
        count = self.congestion[layer][cy][cx]
        max_cells = self.congestion_size * self.congestion_size
        return min(1.0, count / max_cells)

    def get_congestion_map(self) -> Dict[str, float]:
        """Get congestion statistics for all regions."""
        stats: Dict[str, float] = {
            "max_congestion": 0.0,
            "avg_congestion": 0.0,
            "congested_regions": 0,
        }

        total = 0.0
        count = 0
        max_cells = self.congestion_size * self.congestion_size

        for layer in range(self.layers):
            for cy in range(self.congestion_rows):
                for cx in range(self.congestion_cols):
                    density = self.congestion[layer][cy][cx] / max_cells
                    total += density
                    count += 1
                    stats["max_congestion"] = max(stats["max_congestion"], density)
                    if density > self.rules.congestion_threshold:
                        stats["congested_regions"] += 1

        stats["avg_congestion"] = total / count if count > 0 else 0.0
        return stats

    def world_to_grid(self, x: float, y: float) -> Tuple[int, int]:
        """Convert world coordinates to grid indices."""
        gx = int((x - self.origin_x) / self.resolution)
        gy = int((y - self.origin_y) / self.resolution)
        return (max(0, min(gx, self.cols - 1)), max(0, min(gy, self.rows - 1)))

    def grid_to_world(self, gx: int, gy: int) -> Tuple[float, float]:
        """Convert grid indices to world coordinates."""
        return (
            self.origin_x + gx * self.resolution,
            self.origin_y + gy * self.resolution,
        )

    def add_obstacle(self, obs: Obstacle) -> None:
        """Mark grid cells as blocked by an obstacle."""
        clearance = obs.clearance + self.rules.trace_clearance

        # Calculate affected grid region
        x1 = obs.x - obs.width / 2 - clearance
        y1 = obs.y - obs.height / 2 - clearance
        x2 = obs.x + obs.width / 2 + clearance
        y2 = obs.y + obs.height / 2 + clearance

        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        layer_idx = self.layer_to_index(obs.layer.value)

        for gy in range(gy1, gy2 + 1):
            for gx in range(gx1, gx2 + 1):
                if 0 <= gx < self.cols and 0 <= gy < self.rows:
                    self.grid[layer_idx][gy][gx].blocked = True

    def add_pad(self, pad: Pad) -> None:
        """Add a pad as an obstacle (except for its own net)."""
        # Grid quantization margin
        grid_margin = self.resolution * 0.71  # sqrt(2)/2

        # Clearance model
        clearance = grid_margin + self.rules.trace_width / 2 + self.rules.trace_clearance

        if pad.through_hole:
            if pad.width > 0 and pad.height > 0:
                effective_width = pad.width
                effective_height = pad.height
            elif pad.drill > 0:
                effective_width = pad.drill + 0.7
                effective_height = effective_width
            else:
                effective_width = 1.7
                effective_height = 1.7
        else:
            effective_width = pad.width
            effective_height = pad.height

        x1 = pad.x - effective_width / 2 - clearance
        y1 = pad.y - effective_height / 2 - clearance
        x2 = pad.x + effective_width / 2 + clearance
        y2 = pad.y + effective_height / 2 + clearance

        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        # PTH pads block all layers, SMD pads block only their layer
        if pad.through_hole:
            layers_to_block = list(range(self.num_layers))
        else:
            layers_to_block = [self.layer_to_index(pad.layer.value)]

        # Get center cell coordinates
        center_gx, center_gy = self.world_to_grid(pad.x, pad.y)

        # Calculate pad metal area bounds (without clearance)
        metal_x1 = pad.x - effective_width / 2
        metal_y1 = pad.y - effective_height / 2
        metal_x2 = pad.x + effective_width / 2
        metal_y2 = pad.y + effective_height / 2
        metal_gx1, metal_gy1 = self.world_to_grid(metal_x1, metal_y1)
        metal_gx2, metal_gy2 = self.world_to_grid(metal_x2, metal_y2)

        for layer_idx in layers_to_block:
            for gy in range(gy1, gy2 + 1):
                for gx in range(gx1, gx2 + 1):
                    if 0 <= gx < self.cols and 0 <= gy < self.rows:
                        cell = self.grid[layer_idx][gy][gx]
                        cell.blocked = True

                        is_metal_area = (
                            metal_gx1 <= gx <= metal_gx2 and metal_gy1 <= gy <= metal_gy2
                        )

                        if is_metal_area:
                            if cell.net == 0:
                                cell.net = pad.net
                            elif cell.net != pad.net and pad.net != 0:
                                cell.is_obstacle = True
                        else:
                            if pad.net == 0:
                                if cell.net != 0:
                                    cell.is_obstacle = True
                            elif cell.net == 0:
                                cell.net = pad.net
                            elif cell.net != pad.net:
                                cell.is_obstacle = True

            # Always mark the center cell with this pad's net
            if 0 <= center_gx < self.cols and 0 <= center_gy < self.rows:
                self.grid[layer_idx][center_gy][center_gx].net = pad.net

    def add_keepout(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layers: Optional[List[Layer]] = None,
    ) -> None:
        """Add a keepout region."""
        if layers is None:
            layer_indices = self.get_routable_indices()
        else:
            layer_indices = [self.layer_to_index(layer.value) for layer in layers]

        gx1, gy1 = self.world_to_grid(x1, y1)
        gx2, gy2 = self.world_to_grid(x2, y2)

        for layer_idx in layer_indices:
            for gy in range(gy1, gy2 + 1):
                for gx in range(gx1, gx2 + 1):
                    if 0 <= gx < self.cols and 0 <= gy < self.rows:
                        self.grid[layer_idx][gy][gx].blocked = True

    def is_blocked(self, gx: int, gy: int, layer: Layer, net: int = 0) -> bool:
        """Check if a cell is blocked for routing."""
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return True
        layer_idx = self.layer_to_index(layer.value)
        cell = self.grid[layer_idx][gy][gx]
        if cell.blocked:
            return cell.net == 0 or cell.net != net
        return False

    def mark_route(self, route: Route) -> None:
        """Mark a route's cells as used."""
        total_clearance = self.rules.trace_width / 2 + self.rules.trace_clearance
        clearance_cells = int(total_clearance / self.resolution) + 1

        for seg in route.segments:
            self._mark_segment(seg, clearance_cells=clearance_cells)
        for via in route.vias:
            self._mark_via(via)
        self.routes.append(route)

    def _mark_segment(self, seg: Segment, clearance_cells: int = 1) -> None:
        """Mark cells along a segment as blocked (with clearance buffer)."""
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)

        layer_idx = self.layer_to_index(seg.layer.value)
        marked_cells: Set[Tuple[int, int]] = set()

        def mark_with_clearance(gx: int, gy: int) -> None:
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if not cell.blocked:
                            marked_cells.add((nx, ny))
                        cell.blocked = True
                        cell.net = seg.net

        # Simple line marking
        if gx1 == gx2:  # Vertical
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                mark_with_clearance(gx1, gy)
        elif gy1 == gy2:  # Horizontal
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                mark_with_clearance(gx, gy1)
        else:  # Diagonal - use Bresenham
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                mark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

        # Update congestion for all newly marked cells
        for nx, ny in marked_cells:
            self._update_congestion(nx, ny, layer_idx)

    def _mark_via(self, via: Via) -> None:
        """Mark cells around a via as blocked on ALL layers (through-hole via)."""
        gx, gy = self.world_to_grid(via.x, via.y)
        radius = int((via.diameter / 2 + self.rules.via_clearance) / self.resolution)

        for layer_idx in range(self.num_layers):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if not cell.blocked:
                            self._update_congestion(nx, ny, layer_idx)
                        cell.blocked = True
                        cell.net = via.net

    def unmark_route(self, route: Route) -> None:
        """Unmark a route's cells (rip-up). Reverses mark_route()."""
        total_clearance = self.rules.trace_width / 2 + self.rules.trace_clearance
        clearance_cells = int(total_clearance / self.resolution) + 1

        for seg in route.segments:
            self._unmark_segment(seg, clearance_cells=clearance_cells)
        for via in route.vias:
            self._unmark_via(via)

        if route in self.routes:
            self.routes.remove(route)

    def _unmark_segment(self, seg: Segment, clearance_cells: int = 1) -> None:
        """Unmark cells along a segment (clear blocked status and net)."""
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)

        layer_idx = self.layer_to_index(seg.layer.value)

        def unmark_with_clearance(gx: int, gy: int) -> None:
            for dy in range(-clearance_cells, clearance_cells + 1):
                for dx in range(-clearance_cells, clearance_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if cell.net == seg.net:
                            cell.blocked = False
                            cell.net = 0

        if gx1 == gx2:
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                unmark_with_clearance(gx1, gy)
        elif gy1 == gy2:
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                unmark_with_clearance(gx, gy1)
        else:
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                unmark_with_clearance(gx, gy)
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy

    def _unmark_via(self, via: Via) -> None:
        """Unmark cells around a via on ALL layers."""
        gx, gy = self.world_to_grid(via.x, via.y)
        radius = int((via.diameter / 2 + self.rules.via_clearance) / self.resolution)

        for layer_idx in range(self.num_layers):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= nx < self.cols and 0 <= ny < self.rows:
                        cell = self.grid[layer_idx][ny][nx]
                        if cell.net == via.net:
                            cell.blocked = False
                            cell.net = 0

    # =========================================================================
    # NEGOTIATED CONGESTION ROUTING SUPPORT
    # =========================================================================

    def reset_route_usage(self) -> None:
        """Reset all usage counts (start of new negotiation iteration)."""
        for layer_idx in range(self.layers):
            for gy in range(self.rows):
                for gx in range(self.cols):
                    self.grid[layer_idx][gy][gx].usage_count = 0

    def mark_route_usage(
        self, route: Route, net_cells: Optional[Dict[int, Set]] = None
    ) -> Set[Tuple[int, int, int]]:
        """Mark cells used by a route, incrementing usage count."""
        cells_used: Set[Tuple[int, int, int]] = set()

        for seg in route.segments:
            seg_cells = self._get_segment_cells(seg)
            cells_used.update(seg_cells)

        for via in route.vias:
            via_cells = self._get_via_cells(via)
            cells_used.update(via_cells)

        for gx, gy, layer_idx in cells_used:
            if 0 <= gx < self.cols and 0 <= gy < self.rows:
                self.grid[layer_idx][gy][gx].usage_count += 1

        if net_cells is not None:
            if route.net not in net_cells:
                net_cells[route.net] = set()
            net_cells[route.net].update(cells_used)

        return cells_used

    def unmark_route_usage(self, route: Route, net_cells: Optional[Dict[int, Set]] = None) -> None:
        """Remove a route's usage (rip-up), decrementing usage count."""
        cells_used: Set[Tuple[int, int, int]] = set()

        for seg in route.segments:
            seg_cells = self._get_segment_cells(seg)
            cells_used.update(seg_cells)

        for via in route.vias:
            via_cells = self._get_via_cells(via)
            cells_used.update(via_cells)

        for gx, gy, layer_idx in cells_used:
            if 0 <= gx < self.cols and 0 <= gy < self.rows:
                cell = self.grid[layer_idx][gy][gx]
                cell.usage_count = max(0, cell.usage_count - 1)

        if net_cells is not None and route.net in net_cells:
            net_cells[route.net] -= cells_used

    def _get_segment_cells(self, seg: Segment) -> Set[Tuple[int, int, int]]:
        """Get all grid cells occupied by a segment."""
        cells: Set[Tuple[int, int, int]] = set()
        gx1, gy1 = self.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = self.world_to_grid(seg.x2, seg.y2)
        layer_idx = self.layer_to_index(seg.layer.value)

        if gx1 == gx2:
            for gy in range(min(gy1, gy2), max(gy1, gy2) + 1):
                cells.add((gx1, gy, layer_idx))
        elif gy1 == gy2:
            for gx in range(min(gx1, gx2), max(gx1, gx2) + 1):
                cells.add((gx, gy1, layer_idx))
        else:
            dx = abs(gx2 - gx1)
            dy = abs(gy2 - gy1)
            sx = 1 if gx1 < gx2 else -1
            sy = 1 if gy1 < gy2 else -1
            err = dx - dy
            gx, gy = gx1, gy1
            while True:
                cells.add((gx, gy, layer_idx))
                if gx == gx2 and gy == gy2:
                    break
                e2 = 2 * err
                if e2 > -dy:
                    err -= dy
                    gx += sx
                if e2 < dx:
                    err += dx
                    gy += sy
        return cells

    def _get_via_cells(self, via: Via) -> Set[Tuple[int, int, int]]:
        """Get all grid cells occupied by a via (all layers for through-hole)."""
        cells: Set[Tuple[int, int, int]] = set()
        gx, gy = self.world_to_grid(via.x, via.y)
        for layer_idx in range(self.num_layers):
            cells.add((gx, gy, layer_idx))
        return cells

    def find_overused_cells(self) -> List[Tuple[int, int, int, int]]:
        """Find cells with usage_count > 1 (resource conflicts)."""
        overused = []
        for layer_idx in range(self.layers):
            for gy in range(self.rows):
                for gx in range(self.cols):
                    cell = self.grid[layer_idx][gy][gx]
                    if cell.usage_count > 1:
                        overused.append((gx, gy, layer_idx, cell.usage_count))
        return overused

    def update_history_costs(self, history_increment: float = 1.0) -> None:
        """Increase history cost for overused cells (PathFinder-style)."""
        for layer_idx in range(self.layers):
            for gy in range(self.rows):
                for gx in range(self.cols):
                    cell = self.grid[layer_idx][gy][gx]
                    if cell.usage_count > 1:
                        cell.history_cost += history_increment * (cell.usage_count - 1)

    def get_negotiated_cost(
        self, gx: int, gy: int, layer: int, present_cost_factor: float = 1.0
    ) -> float:
        """Get the negotiated congestion cost for a cell."""
        if not (0 <= gx < self.cols and 0 <= gy < self.rows):
            return float("inf")

        cell = self.grid[layer][gy][gx]

        if cell.is_obstacle:
            return float("inf")

        present_cost = present_cost_factor * cell.usage_count
        history_cost = cell.history_cost

        return present_cost + history_cost

    def get_total_overflow(self) -> int:
        """Get total overflow (sum of usage_count - 1 for overused cells)."""
        overflow = 0
        for layer_idx in range(self.layers):
            for gy in range(self.rows):
                for gx in range(self.cols):
                    usage = self.grid[layer_idx][gy][gx].usage_count
                    if usage > 1:
                        overflow += usage - 1
        return overflow
