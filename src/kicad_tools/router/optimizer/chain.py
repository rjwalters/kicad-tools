"""Chain sorting for connected segments.

Splits routed segments into linear sub-chains at junction nodes. A junction
node is any endpoint shared by 3 or more segment endpoints (Y/T-junctions
that arise on multi-pad nets). Linearizing such a graph as a single chain
causes downstream optimisations (collinear merge, pull-tight, etc.) to drop
or rewrite branches and silently disconnect pads (issue #2389).
"""

from __future__ import annotations

from collections import defaultdict

from ..primitives import Segment


def _quantize(value: float, tolerance: float) -> int:
    """Quantize a coordinate so endpoints within ``tolerance`` map together."""
    # Use a quantization grid roughly an order of magnitude finer than the
    # tolerance so neighbouring values round to the same key.  Using ``round``
    # at the tolerance scale keeps small floating-point drift on the same
    # vertex without merging genuinely distinct endpoints.
    if tolerance <= 0:
        return int(round(value * 1e9))
    return int(round(value / tolerance))


def _vertex_key(x: float, y: float, tolerance: float) -> tuple[int, int]:
    """Return a hashable key that two endpoints within ``tolerance`` share."""
    return (_quantize(x, tolerance), _quantize(y, tolerance))


def sort_into_chains(segments: list[Segment], tolerance: float = 1e-4) -> list[list[Segment]]:
    """Sort segments into connected linear chains.

    Groups segments that form continuous paths and **splits each connected
    component at junction nodes** (vertices of degree >= 3) so that every
    returned chain is a true linear path.  This prevents the downstream
    linear optimisation passes from merging or shortening across a Y/T
    junction and dropping pad endpoints.

    Args:
        segments: List of segments to sort.
        tolerance: Tolerance for endpoint comparison.

    Returns:
        List of chains, where each chain is an ordered list of segments
        whose endpoints meet head-to-tail.  Chains do not span junctions.
    """
    if not segments:
        return []

    # Build a vertex -> list[(seg_idx, which_end)] adjacency.  ``which_end``
    # is 0 for the (x1, y1) endpoint of the segment and 1 for (x2, y2).
    vertex_endpoints: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for idx, seg in enumerate(segments):
        vertex_endpoints[_vertex_key(seg.x1, seg.y1, tolerance)].append((idx, 0))
        vertex_endpoints[_vertex_key(seg.x2, seg.y2, tolerance)].append((idx, 1))

    # A vertex is a "boundary" vertex (a sub-chain split point) if it has
    # degree != 2 -- i.e. either a leaf (1 endpoint) or a junction (>=3).
    def is_boundary(key: tuple[int, int]) -> bool:
        return len(vertex_endpoints[key]) != 2

    # Walk linear sub-chains from each boundary vertex.  Each segment is
    # visited at most once via the ``visited`` set.
    visited: set[int] = set()
    chains: list[list[Segment]] = []

    boundary_keys = [k for k in vertex_endpoints if is_boundary(k)]

    for start_key in boundary_keys:
        # Try to start a sub-chain from each unvisited segment incident
        # to this boundary vertex.
        for seg_idx, which_end in list(vertex_endpoints[start_key]):
            if seg_idx in visited:
                continue
            chain = _walk_linear_chain(
                segments, vertex_endpoints, seg_idx, which_end, visited, tolerance
            )
            if chain:
                chains.append(chain)

    # Any segments still unvisited belong to pure loops (all vertices
    # degree 2).  Walk each loop as a single chain.
    for idx in range(len(segments)):
        if idx in visited:
            continue
        chain = _walk_linear_chain(segments, vertex_endpoints, idx, 0, visited, tolerance)
        if chain:
            chains.append(chain)

    return chains


def _walk_linear_chain(
    segments: list[Segment],
    vertex_endpoints: dict[tuple[int, int], list[tuple[int, int]]],
    start_idx: int,
    start_which_end: int,
    visited: set[int],
    tolerance: float,
) -> list[Segment]:
    """Walk a single linear sub-chain starting from segment ``start_idx``.

    The walk begins at endpoint ``start_which_end`` of ``segments[start_idx]``
    and proceeds away from that endpoint, following degree-2 vertices through
    the segment graph.  It stops when:

    - the next vertex has degree != 2 (leaf or junction), or
    - the next segment has already been visited (loop closed).

    Args:
        segments: All segments.
        vertex_endpoints: Vertex -> list[(seg_idx, which_end)] map.
        start_idx: Index of the segment to start from.
        start_which_end: 0 if walk starts at (x1, y1), 1 if at (x2, y2).
        visited: Set of segment indices already placed in a chain. Mutated.
        tolerance: Endpoint tolerance.

    Returns:
        Ordered list of segments forming a linear path. Segments are
        re-oriented so each segment's end matches the next segment's start.
    """
    if start_idx in visited:
        return []

    chain: list[Segment] = []
    visited.add(start_idx)

    seg = segments[start_idx]
    # Orient the first segment so that the walk-start endpoint comes first.
    if start_which_end == 1:
        seg = _flip_segment(seg)
    chain.append(seg)

    # Walk forward from seg's "end" vertex.
    current_end_key = _vertex_key(seg.x2, seg.y2, tolerance)

    while True:
        incident = vertex_endpoints[current_end_key]
        # Stop at boundaries (leaves and junctions).
        if len(incident) != 2:
            break

        # Find the *other* incident segment at this vertex.
        next_seg_idx: int | None = None
        next_which_end: int | None = None
        for cand_idx, cand_which in incident:
            if cand_idx not in visited:
                next_seg_idx = cand_idx
                next_which_end = cand_which
                break

        if next_seg_idx is None:
            # All incident segments visited (loop closed).
            break

        visited.add(next_seg_idx)
        next_seg = segments[next_seg_idx]
        # Orient so the matching endpoint comes first (start of next_seg).
        if next_which_end == 1:
            next_seg = _flip_segment(next_seg)
        chain.append(next_seg)
        current_end_key = _vertex_key(next_seg.x2, next_seg.y2, tolerance)

    return chain


def _flip_segment(seg: Segment) -> Segment:
    """Return a copy of ``seg`` with its endpoints swapped."""
    return Segment(
        x1=seg.x2,
        y1=seg.y2,
        x2=seg.x1,
        y2=seg.y1,
        width=seg.width,
        layer=seg.layer,
        net=seg.net,
        net_name=seg.net_name,
    )


def sort_chain_segments(segments: list[Segment], tolerance: float = 1e-4) -> list[Segment]:
    """Sort segments within a single connected chain into path order.

    .. note::
        This is the legacy single-chain sorter.  It assumes the input is a
        linear (non-branching) path; the new :func:`sort_into_chains`
        already splits at junctions, so this function should only ever
        receive linear inputs from internal callers.  External callers
        passing a Y/T-shape will get a single linearised result with
        branches appended in arbitrary order -- prefer
        :func:`sort_into_chains` instead.

    Args:
        segments: List of segments belonging to the same linear chain.
        tolerance: Tolerance for endpoint comparison.

    Returns:
        Segments sorted in path order.
    """
    if len(segments) <= 1:
        return list(segments)

    # If callers hand us a multi-branch shape, split it first and
    # concatenate the branches so we still produce a valid (if not
    # ideal) ordering rather than silently dropping branches.
    chains = sort_into_chains(segments, tolerance)
    result: list[Segment] = []
    for chain in chains:
        result.extend(chain)
    return result
