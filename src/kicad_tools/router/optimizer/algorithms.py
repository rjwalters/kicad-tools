"""Optimization algorithms for trace cleanup."""

from __future__ import annotations

import math
from typing import Callable

from ..primitives import Segment
from ..quantize import dogleg_points
from .config import OptimizationConfig
from .geometry import (
    is_90_degree_corner,
    is_connected,
    is_zigzag,
    perpendicular_direction,
    project_point_onto_line,
    same_direction,
    segment_direction,
    segment_length,
    shorten_segment_end,
    shorten_segment_start,
    translate_segment,
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
    pad_positions: set[tuple[float, float]] | None = None,
) -> list[Segment]:
    """Convert 90-degree corners to 45-degree chamfers.

    Replaces sharp 90-degree turns with smoother 45-degree entry/exit,
    but only if the chamfer path is clear of obstacles.

    After chamfering, the original terminal endpoints (start of first segment,
    end of last segment) are restored if they were displaced.  This prevents
    chamfering from moving trace endpoints away from pad positions, which
    would break electrical connectivity.

    Args:
        segments: List of segments to process.
        config: Optimization configuration.
        path_is_clear: Optional function to check if a path is clear.
        pad_positions: Optional set of (x, y) pad positions.  When provided,
            terminal endpoint restoration only fires for endpoints that are
            within tolerance of a pad position.  When ``None``, terminal
            endpoints are always restored (safe default).

    Returns:
        List with corners converted to 45 degrees.
    """
    if len(segments) < 2:
        return list(segments)

    # Record original terminal endpoints before any modification.
    orig_start = (segments[0].x1, segments[0].y1)
    orig_end = (segments[-1].x2, segments[-1].y2)

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
                # Only shorten if next segment can also be shortened for the chamfer.
                # If next segment is too short (e.g., final approach to a pad),
                # don't create a partial chamfer that would leave a gap.
                next_can_shorten = (
                    shorten_segment_start(next_seg, chamfer, config.min_segment_length) is not None
                )
                if next_can_shorten:
                    shortened = shorten_segment_end(
                        modified_seg, chamfer, config.min_segment_length
                    )
                    if shortened:
                        modified_seg = shortened

            result.append(modified_seg)

    # --- Post-chamfer terminal endpoint restoration ---
    # Chamfering may have displaced the start of the first segment or the
    # end of the last segment (the chain's "pad endpoints").  Restore them
    # so the trace still terminates exactly at the pad centre.
    result = _restore_terminal_endpoints(
        result, orig_start, orig_end, pad_positions, config.tolerance
    )

    return result


def _point_near_any_pad(
    point: tuple[float, float],
    pad_positions: set[tuple[float, float]],
    tolerance: float,
) -> bool:
    """Return True if *point* is within *tolerance* of any pad position."""
    tol_sq = tolerance * tolerance
    for pad in pad_positions:
        dx = point[0] - pad[0]
        dy = point[1] - pad[1]
        if dx * dx + dy * dy < tol_sq:
            return True
    return False


