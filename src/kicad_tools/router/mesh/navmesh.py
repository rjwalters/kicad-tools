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

from .funnel import Portal
from .geometry import EPS, Pt, centroid, point_in_triangle

Triangle = tuple[int, int, int]


class NavMesh:
    """Triangle-dual navmesh over a constrained-Delaunay triangulation."""

    def __init__(self, vertices: list[Pt], triangles: list[Triangle]) -> None:
        self.vertices = vertices
        self.triangles = triangles
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

    # -- A* over the triangle dual ----------------------------------------

    def astar(self, start: Pt, goal: Pt) -> list[int] | None:
        """Return a corridor (triangle-index sequence) from ``start`` to ``goal``.

        Portal-midpoint step cost with a straight-line-to-goal heuristic.
        Returns ``None`` if the two points are in disconnected mesh regions.
        """
        start_tris = self.locate(start)
        goal_tris = set(self.locate(goal))
        if not start_tris or not goal_tris:
            return None

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

    # -- corridor -> oriented portals -------------------------------------

    def corridor_to_portals(self, corridor: list[int], start: Pt, goal: Pt) -> list[Portal]:
        """Convert a triangle corridor into oriented ``(left, right)`` portals.

        Each portal is the shared edge between consecutive corridor triangles,
        oriented so ``left`` sits on the left-hand side of travel (the funnel's
        sign convention).  The near-triangle centroid is the orientation
        reference: it always lies on the traveling-from side of the edge.
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
            if _mononen_area(ref, p, q) > 0.0:
                portals.append((p, q))
            else:
                portals.append((q, p))
        portals.append((goal, goal))
        return portals

    def _shared_edge(self, t0: int, t1: int) -> tuple[int, int] | None:
        s0 = set(self.triangles[t0])
        s1 = set(self.triangles[t1])
        common = tuple(sorted(s0 & s1))
        if len(common) == 2:
            return (common[0], common[1])
        return None


def _mononen_area(a: Pt, b: Pt, c: Pt) -> float:
    """Mononen ``triarea2`` sign (matches :func:`funnel._area`)."""
    ax = b[0] - a[0]
    ay = b[1] - a[1]
    bx = c[0] - a[0]
    by = c[1] - a[1]
    return bx * ay - ax * by
