"""Rectilinear Steiner Minimum Tree (RSMT) construction.

This module provides RSMT decomposition for multi-terminal nets using
Hanan grid construction and iterative 1-Steiner insertion. The RSMT
produces shorter total wirelength than MST by introducing Steiner
points (branch points) at optimal locations.

Algorithm overview:
1. Build the Hanan grid (all intersections of horizontal/vertical lines
   through terminal positions).
2. Start with an MST of the terminals.
3. Iteratively evaluate each Hanan grid point as a candidate Steiner
   point. Insert the one that gives the largest cost reduction. Repeat
   until no improvement is found.

For 2-terminal nets, the result is identical to MST (single edge).
For 3-terminal nets, the optimal Steiner topology is found directly.
For larger nets, iterative 1-Steiner insertion provides a good
approximation with bounded runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..primitives import Pad


def _manhattan(x1: float, y1: float, x2: float, y2: float) -> float:
    """Compute Manhattan distance between two points."""
    return abs(x1 - x2) + abs(y1 - y2)


def _build_mst_edges(
    points: list[tuple[float, float]],
    cost_fn: Callable[[float, float, float, float], float] | None = None,
) -> list[tuple[int, int]]:
    """Build MST edges using Prim's algorithm.

    Args:
        points: List of (x, y) coordinates.
        cost_fn: Optional cost function(x1, y1, x2, y2) -> cost.
            Defaults to Manhattan distance.

    Returns:
        List of (i, j) index pairs forming the MST.
    """
    n = len(points)
    if n < 2:
        return []

    dist_fn = cost_fn or _manhattan

    connected: set[int] = {0}
    unconnected = set(range(1, n))
    edges: list[tuple[int, int]] = []

    while unconnected:
        best_cost = float("inf")
        best_edge: tuple[int, int] | None = None

        for i in connected:
            xi, yi = points[i]
            for j in unconnected:
                xj, yj = points[j]
                c = dist_fn(xi, yi, xj, yj)
                if c < best_cost:
                    best_cost = c
                    best_edge = (i, j)

        if best_edge is None:
            break
        i, j = best_edge
        edges.append((i, j))
        connected.add(j)
        unconnected.remove(j)

    return edges


def _mst_cost(
    points: list[tuple[float, float]],
    cost_fn: Callable[[float, float, float, float], float] | None = None,
) -> float:
    """Compute total MST cost for a set of points."""
    dist_fn = cost_fn or _manhattan
    edges = _build_mst_edges(points, cost_fn)
    total = 0.0
    for i, j in edges:
        xi, yi = points[i]
        xj, yj = points[j]
        total += dist_fn(xi, yi, xj, yj)
    return total


def _hanan_grid(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Compute Hanan grid points that are not already terminals.

    The Hanan grid is the set of intersections formed by drawing
    horizontal and vertical lines through every terminal. We exclude
    points that coincide with existing terminals.

    Args:
        points: Terminal (x, y) coordinates.

    Returns:
        List of candidate Steiner point coordinates.
    """
    xs = sorted({p[0] for p in points})
    ys = sorted({p[1] for p in points})
    terminal_set = set(points)

    candidates: list[tuple[float, float]] = []
    for x in xs:
        for y in ys:
            if (x, y) not in terminal_set:
                candidates.append((x, y))
    return candidates


def _iterative_one_steiner(
    terminals: list[tuple[float, float]],
    cost_fn: Callable[[float, float, float, float], float] | None = None,
    max_iterations: int = 50,
) -> tuple[list[tuple[float, float]], list[tuple[int, int]]]:
    """Iterative 1-Steiner insertion on the Hanan grid.

    Starting from the MST of the terminals, repeatedly find the Hanan
    grid point whose insertion most reduces total tree cost, until no
    improvement is found or max_iterations is reached.

    Args:
        terminals: List of terminal (x, y) coordinates.
        cost_fn: Optional cost function. Defaults to Manhattan distance.
        max_iterations: Maximum Steiner point insertions.

    Returns:
        (all_points, edges) where all_points = terminals + steiner_points,
        and edges are index pairs into all_points.
    """
    all_points = list(terminals)
    current_cost = _mst_cost(all_points, cost_fn)

    for _ in range(max_iterations):
        candidates = _hanan_grid(all_points)
        if not candidates:
            break

        best_gain = 0.0
        best_candidate: tuple[float, float] | None = None

        for candidate in candidates:
            trial = all_points + [candidate]
            trial_cost = _mst_cost(trial, cost_fn)
            gain = current_cost - trial_cost
            if gain > best_gain:
                best_gain = gain
                best_candidate = candidate

        if best_candidate is None or best_gain <= 0:
            break

        all_points.append(best_candidate)
        current_cost -= best_gain

    edges = _build_mst_edges(all_points, cost_fn)
    return all_points, edges


