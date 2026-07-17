"""Triangle-dual navmesh + portal-midpoint A* for the mesh router (#4268).

Given the ``(vertices, triangles)`` output of the poly2tri constrained-
Delaunay mesh, this builds the triangle adjacency graph and runs A* over the
triangle dual using the **portal-midpoint** step cost.  The ADR (P0.5 spike,
CASE 2) settled on portal-midpoint cost over naive centroid cost: centroid A*
picks suboptimal corridors (1.341x straight-line) while portal-midpoint keeps
it near-optimal (1.055x) with no mesh refinement.

The A* result is a *corridor* (triangle sequence), converted to an oriented
``(left, right)`` portal list for the funnel string-pull, which produces the
actual Euclidean geodesic.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable

from .funnel import Portal
from .geometry import EPS, Pt, centroid, merge_intervals, point_in_triangle

Triangle = tuple[int, int, int]

# A step of a multi-layer corridor (issue #4276): the triangle, the copper
# layer index it is traversed on, the point at which the corridor entered that
# ``(triangle, layer)`` node, and whether the entering edge was a via (a
# layer-change hop) rather than an in-layer portal crossing.
LayeredStep = tuple[int, int, Pt, bool]

# Callables the 2.5D A* consults so the navmesh stays geometry-only and the
# per-layer obstacle / manufacturability policy lives in the pathfinder:
#   * ``PortalBlockedFn(edge, layer)`` -> True if committed copper on ``layer``
#     crosses that portal (the per-layer analogue of P2's committed copper, so
#     A* has a cost reason to dip to a clear layer, issue #4276 section 3).
#   * ``ViaAllowedFn(site, layer_a, layer_b)`` -> True if a via at ``site`` is
#     DRC-legal on BOTH layers it joins (obstacle-aware placement at *search*
#     time -- the #3906 lesson, issue #4276 section 5).
PortalBlockedFn = Callable[[tuple[int, int], int], bool]
ViaAllowedFn = Callable[[Pt, int, int], bool]


class NavMesh:
    """Triangle-dual navmesh over a constrained-Delaunay triangulation."""

    def __init__(
        self,
        vertices: list[Pt],
        triangles: list[Triangle],
        channel: float = 0.0,
    ) -> None:
        self.vertices = vertices
        self.triangles = triangles
        # Issue #4269: ``channel`` = trace width + 2*clearance, the lane pitch a
        # portal (shared triangle edge) must fit an integer number of nets into.
        # ``capacity = floor(edge_len / channel)`` (P0.5 measured 2/12/64 lanes
        # across a test corridor).  ``channel <= 0`` disables the capacity model
        # (portals are treated as effectively unbounded), preserving the P1
        # single-net contract for callers that never negotiate.
        self.channel = channel
        # Per-portal (shared-edge key) occupancy + history congestion tables.
        # Keyed by the same sorted vertex-index pairs used in ``_adj`` so the
        # A* step and the negotiation bookkeeping speak the same portal id.
        self._occupancy: dict[tuple[int, int], int] = {}
        self._history: dict[tuple[int, int], float] = {}
        self._centroids: list[Pt] = [
            centroid(vertices[a], vertices[b], vertices[c]) for (a, b, c) in triangles
        ]
        # edge (sorted vertex-index pair) -> list of incident triangle indices
        self._edge_tris: dict[tuple[int, int], list[int]] = {}
        for ti, (a, b, c) in enumerate(triangles):
            for u, v in ((a, b), (b, c), (c, a)):
                key = (u, v) if u < v else (v, u)
                self._edge_tris.setdefault(key, []).append(ti)
        # adjacency: triangle -> list of (neighbor triangle, shared edge)
        self._adj: list[list[tuple[int, tuple[int, int]]]] = [[] for _ in triangles]
        for key, tris in self._edge_tris.items():
            if len(tris) == 2:
                t0, t1 = tris
                self._adj[t0].append((t1, key))
                self._adj[t1].append((t0, key))
        # vertex index -> incident triangles (for endpoint location)
        self._vertex_tris: dict[int, list[int]] = {}
        for ti, (a, b, c) in enumerate(triangles):
            for v in (a, b, c):
                self._vertex_tris.setdefault(v, []).append(ti)

    # -- endpoint location -------------------------------------------------

    def _vertex_index(self, p: Pt) -> int | None:
        """Return the vertex index coincident with ``p`` (or None)."""
        best: int | None = None
        best_d = EPS
        for i, v in enumerate(self.vertices):
            d = math.hypot(v[0] - p[0], v[1] - p[1])
            if d <= best_d:
                best_d = d
                best = i
        return best

    def locate(self, p: Pt) -> list[int]:
        """Triangles that ``p`` belongs to.

        A Steiner endpoint is a mesh vertex shared by a fan of triangles; a
        free point falls inside exactly one.  Returns all candidate triangles
        so A* can seed / terminate on any of them.
        """
        vi = self._vertex_index(p)
        if vi is not None and vi in self._vertex_tris:
            return list(self._vertex_tris[vi])
        hits = [
            ti
            for ti, (a, b, c) in enumerate(self.triangles)
            if point_in_triangle(p, self.vertices[a], self.vertices[b], self.vertices[c])
        ]
        return hits

    # -- portal capacity / occupancy / congestion (issue #4269) -----------

    def edge_length(self, edge: tuple[int, int]) -> float:
        """Euclidean length of a portal (shared triangle edge)."""
        a = self.vertices[edge[0]]
        b = self.vertices[edge[1]]
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def capacity(self, edge: tuple[int, int]) -> int:
        """Integer lane count of a portal: ``floor(edge_len / channel)``.

        ``channel <= 0`` (capacity model disabled) reports a very large
        capacity so no portal is ever congested -- the single-net P1 default.
        """
        if self.channel <= 0.0:
            return 1_000_000_000
        return int(self.edge_length(edge) / self.channel)

    def occupancy(self, edge: tuple[int, int]) -> int:
        """Number of nets currently routed across a portal."""
        return self._occupancy.get(edge, 0)

    def history(self, edge: tuple[int, int]) -> float:
        """Accumulated cross-iteration history congestion for a portal."""
        return self._history.get(edge, 0.0)

    def commit_portal(self, edge: tuple[int, int]) -> None:
        """Record one more net crossing this portal (a committed route)."""
        self._occupancy[edge] = self._occupancy.get(edge, 0) + 1

    def release_portal(self, edge: tuple[int, int]) -> None:
        """Undo one net crossing (rip-up)."""
        cur = self._occupancy.get(edge, 0)
        if cur > 0:
            self._occupancy[edge] = cur - 1

    def reset_occupancy(self) -> None:
        """Clear all present occupancy (start of a fresh negotiation pass).

        History is deliberately preserved -- it is the persistent memory that
        drives PathFinder/VPR convergence across iterations.
        """
        self._occupancy.clear()

    def add_history(self, edge: tuple[int, int], increment: float) -> None:
        """Bump a portal's persistent history congestion (over-capacity penalty)."""
        self._history[edge] = self._history.get(edge, 0.0) + increment

    def occupied_portals(self) -> list[tuple[int, int]]:
        """Portal keys with non-zero present occupancy."""
        return [e for e, occ in self._occupancy.items() if occ > 0]

    def portal_penalty(
        self,
        edge: tuple[int, int],
        present_cost_factor: float,
        cost_congestion: float,
        congestion_threshold: float,
    ) -> float:
        """Unitless congestion multiplier for a portal (PathFinder present+history).

        Density is ``(occupancy + 1) / capacity`` -- the ``+1`` prices the
        portal as if *this* net also used it, so a route steers away from lanes
        already at capacity.  When density exceeds ``congestion_threshold`` the
        present term grows linearly (mirrors the grid ``cost_congestion`` /
        ``congestion_threshold`` semantics, ``rules.py:164-165``); the history
        term is added unscaled so persistently-overused portals ratchet up
        across iterations and the negotiation converges.
        """
        cap = self.capacity(edge)
        occ = self.occupancy(edge)
        if cap <= 0:
            # A portal too narrow for even one net: any use is over capacity.
            density = float(occ + 1)
        else:
            density = (occ + 1) / cap
        present = 0.0
        if density > congestion_threshold:
            present = cost_congestion * (density - congestion_threshold)
        return present_cost_factor * present + self.history(edge)

    def corridor_portals(self, corridor: list[int]) -> list[tuple[int, int]]:
        """Portal (shared-edge) keys crossed by a triangle corridor."""
        portals: list[tuple[int, int]] = []
        for i in range(len(corridor) - 1):
            edge = self._shared_edge(corridor[i], corridor[i + 1])
            if edge is not None:
                portals.append(edge)
        return portals

    # -- A* over the triangle dual ----------------------------------------

    def astar(
        self,
        start: Pt,
        goal: Pt,
        *,
        present_cost_factor: float = 0.0,
        cost_congestion: float = 0.0,
        congestion_threshold: float = 0.0,
    ) -> list[int] | None:
        """Return a corridor (triangle-index sequence) from ``start`` to ``goal``.

        Portal-midpoint step cost with a straight-line-to-goal heuristic.
        Returns ``None`` if the two points are in disconnected mesh regions.

        Issue #4269: when ``present_cost_factor`` is non-zero (or any history
        has accumulated) the per-portal congestion penalty multiplies the step
        cost, so committed copper raises the price of a shared corridor and the
        next net is steered elsewhere.  With the defaults (``present_cost_factor
        == 0`` and no history) the arithmetic reduces to the P1 portal-midpoint
        distance exactly -- the single-net path is unchanged.
        """
        start_tris = self.locate(start)
        goal_tris = set(self.locate(goal))
        if not start_tris or not goal_tris:
            return None

        negotiating = present_cost_factor != 0.0 or bool(self._history)

        def edge_mid(edge: tuple[int, int]) -> Pt:
            a = self.vertices[edge[0]]
            b = self.vertices[edge[1]]
            return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

        def h(point: Pt) -> float:
            return math.hypot(goal[0] - point[0], goal[1] - point[1])

        # Priority queue of (f, unique, triangle, entry_point).
        g_score: dict[int, float] = {}
        came_from: dict[int, int] = {}
        counter = 0
        open_heap: list[tuple[float, int, int, Pt]] = []
        for t in start_tris:
            g_score[t] = 0.0
            heapq.heappush(open_heap, (h(start), counter, t, start))
            counter += 1

        while open_heap:
            f, _cnt, tri, entry = heapq.heappop(open_heap)
            g = g_score.get(tri, math.inf)
            # Stale-entry guard: f encodes g at push time; recompute is cheap.
            if f - h(entry) > g + 1e-6:
                continue
            if tri in goal_tris:
                return self._reconstruct(came_from, tri, start_tris)
            for nbr, edge in self._adj[tri]:
                mid = edge_mid(edge)
                step = math.hypot(mid[0] - entry[0], mid[1] - entry[1])
                if negotiating:
                    penalty = self.portal_penalty(
                        edge, present_cost_factor, cost_congestion, congestion_threshold
                    )
                    step = step * (1.0 + penalty)
                tentative = g + step
                if tentative < g_score.get(nbr, math.inf) - 1e-9:
                    g_score[nbr] = tentative
                    came_from[nbr] = tri
                    heapq.heappush(open_heap, (tentative + h(mid), counter, nbr, mid))
                    counter += 1
        return None

    @staticmethod
    def _reconstruct(came_from: dict[int, int], tri: int, start_tris: list[int]) -> list[int]:
        start_set = set(start_tris)
        corridor = [tri]
        while tri not in start_set and tri in came_from:
            tri = came_from[tri]
            corridor.append(tri)
        corridor.reverse()
        return corridor

    # -- multi-layer A* over identically-meshed layers (issue #4276) -------

    def astar_layered(
        self,
        start: Pt,
        goal: Pt,
        start_layer: int,
        goal_layer: int,
        num_layers: int,
        portal_blocked: PortalBlockedFn,
        via_allowed: ViaAllowedFn,
        via_cost: float,
        *,
        present_cost_factor: float = 0.0,
        cost_congestion: float = 0.0,
        congestion_threshold: float = 0.0,
    ) -> list[LayeredStep] | None:
        """A* over the ``(triangle, layer)`` graph -- the 2.5D via-injection core.

        The single static triangulation is *replicated* across ``num_layers``
        identically-meshed copper layers (no per-layer re-triangulation -- the
        static-mesh-once invariant holds).  A* nodes are ``(triangle, layer)``;
        edges are:

        * **in-layer** triangle adjacency, skipped when ``portal_blocked(edge,
          layer)`` reports committed copper crosses that portal on that layer
          (this is what gives the search a cost reason to *dip* rather than
          plough through the blocked corridor); and
        * **via** hops connecting ``(tri, L) <-> (tri, L+/-1)`` at the current
          **portal-midpoint** site, cost ``via_cost``, admitted only when
          ``via_allowed(site, L, L')`` proves the via DRC-legal on both layers.

        Vias are only taken at portal midpoints (never at the start pad), so an
        injected via is a free-space through-via by construction.  Returns the
        corridor as a list of :data:`LayeredStep` (``(tri, layer, entry, via)``)
        from ``start`` to ``goal``, or ``None`` if disconnected.  With a single
        layer (``num_layers == 1``) no via edge is ever generated and the result
        reduces to a same-layer corridor.
        """
        start_tris = self.locate(start)
        goal_tris = set(self.locate(goal))
        if not start_tris or not goal_tris:
            return None

        negotiating = present_cost_factor != 0.0 or bool(self._history)

        def edge_mid(edge: tuple[int, int]) -> Pt:
            a = self.vertices[edge[0]]
            b = self.vertices[edge[1]]
            return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

        def h(point: Pt) -> float:
            return math.hypot(goal[0] - point[0], goal[1] - point[1])

        State = tuple[int, int]  # (triangle, layer)
        g_score: dict[State, float] = {}
        # came_from[state] = (prev_state, entry_point_at_state, via_into_state)
        came_from: dict[State, tuple[State, Pt, bool]] = {}
        counter = 0
        # heap item: (f, unique, tri, layer, entry, at_portal)
        open_heap: list[tuple[float, int, int, int, Pt, bool]] = []
        for t in start_tris:
            g_score[(t, start_layer)] = 0.0
            heapq.heappush(open_heap, (h(start), counter, t, start_layer, start, False))
            counter += 1

        while open_heap:
            f, _cnt, tri, layer, entry, at_portal = heapq.heappop(open_heap)
            st: State = (tri, layer)
            g = g_score.get(st, math.inf)
            if f - h(entry) > g + 1e-6:
                continue
            if tri in goal_tris and layer == goal_layer:
                return self._reconstruct_layered(came_from, st, start)

            # In-layer triangle adjacency (masked per layer by committed copper).
            for nbr, edge in self._adj[tri]:
                if portal_blocked(edge, layer):
                    continue
                mid = edge_mid(edge)
                step = math.hypot(mid[0] - entry[0], mid[1] - entry[1])
                if negotiating:
                    penalty = self.portal_penalty(
                        edge, present_cost_factor, cost_congestion, congestion_threshold
                    )
                    step = step * (1.0 + penalty)
                tentative = g + step
                nst: State = (nbr, layer)
                if tentative < g_score.get(nst, math.inf) - 1e-9:
                    g_score[nst] = tentative
                    came_from[nst] = (st, mid, False)
                    heapq.heappush(open_heap, (tentative + h(mid), counter, nbr, layer, mid, True))
                    counter += 1

            # Via hops: only at a genuine portal-midpoint site (never the pad).
            if at_portal and num_layers > 1:
                for target in (layer - 1, layer + 1):
                    if target < 0 or target >= num_layers:
                        continue
                    if not via_allowed(entry, layer, target):
                        continue
                    tentative = g + via_cost
                    nst = (tri, target)
                    if tentative < g_score.get(nst, math.inf) - 1e-9:
                        g_score[nst] = tentative
                        came_from[nst] = (st, entry, True)
                        heapq.heappush(
                            open_heap, (tentative + h(entry), counter, tri, target, entry, True)
                        )
                        counter += 1
        return None

    @staticmethod
    def _reconstruct_layered(
        came_from: dict[tuple[int, int], tuple[tuple[int, int], Pt, bool]],
        goal_st: tuple[int, int],
        start_pt: Pt,
    ) -> list[LayeredStep]:
        chain: list[LayeredStep] = []
        st = goal_st
        while st in came_from:
            prev_st, entry_at_st, via_into = came_from[st]
            chain.append((st[0], st[1], entry_at_st, via_into))
            st = prev_st
        # ``st`` is now a start node: its entry point is the start pad.
        chain.append((st[0], st[1], start_pt, False))
        chain.reverse()
        return chain

    # -- corridor -> oriented portals -------------------------------------

    def corridor_to_portals(
        self,
        corridor: list[int],
        start: Pt,
        goal: Pt,
        consumed: dict[tuple[int, int], list[tuple[float, float]]] | None = None,
        pack: str = "largest",
    ) -> list[Portal]:
        """Convert a triangle corridor into oriented ``(left, right)`` portals.

        Each portal is the shared edge between consecutive corridor triangles,
        oriented so ``left`` sits on the left-hand side of travel (the funnel's
        sign convention).  The near-triangle centroid is the orientation
        reference: it always lies on the traveling-from side of the edge.

        Issue #4274 (in-corridor lane assignment): ``consumed`` maps each portal
        edge key to the parametric ``[t0, t1]`` intervals (along
        ``vertices[edge[0]] -> vertices[edge[1]]``) already occupied by committed
        copper.  When supplied, each crossed portal's opening is narrowed to the
        residual sub-segment so the funnel yields a *parallel* geodesic for the
        next net instead of the same taut path.  ``consumed`` defaults to
        ``None``, so the single-net P1/P2 contract (full openings) is unchanged
        byte-for-byte.
        """
        portals: list[Portal] = [(start, start)]
        for i in range(len(corridor) - 1):
            cur = corridor[i]
            nxt = corridor[i + 1]
            edge = self._shared_edge(cur, nxt)
            if edge is None:
                continue
            p = self.vertices[edge[0]]
            q = self.vertices[edge[1]]
            ref = self._centroids[cur]
            # Mononen sign: _area(ref, left, right) > 0 for correct orientation.
            # ``reversed_`` records whether ``left`` is ``q`` (edge[1]) so the
            # consumed intervals (parametric p->q) map into the left->right frame.
            if _mononen_area(ref, p, q) > 0.0:
                left, right, reversed_ = p, q, False
            else:
                left, right, reversed_ = q, p, True
            if consumed:
                bands = consumed.get(edge)
                if bands:
                    left, right = _narrow_portal(left, right, reversed_, bands, pack)
            portals.append((left, right))
        portals.append((goal, goal))
        return portals

    def _shared_edge(self, t0: int, t1: int) -> tuple[int, int] | None:
        s0 = set(self.triangles[t0])
        s1 = set(self.triangles[t1])
        common = tuple(sorted(s0 & s1))
        if len(common) == 2:
            return (common[0], common[1])
        return None


