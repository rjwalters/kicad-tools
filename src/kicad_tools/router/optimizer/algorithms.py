"""Optimization algorithms for trace cleanup."""

from __future__ import annotations

import math
from typing import Callable

from ..primitives import Segment
from .config import OptimizationConfig
from .geometry import (
    is_90_degree_corner,
    is_connected,
    is_zigzag,
    same_direction,
    segment_direction,
    shorten_segment_end,
    shorten_segment_start,
)


def merge_collinear(
    segments: list[Segment],
    config: OptimizationConfig,
    path_is_clear: Callable[[Segment], bool] | None = None,
) -> list[Segment]:
    """Merge adjacent collinear segments.

    Combines segments that:
    - Are connected (end of one matches start of next)
    - Have the same direction
    - Are on the same layer
    - Would not cross obstacles (if collision checker provided)

    Args:
        segments: List of segments to merge.
        config: Optimization configuration.
        path_is_clear: Optional function to check if a path is clear.

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
            is_connected(current, next_seg, config.tolerance)
            and same_direction(current, next_seg, config.tolerance)
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
            if path_is_clear is None or path_is_clear(merged):
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


def eliminate_zigzags(
    segments: list[Segment],
    config: OptimizationConfig,
    path_is_clear: Callable[[Segment], bool] | None = None,
) -> list[Segment]:
    """Remove unnecessary zigzag patterns.

    Identifies segments where the path backtracks and removes
    the unnecessary detour, but only if the shortcut path is clear.

    Args:
        segments: List of segments to process.
        config: Optimization configuration.
        path_is_clear: Optional function to check if a path is clear.

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
        if is_zigzag(prev, curr, next_seg, config.tolerance):
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
            if path_is_clear is None or path_is_clear(shortcut):
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


def compress_staircase(
    segments: list[Segment],
    config: OptimizationConfig,
    path_is_clear: Callable[[Segment], bool] | None = None,
) -> list[Segment]:
    """Compress staircase patterns into optimal diagonal+orthogonal paths.

    Identifies runs of segments alternating between two directions
    (e.g., horizontal and 45° diagonal) and replaces them with an
    optimal 2-3 segment path, but only if the replacement is clear.

    Args:
        segments: List of segments to process.
        config: Optimization configuration.
        path_is_clear: Optional function to check if a path is clear.

    Returns:
        List with staircase patterns compressed.
    """
    if not config.compress_staircase:
        return list(segments)

    if len(segments) < config.min_staircase_segments:
        return list(segments)

    result: list[Segment] = []
    i = 0

    while i < len(segments):
        # Look for staircase pattern starting at i
        staircase_end = _find_staircase_end(segments, i, config)

        if staircase_end - i >= config.min_staircase_segments:
            # Found a staircase of sufficient length
            start_point = (segments[i].x1, segments[i].y1)
            end_point = (segments[staircase_end - 1].x2, segments[staircase_end - 1].y2)

            # Generate optimal replacement path
            template = segments[i]
            replacement = _optimal_path(start_point, end_point, template, config)

            # Check if all replacement segments are clear
            if path_is_clear is None:
                all_clear = True
            else:
                all_clear = all(path_is_clear(seg) for seg in replacement)

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


