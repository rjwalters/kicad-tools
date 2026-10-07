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

import math
from typing import TYPE_CHECKING, Any, Callable

from ..resource_guard import reraise_if_resource_exhaustion

if TYPE_CHECKING:
    from ..primitives import Pad


def relocate_blocked_point(
    gx: int,
    gy: int,
    is_blocked_fn: Callable[[int, int], bool],
    max_radius: int = 20,
) -> tuple[int, int]:
    """Relocate a grid cell to the nearest unblocked cell (Issue #3471).

    Synthetic Steiner branch points are virtual pads with no footprint
    ``ref``, so none of the off-grid / sub-grid pad rescues apply to
    them.  When :func:`build_rsmt` synthesises a branch point that lands
    on copper-blocked cells (board 05's ISENSE_A+ Steiner point landed
    on a MOSFET through-hole leg at (136.9, 176.0)), every A* edge
    incident to that point deterministically fails and the whole
    multi-terminal net is reported ``blocked_path`` -- even on an
    otherwise empty board.

    This helper performs a deterministic ring scan (increasing Chebyshev
    radius, then row-major within each ring) and returns the first cell
    for which ``is_blocked_fn`` is False.  If no free cell is found
    within ``max_radius`` cells the original coordinates are returned
    unchanged (legacy behaviour: the incident edges fail and the failure
    surfaces through the existing failure-callback path).

    Args:
        gx: Grid x of the candidate point.
        gy: Grid y of the candidate point.
        is_blocked_fn: Callable ``(gx, gy) -> bool`` returning True when
            the cell cannot host a routable Steiner point (e.g. blocked
            by foreign-net copper on every routable layer).
        max_radius: Maximum Chebyshev search radius in cells.

    Returns:
        ``(gx, gy)`` of the nearest free cell, or the input coordinates
        when none is found within the search radius.
    """
    if not is_blocked_fn(gx, gy):
        return gx, gy
    for radius in range(1, max_radius + 1):
        # Ring at Chebyshev distance ``radius``, scanned in deterministic
        # row-major order (dy outer, dx inner).
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue  # interior cells were checked at smaller radii
                cx, cy = gx + dx, gy + dy
                if not is_blocked_fn(cx, cy):
                    return cx, cy
    return gx, gy


def make_blocked_cell_predicate(
    grid: Any,
    rules: Any,
    net: int,
) -> Callable[[int, int], bool] | None:
    """Build the blocked-cell predicate used by Steiner-point relocation.

    Shared between the negotiated RSMT path
    (``NegotiatedRouter.route_net_negotiated``) and the MST/RSMT path
    (``MSTRouter.route_net`` -- the path the auto-layers two-phase flow
    executes, Issue #3471 board 05).  A branch point is only usable when
    a trace can actually be CENTERED there: the cell plus a
    trace-radius+clearance margin must be free on at least one routable
    layer.  A bare single-cell check relocates the point to the first
    free cell hugging the obstacle wall, where the A* start still fails
    the clearance expansion (measured on board 05: the point moved
    0.1 mm off the Q5 leg keepout and both incident edges kept failing).

    Failure-safe by construction: returns ``None`` when the grid lacks
    the required APIs (fixture/mock grids), and the returned predicate
    treats any per-cell exception as "free", so legacy snapping
    behaviour is preserved byte-for-byte on grids that cannot answer
    occupancy queries.

    Issue #4724: "any per-cell exception" excludes HOST resource
    exhaustion.  Every fallback here changes where a Steiner branch point
    lands -- i.e. it changes the routed result -- so absorbing a
    ``MemoryError`` from the grid's occupancy scan would silently make a
    loaded host route differently from a quiet one.  Each handler defers
    to :func:`~..resource_guard.reraise_if_resource_exhaustion` first;
    ordinary fixture/mock exceptions keep their historical fallback.

    Args:
        grid: ``RoutingGrid`` / ``CppGrid``-compatible object exposing
            ``get_routable_indices()``, ``is_blocked_for_net(gx, gy,
            layer_idx, net)`` and ``resolution``.
        rules: ``DesignRules``-compatible object exposing
            ``trace_width`` and ``trace_clearance`` (may be ``None``).
        net: Net id the Steiner points belong to (same-net copper does
            not block).

    Returns:
        ``(gx, gy) -> bool`` predicate, or ``None`` when the grid does
        not expose the required occupancy APIs.
    """
    try:
        routable_indices: list[int] = list(grid.get_routable_indices())
    except Exception:
        reraise_if_resource_exhaustion("steiner: routable-layer probe")
        return None
    if not routable_indices or not hasattr(grid, "is_blocked_for_net"):
        return None

    margin_cells = 1
    try:
        res = float(getattr(grid, "resolution", 0.0) or 0.0)
        if res > 0 and rules is not None:
            margin_cells = max(
                1,
                math.ceil((rules.trace_width / 2.0 + rules.trace_clearance) / res),
            )
    except Exception:
        # Fixture/mock rules without these attributes: keep the 1-cell
        # margin.
        reraise_if_resource_exhaustion("steiner: relocation-margin resolution")
        margin_cells = 1

    def _point_blocked(gx: int, gy: int) -> bool:
        try:
            for layer_idx in routable_indices:
                layer_ok = True
                for dy in range(-margin_cells, margin_cells + 1):
                    for dx in range(-margin_cells, margin_cells + 1):
                        if grid.is_blocked_for_net(gx + dx, gy + dy, layer_idx, net):
                            layer_ok = False
                            break
                    if not layer_ok:
                        break
                if layer_ok:
                    return False
            return True
        except Exception:
            reraise_if_resource_exhaustion("steiner: blocked-cell occupancy scan")
            return False

    return _point_blocked


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