def _restore_terminal_endpoints(
    segments: list[Segment],
    orig_start: tuple[float, float],
    orig_end: tuple[float, float],
    pad_positions: set[tuple[float, float]] | None,
    tolerance: float,
) -> list[Segment]:
    """Restore chain terminal endpoints that were displaced by chamfering.

    If *pad_positions* is provided, restoration only fires when the original
    endpoint is near a pad.  Otherwise it fires unconditionally (safe
    default -- terminal endpoints should always be preserved).
    """
    if not segments:
        return segments

    # Use a generous tolerance for pad matching (0.05 mm) to catch pads
    # that are close but not exact due to coordinate rounding.
    pad_match_tolerance = 0.05

    def _make_legs(
        template: Segment,
        sx: float,
        sy: float,
        ex: float,
        ey: float,
    ) -> list[Segment]:
        """Build 45-quantized segment(s) from (sx, sy) to (ex, ey).

        Issue #3532: restoring a terminal endpoint to the (off-grid) pad
        centre used to skew the terminal segment off the 0/45/90/135
        angle set.  Emit an exact dogleg (45-degree leg + axis leg)
        instead of a single skewed segment.
        """
        points = dogleg_points(sx, sy, ex, ey)
        return [
            Segment(
                x1=ax,
                y1=ay,
                x2=bx,
                y2=by,
                width=template.width,
                layer=template.layer,
                net=template.net,
                net_name=template.net_name,
            )
            for (ax, ay), (bx, by) in zip(points, points[1:], strict=False)
            if (ax, ay) != (bx, by)
        ]

    # --- Restore start of first segment ---
    first = segments[0]
    start_displaced = (
        abs(first.x1 - orig_start[0]) > tolerance or abs(first.y1 - orig_start[1]) > tolerance
    )
    if start_displaced:
        should_restore_start = pad_positions is None or _point_near_any_pad(
            orig_start, pad_positions, pad_match_tolerance
        )
        if should_restore_start:
            segments[0:1] = _make_legs(first, orig_start[0], orig_start[1], first.x2, first.y2)

    # --- Restore end of last segment ---
    last = segments[-1]
    end_displaced = abs(last.x2 - orig_end[0]) > tolerance or abs(last.y2 - orig_end[1]) > tolerance
    if end_displaced:
        should_restore_end = pad_positions is None or _point_near_any_pad(
            orig_end, pad_positions, pad_match_tolerance
        )
        if should_restore_end:
            segments[-1:] = _make_legs(last, last.x1, last.y1, orig_end[0], orig_end[1])

    return segments


# ---------------------------------------------------------------------------
# PullTight post-processing
# ---------------------------------------------------------------------------


def _segment_parallel_to(seg: Segment, ux: float, uy: float) -> bool:
    """True if *seg* runs parallel to direction ``(ux, uy)``.

    A (near-)zero-length segment counts as parallel: re-aiming it along
    the translation direction cannot skew it off the 45-degree set.
    """
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    norm = math.hypot(dx, dy)
    if norm < 1e-9:
        return True
    # |sin(angle between)| -- (ux, uy) is a unit vector.
    return abs(dx * uy - dy * ux) / norm < 1e-4