def _narrow_portal(
    left: Pt,
    right: Pt,
    reversed_: bool,
    bands: list[tuple[float, float]],
    pack: str = "largest",
) -> Portal:
    """Clip an oriented portal ``(left, right)`` to a residual free opening (#4274).

    ``bands`` are the consumed ``[t0, t1]`` intervals in the *edge* frame
    (``vertices[edge[0]] -> vertices[edge[1]]``); ``reversed_`` says whether the
    oriented ``left`` is ``edge[1]`` (so ``s = 1 - t``).  Work in the oriented
    ``s`` frame (``s = 0`` at ``left``, ``s = 1`` at ``right``): subtract the
    consumed bands from ``[0, 1]`` to get the free gaps and hand the funnel one
    of them (still oriented left/right).  ``pack`` selects which gap, giving the
    re-funnel a small family of parallel-lane candidates:

    * ``"largest"`` -- the widest free gap (the roomiest lane);
    * ``"left"`` -- the free gap nearest the ``left`` wall (``s = 0``);
    * ``"right"`` -- the free gap nearest the ``right`` wall (``s = 1``).

    ``"left"`` / ``"right"`` are the **monotone one-wall** disciplines: every
    portal along the corridor packs against the *same* wall, so the funnel is
    forced consistently to one side of the committed copper and successive lanes
    stack in parallel rather than weaving across each other.  A fully-consumed
    portal collapses to a near-degenerate opening whose funnel path the
    octilinear fit declines rather than a short.
    """
    # Consumed bands -> oriented s-frame, then merged.
    s_bands: list[tuple[float, float]] = []
    for t0, t1 in bands:
        if reversed_:
            s0, s1 = 1.0 - t1, 1.0 - t0
        else:
            s0, s1 = t0, t1
        s_bands.append((max(0.0, min(1.0, s0)), max(0.0, min(1.0, s1))))
    consumed = merge_intervals(s_bands)
    if not consumed:
        return left, right

    # Complement of the consumed bands within [0, 1] -> free gaps.
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for c0, c1 in consumed:
        if c0 > cursor:
            gaps.append((cursor, c0))
        cursor = max(cursor, c1)
    if cursor < 1.0:
        gaps.append((cursor, 1.0))
    if not gaps:
        # Portal fully consumed: collapse toward the right wall so the funnel
        # yields a path the octilinear fit declines (never a short).
        return right, right

    if pack == "left":
        lo, hi = gaps[0]
    elif pack == "right":
        lo, hi = gaps[-1]
    else:
        lo, hi = max(gaps, key=lambda g: g[1] - g[0])
    lx = left[0] + lo * (right[0] - left[0])
    ly = left[1] + lo * (right[1] - left[1])
    rx = left[0] + hi * (right[0] - left[0])
    ry = left[1] + hi * (right[1] - left[1])
    return (lx, ly), (rx, ry)


def _mononen_area(a: Pt, b: Pt, c: Pt) -> float:
    """Mononen ``triarea2`` sign (matches :func:`funnel._area`)."""
    ax = b[0] - a[0]
    ay = b[1] - a[1]
    bx = c[0] - a[0]
    by = c[1] - a[1]
    return bx * ay - ax * by
