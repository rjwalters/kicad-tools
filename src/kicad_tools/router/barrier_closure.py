"""Barrier-closure diagnostic: predict severed nets when a routing loop closes.

Report-only (Issue #9000).  Unrouteability is normally discovered late -- a
later net's search fails, rip-up churns, diagnosis is forensic.  The cheapest
moment to know is when the *closing* copper is committed, while a different
choice is still cheap.  This module keeps a union-find over coarse board
regions and, on each committed route, says whether that route closed a loop of
barrier copper and which still-unrouted nets the loop now cuts in two.

Model (an approximation, deliberately coarse)
---------------------------------------------

* The board is tiled by the same coarse grid
  :class:`~kicad_tools.router.region_graph.RegionGraph` uses; a node is one
  ``(layer, region)`` tile.
* Copper on a layer is a wall: traces of different nets cannot cross on one
  layer.  A route welds the tiles of its *run endpoints* (the two ends of each
  same-layer chain of segments -- pad and via positions), not the tiles it
  merely bends through.  A tile stands in for a pad island, so two walls that
  end in the same tile are treated as joined there.
* When a weld joins two tiles that are already joined on that layer, a closed
  loop of walls exists.  The loop polygon is the copper path that already
  joined the two tiles plus the new copper.
* A loop on one layer does not stop a connection that can cross on another
  layer.  A still-unrouted connection is reported as **severed** only when its
  pad components lie on opposite sides of a closed loop on *every* signal
  layer; those blocked on the closing layer alone are reported separately as
  ``layer_blocked``.  Per-layer structure is intentional: welding across vias
  would call a top-layer loop closed when the bottom layer is wide open.
* Nets whose own copper is part of the loop are not counted (their copper
  merges with the wall rather than being stopped by it).

Pure bookkeeping.  Nothing here reads or writes the routing grid, the
pathfinder or any cost; the driver hook is gated on ``KCT_BARRIER_CLOSURE``
and swallows every exception, so routing results are byte-identical with the
diagnostic on or off.

Rip-up: the structure is rebuilt from the committed routes whenever a route it
welded is no longer committed, so no weld outlives its copper.

Not implemented: capacity-saturated channel welds.  They need committed (not
planned) per-edge utilisation, which neither the negotiated driver nor
``RegionGraph`` tracks at commit time.

Ideas-only provenance: implemented from the behaviour description in Issue
#9000, never from upstream WeaveEngine code (GPL-3.0).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Route

logger = logging.getLogger(__name__)

#: Environment switch read by the negotiated driver.  Off unless truthy.
ENV_FLAG = "KCT_BARRIER_CLOSURE"

#: Edge length (mm) of a coarse region tile when the driver builds the tiling.
#: The driver clamps the tiling to 60x60 tiles (core.py), so a board longer than
#: 60 * DEFAULT_TILE_MM (120 mm) on an axis silently gets coarser tiles.
DEFAULT_TILE_MM = 2.0

#: Coordinate rounding (mm) used to chain segments end-to-end.
_POINT_PLACES = 3

Point = tuple[float, float]


@dataclass(frozen=True)
class ClosureEvent:
    """One loop closure detected by a commit."""

    seq: int
    """1-based index of the commit seen by this tracker."""
    stage: str
    """Free-form label of the routing stage, e.g. ``"iteration[3]"``."""
    net: int
    """Net of the committed route that closed the loop."""
    layer: int
    """Grid layer index on which the loop closed."""
    severed_nets: tuple[int, ...]
    """Unrouted nets cut in two on every signal layer."""
    severed_connections: int
    """Minimum connections that must cross the loop on every layer."""
    layer_blocked_nets: tuple[int, ...]
    """Unrouted nets cut in two on the closing layer alone (superset)."""
    layer_blocked_connections: int


@dataclass
class _Ring:
    polygon: list[Point]
    nets: frozenset[int]


@dataclass
class _LayerState:
    parent: dict[int, int] = field(default_factory=dict)
    adj: dict[int, list[tuple[int, list[Point], int]]] = field(default_factory=dict)
    rings: list[_Ring] = field(default_factory=list)

    def find(self, a: int) -> int:
        parent = self.parent
        parent.setdefault(a, a)
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    def path(self, a: int, b: int) -> list[tuple[list[Point], int]] | None:
        """Copper polylines (oriented a -> b) and nets of one weld path a..b."""
        prev: dict[int, tuple[int, list[Point], int]] = {}
        seen = {a}
        queue: deque[int] = deque([a])
        while queue:
            node = queue.popleft()
            if node == b:
                break
            for nbr, pts, net in self.adj.get(node, ()):
                if nbr not in seen:
                    seen.add(nbr)
                    prev[nbr] = (node, pts, net)
                    queue.append(nbr)
        if b not in seen:
            return None
        out: list[tuple[list[Point], int]] = []
        node = b
        while node != a:
            parent_node, pts, net = prev[node]
            out.append((pts, net))
            node = parent_node
        out.reverse()
        return out


def point_in_polygon(pt: Point, polygon: Sequence[Point]) -> bool:
    """Even-odd ray cast.  Degenerate (< 3 point) polygons contain nothing."""
    if len(polygon) < 3:
        return False
    x, y = pt
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _key(x: float, y: float) -> Point:
    return (round(x, _POINT_PLACES), round(y, _POINT_PLACES))


class BarrierClosureTracker:
    """Incremental closed-loop detector over coarse per-layer regions.

    Args:
        region_of: ``(x, y) -> region id`` (``None`` off the board).  Normally
            ``lambda x, y: region_graph.get_region_at(x, y).id``.
        layer_of: ``Segment.layer`` / ``Via.layers`` member -> grid layer index.
        signal_layers: Grid indices that can carry signal.
        net_pads: ``net -> pad centre positions`` of every net that may still
            be unrouted.
        stage: Returns the current stage label for event records.
    """

    def __init__(
        self,
        region_of: Callable[[float, float], int | None],
        layer_of: Callable[[object], int | None],
        signal_layers: Iterable[int],
        net_pads: Mapping[int, Sequence[Point]],
        stage: Callable[[], str] | None = None,
        pad_tolerance: float = 0.5,
    ) -> None:
        self._region_of = region_of
        self._layer_of = layer_of
        self._signal = sorted(set(signal_layers))
        self._net_pads = net_pads
        self._stage = stage or (lambda: "")
        self._pad_tol = pad_tolerance
        self._layers: dict[int, _LayerState] = {}
        self._known: dict[int, Route] = {}
        self._committed: Sequence[Route] = ()
        self._seq = 0
        self.events: list[ClosureEvent] = []
        self.rebuilds = 0
        #: Opaque handle the driver uses to notice its grid was replaced.
        self.owner: object | None = None

    # -- public API ------------------------------------------------------

    @property
    def ring_count(self) -> int:
        """Closed loops currently held, summed over layers."""
        return sum(len(st.rings) for st in self._layers.values())

    def on_commit(
        self, route: Route, committed: Sequence[Route] | None = None
    ) -> ClosureEvent | None:
        """Account for a freshly committed ``route``.

        Args:
            route: The copper just committed.
            committed: Every route currently committed, *including* ``route``.
                When a route this tracker welded is missing from it (rip-up),
                the structure is rebuilt from ``committed`` first.

        Returns:
            The :class:`ClosureEvent` when this commit closed a loop that cuts
            at least one unrouted net on the closing layer, else ``None``.
        """
        if committed is not None:
            self._committed = committed
            present = {id(r) for r in committed}
            if any(rid not in present for rid in self._known):
                self.rebuild([r for r in committed if r is not route])
        self._seq += 1
        return self._weld_route(route, report=True)

    def rebuild(self, committed: Iterable[Route]) -> None:
        """Drop every weld and re-derive them from ``committed`` (no reports)."""
        self.rebuilds += 1
        self._layers = {}
        self._known = {}
        for r in committed:
            self._weld_route(r, report=False)

    # -- welding ---------------------------------------------------------

    def _state(self, layer: int) -> _LayerState:
        st = self._layers.get(layer)
        if st is None:
            st = self._layers[layer] = _LayerState()
        return st

    def _weld_route(self, route: Route, report: bool) -> ClosureEvent | None:
        if id(route) in self._known:
            return None
        self._known[id(route)] = route
        event: ClosureEvent | None = None
        for layer, a, b, pts in self._runs(route):
            ev = self._weld(layer, a, b, pts, route.net, report)
            if ev is not None and (event is None or ev.layer_blocked_connections > 0):
                event = ev
        return event

    def _runs(self, route: Route) -> list[tuple[int, int, int, list[Point]]]:
        """``(layer, region_a, region_b, polyline)`` for each same-layer chain."""
        by_layer: dict[int, list[tuple[Point, Point]]] = {}
        for seg in route.segments:
            layer = self._layer_of(seg.layer)
            if layer is None:
                continue
            by_layer.setdefault(layer, []).append((_key(seg.x1, seg.y1), _key(seg.x2, seg.y2)))
        runs: list[tuple[int, int, int, list[Point]]] = []
        for layer, segs in by_layer.items():
            for chain in _chains(segs):
                a = self._region_of(*chain[0])
                b = self._region_of(*chain[-1])
                if a is None or b is None:
                    continue
                runs.append((layer, a, b, chain))
        return runs

    def _weld(
        self, layer: int, a: int, b: int, pts: list[Point], net: int, report: bool
    ) -> ClosureEvent | None:
        if a == b:
            return None
        st = self._state(layer)
        event: ClosureEvent | None = None
        if st.find(a) == st.find(b):
            existing = st.path(a, b)
            if existing is not None:
                polygon: list[Point] = []
                nets = {net}
                for seg_pts, seg_net in existing:
                    polygon.extend(seg_pts)
                    nets.add(seg_net)
                polygon.extend(reversed(pts))
                st.rings.append(_Ring(polygon, frozenset(nets)))
                if report:
                    event = self._evaluate(layer, st.rings[-1], net)
        else:
            st.parent[st.find(a)] = st.find(b)
        st.adj.setdefault(a, []).append((b, pts, net))
        st.adj.setdefault(b, []).append((a, list(reversed(pts)), net))
        return event

    # -- evaluation ------------------------------------------------------

    def _evaluate(self, layer: int, ring: _Ring, net: int) -> ClosureEvent | None:
        comps_by_net = self._unrouted_components(ring.nets)
        if not comps_by_net:
            return None
        signal = self._signal or [layer]
        blocked: dict[int, int] = {}
        severed: dict[int, int] = {}
        for cand_net, comps in comps_by_net.items():
            here = self._groups(comps, self._layers[layer].rings, cand_net)
            if here <= 1:
                continue
            blocked[cand_net] = here - 1
            everywhere = self._groups_all_layers(comps, signal, cand_net)
            if everywhere > 1:
                severed[cand_net] = everywhere - 1
        if not blocked:
            return None
        ev = ClosureEvent(
            seq=self._seq,
            stage=self._stage(),
            net=net,
            layer=layer,
            severed_nets=tuple(sorted(severed)),
            severed_connections=sum(severed.values()),
            layer_blocked_nets=tuple(sorted(blocked)),
            layer_blocked_connections=sum(blocked.values()),
        )
        self.events.append(ev)
        log = logger.warning if severed else logger.info
        log(
            "barrier closure @%s commit #%d layer %d: %d unrouted connection(s) "
            "severed on every layer (nets %s); %d blocked on this layer (nets %s)",
            ev.stage,
            ev.seq,
            layer,
            ev.severed_connections,
            list(ev.severed_nets),
            ev.layer_blocked_connections,
            list(ev.layer_blocked_nets),
        )
        return ev

    def _layer_rings(self, layer: int) -> list[_Ring]:
        st = self._layers.get(layer)
        return st.rings if st is not None else []

    def _separated(
        self, comps: list[list[Point]], rings: Sequence[_Ring], net: int
    ) -> set[tuple[int, int]]:
        pairs: set[tuple[int, int]] = set()
        for ring in rings:
            if net in ring.nets:
                continue
            sides = [point_in_polygon(c[0], ring.polygon) for c in comps]
            for i in range(len(comps)):
                for j in range(i + 1, len(comps)):
                    if sides[i] != sides[j]:
                        pairs.add((i, j))
        return pairs

    @staticmethod
    def _count_groups(n: int, separated: set[tuple[int, int]]) -> int:
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                if (i, j) not in separated:
                    parent[find(i)] = find(j)
        return len({find(i) for i in range(n)})

    def _groups(self, comps: list[list[Point]], rings: Sequence[_Ring], net: int) -> int:
        return self._count_groups(len(comps), self._separated(comps, rings, net))

    def _groups_all_layers(self, comps: list[list[Point]], signal: Sequence[int], net: int) -> int:
        common: set[tuple[int, int]] | None = None
        for sl in signal:
            pairs = self._separated(comps, self._layer_rings(sl), net)
            common = pairs if common is None else common & pairs
        return self._count_groups(len(comps), common or set())

    def _unrouted_components(self, exclude: frozenset[int]) -> dict[int, list[list[Point]]]:
        """Pad components per net with >= 2 unconnected components."""
        by_net: dict[int, list[Route]] = {}
        for r in self._committed:
            by_net.setdefault(r.net, []).append(r)
        out: dict[int, list[list[Point]]] = {}
        for net, pads in self._net_pads.items():
            if net in exclude or len(pads) < 2:
                continue
            comps = _pad_components(pads, by_net.get(net, ()), self._pad_tol)
            if len(comps) > 1:
                out[net] = comps
        return out


def _chains(segs: list[tuple[Point, Point]]) -> list[list[Point]]:
    """Order segments into end-to-end point chains (one per connected piece).

    Leaves (degree 1) start a chain; the walk stops at the first branch node,
    so a branched piece yields one chain per leaf.  A closed piece has no leaf
    and yields no chain at all -- per-connection routes are paths, so this is
    the rare case.
    """
    adj: dict[Point, list[Point]] = {}
    for p, q in segs:
        if p == q:
            continue
        adj.setdefault(p, []).append(q)
        adj.setdefault(q, []).append(p)
    seen_edges: set[frozenset[Point]] = set()
    chains: list[list[Point]] = []
    for start in sorted(p for p, n in adj.items() if len(n) == 1):
        if any(frozenset((start, n)) in seen_edges for n in adj[start]):
            continue
        chain = [start]
        cur = start
        prev: Point | None = None
        while True:
            nxt = [n for n in adj[cur] if n != prev and frozenset((cur, n)) not in seen_edges]
            if not nxt:
                break
            n = nxt[0]
            seen_edges.add(frozenset((cur, n)))
            chain.append(n)
            prev, cur = cur, n
            if len(adj[cur]) != 2:
                break
        if len(chain) > 1:
            chains.append(chain)
    return chains


def _pad_components(
    pads: Sequence[Point], routes: Iterable[Route], tol: float
) -> list[list[Point]]:
    """Group pads by the copper already connecting them."""
    n = len(pads)
    parent: dict[object, object] = {}

    def find(a: object) -> object:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: object, b: object) -> None:
        parent[find(a)] = find(b)

    points: set[Point] = set()
    for r in routes:
        for s in r.segments:
            p, q = _key(s.x1, s.y1), _key(s.x2, s.y2)
            union(("pt", p), ("pt", q))
            points.update((p, q))
        for v in r.vias:
            points.add(_key(v.x, v.y))
    for i, (px, py) in enumerate(pads):
        find(("pad", i))
        for p in points:
            if abs(p[0] - px) <= tol and abs(p[1] - py) <= tol:
                union(("pad", i), ("pt", p))
    groups: dict[object, list[Point]] = {}
    for i in range(n):
        groups.setdefault(find(("pad", i)), []).append(pads[i])
    return list(groups.values())
