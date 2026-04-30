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
    from .io import FineZone
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
            escape endpoint. Default adapts to grid resolution
            (minimum 3 cells, scaled so search covers at least 0.3mm).
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        grid_tolerance: float | None = None,
        escape_search_radius: int | None = None,
        clearance_weight: float = 2.5,
        fine_zones: list[FineZone] | None = None,
    ):
        self.grid = grid
        self.rules = rules
        self.grid_tolerance = grid_tolerance if grid_tolerance is not None else grid.resolution / 4
        # Adaptive search radius: at fine grids (< 0.1mm), 3 cells may not
        # reach past neighboring clearance zones for fine-pitch packages.
        # Scale the radius so the physical search distance is at least 0.3mm.
        if escape_search_radius is not None:
            self.escape_search_radius = escape_search_radius
        else:
            min_search_mm = 0.3  # minimum physical search distance
            self.escape_search_radius = max(3, math.ceil(min_search_mm / grid.resolution))
        self.clearance_weight = clearance_weight
        self.fine_zones: list[FineZone] = fine_zones or []

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
                    # Unblock clearance-zone cells so the router can reach
                    # this grid point.  Clearance-zone cells (blocked but NOT
                    # pad_blocked) may belong to a neighboring pad's net at
                    # fine pitch (e.g. 0.65mm on 0.05mm grid).  The escape
                    # segment was already validated by
                    # validate_segment_clearance() in generate_escape_segments,
                    # so overriding the net assignment here is safe.  Actual
                    # copper (pad_blocked=True) is never unblocked.
                    if cell.blocked and not cell.pad_blocked:
                        # Clearance-zone cell -- safe to claim for our net
                        prev_net = cell.net
                        cell.blocked = False
                        cell.net = pad.net
                        unblocked += 1
                        if prev_net != pad.net and prev_net != 0:
                            logger.debug(
                                "Overriding clearance cell (%d,%d) net %d -> "
                                "%d for %s.%s escape",
                                gx, gy, prev_net, pad.net,
                                pad.ref, pad.pin,
                            )
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

    def _min_clearance_to_neighbors(
        self,
        x: float,
        y: float,
        half_width: float,
        net: int,
        layer_idx: int,
    ) -> float:
        """Compute minimum edge-to-edge distance from a candidate point to any different-net pad.

        Used for clearance-weighted scoring of escape candidates (Issue #1642).
        For each pad on the board that belongs to a different net and overlaps
        the candidate's layer, computes the edge-to-edge distance (center-to-center
        minus the candidate's half-width minus the pad's effective radius).

        Args:
            x: Candidate snap point X coordinate (world units, mm)
            y: Candidate snap point Y coordinate (world units, mm)
            half_width: Half the trace width of the escape segment
            net: Net ID of the pad being escaped (neighbors on this net are skipped)
            layer_idx: Grid layer index to check (SMD pads on other layers are skipped)

        Returns:
            Minimum edge-to-edge distance in mm. Returns ``float('inf')`` when
            there are no different-net pads on the relevant layer.
        """
        min_dist = float("inf")
        for pad in self.grid._pads:
            if pad.net == net:
                continue
            # Layer filtering: through-hole pads appear on all layers,
            # SMD pads only on their own layer.
            if not pad.through_hole:
                try:
                    pad_li = self.grid.layer_to_index(pad.layer.value)
                except Exception:
                    continue
                if pad_li != layer_idx:
                    continue
            pad_radius = max(pad.width, pad.height) / 2
            dx = x - pad.x
            dy = y - pad.y
            dist = math.sqrt(dx * dx + dy * dy) - half_width - pad_radius
            if dist < min_dist:
                min_dist = dist
        return min_dist

    def _get_pad_fine_resolution(self, pad: Pad) -> float | None:
        """Return the fine-zone resolution for a pad, or None if not in a fine zone.

        When a pad falls inside one or more fine zones, the finest (smallest)
        resolution is returned so that escape candidates are generated on the
        densest applicable grid.

        Args:
            pad: The pad to check.

        Returns:
            Fine resolution in mm, or None if the pad is outside all fine zones.
        """
        best: float | None = None
        for zone in self.fine_zones:
            if zone.contains(pad.x, pad.y):
                if best is None or zone.resolution < best:
                    best = zone.resolution
        return best

    def _generate_fine_grid_candidates(
        self,
        sgp: SubGridPad,
        fine_resolution: float,
        min_clearance_factor: float = 1.0,
    ) -> list[tuple[float, int, int, float, float]]:
        """Generate escape candidates on a fine grid, bridging to the coarse grid.

        For pads inside a fine zone, this generates candidate escape points on
        the fine grid (``fine_resolution`` spacing) within the search radius,
        then maps each fine-grid candidate to the nearest coarse-grid cell.
        This ensures the escape segment terminates at a point the main A*
        router can reach on the coarse grid.

        Args:
            sgp: The off-grid pad to generate candidates for.
            fine_resolution: Fine grid resolution in mm.
            min_clearance_factor: Multiplier applied to ``trace_clearance``
                for the hard-reject threshold.  1.0 uses normal clearance;
                values < 1.0 relax the requirement (used in fallback modes).

        Returns:
            List of ``(score, gx, gy, snap_x, snap_y)`` tuples where
            ``(gx, gy)`` is the coarse grid cell and ``(snap_x, snap_y)``
            is the fine-grid world point used as the escape endpoint.
        """
        pad = sgp.pad

        # Physical search distance: use the same minimum as the coarse search
        # but expressed in fine-grid cells.
        min_search_mm = 0.3
        fine_radius = max(3, math.ceil(min_search_mm / fine_resolution))

        # Issue #1834: Extend search radius for tight-pitch pads where the
        # inter-pad gap is too narrow for between-pad escape.  This pushes
        # candidates outward (away from the IC body) where clearance permits.
        half_width = (
            self.rules.trace_width / 2
            if self.rules.min_trace_width is None
            else self.rules.min_trace_width / 2
        )
        required_clearance = self.rules.trace_clearance * min_clearance_factor
        pitches = self.grid.compute_component_pitches()
        pad_pitch = pitches.get(pad.ref)
        if pad_pitch is not None:
            pad_half = max(pad.width, pad.height) / 2
            min_channel = pad_pitch - 2 * pad_half
            needed_channel = 2 * half_width + 2 * required_clearance
            if min_channel < needed_channel:
                # Between-pad routing is impossible; extend radius so the
                # search reaches past the IC body edge.
                extended_mm = max(min_search_mm, pad_pitch * 2)
                fine_radius = max(fine_radius, math.ceil(extended_mm / fine_resolution))

        # The fine grid is centred on the pad's nearest coarse-grid point
        # (sgp.snap_x, sgp.snap_y) and extends ``fine_radius`` fine cells
        # in each direction.
        candidates: list[tuple[float, int, int, float, float]] = []

        for dy in range(-fine_radius, fine_radius + 1):
            for dx in range(-fine_radius, fine_radius + 1):
                # Fine-grid candidate in world coordinates
                fx = sgp.snap_x + dx * fine_resolution
                fy = sgp.snap_y + dy * fine_resolution

                # Distance from pad center to this fine-grid point
                dist = math.sqrt((pad.x - fx) ** 2 + (pad.y - fy) ** 2)

                # Map the fine candidate back to the nearest coarse grid cell
                # so the main router can connect to it.
                gx, gy = self.grid.world_to_grid(fx, fy)

                if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                    continue

                # The escape segment terminates at the fine-grid point (fx, fy),
                # but the coarse grid cell (gx, gy) is what gets unblocked for
                # the A* router.  Check accessibility on the coarse grid.
                if pad.through_hole:
                    check_layers = list(range(self.grid.num_layers))
                else:
                    check_layers = [self.grid.layer_to_index(pad.layer.value)]

                accessible = False
                for layer_idx in check_layers:
                    cell = self.grid.grid[layer_idx][gy][gx]
                    if not cell.blocked:
                        accessible = True
                        break
                    elif not cell.pad_blocked:
                        accessible = True
                        break
                if not accessible:
                    continue

                # Score: distance + escape direction bonus (same logic as coarse)
                score = dist

                if sgp.escape_direction != (0.0, 0.0):
                    ex, ey = sgp.escape_direction
                    cdx = fx - pad.x
                    cdy = fy - pad.y
                    cdist = math.sqrt(cdx * cdx + cdy * cdy)
                    if cdist > 0.001:
                        dot = (cdx / cdist) * ex + (cdy / cdist) * ey
                        score -= dot * fine_resolution * 0.5

                # Penalty for being too far (use fine resolution for threshold)
                if dist > fine_resolution * fine_radius:
                    score += dist * 2

                # Issue #1834: Hard-reject candidates that violate minimum
                # clearance against neighboring pads.  On tight-pitch ICs
                # (e.g. 0.65mm SSOP) the inter-pad gap is too narrow for a
                # trace, so candidates between pads must be eliminated early
                # rather than merely penalized.
                neighbor_clearance = self._min_clearance_to_neighbors(
                    fx, fy, half_width, pad.net, check_layers[0],
                )
                if neighbor_clearance < required_clearance:
                    continue  # Physically impossible -- skip

                # Clearance-aware soft scoring for remaining candidates
                if self.clearance_weight > 0:
                    threshold = required_clearance * 2
                    if neighbor_clearance < threshold:
                        clearance_penalty = (
                            (threshold - neighbor_clearance) * self.clearance_weight
                        )
                        score += clearance_penalty

                candidates.append((score, gx, gy, fx, fy))

        return candidates

    def _collect_coarse_candidates(
        self,
        sgp: SubGridPad,
        radius: int,
        check_layers: list[int],
        width: float,
        min_clearance_factor: float = 1.0,
    ) -> list[tuple[float, int, int, float, float]]:
        """Collect accessible coarse-grid escape candidates around a pad.

        Searches grid points in a square region of ``radius`` cells around
        the pad's nearest grid point.  Each candidate is scored by distance,
        escape-direction alignment, and clearance to neighboring pads.

        Args:
            sgp: The off-grid pad to find candidates for.
            radius: Number of grid cells to search in each direction.
            check_layers: Grid layer indices to check for accessibility.
            width: Trace width for clearance calculations.
            min_clearance_factor: Multiplier applied to ``trace_clearance``
                for the hard-reject threshold.  1.0 uses normal clearance;
                values < 1.0 relax the requirement (used in fallback modes).

        Returns:
            List of ``(score, gx, gy, snap_x, snap_y)`` tuples.
        """
        pad = sgp.pad
        candidates: list[tuple[float, int, int, float, float]] = []
        required_clearance = self.rules.trace_clearance * min_clearance_factor

        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                gx = sgp.grid_x + dx
                gy = sgp.grid_y + dy

                if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                    continue

                snap_x, snap_y = self.grid.grid_to_world(gx, gy)

                # Distance from pad center to this grid point
                dist = math.sqrt((pad.x - snap_x) ** 2 + (pad.y - snap_y) ** 2)

                # Check if this grid point is a valid escape target on any
                # valid layer.  The escape target must be a cell that
                # apply_escape_segments() can make routable:
                #   - Already unblocked (free cell -- no action needed)
                #   - Clearance-zone cell of ANY net (blocked, not
                #     pad_blocked) -- apply_escape_segments will unblock it
                # Cells that are the pad's own copper (pad_blocked, same net)
                # are NOT useful targets: they are already blocked and
                # cannot be unblocked (they ARE the pad).  The router needs
                # an entry point OUTSIDE the pad copper.
                # Cells that are another net's copper (pad_blocked, different
                # net) are also rejected -- cannot unblock real copper.
                accessible = False
                for layer_idx in check_layers:
                    cell = self.grid.grid[layer_idx][gy][gx]
                    if not cell.blocked:
                        # Free cell -- router can already reach it
                        accessible = True
                        break
                    elif not cell.pad_blocked:
                        # Clearance-zone cell (any net) -- can be unblocked
                        # by apply_escape_segments; validate_segment_clearance
                        # will do the precise DRC check later.
                        accessible = True
                        break
                    # pad_blocked cells (own or other net copper) are skipped

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

                # Hard-reject coarse candidates that violate minimum
                # clearance against neighboring pads (Issue #1834).
                neighbor_clearance = self._min_clearance_to_neighbors(
                    snap_x, snap_y, width / 2, pad.net, check_layers[0],
                )
                if neighbor_clearance < required_clearance:
                    continue  # Physically impossible -- skip

                # Clearance-aware scoring (Phase 2, Issue #1642)
                if self.clearance_weight > 0:
                    threshold = required_clearance * 2
                    if neighbor_clearance < threshold:
                        clearance_penalty = (
                            (threshold - neighbor_clearance) * self.clearance_weight
                        )
                        score += clearance_penalty

                candidates.append((score, gx, gy, snap_x, snap_y))

        return candidates

    def _deduplicate_candidates(
        self,
        candidates: list[tuple[float, int, int, float, float]],
    ) -> list[tuple[float, int, int, float, float]]:
        """Deduplicate candidates that share the same coarse grid cell.

        When fine-grid candidates map to the same (gx, gy) as a coarse
        candidate, keep only the one with the best (lowest) score.
        Different snap points for the same grid cell ARE useful if they
        produce different escape segment geometry, so we deduplicate by
        the full (gx, gy, snap_x_rounded, snap_y_rounded) key.

        Args:
            candidates: Raw candidate list.

        Returns:
            Deduplicated candidate list sorted by score (lowest first).
        """
        seen: dict[tuple[int, int, int, int], tuple[float, int, int, float, float]] = {}
        for cand in candidates:
            score, gx, gy, sx, sy = cand
            key = (gx, gy, round(sx * 10000), round(sy * 10000))
            if key not in seen or score < seen[key][0]:
                seen[key] = cand
        result = list(seen.values())
        result.sort(key=lambda c: c[0])
        return result

    def _try_candidates_with_clearance(
        self,
        sgp: SubGridPad,
        candidates: list[tuple[float, int, int, float, float]],
        width: float,
        layer: Layer,
        component_pitches: dict[str, float],
        min_clearance: float | None = None,
    ) -> SubGridEscape | None:
        """Try candidates in score order, validating clearance.

        Args:
            sgp: The off-grid pad being escaped.
            candidates: Sorted candidate list (lowest score first).
            width: Trace width for the escape segment.
            layer: Layer for the escape segment.
            component_pitches: Component pitch map for clearance validation.
            min_clearance: If provided, use this as the clearance threshold
                for ALL pads (overriding per-component clearance).  This is
                the relaxed-clearance fallback for escape segments.

        Returns:
            SubGridEscape if a valid candidate is found, None otherwise.
        """
        pad = sgp.pad

        for _score, gx, gy, snap_x, snap_y in candidates:
            segment = Segment(
                x1=pad.x,
                y1=pad.y,
                x2=snap_x,
                y2=snap_y,
                width=width,
                layer=layer,
                net=pad.net,
                net_name=pad.net_name,
            )

            if min_clearance is not None:
                # Relaxed mode: manually check clearance against neighbor
                # pads using the reduced threshold, bypassing per-component
                # clearance overrides that would use the stricter value.
                is_valid = self._validate_segment_relaxed(
                    segment, pad.net, min_clearance,
                )
                violation_loc = None
            else:
                # Normal mode: use full validate_segment_clearance
                is_valid, _clearance, violation_loc = self.grid.validate_segment_clearance(
                    segment,
                    exclude_net=pad.net,
                    component_pitches=component_pitches,
                )

            if is_valid:
                return SubGridEscape(
                    pad=pad,
                    segment=segment,
                    grid_point=(gx, gy),
                    snap_point=(snap_x, snap_y),
                )
            else:
                logger.debug(
                    "Escape candidate (%d, %d) for %s.%s failed clearance "
                    "at (%.3f, %.3f), trying next",
                    gx,
                    gy,
                    pad.ref,
                    pad.pin,
                    violation_loc[0] if violation_loc else 0.0,
                    violation_loc[1] if violation_loc else 0.0,
                )

        return None

    def _validate_segment_relaxed(
        self,
        seg: Segment,
        exclude_net: int,
        min_clearance: float,
    ) -> bool:
        """Validate a segment's clearance using a relaxed threshold.

        Unlike ``grid.validate_segment_clearance()``, this method uses a
        single flat clearance threshold for ALL neighboring pads, ignoring
        per-component clearance overrides.  This is used in the relaxed-
        clearance fallback (Issue #1965) where the normal clearance is too
        strict for escape segments.

        The check still ensures no actual copper overlap (clearance > 0)
        even if ``min_clearance`` is very small.

        Args:
            seg: The escape segment to validate.
            exclude_net: Net ID to exclude (same-net pads are skipped).
            min_clearance: Relaxed clearance threshold in mm.

        Returns:
            True if the segment passes the relaxed clearance check.
        """
        seg_half_width = seg.width / 2
        seg_layer_idx = self.grid.layer_to_index(seg.layer.value)

        for pad in self.grid._pads:
            if pad.net == exclude_net:
                continue

            # Layer filtering
            if not pad.through_hole:
                try:
                    pad_li = self.grid.layer_to_index(pad.layer.value)
                except Exception:
                    continue
                if pad_li != seg_layer_idx:
                    continue

            pad_radius = max(pad.width, pad.height) / 2
            dist = self.grid._point_to_segment_distance(
                pad.x, pad.y, seg.x1, seg.y1, seg.x2, seg.y2,
            )
            clearance = dist - seg_half_width - pad_radius

            if clearance < min_clearance:
                return False

        return True

    def _find_escape_for_pad(self, sgp: SubGridPad) -> SubGridEscape | None:
        """Find the best escape point for a single off-grid pad.

        Searches nearby grid points for the best escape target, considering
        blockage, distance, escape direction, and clearance validation.

        Issue #1626: Candidate escape segments are now validated against
        ``validate_segment_clearance()`` before being accepted. If the
        best-scoring candidate fails clearance, the next-best candidate is
        tried, and so on. This prevents escape segments from creating DRC
        violations against neighboring pads/traces of other nets.

        Issue #1965: When all candidates at the initial search radius fail,
        three fallback strategies are attempted in order:
        1. **Expanded search radius** -- doubles the radius to find grid
           points beyond the initial clearance-blocked zone.
        2. **Relaxed clearance** -- reduces the clearance threshold to 50%
           for escape segments only, since escape segments are short and
           directly connect pad copper to the grid.
        3. **Multi-hop escape** -- routes through an intermediate grid
           point when a direct segment cannot satisfy clearance.

        Args:
            sgp: SubGridPad to find escape for

        Returns:
            SubGridEscape if found, None if no valid escape point exists
        """
        pad = sgp.pad

        # Determine the layer to check
        if pad.through_hole:
            check_layers = list(range(self.grid.num_layers))
        else:
            check_layers = [self.grid.layer_to_index(pad.layer.value)]

        # Determine trace width for escape segment (needed for clearance scoring)
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

        # Compute component pitches once for clearance validation
        component_pitches = self.grid.compute_component_pitches()

        # --- Phase 1: Normal search radius with full clearance ---
        radius = self.escape_search_radius
        candidates = self._collect_coarse_candidates(
            sgp, radius, check_layers, width,
        )

        # Issue #1828: When the pad falls within a fine zone, generate
        # additional candidates on the fine grid.
        fine_res = self._get_pad_fine_resolution(pad)
        if fine_res is not None and fine_res < self.grid.resolution:
            fine_candidates = self._generate_fine_grid_candidates(sgp, fine_res)
            candidates.extend(fine_candidates)
            logger.debug(
                "Fine-zone escape for %s.%s: %d fine candidates + %d coarse candidates "
                "(fine_res=%.4fmm)",
                pad.ref, pad.pin,
                len(fine_candidates),
                len(candidates) - len(fine_candidates),
                fine_res,
            )

        if not candidates:
            # No accessible candidates at all -- try expanded radius
            # before giving up (Phase 2 below).
            pass
        else:
            candidates = self._deduplicate_candidates(candidates)

            escape = self._try_candidates_with_clearance(
                sgp, candidates, width, layer, component_pitches,
            )
            if escape is not None:
                return escape

        # --- Phase 2: Expanded search radius (2x) ---
        # The initial radius may be too small for pads surrounded by
        # clearance zones of neighboring components.  Doubling the radius
        # reaches grid points beyond the congested zone.
        expanded_radius = radius * 2
        expanded_candidates = self._collect_coarse_candidates(
            sgp, expanded_radius, check_layers, width,
        )

        # Include fine-grid candidates at expanded radius if applicable
        if fine_res is not None and fine_res < self.grid.resolution:
            expanded_candidates.extend(
                self._generate_fine_grid_candidates(sgp, fine_res)
            )

        if expanded_candidates:
            expanded_candidates = self._deduplicate_candidates(expanded_candidates)

            escape = self._try_candidates_with_clearance(
                sgp, expanded_candidates, width, layer, component_pitches,
            )
            if escape is not None:
                logger.debug(
                    "Escape for %s.%s succeeded with expanded radius %d",
                    pad.ref, pad.pin, expanded_radius,
                )
                return escape

        # --- Phase 3: Relaxed clearance mode ---
        # For escape segments specifically, reduce the clearance requirement
        # by the configurable subgrid_clearance_factor.  Escape segments are
        # very short (sub-grid distance) and connect directly to pad copper,
        # so slightly reduced clearance is acceptable and preferable to no
        # connection.  The factor is configurable via DesignRules to allow
        # boards with tight-pitch packages to tighten or loosen the relaxation.
        factor = self.rules.subgrid_clearance_factor
        relaxed_clearance = self.rules.trace_clearance * factor

        # Re-collect candidates with relaxed clearance for the hard-reject
        # filter so previously rejected candidates are now included.
        relaxed_candidates = self._collect_coarse_candidates(
            sgp, expanded_radius, check_layers, width,
            min_clearance_factor=factor,
        )
        if fine_res is not None and fine_res < self.grid.resolution:
            # Generate fine-grid candidates with relaxed clearance
            relaxed_candidates.extend(
                self._generate_fine_grid_candidates(
                    sgp, fine_res, min_clearance_factor=factor,
                )
            )

        if relaxed_candidates:
            relaxed_candidates = self._deduplicate_candidates(relaxed_candidates)

            escape = self._try_candidates_with_clearance(
                sgp, relaxed_candidates, width, layer, component_pitches,
                min_clearance=relaxed_clearance,
            )
            if escape is not None:
                logger.debug(
                    "Escape for %s.%s succeeded with relaxed clearance "
                    "(%.3fmm vs normal %.3fmm)",
                    pad.ref, pad.pin,
                    relaxed_clearance, self.rules.trace_clearance,
                )
                return escape

        # --- Phase 4: Multi-hop escape ---
        # When direct escape fails, try routing through an intermediate
        # grid point.  The first hop goes from the pad to a nearby grid
        # point (even if that point is in a clearance zone), and the
        # second hop connects from that intermediate point to a free grid
        # point the main router can reach.
        escape = self._try_multi_hop_escape(
            sgp, check_layers, width, layer, component_pitches,
        )
        if escape is not None:
            logger.debug(
                "Escape for %s.%s succeeded via multi-hop",
                pad.ref, pad.pin,
            )
            return escape

        # All strategies exhausted
        logger.debug(
            "All escape strategies failed for %s.%s "
            "(normal, expanded, relaxed, multi-hop)",
            pad.ref, pad.pin,
        )
        return None

    def _try_multi_hop_escape(
        self,
        sgp: SubGridPad,
        check_layers: list[int],
        width: float,
        layer: Layer,
        component_pitches: dict[str, float],
    ) -> SubGridEscape | None:
        """Attempt a multi-hop escape through an intermediate grid point.

        When a direct escape from pad to grid point fails clearance, this
        method tries a two-segment path: pad -> intermediate -> target.
        The intermediate point is chosen from same-net or unblocked cells
        near the pad, and the target is a free grid cell reachable from
        the intermediate.

        The resulting escape uses the intermediate point as the segment
        endpoint (the main router handles the rest).  Only the first hop
        (pad to intermediate) is stored as the escape segment; the second
        hop from intermediate to target is implicit since both are on the
        grid and the A* router can connect them.

        Args:
            sgp: The off-grid pad to escape.
            check_layers: Grid layer indices to check.
            width: Trace width for escape segments.
            layer: Layer for escape segments.
            component_pitches: Component pitch map for clearance validation.

        Returns:
            SubGridEscape if a valid multi-hop path is found, None otherwise.
        """
        pad = sgp.pad
        radius = self.escape_search_radius

        # Collect intermediate candidates: grid points near the pad that
        # might be accessible even with clearance issues (we use relaxed
        # clearance for the first hop).
        relaxed_clearance = self.rules.trace_clearance * self.rules.subgrid_clearance_factor

        # Search for intermediate points (within normal radius)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                mid_gx = sgp.grid_x + dx
                mid_gy = sgp.grid_y + dy

                if not (0 <= mid_gx < self.grid.cols and 0 <= mid_gy < self.grid.rows):
                    continue

                mid_x, mid_y = self.grid.grid_to_world(mid_gx, mid_gy)

                # The intermediate must be accessible (free or clearance-zone)
                accessible = False
                for layer_idx in check_layers:
                    cell = self.grid.grid[layer_idx][mid_gy][mid_gx]
                    if not cell.blocked or not cell.pad_blocked:
                        accessible = True
                        break
                if not accessible:
                    continue

                # Check that the first hop (pad -> intermediate) passes
                # relaxed clearance (using flat threshold, not per-component)
                hop1 = Segment(
                    x1=pad.x, y1=pad.y,
                    x2=mid_x, y2=mid_y,
                    width=width, layer=layer,
                    net=pad.net, net_name=pad.net_name,
                )
                if not self._validate_segment_relaxed(
                    hop1, pad.net, relaxed_clearance,
                ):
                    continue

                # Now check that a free grid cell is reachable from the
                # intermediate point within 1-2 cells (the second hop).
                # We only need the intermediate to be adjacent to a free cell.
                for dy2 in range(-2, 3):
                    for dx2 in range(-2, 3):
                        if dx2 == 0 and dy2 == 0:
                            continue
                        tgt_gx = mid_gx + dx2
                        tgt_gy = mid_gy + dy2

                        if not (0 <= tgt_gx < self.grid.cols and 0 <= tgt_gy < self.grid.rows):
                            continue

                        # Target must be a free cell (not blocked at all)
                        target_free = False
                        for layer_idx in check_layers:
                            cell = self.grid.grid[layer_idx][tgt_gy][tgt_gx]
                            if not cell.blocked:
                                target_free = True
                                break
                        if not target_free:
                            continue

                        # The escape terminates at the intermediate point.
                        # apply_escape_segments will unblock it, and the
                        # main router can then path from there to the target.
                        return SubGridEscape(
                            pad=pad,
                            segment=hop1,
                            grid_point=(mid_gx, mid_gy),
                            snap_point=(mid_x, mid_y),
                        )

        return None

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
