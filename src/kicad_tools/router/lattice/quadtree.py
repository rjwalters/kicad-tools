"""Balanced-quadtree octilinear lattice generator (issue #4278, P2.7).

The lattice is a quadtree decomposition of the board bounding box into
square cells: coarse in open space, fine near pads (fine size is a
*per-region* parameter -- :class:`RefineRegion` -- so pad clusters with
different pitches can request different densities, issue #4278 risk 5).

Graph shape (the substrate the #4267 lattice spike validated):

* **nodes** = cell corners + cell centers (+ balanced T-junction side
  midpoints);
* **edges** = axis-aligned cell sides (split at the midpoint when the
  neighbor across is one level finer) + the 4 corner->center
  half-diagonals.

Every edge is 0/45/90/135 degrees **by construction**, and edges meet only
at shared nodes (planar), so an A* path over the lattice IS 45-degree-legal
copper -- no funnel stage, no octilinear post-fit stage exists downstream.

The load-bearing invariant is **balanced refinement**: adjacent leaves
differ by at most one level, so every T-junction splits a side exactly at
its midpoint and the corner->midpoint sub-edges remain axis-aligned.
Octilinearity survives refinement boundaries because of this invariant --
it is property-tested before any routing test
(``tests/router/lattice/test_quadtree_properties.py``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .geometry import Pt, Rect, dist, rects_overlap

NodeKey = tuple[int, int]
EdgeKey = tuple[NodeKey, NodeKey]

# Quadtree cell identifier: (level, i, j) with cell size = coarse / 2**level.
CellKey = tuple[int, int, int]

_SIDES: dict[str, tuple[int, int]] = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}


@dataclass(frozen=True)
class RefineRegion:
    """A rectangular region requesting a local cell size of ``fine`` mm.

    Density is a per-region parameter (not a global constant) so pad
    clusters can request pitch-derived cell sizes (issue #4278 risk 5).
    The generator refines any cell intersecting ``rect`` until the cell
    size is <= ``fine`` (snapped to the nearest power-of-two division of
    the coarse cell).
    """

    rect: Rect
    fine: float

    def level_for(self, coarse: float) -> int:
        """Quadtree level whose cell size first reaches ``fine``."""
        if self.fine >= coarse:
            return 0
        return max(0, math.ceil(math.log2(coarse / self.fine) - 1e-9))


class OctilinearLattice:
    """Balanced quadtree lattice over a rectangular board region.

    Attributes:
        nodes: node key -> exact (x, y) coordinate.  Node keys are integer
            multiples of :attr:`unit` from :attr:`origin`, so coincident
            corners/midpoints/centers from different cells snap to the SAME
            node (this is what makes the graph planar).
        adj: node key -> list of ``(neighbor_key, edge_length)``.
        edges: set of undirected edges as ``(min_key, max_key)`` tuples.
        leaves: the balanced quadtree leaf cells (for tests/diagnostics).
    """

    def __init__(
        self,
        bbox: Rect,
        refine_regions: list[RefineRegion],
        *,
        coarse: float = 3.2,
    ) -> None:
        if coarse <= 0:
            raise ValueError(f"coarse cell size must be positive, got {coarse}")
        self.bbox = bbox
        self.coarse = coarse
        self.refine_regions = list(refine_regions)
        self.max_level = max((r.level_for(coarse) for r in self.refine_regions), default=0)
        # Finest cell size actually representable, and the node-coordinate
        # quantum (half the finest cell: centers sit at half-cell offsets).
        self.fine = coarse / (2**self.max_level)
        self.unit = self.fine / 2.0
        self.origin: Pt = (bbox[0], bbox[1])
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        self.nx = max(1, math.ceil(width / coarse - 1e-9))
        self.ny = max(1, math.ceil(height / coarse - 1e-9))
        self._region_levels: list[tuple[Rect, int]] = [
            (r.rect, r.level_for(coarse)) for r in self.refine_regions
        ]

        self.leaves: set[CellKey] = self._build_quadtree()
        self._balance()
        self.nodes: dict[NodeKey, Pt]
        self.adj: dict[NodeKey, list[tuple[NodeKey, float]]]
        self.edges: set[EdgeKey]
        self.nodes, self.adj, self.edges = self._build_graph()

    # -- quadtree ----------------------------------------------------------

    def cell_rect(self, level: int, i: int, j: int) -> tuple[Rect, float]:
        """World-coordinate rectangle and side length of cell ``(level, i, j)``."""
        s = self.coarse / (2**level)
        x0 = self.origin[0] + i * s
        y0 = self.origin[1] + j * s
        return (x0, y0, x0 + s, y0 + s), s

    def _needs_refine(self, rect: Rect, level: int) -> bool:
        """True if a refine region intersecting ``rect`` wants a finer level."""
        return any(
            region_level > level and rects_overlap(rect, region_rect)
            for region_rect, region_level in self._region_levels
        )

    def _cell_in_bounds(self, rect: Rect) -> bool:
        """False for cells whose origin already lies past the board bbox."""
        return rect[0] < self.bbox[2] - 1e-9 and rect[1] < self.bbox[3] - 1e-9

    def _build_quadtree(self) -> set[CellKey]:
        leaves: set[CellKey] = set()
        stack: list[CellKey] = [(0, i, j) for i in range(self.nx) for j in range(self.ny)]
        while stack:
            level, i, j = stack.pop()
            rect, _s = self.cell_rect(level, i, j)
            if not self._cell_in_bounds(rect):
                continue
            if level < self.max_level and self._needs_refine(rect, level):
                for di in (0, 1):
                    for dj in (0, 1):
                        stack.append((level + 1, 2 * i + di, 2 * j + dj))
            else:
                leaves.add((level, i, j))
        return leaves

    def neighbor_max_level(self, level: int, i: int, j: int, direction: str) -> int:
        """Finest leaf level adjacent across one side (-1 if none).

        Looks for the neighbor first among coarser-or-equal ancestors, then
        descends into the finer children adjacent to the shared side.
        """
        di, dj = _SIDES[direction]
        ni, nj = i + di, j + dj
        if ni < 0 or nj < 0:
            return -1
        # Coarser ancestors (including the same level).
        ll = level
        while ll >= 0:
            if (ll, ni >> (level - ll), nj >> (level - ll)) in self.leaves:
                return ll
            ll -= 1
        # Descend into finer children adjacent to the shared side.
        best = -1
        frontier: list[CellKey] = [(level, ni, nj)]
        while frontier:
            cl, ci, cj = frontier.pop()
            if (cl, ci, cj) in self.leaves:
                best = max(best, cl)
                continue
            if cl >= self.max_level:
                continue
            if direction == "E":  # neighbor's west children face back at us
                kids = [(cl + 1, 2 * ci, 2 * cj), (cl + 1, 2 * ci, 2 * cj + 1)]
            elif direction == "W":
                kids = [(cl + 1, 2 * ci + 1, 2 * cj), (cl + 1, 2 * ci + 1, 2 * cj + 1)]
            elif direction == "N":
                kids = [(cl + 1, 2 * ci, 2 * cj), (cl + 1, 2 * ci + 1, 2 * cj)]
            else:  # "S"
                kids = [(cl + 1, 2 * ci, 2 * cj + 1), (cl + 1, 2 * ci + 1, 2 * cj + 1)]
            frontier.extend(kids)
        return best

    def _balance(self) -> None:
        """Enforce the <=1-level jump invariant between adjacent leaves.

        Any leaf with a neighbor two or more levels finer is split; splitting
        can create new violations on the *other* sides, so affected coarser
        neighbors re-enter the queue until a fixpoint is reached.  This
        invariant is what keeps every T-junction split at an exact side
        midpoint (octilinearity across refinement boundaries).
        """
        queue = list(self.leaves)
        while queue:
            cell = queue.pop()
            if cell not in self.leaves:
                continue  # already split by a prior iteration
            level, i, j = cell
            if not any(self.neighbor_max_level(level, i, j, d) >= level + 2 for d in _SIDES):
                continue
            self.leaves.discard(cell)
            for di in (0, 1):
                for dj in (0, 1):
                    kid = (level + 1, 2 * i + di, 2 * j + dj)
                    rect, _s = self.cell_rect(*kid)
                    if not self._cell_in_bounds(rect):
                        continue
                    self.leaves.add(kid)
                    queue.append(kid)
            # Splitting may push a coarser neighbor out of balance in turn.
            for d in _SIDES:
                di, dj = _SIDES[d]
                ll, ii, jj = level, i + di, j + dj
                while ll >= 0:
                    if (ll, ii, jj) in self.leaves:
                        queue.append((ll, ii, jj))
                        break
                    ll -= 1
                    ii >>= 1
                    jj >>= 1

    # -- graph --------------------------------------------------------------

    def node_key(self, x: float, y: float) -> NodeKey:
        """Integer node key for a world coordinate (multiple of :attr:`unit`)."""
        return (
            round((x - self.origin[0]) / self.unit),
            round((y - self.origin[1]) / self.unit),
        )

    def node_point(self, key: NodeKey) -> Pt:
        """Exact world coordinate of a node key."""
        return (
            self.origin[0] + key[0] * self.unit,
            self.origin[1] + key[1] * self.unit,
        )

    def _inside(self, x: float, y: float) -> bool:
        return (
            self.bbox[0] - 1e-9 <= x <= self.bbox[2] + 1e-9
            and self.bbox[1] - 1e-9 <= y <= self.bbox[3] + 1e-9
        )

    def _build_graph(
        self,
    ) -> tuple[dict[NodeKey, Pt], dict[NodeKey, list[tuple[NodeKey, float]]], set[EdgeKey]]:
        nodes: dict[NodeKey, Pt] = {}
        edges: set[EdgeKey] = set()

        def add_node(x: float, y: float) -> NodeKey | None:
            if not self._inside(x, y):
                return None  # board outline (bbox) clips the lattice
            k = self.node_key(x, y)
            nodes[k] = self.node_point(k)
            return k

        def add_edge(k1: NodeKey | None, k2: NodeKey | None) -> None:
            if k1 is None or k2 is None or k1 == k2:
                return
            edges.add((min(k1, k2), max(k1, k2)))

        for level, i, j in self.leaves:
            rect, _s = self.cell_rect(level, i, j)
            x0, y0, x1, y1 = rect
            c00 = add_node(x0, y0)
            c10 = add_node(x1, y0)
            c01 = add_node(x0, y1)
            c11 = add_node(x1, y1)
            center = add_node((x0 + x1) / 2, (y0 + y1) / 2)
            # The 4 corner->center half-diagonals (exact 45 degrees).
            for corner in (c00, c10, c01, c11):
                add_edge(corner, center)
            # Sides: split at the midpoint when the neighbor across is finer.
            # Balance guarantees the finer neighbor is exactly ONE level down,
            # so the midpoint coincides with that neighbor's shared corner and
            # both sub-edges stay exactly axis-aligned.
            sides: list[tuple[str, NodeKey | None, NodeKey | None, Pt]] = [
                ("S", c00, c10, ((x0 + x1) / 2, y0)),
                ("N", c01, c11, ((x0 + x1) / 2, y1)),
                ("W", c00, c01, (x0, (y0 + y1) / 2)),
                ("E", c10, c11, (x1, (y0 + y1) / 2)),
            ]
            for direction, ca, cb, midpoint in sides:
                if self.neighbor_max_level(level, i, j, direction) >= level + 1:
                    mid = add_node(*midpoint)
                    add_edge(ca, mid)
                    add_edge(mid, cb)
                else:
                    add_edge(ca, cb)

        adj: dict[NodeKey, list[tuple[NodeKey, float]]] = defaultdict(list)
        for k1, k2 in edges:
            length = dist(self.node_point(k1), self.node_point(k2))
            adj[k1].append((k2, length))
            adj[k2].append((k1, length))
        return nodes, dict(adj), edges

    # -- sizing --------------------------------------------------------------

    def memory_estimate(self, n_layers: int) -> tuple[int, int, int]:
        """``(n_nodes, n_edges, bytes)`` for ``n_layers`` lattice replicas.

        Accounting mirrors the #4267 sizing spikes so numbers are comparable
        across the epic: 16 B coordinates + 8 B mask word per node per layer,
        plus 2 x (8 B index + 8 B length) per undirected edge per layer.
        """
        n = len(self.nodes)
        m = len(self.edges)
        return n, m, n_layers * (n * (16 + 8) + m * 32)
