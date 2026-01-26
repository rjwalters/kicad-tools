"""
Sub-grid routing for fine-pitch component pad connections.

This module provides hybrid-grid routing for fine-pitch ICs (TSSOP, SSOP, QFN, etc.)
whose pads don't align to the main routing grid. Instead of using a global fine grid
(which is computationally intractable for large boards), this module creates localized
fine-grid regions around fine-pitch components and generates escape segments that
connect off-grid pads to the nearest main-grid points.

Issue #1109: Router support for fine-pitch components (sub-grid routing)

Strategy:
1. Detect fine-pitch components with off-grid pads
2. For each such pad, compute the nearest reachable main-grid point
3. Generate a short escape segment from exact pad coordinates to the grid point
4. The main router then routes from grid point to grid point as usual

This avoids the O(N^2) cost explosion of a global fine grid while still enabling
routing to pads that fall between grid points.

Example::

    from kicad_tools.router.subgrid import SubGridRouter

    subgrid = SubGridRouter(grid, rules)
    result = subgrid.analyze_pads(pads)

    if result.has_off_grid_pads:
        # Generate escape segments for off-grid pads
        escapes = subgrid.generate_escape_segments(result)
        # Apply escapes to unblock pad grid cells
        subgrid.apply_escape_segments(escapes)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .rules import DesignRules

from .layers import Layer
from .primitives import Pad, Route, Segment

logger = logging.getLogger(__name__)


@dataclass
class SubGridPad:
    """A pad that requires sub-grid routing.

    Attributes:
        pad: The original pad object
        grid_x: Nearest grid X coordinate
        grid_y: Nearest grid Y coordinate
        offset_x: Distance from pad center to nearest grid X
        offset_y: Distance from pad center to nearest grid Y
        snap_x: World coordinate of the grid snap point X
        snap_y: World coordinate of the grid snap point Y
        escape_direction: Direction of escape (outward from component center)
    """

    pad: Pad
    grid_x: int
    grid_y: int
    offset_x: float
    offset_y: float
    snap_x: float
    snap_y: float
    escape_direction: tuple[float, float] = (0.0, 0.0)


@dataclass
class SubGridEscape:
    """An escape segment connecting an off-grid pad to the main routing grid.

    Attributes:
        pad: The pad being escaped
        segment: Trace segment from pad center to grid snap point
        grid_point: The main-grid (gx, gy) where the escape terminates
        snap_point: World coordinates of the grid snap point
    """

    pad: Pad
    segment: Segment
    grid_point: tuple[int, int]
    snap_point: tuple[float, float]


@dataclass
class SubGridAnalysis:
    """Results of sub-grid pad analysis for a set of components.

    Attributes:
        off_grid_pads: Pads requiring sub-grid escape routing
        on_grid_pads: Pads that are already on the main grid
        component_centers: Center position for each component (for escape direction)
        grid_resolution: The main grid resolution used for analysis
        grid_tolerance: Tolerance for considering a pad "on grid"
    """

    off_grid_pads: list[SubGridPad] = field(default_factory=list)
    on_grid_pads: list[Pad] = field(default_factory=list)
    component_centers: dict[str, tuple[float, float]] = field(default_factory=dict)
    grid_resolution: float = 0.0
    grid_tolerance: float = 0.0

    @property
    def has_off_grid_pads(self) -> bool:
        """True if any pads require sub-grid routing."""
        return len(self.off_grid_pads) > 0

    @property
    def off_grid_count(self) -> int:
        """Number of pads requiring sub-grid routing."""
        return len(self.off_grid_pads)

    @property
    def total_pads(self) -> int:
        """Total number of pads analyzed."""
        return len(self.off_grid_pads) + len(self.on_grid_pads)

    @property
    def off_grid_percentage(self) -> float:
        """Percentage of pads that are off-grid."""
        if self.total_pads == 0:
            return 0.0
        return 100.0 * self.off_grid_count / self.total_pads

    def format_summary(self) -> str:
        """Format a summary of the sub-grid analysis."""
        lines = [
            f"Sub-grid analysis: {self.off_grid_count}/{self.total_pads} pads off-grid "
            f"({self.off_grid_percentage:.1f}%)",
        ]
        if self.off_grid_pads:
            # Group by component
            by_ref: dict[str, list[SubGridPad]] = {}
            for sgp in self.off_grid_pads:
                ref = sgp.pad.ref
                if ref not in by_ref:
                    by_ref[ref] = []
                by_ref[ref].append(sgp)

            for ref, pads in sorted(by_ref.items()):
                offsets = [max(abs(p.offset_x), abs(p.offset_y)) for p in pads]
                avg_offset = sum(offsets) / len(offsets)
                lines.append(
                    f"  {ref}: {len(pads)} off-grid pads, "
                    f"avg offset {avg_offset:.3f}mm"
                )
        return "\n".join(lines)


@dataclass
class SubGridResult:
    """Complete result of sub-grid escape routing.

    Attributes:
        escapes: Generated escape segments
        analysis: The analysis that produced these escapes
        unblocked_count: Number of pad grid cells that were unblocked
        failed_pads: Pads where escape routing could not find a valid path
    """

    escapes: list[SubGridEscape] = field(default_factory=list)
    analysis: SubGridAnalysis | None = None
    unblocked_count: int = 0
    failed_pads: list[Pad] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        """Number of pads successfully escaped."""
        return len(self.escapes)

    @property
    def total_attempted(self) -> int:
        """Total pads attempted."""
        return self.success_count + len(self.failed_pads)

    def format_summary(self) -> str:
        """Format a summary of escape results."""
        lines = [
            f"Sub-grid escape routing: {self.success_count}/{self.total_attempted} pads escaped",
        ]
        if self.unblocked_count > 0:
            lines.append(f"  Grid cells unblocked: {self.unblocked_count}")
        if self.failed_pads:
            failed_refs = sorted({p.ref for p in self.failed_pads})
            lines.append(f"  Failed components: {', '.join(failed_refs)}")
        return "\n".join(lines)


class SubGridRouter:
    """Sub-grid router for fine-pitch component pad connections.

    Creates localized escape segments from off-grid pads to the nearest
    main-grid points, enabling the main A* router to handle fine-pitch
    components without requiring a global fine grid.

    The router works in three phases:
    1. **Analysis**: Identify pads that don't align with the main grid
    2. **Escape generation**: Create short segments from pad centers to grid points
    3. **Grid preparation**: Unblock grid cells at escape endpoints so the
       main router can use them as route start/end points

    Args:
        grid: The main routing grid
        rules: Design rules for routing
        grid_tolerance: Maximum offset to consider a pad "on grid".
            Default is resolution/4, which catches pads that are more than
            25% of a grid cell away from the nearest grid point.
        escape_search_radius: Number of grid cells to search for a valid
            escape endpoint. Default is 3 cells.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        grid_tolerance: float | None = None,
        escape_search_radius: int = 3,
    ):
        self.grid = grid
        self.rules = rules
        self.grid_tolerance = grid_tolerance if grid_tolerance is not None else grid.resolution / 4
        self.escape_search_radius = escape_search_radius

    def analyze_pads(
        self,
        pads: dict[tuple[str, str], Pad] | list[Pad],
    ) -> SubGridAnalysis:
        """Analyze pads to identify those requiring sub-grid routing.

        Checks each pad's position against the main routing grid and identifies
        pads whose centers fall between grid points (off-grid pads).

        Args:
            pads: Dictionary mapping (ref, pin) to Pad, or list of Pad objects

        Returns:
            SubGridAnalysis with categorized pads and component centers
        """
        # Normalize input
        if isinstance(pads, dict):
            pad_list = list(pads.values())
        else:
            pad_list = list(pads)

        analysis = SubGridAnalysis(
            grid_resolution=self.grid.resolution,
            grid_tolerance=self.grid_tolerance,
        )

        # Compute component centers for escape direction calculation
        pads_by_ref: dict[str, list[Pad]] = {}
        for pad in pad_list:
            ref = pad.ref
            if ref:
                if ref not in pads_by_ref:
                    pads_by_ref[ref] = []
                pads_by_ref[ref].append(pad)

        for ref, comp_pads in pads_by_ref.items():
            if comp_pads:
                cx = sum(p.x for p in comp_pads) / len(comp_pads)
                cy = sum(p.y for p in comp_pads) / len(comp_pads)
                analysis.component_centers[ref] = (cx, cy)

        # Classify each pad
        for pad in pad_list:
            gx, gy = self.grid.world_to_grid(pad.x, pad.y)
            snap_x, snap_y = self.grid.grid_to_world(gx, gy)

            offset_x = pad.x - snap_x
            offset_y = pad.y - snap_y
            max_offset = max(abs(offset_x), abs(offset_y))

            if max_offset > self.grid_tolerance:
                # Calculate escape direction (outward from component center)
                escape_dir = (0.0, 0.0)
                ref = pad.ref
                if ref and ref in analysis.component_centers:
                    cx, cy = analysis.component_centers[ref]
                    dx = pad.x - cx
                    dy = pad.y - cy
                    length = math.sqrt(dx * dx + dy * dy)
                    if length > 0.001:
                        escape_dir = (dx / length, dy / length)

                analysis.off_grid_pads.append(
                    SubGridPad(
                        pad=pad,
                        grid_x=gx,
                        grid_y=gy,
                        offset_x=offset_x,
                        offset_y=offset_y,
                        snap_x=snap_x,
                        snap_y=snap_y,
                        escape_direction=escape_dir,
                    )
                )
            else:
                analysis.on_grid_pads.append(pad)

        return analysis

    def generate_escape_segments(
        self,
        analysis: SubGridAnalysis,
    ) -> SubGridResult:
        """Generate escape segments for all off-grid pads.

        For each off-grid pad, finds the best nearby grid point and creates
        a short escape segment from the pad center to that grid point.

        The escape point selection considers:
        - Distance from pad center (shorter is better)
        - Whether the grid cell is blocked by other nets
        - Escape direction (prefer outward from component center)
        - Clearance to adjacent pads

        Args:
            analysis: SubGridAnalysis from analyze_pads()

        Returns:
            SubGridResult with escape segments and statistics
        """
        result = SubGridResult(analysis=analysis)

        for sgp in analysis.off_grid_pads:
            escape = self._find_escape_for_pad(sgp)
            if escape is not None:
                result.escapes.append(escape)
            else:
                result.failed_pads.append(sgp.pad)
                logger.debug(
                    "Sub-grid escape failed for %s.%s at (%.3f, %.3f)",
                    sgp.pad.ref,
                    sgp.pad.pin,
                    sgp.pad.x,
                    sgp.pad.y,
                )

        logger.info(
            "Sub-grid escape routing: %d/%d pads escaped",
            result.success_count,
            result.total_attempted,
        )

        return result

    def apply_escape_segments(
        self,
        result: SubGridResult,
    ) -> int:
        """Apply escape segments to the grid, unblocking escape endpoints.

        For each escape segment, marks the grid cell at the escape endpoint
        as belonging to the pad's net, allowing the main router to start/end
        routes at these points.

        Args:
            result: SubGridResult from generate_escape_segments()

        Returns:
            Number of grid cells unblocked
        """
        unblocked = 0

        for escape in result.escapes:
            gx, gy = escape.grid_point
            pad = escape.pad

            # Determine which layers to unblock
            if pad.through_hole:
                layer_indices = list(range(self.grid.num_layers))
            else:
                layer_indices = [self.grid.layer_to_index(pad.layer.value)]

            for layer_idx in layer_indices:
                if 0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows:
                    cell = self.grid.grid[layer_idx][gy][gx]
                    # Only unblock if the cell belongs to our net or is unassigned
                    if cell.net == pad.net or cell.net == 0:
                        if cell.blocked and not cell.pad_blocked:
                            # This is a clearance zone cell, not actual pad copper.
                            # Unblock it so the router can reach this grid point.
                            cell.blocked = False
                            cell.net = pad.net
                            unblocked += 1
                        elif not cell.blocked:
                            # Already unblocked, just ensure net assignment
                            cell.net = pad.net

        result.unblocked_count = unblocked
        logger.info("Sub-grid escape: unblocked %d grid cells", unblocked)
        return unblocked

    def route_with_subgrid(
        self,
        pads: dict[tuple[str, str], Pad] | list[Pad],
    ) -> SubGridResult:
        """Convenience method: analyze, generate escapes, and apply to grid.

        This is the primary entry point for sub-grid routing. It performs
        the full three-phase process in one call.

        Args:
            pads: Pads to analyze and generate escapes for

        Returns:
            SubGridResult with all escape routing results
        """
        analysis = self.analyze_pads(pads)

        if not analysis.has_off_grid_pads:
            logger.info("No off-grid pads detected, sub-grid routing not needed")
            return SubGridResult(analysis=analysis)

        logger.info(analysis.format_summary())

        result = self.generate_escape_segments(analysis)
        self.apply_escape_segments(result)

        return result

    def _find_escape_for_pad(self, sgp: SubGridPad) -> SubGridEscape | None:
        """Find the best escape point for a single off-grid pad.

        Searches nearby grid points for the best escape target, considering
        blockage, distance, and escape direction.

        Args:
            sgp: SubGridPad to find escape for

        Returns:
            SubGridEscape if found, None if no valid escape point exists
        """
        pad = sgp.pad
        best_score = float("inf")
        best_gx, best_gy = sgp.grid_x, sgp.grid_y
        best_snap_x, best_snap_y = sgp.snap_x, sgp.snap_y
        found = False

        # Determine the layer to check
        if pad.through_hole:
            check_layers = list(range(self.grid.num_layers))
        else:
            check_layers = [self.grid.layer_to_index(pad.layer.value)]

        # Search in a spiral pattern around the nearest grid point
        radius = self.escape_search_radius
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                gx = sgp.grid_x + dx
                gy = sgp.grid_y + dy

                if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                    continue

                snap_x, snap_y = self.grid.grid_to_world(gx, gy)

                # Distance from pad center to this grid point
                dist = math.sqrt((pad.x - snap_x) ** 2 + (pad.y - snap_y) ** 2)

                # Check if this grid point is accessible on any valid layer
                accessible = False
                for layer_idx in check_layers:
                    cell = self.grid.grid[layer_idx][gy][gx]
                    # Accept cells that are:
                    # - Unblocked
                    # - Same-net (our pad's clearance zone)
                    # - Clearance-only (not actual pad copper from other net)
                    if not cell.blocked:
                        accessible = True
                        break
                    elif cell.net == pad.net:
                        accessible = True
                        break
                    elif not cell.pad_blocked and cell.original_net == pad.net:
                        # Clearance zone of our own pad
                        accessible = True
                        break

                if not accessible:
                    continue

                # Score this candidate: prefer close, in escape direction
                score = dist

                # Bonus for being in the escape direction
                if sgp.escape_direction != (0.0, 0.0):
                    ex, ey = sgp.escape_direction
                    # Direction from pad to candidate
                    cdx = snap_x - pad.x
                    cdy = snap_y - pad.y
                    cdist = math.sqrt(cdx * cdx + cdy * cdy)
                    if cdist > 0.001:
                        # Dot product with escape direction (1.0 = same direction)
                        dot = (cdx / cdist) * ex + (cdy / cdist) * ey
                        # Prefer candidates in the escape direction
                        # (lower score = better, so subtract bonus for alignment)
                        score -= dot * self.grid.resolution * 0.5

                # Penalty for being too far
                if dist > self.grid.resolution * radius:
                    score += dist * 2

                if score < best_score:
                    best_score = score
                    best_gx, best_gy = gx, gy
                    best_snap_x, best_snap_y = snap_x, snap_y
                    found = True

        if not found:
            return None

        # Create escape segment from pad center to grid point
        layer = pad.layer
        width = self.rules.trace_width

        # Apply neck-down if configured for fine-pitch
        if self.rules.min_trace_width is not None:
            ref = pad.ref
            pin_pitch = None
            if ref:
                pitches = self.grid.compute_component_pitches()
                pin_pitch = pitches.get(ref)
            if self.rules.should_apply_neck_down(ref, pin_pitch):
                width = self.rules.min_trace_width

        segment = Segment(
            x1=pad.x,
            y1=pad.y,
            x2=best_snap_x,
            y2=best_snap_y,
            width=width,
            layer=layer,
            net=pad.net,
            net_name=pad.net_name,
        )

        return SubGridEscape(
            pad=pad,
            segment=segment,
            grid_point=(best_gx, best_gy),
            snap_point=(best_snap_x, best_snap_y),
        )

    def get_escape_routes(self, result: SubGridResult) -> list[Route]:
        """Convert escape segments into Route objects for PCB output.

        Each escape segment becomes a single-segment Route that can be
        included in the final PCB output alongside the main routed traces.

        Args:
            result: SubGridResult with escape segments

        Returns:
            List of Route objects for the escape paths
        """
        routes: list[Route] = []
        for escape in result.escapes:
            route = Route(
                net=escape.pad.net,
                net_name=escape.pad.net_name,
                segments=[escape.segment],
            )
            routes.append(route)
        return routes


