"""TraceOptimizer class for PCB trace cleanup and simplification."""

from __future__ import annotations

from ..layers import Layer
from ..primitives import Route, Segment
from .algorithms import (
    _find_staircase_end,
    _optimal_path,
    compress_staircase,
    convert_corners_45,
    eliminate_zigzags,
    merge_collinear,
)
from .chain import sort_into_chains
from .collision import CollisionChecker
from .config import OptimizationConfig, OptimizationStats
from .geometry import (
    angle_between,
    count_corners,
    is_90_degree_corner,
    is_connected,
    is_zigzag,
    same_direction,
    segment_direction,
    segments_touch,
    shorten_segment_end,
    shorten_segment_start,
    total_length,
)
from .pcb import optimize_pcb, parse_net_names, parse_segments, replace_segments


class TraceOptimizer:
    """Optimizer for PCB trace cleanup and simplification.

    Optionally uses a collision checker to ensure optimizations don't
    create DRC violations (shorts, track crossings with other nets).
    When a collision checker is provided, optimizations that would create
    collisions are skipped, preserving the original path.

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

    def __init__(
        self,
        config: OptimizationConfig | None = None,
        collision_checker: CollisionChecker | None = None,
    ):
        """Initialize the trace optimizer.

        Args:
            config: Optimization configuration. Uses defaults if None.
            collision_checker: Optional collision checker for DRC-safe optimization.
                When provided, optimizations that would create collisions are skipped.
                When None, no collision checking is performed (original behavior).
        """
        self.config = config or OptimizationConfig()
        self.collision_checker = collision_checker

    def optimize_segments(self, segments: list[Segment]) -> list[Segment]:
        """Optimize a list of segments for a single net/layer.

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
        """Merge adjacent collinear segments.

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
        return merge_collinear(segments, self.config, self._path_is_clear)

    def eliminate_zigzags(self, segments: list[Segment]) -> list[Segment]:
        """Remove unnecessary zigzag patterns.

        Identifies segments where the path backtracks and removes
        the unnecessary detour, but only if the shortcut path is clear.

        Args:
            segments: List of segments to process.

        Returns:
            List with zigzags eliminated.
        """
        return eliminate_zigzags(segments, self.config, self._path_is_clear)

    def compress_staircase(self, segments: list[Segment]) -> list[Segment]:
        """Compress staircase patterns into optimal diagonal+orthogonal paths.

        Identifies runs of segments alternating between two directions
        (e.g., horizontal and 45° diagonal) and replaces them with an
        optimal 2-3 segment path, but only if the replacement is clear.

        Args:
            segments: List of segments to process.

        Returns:
            List with staircase patterns compressed.
        """
        return compress_staircase(segments, self.config, self._path_is_clear)

    def convert_corners_45(self, segments: list[Segment]) -> list[Segment]:
        """Convert 90-degree corners to 45-degree chamfers.

        Replaces sharp 90-degree turns with smoother 45-degree entry/exit,
        but only if the chamfer path is clear of obstacles.

        Args:
            segments: List of segments to process.

        Returns:
            List with corners converted to 45 degrees.
        """
        return convert_corners_45(segments, self.config, self._path_is_clear)

    def optimize_route(self, route: Route) -> Route:
        """Optimize a complete route.

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
        """Optimize traces in a PCB file.

        Args:
            pcb_path: Path to input .kicad_pcb file.
            output_path: Path for output file. If None, modifies in place.
            net_filter: Only optimize nets matching this pattern.
            dry_run: If True, calculate stats but don't write output.

        Returns:
            Statistics about the optimization.
        """
        return optimize_pcb(
            pcb_path=pcb_path,
            output_path=output_path,
            optimize_fn=self.optimize_segments,
            config=self.config,
            net_filter=net_filter,
            dry_run=dry_run,
        )

    # =========================================================================
    # Helper methods exposed for tests
    # =========================================================================

    def _is_connected(self, s1: Segment, s2: Segment) -> bool:
        """Check if end of s1 connects to start of s2."""
        return is_connected(s1, s2, self.config.tolerance)

    def _segments_touch(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments share any endpoint (regardless of direction)."""
        return segments_touch(s1, s2, self.config.tolerance)

    def _sort_into_chains(self, segments: list[Segment]) -> list[list[Segment]]:
        """Sort segments into connected chains."""
        return sort_into_chains(segments, self.config.tolerance)

    def _same_direction(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments have the same direction."""
        return same_direction(s1, s2, self.config.tolerance)

    def _is_zigzag(self, s1: Segment, s2: Segment, s3: Segment) -> bool:
        """Check if s2 is a zigzag (backtrack) between s1 and s3."""
        return is_zigzag(s1, s2, s3, self.config.tolerance)

    def _angle_between(self, s1: Segment, s2: Segment) -> float:
        """Calculate angle between two segments in degrees (0-180)."""
        return angle_between(s1, s2, self.config.tolerance)

    def _is_90_degree_corner(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments form a 90-degree corner."""
        return is_90_degree_corner(s1, s2)

    def _shorten_segment_end(self, seg: Segment, amount: float) -> Segment | None:
        """Shorten a segment from its end by the given amount."""
        return shorten_segment_end(seg, amount, self.config.min_segment_length)

    def _shorten_segment_start(self, seg: Segment, amount: float) -> Segment | None:
        """Shorten a segment from its start by the given amount."""
        return shorten_segment_start(seg, amount, self.config.min_segment_length)

    def _count_corners(self, segments: list[Segment]) -> int:
        """Count number of corners (direction changes) in a segment list."""
        return count_corners(segments, self.config.tolerance)

    def _total_length(self, segments: list[Segment]) -> float:
        """Calculate total length of segments."""
        return total_length(segments)

    def _segment_direction(self, seg: Segment) -> float:
        """Calculate the direction of a segment in degrees (0-360)."""
        return segment_direction(seg, self.config.tolerance)

    def _find_staircase_end(self, segments: list[Segment], start_idx: int) -> int:
        """Find the end index of a staircase pattern starting at start_idx."""
        return _find_staircase_end(segments, start_idx, self.config)

    def _optimal_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        template: Segment,
    ) -> list[Segment]:
        """Generate an optimal 2-3 segment path from start to end."""
        return _optimal_path(start, end, template, self.config)

    def _parse_net_names(self, pcb_text: str) -> dict[int, str]:
        """Parse net ID to name mapping from PCB file."""
        return parse_net_names(pcb_text)

    def _parse_segments(self, pcb_text: str) -> dict[str, list[Segment]]:
        """Parse segments from PCB file text, grouped by net name."""
        return parse_segments(pcb_text)

    def _replace_segments(
        self,
        pcb_text: str,
        original: dict[str, list[Segment]],
        optimized: dict[str, list[Segment]],
    ) -> str:
        """Replace original segments with optimized ones in PCB text."""
        return replace_segments(pcb_text, original, optimized)
