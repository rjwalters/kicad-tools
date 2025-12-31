"""
Trace optimizer for post-routing cleanup.

Provides algorithms to optimize routed traces:
- Collinear segment merging (combine same-direction segments)
- Zigzag elimination (remove unnecessary back-and-forth)
- Staircase compression (compress alternating horizontal/diagonal patterns)
- 45-degree corner conversion (smooth 90-degree turns)

Collision detection is supported to prevent optimizations that would
create DRC violations (shorts, track crossings).

Example::

    from kicad_tools.router import TraceOptimizer, OptimizationConfig

    # Optimize a route in memory (no collision checking)
    optimizer = TraceOptimizer()
    optimized_route = optimizer.optimize_route(route)

    # Optimize with collision checking
    from kicad_tools.router import GridCollisionChecker
    checker = GridCollisionChecker(grid)
    optimizer = TraceOptimizer(collision_checker=checker)
    optimized_route = optimizer.optimize_route(route)

    # Optimize traces in a PCB file
    stats = optimizer.optimize_pcb("board.kicad_pcb", output="optimized.kicad_pcb")
    print(f"Reduced segments from {stats['before']} to {stats['after']}")
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .layers import Layer
from .primitives import Route, Segment

if TYPE_CHECKING:
    from .grid import RoutingGrid


class CollisionChecker(Protocol):
    """Protocol for checking if a path is clear of obstacles.

    Implementations can use different strategies:
    - Grid-based: Use RoutingGrid obstacle data
    - Segment intersection: Check for crossings with other nets
    - Quadtree: Spatial indexing for efficient queries

    The collision checker should return True if the path is clear,
    False if it would cross obstacles or other nets.
    """

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path from (x1, y1) to (x2, y2) is clear of obstacles.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks (own net).

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        ...


class GridCollisionChecker:
    """Collision checker using the routing grid.

    Uses the RoutingGrid's obstacle data to check if paths are clear.
    This reuses the same collision detection logic as the autorouter.
    """

    def __init__(self, grid: RoutingGrid):
        """Initialize with a routing grid.

        Args:
            grid: The routing grid with obstacle and net data.
        """
        self.grid = grid

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear using grid-based collision detection.

        Uses Bresenham's line algorithm to check all grid cells along the path,
        including a buffer for trace width and clearance.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks.

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        # Convert to grid coordinates
        gx1, gy1 = self.grid.world_to_grid(x1, y1)
        gx2, gy2 = self.grid.world_to_grid(x2, y2)

        # Calculate clearance buffer in grid cells
        total_clearance = width / 2 + self.grid.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Get layer index
        try:
            layer_idx = self.grid.layer_to_index(layer.value)
        except Exception:
            return False  # Invalid layer

        # Check all cells along the path using Bresenham's algorithm
        cells_to_check = self._get_path_cells(gx1, gy1, gx2, gy2, clearance_cells)

        for gx, gy in cells_to_check:
            if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                continue  # Out of bounds - skip but don't fail

            cell = self.grid.grid[layer_idx][gy][gx]

            # Check if blocked by another net
            if cell.blocked:
                # Cell is blocked - check if it's our net or another net
                if cell.net != 0 and cell.net != exclude_net:
                    return False  # Blocked by another net
                if cell.is_obstacle:
                    return False  # Hard obstacle (pad, keepout)

        return True

    def _get_path_cells(
        self, gx1: int, gy1: int, gx2: int, gy2: int, clearance: int
    ) -> list[tuple[int, int]]:
        """Get all grid cells along a path with clearance buffer.

        Uses Bresenham's line algorithm with clearance expansion.

        Args:
            gx1, gy1: Start grid coordinates.
            gx2, gy2: End grid coordinates.
            clearance: Clearance buffer in grid cells.

        Returns:
            List of (gx, gy) grid coordinates to check.
        """
        cells: set[tuple[int, int]] = set()

        # Bresenham's line algorithm
        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy

        gx, gy = gx1, gy1
        while True:
            # Add cell and clearance buffer
            for cy in range(-clearance, clearance + 1):
                for cx in range(-clearance, clearance + 1):
                    cells.add((gx + cx, gy + cy))

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy

        return list(cells)


@dataclass
class OptimizationConfig:
    """Configuration for trace optimization."""

    merge_collinear: bool = True
    """Merge adjacent segments with the same direction."""

    eliminate_zigzags: bool = True
    """Remove unnecessary back-and-forth patterns."""

    convert_45_corners: bool = True
    """Convert 90-degree corners to 45-degree chamfers."""

    compress_staircase: bool = True
    """Compress staircase patterns (alternating horizontal/diagonal) into optimal paths."""

    min_staircase_segments: int = 3
    """Minimum number of segments to consider as a staircase pattern."""

    min_segment_length: float = 0.05
    """Minimum segment length to keep (mm). Shorter segments may be merged."""

    corner_chamfer_size: float = 0.5
    """Size of 45-degree chamfer at corners (mm)."""

    tolerance: float = 1e-4
    """Tolerance for floating-point comparisons (mm)."""


@dataclass
class OptimizationStats:
    """Statistics from trace optimization."""

    segments_before: int = 0
    segments_after: int = 0
    corners_before: int = 0
    corners_after: int = 0
    length_before: float = 0.0
    length_after: float = 0.0
    nets_optimized: int = 0

    @property
    def segment_reduction(self) -> float:
        """Percentage reduction in segment count."""
        if self.segments_before == 0:
            return 0.0
        return (1 - self.segments_after / self.segments_before) * 100

    @property
    def length_reduction(self) -> float:
        """Percentage reduction in total length."""
        if self.length_before == 0:
            return 0.0
        return (1 - self.length_after / self.length_before) * 100


class TraceOptimizer:
    """Optimizer for PCB trace cleanup and simplification.

    Optionally uses a collision checker to ensure optimizations don't
    create DRC violations (shorts, track crossings with other nets).
    When a collision checker is provided, optimizations that would create
    collisions are skipped, preserving the original path.
    """

    def __init__(
        self,
        config: OptimizationConfig | None = None,
        collision_checker: CollisionChecker | None = None,
    ):
        """
        Initialize the trace optimizer.

        Args:
            config: Optimization configuration. Uses defaults if None.
            collision_checker: Optional collision checker for DRC-safe optimization.
                When provided, optimizations that would create collisions are skipped.
                When None, no collision checking is performed (original behavior).
        """
        self.config = config or OptimizationConfig()
        self.collision_checker = collision_checker

    def optimize_segments(self, segments: list[Segment]) -> list[Segment]:
        """
        Optimize a list of segments for a single net/layer.

        Applies enabled optimizations in order:
        1. Sort segments into connected chains (to avoid cross-chain shortcuts)
        2. Collinear segment merging
        3. Zigzag elimination
        4. Staircase compression
        5. 45-degree corner conversion

        Args:
            segments: List of segments to optimize (may contain multiple chains).

        Returns:
            Optimized list of segments.
        """
        if not segments:
            return []

        # Sort segments into connected chains to prevent cross-chain shortcuts
        chains = self._sort_into_chains(segments)

        # Optimize each chain independently
        all_optimized: list[Segment] = []
        for chain in chains:
            result = list(chain)

            # Apply optimizations in order
            if self.config.merge_collinear:
                result = self.merge_collinear(result)

            if self.config.eliminate_zigzags:
                result = self.eliminate_zigzags(result)

            if self.config.compress_staircase:
                result = self.compress_staircase(result)

            if self.config.convert_45_corners:
                result = self.convert_corners_45(result)

            all_optimized.extend(result)

        return all_optimized

    def _path_is_clear(self, seg: Segment) -> bool:
        """Check if a segment's path is clear using the collision checker.

        Args:
            seg: The segment to check.

        Returns:
            True if path is clear (or no collision checker), False if blocked.
        """
        if self.collision_checker is None:
            return True  # No collision checking - allow all paths

        return self.collision_checker.path_is_clear(
            x1=seg.x1,
            y1=seg.y1,
            x2=seg.x2,
            y2=seg.y2,
            layer=seg.layer,
            width=seg.width,
            exclude_net=seg.net,
        )

    def merge_collinear(self, segments: list[Segment]) -> list[Segment]:
        """
        Merge adjacent collinear segments.

        Combines segments that:
        - Are connected (end of one matches start of next)
        - Have the same direction
        - Are on the same layer
        - Would not cross obstacles (if collision checker provided)

        Args:
            segments: List of segments to merge.

        Returns:
            List with collinear segments merged.
        """
        if len(segments) < 2:
            return list(segments)

        result: list[Segment] = []
        current = segments[0]

        for next_seg in segments[1:]:
            # Check if segments can be merged
            if (
                self._is_connected(current, next_seg)
                and self._same_direction(current, next_seg)
                and current.layer == next_seg.layer
                and current.net == next_seg.net
            ):
                # Create candidate merged segment
                merged = Segment(
                    x1=current.x1,
                    y1=current.y1,
                    x2=next_seg.x2,
                    y2=next_seg.y2,
                    width=current.width,
                    layer=current.layer,
                    net=current.net,
                    net_name=current.net_name,
                )
                # Only merge if the extended path is clear
                if self._path_is_clear(merged):
                    current = merged
                else:
                    # Collision detected - keep segments separate
                    result.append(current)
                    current = next_seg
            else:
                # Can't merge, save current and start new
                result.append(current)
                current = next_seg

        result.append(current)
        return result

    def eliminate_zigzags(self, segments: list[Segment]) -> list[Segment]:
        """
        Remove unnecessary zigzag patterns.

        Identifies segments where the path backtracks and removes
        the unnecessary detour, but only if the shortcut path is clear.

        Args:
            segments: List of segments to process.

        Returns:
            List with zigzags eliminated.
        """
        if len(segments) < 3:
            return list(segments)

        result: list[Segment] = [segments[0]]
        i = 1

        while i < len(segments) - 1:
            prev = result[-1]
            curr = segments[i]
            next_seg = segments[i + 1]

            # Check if curr is a zigzag (backtrack)
            if self._is_zigzag(prev, curr, next_seg):
                # Create candidate shortcut segment
                shortcut = Segment(
                    x1=prev.x1,
                    y1=prev.y1,
                    x2=curr.x2,  # Connect to where curr ends
                    y2=curr.y2,
                    width=prev.width,
                    layer=prev.layer,
                    net=prev.net,
                    net_name=prev.net_name,
                )
                # Only eliminate zigzag if the shortcut path is clear
                if self._path_is_clear(shortcut):
                    result[-1] = shortcut
                    i += 1  # Skip curr
                else:
                    # Collision detected - keep the zigzag
                    result.append(curr)
                    i += 1
            else:
                result.append(curr)
                i += 1

        # Add the last segment
        if segments:
            result.append(segments[-1])

        return result

    def compress_staircase(self, segments: list[Segment]) -> list[Segment]:
        """
        Compress staircase patterns into optimal diagonal+orthogonal paths.

        Identifies runs of segments alternating between two directions
        (e.g., horizontal and 45° diagonal) and replaces them with an
        optimal 2-3 segment path, but only if the replacement is clear.

        Args:
            segments: List of segments to process.

        Returns:
            List with staircase patterns compressed.
        """
        if not self.config.compress_staircase:
            return list(segments)

        if len(segments) < self.config.min_staircase_segments:
            return list(segments)

        result: list[Segment] = []
        i = 0

        while i < len(segments):
            # Look for staircase pattern starting at i
            staircase_end = self._find_staircase_end(segments, i)

            if staircase_end - i >= self.config.min_staircase_segments:
                # Found a staircase of sufficient length
                start_point = (segments[i].x1, segments[i].y1)
                end_point = (segments[staircase_end - 1].x2, segments[staircase_end - 1].y2)

                # Generate optimal replacement path
                template = segments[i]
                replacement = self._optimal_path(start_point, end_point, template)

                # Check if all replacement segments are clear
                all_clear = all(self._path_is_clear(seg) for seg in replacement)

                if all_clear and replacement:
                    result.extend(replacement)
                else:
                    # Collision detected - keep original staircase segments
                    for j in range(i, staircase_end):
                        result.append(segments[j])
                i = staircase_end
            else:
                # Not a staircase or too short, keep the segment
                result.append(segments[i])
                i += 1

        return result

    def _find_staircase_end(self, segments: list[Segment], start_idx: int) -> int:
        """
        Find the end index of a staircase pattern starting at start_idx.

        A staircase is a run of segments alternating between two directions
        that are approximately 45° apart (e.g., 0° and 45°, or 180° and 135°).

        Args:
            segments: List of all segments.
            start_idx: Index to start looking from.

        Returns:
            End index (exclusive) of the staircase pattern.
        """
        if start_idx >= len(segments) - 1:
            return start_idx + 1

        # Get the two alternating directions
        dir1 = self._segment_direction(segments[start_idx])
        dir2 = self._segment_direction(segments[start_idx + 1])

        # Check if they form a valid staircase pair (approximately 45° apart)
        angle_diff = abs(dir1 - dir2)
        # Handle wraparound (e.g., 350° and 10° are 20° apart, not 340°)
        if angle_diff > 180:
            angle_diff = 360 - angle_diff

        # Valid staircase: directions should be ~45° apart (allow ±15° tolerance)
        if not (30 <= angle_diff <= 60):
            return start_idx + 1

        # Find how far the alternating pattern continues
        i = start_idx + 2
        while i < len(segments):
            dir_i = self._segment_direction(segments[i])
            # Expect alternating pattern: dir1, dir2, dir1, dir2, ...
            expected = dir1 if (i - start_idx) % 2 == 0 else dir2

            # Check if direction matches expected (with tolerance)
            diff = abs(dir_i - expected)
            if diff > 180:
                diff = 360 - diff
            if diff > 15:  # Tolerance for direction matching
                break
            i += 1

        return i

    def _segment_direction(self, seg: Segment) -> float:
        """
        Calculate the direction of a segment in degrees (0-360).

        0° is positive X (right), 90° is positive Y (up),
        180° is negative X (left), 270° is negative Y (down).

        Args:
            seg: The segment to analyze.

        Returns:
            Direction in degrees (0-360).
        """
        dx = seg.x2 - seg.x1
        dy = seg.y2 - seg.y1

        if abs(dx) < self.config.tolerance and abs(dy) < self.config.tolerance:
            return 0.0  # Zero-length segment

        angle = math.degrees(math.atan2(dy, dx))
        if angle < 0:
            angle += 360
        return angle

    def _optimal_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        template: Segment,
    ) -> list[Segment]:
        """
        Generate an optimal 2-3 segment path from start to end.

        Uses 45° diagonal routing to minimize segment count while
        maintaining connectivity.

        Args:
            start: Starting point (x, y).
            end: Ending point (x, y).
            template: Template segment for properties (width, layer, net, net_name).

        Returns:
            List of 1-3 segments forming the optimal path.
        """
        dx = end[0] - start[0]
        dy = end[1] - start[1]

        # Handle degenerate cases
        if abs(dx) < self.config.tolerance and abs(dy) < self.config.tolerance:
            return []  # Start and end are the same point

        # Calculate the diagonal and remaining orthogonal distances
        abs_dx = abs(dx)
        abs_dy = abs(dy)

        # The diagonal distance covers the smaller of |dx| and |dy|
        diag_dist = min(abs_dx, abs_dy)

        # Determine diagonal direction based on signs of dx and dy
        diag_dx = math.copysign(diag_dist, dx)
        diag_dy = math.copysign(diag_dist, dy)

        # Calculate intermediate point after diagonal segment
        mid_x = start[0] + diag_dx
        mid_y = start[1] + diag_dy

        result: list[Segment] = []

        # Create diagonal segment if there's diagonal distance
        if diag_dist > self.config.tolerance:
            diag_seg = Segment(
                x1=start[0],
                y1=start[1],
                x2=mid_x,
                y2=mid_y,
                width=template.width,
                layer=template.layer,
                net=template.net,
                net_name=template.net_name,
            )
            result.append(diag_seg)

        # Create orthogonal segment for remaining distance
        remaining_dx = end[0] - mid_x
        remaining_dy = end[1] - mid_y

        if abs(remaining_dx) > self.config.tolerance or abs(remaining_dy) > self.config.tolerance:
            ortho_seg = Segment(
                x1=mid_x,
                y1=mid_y,
                x2=end[0],
                y2=end[1],
                width=template.width,
                layer=template.layer,
                net=template.net,
                net_name=template.net_name,
            )
            result.append(ortho_seg)

        # If we couldn't create any segments, create a direct connection
        if not result:
            result.append(Segment(
                x1=start[0],
                y1=start[1],
                x2=end[0],
                y2=end[1],
                width=template.width,
                layer=template.layer,
                net=template.net,
                net_name=template.net_name,
            ))

        return result

    def convert_corners_45(self, segments: list[Segment]) -> list[Segment]:
        """
        Convert 90-degree corners to 45-degree chamfers.

        Replaces sharp 90-degree turns with smoother 45-degree entry/exit,
        but only if the chamfer path is clear of obstacles.

        Args:
            segments: List of segments to process.

        Returns:
            List with corners converted to 45 degrees.
        """
        if len(segments) < 2:
            return list(segments)

        result: list[Segment] = []
        chamfer = self.config.corner_chamfer_size

        for i, seg in enumerate(segments):
            if i == 0:
                # First segment - check if next segment forms 90-degree corner
                if i + 1 < len(segments):
                    next_seg = segments[i + 1]
                    if self._is_90_degree_corner(seg, next_seg):
                        # Shorten this segment to leave room for chamfer
                        shortened = self._shorten_segment_end(seg, chamfer)
                        if shortened:
                            result.append(shortened)
                        else:
                            result.append(seg)
                    else:
                        result.append(seg)
                else:
                    result.append(seg)

            elif i == len(segments) - 1:
                # Last segment - check if prev segment forms 90-degree corner
                prev_seg = segments[i - 1]
                if self._is_90_degree_corner(prev_seg, seg):
                    # Shorten start of this segment
                    shortened = self._shorten_segment_start(seg, chamfer)
                    if shortened and result:
                        # Add chamfer segment connecting prev end to this start
                        chamfer_seg = Segment(
                            x1=result[-1].x2,
                            y1=result[-1].y2,
                            x2=shortened.x1,
                            y2=shortened.y1,
                            width=seg.width,
                            layer=seg.layer,
                            net=seg.net,
                            net_name=seg.net_name,
                        )
                        # Only add chamfer if path is clear
                        if self._path_is_clear(chamfer_seg):
                            result.append(chamfer_seg)
                            result.append(shortened)
                        else:
                            # Collision - keep original segment
                            result.append(seg)
                    else:
                        result.append(seg)
                else:
                    result.append(seg)

            else:
                # Middle segment - check both corners
                prev_seg = segments[i - 1]
                next_seg = segments[i + 1]

                modified_seg = seg

                # Handle corner with previous segment
                if self._is_90_degree_corner(prev_seg, seg) and result:
                    shortened = self._shorten_segment_start(modified_seg, chamfer)
                    if shortened:
                        # Add chamfer
                        chamfer_seg = Segment(
                            x1=result[-1].x2,
                            y1=result[-1].y2,
                            x2=shortened.x1,
                            y2=shortened.y1,
                            width=seg.width,
                            layer=seg.layer,
                            net=seg.net,
                            net_name=seg.net_name,
                        )
                        # Only add chamfer if path is clear
                        if self._path_is_clear(chamfer_seg):
                            result.append(chamfer_seg)
                            modified_seg = shortened

                # Handle corner with next segment
                if self._is_90_degree_corner(seg, next_seg):
                    shortened = self._shorten_segment_end(modified_seg, chamfer)
                    if shortened:
                        modified_seg = shortened

                result.append(modified_seg)

        return result

    def optimize_route(self, route: Route) -> Route:
        """
        Optimize a complete route.

        Segments are grouped by layer and then sorted into connected chains
        before optimization. This prevents optimization from creating
        shortcuts between unconnected parts of the route.

        Args:
            route: Route to optimize.

        Returns:
            New Route with optimized segments.
        """
        # Group segments by layer for optimization
        segments_by_layer: dict[Layer, list[Segment]] = {}
        for seg in route.segments:
            if seg.layer not in segments_by_layer:
                segments_by_layer[seg.layer] = []
            segments_by_layer[seg.layer].append(seg)

        # Optimize each layer's segments (chain sorting happens in optimize_segments)
        optimized_segments: list[Segment] = []
        for _layer, segs in segments_by_layer.items():
            optimized = self.optimize_segments(segs)
            optimized_segments.extend(optimized)

        return Route(
            net=route.net,
            net_name=route.net_name,
            segments=optimized_segments,
            vias=list(route.vias),  # Vias unchanged
        )

    def optimize_pcb(
        self,
        pcb_path: str,
        output_path: str | None = None,
        net_filter: str | None = None,
        dry_run: bool = False,
    ) -> OptimizationStats:
        """
        Optimize traces in a PCB file.

        Args:
            pcb_path: Path to input .kicad_pcb file.
            output_path: Path for output file. If None, modifies in place.
            net_filter: Only optimize nets matching this pattern.
            dry_run: If True, calculate stats but don't write output.

        Returns:
            Statistics about the optimization.
        """
        pcb_text = Path(pcb_path).read_text()
        stats = OptimizationStats()

        # Parse existing segments
        segments_by_net = self._parse_segments(pcb_text)

        # Filter nets if requested
        if net_filter:
            segments_by_net = {
                net: segs
                for net, segs in segments_by_net.items()
                if net_filter.lower() in net.lower()
            }

        # Calculate before stats
        for net, segs in segments_by_net.items():
            stats.segments_before += len(segs)
            stats.corners_before += self._count_corners(segs)
            stats.length_before += self._total_length(segs)

        # Optimize each net
        optimized_segments: dict[str, list[Segment]] = {}
        for net, segs in segments_by_net.items():
            optimized = self.optimize_segments(segs)
            optimized_segments[net] = optimized
            stats.nets_optimized += 1

        # Calculate after stats
        for net, segs in optimized_segments.items():
            stats.segments_after += len(segs)
            stats.corners_after += self._count_corners(segs)
            stats.length_after += self._total_length(segs)

        # Generate output (only if not dry run)
        if not dry_run:
            output_text = self._replace_segments(pcb_text, segments_by_net, optimized_segments)
            out_path = output_path or pcb_path
            Path(out_path).write_text(output_text)

        return stats

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _is_connected(self, s1: Segment, s2: Segment) -> bool:
        """Check if end of s1 connects to start of s2."""
        tol = self.config.tolerance
        return abs(s1.x2 - s2.x1) < tol and abs(s1.y2 - s2.y1) < tol

    def _segments_touch(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments share any endpoint (regardless of direction)."""
        tol = self.config.tolerance

        # Check all four possible endpoint connections
        # s1.end -> s2.start
        if abs(s1.x2 - s2.x1) < tol and abs(s1.y2 - s2.y1) < tol:
            return True
        # s1.end -> s2.end
        if abs(s1.x2 - s2.x2) < tol and abs(s1.y2 - s2.y2) < tol:
            return True
        # s1.start -> s2.start
        if abs(s1.x1 - s2.x1) < tol and abs(s1.y1 - s2.y1) < tol:
            return True
        # s1.start -> s2.end
        if abs(s1.x1 - s2.x2) < tol and abs(s1.y1 - s2.y2) < tol:
            return True

        return False

    def _sort_into_chains(self, segments: list[Segment]) -> list[list[Segment]]:
        """
        Sort segments into connected chains.

        Groups segments that form continuous paths. Segments that share
        endpoints belong to the same chain. This prevents optimization
        from creating shortcuts between unconnected segments.

        Args:
            segments: List of segments to sort.

        Returns:
            List of chains, where each chain is a list of connected segments.
        """
        if not segments:
            return []

        if len(segments) == 1:
            return [list(segments)]

        # Track which segments have been assigned to a chain
        remaining = set(range(len(segments)))
        chains: list[list[Segment]] = []

        while remaining:
            # Start a new chain with an arbitrary remaining segment
            start_idx = next(iter(remaining))
            remaining.remove(start_idx)

            chain_indices = [start_idx]

            # Grow the chain by finding connected segments
            changed = True
            while changed:
                changed = False
                for idx in list(remaining):
                    seg = segments[idx]
                    # Check if this segment connects to any segment in the chain
                    for chain_idx in chain_indices:
                        if self._segments_touch(segments[chain_idx], seg):
                            chain_indices.append(idx)
                            remaining.remove(idx)
                            changed = True
                            break

            # Sort chain segments into path order
            chain_segments = [segments[i] for i in chain_indices]
            sorted_chain = self._sort_chain_segments(chain_segments)
            chains.append(sorted_chain)

        return chains

    def _sort_chain_segments(self, segments: list[Segment]) -> list[Segment]:
        """
        Sort segments within a chain into connected path order.

        Arranges segments so that each segment's end connects to the
        next segment's start, forming a continuous path.

        Args:
            segments: List of segments belonging to the same chain.

        Returns:
            Segments sorted in path order.
        """
        if len(segments) <= 1:
            return list(segments)

        tol = self.config.tolerance

        # Build adjacency: for each segment, find what connects to its endpoints
        # We'll find a segment that has an endpoint not shared with any other segment
        # (a "tip" of the chain) and traverse from there.

        result: list[Segment] = []
        remaining = list(segments)

        # Find a starting segment (one with an endpoint that's not shared)
        def find_chain_tip() -> int:
            """Find a segment at the tip of the chain (has an unshared endpoint)."""
            for i, seg in enumerate(remaining):
                # Check if seg's start point is shared with any other segment
                start_shared = False
                end_shared = False
                for j, other in enumerate(remaining):
                    if i == j:
                        continue
                    # Check if start of seg matches any endpoint of other
                    if (abs(seg.x1 - other.x1) < tol and abs(seg.y1 - other.y1) < tol) or \
                       (abs(seg.x1 - other.x2) < tol and abs(seg.y1 - other.y2) < tol):
                        start_shared = True
                    # Check if end of seg matches any endpoint of other
                    if (abs(seg.x2 - other.x1) < tol and abs(seg.y2 - other.y1) < tol) or \
                       (abs(seg.x2 - other.x2) < tol and abs(seg.y2 - other.y2) < tol):
                        end_shared = True

                # If start is not shared, this is a good starting point
                if not start_shared:
                    return i
                # If end is not shared but start is, we can use this (will need to traverse)
                if not end_shared:
                    return i

            # All endpoints are shared (could be a loop), just pick first
            return 0

        # Start from a tip
        start_idx = find_chain_tip()
        current = remaining.pop(start_idx)

        # Ensure segment is oriented so we're starting from an unshared endpoint
        # Check if current.start is shared with remaining segments
        start_shared = any(
            (abs(current.x1 - other.x1) < tol and abs(current.y1 - other.y1) < tol) or
            (abs(current.x1 - other.x2) < tol and abs(current.y1 - other.y2) < tol)
            for other in remaining
        )
        if start_shared:
            # Flip the segment so we start from the unshared end
            current = Segment(
                x1=current.x2,
                y1=current.y2,
                x2=current.x1,
                y2=current.y1,
                width=current.width,
                layer=current.layer,
                net=current.net,
                net_name=current.net_name,
            )

        result.append(current)

        # Traverse the chain, finding segments that connect to the current end
        while remaining:
            found = False
            for i, seg in enumerate(remaining):
                # Check if seg's start connects to current's end
                if abs(current.x2 - seg.x1) < tol and abs(current.y2 - seg.y1) < tol:
                    current = remaining.pop(i)
                    result.append(current)
                    found = True
                    break
                # Check if seg's end connects to current's end (need to flip seg)
                if abs(current.x2 - seg.x2) < tol and abs(current.y2 - seg.y2) < tol:
                    seg = remaining.pop(i)
                    current = Segment(
                        x1=seg.x2,
                        y1=seg.y2,
                        x2=seg.x1,
                        y2=seg.y1,
                        width=seg.width,
                        layer=seg.layer,
                        net=seg.net,
                        net_name=seg.net_name,
                    )
                    result.append(current)
                    found = True
                    break

            if not found:
                # Remaining segments aren't connected to current chain end
                # This shouldn't happen for a properly connected chain,
                # but handle gracefully by just appending the rest
                result.extend(remaining)
                break

        return result

    def _same_direction(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments have the same direction."""
        dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
        dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

        # Handle zero-length segments
        len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
        len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

        if len1 < self.config.tolerance or len2 < self.config.tolerance:
            return True  # Zero-length segments are "same direction"

        # Normalize
        dx1, dy1 = dx1 / len1, dy1 / len1
        dx2, dy2 = dx2 / len2, dy2 / len2

        # Cross product should be ~0 for parallel
        cross = abs(dx1 * dy2 - dy1 * dx2)
        # Dot product should be positive (same direction, not opposite)
        dot = dx1 * dx2 + dy1 * dy2

        return cross < 0.01 and dot > 0

    def _is_zigzag(self, s1: Segment, s2: Segment, s3: Segment) -> bool:
        """Check if s2 is a zigzag (backtrack) between s1 and s3."""
        # Calculate angles
        angle12 = self._angle_between(s1, s2)
        self._angle_between(s2, s3)

        # Zigzag: s2 goes roughly opposite to s1, then s3 continues roughly same as s1
        # This means angle12 is close to 180 degrees
        return abs(angle12 - 180) < 30

    def _angle_between(self, s1: Segment, s2: Segment) -> float:
        """Calculate angle between two segments in degrees (0-180)."""
        dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
        dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

        len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
        len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

        if len1 < self.config.tolerance or len2 < self.config.tolerance:
            return 0

        # Dot product
        dot = dx1 * dx2 + dy1 * dy2
        cos_angle = dot / (len1 * len2)
        cos_angle = max(-1, min(1, cos_angle))  # Clamp for numerical stability

        return math.degrees(math.acos(cos_angle))

    def _is_90_degree_corner(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments form a 90-degree corner."""
        angle = self._angle_between(s1, s2)
        return 80 < angle < 100  # Allow some tolerance

    def _shorten_segment_end(self, seg: Segment, amount: float) -> Segment | None:
        """Shorten a segment from its end by the given amount."""
        dx = seg.x2 - seg.x1
        dy = seg.y2 - seg.y1
        length = math.sqrt(dx * dx + dy * dy)

        if length <= amount + self.config.min_segment_length:
            return None  # Can't shorten enough

        # New end point
        ratio = (length - amount) / length
        new_x2 = seg.x1 + dx * ratio
        new_y2 = seg.y1 + dy * ratio

        return Segment(
            x1=seg.x1,
            y1=seg.y1,
            x2=new_x2,
            y2=new_y2,
            width=seg.width,
            layer=seg.layer,
            net=seg.net,
            net_name=seg.net_name,
        )

    def _shorten_segment_start(self, seg: Segment, amount: float) -> Segment | None:
        """Shorten a segment from its start by the given amount."""
        dx = seg.x2 - seg.x1
        dy = seg.y2 - seg.y1
        length = math.sqrt(dx * dx + dy * dy)

        if length <= amount + self.config.min_segment_length:
            return None  # Can't shorten enough

        # New start point
        ratio = amount / length
        new_x1 = seg.x1 + dx * ratio
        new_y1 = seg.y1 + dy * ratio

        return Segment(
            x1=new_x1,
            y1=new_y1,
            x2=seg.x2,
            y2=seg.y2,
            width=seg.width,
            layer=seg.layer,
            net=seg.net,
            net_name=seg.net_name,
        )

    def _count_corners(self, segments: list[Segment]) -> int:
        """Count number of corners (direction changes) in a segment list."""
        if len(segments) < 2:
            return 0

        corners = 0
        for i in range(len(segments) - 1):
            if not self._same_direction(segments[i], segments[i + 1]):
                corners += 1
        return corners

    def _total_length(self, segments: list[Segment]) -> float:
        """Calculate total length of segments."""
        total = 0.0
        for seg in segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            total += math.sqrt(dx * dx + dy * dy)
        return total

    def _parse_net_names(self, pcb_text: str) -> dict[int, str]:
        """Parse net ID to name mapping from PCB file."""
        net_names: dict[int, str] = {}

        # Match net declarations: (net N "name")
        pattern = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
        for match in pattern.finditer(pcb_text):
            net_id = int(match.group(1))
            net_name = match.group(2)
            if net_name:  # Skip empty net names
                net_names[net_id] = net_name

        return net_names

    def _parse_segments(self, pcb_text: str) -> dict[str, list[Segment]]:
        """Parse segments from PCB file text, grouped by net name."""
        segments_by_net: dict[str, list[Segment]] = {}

        # First, build net ID to name mapping
        net_names = self._parse_net_names(pcb_text)

        # Match segment S-expressions (multiline format)
        # (segment
        #     (start X Y)
        #     (end X Y)
        #     (width W)
        #     (layer "L")
        #     (net N)
        #     ...
        # )
        pattern = re.compile(
            r"\(segment\s+"
            r"\(start\s+([\d.-]+)\s+([\d.-]+)\)\s*"
            r"\(end\s+([\d.-]+)\s+([\d.-]+)\)\s*"
            r"\(width\s+([\d.]+)\)\s*"
            r'\(layer\s+"([^"]+)"\)\s*'
            r"\(net\s+(\d+)\)",
            re.DOTALL,
        )

        for match in pattern.finditer(pcb_text):
            x1 = float(match.group(1))
            y1 = float(match.group(2))
            x2 = float(match.group(3))
            y2 = float(match.group(4))
            width = float(match.group(5))
            layer_name = match.group(6)
            net = int(match.group(7))
            net_name = net_names.get(net, f"Net{net}")

            # Convert layer name to Layer enum
            layer = Layer.F_CU  # Default
            for l in Layer:
                if l.kicad_name == layer_name:
                    layer = l
                    break

            seg = Segment(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                width=width,
                layer=layer,
                net=net,
                net_name=net_name,
            )

            if net_name not in segments_by_net:
                segments_by_net[net_name] = []
            segments_by_net[net_name].append(seg)

        return segments_by_net

    def _replace_segments(
        self,
        pcb_text: str,
        original: dict[str, list[Segment]],
        optimized: dict[str, list[Segment]],
    ) -> str:
        """Replace original segments with optimized ones in PCB text."""
        result = pcb_text

        # Get net IDs for each net name
        net_ids_to_remove: set[int] = set()
        for net_name, segs in original.items():
            if net_name in optimized and segs:
                net_ids_to_remove.add(segs[0].net)

        # Remove existing segment blocks for nets we optimized
        # Match the multiline segment format:
        # (segment
        #     (start X Y)
        #     ...
        #     (net N)
        #     ...
        # )
        for net_id in net_ids_to_remove:
            pattern = re.compile(
                r"\(segment\s+[^)]*\(net\s+" + str(net_id) + r"\)[^)]*\)\s*", re.DOTALL
            )
            result = pattern.sub("", result)

        # Add optimized segments before the closing parenthesis
        new_segments_sexp = []
        for net_name, segs in optimized.items():
            for seg in segs:
                new_segments_sexp.append(seg.to_sexp())

        if new_segments_sexp:
            # Find the last ) and insert before it
            insert_pos = result.rfind(")")
            if insert_pos > 0:
                indent = "  "
                new_content = "\n" + indent + f"\n{indent}".join(new_segments_sexp) + "\n"
                result = result[:insert_pos] + new_content + result[insert_pos:]

        return result