# Issue #6019: below this many points per trial MST the scalar Prim is
# cheaper than the numpy set-up cost.  Both paths return bit-identical
# results (see ``_batched_trial_mst_costs``), so this is purely a speed knob.
_BATCHED_MIN_POINTS = 8


def _batched_trial_mst_costs(
    points: list[tuple[float, float]],
    candidates: list[tuple[float, float]],
) -> list[float]:
    """``[_mst_cost(points + [c]) for c in candidates]``, batched in numpy.

    Issue #6019.  Runs one Prim per candidate, vectorised across all
    candidates, and returns values **bit-identical** to the scalar
    :func:`_mst_cost` with Manhattan distance.  Exactness rests on three
    properties, each mirrored from :func:`_build_mst_edges`:

    1. *Same per-edge value.*  ``abs(xi - xj) + abs(yi - yj)`` evaluated
       elementwise in float64 is the same IEEE operation sequence as the
       scalar ``_manhattan`` (and ``a - b`` / ``b - a`` differ only in
       sign, which ``abs`` removes exactly).  The step minimum is a
       ``min`` over those values, which is order-independent.
    2. *Same summation order.*  The total is accumulated one Prim step at
       a time from ``0.0`` -- the same left fold ``_mst_cost`` performs
       over the edges in the order Prim appended them.
    3. *Same tie-break.*  The scalar Prim takes the first minimum in
       ``for i in connected: for j in unconnected`` order, i.e. CPython
       ``set`` iteration order, which depends on insertion history (it is
       not ascending once indices collide in the hash table).  A plain
       ``argmin`` therefore diverges whenever two *different* unconnected
       vertices tie for the minimum -- the divergence the PR #6022 review
       measured on 44 of 2000 random point sets.  Rows with such a tie
       replay the real ``set`` objects with the scalar's exact insertion
       sequence and pick the first minimum in their actual iteration
       order.  Ties between different ``i`` for the same ``j`` cannot
       change the cost or the connected set, so they need no resolution.

    The final tree's *edges* are still produced by the scalar
    :func:`_build_mst_edges`; this function only scores candidates.
    """
    import numpy as np

    n_cand = len(candidates)
    m = len(points) + 1
    base = np.asarray(points, dtype=np.float64)
    cand = np.asarray(candidates, dtype=np.float64)
    xs = np.empty((n_cand, m), dtype=np.float64)
    ys = np.empty((n_cand, m), dtype=np.float64)
    xs[:, :-1] = base[:, 0]
    ys[:, :-1] = base[:, 1]
    xs[:, -1] = cand[:, 0]
    ys[:, -1] = cand[:, 1]

    rows = np.arange(n_cand)
    # +inf on vertices already in the tree, 0.0 elsewhere; adding it to a
    # (non-negative, finite) distance is exact and masks tree vertices.
    in_tree_pen = np.zeros((n_cand, m), dtype=np.float64)
    in_tree_pen[:, 0] = np.inf
    key = np.abs(xs - xs[:, :1]) + np.abs(ys - ys[:, :1])
    key[:, 0] = np.inf
    total = np.zeros(n_cand, dtype=np.float64)
    # Prim insertion sequence per row (column 0 is the seed vertex 0).
    order = np.zeros((n_cand, m), dtype=np.intp)

    # Lazily replayed scalar sets for rows that hit a tie:
    # row -> [connected, unconnected, number of order entries replayed].
    replay: dict[int, list[Any]] = {}

    for step in range(1, m):
        chosen = key.argmin(axis=1)  # lowest tied index; resolved below
        step_min = key[rows, chosen]
        tied = key == step_min[:, None]
        n_tied = np.count_nonzero(tied, axis=1)
        amb = np.flatnonzero(n_tied > 1)
        if amb.size:
            # Replay the scalar's sets for every ambiguous row and read the
            # real ``connected`` iteration order (``step`` vertices each).
            amb_rows = amb.tolist()
            unconnected_of: list[set[int]] = []
            conn_flat: list[int] = []
            for r in amb_rows:
                state = replay.get(r)
                if state is None:
                    # Built exactly as ``_build_mst_edges`` builds them.
                    state = [{0}, set(range(1, m)), 1]
                    replay[r] = state
                connected, unconnected, done = state
                for j in order[r, done:step].tolist():
                    connected.add(j)
                    unconnected.remove(j)
                state[2] = step
                conn_flat.extend(connected)
                unconnected_of.append(unconnected)
            k = len(amb_rows)
            kr = np.arange(k)
            conn = np.array(conn_flat, dtype=np.intp).reshape(k, step)
            counts = n_tied[amb]
            jmax = int(counts.max())
            # Tied vertices in ascending index order, padded to ``jmax``.
            tied_js = np.argsort(~tied[amb], axis=1, kind="stable")[:, :jmax]
            j_valid = np.arange(jmax)[None, :] < counts[:, None]
            ra = amb[:, None]
            xc, yc = xs[ra, conn], ys[ra, conn]
            xj, yj = xs[ra, tied_js], ys[ra, tied_js]
            # (i connected, j unconnected) in the scalar's operand order.
            d = np.abs(xc[:, :, None] - xj[:, None, :]) + np.abs(yc[:, :, None] - yj[:, None, :])
            hit = (d == step_min[amb][:, None, None]) & j_valid[:, None, :]
            i_hit = hit.any(axis=2)
            if not bool(i_hit.any(axis=1).all()):
                # Unreachable while ``step_min`` is an exact minimum of the
                # same values; if that invariant ever broke, give up on
                # batching rather than return a different tree.
                return [_mst_cost(points + [c]) for c in candidates]
            # First connected vertex (in set order) with a minimum edge ...
            first_hits = hit[kr, i_hit.argmax(axis=1)]
            picks = tied_js[kr, first_hits.argmax(axis=1)]
            # ... then the first of its hits in ``unconnected`` set order.
            for t in np.flatnonzero(first_hits.sum(axis=1) > 1).tolist():
                hit_js = set(tied_js[t][first_hits[t]].tolist())
                picks[t] = next(j for j in unconnected_of[t] if j in hit_js)
            chosen[amb] = picks

        total += step_min
        order[:, step] = chosen
        in_tree_pen[rows, chosen] = np.inf
        key[rows, chosen] = np.inf
        d = np.abs(xs - xs[rows, chosen][:, None])
        d += np.abs(ys - ys[rows, chosen][:, None])
        d += in_tree_pen
        np.minimum(key, d, out=key)

    costs: list[float] = total.tolist()
    return costs


