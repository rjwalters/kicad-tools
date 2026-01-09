"""
Serpentine generation for length tuning.

This module provides:
- SerpentineConfig: Configuration for serpentine patterns
- SerpentineGenerator: Generates serpentine patterns to increase trace length
- add_serpentine: Add serpentine to a route to reach target length
- tune_match_group: Adjust routes in a match group to equal lengths

Serpentines (also called meanders) are used to increase trace length
for timing-critical signals like DDR data buses and differential pairs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from ..primitives import Route, Segment
from .geometry import segment_length

if TYPE_CHECKING:
    from ..grid import RoutingGrid


class SerpentineStyle(Enum):
    """Style of serpentine pattern."""

    RECTANGULAR = "rectangular"  # 90-degree corners
    TROMBONE = "trombone"  # U-shaped loops
    SAWTOOTH = "sawtooth"  # Angled zigzag


@dataclass
class SerpentineConfig:
    """Configuration for serpentine pattern generation.

    Attributes:
        style: Pattern style (rectangular, trombone, sawtooth)
        amplitude: Height of each serpentine wave in mm
        min_spacing: Minimum spacing between parallel traces in mm
        min_segment_length: Minimum segment length for serpentine insertion in mm
        gap_factor: Multiplier for spacing (gap = min_spacing * gap_factor)
        max_iterations: Maximum loops to add per segment
    """

    style: SerpentineStyle = SerpentineStyle.TROMBONE
    amplitude: float = 1.0  # mm
    min_spacing: float = 0.2  # mm (clearance)
    min_segment_length: float = 2.0  # mm
    gap_factor: float = 2.0
    max_iterations: int = 20


@dataclass
class SerpentineResult:
    """Result of serpentine generation.

    Attributes:
        success: Whether serpentine was successfully added
        new_segments: New segments with serpentine pattern
        length_added: Additional length added in mm
        num_loops: Number of serpentine loops added
        message: Status message
    """

    success: bool
    new_segments: list[Segment] = field(default_factory=list)
    length_added: float = 0.0
    num_loops: int = 0
    message: str = ""


class SerpentineGenerator:
    """Generates serpentine patterns for length tuning.

    This class finds suitable segments in a route and inserts serpentine
    patterns to increase the total trace length to meet constraints.
    """

    def __init__(self, config: SerpentineConfig | None = None):
        """Initialize serpentine generator.

        Args:
            config: Configuration for serpentine generation
        """
        self.config = config or SerpentineConfig()

    def find_best_segment(self, route: Route) -> tuple[int, Segment] | None:
        """Find the best segment in a route for serpentine insertion.

        Prefers:
        - Longest straight segments (more room for serpentine)
        - Horizontal or vertical segments (easier pattern generation)
        - Segments not near route endpoints (avoid pad connections)

        Args:
            route: Route to analyze

        Returns:
            Tuple of (index, segment) or None if no suitable segment found
        """
        if not route.segments:
            return None

        best_idx = -1
        best_score = 0.0
        best_segment: Segment | None = None

        for i, seg in enumerate(route.segments):
            length = segment_length(seg)

            # Skip segments that are too short
            if length < self.config.min_segment_length:
                continue

            # Score based on length
            score = length

            # Prefer segments not at the start or end (near pads)
            if i > 0 and i < len(route.segments) - 1:
                score *= 1.2

            # Prefer horizontal or vertical segments
            dx = abs(seg.x2 - seg.x1)
            dy = abs(seg.y2 - seg.y1)
            if length > 0:
                # Check if nearly horizontal or vertical
                if dx / length > 0.95 or dy / length > 0.95:
                    score *= 1.5

            if score > best_score:
                best_score = score
                best_idx = i
                best_segment = seg

        if best_idx >= 0 and best_segment:
            return (best_idx, best_segment)
        return None

    def generate_trombone(
        self,
        segment: Segment,
        target_length_add: float,
    ) -> SerpentineResult:
        """Generate a trombone (U-shaped) serpentine pattern.

        The trombone style creates U-shaped detours perpendicular to
        the main trace direction.

        Args:
            segment: Original segment to replace
            target_length_add: Additional length needed in mm

        Returns:
            SerpentineResult with new segments
        """
        if target_length_add <= 0:
            return SerpentineResult(
                success=True,
                new_segments=[segment],
                message="No length addition needed",
            )

        # Get segment direction and perpendicular
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)

        if length < self.config.min_segment_length:
            return SerpentineResult(
                success=False,
                new_segments=[segment],
                message=f"Segment too short ({length:.2f}mm < {self.config.min_segment_length:.2f}mm)",
            )

        # Unit vectors
        ux, uy = dx / length, dy / length  # Along segment
        px, py = -uy, ux  # Perpendicular (90 degrees CCW)

        # Calculate number of loops needed
        amplitude = self.config.amplitude
        gap = self.config.min_spacing * self.config.gap_factor

        # Each trombone loop adds approximately: 2 * amplitude + gap
        # Actually: forward travel + up + back + down = gap + amplitude + gap + amplitude
        # Net forward: gap
        # Added length per loop: 2 * amplitude + gap - gap = 2 * amplitude
        # But we also need the connecting segments, so:
        # per_loop_length = 2 * amplitude (the vertical parts)
        length_per_loop = 2 * amplitude
        num_loops = int(math.ceil(target_length_add / length_per_loop))
        num_loops = min(num_loops, self.config.max_iterations)

        if num_loops <= 0:
            return SerpentineResult(
                success=True,
                new_segments=[segment],
                message="No loops needed",
            )

        # Check if we have enough segment length for the loops
        total_forward = num_loops * gap * 2 + gap  # Entry + loops + exit
        if total_forward > length * 0.9:  # Leave 10% margin
            # Reduce loops to fit
            num_loops = int((length * 0.9 - gap) / (2 * gap))
            if num_loops <= 0:
                return SerpentineResult(
                    success=False,
                    new_segments=[segment],
                    message=f"Segment too short for serpentine ({length:.2f}mm)",
                )

        # Generate serpentine segments
        new_segments: list[Segment] = []
        current_x = segment.x1
        current_y = segment.y1

        # Entry: move forward a bit
        entry_len = gap
        next_x = current_x + ux * entry_len
        next_y = current_y + uy * entry_len
        new_segments.append(
            Segment(
                x1=current_x,
                y1=current_y,
                x2=next_x,
                y2=next_y,
                width=segment.width,
                layer=segment.layer,
                net=segment.net,
                net_name=segment.net_name,
            )
        )
        current_x, current_y = next_x, next_y

        # Alternate direction for each loop
        direction = 1  # Start going "up" (positive perpendicular)

        for loop in range(num_loops):
            # 1. Go perpendicular (up or down)
            next_x = current_x + px * amplitude * direction
            next_y = current_y + py * amplitude * direction
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=next_x,
                    y2=next_y,
                    width=segment.width,
                    layer=segment.layer,
                    net=segment.net,
                    net_name=segment.net_name,
                )
            )
            current_x, current_y = next_x, next_y

            # 2. Go forward (along original direction)
            next_x = current_x + ux * gap
            next_y = current_y + uy * gap
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=next_x,
                    y2=next_y,
                    width=segment.width,
                    layer=segment.layer,
                    net=segment.net,
                    net_name=segment.net_name,
                )
            )
            current_x, current_y = next_x, next_y

            # 3. Go perpendicular back (down or up)
            next_x = current_x - px * amplitude * direction
            next_y = current_y - py * amplitude * direction
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=next_x,
                    y2=next_y,
                    width=segment.width,
                    layer=segment.layer,
                    net=segment.net,
                    net_name=segment.net_name,
                )
            )
            current_x, current_y = next_x, next_y

            # 4. Go forward again (short connection to next loop)
            if loop < num_loops - 1:
                next_x = current_x + ux * gap
                next_y = current_y + uy * gap
                new_segments.append(
                    Segment(
                        x1=current_x,
                        y1=current_y,
                        x2=next_x,
                        y2=next_y,
                        width=segment.width,
                        layer=segment.layer,
                        net=segment.net,
                        net_name=segment.net_name,
                    )
                )
                current_x, current_y = next_x, next_y

            # Alternate direction
            direction *= -1

        # Exit: connect to original segment end
        new_segments.append(
            Segment(
                x1=current_x,
                y1=current_y,
                x2=segment.x2,
                y2=segment.y2,
                width=segment.width,
                layer=segment.layer,
                net=segment.net,
                net_name=segment.net_name,
            )
        )

        # Calculate actual length added
        original_length = segment_length(segment)
        new_length = sum(segment_length(s) for s in new_segments)
        length_added = new_length - original_length

        return SerpentineResult(
            success=True,
            new_segments=new_segments,
            length_added=length_added,
            num_loops=num_loops,
            message=f"Added {num_loops} loops, {length_added:.3f}mm",
        )

    def add_serpentine(
        self,
        route: Route,
        target_length: float,
        grid: RoutingGrid | None = None,
    ) -> tuple[Route, SerpentineResult]:
        """Add serpentine to a route to reach target length.

        Args:
            route: Route to modify
            target_length: Target total length in mm
            grid: Optional routing grid for collision checking

        Returns:
            Tuple of (modified route, result)
        """
        from ..length import LengthTracker

        current_length = LengthTracker.calculate_route_length(route)
        length_needed = target_length - current_length

        if length_needed <= 0:
            return route, SerpentineResult(
                success=True,
                new_segments=route.segments.copy(),
                message="Route already meets target length",
            )

        # Find best segment for serpentine
        best = self.find_best_segment(route)
        if not best:
            return route, SerpentineResult(
                success=False,
                new_segments=route.segments.copy(),
                message="No suitable segment found for serpentine",
            )

        seg_idx, segment = best

        # Generate serpentine based on style
        if self.config.style == SerpentineStyle.TROMBONE:
            result = self.generate_trombone(segment, length_needed)
        else:
            # Default to trombone for now
            result = self.generate_trombone(segment, length_needed)

        if not result.success:
            return route, result

        # TODO: Add collision checking with grid if provided

        # Build new route with serpentine
        new_segments = (
            route.segments[:seg_idx] + result.new_segments + route.segments[seg_idx + 1 :]
        )

        new_route = Route(
            net=route.net,
            net_name=route.net_name,
            segments=new_segments,
            vias=route.vias.copy(),
        )

        return new_route, result


def add_serpentine(
    route: Route,
    target_length: float,
    grid: RoutingGrid | None = None,
    config: SerpentineConfig | None = None,
) -> tuple[Route, SerpentineResult]:
    """Add serpentine tuning to increase route length.

    This is a convenience function that creates a SerpentineGenerator
    and adds serpentine to reach the target length.

    Args:
        route: Route to modify
        target_length: Target total length in mm
        grid: Optional routing grid for collision checking
        config: Optional serpentine configuration

    Returns:
        Tuple of (modified route, result)

    Example:
        >>> from kicad_tools.router.optimizer.serpentine import add_serpentine
        >>> new_route, result = add_serpentine(route, target_length=50.0)
        >>> if result.success:
        ...     print(f"Added {result.length_added:.3f}mm with {result.num_loops} loops")
    """
    generator = SerpentineGenerator(config)
    return generator.add_serpentine(route, target_length, grid)


def tune_match_group(
    routes: dict[int, Route],
    group_net_ids: list[int],
    tolerance: float = 0.5,
    grid: RoutingGrid | None = None,
    config: SerpentineConfig | None = None,
) -> dict[int, tuple[Route, SerpentineResult]]:
    """Adjust routes in a match group to equal lengths.

    For match groups, we can only add length (not remove it), so the
    target is the longest route. Shorter routes get serpentines added.

    Args:
        routes: Dictionary mapping net ID to Route
        group_net_ids: List of net IDs in the match group
        tolerance: Length match tolerance in mm
        grid: Optional routing grid for collision checking
        config: Optional serpentine configuration

    Returns:
        Dictionary mapping net ID to (modified route, result)

    Example:
        >>> results = tune_match_group(
        ...     routes={100: route1, 101: route2, 102: route3},
        ...     group_net_ids=[100, 101, 102],
        ...     tolerance=0.5,
        ... )
        >>> for net_id, (new_route, result) in results.items():
        ...     if result.success:
        ...         print(f"Net {net_id}: {result.message}")
    """
    from ..length import LengthTracker

    generator = SerpentineGenerator(config)
    results: dict[int, tuple[Route, SerpentineResult]] = {}

    # Get routes for the match group
    group_routes = {net_id: routes[net_id] for net_id in group_net_ids if net_id in routes}

    if len(group_routes) < 2:
        return results

    # Find target length (longest route, can't shorten)
    lengths = {
        net_id: LengthTracker.calculate_route_length(route)
        for net_id, route in group_routes.items()
    }
    target_length = max(lengths.values())

    # Extend shorter routes
    for net_id, route in group_routes.items():
        current_length = lengths[net_id]

        if current_length >= target_length - tolerance:
            # Already within tolerance
            results[net_id] = (
                route,
                SerpentineResult(
                    success=True,
                    new_segments=route.segments.copy(),
                    message="Already within tolerance",
                ),
            )
        else:
            # Need to add serpentine
            new_route, result = generator.add_serpentine(route, target_length, grid)
            results[net_id] = (new_route, result)

    return results
