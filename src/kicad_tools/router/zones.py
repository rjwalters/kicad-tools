"""
Zone flood fill algorithm for copper pour support.

This module provides:
- FilledZone: Result of zone fill with cell coordinates
- ZoneFiller: Grid-based flood fill for zone polygons
- ThermalRelief: Thermal relief pattern for pad-to-zone connections
- ConnectionType: Enum for pad connection types
- ZoneManager: High-level zone management for Autorouter integration
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from kicad_tools.schema.pcb import Zone

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

    from .grid import RoutingGrid
    from .primitives import Pad
    from .rules import DesignRules


class ConnectionType(Enum):
    """Type of connection between pad and zone copper."""

    THERMAL = "thermal"  # Thermal relief with spokes
    SOLID = "solid"  # Direct solid connection
    NONE = "none"  # No connection (antipad only)


@dataclass
class FilledZone:
    """Result of filling a zone with grid cells.

    Attributes:
        zone: The original zone definition
        filled_cells: Set of (gx, gy) grid coordinates that are filled
        layer_index: Grid layer index for this zone
    """

    zone: Zone
    filled_cells: set[tuple[int, int]] = field(default_factory=set)
    layer_index: int = 0


@dataclass
class ThermalRelief:
    """Thermal relief pattern for connecting a pad to zone copper.

    Thermal reliefs provide electrical connection while limiting heat
    transfer, making hand soldering possible. They consist of:
    - An antipad (clearance ring) around the pad
    - Spokes connecting the pad to surrounding zone copper

    Attributes:
        pad: The pad being connected
        zone: The zone providing copper
        gap: Clearance distance from pad edge to zone copper (mm)
        spoke_width: Width of connecting spokes (mm)
        spoke_count: Number of spokes (typically 2 or 4)
        spoke_angle: Rotation of spoke pattern in degrees (0 or 45)
        layer_index: Grid layer index
    """

    pad: "Pad"
    zone: Zone
    gap: float
    spoke_width: float
    spoke_count: int = 4
    spoke_angle: float = 45.0
    layer_index: int = 0

    def generate_antipad_cells(self, grid: "RoutingGrid") -> set[tuple[int, int]]:
        """Generate grid cells forming the antipad (clearance ring).

        The antipad is the area around the pad that must remain copper-free
        except for the spoke connections.

        Args:
            grid: Routing grid for coordinate conversion

        Returns:
            Set of (gx, gy) grid cells in the antipad region
        """
        cells: set[tuple[int, int]] = set()

        # Pad dimensions with gap
        pad_half_w = self.pad.width / 2
        pad_half_h = self.pad.height / 2
        outer_radius = max(pad_half_w, pad_half_h) + self.gap

        # Convert to grid cells
        outer_cells = int(outer_radius / grid.resolution) + 1

        # Get pad center in grid coordinates
        pad_gx, pad_gy = grid.world_to_grid(self.pad.x, self.pad.y)

        # Generate antipad ring (cells outside pad but within gap)
        for dy in range(-outer_cells, outer_cells + 1):
            for dx in range(-outer_cells, outer_cells + 1):
                gx, gy = pad_gx + dx, pad_gy + dy

                # Skip cells outside grid
                if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                    continue

                # Get world position of cell
                wx, wy = grid.grid_to_world(gx, gy)

                # Distance from pad center
                rel_x = wx - self.pad.x
                rel_y = wy - self.pad.y

                # Check if in antipad region (outside pad, inside outer boundary)
                # Use rectangular check for pad shape
                in_pad = abs(rel_x) <= pad_half_w and abs(rel_y) <= pad_half_h

                # Use circular check for outer boundary
                dist = math.sqrt(rel_x * rel_x + rel_y * rel_y)
                in_outer = dist <= outer_radius

                if not in_pad and in_outer:
                    cells.add((gx, gy))

        return cells

    def generate_spoke_cells(self, grid: "RoutingGrid") -> set[tuple[int, int]]:
        """Generate grid cells forming the connecting spokes.

        Spokes are narrow bridges of copper connecting the pad to
        the surrounding zone copper through the antipad.

        Args:
            grid: Routing grid for coordinate conversion

        Returns:
            Set of (gx, gy) grid cells forming the spokes
        """
        cells: set[tuple[int, int]] = set()

        # Pad dimensions
        pad_half_w = self.pad.width / 2
        pad_half_h = self.pad.height / 2
        outer_radius = max(pad_half_w, pad_half_h) + self.gap

        # Spoke parameters
        spoke_half_width = self.spoke_width / 2
        angle_step = 360.0 / self.spoke_count

        # Get pad center in grid coordinates
        pad_gx, pad_gy = grid.world_to_grid(self.pad.x, self.pad.y)

        # Grid range to check
        outer_cells = int(outer_radius / grid.resolution) + 2

        for dy in range(-outer_cells, outer_cells + 1):
            for dx in range(-outer_cells, outer_cells + 1):
                gx, gy = pad_gx + dx, pad_gy + dy

                # Skip cells outside grid
                if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                    continue

                # Get world position of cell
                wx, wy = grid.grid_to_world(gx, gy)

                # Position relative to pad center
                rel_x = wx - self.pad.x
                rel_y = wy - self.pad.y

                # Check if cell is in the antipad region
                in_pad = abs(rel_x) <= pad_half_w and abs(rel_y) <= pad_half_h
                dist = math.sqrt(rel_x * rel_x + rel_y * rel_y)
                in_outer = dist <= outer_radius

                if in_pad or not in_outer:
                    continue  # Only care about antipad region

                # Check if cell falls within any spoke
                cell_angle = math.degrees(math.atan2(rel_y, rel_x))
                if cell_angle < 0:
                    cell_angle += 360

                for i in range(self.spoke_count):
                    spoke_angle = self.spoke_angle + i * angle_step
                    spoke_angle = spoke_angle % 360

                    # Angular distance to spoke center
                    angle_diff = abs(cell_angle - spoke_angle)
                    if angle_diff > 180:
                        angle_diff = 360 - angle_diff

                    # Convert angular width to linear at this distance
                    if dist > 0:
                        # Spoke width check: perpendicular distance from spoke line
                        spoke_rad = math.radians(spoke_angle)
                        # Project onto perpendicular to spoke direction
                        perp_dist = abs(rel_x * math.sin(spoke_rad) - rel_y * math.cos(spoke_rad))
                        if perp_dist <= spoke_half_width:
                            cells.add((gx, gy))
                            break

        return cells


def get_connection_type(pad: "Pad", zone: Zone) -> ConnectionType:
    """Determine connection type for a pad in a zone.

    Rules:
    - PTH (through-hole) pads always get thermal relief
    - SMD pads follow zone's connect_pads setting

    Args:
        pad: Pad to check
        zone: Zone the pad connects to

    Returns:
        ConnectionType for this pad-zone pair
    """
    # PTH pads always get thermal relief for solderability
    if pad.through_hole:
        return ConnectionType.THERMAL

    # SMD pads follow zone setting
    if zone.connect_pads == "solid":
        return ConnectionType.SOLID
    elif zone.connect_pads == "none":
        return ConnectionType.NONE
    else:
        # Default: thermal_reliefs
        return ConnectionType.THERMAL


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

    def point_in_polygon(self, x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
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

    def _can_fill_cell(self, gx: int, gy: int, layer_index: int, zone_net: int) -> bool:
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
        self, zone: Zone, layer_index: int, clearance: float | None = None
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
        cells_to_remove: set[tuple[int, int]] = set()

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

    def generate_thermal_reliefs(
        self, filled_zone: FilledZone, pads: list["Pad"]
    ) -> list[ThermalRelief]:
        """Generate thermal relief patterns for same-net pads in a zone.

        Finds all pads that are:
        1. On the same net as the zone
        2. Within the zone boundary
        3. Require thermal relief (based on connection type)

        Args:
            filled_zone: A filled zone to add thermal reliefs to
            pads: List of all pads to check

        Returns:
            List of ThermalRelief objects for applicable pads
        """
        from .primitives import Pad as PadType  # noqa: F401

        reliefs: list[ThermalRelief] = []
        zone = filled_zone.zone

        for pad in pads:
            # Must be same net
            if pad.net != zone.net_number:
                continue

            # Must be within zone polygon
            if zone.polygon and not self.point_in_polygon(pad.x, pad.y, zone.polygon):
                continue

            # Determine connection type
            conn_type = get_connection_type(pad, zone)

            if conn_type == ConnectionType.THERMAL:
                relief = ThermalRelief(
                    pad=pad,
                    zone=zone,
                    gap=zone.thermal_gap,
                    spoke_width=zone.thermal_bridge_width,
                    spoke_count=4,  # Standard 4-spoke
                    spoke_angle=45.0,  # Standard 45° rotation
                    layer_index=filled_zone.layer_index,
                )
                reliefs.append(relief)

        return reliefs

    def apply_thermal_reliefs(self, filled_zone: FilledZone, reliefs: list[ThermalRelief]) -> None:
        """Apply thermal relief patterns to a filled zone.

        This modifies the filled_cells set:
        1. Removes antipad cells (clearance around pads)
        2. Adds spoke cells (connections through antipad)

        Args:
            filled_zone: Zone to modify (mutated in place)
            reliefs: Thermal relief patterns to apply
        """
        for relief in reliefs:
            # Remove antipad cells from zone fill
            antipad_cells = relief.generate_antipad_cells(self.grid)
            filled_zone.filled_cells -= antipad_cells

            # Add spoke cells back
            spoke_cells = relief.generate_spoke_cells(self.grid)
            filled_zone.filled_cells |= spoke_cells


def fill_zones_by_priority(
    zones: list[Zone],
    grid: "RoutingGrid",
    rules: "DesignRules",
    layer_map: dict,
) -> list[FilledZone]:
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

    results: list[FilledZone] = []

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


class ZoneManager:
    """High-level zone management for the Autorouter.

    Orchestrates the zone fill process:
    1. Load zones from PCB or manual input
    2. Sort zones by priority
    3. Fill zones onto the routing grid
    4. Generate and apply thermal reliefs
    5. Make zone copper available for routing decisions

    The ZoneManager works with the Autorouter to enable zone-aware routing
    where routes can pass through same-net zones with reduced cost and
    are blocked by other-net zones.

    Example:
        grid = RoutingGrid(100, 80, rules)
        zone_manager = ZoneManager(grid, rules)

        # Load zones from PCB
        zones = zone_manager.load_zones(pcb)

        # Fill all zones
        filled_zones = zone_manager.fill_all_zones(zones, pads)

        # Zones are now marked on grid, routing can proceed
        router = Router(grid, rules)
        route = router.route(start_pad, end_pad)  # Zone-aware
    """

    def __init__(self, grid: "RoutingGrid", rules: "DesignRules"):
        """Initialize zone manager.

        Args:
            grid: The routing grid to fill zones onto
            rules: Design rules including zone-specific parameters
        """
        self.grid = grid
        self.rules = rules
        self.filler = ZoneFiller(grid, rules)
        self.filled_zones: list[FilledZone] = []
        self._layer_map: dict = {}

    def _build_layer_map(self) -> dict:
        """Build mapping from KiCad layer names to grid indices."""
        if self._layer_map:
            return self._layer_map

        from .layers import Layer

        layer_map = {}
        for layer in Layer:
            try:
                idx = self.grid.layer_to_index(layer.value)
                layer_map[layer.kicad_name] = idx
            except Exception:
                pass  # Layer not in stack

        self._layer_map = layer_map
        return layer_map

    def load_zones(self, pcb: "PCB") -> list[Zone]:
        """Load and sort zones from a PCB.

        Args:
            pcb: PCB object containing zone definitions

        Returns:
            List of zones sorted by priority (highest first)
        """
        if not hasattr(pcb, "zones") or not pcb.zones:
            return []

        # Sort by priority (lower number = higher priority = fills first)
        return sorted(pcb.zones, key=lambda z: z.priority)

    def fill_zone(self, zone: Zone, pads: list["Pad"] | None = None) -> FilledZone | None:
        """Fill a single zone onto the grid.

        Args:
            zone: Zone to fill
            pads: List of pads for thermal relief generation

        Returns:
            FilledZone result or None if zone couldn't be filled
        """
        layer_map = self._build_layer_map()

        if zone.layer not in layer_map:
            return None

        layer_index = layer_map[zone.layer]

        # Fill zone with clearance
        filled = self.filler.fill_zone_with_clearance(zone, layer_index)

        if not filled.filled_cells:
            return filled

        # Generate thermal reliefs for same-net pads
        if pads:
            reliefs = self.filler.generate_thermal_reliefs(filled, pads)
            if reliefs:
                self.filler.apply_thermal_reliefs(filled, reliefs)

        return filled

    def fill_all_zones(
        self,
        zones: list[Zone],
        pads: list["Pad"] | None = None,
        apply_to_grid: bool = True,
    ) -> list[FilledZone]:
        """Fill all zones in priority order.

        Args:
            zones: List of zones to fill
            pads: List of pads for thermal relief generation
            apply_to_grid: Whether to mark filled cells on the grid

        Returns:
            List of FilledZone results
        """
        layer_map = self._build_layer_map()

        # Sort by priority
        sorted_zones = sorted(zones, key=lambda z: z.priority)

        results: list[FilledZone] = []

        for zone in sorted_zones:
            if zone.layer not in layer_map:
                continue

            layer_index = layer_map[zone.layer]

            # Fill with clearance
            filled = self.filler.fill_zone_with_clearance(zone, layer_index)

            if filled.filled_cells:
                # Generate and apply thermal reliefs
                if pads:
                    reliefs = self.filler.generate_thermal_reliefs(filled, pads)
                    if reliefs:
                        self.filler.apply_thermal_reliefs(filled, reliefs)

                # Mark on grid for subsequent zones and routing
                if apply_to_grid:
                    self.grid.add_zone_cells(zone, filled.filled_cells, layer_index)

            results.append(filled)

        self.filled_zones = results
        return results

    def clear_all_zones(self) -> None:
        """Remove all zone markings from the grid."""
        self.grid.clear_zones()
        self.filled_zones = []

    def get_zone_statistics(self) -> dict:
        """Get statistics about filled zones.

        Returns:
            Dictionary with zone fill statistics
        """
        total_cells = 0
        zone_count = len(self.filled_zones)
        zone_stats = []

        for filled in self.filled_zones:
            cell_count = len(filled.filled_cells)
            total_cells += cell_count
            zone_stats.append(
                {
                    "zone_id": filled.zone.uuid,
                    "net": filled.zone.net_number,
                    "net_name": filled.zone.net_name,
                    "layer": filled.zone.layer,
                    "cells": cell_count,
                    "priority": filled.zone.priority,
                }
            )

        return {
            "zone_count": zone_count,
            "total_cells": total_cells,
            "zones": zone_stats,
        }