def compute_subgrid_resolution(
    pin_pitch: float,
    main_resolution: float,
) -> float:
    """Compute an appropriate sub-grid resolution for a given pin pitch.

    Finds the finest resolution that:
    1. Divides evenly into the pin pitch (or nearly so)
    2. Is finer than the main grid resolution
    3. Is not excessively fine (minimum 0.005mm)

    Args:
        pin_pitch: Component pin pitch in mm
        main_resolution: Main grid resolution in mm

    Returns:
        Recommended sub-grid resolution in mm

    Example:
        >>> compute_subgrid_resolution(0.65, 0.1)
        0.025  # 0.65 / 26 = 0.025mm, divides well
    """
    # Try common grid values that work well with typical pitches
    candidates = [0.005, 0.01, 0.0125, 0.025, 0.05]

    best_res = main_resolution / 2  # Fallback: half the main grid

    for res in candidates:
        if res >= main_resolution:
            continue  # Must be finer than main grid

        # Check how well this resolution aligns with the pitch
        ratio = pin_pitch / res
        alignment_error = abs(ratio - round(ratio))

        if alignment_error < 0.01:
            # Good alignment - this resolution works well
            best_res = res
            break

    return best_res


__all__ = [
    "SubGridAnalysis",
    "SubGridEscape",
    "SubGridPad",
    "SubGridResult",
    "SubGridRouter",
    "compute_subgrid_resolution",
]