def _solve_3_terminal(
    terminals: list[tuple[float, float]],
    cost_fn: Callable[[float, float, float, float], float] | None = None,
) -> tuple[list[tuple[float, float]], list[tuple[int, int]]]:
    """Optimal RSMT for exactly 3 terminals.

    For 3 rectilinear terminals, the optimal Steiner point (if any) lies
    at the intersection of the median x and median y coordinates. We
    compare the MST cost with the tree cost using this Steiner point.

    Args:
        terminals: Exactly 3 terminal (x, y) coordinates.
        cost_fn: Optional cost function. Defaults to Manhattan distance.

    Returns:
        (all_points, edges) as in _iterative_one_steiner.
    """
    dist_fn = cost_fn or _manhattan

    xs = sorted(t[0] for t in terminals)
    ys = sorted(t[1] for t in terminals)
    steiner = (xs[1], ys[1])  # median x, median y

    # MST cost without Steiner point
    mst_edges = _build_mst_edges(terminals, cost_fn)
    mst_cost = sum(
        dist_fn(terminals[i][0], terminals[i][1], terminals[j][0], terminals[j][1])
        for i, j in mst_edges
    )

    # Check if Steiner point coincides with a terminal
    if steiner in set(terminals):
        return list(terminals), mst_edges

    # Cost with Steiner point: connect each terminal to the Steiner point
    steiner_cost = sum(
        dist_fn(t[0], t[1], steiner[0], steiner[1]) for t in terminals
    )

    if steiner_cost < mst_cost:
        all_points = list(terminals) + [steiner]
        # Build MST of the 4-point set (will naturally use star topology
        # through the Steiner point when optimal)
        edges = _build_mst_edges(all_points, cost_fn)
        return all_points, edges
    else:
        return list(terminals), mst_edges


def build_rsmt(
    pad_objs: list[Pad],
    congestion_fn: Callable[[float, float, float, float], float] | None = None,
    snap_fn: Callable[[float, float], tuple[float, float]] | None = None,
) -> tuple[list[Pad], list[tuple[int, int]]]:
    """Build Rectilinear Steiner Minimum Tree.

    Computes an RSMT for the given pads using Hanan grid construction
    and iterative 1-Steiner insertion. Returns extended pad list
    (original pads + Steiner point virtual pads) and edges as index
    pairs, suitable as a drop-in replacement for MST decomposition.

    For 2-terminal nets, returns identical result to MST (single edge).
    For 3-terminal nets, finds optimal Steiner topology.
    For 4-9 terminal nets, uses iterative 1-Steiner insertion.
    For >9 terminal nets, uses iterative 1-Steiner with bounded iterations.

    Args:
        pad_objs: Terminal pads to connect.
        congestion_fn: Optional function(x1, y1, x2, y2) -> cost.
            If None, uses Manhattan distance.
        snap_fn: Optional function(x, y) -> (x, y) used to snap the
            SYNTHESISED Steiner branch points onto the routing grid
            (PR #3481 fix).  Hanan-grid candidates inherit raw terminal
            coordinates, which generally do NOT align to the routing
            grid; real pads get off-grid rescue via sub-grid / waypoint
            injection, but virtual Steiner pads have no ``ref`` so no
            rescue applies.  Without snapping, a multi-terminal net
            whose Steiner point lands off-grid fails ``pin_access``
            with ``PADS_OFF_GRID: steiner@(...)`` — the softstart
            SRC_POS / BUS_LINE / SCAP_POS+ / VRECT signature.  Terminal
            pads are never snapped, only synthetic points.

    Returns:
        (extended_pads, edges) where extended_pads includes original
        pads plus any Steiner points (marked with steiner_point=True),
        and edges are index pairs into extended_pads sorted by cost.
    """
    from ..primitives import Pad

    n = len(pad_objs)
    if n < 2:
        return list(pad_objs), []

    if n == 2:
        return list(pad_objs), [(0, 1)]

    # Extract coordinates
    terminals = [(p.x, p.y) for p in pad_objs]

    # Choose algorithm based on terminal count
    if n == 3:
        all_points, edges = _solve_3_terminal(terminals, congestion_fn)
    elif n <= 9:
        all_points, edges = _iterative_one_steiner(
            terminals, congestion_fn, max_iterations=50
        )
    else:
        # Larger nets: limit iterations to keep runtime bounded
        all_points, edges = _iterative_one_steiner(
            terminals, congestion_fn, max_iterations=min(n, 30)
        )

    # Build extended pad list with Steiner point virtual pads
    num_terminals = len(terminals)
    extended_pads: list[Pad] = list(pad_objs)

    for idx in range(num_terminals, len(all_points)):
        sx, sy = all_points[idx]
        # PR #3481 fix: snap synthetic branch points onto the routing
        # grid so the A* endpoints are reachable (see ``snap_fn`` doc).
        if snap_fn is not None:
            sx, sy = snap_fn(sx, sy)
        # Create virtual Steiner point pad using the net info from the
        # first terminal pad. Use minimal size for a virtual pad.
        ref_pad = pad_objs[0]
        steiner_pad = Pad(
            x=sx,
            y=sy,
            width=0.0,
            height=0.0,
            net=ref_pad.net,
            net_name=ref_pad.net_name,
            layer=ref_pad.layer,
            ref="",
            pin="",
            through_hole=False,
            drill=0.0,
            steiner_point=True,
        )
        extended_pads.append(steiner_pad)

    # Sort edges by cost (shortest first) for routing order
    dist_fn = congestion_fn or _manhattan
    edges.sort(
        key=lambda e: dist_fn(
            all_points[e[0]][0],
            all_points[e[0]][1],
            all_points[e[1]][0],
            all_points[e[1]][1],
        )
    )

    return extended_pads, edges
