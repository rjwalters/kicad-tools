"""
Zone flood fill algorithm for copper pour support.

This module provides:
- FilledZone: Result of zone fill with cell coordinates
- ZoneFiller: Grid-based flood fill for zone polygons
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from kicad_tools.schema.pcb import Zone

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .rules import DesignRules


@dataclass
class FilledZone:
    """Result of filling a zone with grid cells.

    Attributes:
        zone: The original zone definition
        filled_cells: Set of (gx, gy) grid coordinates that are filled
        layer_index: Grid layer index for this zone
    """

    zone: Zone
    filled_cells: Set[Tuple[int, int]] = field(default_factory=set)
    layer_index: int = 0


class ZoneFiller:
    """Grid-based flood fill algorithm for zone polygons.

    Fills zone boundaries onto the routing grid, respecting obstacles
    and applying clearances for different-net elements.

    The algorithm:
    1. For each grid cell in the zone's bounding box
    2. Test if the cell center is inside the polygon (ray casting)
    3. If inside, check for obstacles:
       - Same-net pads/traces: no clearance (direct connection)
       - Other-net pads/traces: apply clearance (antipad)
    4. Mark valid cells as zone copper
    """

    def __init__(self, grid: "RoutingGrid", rules: "DesignRules"):
        """Initialize zone filler.

        Args:
            grid: The routing grid to fill zones onto
            rules: Design rules for clearances
        """
        self.grid = grid
        self.rules = rules

    def fill_zone(self, zone: Zone, layer_index: int) -> FilledZone:
        """Fill a zone's polygon onto the grid.

        Args:
            zone: Zone with polygon boundary to fill
            layer_index: Grid layer index to fill on

        Returns:
            FilledZone with set of filled grid cells
        """
        result = FilledZone(zone=zone, layer_index=layer_index)

        if not zone.polygon or len(zone.polygon) < 3:
            return result

        # Get bounding box in grid coordinates
        min_x = min(p[0] for p in zone.polygon)
        max_x = max(p[0] for p in zone.polygon)
        min_y = min(p[1] for p in zone.polygon)
        max_y = max(p[1] for p in zone.polygon)

        gx_min, gy_min = self.grid.world_to_grid(min_x, min_y)
        gx_max, gy_max = self.grid.world_to_grid(max_x, max_y)

        # Clamp to grid bounds
        gx_min = max(0, gx_min)
        gy_min = max(0, gy_min)
        gx_max = min(self.grid.cols - 1, gx_max)
        gy_max = min(self.grid.rows - 1, gy_max)

        # Scanline fill: iterate through all cells in bounding box
        for gy in range(gy_min, gy_max + 1):
            for gx in range(gx_min, gx_max + 1):
                # Get world coordinates of cell center
                wx, wy = self.grid.grid_to_world(gx, gy)

                # Test if point is inside polygon
                if not self.point_in_polygon(wx, wy, zone.polygon):
                    continue

                # Check if cell should be filled (not blocked by other-net obstacle)
                if self._can_fill_cell(gx, gy, layer_index, zone.net_number):
                    result.filled_cells.add((gx, gy))

        return result

    def point_in_polygon(
        self, x: float, y: float, polygon: List[Tuple[float, float]]
    ) -> bool:
        """Test if a point is inside a polygon using ray casting.

        Casts a ray from the point to the right (+X) and counts
        intersections with polygon edges. Odd count = inside.

        Args:
            x, y: Point to test
            polygon: List of (x, y) vertices

        Returns:
            True if point is inside polygon
        """
        n = len(polygon)
        if n < 3:
            return False

        inside = False

        j = n - 1
        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]

            # Check if ray crosses this edge
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside

            j = i

        return inside

    def _can_fill_cell(
        self, gx: int, gy: int, layer_index: int, zone_net: int
    ) -> bool:
        """Check if a cell can be filled with zone copper.

        Rules:
        - Empty cells: fill
        - Same-net cells: fill (direct connection)
        - Other-net cells with clearance: don't fill
        - Obstacle cells: check if same net

        Args:
            gx, gy: Grid coordinates
            layer_index: Grid layer
            zone_net: Net number of the zone

        Returns:
            True if cell can be filled
        """
        cell = self.grid.grid[layer_index][gy][gx]

        # If cell is an obstacle (pad center), only fill if same net
        if cell.is_obstacle:
            return cell.net == zone_net

        # If cell is blocked by another net, don't fill
        if cell.blocked and cell.net != 0 and cell.net != zone_net:
            return False

        # Empty or same-net: can fill
        return True

    def fill_zone_with_clearance(
        self, zone: Zone, layer_index: int, clearance: Optional[float] = None
    ) -> FilledZone:
        """Fill zone with clearance around other-net obstacles.

        This is a more sophisticated fill that:
        1. First does a basic fill
        2. Then removes cells within clearance of other-net obstacles

        Args:
            zone: Zone to fill
            layer_index: Grid layer index
            clearance: Clearance distance in mm (defaults to zone.clearance)

        Returns:
            FilledZone with cells respecting clearances
        """
        # Start with basic fill
        result = self.fill_zone(zone, layer_index)

        if not result.filled_cells:
            return result

        # Use zone's clearance or provided value
        clear_dist = clearance if clearance is not None else zone.clearance
        clear_cells = int(clear_dist / self.grid.resolution) + 1

        # Find cells to remove (within clearance of other-net obstacles)
        cells_to_remove: Set[Tuple[int, int]] = set()

        for gx, gy in result.filled_cells:
            # Check nearby cells for other-net obstacles
            for dy in range(-clear_cells, clear_cells + 1):
                for dx in range(-clear_cells, clear_cells + 1):
                    nx, ny = gx + dx, gy + dy
                    if not (0 <= nx < self.grid.cols and 0 <= ny < self.grid.rows):
                        continue

                    neighbor = self.grid.grid[layer_index][ny][nx]

                    # If neighbor is blocked by different net, this cell needs clearance
                    if neighbor.blocked and neighbor.net != 0 and neighbor.net != zone.net_number:
                        # Check actual distance
                        dist_sq = dx * dx + dy * dy
                        if dist_sq <= clear_cells * clear_cells:
                            cells_to_remove.add((gx, gy))
                            break
                else:
                    continue
                break

        # Remove cells that violate clearance
        result.filled_cells -= cells_to_remove

        return result


def fill_zones_by_priority(
    zones: List[Zone],
    grid: "RoutingGrid",
    rules: "DesignRules",
    layer_map: dict,
) -> List[FilledZone]:
    """Fill multiple zones in priority order.

    Higher priority zones fill first and take precedence.

    Args:
        zones: List of zones to fill
        grid: Routing grid
        rules: Design rules
        layer_map: Mapping from layer name to grid index

    Returns:
        List of FilledZone results
    """
    filler = ZoneFiller(grid, rules)

    # Sort by priority (higher priority = lower number = first)
    sorted_zones = sorted(zones, key=lambda z: z.priority)

    results: List[FilledZone] = []

    for zone in sorted_zones:
        # Get layer index
        if zone.layer not in layer_map:
            continue

        layer_index = layer_map[zone.layer]

        # Fill with clearance
        filled = filler.fill_zone_with_clearance(zone, layer_index)
        results.append(filled)

        # Mark filled cells in grid (so subsequent zones see them as obstacles)
        grid.add_zone_cells(zone, filled.filled_cells, layer_index)

    return results
