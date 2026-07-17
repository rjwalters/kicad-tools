"""``LatticePathfinder`` -- the adaptive octilinear lattice engine (#4278).

Third route engine (``--route-engine lattice``), validated by the lattice
substrate spike on #4267.  The full pipeline:

    balanced quadtree lattice (built ONCE per board)   (quadtree.py)
      -> per-layer pad masking                          (obstacles.py)
      -> clearance-checked dogleg pad stubs             (quantize.dogleg_points)
      -> negotiated A* over the (node, layer) graph     (this module)
      -> Route of 45-legal Segments + through Vias      (path IS copper)

There is **no funnel stage and no octilinear post-fit stage**: every lattice
edge is 0/45/90/135 by construction, so an A* path over the lattice is
already 45-degree-legal copper and is emitted directly through the #3907
by-construction choke (``primitives.Segment`` 45-degree enforcement).

**Static substrate invariant (mirrors the mesh's ``triangulation_calls``
discipline):** the lattice + its per-layer static masks are built **once per
board** (:meth:`build`, counted by :attr:`lattice_builds`); negotiation only
mutates committed-copper state and history costs -- never the lattice.

**Via model -- N/A-by-construction gates:** via edges join *matching
free-space lattice nodes on adjacent layers* and every emitted via is a
through-via spanning the whole stack.  Because vias can only land on
free-space lattice nodes (a node inside any pad keep-out is masked),
**via-in-pad can never occur** -- the ``MfrLimits.via_in_pad_supported``
tier gate is moot for this engine rather than merely disabled.  Likewise
**blind/buried vias are out of scope by construction**: only through-via
edges are generated, and a committed through-via masks its node on ALL
layers.

**Never-ship-a-short (#3906):** every stub leg and every lattice resource is
validated against BOTH the static per-layer pad masks and a geometric
committed-copper model before acceptance; a connection that cannot clear
DECLINES (``None``) -- it is never emitted crossing copper.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from pathlib import Path

from ..layers import LayerStack
from ..primitives import Pad, Route, Segment, Via
from ..quantize import dogleg_points
from ..rules import DesignRules
from .geometry import Pt, Rect, dist, pt_in_rect
from .obstacles import CommittedCopper, LatticeObstacleModel
from .quadtree import EdgeKey, NodeKey, OctilinearLattice, RefineRegion

# A negotiation connection: (opaque hashable key, start pad, end pad, net_class)
# -- the same shape as ``mesh.pathfinder.Connection``.
Connection = tuple[object, Pad, Pad, object]

# A congestion resource: ("e", edge_key, layer) or ("v", node_key).
Resource = tuple[object, ...]

# A* state: (node_key, layer_index).
_State = tuple[NodeKey, int]
_GOAL: _State = (("GOAL",), -1)  # type: ignore[assignment]


@dataclass(frozen=True)
class LatticeNegotiationStats:
    """Outcome of a :meth:`LatticePathfinder.route_netset` negotiation run."""

    iterations: int
    converged: bool
    routed: int
    total: int
    lattice_builds: int

    @property
    def completion(self) -> float:
        return self.routed / self.total if self.total else 0.0


@dataclass
class _RouteResult:
    """Internal per-connection result: emitted route + commit geometry."""

    route: Route
    runs: list[tuple[int, list[Pt]]]  # (layer_index, polyline) per copper run
    via_points: list[Pt]
    resources: set[Resource]  # lattice edges/via-nodes consumed (history keys)


class LatticePathfinder:
    """Adaptive octilinear lattice pathfinder returning ``Route`` objects.

    The lattice is built once per board and reused; per-net legality consults
    the static per-layer pad masks plus a per-pass committed-copper model --
    no lattice rebuild, ever (:attr:`lattice_builds` stays 1).
    """

    # Mirrors ``MeshPathfinder.supports_waypoint_injection``: pads attach via
    # exact dogleg stubs, so the grid waypoint machinery does not apply.
    supports_waypoint_injection: bool = False

    def __init__(
        self,
        outline: list[Pt],
        pads: list[Pad],
        rules: DesignRules | None = None,
        layer_stack: LayerStack | None = None,
        *,
        coarse: float = 3.2,
        fine: float = 0.4,
        margin: float = 0.8,
        via_cost: float = 3.0,
    ) -> None:
        self.outline = outline
        self.pads = pads
        self.rules = rules or DesignRules()
        # Real copper stack (issue #4278 acceptance 6): the lattice is
        # replicated across the board's actual routing-layer count -- never
        # hardcoded to 2.
        self.layer_stack = layer_stack or LayerStack.two_layer()
        self.coarse = coarse
        self.fine = fine
        self.margin = margin
        # Per-via-hop layer-change cost (mm-equivalent; spike default 3.0).
        self.via_cost = via_cost

        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        self._bbox: Rect = (min(xs), min(ys), max(xs), max(ys))

        # Derived clearance gaps (centreline distances).
        tw = self.rules.trace_width
        clr = self.rules.trace_clearance
        via_r = self.rules.via_diameter / 2.0
        self._trace_half = tw / 2.0
        self._agent_radius = tw / 2.0 + clr
        self._copper_gap = tw + clr
        self._via_copper_gap = via_r + clr + tw / 2.0
        self._via_via_gap = self.rules.via_diameter + clr
        self._same_net_via_gap = self.rules.via_drill + self.rules.min_hole_to_hole
        self._via_pad_grow = via_r + clr - self._agent_radius

        # Static substrate, built lazily ONCE (the #4278 acceptance counter).
        self._lattice: OctilinearLattice | None = None
        self._obstacles: LatticeObstacleModel | None = None
        self.lattice_builds: int = 0
        # Per-connection decline reasons for the best negotiation pass
        # (honest shortfall diagnosis; reset by :meth:`route_netset`).
        self.failure_reasons: dict[object, str] = {}

    # -- construction from a board ----------------------------------------

    @classmethod
    def from_board(
        cls,
        pcb_path_or_text: str | Path,
        rules: DesignRules | None = None,
        layer_stack: LayerStack | None = None,
        **knobs: float,
    ) -> LatticePathfinder:
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
        return cls(outline, pads, rules, layer_stack, **knobs)

    # -- static substrate ---------------------------------------------------

    @property
    def num_layers(self) -> int:
        return self.layer_stack.num_layers

    def build(self) -> OctilinearLattice:
        """Build (once) and return the static per-board lattice + masks.

        Refine regions are pitch-derived per pad (issue #4278 risk 5): each
        pad requests a local cell size of half its nearest-neighbour pad
        distance, clamped to ``[fine / 2, fine]`` -- dense clusters densify
        locally, open space stays coarse.  Subsequent negotiation passes
        mutate only committed copper / history, never this substrate.
        """
        if self._lattice is not None:
            return self._lattice
        self.lattice_builds += 1
        self._lattice = OctilinearLattice(self._bbox, self._refine_regions(), coarse=self.coarse)
        self._obstacles = LatticeObstacleModel(
            self._lattice,
            self.pads,
            [self._pad_layer_indices(p) for p in self.pads],
            self.num_layers,
            self._agent_radius,
        )
        return self._lattice

    @property
    def obstacles(self) -> LatticeObstacleModel:
        """The static per-layer pad-mask model (builds the lattice on demand)."""
        self.build()
        assert self._obstacles is not None
        return self._obstacles

    def _refine_regions(self) -> list[RefineRegion]:
        """Pad keep-out rectangles inflated by ``margin``, pitch-derived fine."""
        centers = [(p.x, p.y) for p in self.pads]
        regions: list[RefineRegion] = []
        for i, pad in enumerate(self.pads):
            nn = math.inf
            for j, other in enumerate(centers):
                if j == i:
                    continue
                d = math.hypot(other[0] - pad.x, other[1] - pad.y)
                if d > 1e-9:
                    nn = min(nn, d)
            fine_i = (
                self.fine
                if not math.isfinite(nn)
                else min(self.fine, max(nn / 2.0, self.fine / 2.0))
            )
            hx = pad.width / 2.0 + self.margin
            hy = pad.height / 2.0 + self.margin
            regions.append(RefineRegion((pad.x - hx, pad.y - hy, pad.x + hx, pad.y + hy), fine_i))
        return regions

    def _pad_layer_indices(self, pad: Pad) -> tuple[int, ...]:
        """Routing-layer indices a pad's copper occupies.

        SMD pads mask only their own layer; through-hole pads mask every
        layer.  A pad whose layer is off the stack is treated conservatively
        as through-blocking (never under-mask -- the #3906 direction).
        """
        if pad.through_hole:
            return tuple(range(self.num_layers))
        try:
            return (self.layer_stack.layer_enum_to_index(pad.layer),)
        except Exception:
            return tuple(range(self.num_layers))

    def _fresh_committed(self) -> CommittedCopper:
        return CommittedCopper(
            self.num_layers,
            copper_gap=self._copper_gap,
            via_copper_gap=self._via_copper_gap,
            via_via_gap=self._via_via_gap,
            same_net_via_gap=self._same_net_via_gap,
        )

    # -- pad stubs ------------------------------------------------------------

    def pad_stubs(
        self,
        pad: Pad,
        net: int,
        committed: CommittedCopper | None = None,
        *,
        kmax: int = 4,
        search_radius: float | None = None,
    ) -> list[tuple[NodeKey, int, list[Pt], float]]:
        """Legal dogleg stubs ``(node_key, layer, polyline pad->node, length)``.

        Each stub is a 45-degree-legal two-segment dogleg
        (:func:`~kicad_tools.router.quantize.dogleg_points`) from the EXACT
        pad position to a nearby unmasked lattice node, with **every leg
        clearance-checked before acceptance** against the static per-layer
        pad masks AND committed copper (the ``subgrid.py`` reject-and-retry
        discipline -- the standing #3906 lesson).  A pad none of whose
        candidate stubs clears yields ``[]`` -> the connection declines.
        """
        lattice = self.build()
        obstacles = self.obstacles
        if committed is None:
            committed = self._fresh_committed()
        if search_radius is None:
            search_radius = max(3.0 * lattice.fine + max(pad.width, pad.height), 2.0)

        layers = self._pad_layer_indices(pad)
        candidates: list[tuple[float, NodeKey, Pt]] = []
        pad_pt = (pad.x, pad.y)
        for key, point in lattice.nodes.items():
            d = dist(point, pad_pt)
            if d <= search_radius:
                candidates.append((d, key, point))
        candidates.sort()

        out: list[tuple[NodeKey, int, list[Pt], float]] = []
        for _d, key, point in candidates:
            for layer in layers:
                if obstacles.node_blocked(key, layer, net):
                    continue
                if not committed.node_clear(point, layer, net):
                    continue
                accepted: list[Pt] | None = None
                for axis_first in (False, True):
                    poly = dogleg_points(pad.x, pad.y, point[0], point[1], axis_first=axis_first)
                    ok = True
                    for a, b in zip(poly, poly[1:], strict=False):
                        if obstacles.segment_blocked(a, b, layer, net):
                            ok = False
                            break
                        if not committed.seg_clear(a, b, layer, net):
                            ok = False
                            break
                    if ok:
                        accepted = poly
                        break
                if accepted is not None:
                    length = sum(dist(a, b) for a, b in zip(accepted, accepted[1:], strict=False))
                    out.append((key, layer, accepted, length))
            if len(out) >= kmax * len(layers):
                break
        return out

    # -- via legality -----------------------------------------------------------

    def _via_ok(self, key: NodeKey, net: int, committed: CommittedCopper) -> bool:
        """Through-via legality at a lattice node.

        Static part: the via body (inflated beyond the trace keep-out by
        ``via_radius + clearance - agent_radius``) must clear every OTHER-net
        pad on ANY layer (a through-via exists on all of them), and keep the
        hole-to-hole floor from through-hole pads of any net.  Because pads
        mask their surrounding nodes, free-space nodes pass by default -- the
        gate exists to prune boundary sites.  Dynamic part: committed copper
        on all layers + committed vias (:meth:`CommittedCopper.via_clear`).
        """
        lattice = self.build()
        obstacles = self.obstacles
        point = lattice.node_point(key)
        grow = max(self._via_pad_grow, 0.0)
        window = grow + 0.5
        for idx in obstacles.pads_near(
            point[0] - window, point[1] - window, point[0] + window, point[1] + window
        ):
            pad = self.pads[idx]
            if pad.through_hole:
                # Hole-to-hole floor applies regardless of net.
                min_cc = self.rules.via_drill / 2.0 + pad.drill / 2.0 + self.rules.min_hole_to_hole
                if dist(point, (pad.x, pad.y)) < min_cc - 1e-9:
                    return False
            if pad.net == net:
                continue
            rect = obstacles.pad_rects[idx]
            grown = (rect[0] - grow, rect[1] - grow, rect[2] + grow, rect[3] + grow)
            if pt_in_rect(point, grown):
                return False
        return committed.via_clear(point, net)

    # -- the route contract ------------------------------------------------------

    def route(
        self,
        start: Pad,
        end: Pad,
        net_class: object | None = None,
        **_ignored: object,
    ) -> Route | None:
        """Route a single net between two pads; ``None`` if it cannot.

        Single-net contract mirroring ``MeshPathfinder.route``: no committed
        copper, no history.  Multi-net work goes through :meth:`route_netset`.
        """
        result, _reason = self._route_impl(
            start, end, net_class, committed=self._fresh_committed(), history={}, present=0.0
        )
        return result.route if result is not None else None

    def _route_impl(
        self,
        start: Pad,
        end: Pad,
        net_class: object | None,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        allow_vias: bool = True,
    ) -> tuple[_RouteResult | None, str]:
        """A* over the (node, layer) graph; returns ``(result, reason)``.

        Octilinear geometric edge costs + ``via_cost`` per layer hop +
        congestion penalties (``present * history[resource]``) on capacity-1
        lattice resources.  Hard legality (static masks + geometric committed
        copper) is never traded against cost: an illegal resource is simply
        not expanded, so the search can only DECLINE, never ship a short.
        """
        lattice = self.build()
        obstacles = self.obstacles
        net = start.net

        stubs_a = self.pad_stubs(start, net, committed)
        if not stubs_a:
            return None, "pad-escape-start"
        stubs_b = self.pad_stubs(end, net, committed)
        if not stubs_b:
            return None, "pad-escape-end"

        goal: dict[_State, tuple[float, list[Pt]]] = {}
        for key, layer, poly, length in stubs_b:
            state = (key, layer)
            if state not in goal or length < goal[state][0]:
                goal[state] = (length, poly)
        end_pt = (end.x, end.y)

        def h(key: NodeKey) -> float:
            return dist(lattice.node_point(key), end_pt)

        g_score: dict[_State, float] = {}
        came: dict[_State, tuple[_State, str]] = {}
        start_stub: dict[_State, list[Pt]] = {}
        heap: list[tuple[float, int, _State]] = []
        counter = 0
        for key, layer, poly, length in stubs_a:
            state = (key, layer)
            if state not in g_score or length < g_score[state]:
                g_score[state] = length
                start_stub[state] = poly
                heapq.heappush(heap, (length + h(key), counter, state))
                counter += 1

        edge_ok: dict[tuple[EdgeKey, int], bool] = {}
        node_ok: dict[_State, bool] = {}
        via_ok: dict[NodeKey, bool] = {}
        end_state: _State | None = None

        while heap:
            f, _c, state = heapq.heappop(heap)
            if state == _GOAL:
                end_state = came[_GOAL][0]
                break
            key, layer = state
            g = g_score.get(state, math.inf)
            if f > g + h(key) + 1e-9:
                continue  # stale heap entry
            if state in goal:
                total = g + goal[state][0]
                if total < g_score.get(_GOAL, math.inf) - 1e-12:
                    g_score[_GOAL] = total
                    came[_GOAL] = (state, "arrive")
                    heapq.heappush(heap, (total, counter, _GOAL))
                    counter += 1
            for nbr, elen in lattice.adj.get(key, ()):
                edge = (min(key, nbr), max(key, nbr))
                ek = (edge, layer)
                ok = edge_ok.get(ek)
                if ok is None:
                    ok = not obstacles.edge_blocked(edge, layer, net) and committed.seg_clear(
                        lattice.node_point(edge[0]), lattice.node_point(edge[1]), layer, net
                    )
                    edge_ok[ek] = ok
                if not ok:
                    continue
                nstate = (nbr, layer)
                nok = node_ok.get(nstate)
                if nok is None:
                    nok = not obstacles.node_blocked(nbr, layer, net) and committed.node_clear(
                        lattice.node_point(nbr), layer, net
                    )
                    node_ok[nstate] = nok
                if not nok:
                    continue
                step = elen + present * history.get(("e", edge, layer), 0.0)
                tentative = g + step
                if tentative < g_score.get(nstate, math.inf) - 1e-12:
                    g_score[nstate] = tentative
                    came[nstate] = (state, "move")
                    heapq.heappush(heap, (tentative + h(nbr), counter, nstate))
                    counter += 1
            if allow_vias and self.num_layers > 1:
                vok = via_ok.get(key)
                if vok is None:
                    vok = self._via_ok(key, net, committed)
                    via_ok[key] = vok
                if vok:
                    # Via edges join matching nodes on ADJACENT layers only
                    # (issue #4278 acceptance 6).  The emitted via is still a
                    # through-via; a multi-layer dip pays via_cost per hop.
                    for nl in (layer - 1, layer + 1):
                        if nl < 0 or nl >= self.num_layers:
                            continue
                        nstate = (key, nl)
                        nok = node_ok.get(nstate)
                        if nok is None:
                            nok = not obstacles.node_blocked(key, nl, net) and committed.node_clear(
                                lattice.node_point(key), nl, net
                            )
                            node_ok[nstate] = nok
                        if not nok:
                            continue
                        step = self.via_cost + present * history.get(("v", key), 0.0)
                        tentative = g + step
                        if tentative < g_score.get(nstate, math.inf) - 1e-12:
                            g_score[nstate] = tentative
                            came[nstate] = (state, "via")
                            heapq.heappush(heap, (tentative + h(key), counter, nstate))
                            counter += 1

        if end_state is None:
            return None, "no-path"

        # -- reconstruct ---------------------------------------------------
        chain: list[_State] = [end_state]
        moves: list[str] = []
        cur = end_state
        while cur in came:
            prev, move = came[cur]
            chain.append(prev)
            moves.append(move)
            cur = prev
        chain.reverse()
        moves.reverse()
        start_state = chain[0]

        resources: set[Resource] = set()
        runs: list[tuple[int, list[Pt]]] = []
        via_points: list[Pt] = []
        cur_layer = start_state[1]
        cur_pts: list[Pt] = list(start_stub[start_state])  # pad -> first node
        for idx in range(1, len(chain)):
            key, layer = chain[idx]
            move = moves[idx - 1]
            if move == "via":
                runs.append((cur_layer, cur_pts))
                via_points.append(lattice.node_point(key))
                resources.add(("v", key))
                cur_layer = layer
                cur_pts = [lattice.node_point(key)]
            else:
                prev_key = chain[idx - 1][0]
                resources.add(("e", (min(prev_key, key), max(prev_key, key)), layer))
                cur_pts.append(lattice.node_point(key))
        _stub_len, stub_b_poly = goal[end_state]
        cur_pts.extend(reversed(stub_b_poly[:-1]))  # last node -> pad (exact)
        runs.append((cur_layer, cur_pts))

        route = self._emit(start, net, net_class, runs, via_points)
        if route is None:
            return None, "empty-route"
        return _RouteResult(route, runs, via_points, resources), "ok"

    # -- emission ---------------------------------------------------------------

    def _emit(
        self,
        start: Pad,
        net: int,
        net_class: object | None,
        runs: list[tuple[int, list[Pt]]],
        via_points: list[Pt],
    ) -> Route | None:
        """Lattice path + stubs -> ``Segment`` / ``Via`` (path IS copper).

        Emission goes straight through the #3907 by-construction choke
        (``Segment.to_sexp`` 45-degree enforcement) with
        :func:`~kicad_tools.router.optimizer.algorithms.merge_collinear`
        fusing the node-by-node steps; there is no post-fit stage.
        """
        from ..optimizer.algorithms import merge_collinear
        from ..optimizer.config import OptimizationConfig

        trace_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        config = OptimizationConfig()
        route = Route(net=net, net_name=start.net_name)
        for layer_idx, points in runs:
            try:
                layer_enum = self.layer_stack.index_to_layer_enum(layer_idx)
            except Exception:
                return None
            segments = [
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
                for a, b in zip(points, points[1:], strict=False)
                if dist(a, b) > 1e-9
            ]
            # Collinear merging is geometry-preserving (same centreline), so
            # no clearance re-check is needed.
            route.segments.extend(merge_collinear(segments, config))

        if not route.segments:
            return None

        # One through-via per distinct site (the default manufacturable via;
        # blind/buried are N/A-by-construction for this engine).
        top = self.layer_stack.index_to_layer_enum(0)
        bottom = self.layer_stack.index_to_layer_enum(self.num_layers - 1)
        seen: set[tuple[float, float]] = set()
        for site in via_points:
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

    # -- multi-net negotiation ------------------------------------------------

    def route_netset(
        self,
        connections: list[Connection],
        *,
        max_iterations: int = 8,
        present_cost_initial: float = 1.0,
        present_cost_growth: float = 1.6,
        history_increment_factor: float = 1.0,
    ) -> tuple[dict[object, Route], LatticeNegotiationStats]:
        """PathFinder/VPR-style negotiation over capacity-1 lattice resources.

        Each pass routes every connection shortest-first against committed
        copper (hard geometric blocking -- a net that would cross committed
        copper is declined, never shipped as a short) plus soft history
        penalties on contested lattice edges / via nodes.  Between passes the
        *desired* (uncongested) corridor of every FAILED connection accrues
        history (increment ``history_increment_factor * rules.cost_congestion
        * present``, the grid's congestion-cost knob), pressuring whoever
        occupies it to detour next pass; ``present`` grows geometrically so
        later passes push harder.  ``rules.congestion_threshold`` is
        degenerate here: lattice resources have capacity 1, so ANY sharing is
        over-threshold and blocking is hard rather than cost-mediated.

        The lattice is built **once** (:attr:`lattice_builds` == 1 across the
        whole negotiation); rip-up resets only the committed-copper model.
        Returns ``(routes_by_key, stats)`` for the best pass seen;
        :attr:`failure_reasons` carries the per-connection decline diagnosis
        for that pass.
        """
        self.build()

        ordered = sorted(
            connections,
            key=lambda c: math.hypot(c[1].x - c[2].x, c[1].y - c[2].y),
        )

        history: dict[Resource, float] = {}
        best_routes: dict[object, Route] = {}
        best_reasons: dict[object, str] = {}
        best_count = -1
        converged = False
        iterations_run = 0
        present = present_cost_initial

        for it in range(max_iterations):
            iterations_run = it + 1
            committed = self._fresh_committed()
            routes: dict[object, Route] = {}
            reasons: dict[object, str] = {}
            failed: list[Connection] = []
            for key, start, end, net_class in ordered:
                result, reason = self._route_impl(
                    start, end, net_class, committed=committed, history=history, present=present
                )
                if result is None:
                    reasons[key] = reason
                    failed.append((key, start, end, net_class))
                    continue
                routes[key] = result.route
                half = self._trace_half
                for layer_idx, points in result.runs:
                    committed.add_run(layer_idx, points, start.net, half)
                for via_pt in result.via_points:
                    committed.add_via(via_pt, start.net)

            if len(routes) > best_count:
                best_count = len(routes)
                best_routes = routes
                best_reasons = reasons

            if not failed:
                converged = True
                break
            if it == max_iterations - 1:
                break

            # Failed-net demand: bump history on the resources each blocked
            # net WANTS (its corridor with no committed copper in the way) so
            # the occupying nets are pressured to detour next pass.
            increment = history_increment_factor * self.rules.cost_congestion * present
            for _key, start, end, net_class in failed:
                desired, _reason = self._route_impl(
                    start,
                    end,
                    net_class,
                    committed=self._fresh_committed(),
                    history=history,
                    present=present,
                )
                if desired is None:
                    continue
                for resource in desired.resources:
                    history[resource] = history.get(resource, 0.0) + increment
            present *= present_cost_growth

        self.failure_reasons = best_reasons
        stats = LatticeNegotiationStats(
            iterations=iterations_run,
            converged=converged,
            routed=len(best_routes),
            total=len(connections),
            lattice_builds=self.lattice_builds,
        )
        return best_routes, stats


def _outline_from_edges(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[Pt]:
    """Board outline as a CCW bbox rectangle from Edge.Cuts segments.

    Same contract as the mesh engine's outline reader: every committed fleet
    board in scope is rectangular, so the bbox is the exact outline.
    """
    if not segments:
        return []
    xs: list[float] = []
    ys: list[float] = []
    for (x1, y1), (x2, y2) in segments:
        xs.extend((x1, x2))
        ys.extend((y1, y2))
    return [
        (min(xs), min(ys)),
        (max(xs), min(ys)),
        (max(xs), max(ys)),
        (min(xs), max(ys)),
    ]
