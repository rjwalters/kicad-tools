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

from ..layers import LayerStack
from ..primitives import Pad, Route, Segment, Via
from ..rules import DesignRules
from .geometry import (
    Pt,
    merge_intervals,
    point_in_polygon,
    segment_polygon_interval,
)
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
        layer_stack: LayerStack | None = None,
    ) -> None:
        self.outline = outline
        self.pads = pads
        self.rules = rules or DesignRules()
        # Copper stack the 2.5D via injection replicates the mesh across
        # (issue #4276).  Consumed from the board's real routing-layer count --
        # NOT hardcoded to 2 -- so via edges span any number of adjacent layers.
        self.layer_stack = layer_stack or LayerStack.two_layer()
        # Per-via-hop layer-change cost (mm-equivalent).  Kept well above a
        # typical in-layer portal step so the search only dips when a layer is
        # actually blocked, never gratuitously (the via-soup guard, risk 3).
        self.via_cost = 5.0
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
        layer_stack: LayerStack | None = None,
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
        return cls(outline, pads, rules, pours, layer_stack)

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
        fixed_copper: list[Route] | None = None,
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

        Issue #4364: ``fixed_copper`` is the preserved copper of NON-listed
        nets (``Autorouter.existing_routes`` minus the routable set, loaded
        under ``--nets`` / ``--preserve-existing`` after #4355).  Because the
        per-pass ``committed_by_layer`` obstacle dict is reset empty at the top
        of every pass, the fixed copper is **re-seeded into it each pass** via
        the same :meth:`_route_obstacles_by_layer` capsule conversion the
        commit loop uses -- so a negotiated net routes AROUND foreign copper or
        is honestly declined, never emitted overlapping it.  The mesh committed
        model is net-AGNOSTIC (plain polygons, no same-net exemption); this is
        safe only because ``fixed_copper`` is pre-filtered to non-listed nets
        by the driver, so a net being routed never self-blocks on its own seed.
        ``fixed_copper=None`` / ``[]`` seeds nothing and is a byte-identical
        no-op relative to the pre-#4364 negotiation.

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
            # Inflated copper committed by earlier nets THIS pass, bucketed by
            # routing-graph layer index (issue #4276) -- later nets must clear
            # it (net-vs-net short avoidance).  Per-layer so an F.Cu net is not
            # blocked by another net's B.Cu cross-under.  Reset each pass so a
            # rip-up genuinely frees the corridor.
            committed_by_layer: dict[int, list[list[Pt]]] = {}
            # Issue #4364: pre-seed the preserved copper of NON-listed nets as
            # an immovable hard obstacle.  The dict is reset every pass (so a
            # rip-up frees listed-net corridors), therefore the fixed seed must
            # be re-applied every pass.  Reusing ``_route_obstacles_by_layer``
            # inflates and layer-buckets it identically to same-pass committed
            # copper, so both the in-layer fit and ``_route_via_injection``
            # (which reads the whole dict) honor it.
            self._seed_fixed_copper(committed_by_layer, fixed_copper)
            for key, start, end, net_class in ordered:
                start_L = self._layer_index(start.layer) or 0
                # Issue #4274: the net's own-layer committed copper doubles as
                # the lane-assignment model -- ``_route_with_portals`` narrows
                # this net's portals by that copper so its funnel produces a
                # parallel lane rather than the geodesic a prior net occupies.
                result = self._route_with_portals(
                    start,
                    end,
                    net_class,
                    negotiated_mode=True,
                    present_cost_factor=present,
                    committed=committed_by_layer.get(start_L, []),
                )
                # Issue #4276: when the in-layer attempt declines (a transverse
                # crossing the lane allocator cannot resolve), retry by dipping
                # layers -- via down, cross under, via up.  Additive: the
                # single-layer geodesic was already tried, so completion can only
                # rise and a dip that still cannot clear declines, never crosses.
                if result is None:
                    result = self._route_via_injection(
                        start,
                        end,
                        net_class,
                        committed_by_layer,
                        present_cost_factor=present,
                    )
                if result is None:
                    failed.append((key, start, end, net_class))
                    continue
                route, portals = result
                routes[key] = route
                for edge in portals:
                    navmesh.commit_portal(edge)
                for lidx, polys in self._route_obstacles_by_layer(route).items():
                    committed_by_layer.setdefault(lidx, []).extend(polys)

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

    # -- 2.5D via injection (issue #4276) ---------------------------------

    @property
    def _via_in_pad_allowed(self) -> bool:
        """Whether the configured fab tier permits via-in-pad (else OFF).

        Read from the real fab model exactly as the grid escape router does:
        ``rules.manufacturer`` -> ``MfrLimits.via_in_pad_supported`` (base
        ``jlcpcb`` = False, ``jlcpcb-tier1`` = True).  With no manufacturer
        configured the conservative default is False, so pad-site vias are
        pruned and only free-space portal-midpoint through-vias are injected.
        """
        mfr = self.rules.manufacturer
        if not mfr:
            return False
        try:
            from ..mfr_limits import get_mfr_limits

            return bool(get_mfr_limits(mfr).via_in_pad_supported)
        except Exception:
            return False

    def _layer_index(self, layer: object) -> int | None:
        """Routing-graph layer index for a KiCad layer enum (None if off-stack)."""
        try:
            return self.layer_stack.layer_enum_to_index(layer)  # type: ignore[arg-type]
        except Exception:
            return None

    def _keepouts_layer(self, net: int, agent_radius: float, layer_idx: int) -> list[Rect]:
        """Per-layer inflated pad keep-outs (issue #4276 section 3).

        An SMD pad blocks only its own copper layer; a through-hole pad blocks
        EVERY layer.  Same-net copper is never an obstacle.  The triangulation
        is shared across layers -- only this obstacle mask is per-layer.
        """
        from .obstacles import merge_overlapping

        try:
            layer_enum = self.layer_stack.index_to_layer_enum(layer_idx)
        except Exception:
            layer_enum = None

        raw: list[Rect] = []
        bx0, by0, bx1, by1 = self._bbox
        margin = 1e-3
        for pad in self.pads:
            if pad.net == net:
                continue
            # SMD pad on another layer does not block this one; PTH blocks all.
            if not pad.through_hole and layer_enum is not None and pad.layer != layer_enum:
                continue
            hx = pad.width / 2.0 + agent_radius
            hy = pad.height / 2.0 + agent_radius
            r = (
                max(pad.x - hx, bx0 + margin),
                max(pad.y - hy, by0 + margin),
                min(pad.x + hx, bx1 - margin),
                min(pad.y + hy, by1 - margin),
            )
            if r[2] - r[0] < margin or r[3] - r[1] < margin:
                continue
            raw.append(r)
        return merge_overlapping(raw)

    def _via_allowed_at(
        self,
        site: Pt,
        net: int,
        via_radius: float,
        layers: tuple[int, int],
        committed_by_layer: dict[int, list[list[Pt]]],
    ) -> bool:
        """Obstacle-aware via placement test (issue #4276 section 5, #3906).

        A via at ``site`` joining ``layers`` is admitted only when its body is
        DRC-legal on BOTH layers it spans:

        * inside a DIFFERENT net's pad -> a short, never allowed;
        * inside its OWN net's pad -> via-in-pad, allowed only when the fab tier
          permits it (default OFF -> pruned, so pad-site vias never ship);
        * over copper another net committed on either spanned layer -> declined.

        Portal midpoints are free-space by construction, so the default result
        is "allowed"; this gate exists to prune the pathological sites.
        """
        for pad in self.pads:
            hx = pad.width / 2.0 + via_radius
            hy = pad.height / 2.0 + via_radius
            if abs(site[0] - pad.x) <= hx and abs(site[1] - pad.y) <= hy:
                if pad.net == net:
                    if not self._via_in_pad_allowed:
                        return False
                else:
                    return False
        # A through-via spans every copper layer, so its body must clear
        # committed cross-net copper on ALL layers -- not just the two the A*
        # hop nominally joins.  Check every layer with committed copper.
        for caps in committed_by_layer.values():
            for poly in caps:
                if point_in_polygon(site, poly):
                    return False
        return True

    def _route_via_injection(
        self,
        start: Pad,
        end: Pad,
        net_class: object | None,
        committed_by_layer: dict[int, list[list[Pt]]],
        *,
        present_cost_factor: float,
    ) -> tuple[Route, list[tuple[int, int]]] | None:
        """Route by dipping layers when the in-layer geodesic is blocked (#4276).

        This is the 2.5D fallback the negotiator reaches for only after the
        single-layer attempt declines: it runs the ``(triangle, layer)`` A*
        (mesh replicated across ``layer_stack.num_layers``), splits the returned
        multi-layer corridor at its via sites, funnels + 45-fits EACH per-layer
        run against that layer's own obstacle model, and stitches the runs with
        through-vias.  If ANY run cannot be made clearance-clean the whole route
        declines (``None``) -- never a short (the authoritative octilinear gate
        stays in force per layer).
        """
        navmesh = self.build()
        num_layers = self.layer_stack.num_layers
        if not navmesh.triangles or num_layers < 2:
            return None

        start_layer = self._layer_index(start.layer)
        goal_layer = self._layer_index(end.layer)
        if start_layer is None or goal_layer is None:
            return None

        trace_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        clearance = self.rules.trace_clearance
        agent_radius = trace_w / 2.0 + clearance
        via_radius = self.rules.via_diameter / 2.0 + clearance
        net = start.net
        start_pt: Pt = (start.x, start.y)
        end_pt: Pt = (end.x, end.y)

        # Per-layer obstacle model for the authoritative octilinear fit.
        obstacles_by_layer: dict[int, ObstacleModel] = {}
        for lidx in range(num_layers):
            keepouts = self._keepouts_layer(net, agent_radius, lidx)
            pour_obstacles = self.pours + committed_by_layer.get(lidx, [])
            obstacles_by_layer[lidx] = ObstacleModel(self.outline, keepouts, pour_obstacles)

        # Per-layer portal blocking (issue #4276 section 3): a portal is blocked
        # on a layer when a trace cannot thread it there -- tested with the SAME
        # per-layer obstacle model the octilinear fit uses, on the segment
        # joining the two incident triangle centroids.  This gives A* a cost
        # reason to DIP where a layer is obstructed (by committed copper OR an
        # other-net pad on that layer) to a clear layer, rather than ploughing on
        # and declining at the fit.  Same-net pads are not obstacles, so a net's
        # own pad-escape portals stay open.  The authoritative per-layer fit
        # still gates every emitted leg, so this heuristic can only steer, never
        # ship a short.
        centroids = navmesh._centroids

        def portal_blocked(edge: tuple[int, int], layer: int) -> bool:
            tris = navmesh._edge_tris.get(edge)
            if not tris or len(tris) < 2:
                return False
            return not obstacles_by_layer[layer].is_clear(centroids[tris[0]], centroids[tris[1]])

        def via_allowed(site: Pt, layer_a: int, layer_b: int) -> bool:
            return self._via_allowed_at(
                site, net, via_radius, (layer_a, layer_b), committed_by_layer
            )

        pcf = present_cost_factor
        corridor = navmesh.astar_layered(
            start_pt,
            end_pt,
            start_layer,
            goal_layer,
            num_layers,
            portal_blocked,
            via_allowed,
            self.via_cost,
            present_cost_factor=pcf,
            cost_congestion=self.rules.cost_congestion if pcf else 0.0,
            congestion_threshold=self.rules.congestion_threshold,
        )
        if corridor is None:
            return None

        route = self._build_multilayer_route(
            navmesh, corridor, start, end, net, trace_w, obstacles_by_layer, committed_by_layer
        )
        if route is None:
            return None

        portals = [
            edge
            for i in range(len(corridor) - 1)
            if not corridor[i + 1][3]
            and (edge := navmesh._shared_edge(corridor[i][0], corridor[i + 1][0])) is not None
        ]
        return route, portals

    def _fit_layer_run(
        self,
        navmesh: NavMesh,
        tris: list[int],
        s_pt: Pt,
        e_pt: Pt,
        obstacles: ObstacleModel,
        committed_on_layer: list[list[Pt]],
    ) -> list[Pt] | None:
        """Funnel + 45-fit one per-layer run, with the P2.5 lane-narrowing retry.

        Mirrors the single-layer re-funnel (``_route_with_portals``): try the
        taut geodesic first, and if it collides copper committed on THIS layer,
        narrow the run's portals to their residual openings and retry the widest
        gap then each one-wall packing.  Every candidate is validated against the
        authoritative per-layer obstacle model, so a run that still cannot clear
        declines (``None``) rather than shipping a short.
        """
        fitted = self._fit_corridor(navmesh, tris, s_pt, e_pt, obstacles, None)
        if fitted is not None:
            return fitted
        if not committed_on_layer:
            return None
        derived: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for edge in navmesh.corridor_portals(tris):
            bands = _edge_consumed_bands(navmesh, edge, committed_on_layer)
            if bands:
                derived[edge] = bands
        if not derived:
            return None
        for pack in ("largest", "left", "right"):
            fitted = self._fit_corridor(navmesh, tris, s_pt, e_pt, obstacles, derived, pack)
            if fitted is not None:
                return fitted
        return None

    def _build_multilayer_route(
        self,
        navmesh: NavMesh,
        corridor: list[tuple[int, int, Pt, bool]],
        start: Pad,
        end: Pad,
        net: int,
        trace_w: float,
        obstacles_by_layer: dict[int, ObstacleModel],
        committed_by_layer: dict[int, list[list[Pt]]],
    ) -> Route | None:
        """Split a layered corridor into per-layer runs; funnel/fit/stitch (#4276)."""
        start_pt: Pt = (start.x, start.y)
        end_pt: Pt = (end.x, end.y)

        # Break the corridor into maximal same-layer runs, recording the
        # portal-midpoint via site at each layer change.
        runs: list[tuple[int, list[int], Pt, Pt]] = []
        via_sites: list[Pt] = []
        cur_layer = corridor[0][1]
        cur_tris = [corridor[0][0]]
        run_start = start_pt
        for k in range(1, len(corridor)):
            tri, layer, entry, via_into = corridor[k]
            if via_into:
                runs.append((cur_layer, cur_tris, run_start, entry))
                via_sites.append(entry)
                cur_layer = layer
                cur_tris = [tri]
                run_start = entry
            else:
                cur_tris.append(tri)
        runs.append((cur_layer, cur_tris, run_start, end_pt))

        route = Route(net=net, net_name=start.net_name)
        for layer_idx, tris, s_pt, e_pt in runs:
            if math.hypot(e_pt[0] - s_pt[0], e_pt[1] - s_pt[1]) < 1e-6:
                continue  # degenerate run (through-via passing this layer)
            try:
                layer_enum = self.layer_stack.index_to_layer_enum(layer_idx)
            except Exception:
                return None
            fitted = self._fit_layer_run(
                navmesh,
                tris,
                s_pt,
                e_pt,
                obstacles_by_layer[layer_idx],
                committed_by_layer.get(layer_idx, []),
            )
            if fitted is None or len(fitted) < 2:
                return None
            for i in range(len(fitted) - 1):
                a, b = fitted[i], fitted[i + 1]
                route.segments.append(
                    Segment(
                        x1=a[0],
                        y1=a[1],
                        x2=b[0],
                        y2=b[1],
                        width=trace_w,
                        layer=layer_enum,
                        net=net,
                        net_name=start.net_name,
                    )
                )

        if not route.segments or not via_sites:
            return None

        # Through-via per distinct site (default manufacturable via).  Blind /
        # buried are tier-gated and out of scope -- a placed via spans the whole
        # copper stack, so one Via joins the top and bottom outer layers.
        top = self.layer_stack.index_to_layer_enum(0)
        bottom = self.layer_stack.index_to_layer_enum(self.layer_stack.num_layers - 1)
        seen: set[tuple[float, float]] = set()
        for site in via_sites:
            key = (round(site[0], 4), round(site[1], 4))
            if key in seen:
                continue
            seen.add(key)
            route.vias.append(
                Via(
                    x=site[0],
                    y=site[1],
                    drill=self.rules.via_drill,
                    diameter=self.rules.via_diameter,
                    layers=(top, bottom),
                    net=net,
                    net_name=start.net_name,
                )
            )
        return route

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

    def _route_obstacles_by_layer(self, route: Route) -> dict[int, list[list[Pt]]]:
        """Committed-copper capsules bucketed by routing-graph layer index (#4276).

        A via-injected route lays copper on several layers; committed copper
        must be tracked per layer so later nets see it only where it actually
        sits (an F.Cu net is not blocked by another net's B.Cu cross-under).
        """
        half = self.rules.trace_width + self.rules.trace_clearance
        out: dict[int, list[list[Pt]]] = {}
        for seg in route.segments:
            lidx = self._layer_index(seg.layer)
            if lidx is None:
                continue
            poly = _segment_capsule((seg.x1, seg.y1), (seg.x2, seg.y2), half)
            if poly is not None:
                out.setdefault(lidx, []).append(poly)
        return out

    def _seed_fixed_copper(
        self,
        committed_by_layer: dict[int, list[list[Pt]]],
        fixed_copper: list[Route] | None,
    ) -> None:
        """Pre-seed ``committed_by_layer`` with preserved non-listed copper (#4364).

        Mirrors the same-pass commit loop's use of
        :meth:`_route_obstacles_by_layer`: each fixed route's segments become
        inflated per-layer capsule polygons so later listed nets clear them.
        Called at the top of every negotiation pass (the dict is reset each
        pass), so the seed is re-applied per pass.  A ``None`` / empty seed adds
        nothing and is a byte-identical no-op.
        """
        for route in fixed_copper or []:
            for lidx, polys in self._route_obstacles_by_layer(route).items():
                committed_by_layer.setdefault(lidx, []).extend(polys)

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