def _trial_mst_costs(
    all_points: list[tuple[float, float]],
    candidates: list[tuple[float, float]],
    cost_fn: Callable[[float, float, float, float], float] | None,
) -> list[float]:
    """MST cost of ``all_points + [c]`` for each candidate ``c``.

    Dispatches to the batched numpy Prim (Issue #6019) for the default
    Manhattan cost on finite coordinates; custom ``cost_fn`` (the
    congestion-aware router path) and degenerate inputs keep the scalar
    path.  Both return bit-identical values.
    """
    if (
        cost_fn is None
        and len(all_points) + 1 >= _BATCHED_MIN_POINTS
        and all(math.isfinite(c) for p in all_points for c in p)
        and all(math.isfinite(c) for p in candidates for c in p)
    ):
        return _batched_trial_mst_costs(all_points, candidates)
    return [_mst_cost(all_points + [c], cost_fn) for c in candidates]


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

        trial_costs = _trial_mst_costs(all_points, candidates, cost_fn)
        for candidate, trial_cost in zip(candidates, trial_costs, strict=True):
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
    steiner_cost = sum(dist_fn(t[0], t[1], steiner[0], steiner[1]) for t in terminals)

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
    *,
    allow_kelvin: bool = True,
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
        allow_kelvin: When True (default), recognise current-sense /
            Kelvin sense nets (Issue #4473) and route them as a *star
            rooted at the shunt/sense-resistor pad* instead of an
            arbitrary RSMT.  A Kelvin sense tap must connect AT the shunt
            pad (the Kelvin point) and must not share the high-current
            segment; the generic RSMT's arbitrary Steiner branch points
            violate that.  Detection is conservative (net-name pattern
            AND a resolvable shunt resistor pad) so ordinary nets are
            untouched -- see :mod:`kicad_tools.router.kelvin`.  Set False
            to force the plain RSMT (used by tests exercising the RSMT
            path directly).

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

    # Issue #4473: Kelvin / current-sense nets get a constrained star
    # topology (sense trace connects AT the Kelvin point, no shared
    # high-current segment) instead of an arbitrary RSMT.  Returns None
    # for every non-sense net, so this is a no-op for ordinary nets.
    if allow_kelvin:
        from ..kelvin import build_kelvin_topology

        kelvin = build_kelvin_topology(pad_objs)
        if kelvin is not None:
            return kelvin

    # Extract coordinates
    terminals = [(p.x, p.y) for p in pad_objs]

    # Choose algorithm based on terminal count
    if n == 3:
        all_points, edges = _solve_3_terminal(terminals, congestion_fn)
    elif n <= 9:
        all_points, edges = _iterative_one_steiner(terminals, congestion_fn, max_iterations=50)
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
