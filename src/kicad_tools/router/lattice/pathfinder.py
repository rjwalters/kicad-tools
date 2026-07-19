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

**Via model:** via edges join *matching lattice nodes on adjacent layers*
and every emitted via is a through-via spanning the whole stack.
**Blind/buried vias are out of scope by construction**: only through-via
edges are generated, and a committed through-via masks its node on ALL
layers.  **Via-in-pad is tier-gated, not N/A** (issue #4284): the static
pad masks exclude only OTHER-net pads, so a lattice node under a same-net
SMD pad is a legal route node -- :meth:`LatticePathfinder._via_ok`
therefore rejects any via whose barrel would intersect a same-net SMD pad
rect unless the configured fab tier sets ``MfrLimits.via_in_pad_supported``
(``DesignRules.manufacturer``; conservative OFF by default), exactly the
mesh engine's ``_via_allowed_at`` gate.  Other-net pad sites are always
rejected.

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
from typing import Any

from ..layers import LayerStack
from ..primitives import Pad, Route, Segment, Via
from ..quantize import dogleg_points
from ..rules import DesignRules
from .coupled import CoupledConnection
from .geometry import Pt, Rect, dist, pt_in_rect, seg_seg_dist
from .obstacles import CommittedCopper, LatticeObstacleModel, seg_body_crosses_pt
from .quadtree import EdgeKey, NodeKey, OctilinearLattice, RefineRegion

# A negotiation connection: (opaque hashable key, start pad, end pad, net_class)
# -- the same shape as ``mesh.pathfinder.Connection``.
Connection = tuple[object, Pad, Pad, object]

# A congestion resource: ("e", edge_key, layer) or ("v", node_key).
Resource = tuple[object, ...]

# A* state: (node_key, layer_index).
_State = tuple[NodeKey, int]
_GOAL: _State = (("GOAL",), -1)  # type: ignore[assignment]

# Escape-fan size for the oversize neck-down retry (issue #4293): a thin neck
# escapes the pad, but the WIDENED body must then egress at full width, so the
# useful escape node often sits at the edge of the congested field rather than
# hugging the pad.  A nearest-first count cap fills up on dead-end near nodes
# and never reaches that egress node, so the neck retry collects a large
# radius-bounded fan instead (the count-capped-in-a-pocket failure the pair
# path also avoids).  A search-tuning knob, not a geometric constant -- it only
# widens the candidate set, never the copper.
_OVERSIZE_STUB_KMAX = 4096


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
    # (layer_index, polyline, per-segment widths) per copper run.  The escape
    # legs may be a narrower neck than the lattice body (issue #4293), so each
    # segment carries the width it was emitted at.
    runs: list[tuple[int, list[Pt], list[float]]]
    via_points: list[Pt]
    resources: set[Resource]  # lattice edges/via-nodes consumed (history keys)


@dataclass
class _PairResult:
    """Internal per-pair result: both emitted legs + commit geometry (#4270)."""

    routes: list[Route]  # [P route, N route]
    runs: list[tuple[int, int, list[Pt]]]  # (net, layer_index, polyline) per leg
    resources: set[Resource]  # the fat centerline's lattice resources


def _diverse_pair_stubs(
    stubs: list[tuple[NodeKey, int, list[Pt], float]],
    mid: Pt,
    pair_pads: tuple[Pad, Pad],
    *,
    max_axis_dot: float = 0.5,
    per_bucket: int = 2,
    cap: int = 48,
) -> list[tuple[NodeKey, int, list[Pt], float]]:
    """Downselect fat pair stubs for direction + distance diversity (#4270).

    Two filters, then a diversity cap:

    * **Perpendicular gate**: the stub's first segment must leave the pad
      midpoint roughly THROUGH the pair gate, i.e. not nearly parallel to
      the P-N pad axis (``|dir . axis| <= max_axis_dot`` keeps the first
      leg within 30 degrees of perpendicular).  An along-axis approach lays one
      offset leg straight along the partner's endpoint pad -- the emitted
      geometry then fails the partner-pad floor anyway, so pruning it here
      lets the A* try approaches that can actually be emitted.  Zero-length
      stubs (midpoint exactly on a node) are dropped for the same reason:
      they carry no direction for the parity mechanism.
    * **Octant x distance-shell diversity**: keep the ``per_bucket``
      shortest stubs per (direction octant, shell of node distance in
      {<1 mm, <2 mm, >=2 mm}) so enclosed pockets cannot crowd out the far
      stubs that escape them.
    """
    import math as _math

    px = pair_pads[0].x - pair_pads[1].x
    py = pair_pads[0].y - pair_pads[1].y
    axis_len = _math.hypot(px, py)
    axis = (px / axis_len, py / axis_len) if axis_len > 1e-9 else None

    def first_dir(poly: list[Pt]) -> Pt | None:
        for a, b in zip(poly, poly[1:], strict=False):
            d = dist(a, b)
            if d > 1e-9:
                return ((b[0] - a[0]) / d, (b[1] - a[1]) / d)
        return None

    gated: list[tuple[NodeKey, int, list[Pt], float]] = []
    for stub in stubs:
        d = first_dir(stub[2])
        if d is None:
            continue
        if axis is not None and abs(d[0] * axis[0] + d[1] * axis[1]) > max_axis_dot:
            continue
        gated.append(stub)

    gated.sort(key=lambda s: s[3])
    buckets: dict[tuple[int, int, int], int] = {}
    out: list[tuple[NodeKey, int, list[Pt], float]] = []
    for stub in gated:
        node_pt = stub[2][-1]
        dx, dy = node_pt[0] - mid[0], node_pt[1] - mid[1]
        r = _math.hypot(dx, dy)
        octant = int(((_math.atan2(dy, dx) + _math.tau) % _math.tau) // (_math.tau / 8))
        shell = 0 if r < 1.0 else (1 if r < 2.0 else 2)
        key = (stub[1], octant, shell)
        if buckets.get(key, 0) >= per_bucket:
            continue
        buckets[key] = buckets.get(key, 0) + 1
        out.append(stub)
        if len(out) >= cap:
            break
    return out


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

        # Derived clearance gaps (centreline distances).  These are the
        # BOARD-GLOBAL defaults; per-connection widths/clearances from a
        # threaded net class (issue #4271) are resolved by
        # :meth:`_conn_geometry` and carried through the committed-copper
        # model per segment.
        tw = self.rules.trace_width
        clr = self.rules.trace_clearance
        via_r = self.rules.via_diameter / 2.0
        self._trace_half = tw / 2.0
        self._agent_radius = tw / 2.0 + clr
        self._via_via_gap = self.rules.via_diameter + clr
        self._same_net_via_gap = self.rules.via_drill + self.rules.min_hole_to_hole
        self._via_pad_grow = via_r + clr - self._agent_radius
        # Largest drilled hole on the board (PTH or NPTH), computed once:
        # the _via_ok pad query window must reach via_drill/2 + pad_drill/2
        # + min_hole_to_hole or large-drill pads silently escape the
        # hole-to-hole check (issue #4291 -- softstart's 1.3mm terminal
        # drills sat beyond the old ~0.8mm window).
        self._max_pad_drill = max((p.drill for p in pads), default=0.0)

        # Static substrate, built lazily ONCE (the #4278 acceptance counter).
        self._lattice: OctilinearLattice | None = None
        self._obstacles: LatticeObstacleModel | None = None
        self.lattice_builds: int = 0
        # Per-connection decline reasons for the best negotiation pass
        # (honest shortfall diagnosis; reset by :meth:`route_netset`).
        self.failure_reasons: dict[object, str] = {}
        # Per-diff-pair outcome for the best pass: "coupled" or a decline
        # reason, keyed by ``CoupledConnection.key`` (issue #4270).
        self.pair_outcomes: dict[object, str] = {}

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

        ``drill > 0`` also blocks every layer regardless of the
        ``through_hole`` flag (issue #4271): NPTH mounting holes load as
        ``np_thru_hole`` pads with ``through_hole=False`` on a single
        copper layer, but the DRILLED BARREL physically exists on every
        layer -- softstart rev-C shipped an inner-layer track through the
        fuse holder's 2.7 mm NPTH hole (kicad-cli ``hole_clearance``)
        before this.
        """
        if pad.through_hole or pad.drill > 0:
            return tuple(range(self.num_layers))
        try:
            return (self.layer_stack.layer_enum_to_index(pad.layer),)
        except Exception:
            return tuple(range(self.num_layers))

    def _fresh_committed(self) -> CommittedCopper:
        return CommittedCopper(
            self.num_layers,
            trace_half=self._trace_half,
            clearance=self.rules.trace_clearance,
            via_radius=self.rules.via_diameter / 2.0,
            via_via_gap=self._via_via_gap,
            same_net_via_gap=self._same_net_via_gap,
        )

    def _conn_geometry(self, net_class: object | None) -> tuple[float, float]:
        """Per-connection ``(trace half-width, clearance)`` (issue #4271).

        Width follows the class exactly as :meth:`_emit` does (the copper is
        spaced at the SAME width it is emitted at -- the #4271 invariant);
        clearance takes ``max(class, rules)`` so a class can only GROW the
        gap, never shrink below the design rules.  ``None`` -> global
        geometry, preserving the single-width pre-#4271 behavior exactly.
        """
        tw = getattr(net_class, "trace_width", None) or self.rules.trace_width
        clr = getattr(net_class, "clearance", None) or 0.0
        return tw / 2.0, max(clr, self.rules.trace_clearance)

    # -- pad stubs ------------------------------------------------------------

    def pad_stubs(
        self,
        pad: Pad,
        net: int,
        committed: CommittedCopper | None = None,
        *,
        kmax: int = 4,
        search_radius: float | None = None,
        extra_clearance: float = 0.0,
        partner_net: int | None = None,
        layers: tuple[int, ...] | None = None,
        exempt_pads: frozenset[int] | None = None,
        net_class: object | None = None,
    ) -> list[tuple[NodeKey, int, list[Pt], float]]:
        """Legal dogleg stubs ``(node_key, layer, polyline pad->node, length)``.

        Each stub is a 45-degree-legal two-segment dogleg
        (:func:`~kicad_tools.router.quantize.dogleg_points`) from the EXACT
        pad position to a nearby unmasked lattice node, with **every leg
        clearance-checked before acceptance** against the static per-layer
        pad masks AND committed copper (the ``subgrid.py`` reject-and-retry
        discipline -- the standing #3906 lesson).  A pad none of whose
        candidate stubs clears yields ``[]`` -> the connection declines.

        Fat-agent mode (issue #4270 diff pairs): ``extra_clearance`` grows
        every gap by the pair half-envelope and ``partner_net`` joins the
        "self" net set, so a pair *centerline* stub reserves room for both
        offset legs.  The static check becomes a geometric query against the
        grown pad rects (no second mask build -- ``lattice_builds`` stays 1)
        and ``layers`` restricts candidates to layers every pair endpoint
        pad owns (the v1 coupled run is planar).

        ``net_class`` (issue #4271) sizes every clearance check at the
        connection's TRUE half-width/clearance: committed-copper predicates
        take the per-class geometry, and the static pad checks are inflated
        by the surcharge over the global agent radius (single-ended path;
        the fat-agent path carries its own grown envelope).
        """
        half, clr = self._conn_geometry(net_class)
        return self._scan_stubs(
            pad,
            net,
            committed,
            half,
            clr,
            kmax=kmax,
            search_radius=search_radius,
            extra_clearance=extra_clearance,
            partner_net=partner_net,
            layers=layers,
            exempt_pads=exempt_pads,
        )

    def _scan_stubs(
        self,
        pad: Pad,
        net: int,
        committed: CommittedCopper | None,
        half: float,
        clr: float,
        *,
        kmax: int = 4,
        search_radius: float | None = None,
        extra_clearance: float = 0.0,
        partner_net: int | None = None,
        layers: tuple[int, ...] | None = None,
        exempt_pads: frozenset[int] | None = None,
    ) -> list[tuple[NodeKey, int, list[Pt], float]]:
        """Candidate scan for :meth:`pad_stubs` at an EXPLICIT ``(half, clr)``.

        Factored out so the tapered pad-escape fallback (issue #4293) can
        re-run the identical clearance-checked dogleg search at a narrower
        neck half-width and a wider fan without duplicating the reject-and-
        retry body.
        """
        lattice = self.build()
        obstacles = self.obstacles
        if committed is None:
            committed = self._fresh_committed()
        fat = extra_clearance > 0.0 or partner_net is not None
        if search_radius is None:
            search_radius = max(3.0 * lattice.fine + max(pad.width, pad.height), 2.0)
            if fat:
                # A fat agent may need to gather farther out of a dense pad
                # cluster.  The stub is part of the coupled centerline, so a
                # longer dogleg costs length, not coupling quality.
                search_radius = max(search_radius, 4.0)

        if layers is None:
            layers = self._pad_layer_indices(pad)
        pair_nets = {net} if partner_net is None else {net, partner_net}
        if fat:
            from .coupled import (
                committed_point_clear_grown,
                committed_seg_clear_grown,
                pads_block_point_grown,
                pads_block_segment_grown,
            )
        extra = max(0.0, half + clr - self._agent_radius)

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
                if fat:
                    if pads_block_point_grown(
                        obstacles, point, layer, pair_nets, extra_clearance, exempt_pads
                    ):
                        continue
                    if not committed_point_clear_grown(
                        committed, point, layer, pair_nets, extra_clearance
                    ):
                        continue
                else:
                    if obstacles.node_blocked(key, layer, net):
                        continue
                    if extra > 0.0 and obstacles.segment_blocked(point, point, layer, net, extra):
                        continue
                    if not committed.node_clear(point, layer, net, half, clr):
                        continue
                accepted: list[Pt] | None = None
                # Fat mode prefers the axis-first dogleg: its first leg is
                # axis-aligned, which is what the pair perpendicular gate
                # (:func:`_diverse_pair_stubs`) needs to see (#4270).
                for axis_first in (True, False) if fat else (False, True):
                    poly = dogleg_points(pad.x, pad.y, point[0], point[1], axis_first=axis_first)
                    ok = True
                    for a, b in zip(poly, poly[1:], strict=False):
                        if fat:
                            if pads_block_segment_grown(
                                obstacles, a, b, layer, pair_nets, extra_clearance, exempt_pads
                            ) or not committed_seg_clear_grown(
                                committed, a, b, layer, pair_nets, extra_clearance
                            ):
                                ok = False
                                break
                        else:
                            if obstacles.segment_blocked(a, b, layer, net, extra):
                                ok = False
                                break
                            if not committed.seg_clear(a, b, layer, net, half, clr):
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

    # -- tapered pad escape (issue #4293) --------------------------------------

    def _neck_width(self, net_class: object | None) -> float:
        """Principled neck width for a tapered pad escape (issue #4293).

        Oversize copper cannot always clear a dense pad field at its full
        class width.  The neck floor is the fab minimum track
        (``DesignRules.min_trace_width`` if configured, else the board default
        ``trace_width`` -- both DRC-legal, manufacturable widths), which a
        class may raise with ``NetClassRouting.neck_trace_width`` for
        ampacity.  It is never a magic constant and never below the fab min.
        """
        dru_min = self.rules.min_trace_width or self.rules.trace_width
        override = getattr(net_class, "neck_trace_width", None)
        neck = override if override else dru_min
        return max(dru_min, float(neck))

    def _escape_stubs(
        self,
        pad: Pad,
        net: int,
        committed: CommittedCopper | None,
        *,
        kmax: int,
        extra_clearance: float,
        partner_net: int | None,
        layers: tuple[int, ...] | None,
        exempt_pads: frozenset[int] | None,
        net_class: object | None,
    ) -> tuple[list[tuple[NodeKey, int, list[Pt], float]], float]:
        """Escape stubs for one pad, with an oversize neck-down fallback.

        Returns ``(stubs, emitted_width)`` -- the width the returned legs are
        emitted (and spaced) at.  The FULL-width escape is tried first and,
        when it yields ANY stub, is returned unchanged: every connection that
        already escapes is byte-identical to the pre-#4293 behavior.  Only
        when the oversize keep-out surcharge blocks EVERY full-width dogleg
        (the honest ``pad-escape-*`` decline of record) does a narrower legal
        neck retry -- with a WIDER escape fan (grown ``search_radius`` +
        ``kmax``) so the thin neck can thread past the full-width keep-out
        ring the oversize body could not clear, then widen back to the class
        width at the first lattice node.  Never-ship-a-short holds absolutely:
        the neck legs are clearance-checked at the neck width, exactly as the
        full-width legs are checked at the full width (#3906).
        """
        body_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        full_half, clr = self._conn_geometry(net_class)
        fat = extra_clearance > 0.0 or partner_net is not None

        stubs = self.pad_stubs(
            pad,
            net,
            committed,
            kmax=kmax,
            extra_clearance=extra_clearance,
            partner_net=partner_net,
            layers=layers,
            exempt_pads=exempt_pads,
            net_class=net_class,
        )
        # A successful full-width escape, or a fat coupled agent (whose grown
        # envelope has its own escape discipline), is returned as-is.
        if stubs or fat:
            return stubs, body_w

        neck_w = self._neck_width(net_class)
        neck_half = neck_w / 2.0
        if neck_half >= full_half - 1e-9:
            # Not oversize: the neck would not be narrower than the body, so
            # there is nothing to taper -- the honest decline stands.
            return stubs, body_w

        lattice = self.build()
        base_sr = max(3.0 * lattice.fine + max(pad.width, pad.height), 2.0)
        # Reach past the full-width keep-out ring that blocked the body escape:
        # a thin neck can clear a node close to the pad, but the widened body
        # must then EGRESS at full width, so the useful escape node is often
        # one that sits at the edge of the congested field.  Grow the radius by
        # the full-width surcharge and collect EVERY clearing node in it (a
        # radius-bounded fan, not a nearest-first count cap that would exhaust
        # itself on dead-end near nodes with no full-width egress -- the
        # count-capped-in-a-pocket failure the pair path also avoids).
        grown_sr = base_sr + max(0.0, full_half + clr - self._agent_radius)
        neck_stubs = self._scan_stubs(
            pad,
            net,
            committed,
            neck_half,
            clr,
            kmax=_OVERSIZE_STUB_KMAX,
            search_radius=grown_sr,
            layers=layers,
            exempt_pads=exempt_pads,
        )
        return neck_stubs, neck_w

    # -- via legality -----------------------------------------------------------

    @property
    def _via_in_pad_allowed(self) -> bool:
        """Whether the configured fab tier permits via-in-pad (else OFF).

        Read from the real fab model exactly as the mesh engine
        (``mesh/pathfinder.py``) and the grid escape router do:
        ``rules.manufacturer`` -> ``MfrLimits.via_in_pad_supported`` (base
        ``jlcpcb`` = False, ``jlcpcb-tier1`` = True).  With no manufacturer
        configured the conservative default is False, so pad-site vias are
        pruned and layer changes happen only after a clear escape stub.
        """
        mfr = self.rules.manufacturer
        if not mfr:
            return False
        try:
            from ..mfr_limits import get_mfr_limits

            return bool(get_mfr_limits(mfr).via_in_pad_supported)
        except Exception:
            return False

    def _via_ok(self, key: NodeKey, net: int, committed: CommittedCopper) -> bool:
        """Through-via legality at a lattice node.

        Static part: the via body (inflated beyond the trace keep-out by
        ``via_radius + clearance - agent_radius``) must clear every OTHER-net
        pad on ANY layer (a through-via exists on all of them), and keep the
        hole-to-hole floor from through-hole pads of any net.  Additionally
        (issue #4284) the via barrel must not intersect ANY SMD pad rect
        (window: pad half-extent + via radius, the mesh engine's window):
        other-net is always rejected (the grown-rect veto already covers it
        with clearance on top), and a SAME-net hit is via-in-pad -- admitted
        only when the fab tier supports it (:attr:`_via_in_pad_allowed`).
        Dynamic part: committed copper on all layers + committed vias
        (:meth:`CommittedCopper.via_clear`).
        """
        lattice = self.build()
        obstacles = self.obstacles
        point = lattice.node_point(key)
        via_radius = self.rules.via_diameter / 2.0
        grow = max(self._via_pad_grow, 0.0)
        # The query window must cover the farthest centre distance any check
        # below can reject at.  The hole-to-hole floor against the board's
        # largest drill needs via_drill/2 + max_drill/2 + min_hole_to_hole
        # -- beyond the copper-derived window for large-drill PTH/NPTH pads
        # (issue #4291), which the old window silently left unchecked.
        hole_window = (
            self.rules.via_drill / 2.0 + self._max_pad_drill / 2.0 + self.rules.min_hole_to_hole
        )
        window = max(max(grow, via_radius) + 0.5, hole_window)
        for idx in obstacles.pads_near(
            point[0] - window, point[1] - window, point[0] + window, point[1] + window
        ):
            pad = self.pads[idx]
            if pad.through_hole or pad.drill > 0:
                # Hole-to-hole floor applies regardless of net (and to NPTH
                # holes, whose through_hole flag is False -- issue #4271).
                min_cc = self.rules.via_drill / 2.0 + pad.drill / 2.0 + self.rules.min_hole_to_hole
                if dist(point, (pad.x, pad.y)) < min_cc - 1e-9:
                    return False
            elif (
                abs(point[0] - pad.x) <= pad.width / 2.0 + via_radius
                and abs(point[1] - pad.y) <= pad.height / 2.0 + via_radius
            ):
                # Via barrel intersects an SMD pad rect (#4284).  Same-net is
                # via-in-pad: legal only on fab tiers that fill/cap the via
                # (jlcpcb-tier1, pcbway); the default tier rejects so the
                # layer change moves off the pad onto the escape stub.
                # Other-net falls through to the unconditional grown-rect
                # veto below.
                if pad.net == net and not self._via_in_pad_allowed:
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
        extra_clearance: float = 0.0,
        partner_net: int | None = None,
        stub_layers: tuple[int, ...] | None = None,
        stubs_override: tuple[list, list] | None = None,
        exempt_pads: frozenset[int] | None = None,
    ) -> tuple[_RouteResult | None, str]:
        """A* over the (node, layer) graph; returns ``(result, reason)``.

        Octilinear geometric edge costs + ``via_cost`` per layer hop +
        congestion penalties (``present * history[resource]``) on capacity-1
        lattice resources.  Hard legality (static masks + geometric committed
        copper) is never traded against cost: an illegal resource is simply
        not expanded, so the search can only DECLINE, never ship a short.

        Fat-agent mode (issue #4270): ``extra_clearance`` / ``partner_net``
        switch every legality check to the grown geometric predicates from
        :mod:`.coupled`, so the path reserves the whole pair envelope.  Fat
        agents are planar in v1 -- ``allow_vias`` is forced off (a coupled
        run that cannot complete on one layer declines honestly).
        ``stubs_override`` lets :meth:`_route_pair_impl` supply parity-
        filtered stub sets (``(stubs_a, stubs_b)``) instead of recomputing.
        """
        lattice = self.build()
        obstacles = self.obstacles
        net = start.net
        # Per-connection copper geometry (issue #4271): the class half-width
        # and clearance size EVERY legality check below, and ``extra`` is the
        # keep-out surcharge over the global agent radius for the static pad
        # masks (0.0 for default-width nets -> pre-#4271 behavior exactly).
        half, clr = self._conn_geometry(net_class)
        extra = max(0.0, half + clr - self._agent_radius)

        fat = extra_clearance > 0.0 or partner_net is not None
        if fat:
            # v1 coupled runs are planar; there is no fat via legality.
            allow_vias = False
            from .coupled import (
                committed_point_clear_grown,
                committed_seg_clear_grown,
                pads_block_point_grown,
                pads_block_segment_grown,
            )

            pair_nets = {net} if partner_net is None else {net, partner_net}

        # The lattice body is emitted (and spaced) at the full class width;
        # the pad-escape legs may taper to a narrower neck (issue #4293).
        body_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        if stubs_override is not None:
            stubs_a, stubs_b = stubs_override
            width_a = width_b = body_w
        else:
            stub_kmax = 24 if fat else 4
            stubs_a, width_a = self._escape_stubs(
                start,
                net,
                committed,
                kmax=stub_kmax,
                extra_clearance=extra_clearance,
                partner_net=partner_net,
                layers=stub_layers,
                exempt_pads=exempt_pads,
                net_class=net_class,
            )
            stubs_b, width_b = self._escape_stubs(
                end,
                net,
                committed,
                kmax=stub_kmax,
                extra_clearance=extra_clearance,
                partner_net=partner_net,
                layers=stub_layers,
                exempt_pads=exempt_pads,
                net_class=net_class,
            )
        if not stubs_a:
            return None, "pad-escape-start"
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
                    if fat:
                        pa = lattice.node_point(edge[0])
                        pb = lattice.node_point(edge[1])
                        ok = not pads_block_segment_grown(
                            obstacles, pa, pb, layer, pair_nets, extra_clearance, exempt_pads
                        ) and committed_seg_clear_grown(
                            committed, pa, pb, layer, pair_nets, extra_clearance
                        )
                    else:
                        ea = lattice.node_point(edge[0])
                        eb = lattice.node_point(edge[1])
                        ok = (
                            not obstacles.edge_blocked(edge, layer, net)
                            and not (
                                extra > 0.0 and obstacles.segment_blocked(ea, eb, layer, net, extra)
                            )
                            and committed.seg_clear(ea, eb, layer, net, half, clr)
                        )
                    edge_ok[ek] = ok
                if not ok:
                    continue
                nstate = (nbr, layer)
                nok = node_ok.get(nstate)
                if nok is None:
                    if fat:
                        npt = lattice.node_point(nbr)
                        nok = not pads_block_point_grown(
                            obstacles, npt, layer, pair_nets, extra_clearance, exempt_pads
                        ) and committed_point_clear_grown(
                            committed, npt, layer, pair_nets, extra_clearance
                        )
                    else:
                        npt = lattice.node_point(nbr)
                        nok = (
                            not obstacles.node_blocked(nbr, layer, net)
                            and not (
                                extra > 0.0
                                and obstacles.segment_blocked(npt, npt, layer, net, extra)
                            )
                            and committed.node_clear(npt, layer, net, half, clr)
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
                            kpt = lattice.node_point(key)
                            nok = (
                                not obstacles.node_blocked(key, nl, net)
                                and not (
                                    extra > 0.0
                                    and obstacles.segment_blocked(kpt, kpt, nl, net, extra)
                                )
                                and committed.node_clear(kpt, nl, net, half, clr)
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
        runs: list[tuple[int, list[Pt], list[float]]] = []
        via_points: list[Pt] = []
        cur_layer = start_state[1]
        start_poly = list(start_stub[start_state])  # pad -> first node
        cur_pts: list[Pt] = start_poly
        # The start escape legs carry ``width_a`` (neck or full); the lattice
        # body carries ``body_w``; the end escape legs carry ``width_b``.  Each
        # segment is spaced at the width it is EMITTED at (issue #4293).
        cur_ws: list[float] = [width_a] * (len(start_poly) - 1)
        for idx in range(1, len(chain)):
            key, layer = chain[idx]
            move = moves[idx - 1]
            if move == "via":
                runs.append((cur_layer, cur_pts, cur_ws))
                via_points.append(lattice.node_point(key))
                resources.add(("v", key))
                cur_layer = layer
                cur_pts = [lattice.node_point(key)]
                cur_ws = []
            else:
                prev_key = chain[idx - 1][0]
                resources.add(("e", (min(prev_key, key), max(prev_key, key)), layer))
                cur_pts.append(lattice.node_point(key))
                cur_ws.append(body_w)
        _stub_len, stub_b_poly = goal[end_state]
        tail = list(reversed(stub_b_poly[:-1]))  # last node -> pad (exact)
        cur_pts.extend(tail)
        cur_ws.extend([width_b] * len(tail))
        runs.append((cur_layer, cur_pts, cur_ws))

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
        runs: list[tuple[int, list[Pt], list[float]]],
        via_points: list[Pt],
    ) -> Route | None:
        """Lattice path + stubs -> ``Segment`` / ``Via`` (path IS copper).

        Emission goes straight through the #3907 by-construction choke
        (``Segment.to_sexp`` 45-degree enforcement) with
        :func:`~kicad_tools.router.optimizer.algorithms.merge_collinear`
        fusing the node-by-node steps; there is no post-fit stage.

        Each segment is emitted at its own width (issue #4293): the escape
        legs may be a narrower neck than the widened lattice body.  Collinear
        merging is done PER contiguous equal-width group so a neck leg and a
        body segment never fuse into one width -- a uniform-width run (the
        pre-#4293 case) is one group and merges exactly as before.
        """
        from ..optimizer.algorithms import merge_collinear
        from ..optimizer.config import OptimizationConfig

        config = OptimizationConfig()
        route = Route(net=net, net_name=start.net_name)
        for layer_idx, points, widths in runs:
            try:
                layer_enum = self.layer_stack.index_to_layer_enum(layer_idx)
            except Exception:
                return None
            raw = [
                Segment(
                    x1=a[0],
                    y1=a[1],
                    x2=b[0],
                    y2=b[1],
                    width=w,
                    layer=layer_enum,
                    net=net,
                    net_name=start.net_name,
                )
                for (a, b), w in zip(zip(points, points[1:], strict=False), widths, strict=False)
                if dist(a, b) > 1e-9
            ]
            # Merge only within contiguous equal-width groups (collinear
            # merging is geometry-preserving, so no clearance re-check).
            i = 0
            while i < len(raw):
                j = i + 1
                while j < len(raw) and abs(raw[j].width - raw[i].width) < 1e-12:
                    j += 1
                route.segments.extend(merge_collinear(raw[i:j], config))
                i = j

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

    # -- diff-pair coupled routing (issue #4270) --------------------------------

    def _pair_attach(
        self,
        pad: Pad,
        target: Pt,
        layer: int,
        net: int,
        pair_nets: set[int],
        committed: CommittedCopper,
    ) -> list[Pt] | None:
        """Dogleg from the exact pad position to an emitted leg endpoint.

        Ordinary single-trace clearance (``extra = 0``) but with BOTH pair
        nets as "self" -- the attach necessarily runs between the pair's own
        pads.  Returns the accepted polyline (pad first) or ``None``.
        """
        from .coupled import pads_block_segment_grown

        if dist((pad.x, pad.y), target) <= 1e-9:
            return [(pad.x, pad.y)]
        for axis_first in (False, True):
            poly = dogleg_points(pad.x, pad.y, target[0], target[1], axis_first=axis_first)
            ok = True
            for a, b in zip(poly, poly[1:], strict=False):
                if dist(a, b) <= 1e-9:
                    continue
                if pads_block_segment_grown(self.obstacles, a, b, layer, pair_nets, 0.0):
                    ok = False
                    break
                if not committed.seg_clear(a, b, layer, net):
                    ok = False
                    break
            if ok:
                return poly
        return None

    def _emit_leg(
        self, net: int, net_name: str, points: list[Pt], layer_idx: int, trace_w: float
    ) -> Route | None:
        """One offset leg -> a via-free ``Route`` (same #3907 choke as _emit)."""
        from ..optimizer.algorithms import merge_collinear
        from ..optimizer.config import OptimizationConfig

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
                net_name=net_name,
            )
            for a, b in zip(points, points[1:], strict=False)
            if dist(a, b) > 1e-9
        ]
        route = Route(net=net, net_name=net_name)
        route.segments.extend(merge_collinear(segments, OptimizationConfig()))
        return route if route.segments else None

    def _route_pair_impl(
        self,
        pc: CoupledConnection,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
    ) -> tuple[_PairResult | None, str]:
        """Route one engaged diff pair as a fat centerline agent (#4270).

        Pipeline: virtual midpoint pads -> parity-filtered fat stubs ->
        planar fat A* -> collinear merge -> geometric ``+/- pitch/2`` offset
        emission -> polarity assignment -> pad-attach doglegs -> full
        geometric re-verification of both legs (masks + committed copper +
        intra-pair floor + partner-pad floor) -> emit.  ANY failure declines
        with a reason; nothing partial is ever committed (#3906).

        Parity filtering: the polarity a coupled run can serve is fixed by
        the travel direction of its first/last stub segment (offsetting is
        continuous, so "P on the left" propagates end to end).  Stubs are
        therefore partitioned by which side P would land on and the A* runs
        once per consistent parity -- this lets a pair whose direct approach
        would need a polarity twist hook around an endpoint and approach
        from the reverse direction instead of declining outright.
        """
        from .coupled import (
            assign_polarity,
            merge_collinear_points,
            offset_polyline,
            seg_rect_dist,
            side_bit,
        )

        net_p = pc.pad_p_a.net
        net_n = pc.pad_n_a.net
        pair_nets = {net_p, net_n}
        half_pitch = pc.pitch / 2.0

        layer_sets = [
            set(self._pad_layer_indices(p))
            for p in (pc.pad_p_a, pc.pad_n_a, pc.pad_p_b, pc.pad_n_b)
        ]
        common = tuple(sorted(set.intersection(*layer_sets)))
        if not common:
            return None, "pair-no-common-layer"

        mid_a, mid_b = pc.mid_a, pc.mid_b
        v_a = Pad(
            x=mid_a[0],
            y=mid_a[1],
            width=0.0,
            height=0.0,
            net=net_p,
            net_name=pc.pad_p_a.net_name,
            layer=pc.pad_p_a.layer,
            ref=pc.pad_p_a.ref,
            pin=pc.pad_p_a.pin,
        )
        v_b = Pad(
            x=mid_b[0],
            y=mid_b[1],
            width=0.0,
            height=0.0,
            net=net_p,
            net_name=pc.pad_p_b.net_name,
            layer=pc.pad_p_b.layer,
            ref=pc.pad_p_b.ref,
            pin=pc.pad_p_b.pin,
        )

        # The four main-run endpoint pads are the ONLY pads the fat
        # centerline may overlap; every other pad -- including non-endpoint
        # pads of the pair's own nets -- is an obstacle, because the leg of
        # the opposite polarity would otherwise land on it (board-06 USB2
        # B-row lesson).
        endpoint_sites = {
            (round(pad.x, 6), round(pad.y, 6), pad.net)
            for pad in (pc.pad_p_a, pc.pad_n_a, pc.pad_p_b, pc.pad_n_b)
        }
        exempt = frozenset(
            idx
            for idx, pad in enumerate(self.pads)
            if (round(pad.x, 6), round(pad.y, 6), pad.net) in endpoint_sites
        )

        # Collect EVERY legal fat stub in the search radius, then downselect
        # for geometric diversity.  A count-capped nearest-first scan can
        # exhaust itself inside an enclosed pocket (the board-06 BGA guard
        # ring) and never reach the one dogleg that threads the way out.
        raw_a = self.pad_stubs(
            v_a,
            net_p,
            committed,
            kmax=10_000,
            extra_clearance=half_pitch,
            partner_net=net_n,
            layers=common,
            exempt_pads=exempt,
        )
        stubs_a = _diverse_pair_stubs(raw_a, pc.mid_a, (pc.pad_p_a, pc.pad_n_a))
        if not stubs_a:
            return None, "pair-escape-a"
        raw_b = self.pad_stubs(
            v_b,
            net_p,
            committed,
            kmax=10_000,
            extra_clearance=half_pitch,
            partner_net=net_n,
            layers=common,
            exempt_pads=exempt,
        )
        stubs_b = _diverse_pair_stubs(raw_b, pc.mid_b, (pc.pad_p_b, pc.pad_n_b))
        if not stubs_b:
            return None, "pair-escape-b"

        def first_dir(poly: list[Pt]) -> Pt | None:
            for a, b in zip(poly, poly[1:], strict=False):
                if dist(a, b) > 1e-9:
                    length = dist(a, b)
                    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
            return None

        def stub_bit(stub: tuple[NodeKey, int, list[Pt], float], *, at_goal: bool) -> bool | None:
            d = first_dir(stub[2])
            if d is None:
                return None
            if at_goal:
                # The goal stub is traversed node -> pad, so the centerline's
                # final travel direction is the REVERSE of the stub polyline.
                d = (-d[0], -d[1])
                return side_bit(d, pc.pad_p_b, pc.pad_n_b)
            return side_bit(d, pc.pad_p_a, pc.pad_n_a)

        # A zero-length stub (a lattice node exactly at the midpoint) has no
        # direction of its own, so its polarity side is set by whatever
        # lattice edge the search takes first -- outside the parity
        # mechanism's control.  It is EXCLUDED (already dropped by the
        # perpendicular gate in :func:`_diverse_pair_stubs`); directed
        # doglegs to the surrounding nodes cover the same connectivity.
        by_bit_a: dict[bool, list] = {True: [], False: []}
        for stub in stubs_a:
            bit = stub_bit(stub, at_goal=False)
            if bit is not None:
                by_bit_a[bit].append(stub)
        by_bit_b: dict[bool, list] = {True: [], False: []}
        for stub in stubs_b:
            bit = stub_bit(stub, at_goal=True)
            if bit is not None:
                by_bit_b[bit].append(stub)

        # Try the parity with the shortest direct stubs first.
        def parity_rank(bit: bool) -> float:
            sa = min((s[3] for s in by_bit_a[bit]), default=math.inf)
            sb = min((s[3] for s in by_bit_b[bit]), default=math.inf)
            return sa + sb

        from .coupled import pads_block_segment_grown

        trace_w = getattr(pc.net_class, "trace_width", None) or self.rules.trace_width
        intra_clearance = pc.pitch - trace_w

        def finish(result: _RouteResult) -> tuple[_PairResult | None, str]:
            """Offset emission + verification of one fat centerline."""
            if len(result.runs) != 1 or result.via_points:
                return None, "pair-not-planar"  # defensive: vias are off

            layer, raw_pts, _widths = result.runs[0]
            center = merge_collinear_points(raw_pts)
            if len(center) < 2:
                return None, "pair-degenerate-centerline"
            plus = offset_polyline(center, +half_pitch)
            minus = offset_polyline(center, -half_pitch)
            if plus is None or minus is None:
                return None, "pair-offset-degenerate"
            assigned = assign_polarity(plus, minus, pc.pad_p_a, pc.pad_n_a, pc.pad_p_b, pc.pad_n_b)
            if assigned is None:
                return None, "pair-polarity-twist"
            leg_p, leg_n = assigned

            legs: list[tuple[int, str, list[Pt]]] = []
            for net, pad_a, pad_b, leg in (
                (net_p, pc.pad_p_a, pc.pad_p_b, leg_p),
                (net_n, pc.pad_n_a, pc.pad_n_b, leg_n),
            ):
                attach_a = self._pair_attach(pad_a, leg[0], layer, net, pair_nets, committed)
                if attach_a is None:
                    return None, "pair-pad-attach"
                attach_b = self._pair_attach(pad_b, leg[-1], layer, net, pair_nets, committed)
                if attach_b is None:
                    return None, "pair-pad-attach"
                full = attach_a[:-1] + leg + list(reversed(attach_b))[1:]

                # Never-ship-a-short re-verification of the emitted leg:
                # static masks (pair pads are "self") + committed copper at
                # NORMAL single-trace clearance.  The fat centerline
                # guarantees the parallel body; this catches miter pokes at
                # sharp turns and the attach doglegs.
                for a, b in zip(full, full[1:], strict=False):
                    if dist(a, b) <= 1e-9:
                        continue
                    if pads_block_segment_grown(self.obstacles, a, b, layer, pair_nets, 0.0):
                        return None, "pair-leg-blocked"
                    if not committed.seg_clear(a, b, layer, net):
                        return None, "pair-leg-blocked"
                legs.append((net, pad_a.net_name, full))

            # Intra-pair floor: leg-to-leg centreline separation must never
            # dip below the pitch (== trace_width + intra-pair clearance).
            # The offset body is at exactly the pitch by construction; this
            # guards the attach doglegs and miter joints.  1e-4 mm slop.
            full_p, full_n = legs[0][2], legs[1][2]
            segs_p = [(a, b) for a, b in zip(full_p, full_p[1:], strict=False) if dist(a, b) > 1e-9]
            segs_n = [(a, b) for a, b in zip(full_n, full_n[1:], strict=False) if dist(a, b) > 1e-9]
            for a, b in segs_p:
                for c, d in segs_n:
                    if seg_seg_dist(a, b, c, d) < pc.pitch - 1e-4:
                        return None, "pair-intra-clearance"

            # Partner-pad floor: each leg must keep the intra-pair clearance
            # to the OTHER net's raw pad copper (the pads sit inside the
            # pair envelope, so the static mask deliberately skipped them).
            for (net, _nn, full), other_net in ((legs[0], net_n), (legs[1], net_p)):
                need = intra_clearance + trace_w / 2.0 - 1e-4
                for idx, pad in enumerate(self.pads):
                    if pad.net != other_net:
                        continue
                    if layer not in self.obstacles.pad_layer_indices[idx]:
                        continue
                    rect = (
                        pad.x - pad.width / 2.0,
                        pad.y - pad.height / 2.0,
                        pad.x + pad.width / 2.0,
                        pad.y + pad.height / 2.0,
                    )
                    for a, b in zip(full, full[1:], strict=False):
                        if dist(a, b) <= 1e-9:
                            continue
                        if seg_rect_dist(a, b, rect) < need:
                            return None, "pair-partner-pad"

            routes: list[Route] = []
            for net, net_name, full in legs:
                route = self._emit_leg(net, net_name, full, layer, trace_w)
                if route is None:
                    return None, "pair-empty-route"
                routes.append(route)
            runs = [(net, layer, full) for net, _nn, full in legs]
            return _PairResult(routes=routes, runs=runs, resources=result.resources), "ok"

        # Attempt each consistent parity through the FULL pipeline: a parity
        # whose shortest path emits degenerate/blocked legs must not doom the
        # pair when the other parity works.
        reason = "pair-no-path"
        for bit in sorted((True, False), key=parity_rank):
            if not by_bit_a[bit] or not by_bit_b[bit]:
                continue
            result, search_reason = self._route_impl(
                v_a,
                v_b,
                pc.net_class,
                committed=committed,
                history=history,
                present=present,
                allow_vias=False,
                extra_clearance=half_pitch,
                partner_net=net_n,
                stubs_override=(by_bit_a[bit], by_bit_b[bit]),
                # The exemption is for the perpendicular-gated stub doglegs
                # ONLY: the A* body sees every pad -- including the pair's
                # own -- as an obstacle, else the shortest fat path hugs an
                # endpoint pad row and the emitted legs land on the
                # partner's copper (board-06 MIPI lesson).
                exempt_pads=frozenset(),
            )
            if result is None:
                reason = (
                    search_reason if search_reason.startswith("pair-") else f"pair-{search_reason}"
                )
                continue
            pres, finish_reason = finish(result)
            if pres is not None:
                return pres, "ok"
            reason = finish_reason
        return None, reason

    # -- multi-net negotiation ------------------------------------------------

    def _self_drc_reason(self, result: _RouteResult) -> str | None:
        """Final self-DRC gate over ONE net's freshly emitted geometry (#4318).

        The per-move A* predicates (:meth:`CommittedCopper.seg_clear` /
        :meth:`via_clear`) validate each new segment / via against the
        *committed* model -- copper that is already placed, and (within a pass)
        almost always foreign-net.  A net's own via and its own octilinear
        segments, however, are emitted together and are never cross-checked
        against one another: nothing rejects a segment whose body runs across
        THIS net's own via center.  That is a 0.000mm segment-to-via
        coincidence -- malformed copper the downstream ``kct check`` DRC cannot
        nudge away (and, being same-net, would not even surface, yet is still a
        manufacturing defect).

        Reuse the same body-vs-endpoint predicate the tightened committed-copper
        model uses (:func:`seg_body_crosses_pt`) so the router's accept gate and
        the checker agree by construction.  Returns a decline reason string when
        the route is malformed (flowing into the lattice ``decline[...]``
        census), or ``None`` when it is clean.  The legitimate in-pad-escape
        endpoint-at-via-center invariant (#2706) is exempt by construction.
        """
        if not result.via_points:
            return None
        for _layer_idx, points, _widths in result.runs:
            for a, b in zip(points, points[1:], strict=False):
                if dist(a, b) <= 1e-9:
                    continue
                for via_pt in result.via_points:
                    if seg_body_crosses_pt(a, b, via_pt):
                        return "self-drc-segment-via-coincident"
        return None

    def route_netset(
        self,
        connections: list[Connection],
        *,
        coupled: list[CoupledConnection] | None = None,
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

        Diff pairs (issue #4270): ``coupled`` carries
        :class:`~kicad_tools.router.lattice.coupled.CoupledConnection`
        entries, each negotiated as ONE fat centerline agent, all pairs
        before all singles (shortest-first within each group).  A successful pair contributes two
        routes keyed ``(pc.key, "P")`` / ``(pc.key, "N")``; both legs commit
        under their own net ids so every other net sees them at full copper
        gap while the pair's internal gap stays by-construction.  Per-pair
        outcomes ("coupled" or a decline reason) land in
        :attr:`pair_outcomes` for the best pass.

        The lattice is built **once** (:attr:`lattice_builds` == 1 across the
        whole negotiation); rip-up resets only the committed-copper model.
        Returns ``(routes_by_key, stats)`` for the best pass seen;
        :attr:`failure_reasons` carries the per-connection decline diagnosis
        for that pass.
        """
        self.build()
        pairs = list(coupled or [])

        # Interleave singles and pairs shortest-first (pair length = the
        # midpoint-to-midpoint distance of its main run).  The sort is
        # stable, so with no pairs the ordering is exactly the pre-#4270 one.
        items: list[tuple[float, int, Any]] = [
            (math.hypot(c[1].x - c[2].x, c[1].y - c[2].y), 0, c) for c in connections
        ]
        items.extend((pc.length, 1, pc) for pc in pairs)
        # Pairs first (kind 1 before kind 0), each group shortest-first: the
        # fat coupled agents are by far the most constrained, and a pair's
        # own extra-pad singles must never pre-block its corridor.  With no
        # pairs this is exactly the pre-#4270 shortest-first ordering.
        items.sort(key=lambda t: (-t[1], t[0]))

        history: dict[Resource, float] = {}
        best_routes: dict[object, Route] = {}
        best_reasons: dict[object, str] = {}
        best_pair_outcomes: dict[object, str] = {}
        best_count = -1
        converged = False
        iterations_run = 0
        present = present_cost_initial

        for it in range(max_iterations):
            iterations_run = it + 1
            committed = self._fresh_committed()
            routes: dict[object, Route] = {}
            reasons: dict[object, str] = {}
            pair_outcomes: dict[object, str] = {}
            routed_items = 0
            failed: list[tuple[int, Any]] = []
            for _length, kind, item in items:
                if kind == 1:
                    pres, preason = self._route_pair_impl(
                        item, committed=committed, history=history, present=present
                    )
                    if pres is None:
                        reasons[item.key] = preason
                        pair_outcomes[item.key] = preason
                        failed.append((kind, item))
                        continue
                    pair_outcomes[item.key] = "coupled"
                    routed_items += 1
                    routes[(item.key, "P")] = pres.routes[0]
                    routes[(item.key, "N")] = pres.routes[1]
                    # Legs are EMITTED at the pair class width (#4270), so
                    # commit them at the same width + clearance (#4271) --
                    # emission and spacing must agree.
                    half, clr = self._conn_geometry(item.net_class)
                    for leg_net, layer_idx, points in pres.runs:
                        committed.add_run(layer_idx, points, leg_net, half, clr)
                    continue
                key, start, end, net_class = item
                result, reason = self._route_impl(
                    start, end, net_class, committed=committed, history=history, present=present
                )
                if result is None:
                    reasons[key] = reason
                    failed.append((kind, item))
                    continue
                # Final self-DRC gate (#4318): a route whose own segment body
                # runs across its own via center is malformed copper the
                # checker cannot repair -- decline it honestly rather than
                # ship a 0.000mm segment-to-via coincidence.
                self_reason = self._self_drc_reason(result)
                if self_reason is not None:
                    reasons[key] = self_reason
                    failed.append((kind, item))
                    continue
                routed_items += 1
                routes[key] = result.route
                # Commit each segment at the width it was EMITTED at (issue
                # #4293): the widened lattice body is spaced against 2.6 mm
                # copper as 2.6 mm copper, and a tapered escape neck is spaced
                # honestly as neck copper -- never the taper cheating the
                # spacing model.  Clearance stays the class clearance (#4271).
                _half, clr = self._conn_geometry(net_class)
                for layer_idx, points, widths in result.runs:
                    committed.add_run_widths(
                        layer_idx, points, start.net, [w / 2.0 for w in widths], clr
                    )
                for via_pt in result.via_points:
                    committed.add_via(via_pt, start.net)

            if routed_items > best_count:
                best_count = routed_items
                best_routes = routes
                best_reasons = reasons
                best_pair_outcomes = pair_outcomes

            if not failed:
                converged = True
                break
            if it == max_iterations - 1:
                break

            # Failed-net demand: bump history on the resources each blocked
            # net WANTS (its corridor with no committed copper in the way) so
            # the occupying nets are pressured to detour next pass.
            increment = history_increment_factor * self.rules.cost_congestion * present
            for kind, item in failed:
                if kind == 1:
                    desired_pair, _r = self._route_pair_impl(
                        item, committed=self._fresh_committed(), history=history, present=present
                    )
                    desired_resources = desired_pair.resources if desired_pair is not None else None
                else:
                    key, start, end, net_class = item
                    desired, _reason = self._route_impl(
                        start,
                        end,
                        net_class,
                        committed=self._fresh_committed(),
                        history=history,
                        present=present,
                    )
                    desired_resources = desired.resources if desired is not None else None
                if desired_resources is None:
                    continue
                for resource in desired_resources:
                    history[resource] = history.get(resource, 0.0) + increment
            present *= present_cost_growth

        self.failure_reasons = best_reasons
        self.pair_outcomes = best_pair_outcomes
        stats = LatticeNegotiationStats(
            iterations=iterations_run,
            converged=converged,
            routed=best_count if best_count >= 0 else 0,
            total=len(connections) + len(pairs),
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
