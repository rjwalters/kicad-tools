"""``MeshPathfinder`` -- the mesh-router route contract (#4268 P1 / #4269 P2).

Exposes the same "route between two pads" contract as
``CppPathfinder.route`` (``cpp_backend.py:1322``) and returns an ordinary
:class:`~kicad_tools.router.primitives.Route`, so the emission tail and DRC
gate see a normal route object.  The full pipeline:

    static navmesh (built ONCE per board)  (router_cpp.constrained_delaunay)
      -> triangle-dual portal-midpoint A*    (NavMesh.astar, congestion-aware)
      -> Simple Stupid Funnel                (string_pull)
      -> clearance-aware 45-degree best-fit  (octilinear_fit)
      -> Route of 45-legal Segments

**P2 (#4269) -- multi-net capacity + negotiation.**  The load-bearing risk (e)
constraint is *static mesh + dynamic portal cost*: the poly2tri CDT and the
:class:`NavMesh` are built **once per board** (:meth:`build`, cached on
``self``) and reused across every net and every negotiation iteration.
Committed copper only raises **portal cost / occupancy** -- it never
re-triangulates.  :attr:`triangulation_calls` counts the CDT invocations so an
acceptance test can assert it stays at 1.  Multi-net negotiation
(:meth:`route_netset`) is a PathFinder/VPR-style present+history rip-up loop
over the shared portals, mirroring the grid negotiator's per-cell congestion
per **portal** instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ..primitives import Pad, Route, Segment
from ..rules import DesignRules
from .geometry import Pt, merge_intervals, segment_polygon_interval
from .navmesh import NavMesh
from .obstacles import ObstacleModel, Rect
from .octilinear import octilinear_fit


@dataclass(frozen=True)
class MeshNegotiationStats:
    """Outcome of a :meth:`MeshPathfinder.route_netset` negotiation run."""

    iterations: int
    converged: bool
    routed: int
    total: int
    triangulation_calls: int

    @property
    def completion(self) -> float:
        return self.routed / self.total if self.total else 0.0


# A negotiation connection: (opaque hashable key, start pad, end pad, net_class).
Connection = tuple[object, Pad, Pad, object]


class MeshPathfinder:
    """Navmesh pathfinder returning ordinary ``Route`` objects.

    The navmesh is built once per board and reused; per-net routing consults a
    cheap per-net obstacle model (rectangular pad keep-outs + pour polygons) --
    no re-triangulation.
    """

    # Capability flag mirroring ``CppPathfinder.supports_waypoint_injection``
    # (cpp_backend.py:819).  The mesh strategy does its own off-grid handling
    # (pads enter the mesh as exact Steiner vertices), so the grid waypoint /
    # sub-grid escape machinery does not apply.
    supports_waypoint_injection: bool = False

    def __init__(
        self,
        outline: list[Pt],
        pads: list[Pad],
        rules: DesignRules | None = None,
        pours: list[list[Pt]] | None = None,
    ) -> None:
        self.outline = outline
        self.pads = pads
        self.rules = rules or DesignRules()
        # Filled-copper pour outlines (issue #4269).  Modeled BOTH as poly2tri
        # mesh holes (so corridors route around them) and as obstacle-model
        # polygons (so the 45-fit declines any leg entering a pour).
        self.pours: list[list[Pt]] = [list(p) for p in (pours or [])]
        # Slightly-inset outline bbox used to clamp keep-out holes so poly2tri
        # never sees a hole that pokes through the outer boundary.
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        self._bbox: Rect = (min(xs), min(ys), max(xs), max(ys))
        # Static navmesh + triangulation-call counter (built lazily, once).
        self._navmesh: NavMesh | None = None
        self.triangulation_calls: int = 0

    # -- construction from a board ----------------------------------------

    @classmethod
    def from_board(
        cls,
        pcb_path_or_text: str | Path,
        rules: DesignRules | None = None,
        pours: list[list[Pt]] | None = None,
    ) -> MeshPathfinder:
        """Build a pathfinder from a ``.kicad_pcb`` file or text."""
        from ..io import _extract_edge_segments, load_pads_for_analysis

        if isinstance(pcb_path_or_text, Path):
            text = pcb_path_or_text.read_text()
        elif not pcb_path_or_text.lstrip().startswith("("):
            text = Path(pcb_path_or_text).read_text()
        else:
            text = pcb_path_or_text

        pads = load_pads_for_analysis(text)
        segments = _extract_edge_segments(text)
        outline = _outline_from_edges(segments)
        return cls(outline, pads, rules, pours)

    # -- static mesh ------------------------------------------------------

    @property
    def channel(self) -> float:
        """Lane pitch used for portal capacity: trace width + 2*clearance."""
        return self.rules.trace_width + 2.0 * self.rules.trace_clearance

    def build(self) -> NavMesh:
        """Build (once) and return the static per-board navmesh.

        Every pad centre enters as a Steiner vertex (so any pad is a locatable
        routing endpoint) and every in-bounds pour polygon enters as a hole (so
        corridors route around filled copper).  This is the resolution of risk
        (e): the triangulation happens **once** -- subsequent negotiation passes
        mutate only portal occupancy/cost on the returned :class:`NavMesh`.
        """
        if self._navmesh is not None:
            return self._navmesh

        import kicad_tools.router.router_cpp as router_cpp  # type: ignore[import-not-found]

        steiner = [(p.x, p.y) for p in self.pads]
        holes = self._pour_holes()
        self.triangulation_calls += 1
        verts, tris = router_cpp.constrained_delaunay(self.outline, holes, steiner)
        if not verts or not tris:
            self._navmesh = NavMesh([], [], channel=self.channel)
        else:
            vertices: list[Pt] = [(float(v[0]), float(v[1])) for v in verts]
            triangles = [(int(t[0]), int(t[1]), int(t[2])) for t in tris]
            self._navmesh = NavMesh(vertices, triangles, channel=self.channel)
        return self._navmesh

    def _pour_holes(self) -> list[list[Pt]]:
        """Inflated pour polygons that lie inside the outline (poly2tri holes).

        Pours are inflated by the agent radius (half-trace + clearance), exactly
        as pad keep-outs are (``_keepouts``): the mesh hole is grown so the
        navmesh corridor is pushed a full clearance off the *true* pour boundary
        and the funnel geodesic never hugs the copper edge.  The obstacle model
        still checks legs against the true (un-inflated) pour, so the emitted
        copper keeps at least the clearance margin from the pour.
        """
        bx0, by0, bx1, by1 = self._bbox
        margin = 1e-3
        agent_radius = self.rules.trace_width / 2.0 + self.rules.trace_clearance
        holes: list[list[Pt]] = []
        for poly in self.pours:
            if len(poly) < 3:
                continue
            inflated = _inflate_polygon([(pt[0], pt[1]) for pt in poly], agent_radius)
            # Only keep pours fully inside the (slightly inset) outline: poly2tri
            # holes must not touch or cross the outer boundary.
            if all(
                bx0 + margin <= x <= bx1 - margin and by0 + margin <= y <= by1 - margin
                for (x, y) in inflated
            ):
                holes.append(inflated)
        return holes

    # -- the route contract -----------------------------------------------

    def route(
        self,
        start: Pad,
        end: Pad,
        net_class: object | None = None,
        *,
        negotiated_mode: bool = False,
        present_cost_factor: float = 0.0,
        **_ignored: object,
    ) -> Route | None:
        """Route a single net between two pads; ``None`` if it cannot.

        ``negotiated_mode`` / ``present_cost_factor`` mirror
        ``CppPathfinder.route`` (``cpp_backend.py:1327-1328``): when enabled the
        A* consults per-portal congestion so committed copper (higher portal
        occupancy / history) steers this net onto a different corridor.  The
        defaults reproduce the P1 single-net behaviour exactly.
        """
        result = self._route_with_portals(
            start,
            end,
            net_class,
            negotiated_mode=negotiated_mode,
            present_cost_factor=present_cost_factor,
        )
        return result[0] if result is not None else None

    def _route_with_portals(
        self,
        start: Pad,
        end: Pad,
        net_class: object | None,
        *,
        negotiated_mode: bool,
        present_cost_factor: float,
        committed: list[list[Pt]] | None = None,
        consumed: dict[tuple[int, int], list[tuple[float, float]]] | None = None,
    ) -> tuple[Route, list[tuple[int, int]]] | None:
        """Route + report the portal keys the route crosses (for occupancy).

        ``committed`` carries inflated polygons of copper already laid down by
        OTHER nets in this negotiation pass; the octilinear fit treats them as
        obstacles so a net that would cross committed copper is declined rather
        than shipped as a short (the #3906 "never ship a short" invariant, now
        enforced net-vs-net).  The static mesh is untouched -- committed copper
        only narrows the per-net obstacle model, never re-triangulates.

        ``consumed`` (issue #4274) maps each portal edge to the parametric
        intervals already occupied by committed copper; it narrows this net's
        portal openings to their residual sub-segments so the funnel yields a
        *parallel* geodesic (an actual lane) instead of the same taut path a
        prior net already claimed.  When ``None`` it is derived from
        ``committed`` for this net's own corridor (each portal edge intersected
        with every committed capsule); an empty/absent model reproduces the P2
        full-opening funnel exactly.
        """
        navmesh = self.build()
        if not navmesh.triangles:
            return None

        trace_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        clearance = self.rules.trace_clearance
        agent_radius = trace_w / 2.0 + clearance

        net = start.net
        start_pt: Pt = (start.x, start.y)
        end_pt: Pt = (end.x, end.y)

        # Authoritative per-net obstacle model: EVERY other-net pad inflated,
        # plus every pour polygon and every committed cross-net trace.  Cheap to
        # rebuild (rect + polygon lists, no triangulation).  This is the model
        # the octilinear fit validates each leg against -- a route that cannot
        # clear it is declined (None).
        keepouts = self._keepouts(net, agent_radius)
        pour_obstacles = self.pours + (committed or [])
        obstacles = ObstacleModel(self.outline, keepouts, pour_obstacles)

        cost_congestion = self.rules.cost_congestion if negotiated_mode else 0.0
        congestion_threshold = self.rules.congestion_threshold
        pcf = present_cost_factor if negotiated_mode else 0.0

        corridor = navmesh.astar(
            start_pt,
            end_pt,
            present_cost_factor=pcf,
            cost_congestion=cost_congestion,
            congestion_threshold=congestion_threshold,
        )
        if corridor is None:
            return None

        # First try the full-opening funnel (the P2 geodesic).  When no copper
        # is committed yet -- the first net, and every net on ``--route-engine``
        # paths that never narrow -- this is the only attempt and the result is
        # byte-identical to P2.
        fitted = self._fit_corridor(navmesh, corridor, start_pt, end_pt, obstacles, consumed)

        # Issue #4274 re-funnel: if the taut geodesic collides with copper a
        # prior net committed this pass, narrow this net's portals to their
        # residual openings and re-run the funnel.  Narrowing only *adds* a lane
        # candidate -- the straight geodesic was already tried -- so completion
        # can only rise, never regress, and a lane that still cannot clear
        # declines (``None``) rather than crossing.
        if fitted is None and consumed is None and committed:
            derived: dict[tuple[int, int], list[tuple[float, float]]] = {}
            for edge in navmesh.corridor_portals(corridor):
                bands = _edge_consumed_bands(navmesh, edge, committed)
                if bands:
                    derived[edge] = bands
            if derived:
                # Re-funnel with a small family of parallel-lane candidates
                # (widest gap, then each one-wall packing); accept the first
                # that clears.  Every candidate is validated against the true
                # obstacle model, so a lane that cannot clear still declines.
                for pack in ("largest", "left", "right"):
                    fitted = self._fit_corridor(
                        navmesh, corridor, start_pt, end_pt, obstacles, derived, pack
                    )
                    if fitted is not None:
                        break

        if fitted is None or len(fitted) < 2:
            return None

        route = self._build_route(start, net, trace_w, fitted)
        return route, navmesh.corridor_portals(corridor)

    def _fit_corridor(
        self,
        navmesh: NavMesh,
        corridor: list[int],
        start_pt: Pt,
        end_pt: Pt,
        obstacles: ObstacleModel,
        consumed: dict[tuple[int, int], list[tuple[float, float]]] | None,
        pack: str = "largest",
    ) -> list[Pt] | None:
        """Funnel + clearance-clean 45-fit for one corridor (optionally narrowed)."""
        geodesic = _pull(navmesh, corridor, start_pt, end_pt, consumed, pack)
        fitted = octilinear_fit(geodesic, obstacles.is_clear)
        if fitted is None or len(fitted) < 2:
            return None
        return fitted

    # -- multi-net negotiation (issue #4269) ------------------------------

    def route_netset(
        self,
        connections: list[Connection],
        *,
        max_iterations: int = 8,
        present_cost_initial: float = 0.5,
        present_cost_growth: float = 1.6,
        history_increment_factor: float = 1.0,
    ) -> tuple[dict[object, Route], MeshNegotiationStats]:
        """PathFinder/VPR negotiation over the shared portals.

        Nets compete for corridor capacity instead of committing first-come:
        each pass routes every connection (shortest-first) against the current
        portal occupancy/history AND the copper already committed this pass, so
        a net that would short a committed trace is declined rather than
        crossed.  Between passes two congestion signals ratchet up history:
        over-capacity portals (PathFinder present+history), and the *desired*
        corridor portals of nets that FAILED -- the latter pressures the nets
        currently occupying a contested corridor to detour next pass and free
        it for the blocked net.  The loop keeps the best (most-routed) pass and
        stops early when every net routes or the routed set stops improving.
        The mesh is built **once** (see :meth:`build`); only portal cost changes
        between passes -- never the triangulation (risk (e)).

        Returns ``(routes_by_key, stats)`` for the best iteration seen.
        """
        navmesh = self.build()
        # Fresh negotiation: clear any stale occupancy/history from a prior run.
        navmesh.reset_occupancy()
        navmesh._history.clear()

        # Shortest-first: short nets are easier and fence off less area, which
        # materially lifts the completion rate under committed-copper blocking.
        ordered = sorted(
            connections,
            key=lambda c: math.hypot(c[1].x - c[2].x, c[1].y - c[2].y),
        )

        best_routes: dict[object, Route] = {}
        best_count = -1
        converged = False
        iterations_run = 0
        present = present_cost_initial

        for it in range(max_iterations):
            iterations_run = it + 1
            navmesh.reset_occupancy()
            routes: dict[object, Route] = {}
            failed: list[Connection] = []
            # Inflated copper committed by earlier nets THIS pass -- later nets
            # must clear it (net-vs-net short avoidance).  Reset each pass so a
            # rip-up genuinely frees the corridor.
            committed: list[list[Pt]] = []
            for key, start, end, net_class in ordered:
                # Issue #4274: ``committed`` doubles as the lane-assignment
                # model -- ``_route_with_portals`` narrows this net's portals by
                # the copper already committed this pass so its funnel produces a
                # parallel lane rather than the geodesic a prior net occupies.
                # Reset each pass so a rip-up frees the lane as well as the
                # copper.
                result = self._route_with_portals(
                    start,
                    end,
                    net_class,
                    negotiated_mode=True,
                    present_cost_factor=present,
                    committed=committed,
                )
                if result is None:
                    failed.append((key, start, end, net_class))
                    continue
                route, portals = result
                routes[key] = route
                for edge in portals:
                    navmesh.commit_portal(edge)
                committed.extend(self._route_obstacles(route))

            if len(routes) > best_count:
                best_count = len(routes)
                best_routes = routes

            if not failed:
                converged = True
                break
            if it == max_iterations - 1:
                break

            # Signal 1: over-capacity portals (classic PathFinder present+history).
            for edge in navmesh.occupied_portals():
                over = navmesh.occupancy(edge) - navmesh.capacity(edge)
                if over > 0:
                    navmesh.add_history(
                        edge,
                        history_increment_factor * self.rules.cost_congestion * over,
                    )
            # Signal 2: failed-net demand.  Bump history on the portals each
            # blocked net *wants* (its congestion-free corridor) so whoever is
            # sitting in that corridor is nudged elsewhere next pass.
            for _key, start, end, _nc in failed:
                corridor = navmesh.astar((start.x, start.y), (end.x, end.y))
                if corridor is None:
                    continue
                for edge in navmesh.corridor_portals(corridor):
                    navmesh.add_history(edge, history_increment_factor * self.rules.cost_congestion)
            present *= present_cost_growth

        stats = MeshNegotiationStats(
            iterations=iterations_run,
            converged=converged,
            routed=len(best_routes),
            total=len(connections),
            triangulation_calls=self.triangulation_calls,
        )
        return best_routes, stats

    # -- helpers ----------------------------------------------------------

    def _keepouts(self, net: int, agent_radius: float) -> list[Rect]:
        """Inflated keep-out rects for every OTHER-net pad, clamped + merged."""
        from .obstacles import merge_overlapping

        raw: list[Rect] = []
        bx0, by0, bx1, by1 = self._bbox
        # Keep holes a hair inside the outer boundary (poly2tri requirement).
        margin = 1e-3
        for pad in self.pads:
            if pad.net == net:
                continue  # same-net copper is not an obstacle
            hx = pad.width / 2.0 + agent_radius
            hy = pad.height / 2.0 + agent_radius
            r = (pad.x - hx, pad.y - hy, pad.x + hx, pad.y + hy)
            r = (
                max(r[0], bx0 + margin),
                max(r[1], by0 + margin),
                min(r[2], bx1 - margin),
                min(r[3], by1 - margin),
            )
            if r[2] - r[0] < margin or r[3] - r[1] < margin:
                continue  # clamped away to nothing
            raw.append(r)

        return merge_overlapping(raw)

    def _route_obstacles(self, route: Route) -> list[list[Pt]]:
        """Inflated capsule polygons for each segment of a committed route.

        Each segment is grown by ``trace_width + clearance`` (covers both
        traces' half-widths plus the clearance gap on a single-width board) so
        another net's centreline staying outside keeps full copper clearance.
        """
        half = self.rules.trace_width + self.rules.trace_clearance
        polys: list[list[Pt]] = []
        for seg in route.segments:
            poly = _segment_capsule((seg.x1, seg.y1), (seg.x2, seg.y2), half)
            if poly is not None:
                polys.append(poly)
        return polys

    def _build_route(self, start: Pad, net: int, trace_w: float, fitted: list[Pt]) -> Route:
        route = Route(net=net, net_name=start.net_name)
        layer = start.layer
        for i in range(len(fitted) - 1):
            a, b = fitted[i], fitted[i + 1]
            route.segments.append(
                Segment(
                    x1=a[0],
                    y1=a[1],
                    x2=b[0],
                    y2=b[1],
                    width=trace_w,
                    layer=layer,
                    net=net,
                    net_name=start.net_name,
                )
            )
        return route


def _segment_capsule(a: Pt, b: Pt, half_width: float) -> list[Pt] | None:
    """Rectangle (rounded-cap approximation) around segment ``a-b``.

    The rectangle is the segment inflated by ``half_width`` sideways and its
    ends extended by ``half_width`` (a cheap capsule/Minkowski approximation),
    returned CCW as a 4-point polygon.  ``None`` for a degenerate segment.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return None
    ux, uy = dx / length, dy / length  # along
    nx, ny = -uy, ux  # left normal
    ax = a[0] - ux * half_width
    ay = a[1] - uy * half_width
    bx = b[0] + ux * half_width
    by = b[1] + uy * half_width
    return [
        (ax + nx * half_width, ay + ny * half_width),
        (bx + nx * half_width, by + ny * half_width),
        (bx - nx * half_width, by - ny * half_width),
        (ax - nx * half_width, ay - ny * half_width),
    ]


def _inflate_polygon(poly: list[Pt], margin: float) -> list[Pt]:
    """Grow a polygon outward by ``margin`` by pushing each vertex off the centroid.

    A cheap, conservative disc-inflation good enough for the rectangular /
    convex pour outlines in scope: each vertex moves radially outward from the
    polygon centroid by ``margin`` (corners -- the clearance-critical points a
    corridor rounds -- get the full margin).  Degenerate (centroid-coincident)
    vertices are left in place.
    """
    if margin <= 0.0 or len(poly) < 3:
        return [(p[0], p[1]) for p in poly]
    cx = sum(p[0] for p in poly) / len(poly)
    cy = sum(p[1] for p in poly) / len(poly)
    out: list[Pt] = []
    for x, y in poly:
        dx, dy = x - cx, y - cy
        d = math.hypot(dx, dy)
        if d < 1e-12:
            out.append((x, y))
        else:
            scale = (d + margin) / d
            out.append((cx + dx * scale, cy + dy * scale))
    return out


def _edge_consumed_bands(
    navmesh: NavMesh, edge: tuple[int, int], capsules: list[list[Pt]]
) -> list[tuple[float, float]]:
    """Parametric ``[t0, t1]`` intervals a route's copper carves out of a portal.

    Intersects each committed capsule (inflated by ``trace_width + clearance``,
    the same width the octilinear fit clears against) with the portal edge
    ``vertices[edge[0]] -> vertices[edge[1]]`` and unions the covered spans.
    These are the openings :func:`NavMesh.corridor_to_portals` subtracts so the
    next net funnels through the residual sub-segment (issue #4274).
    """
    a = navmesh.vertices[edge[0]]
    b = navmesh.vertices[edge[1]]
    ivals: list[tuple[float, float]] = []
    for poly in capsules:
        iv = segment_polygon_interval(a, b, poly)
        if iv is not None:
            ivals.append(iv)
    return merge_intervals(ivals)


def _pull(
    navmesh: NavMesh,
    corridor: list[int],
    start: Pt,
    end: Pt,
    consumed: dict[tuple[int, int], list[tuple[float, float]]] | None = None,
    pack: str = "largest",
) -> list[Pt]:
    from .funnel import string_pull

    return string_pull(navmesh.corridor_to_portals(corridor, start, end, consumed, pack))


def _outline_from_edges(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[Pt]:
    """Board outline as a CCW bbox rectangle from Edge.Cuts segments.

    P1 uses the Edge.Cuts bounding box as the outer boundary.  Every committed
    fleet board in scope is rectangular, so the bbox is the exact outline; a
    concave outline would only over-constrain the free space conservatively.
    """
    if not segments:
        return []
    xs: list[float] = []
    ys: list[float] = []
    for (x1, y1), (x2, y2) in segments:
        xs.extend((x1, x2))
        ys.extend((y1, y2))
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    return [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
