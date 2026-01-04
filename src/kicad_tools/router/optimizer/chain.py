"""Chain sorting for connected segments."""

from __future__ import annotations

from ..primitives import Segment
from .geometry import segments_touch


def sort_into_chains(segments: list[Segment], tolerance: float = 1e-4) -> list[list[Segment]]:
    """Sort segments into connected chains.

    Groups segments that form continuous paths. Segments that share
    endpoints belong to the same chain. This prevents optimization
    from creating shortcuts between unconnected segments.

    Args:
        segments: List of segments to sort.
        tolerance: Tolerance for endpoint comparison.

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
                    if segments_touch(segments[chain_idx], seg, tolerance):
                        chain_indices.append(idx)
                        remaining.remove(idx)
                        changed = True
                        break

        # Sort chain segments into path order
        chain_segments = [segments[i] for i in chain_indices]
        sorted_chain = sort_chain_segments(chain_segments, tolerance)
        chains.append(sorted_chain)

    return chains


def sort_chain_segments(segments: list[Segment], tolerance: float = 1e-4) -> list[Segment]:
    """Sort segments within a chain into connected path order.

    Arranges segments so that each segment's end connects to the
    next segment's start, forming a continuous path.

    Args:
        segments: List of segments belonging to the same chain.
        tolerance: Tolerance for endpoint comparison.

    Returns:
        Segments sorted in path order.
    """
    if len(segments) <= 1:
        return list(segments)

    result: list[Segment] = []
    remaining = list(segments)

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
                if (abs(seg.x1 - other.x1) < tolerance and abs(seg.y1 - other.y1) < tolerance) or (
                    abs(seg.x1 - other.x2) < tolerance and abs(seg.y1 - other.y2) < tolerance
                ):
                    start_shared = True
                # Check if end of seg matches any endpoint of other
                if (abs(seg.x2 - other.x1) < tolerance and abs(seg.y2 - other.y1) < tolerance) or (
                    abs(seg.x2 - other.x2) < tolerance and abs(seg.y2 - other.y2) < tolerance
                ):
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
        (abs(current.x1 - other.x1) < tolerance and abs(current.y1 - other.y1) < tolerance)
        or (abs(current.x1 - other.x2) < tolerance and abs(current.y1 - other.y2) < tolerance)
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
            if abs(current.x2 - seg.x1) < tolerance and abs(current.y2 - seg.y1) < tolerance:
                current = remaining.pop(i)
                result.append(current)
                found = True
                break
            # Check if seg's end connects to current's end (need to flip seg)
            if abs(current.x2 - seg.x2) < tolerance and abs(current.y2 - seg.y2) < tolerance:
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