def _find_staircase_end(segments: list[Segment], start_idx: int, config: OptimizationConfig) -> int:
    """Find the end index of a staircase pattern starting at start_idx.

    A staircase is a run of segments alternating between two directions:
    - Orthogonal+diagonal patterns: ~45° apart (e.g., 0° and 45°, or 180° and 135°)
    - Rectilinear patterns: ~90° apart (e.g., 0° and 90°, horizontal/vertical)

    The rectilinear pattern detection enables compression of H/V staircases
    produced by A* routers on rectilinear grids.

    Args:
        segments: List of all segments.
        start_idx: Index to start looking from.
        config: Optimization configuration.

    Returns:
        End index (exclusive) of the staircase pattern.
    """
    if start_idx >= len(segments) - 1:
        return start_idx + 1

    # Get the two alternating directions
    dir1 = segment_direction(segments[start_idx], config.tolerance)
    dir2 = segment_direction(segments[start_idx + 1], config.tolerance)

    # Check if they form a valid staircase pair
    angle_diff = abs(dir1 - dir2)
    # Handle wraparound (e.g., 350° and 10° are 20° apart, not 340°)
    if angle_diff > 180:
        angle_diff = 360 - angle_diff

    # Valid staircase patterns:
    # - Orthogonal+diagonal: ~45° apart (30-60° range)
    # - Rectilinear H/V: ~90° apart (75-105° range)
    is_diagonal_staircase = 30 <= angle_diff <= 60
    is_rectilinear_staircase = 75 <= angle_diff <= 105
    if not (is_diagonal_staircase or is_rectilinear_staircase):
        return start_idx + 1

    # Find how far the alternating pattern continues
    i = start_idx + 2
    while i < len(segments):
        dir_i = segment_direction(segments[i], config.tolerance)
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


def _optimal_path(
    start: tuple[float, float],
    end: tuple[float, float],
    template: Segment,
    config: OptimizationConfig,
) -> list[Segment]:
    """Generate an optimal 2-3 segment path from start to end.

    Uses 45° diagonal routing to minimize segment count while
    maintaining connectivity.

    Args:
        start: Starting point (x, y).
        end: Ending point (x, y).
        template: Template segment for properties (width, layer, net, net_name).
        config: Optimization configuration.

    Returns:
        List of 1-3 segments forming the optimal path.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]

    # Handle degenerate cases
    if abs(dx) < config.tolerance and abs(dy) < config.tolerance:
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
    if diag_dist > config.tolerance:
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

    if abs(remaining_dx) > config.tolerance or abs(remaining_dy) > config.tolerance:
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
        result.append(
            Segment(
                x1=start[0],
                y1=start[1],
                x2=end[0],
                y2=end[1],
                width=template.width,
                layer=template.layer,
                net=template.net,
                net_name=template.net_name,
            )
        )

    return result


def convert_corners_45(
    segments: list[Segment],
    config: OptimizationConfig,
    path_is_clear: Callable[[Segment], bool] | None = None,
) -> list[Segment]:
    """Convert 90-degree corners to 45-degree chamfers.

    Replaces sharp 90-degree turns with smoother 45-degree entry/exit,
    but only if the chamfer path is clear of obstacles.

    Args:
        segments: List of segments to process.
        config: Optimization configuration.
        path_is_clear: Optional function to check if a path is clear.

    Returns:
        List with corners converted to 45 degrees.
    """
    if len(segments) < 2:
        return list(segments)

    result: list[Segment] = []
    chamfer = config.corner_chamfer_size

    for i, seg in enumerate(segments):
        if i == 0:
            # First segment - check if next segment forms 90-degree corner
            if i + 1 < len(segments):
                next_seg = segments[i + 1]
                if is_90_degree_corner(seg, next_seg):
                    # Shorten this segment to leave room for chamfer
                    shortened = shorten_segment_end(seg, chamfer, config.min_segment_length)
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
            if is_90_degree_corner(prev_seg, seg):
                # Shorten start of this segment
                shortened = shorten_segment_start(seg, chamfer, config.min_segment_length)
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
                    if path_is_clear is None or path_is_clear(chamfer_seg):
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
            if is_90_degree_corner(prev_seg, seg) and result:
                shortened = shorten_segment_start(modified_seg, chamfer, config.min_segment_length)
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
                    if path_is_clear is None or path_is_clear(chamfer_seg):
                        result.append(chamfer_seg)
                        modified_seg = shortened

            # Handle corner with next segment
            if is_90_degree_corner(seg, next_seg):
                shortened = shorten_segment_end(modified_seg, chamfer, config.min_segment_length)
                if shortened:
                    modified_seg = shortened

            result.append(modified_seg)

    return result