def pull_tight_pass(
    segments: list[Segment],
    config: OptimizationConfig,
    path_is_clear: Callable[[Segment], bool] | None = None,
) -> list[Segment]:
    """Translate interior segments perpendicular to their direction to shorten total chain length.

    For each interior segment S(i) (between S(i-1) and S(i+1)):
    1. Compute the perpendicular direction of S(i).
    2. Binary-search for the maximum safe displacement toward the direct
       S(i-1)-to-S(i+1) line, validating each candidate with *path_is_clear*.
    3. If displacement reduces total chain length, update S(i) and adjust
       connection points on its neighbours.

    After translating, newly-collinear segments are merged and zero-length
    segments are removed.

    The pass iterates until no segment moves more than *config.tolerance* or
    *config.pull_tight_max_iterations* is reached.

    Args:
        segments: Ordered chain of connected segments (single chain).
        config: Optimization configuration.
        path_is_clear: Optional collision-check callback.

    Returns:
        Optimised list of segments (may be shorter).
    """
    if len(segments) < 3:
        return list(segments)

    result = list(segments)
    max_iters = config.pull_tight_max_iterations
    tol = config.tolerance

    for _iteration in range(max_iters):
        moved = False

        i = 1
        while i < len(result) - 1:
            prev = result[i - 1]
            curr = result[i]
            nxt = result[i + 1]

            # Perpendicular direction of the current segment
            perp_x, perp_y = perpendicular_direction(curr, tol)
            if perp_x == 0.0 and perp_y == 0.0:
                i += 1
                continue

            # Issue #3532: translating curr along perp re-aims prev and
            # nxt at the moved endpoints.  Unless both neighbours run
            # PARALLEL to the translation direction (the classic
            # rectilinear jog window, e.g. H-V-H translated
            # horizontally), any non-zero displacement skews them off
            # the 0/45/90/135 angle set -- skip those windows so the
            # pass never emits arbitrary-angle copper.
            if not (
                _segment_parallel_to(prev, perp_x, perp_y)
                and _segment_parallel_to(nxt, perp_x, perp_y)
            ):
                i += 1
                continue

            # Project the midpoint of curr onto the line from the start of
            # prev to the end of nxt (the "ideal" straight-line path).
            mid_x = (curr.x1 + curr.x2) / 2
            mid_y = (curr.y1 + curr.y2) / 2
            proj_x, proj_y = project_point_onto_line(mid_x, mid_y, prev.x1, prev.y1, nxt.x2, nxt.y2)

            # Desired displacement vector (toward the projected point)
            disp_x = proj_x - mid_x
            disp_y = proj_y - mid_y

            # Component of displacement along the perpendicular direction
            # (the only direction we allow translation)
            perp_component = disp_x * perp_x + disp_y * perp_y

            if abs(perp_component) < tol:
                i += 1
                continue

            # Binary search for the maximum safe displacement
            best_displacement = 0.0
            lo, hi = 0.0, abs(perp_component)
            sign = 1.0 if perp_component > 0 else -1.0

            for _bs in range(16):  # ~1e-5 precision after 16 steps
                mid_disp = (lo + hi) / 2
                dx = sign * perp_x * mid_disp
                dy = sign * perp_y * mid_disp

                # Build candidate segments: translated curr + adjusted neighbours
                cand_curr = translate_segment(curr, dx, dy)

                cand_prev = Segment(
                    x1=prev.x1,
                    y1=prev.y1,
                    x2=cand_curr.x1,
                    y2=cand_curr.y1,
                    width=prev.width,
                    layer=prev.layer,
                    net=prev.net,
                    net_name=prev.net_name,
                )
                cand_nxt = Segment(
                    x1=cand_curr.x2,
                    y1=cand_curr.y2,
                    x2=nxt.x2,
                    y2=nxt.y2,
                    width=nxt.width,
                    layer=nxt.layer,
                    net=nxt.net,
                    net_name=nxt.net_name,
                )

                # Check clearance of all three candidate segments
                clear = True
                if path_is_clear is not None:
                    clear = (
                        path_is_clear(cand_prev)
                        and path_is_clear(cand_curr)
                        and path_is_clear(cand_nxt)
                    )

                if clear:
                    best_displacement = mid_disp
                    lo = mid_disp
                else:
                    hi = mid_disp

            if best_displacement < tol:
                i += 1
                continue

            # Apply the best displacement
            dx = sign * perp_x * best_displacement
            dy = sign * perp_y * best_displacement

            new_curr = translate_segment(curr, dx, dy)

            # Compute old and new total length for the three-segment window
            old_len = segment_length(prev) + segment_length(curr) + segment_length(nxt)

            new_prev = Segment(
                x1=prev.x1,
                y1=prev.y1,
                x2=new_curr.x1,
                y2=new_curr.y1,
                width=prev.width,
                layer=prev.layer,
                net=prev.net,
                net_name=prev.net_name,
            )
            new_nxt = Segment(
                x1=new_curr.x2,
                y1=new_curr.y2,
                x2=nxt.x2,
                y2=nxt.y2,
                width=nxt.width,
                layer=nxt.layer,
                net=nxt.net,
                net_name=nxt.net_name,
            )
            new_len = segment_length(new_prev) + segment_length(new_curr) + segment_length(new_nxt)

            if new_len < old_len - tol:
                result[i - 1] = new_prev
                result[i] = new_curr
                result[i + 1] = new_nxt
                moved = True

            i += 1

        # Post-pass: merge collinear segments and remove zero-length ones
        result = merge_collinear(result, config, path_is_clear)
        result = [s for s in result if segment_length(s) > tol]

        if not moved:
            break

    return result
