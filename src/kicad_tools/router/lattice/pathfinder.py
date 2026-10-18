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
mutates committed-copper state and history costs -- never the lattice.  The
one exception (issue #6489): when the class-pad repair leaves a shortfall,
:meth:`route_netset` grows exactly those pads and rebuilds for a bounded
re-negotiation; with nothing left it never rebuilds.

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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..layers import LayerStack
from ..pairplan.gateway import Gateway, GatewaySite
from ..pairplan.gateway import gateway_anchor_point as _gateway_anchor
from ..pairplan.outcome import (
    EXHAUSTED_SEARCH_REASONS,  # noqa: F401 -- re-exported (core, tests)
    GATEWAY_RETRY_REASONS,
    REASON_NO_PATH,
    REASON_NO_PATH_KEEPOUT,
    REASON_NO_PATH_LAYER,
    SegmentOutcome,
    SegmentOutcomes,
    hybrid_retry_reason,
)
from ..primitives import Pad, Route, Segment, Via, pad_half_extents
from ..quantize import dogleg_points
from ..rules import DesignRules, layer_permitted
from .coupled import CoupledConnection
from .escape_plan import EscapeChoice, chosen_stub, foreign_reservations, plan_tapered_escapes
from .geometry import Pt, Rect, dist, pt_in_rect, seg_seg_dist
from .guide import PartnerGuide
from .history import PhysicalHistory
from .kernel_adapter import LATTICE_CLEARANCE_EPSILON_MM
from .obstacles import (
    CommittedCopper,
    LatticeKeepoutMask,
    LatticeObstacleModel,
    seg_body_crosses_pt,
)
from .quadtree import EdgeKey, NodeKey, OctilinearLattice, RefineRegion

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .class_pad_repair import ClassPadRepairStats
    from .pairwise import LatticePairwise

# A negotiation connection: (opaque hashable key, start pad, end pad, net_class)
# -- the same shape as ``mesh.pathfinder.Connection``.
Connection = tuple[object, Pad, Pad, object]

# A congestion resource: ("e", edge_key, layer) or ("v", node_key).
Resource = tuple[object, ...]

# A* state: (node_key, layer_index).
_State = tuple[NodeKey, int]
_GOAL: _State = (("GOAL",), -1)  # type: ignore[assignment]

# Issue #6957: a path's via sites as a persistent linked list, newest first.
_ViaChain = tuple[Pt, Any] | None

# Escape-fan size for the oversize neck-down retry (issue #4293): a thin neck
# escapes the pad, but the WIDENED body must then egress at full width, so the
# useful escape node often sits at the edge of the congested field rather than
# hugging the pad.  A nearest-first count cap fills up on dead-end near nodes
# and never reaches that egress node, so the neck retry collects a large
# radius-bounded fan instead (the count-capped-in-a-pocket failure the pair
# path also avoids).  A search-tuning knob, not a geometric constant -- it only
# widens the candidate set, never the copper.
_OVERSIZE_STUB_KMAX = 4096

# Decline reasons ``_route_impl`` returns when its A* actually RAN and was
# exhausted (issues #6373, #4605, #4979), and the pair-segment reasons that
# earn one fan-via gateway retry (issue #6444).  Both now live with the rest
# of the engine-free outcome vocabulary in
# :mod:`kicad_tools.router.pairplan.outcome` (issue #6837) and are imported
# above, so their old names here keep working.  Tests monkeypatch
# ``GATEWAY_RETRY_REASONS`` on THIS module, which works because this module
# reads its own global.

#: Issue #6444: net id the fat pair search sees a gateway's own vias under
#: (``_route_pair_impl(keep_clear=...)``) -- never a real net.
_KEEP_CLEAR_NET = -6444

#: Issue #6489: most re-negotiations with grown pads after a class-pad repair.
_MAX_PAD_GROWTH_ROUNDS = 2

# Issue #6703: partner-corridor cost for uncoupled diff-pair fallback legs
# (see :class:`~kicad_tools.router.lattice.guide.PartnerGuide`).  A guided leg
# routes free within ``_GUIDE_NEAR_PITCHES`` pair pitches of its partner on
# the same layer, and pays up to ``_GUIDE_WEIGHT * _GUIDE_CAP_MM`` extra per mm
# of length beyond that.
_GUIDE_NEAR_PITCHES = 2.0
_GUIDE_CAP_MM = 2.0
_GUIDE_WEIGHT = 1.0
# Issue #6824: extra cost per mm of planar copper on a layer outside the net
# class's ``preferred_layers`` (1.0 => non-preferred copper costs 2x, the
# grid pathfinder's 0.5x preferred discount as an admissible surcharge).
_NON_PREFERRED_LAYER_SURCHARGE = 1.0


# Issue #6703: at most this many fallback pairs per flush get the partner-aware
# candidates and their partner-blind rollout of the later pairs; the rest keep
# the pre-#6703 partner-blind legs.  Each such pair costs about six leg
# searches plus two per later pair, so the cap keeps one flush LINEAR in the
# number of fallback pairs (``2n + K * (6 + 2n)`` searches, against ``2n``
# before) instead of quadratic, and the flush runs on every negotiation pass.
_MAX_PARTNER_AWARE_PAIRS = 4

# Issue #6703 (PR #6735 review): a partner-aware leg's clearance to EVERY
# obstacle is raised to a stricter pad class only when that class is at most
# this many times the leg's own clearance; stricter (outlier) pad classes are
# kept clear per pad.  See ``LatticePathfinder._strict_pad_pairwise``.
_SEARCH_FLOOR_MAX_FACTOR = 2.0

# Issue #6703 (PR #6735 review): least summed-skew reduction (mm) for which a
# fallback flush keeps a pair's partner-aware legs over its partner-blind
# ones at the same routed-leg count (and no extra class-pad shortfall).
_MIN_FLUSH_SKEW_GAIN_MM = 0.25

# One fallback leg pair: ``{"P"|"N": (search result or None, reason)}`` (#6703).
_FallbackLegs = dict[str, tuple["_RouteResult | None", str]]

# Through-via barrel length assumed when the router supplies no
# :attr:`LatticePathfinder.pair_length_measure` (a standard 1.6 mm board).
_DEFAULT_VIA_LENGTH_MM = 1.6


def _default_pair_length(route: Route) -> float:
    """Track length plus one board thickness per (through) via, in mm."""
    tracks = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)
    return tracks + _DEFAULT_VIA_LENGTH_MM * len(route.vias)


@dataclass(frozen=True)
class PadGrowthRound:
    """One #6489 grown-keep-out re-negotiation attempt, for the log (issue #6670).

    ``grown`` pads were added by this attempt.  ``footprints`` names the parts
    whose whole class-floored pad set was grown because a shortfall moved to a
    sibling pad (empty for the targeted growth), see
    :meth:`LatticePathfinder.route_netset`.  ``kept`` is whether the attempt
    replaced the previous result; a rejected attempt is rolled back.
    """

    grown: int
    footprints: tuple[str, ...]
    routed_before: int
    routed_after: int
    left_before: int
    left_after: int
    kept: bool


@dataclass(frozen=True)
class LatticeNegotiationStats:
    """Outcome of a :meth:`LatticePathfinder.route_netset` negotiation run."""

    iterations: int
    converged: bool
    routed: int
    total: int
    lattice_builds: int
    # Issue #4477 (epic #4465, Phase 4): bounded per-link termination
    # reporting.  ``deadline_hit`` is True when ``deadline`` (issue #4472) cut
    # the negotiation short before every connection was attempted;
    # ``elapsed_s`` is the wall-clock the WHOLE negotiation took;
    # ``budget_s`` is the budget the caller granted (``None`` when the
    # negotiation ran unbudgeted, preserving the pre-#4472 behaviour).
    deadline_hit: bool = False
    elapsed_s: float = 0.0
    budget_s: float | None = None

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
    # Issue #6444: ``(net, site)`` of every fan via of a gateway-coupled pair
    # (empty for a planar pair), committed with the legs.
    vias: list[tuple[int, Pt]] = field(default_factory=list)
    # Issue #6444: log line naming the gateway (``None`` for a planar pair).
    gateway: str | None = None


#: Bounded retries of a pair search after a ``pair-partner-pad`` decline (#6839).
_PARTNER_PAD_RETRIES = 4

#: Issue #6975: in a parity-filtered pair search, a lattice step and the end
#: stub it joins "reverse" when the cosine of their angle is below this --
#: i.e. the path doubles back along (nearly) the same line.  Only a straight
#: 180-degree reversal is caught; a 90- or 135-degree turn keeps the stub's
#: travel direction (and therefore its parity) and is left to the offset.
_END_REVERSAL_COS = -1.0 + 1e-6


def _unit_step(a: Pt, b: Pt) -> Pt | None:
    """Unit vector ``a -> b``, or ``None`` for a zero-length step."""
    length = dist(a, b)
    if length <= 1e-9:
        return None
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _reverses(d_in: Pt | None, d_out: Pt | None) -> bool:
    """True when travelling ``d_in`` then ``d_out`` doubles back (#6975)."""
    if d_in is None or d_out is None:
        return False
    return d_in[0] * d_out[0] + d_in[1] * d_out[1] < _END_REVERSAL_COS


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


def _gateway_decline_summary(
    tally: dict[str, dict[str, int]], blockers: dict[str, int], *, top: int = 3
) -> str:
    """Per-group decline counts for a gateway decline line (issue #6956).

    ``tally`` maps a combination group (``against``, ``pass-under``) to
    ``{reason: count}``; ``blockers`` counts each ``pad-attach`` blocker.
    Board 03's line used to name only the LAST combination's blocker (the
    GND U2.2 site) while 28 of its attaches had hit the VBUS U2.5 escape
    site.  Returns ``"tried: against pad-attach x16, ...; attach blockers:
    ..."`` (most frequent first), or ``""`` when nothing was tried.
    """
    parts: list[str] = []
    for group, reasons in tally.items():
        ranked = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        parts.append(f"{group} " + ", ".join(f"{why} x{n}" for why, n in ranked))
    if not parts:
        return ""
    text = "tried: " + "; ".join(parts)
    if blockers:
        ranked_b = sorted(blockers.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        text += "; attach blockers: " + ", ".join(f"{b} x{n}" for b, n in ranked_b)
    return text


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
        # Issue #6395: each pad's own net-class clearance, aligned with
        # ``pads`` and installed by the CALLER from its net-class map through
        # :meth:`set_pad_class_clearances` (the pathfinder stays geometry-only,
        # #4597).  It is the PAD side of every track-vs-pad pair,
        # ``max(class(track), class(pad))``, enforced on the negotiated copper
        # by :mod:`.class_pad_repair`.  ``None`` (the default) leaves the
        # routed output byte-identical.
        self.pad_class_clearances: list[float] | None = None
        self.class_pad_repair_stats: ClassPadRepairStats | None = None
        # Real copper stack (issue #4278 acceptance 6): the lattice is
        # replicated across the board's actual routing-layer count -- never
        # hardcoded to 2.
        self.layer_stack = layer_stack or LayerStack.two_layer()
        self.coarse = coarse
        self.fine = fine
        self.margin = margin
        # Per-via-hop layer-change cost (mm-equivalent; spike default 3.0).
        self.via_cost = via_cost
        # Issue #6703: ``Route -> mm`` used to rank the uncoupled-fallback leg
        # candidates by skew.  The router sets the skew checker's via-inclusive
        # measure; ``None`` -> tracks plus a 1.6 mm barrel per through-via.
        self.pair_length_measure: Callable[[Route], float] | None = None
        self._shortfall_memo: dict[int, tuple[_RouteResult, int]] = {}
        # Issue #6703: raised only while a partner-aware fallback leg is
        # SEARCHED (never while it is committed) -- see
        # :meth:`_strict_pad_pairwise`.
        self._search_clearance_floor = 0.0

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
        # Issue #6489: pad index -> class clearance, for pads whose keep-out
        # grows because the class-pad repair could not clear them.
        self._class_pad_growth: dict[int, float] = {}
        # Issue #6670: what each growth round did (kept or rolled back).
        self.class_pad_growth_rounds: list[PadGrowthRound] = []
        # Per-connection decline reasons for the best negotiation pass
        # (honest shortfall diagnosis; reset by :meth:`route_netset`).
        self.failure_reasons: dict[object, str] = {}
        # Per-diff-pair outcome for the best pass: "coupled" or a decline
        # reason, keyed by ``CoupledConnection.key`` (issue #4270).  A
        # ``dict`` of those strings that also keeps the structured
        # :class:`SegmentOutcome` behind each one (issue #6837).
        self.pair_outcomes: dict[object, str] = SegmentOutcomes()
        # Issue #6444: fan-via gateway retry state.  ``pair_gateway_limits``
        # maps a ``CoupledConnection.key`` to the protocol's
        # ``(max uncoupled mm per terminal, max vias per net)`` (either may be
        # ``None``), installed by the caller -- the pathfinder never resolves
        # protocols itself.  ``pair_gateway_log`` holds the latest gateway
        # diagnostic per key; ``pair_gateways`` names the gateway of every
        # pair the BEST pass coupled through one.
        self.pair_gateway_limits: dict[object, tuple[float | None, int | None]] = {}
        self.pair_gateway_log: dict[object, str] = {}
        self.pair_gateways: dict[object, str] = {}
        # Keys the caller excludes from the gateway retry (a trunk the #6535
        # tap-chain retry handles first).
        self.pair_gateway_skip: frozenset[object] = frozenset()
        # Log line when :meth:`route_netset` re-negotiated without gateways.
        self.pair_gateway_rollback: str | None = None
        # Issue #6446: per-end gateway verdict of the latest gateway retry,
        # ``key -> (legal_a, legal_b)``.  ``None`` = a pad of that end owns a
        # gateway layer (no gateway needed); an int is the most legal gateway
        # sites found at that end on any layer tried.  ``pair_hybrid_log``
        # holds the latest hybrid-retry diagnostic per key.
        self.pair_gateway_ends: dict[object, tuple[int | None, int | None]] = {}
        self.pair_hybrid_log: dict[object, str] = {}
        # Issue #4355: immovable copper from NON-listed nets (the preserved
        # ``router.existing_routes``), pre-processed into per-segment seed
        # tuples ``(layer_idx, a, b, net, half_width, clearance)`` plus via
        # sites ``(point, net, clearance)``.  Every fresh CommittedCopper model
        # is pre-seeded with these so a negotiated net spaces against the fixed
        # copper as a HARD geometric block -- routes around it or is honestly
        # reported unroutable, never shipped as a short.  Empty (the default)
        # preserves the pre-#4355 behavior byte-for-byte.
        #
        # Issue #4597: the per-seed CLEARANCE is the preserved net's own
        # net-class clearance (resolved by the caller and handed to
        # :meth:`route_netset` as ``fixed_clearance``), floored at
        # ``rules.trace_clearance``.  Before this the seed hardcoded the
        # board-global floor, so a multi-step ``--preserve-existing``
        # composition silently spaced new copper at the DRU floor from an HV
        # net the net-class map put at 2.0-3.2 mm.
        self._fixed_runs: list[tuple[int, Pt, Pt, int, float, float]] = []
        self._fixed_vias: list[tuple[Pt, int, float, float, tuple[int, ...]]] = []
        # Issue #4602: optional net-id-space HV pairwise projection
        # (:class:`.pairwise.LatticePairwise`), resolved by the CALLER from
        # ``rules.pairwise_clearance`` + the #4506 attach zones -- the
        # pathfinder stays geometry-only and never sees net names (#4597
        # discipline).  ``None`` (the default, and always the case without
        # ``--voltage-map``) keeps every predicate on its scalar path
        # byte-for-byte.
        self._pairwise: LatticePairwise | None = None
        # Issue #4605: optional static keepout rule-area mask
        # (:class:`.obstacles.LatticeKeepoutMask`), resolved by the CALLER
        # from the board's ``(zone ... (keepout ...))`` rule areas + the
        # ``spatial_keepouts`` sidecar filter -- the pathfinder stays
        # geometry-only and never sees zone/net/class names.  ``None`` (the
        # default, and always the case on a board with no track/via-blocking
        # rule areas) keeps every predicate on its pre-#4605 path
        # byte-for-byte.
        self._keepouts: LatticeKeepoutMask | None = None
        # Issue #6935: why the last ``_pair_attach`` found no dogleg.
        self._attach_blocker = ""
        from ..fixed_copper import FixedFillObstacles

        self.fixed_fills = FixedFillObstacles()
        self.set_escape_boundary(None, 0.0)

    def set_escape_boundary(self, edges: list[tuple[Pt, Pt]] | None, clearance: float) -> None:
        """Install actual Edge.Cuts and its clearance floor for all routed copper."""
        from .escape_boundary import EscapeBoundary

        if edges is None:
            edges = list(zip(self.outline, self.outline[1:] + self.outline[:1], strict=True))
        self._escape_boundary = EscapeBoundary(edges, clearance)

    def set_pairwise(self, pairwise: LatticePairwise | None) -> None:
        """Install (or clear) the id-space HV pairwise projection (#4602).

        Threaded into every fresh :class:`CommittedCopper` model and every
        static pad keep-out probe.  Passing ``None`` resets a reused
        pathfinder so it cannot carry a stale projection across runs.
        """
        self._pairwise = pairwise

    def set_pad_class_clearances(self, values: list[float] | None) -> None:
        """Install (or clear) the per-pad net-class clearances (#6395).

        ``values`` aligns with :attr:`pads`.  The search model does not read
        them -- see :mod:`.class_pad_repair` for why the pair is enforced on
        the negotiated copper instead -- so this never rebuilds the lattice.
        """
        if values is not None and len(values) != len(self.pads):
            raise ValueError("pad_class_clearances must align with pads")
        self.pad_class_clearances = list(values) if values is not None else None

    def set_keepouts(self, keepouts: LatticeKeepoutMask | None) -> None:
        """Install (or clear) the keepout rule-area mask (#4605).

        Consulted by the same three predicates every search expansion already
        routes through (node / edge / free-segment legality) plus the via
        gate -- no new call sites in the A* loop.  Passing ``None`` (or an
        empty mask, normalised to ``None``) resets a reused pathfinder so it
        cannot carry stale rule areas across runs.
        """
        self._keepouts = keepouts if keepouts else None

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
        # This constructor still represents the board as a bounding box.
        # A certified chord chain does not make that box equivalent to a
        # curved outer boundary or an interior cutout. Keep the formerly
        # unsupported geometry explicit rather than silently losing it.
        if getattr(segments, "max_error_mm", 0.0) > 0:
            raise ValueError(
                "Unsupported curved Edge.Cuts in this bounding box constructor; "
                "use the grid router to preserve the curved boundary"
            )
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
            pad_extra=self._authored_pad_extra(),
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
            half_w, half_h = pad_half_extents(pad)
            hx = half_w + self.margin
            hy = half_h + self.margin
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
        committed = CommittedCopper(
            self.num_layers,
            trace_half=self._trace_half,
            clearance=self.rules.trace_clearance,
            via_radius=self.rules.via_diameter / 2.0,
            via_via_gap=self._via_via_gap,
            same_net_via_gap=self._same_net_via_gap,
            pairwise=self._pairwise,
        )
        # Issue #4355: pre-seed immovable non-listed copper so EVERY fresh
        # model (per-pass negotiation, isolated stub/pair probes, and the
        # desired-corridor history bumps) spaces against it consistently.  The
        # seed set is empty unless ``route_netset`` was given ``fixed_copper``,
        # so this is a no-op on the pre-#4355 paths.
        self._seed_fixed_copper(committed)
        committed.fixed_fills = self.fixed_fills
        committed.fixed_fill_via_clearance = self.rules.via_clearance
        return committed

    def _seed_fixed_copper(self, committed: CommittedCopper) -> None:
        """Commit the preserved non-listed copper into ``committed`` (#4355).

        Each preserved segment is committed under its OWN net id at its emitted
        half-width and its OWN net-class clearance (issue #4597) -- a hard
        geometric block that ``route_netset`` declines to cross (never shipped
        as a short).  Same-net exemption in :meth:`CommittedCopper.seg_clear`
        means seeding a listed net's own stale copper (if any slipped into the
        fixed set) cannot block that net's fresh route.  Each preserved via
        seeds ``committed.vias`` at that same clearance (a through-via blocks
        every layer).

        Nets with no resolved class clearance seed at ``rules.trace_clearance``
        exactly as before #4597.
        """
        for layer_idx, a, b, net, half, clr in self._fixed_runs:
            committed.add_run(layer_idx, [a, b], net, half, clr)
        for point, net, clr, radius, layers in self._fixed_vias:
            committed.add_via(point, net, clr, radius=radius, layers=layers)

    def _authored_pad_extra(self) -> list[float] | None:
        """Per-pad keep-out growth for authored netclass minima (Issue #6243).

        A pad whose net (or the pad itself, when neutralised) carries an
        authored minimum above the board-global clearance grows its static
        keep-out by the difference, so every lattice pad predicate -- node /
        edge masks, free-segment and via checks, the coupled fat agent --
        keeps that pad's own side of the pair.  ``None`` (no floors) leaves
        the model byte-identical.

        Issue #6449: only DESIGNER-authored minima grow a pad mask.  The
        router's own class clearances also live in ``net_clearance_floors``
        since #6427, but the pad side of a router-class pair is enforced on
        the negotiated copper by :mod:`.class_pad_repair` (#6395): growing the
        masks by it moves tracks by whole lattice rows and stranded board 05's
        ``GND_SENSE`` at ``U2.6`` (32/33).  The loader records the designer
        subset as ``rules.designer_clearance_floors``; rules built elsewhere
        (``None``) treat every floor as designer-authored, as before.
        """
        extra = self._grown_pad_extra()
        if extra is None:
            return self._designer_pad_extra()
        designer_extra = self._designer_pad_extra()
        if designer_extra is None:
            return extra
        return [max(a, b) for a, b in zip(extra, designer_extra, strict=True)]

    def _grown_pad_extra(self) -> list[float] | None:
        """Issue #6489: keep-out growth for pads the repair could not clear."""
        grown = self._class_pad_growth
        if not grown:
            return None
        base = self.rules.trace_clearance
        return [max(0.0, grown.get(i, 0.0) - base) for i in range(len(self.pads))]

    def _designer_pad_extra(self) -> list[float] | None:
        from ..authored_clearance import pad_authored_floor

        base = self.rules.trace_clearance
        designer = self.rules.designer_clearance_floors
        if designer is None:
            floors = self.rules.net_clearance_floors
            if not floors and not any(p.authored_clearance > 0.0 for p in self.pads):
                return None
            return [max(0.0, pad_authored_floor(p, floors) - base) for p in self.pads]
        by_name = self.rules.designer_clearance_floors_by_name
        values = [
            max(
                designer.get(p.net, 0.0),
                # A neutralised pad (net 0) keeps its designer minimum by name,
                # exactly what the #6243 loader stored as ``authored_clearance``.
                by_name.get(p.net_name or "", 0.0) if p.net == 0 else 0.0,
            )
            for p in self.pads
        ]
        if not any(v > 0.0 for v in values):
            return None
        return [max(0.0, v - base) for v in values]

    def _fixed_clearance_for(
        self, net: int, clearances: dict[int, float] | None, item_floor: float = 0.0
    ) -> float:
        """Seed clearance for preserved net ``net`` (issue #4597).

        Same floor semantics as :meth:`_conn_geometry`: a class may only GROW
        the gap, never shrink it below ``rules.trace_clearance``.  An unmapped
        net (or ``clearances is None``) resolves to the board-global floor --
        the pre-#4597 behavior, byte-for-byte.  Issue #6243: the net's
        authored minimum (or the copper's own, ``item_floor``) also raises it.
        """
        authored = max(self.rules.authored_floor(net), item_floor)
        if not clearances:
            return max(self.rules.trace_clearance, authored)
        return max(clearances.get(net) or 0.0, self.rules.trace_clearance, authored)

    def _set_fixed_copper(
        self,
        routes: list[Route] | None,
        clearances: dict[int, float] | None = None,
    ) -> None:
        """Pre-process preserved ``Route``s into flat seed tuples (#4355).

        Called once by :meth:`route_netset`; maps each segment's layer enum to
        a lattice layer index (dropping copper on non-routing layers) and each
        via to a site.  ``None``/empty resets the seed set so a reused
        pathfinder does not carry stale fixed copper.

        ``clearances`` (issue #4597) is a plain ``{net_id: clearance}`` map the
        CALLER resolved from its net-class map -- the pathfinder stays
        geometry-only and never sees net names or net classes.
        """
        runs: list[tuple[int, Pt, Pt, int, float, float]] = []
        vias: list[tuple[Pt, int, float, float, tuple[int, ...]]] = []
        for route in routes or []:
            item_floor = float(getattr(route, "authored_clearance", 0.0) or 0.0)
            for seg in getattr(route, "segments", []):
                try:
                    layer_idx = self.layer_stack.layer_enum_to_index(seg.layer)
                except Exception:
                    # Copper on a layer outside this routing stack cannot
                    # collide with a lattice trace; skip it silently.
                    continue
                if not (0 <= layer_idx < self.num_layers):
                    continue
                a = (seg.x1, seg.y1)
                b = (seg.x2, seg.y2)
                if dist(a, b) <= 1e-9:
                    continue
                half = (getattr(seg, "width", None) or self.rules.trace_width) / 2.0
                runs.append(
                    (
                        layer_idx,
                        a,
                        b,
                        seg.net,
                        half,
                        self._fixed_clearance_for(seg.net, clearances, item_floor),
                    )
                )
            for via in getattr(route, "vias", []):
                # A routing stack may select only part of the physical board
                # (for example F.Cu only). Imported through-via endpoints need
                # not themselves be present in that selected stack.
                first, last = sorted(
                    layer.stack_order for layer in getattr(via, "drilled_layers", via.layers)
                )
                occupied_layers = tuple(
                    layer.index
                    for layer in self.layer_stack.layers
                    if first <= layer.layer_enum.stack_order <= last
                )
                vias.append(
                    (
                        (via.x, via.y),
                        via.net,
                        self._fixed_clearance_for(via.net, clearances, item_floor),
                        via.diameter / 2,
                        occupied_layers,
                    )
                )
        self._fixed_runs = runs
        self._fixed_vias = vias

    def _conn_geometry(
        self, net_class: object | None, net: int | None = None
    ) -> tuple[float, float]:
        """Per-connection ``(trace half-width, clearance)`` (issue #4271).

        Width follows the class exactly as :meth:`_emit` does (the copper is
        spaced at the SAME width it is emitted at -- the #4271 invariant);
        clearance takes ``max(class, rules)`` so a class can only GROW the
        gap, never shrink below the design rules.  ``None`` -> global
        geometry, preserving the single-width pre-#4271 behavior exactly.

        Issue #6243: ``net``'s authored netclass minimum is the connection's
        own side of every pair, so it raises the clearance too -- and, carried
        into committed copper, it is the stored side for every later net.
        """
        tw = getattr(net_class, "trace_width", None) or self.rules.trace_width
        clr = getattr(net_class, "clearance", None) or 0.0
        authored = self.rules.authored_floor(net) if net is not None else 0.0
        return tw / 2.0, max(
            clr, self.rules.trace_clearance, authored, self._search_clearance_floor
        )

    def _preferred_layers(self, net_class: object | None) -> frozenset[int]:
        """Grid-layer indices this net's class prefers (Issue #6824).

        Soft counterpart of :meth:`_hard_avoided_layers`: the class's
        ``preferred_layers`` restricted to layers that exist on this stack and
        are not hard-blocked for the net (a preference for an unreachable
        layer can never be honoured, so it is dropped rather than penalising
        every reachable layer).  Empty when the class declares none.
        """
        raw = getattr(net_class, "preferred_layers", None)
        if not raw:
            return frozenset()
        blocked = self._hard_avoided_layers(net_class)
        return frozenset(
            int(i)
            for i in raw
            if isinstance(i, int) and 0 <= i < self.num_layers and i not in blocked
        )

    def _hard_avoided_layers(self, net_class: object | None) -> frozenset[int]:
        """Grid-layer indices to HARD-block for this connection (issue #4979).

        Mirrors the grid pathfinder's ``_hard_avoided_layers``
        (``router/pathfinder.py``) and the C++ backend's identical gate
        (``router/cpp_backend.py``): both drive
        :meth:`NetClassRouting.hard_avoided_layer_indices` off
        ``DesignRules.strict_layers`` / ``target_ampacity``, so a net whose
        class declares ``avoid_layers`` is hard-blocked from those layers
        under ``--strict-layers`` (or unconditionally when the class also
        declares ``target_ampacity``) on every engine -- not just grid/cpp.

        Before issue #4979 the lattice engine (``--route-engine lattice``,
        the ``--complete`` default) never called this accessor at all: the
        A* search here only ever read ``net_class.trace_width`` /
        ``.clearance`` (issue #4271) for copper geometry, so ``avoid_layers``
        was silently inert regardless of ``--strict-layers`` -- the search
        could (and did) land a hard-current net on an explicitly forbidden
        inner plane with no failure reported.

        Issue #6350: the board-wide ``DesignRules.allowed_layers`` gate is
        folded in too (see :meth:`_allowed_layers_blocked`).  That is the
        field ``--reserve-plane-layers`` (the ``--layers 4`` default) sets to
        keep signal copper off the reference planes; the grid pathfinder and
        the C++ backend enforce it at search time, but before #6350 the
        lattice never read it, so ``--route-engine lattice`` routed signal
        nets on In1.Cu/In2.Cu and ``--no-reserve-plane-layers`` was a no-op.

        Returns an empty set when ``rules.allowed_layers`` is ``None`` and
        ``net_class`` is ``None`` or declares no hard-avoided layers,
        preserving byte-identical routing for every connection that does not
        opt in.
        """
        blocked = self._allowed_layers_blocked(net_class)
        if net_class is None:
            return blocked
        hard_avoided = getattr(net_class, "hard_avoided_layer_indices", None)
        if hard_avoided is None:
            return blocked
        return blocked | frozenset(hard_avoided(self.rules.strict_layers))

    def _allowed_layers_blocked(self, net_class: object | None = None) -> frozenset[int]:
        """Layer indices outside ``rules.allowed_layers`` (issue #6350).

        Issue #6876: a layer ``net_class`` opted in to through
        ``allow_plane_layers`` is not blocked (shared
        :func:`~kicad_tools.router.rules.layer_permitted`).  Hard
        ``avoid_layers`` is applied separately by :meth:`_hard_avoided_layers`
        and still wins.

        ``allowed_layers`` holds KiCad layer names (``["F.Cu", "B.Cu"]`` under
        ``--layers 4`` with plane reservation on); every stack layer whose
        name is absent is hard-blocked for every net, exactly as the grid
        pathfinder's ``_is_layer_allowed`` does.  ``None`` (reservation off,
        ``--layers 4-all``, 2-layer boards) blocks nothing.

        Read at CALL time, never cached: ``route_with_layer_escalation``
        mutates the shared ``rules`` in place between rungs, and this
        pathfinder can outlive a rung.

        Blocked inner planes do not strand F.Cu<->B.Cu nets: the via step-over
        in :meth:`_route_search` hops across them.  A stack whose reserved
        plane were an OUTER layer would instead trip the via-span guard there
        (this engine only emits full-stack through vias) and route planar or
        decline as layer-constrained -- never ship copper on the plane.  None
        of the built-in stacks does that (``four_layer_sig_sig_gnd_pwr``'s
        B.Cu is ``MIXED``, so only its In2.Cu is reserved).
        """
        allowed = self.rules.allowed_layers
        if allowed is None:
            return frozenset()
        return frozenset(
            ld.index
            for ld in self.layer_stack.layers
            if not layer_permitted(self.rules, net_class, ld.index, ld.name)
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
        half, clr = self._conn_geometry(net_class, net)
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
        # Issue #4602: non-fat scans additionally honour the per-PAIR HV pad
        # keep-outs (the fat coupled path is excluded from pairwise widening
        # in this slice -- see ``coupled.committed_seg_clear_grown``).
        pw = None if fat else self._pairwise
        # Issue #4605: keepout rule areas gate every stub candidate + dogleg
        # leg at the leg's TRUE half-width (a tapered neck is probed as neck
        # copper).  The fat coupled centerline skips the mask here -- the
        # emitted offset legs are re-verified against it in
        # ``_route_pair_impl``'s finish gate, so nothing under-masked ships.
        ko = None if fat else self._keepouts

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
                    # Issue #6697: the stub's lattice node is where the
                    # emitted legs start their offset body; keep it a pair
                    # envelope clear of any keepout (reserved via site) so the
                    # finish gate does not decline a corridor the search chose.
                    if self._keepouts is not None and self._keepouts.segment_blocked(
                        point, point, layer, net, half + math.sqrt(2.0) * extra_clearance
                    ):
                        continue
                else:
                    if obstacles.node_blocked(key, layer, net):
                        continue
                    if extra > 0.0 and obstacles.segment_blocked(point, point, layer, net, extra):
                        continue
                    if pw is not None and obstacles.pairwise_pad_blocked(
                        point, point, layer, net, half, extra, pw
                    ):
                        continue
                    if ko is not None and ko.segment_blocked(point, point, layer, net, half):
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
                            if pw is not None and obstacles.pairwise_pad_blocked(
                                a, b, layer, net, half, extra, pw
                            ):
                                ok = False
                                break
                            if ko is not None and ko.segment_blocked(a, b, layer, net, half):
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
        allow_vias: bool = True,
        via_in_pad_override: bool | None = None,
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

        Issue #6381: "the full-width scan produced a stub" is not the same as
        "the full width can escape".  On a fine-pitch row the full-width
        centreline band between the neighbour pads can be narrower than the
        lattice row spacing, so the only nodes a full-width stub reaches are
        cell centres INSIDE the pad's own copper with no legal full-width
        outgoing edge.  Those dead-end stubs used to count as success and
        suppress the neck fallback, so the search popped them and declined.
        A non-fat full-width stub set now counts only when at least one stub
        has egress (:meth:`_stub_has_egress` -- a legal full-width lattice
        edge, or a legal via, out of its landing node, judged by the SAME
        predicates the A* expands with).  When none does, the neck fallback
        runs; if the neck finds nothing either, the original full-width stubs
        are returned unchanged, so the outcome is never worse than before.
        Stub sets with egress (every connection that routed before) are
        returned byte-identically.
        """
        body_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        full_half, clr = self._conn_geometry(net_class, net)
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
        # envelope has its own escape discipline), is returned as-is.  A
        # full-width stub set none of whose stubs can LEAVE its landing node
        # at full width is a dead end, not a success (issue #6381): fall
        # through to the neck scan, keeping the dead-end set as the
        # never-worse result if the neck cannot escape either.
        if fat:
            return stubs, body_w
        if stubs:
            if any(
                self._stub_has_egress(
                    stub,
                    net,
                    committed,
                    full_half,
                    clr,
                    net_class=net_class,
                    allow_vias=allow_vias,
                    via_in_pad_override=via_in_pad_override,
                )
                for stub in stubs
            ):
                return stubs, body_w
            dead_end = stubs
        else:
            dead_end = []

        neck_w = self._neck_width(net_class)
        neck_half = neck_w / 2.0
        if neck_half >= full_half - 1e-9:
            # Not oversize: the neck would not be narrower than the body, so
            # there is nothing to taper -- the honest decline stands.
            return dead_end, body_w

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
        if (
            not neck_stubs
            and not pad.through_hole
            and not pad.steiner_point
            and pad.shape in {"rect", "circle", "oval", "roundrect"}
        ):
            # A wide center-launch cap can approach a sibling pad more closely
            # than the authored pad copper does. Try interior attachment sites
            # without reducing the class neck width or clearance (#4507).
            # Every supported convex pad shape contains the centered disc of
            # radius min(width, height)/2, regardless of rotation/rounding.
            # Keep the original inner fan first, then try near the edge of
            # that disc. A wide neck may need more offset than the inner fan
            # provides (#4507's R58). Both fans stay strictly inside copper;
            # every stub still passes the unchanged width/clearance checks.
            diameter = min(pad.width, pad.height)
            if math.isfinite(diameter) and diameter > 0.0:
                for fraction in (0.25, 0.45):
                    radius = diameter * fraction
                    for angle in range(0, 360, 45):
                        theta = math.radians(angle)
                        attachment = replace(
                            pad,
                            x=pad.x + radius * math.cos(theta),
                            y=pad.y + radius * math.sin(theta),
                        )
                        neck_stubs = self._scan_stubs(
                            attachment,
                            net,
                            committed,
                            neck_half,
                            clr,
                            kmax=_OVERSIZE_STUB_KMAX,
                            search_radius=grown_sr,
                            layers=layers,
                            exempt_pads=exempt_pads,
                        )
                        if neck_stubs:
                            break
                    if neck_stubs:
                        break
                if not neck_stubs:
                    # The trace's round cap may bond the pad even when its
                    # centerline endpoint is outside it. Overlap the guaranteed
                    # inscribed disc by half its radius, never by a tangency.
                    radius = neck_half + diameter / 4.0
                    for angle in range(0, 360, 45):
                        theta = math.radians(angle)
                        attachment = replace(
                            pad,
                            x=pad.x + radius * math.cos(theta),
                            y=pad.y + radius * math.sin(theta),
                        )
                        candidates = self._scan_stubs(
                            attachment,
                            net,
                            committed,
                            neck_half,
                            clr,
                            kmax=_OVERSIZE_STUB_KMAX,
                            search_radius=grown_sr,
                            layers=layers,
                            exempt_pads=exempt_pads,
                        )
                        neck_stubs = [
                            stub
                            for stub in candidates
                            if self._escape_boundary.segment_clear(
                                stub[2][-1], stub[2][-1], full_half
                            )
                            and all(
                                self._escape_boundary.segment_clear(a, b, neck_half)
                                for a, b in zip(stub[2], stub[2][1:], strict=False)
                            )
                        ]
                        if neck_stubs:
                            break
        if not neck_stubs and dead_end:
            # Issue #6381: the neck found nothing either -- keep the pre-#6381
            # result (the dead-end full-width stubs) rather than turning a
            # no-path decline into a pad-escape one.
            return dead_end, body_w
        return neck_stubs, neck_w

    def _body_edge_legal(
        self,
        edge: EdgeKey,
        layer: int,
        net: int,
        half: float,
        clr: float,
        committed: CommittedCopper,
        *,
        extra: float,
        pw: Any,
        ko: Any,
    ) -> tuple[bool, bool]:
        """Single-ended full-width legality of one lattice edge on ``layer``.

        Returns ``(legal, keepout_hit)``.  This is the exact predicate the
        A* in :meth:`_route_search` expands with (issue #6381 factored it out
        so the escape-egress check cannot drift from the search); the
        Edge.Cuts boundary test is applied by the caller.
        """
        lattice = self.build()
        ea = lattice.node_point(edge[0])
        eb = lattice.node_point(edge[1])
        ko_hit = ko is not None and ko.segment_blocked(ea, eb, layer, net, half)
        obstacles = self.obstacles
        ok = (
            not ko_hit
            and not obstacles.edge_blocked(edge, layer, net)
            and not (extra > 0.0 and obstacles.segment_blocked(ea, eb, layer, net, extra))
            and not (
                pw is not None
                and obstacles.pairwise_pad_blocked(ea, eb, layer, net, half, extra, pw)
            )
            and committed.seg_clear(ea, eb, layer, net, half, clr)
        )
        return ok, ko_hit

    def _body_node_legal(
        self,
        key: NodeKey,
        layer: int,
        net: int,
        half: float,
        clr: float,
        committed: CommittedCopper,
        *,
        extra: float,
        pw: Any,
        ko: Any,
    ) -> tuple[bool, bool]:
        """Single-ended full-width legality of one lattice node (``(ok, ko_hit)``).

        Node counterpart of :meth:`_body_edge_legal` (issue #6381).
        """
        npt = self.build().node_point(key)
        ko_hit = ko is not None and ko.segment_blocked(npt, npt, layer, net, half)
        obstacles = self.obstacles
        ok = (
            not ko_hit
            and not obstacles.node_blocked(key, layer, net)
            and not (extra > 0.0 and obstacles.segment_blocked(npt, npt, layer, net, extra))
            and not (
                pw is not None
                and obstacles.pairwise_pad_blocked(npt, npt, layer, net, half, extra, pw)
            )
            and committed.node_clear(npt, layer, net, half, clr)
        )
        return ok, ko_hit

    def _stub_has_egress(
        self,
        stub: tuple[NodeKey, int, list[Pt], float],
        net: int,
        committed: CommittedCopper | None,
        half: float,
        clr: float,
        *,
        net_class: object | None = None,
        allow_vias: bool = True,
        via_in_pad_override: bool | None = None,
    ) -> bool:
        """Whether a full-width stub's landing node can be LEFT (issue #6381).

        True when at least one lattice edge out of the landing ``(node,
        layer)`` is legal at the full body width (edge + far node, the same
        predicates and Edge.Cuts test the A* applies), or -- when vias are
        allowed -- a through via at the node is legal.  A stub without egress
        is a dead end the search can only pop and drop.
        """
        lattice = self.build()
        if committed is None:
            committed = self._fresh_committed()
        key, layer = stub[0], stub[1]
        extra = max(0.0, half + clr - self._agent_radius)
        pw, ko = self._pairwise, self._keepouts
        boundary = self._escape_boundary
        for nbr, _elen in lattice.adj.get(key, ()):
            edge = (min(key, nbr), max(key, nbr))
            if not self._body_edge_legal(
                edge, layer, net, half, clr, committed, extra=extra, pw=pw, ko=ko
            )[0]:
                continue
            if not boundary.segment_clear(
                lattice.node_point(edge[0]), lattice.node_point(edge[1]), half
            ):
                continue
            if self._body_node_legal(
                nbr, layer, net, half, clr, committed, extra=extra, pw=pw, ko=ko
            )[0]:
                return True
        return bool(
            allow_vias
            and self.num_layers > 1
            and self._via_ok(
                key,
                net,
                committed,
                via_in_pad_override=via_in_pad_override,
                clearance=clr if net_class is not None else None,
            )
        )

    def _keepout_escape_reason(
        self,
        pad: Pad,
        net: int,
        stub_layers: tuple[int, ...] | None,
        half: float,
        base: str,
    ) -> str:
        """Refine an empty-stub decline with keepout attribution (#4605).

        A pad whose OWN location sits inside an applicable track-keepout rule
        area on every layer it could escape on can never produce a legal stub;
        report that explicitly (``<base>-keepout``) instead of the generic
        pad-escape decline the #3900 lesson warns against.  Any other empty
        stub set keeps the honest base reason -- the mask is only blamed when
        the pad point itself is provably covered.
        """
        keepouts = self._keepouts
        if keepouts is None:
            return base
        layers = stub_layers if stub_layers is not None else self._pad_layer_indices(pad)
        point = (pad.x, pad.y)
        if layers and all(
            keepouts.segment_blocked(point, point, layer, net, half) for layer in layers
        ):
            return base + "-keepout"
        return base

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

    def _via_ok(
        self,
        key: NodeKey,
        net: int,
        committed: CommittedCopper,
        *,
        via_in_pad_override: bool | None = None,
        clearance: float | None = None,
    ) -> bool:
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

        ``clearance`` carries the effective net-class gap through the entire
        barrel span, including layers the trace search skips. ``None`` keeps
        the board-global trace and separate fixed-fill via defaults.

        ``via_in_pad_override`` (issue #4475, epic #4465 Phase 3) lets a
        caller bypass the tier gate for the SAME-net window hit: ``None``
        (default) preserves the pre-#4475 :attr:`_via_in_pad_allowed`
        tier-only check byte-for-byte; ``False`` forces the same-net hit
        illegal regardless of tier (the last-resort search's stage (a)+(b)
        pass, which must never land a via-in-pad); ``True`` forces it legal
        regardless of tier (a DIAGNOSTIC-ONLY probe used to name the
        tier-limit decline reason -- callers must never emit a route found
        this way). The unconditional OTHER-net grown-rect veto below is
        never affected by this override.
        """
        point = self.build().node_point(key)
        return self._via_ok_at(
            point,
            net,
            committed,
            via_in_pad_override=via_in_pad_override,
            clearance=clearance,
        )

    def _via_ok_at(
        self,
        point: Pt,
        net: int,
        committed: CommittedCopper,
        *,
        via_in_pad_override: bool | None = None,
        clearance: float | None = None,
    ) -> bool:
        """:meth:`_via_ok` at an arbitrary point (issue #6444 fan-via gateways).

        Identical legality -- the lattice-node variant only resolves the
        node's coordinates first -- so a gateway via is held to exactly the
        rules every negotiated via meets.
        """
        obstacles = self.obstacles
        via_radius = self.rules.via_diameter / 2.0
        if not self._escape_boundary.segment_clear(point, point, via_radius):
            return False
        own_clr = (
            self.rules.trace_clearance
            if clearance is None
            else max(self.rules.trace_clearance, clearance)
        )
        grow = max(via_radius + own_clr - self._agent_radius, 0.0)
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
            else:
                half_w, half_h = pad_half_extents(pad)
                if (
                    abs(point[0] - pad.x) <= half_w + via_radius
                    and abs(point[1] - pad.y) <= half_h + via_radius
                ):
                    # Via barrel intersects an SMD pad rect (#4284).  Same-net
                    # is via-in-pad: legal only on fab tiers that fill/cap the
                    # via (jlcpcb-tier1, pcbway); the default tier rejects so
                    # the layer change moves off the pad onto the escape stub.
                    # Other-net falls through to the unconditional grown-rect
                    # veto below.
                    allow_via_in_pad = (
                        self._via_in_pad_allowed
                        if via_in_pad_override is None
                        else via_in_pad_override
                    )
                    if pad.net == net and not allow_via_in_pad:
                        return False
            if pad.net == net:
                continue
            rect = obstacles.pad_rects[idx]
            grown = (rect[0] - grow, rect[1] - grow, rect[2] + grow, rect[3] + grow)
            if pt_in_rect(point, grown):
                return False
        # Issue #4602: a through-via must additionally keep the PAIR (HV)
        # requirement from foreign pads on every layer (``layer=None``) --
        # the barrel exists on all of them.  Dormant when no projection.
        if self._pairwise is not None and obstacles.pairwise_pad_blocked(
            point,
            point,
            None,
            net,
            self.rules.via_diameter / 2.0,
            grow,
            self._pairwise,
        ):
            return False
        # Issue #4605: a ``(vias not_allowed)`` rule area governing this net
        # rejects the through-via when its barrel would enter the area on ANY
        # of the area's layers (the barrel spans the whole stack).
        if self._keepouts is not None and self._keepouts.via_blocked(point, net, via_radius):
            return False
        return committed.via_clear(point, net, clearance)

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
        escape_plan: dict[int, EscapeChoice] | None = None,
        guide: PartnerGuide | None = None,
    ) -> tuple[_RouteResult | None, str]:
        """Apply physical Kelvin isolation throughout every search stage."""
        from ..kelvin import detect_kelvin_topology

        pads = [pad for pad in self.pads if pad.net == start.net]
        # Topology operates on electrical terminals, while obstacle geometry
        # keeps every physical pad occurrence (including same-number arrays).
        terminals = list({pad.key: pad for pad in pads}.values())
        topology = detect_kelvin_topology(terminals)
        previous = committed.kelvin_guard
        if topology is not None:
            from .kelvin import KelvinBranchGuard

            root = terminals[topology.root_index]
            target = end if start.key == root.key else start
            committed.kelvin_guard = KelvinBranchGuard(
                committed, pads, root, target, self._pad_layer_indices
            )
        try:
            return self._route_staged(
                start,
                end,
                net_class,
                committed=committed,
                history=history,
                present=present,
                allow_vias=allow_vias,
                extra_clearance=extra_clearance,
                partner_net=partner_net,
                stub_layers=stub_layers,
                stubs_override=stubs_override,
                exempt_pads=exempt_pads,
                escape_plan=escape_plan,
                guide=guide,
            )
        finally:
            committed.kelvin_guard = previous

    def _route_staged(
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
        escape_plan: dict[int, EscapeChoice] | None = None,
        guide: PartnerGuide | None = None,
    ) -> tuple[_RouteResult | None, str]:
        """Route one connection, staging via-in-pad as a last resort (#4475).

        Thin orchestrator around :meth:`_route_search` (the actual A*
        implementation). When :attr:`DesignRules.via_in_pad_last_resort` is
        off (the default -- byte-for-byte pre-#4475 behavior), this simply
        forwards to a single :meth:`_route_search` call with the legacy
        tier-only via-in-pad gate (#4284): a same-net pad-site via is legal
        whenever the fab tier allows it, opportunistically, in the SAME
        unified search as every other edge.

        When the flag is on (and the connection is via-eligible: planar
        fat-agent runs never get vias at all, so staging is a no-op for
        them), the search runs in stages:

        (a)+(b) In-layer route / free-space (off-pad) via layer change,
            with via-in-pad forced OFF regardless of tier. If this stage
            finds a path, it is returned immediately -- the pad is closed
            WITHOUT a via-in-pad whenever any other route exists.
        (c) Only reached when stage (a)+(b) fails.  The fab tier is the
            hard floor here (#4284): on a tier that does not support
            via-in-pad, stage (c) is never attempted.  Instead a cheap
            DIAGNOSTIC-ONLY re-run (via-in-pad forced legal, its result
            always discarded -- never emitted) checks whether the pad-site
            via is the actual reason the link is stuck, so the decline can
            be reported with the explicit ``"via-in-pad-tier-unsupported"``
            reason (feeds Phase 4 reporting, epic #4465) rather than a
            generic ``"no-path"``.  When the tier DOES support via-in-pad,
            stage (c) retries with the tier-gated legality (identical to
            the single-stage legacy path) and its outcome -- success or
            failure -- is returned as-is.
        """
        fat = extra_clearance > 0.0 or partner_net is not None
        if escape_plan and not fat:
            committed = foreign_reservations(committed, escape_plan, start.net)
        last_resort = (
            self.rules.via_in_pad_last_resort and allow_vias and not fat and self.num_layers > 1
        )
        search_kwargs: dict[str, Any] = {
            "committed": committed,
            "history": history,
            "present": present,
            "allow_vias": allow_vias,
            "extra_clearance": extra_clearance,
            "partner_net": partner_net,
            "stub_layers": stub_layers,
            "stubs_override": stubs_override,
            "exempt_pads": exempt_pads,
        }
        if escape_plan and not fat:
            search_kwargs["escape_plan"] = escape_plan
        if guide is not None and not fat:
            search_kwargs["guide"] = guide
        if not last_resort:
            return self._route_search(start, end, net_class, **search_kwargs)

        # Stage (a)+(b): via-in-pad forced OFF regardless of tier.
        result, reason = self._route_search(
            start, end, net_class, via_in_pad_override=False, **search_kwargs
        )
        if result is not None:
            return result, reason

        # Stage (c): the tier gate is the hard floor.  A tier without
        # via-in-pad support never reaches it -- report the tier-limit
        # reason (only when a diagnostic re-run proves via-in-pad would
        # actually have closed the link; otherwise the honest stage-(a)+(b)
        # decline reason stands, e.g. a pad-escape failure unrelated to
        # vias at all).
        if not self._via_in_pad_allowed:
            diag_result, _diag_reason = self._route_search(
                start, end, net_class, via_in_pad_override=True, **search_kwargs
            )
            if diag_result is not None:
                return None, "via-in-pad-tier-unsupported"
            return None, reason

        return self._route_search(start, end, net_class, via_in_pad_override=None, **search_kwargs)

    def _route_search(
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
        via_in_pad_override: bool | None = None,
        escape_plan: dict[int, EscapeChoice] | None = None,
        guide: PartnerGuide | None = None,
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
        ``via_in_pad_override`` (issue #4475) is forwarded verbatim to every
        :meth:`_via_ok` call this search makes; see that method's docstring.
        """
        lattice = self.build()
        obstacles = self.obstacles
        net = start.net
        # Per-connection copper geometry (issue #4271): the class half-width
        # and clearance size EVERY legality check below, and ``extra`` is the
        # keep-out surcharge over the global agent radius for the static pad
        # masks (0.0 for default-width nets -> pre-#4271 behavior exactly).
        half, clr = self._conn_geometry(net_class, net)
        extra = max(0.0, half + clr - self._agent_radius)

        # Issue #4979: hard-block layers the net's class declares off-limits
        # under ``--strict-layers`` (or unconditionally under
        # ``target_ampacity``) -- see ``_hard_avoided_layers``.  Filtering
        # the pad-escape seed/goal layers below AND the via-hop landing layer
        # further down means no A* state this search ever visits can carry
        # a hard-avoided layer index, so the lattice can only decline a
        # connection that has no legal route -- it can never ship copper on
        # the forbidden layer as partial progress.  A via may still STEP OVER
        # a forbidden layer to reach an allowed one (the emitted through via's
        # barrel crossing a plane is DRC's antipad concern, and no copper is
        # committed there) -- without that, a hard block on both inner layers
        # of a 4-layer board would strand an F/B-only net on whichever outer
        # layer its pads escaped onto, which the grid backend never does.
        # Blocking an outer layer is the opposite case and is handled by the
        # via-span guard further down: the emitted via TERMINATES there, so
        # its annulus is real copper on a forbidden layer.
        hard_avoided = self._hard_avoided_layers(net_class)

        # Issue #6824: ``NetClassRouting.preferred_layers`` as a SOFT bias.
        # Planar copper laid on a layer outside the preferred set pays an
        # extra ``elen * _NON_PREFERRED_LAYER_SURCHARGE`` (the grid
        # pathfinder's 0.5x preferred discount expressed as a surcharge, so
        # the Euclidean heuristic stays admissible).  Empty set -> no
        # preference -> byte-identical to the pre-#6824 search.
        preferred_layers = self._preferred_layers(net_class)

        fat = extra_clearance > 0.0 or partner_net is not None
        # Issue #4602: per-PAIR HV pad keep-outs for the non-fat search (the
        # fat coupled path is excluded from pairwise widening in this slice).
        pw = None if fat else self._pairwise
        # Issue #4605: keepout rule areas prune illegal resources in the same
        # cached edge/node legality memos below.  ``ko_pruned`` records that
        # the mask actually rejected at least one resource for THIS search, so
        # a failing connection can report the keepout as a constraint instead
        # of a bare "no-path" (the #3900 never-misattribute lesson).  The fat
        # coupled centerline skips the mask in-search; its emitted legs are
        # re-verified in ``_route_pair_impl``'s finish gate.
        ko = None if fat else self._keepouts
        # Issue #6697: the fat centerline body ALSO honours the keepout mask,
        # at the pair ENVELOPE half-width (``trace/2 + pitch/2``): the nearer
        # emitted leg then clears the area by exactly its own half-width.
        # Without this, history pressure from a congested pass displaced the
        # centerline onto a reserved pour-pad via site (board 03 U1.5 GND
        # reservation) that only the finish gate saw, declining a pair that
        # had coupled on pass 0.
        fat_ko = self._keepouts if fat else None
        fat_ko_half = (
            (getattr(net_class, "trace_width", None) or self.rules.trace_width) / 2.0
            + extra_clearance
            if fat
            else 0.0
        )
        # A node is a possible polyline VERTEX, and the +/- pitch/2 offset
        # mitres a 90-degree turn out to pitch/2 * sqrt(2) from the
        # centerline, so vertices are probed at the mitred reach (board 03:
        # a 0.2 mm V-jog put the offset leg 0.1 mm inside the U1.5 site).
        fat_ko_node_half = fat_ko_half + (math.sqrt(2.0) - 1.0) * extra_clearance if fat else 0.0
        ko_pruned = False
        # Issue #4979: set when a via-hop was skipped because its target
        # layer is hard-avoided for this net -- lets the final "no-path"
        # decline distinguish a layer-constrained shortfall (see the
        # ``end_state is None`` branch below).
        layer_pruned = False
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

        # Issue #4979: this engine emits EVERY layer change as a through via
        # spanning the whole stack (``_emit``: ``layers=(index 0, index
        # num_layers - 1)``), so the via's own annular ring lands copper on
        # both outer layers no matter which layers the path uses.  The grid
        # backend has no such problem -- its via spans only the two layers it
        # actually hops between -- so the lattice needs an extra guard: when
        # either span endpoint is hard-avoided for this net, NO via this
        # engine can emit is legal, and vias are dropped from the search
        # entirely.  Without it the search would honour the constraint while
        # its own emission broke it, and the post-route layer-intent gate
        # would (correctly) fail the run on copper the router had just
        # decided was legal.  The connection routes planar or declines as
        # layer-constrained -- it never ships the forbidden annulus.
        if allow_vias and hard_avoided and hard_avoided & {0, self.num_layers - 1}:
            allow_vias = False
            layer_pruned = True

        # The lattice body is emitted (and spaced) at the full class width;
        # the pad-escape legs may taper to a narrower neck (issue #4293).
        body_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        if stubs_override is not None:
            stubs_a, stubs_b = stubs_override
            width_a = width_b = body_w
        else:
            stub_kmax = 24 if fat else 4

            def escape(pad: Pad) -> tuple[list, float]:
                if escape_plan and id(pad) in escape_plan:
                    return chosen_stub(escape_plan[id(pad)], committed)
                return self._escape_stubs(
                    pad,
                    net,
                    committed,
                    kmax=stub_kmax,
                    extra_clearance=extra_clearance,
                    partner_net=partner_net,
                    layers=stub_layers,
                    exempt_pads=exempt_pads,
                    net_class=net_class,
                    allow_vias=allow_vias,
                    via_in_pad_override=via_in_pad_override,
                )

            stubs_a, width_a = escape(start)
            stubs_b, width_b = escape(end)
        # Issue #4979: drop any escape stub that lands on a hard-avoided
        # layer BEFORE the empty-stub decline check, so a pad whose only
        # legal escape is on a forbidden inner plane declines honestly
        # (reported as a layer-constrained residual) instead of routing.
        if hard_avoided:
            had_stubs_a, had_stubs_b = bool(stubs_a), bool(stubs_b)
            kept_a = [s for s in stubs_a if s[1] not in hard_avoided]
            kept_b = [s for s in stubs_b if s[1] not in hard_avoided]
            # A dropped-but-not-emptied stub set still narrowed the search.
            if len(kept_a) != len(stubs_a) or len(kept_b) != len(stubs_b):
                layer_pruned = True
            stubs_a, stubs_b = kept_a, kept_b
        else:
            had_stubs_a = had_stubs_b = False
        # The substrate is built at global width. Check the actual emitted
        # neck and body radii against Edge.Cuts before admitting any state.
        boundary = self._escape_boundary
        if not boundary.valid:
            return None, "invalid-board-outline"

        def boundary_stubs(stubs: list, width: float) -> list:
            return [
                stub
                for stub in stubs
                if boundary.segment_clear(
                    lattice.node_point(stub[0]),
                    lattice.node_point(stub[0]),
                    half + extra_clearance,
                )
                and all(
                    boundary.segment_clear(a, b, width / 2 + extra_clearance)
                    for a, b in zip(stub[2], stub[2][1:], strict=False)
                )
            ]

        stubs_a = boundary_stubs(stubs_a, width_a)
        stubs_b = boundary_stubs(stubs_b, width_b)
        if not stubs_a:
            if hard_avoided and had_stubs_a:
                return None, "layer-constrained-start"
            reason = "pad-escape-start"
            if not fat:
                reason = self._keepout_escape_reason(start, net, stub_layers, half, reason)
            return None, reason
        if not stubs_b:
            if hard_avoided and had_stubs_b:
                return None, "layer-constrained-end"
            reason = "pad-escape-end"
            if not fat:
                reason = self._keepout_escape_reason(end, net, stub_layers, half, reason)
            return None, reason

        def copper_cost(a: Pt, b: Pt, layer: int, width: float) -> float:
            if isinstance(history, PhysicalHistory):
                return present * history.cost(
                    a, b, layer, net, width / 2 + extra_clearance, clr, pw, partner_net
                )
            return 0.0

        # Issue #6975: a parity-filtered pair search (``_route_pair_impl``)
        # chose each stub for the polarity its travel direction serves -- the
        # start stub is left ALONG it, the goal stub is entered against its
        # polyline.  A heading-less A* could instead overshoot an end anchor
        # and come straight back along the stub; the trimmed centreline then
        # travels the other way at that end and the pair fails polarity.  So
        # in that search a goal is only arrived at along a step that does not
        # reverse onto its stub, and a start node is never left back along
        # its stub.
        parity_ends = fat and stubs_override is not None

        def stub_tail_dir(poly: list[Pt]) -> Pt | None:
            """Direction of the stub's last non-degenerate step (pad -> node)."""
            for a, b in zip(reversed(poly[:-1]), reversed(poly[1:]), strict=False):
                d = _unit_step(a, b)
                if d is not None:
                    return d
            return None

        goal: dict[_State, tuple[float, list[Pt]]] = {}
        for key, layer, poly, length in stubs_b:
            length += sum(
                copper_cost(a, b, layer, width_b) for a, b in zip(poly, poly[1:], strict=False)
            )
            state = (key, layer)
            if state not in goal or length < goal[state][0]:
                goal[state] = (length, poly)
        # Issue #6975: the travel direction the goal stub is entered with
        # (node -> pad, the reverse of its polyline's last step).
        goal_out: dict[_State, Pt | None] = {}
        if parity_ends:
            for state, (_len, poly) in goal.items():
                last = stub_tail_dir(poly)
                goal_out[state] = None if last is None else (-last[0], -last[1])
        end_pt = (end.x, end.y)

        def h(key: NodeKey) -> float:
            return dist(lattice.node_point(key), end_pt)

        g_score: dict[_State, float] = {}
        came: dict[_State, tuple[_State, str]] = {}
        # Issue #6957: the via sites already on each state's best path, as a
        # persistent linked list ``(site, rest)`` (newest first).  Kept in step
        # with ``came`` so a via expansion can be checked against its OWN
        # path's earlier drills -- ``via_ok`` only sees committed copper.
        path_vias: dict[_State, _ViaChain] = {}
        start_stub: dict[_State, list[Pt]] = {}
        heap: list[tuple[float, int, _State]] = []
        counter = 0
        for key, layer, poly, length in stubs_a:
            length += sum(
                copper_cost(a, b, layer, width_a) for a, b in zip(poly, poly[1:], strict=False)
            )
            state = (key, layer)
            if state not in g_score or length < g_score[state]:
                g_score[state] = length
                start_stub[state] = poly
                heapq.heappush(heap, (length + h(key), counter, state))
                counter += 1

        # Issue #6975: the travel direction each start node is reached with.
        start_in: dict[_State, Pt | None] = (
            {state: stub_tail_dir(poly) for state, poly in start_stub.items()}
            if parity_ends
            else {}
        )

        edge_ok: dict[tuple[EdgeKey, int], bool] = {}
        node_ok: dict[_State, bool] = {}
        via_ok: dict[NodeKey, bool] = {}
        end_state: _State | None = None
        # Issue #6975: under ``parity_ends`` a goal is registered per arriving
        # EDGE (``"arrive-edge"``): the goal node's own best arrival may be the
        # reversing one, so its ``came`` entry cannot name the predecessor of
        # a non-reversing arrival.  ``arrive_node`` is that goal node.
        arrive_node: _State | None = None
        end_pred: _State | None = None

        def passes_through(tip: _State, node: _State) -> bool:
            """True when ``node`` is on ``tip``'s settled path (#6975).

            An ``arrive-edge`` goal must not loop back to a goal node its
            path already crossed: that micro-hook is the same overshoot with
            an extra spur, and offsetting it fails just the same.
            """
            cur: _State | None = tip
            while cur is not None:
                if cur == node:
                    return True
                cur = came[cur][0] if cur in came else None
            return False

        while heap:
            f, _c, state = heapq.heappop(heap)
            if state == _GOAL:
                if came[_GOAL][1] == "arrive-edge":
                    end_state, end_pred = arrive_node, came[_GOAL][0]
                else:
                    end_state = came[_GOAL][0]
                break
            key, layer = state
            g = g_score.get(state, math.inf)
            if f > g + h(key) + 1e-9:
                continue  # stale heap entry
            is_start = parity_ends and state not in came
            if state in goal and (
                not parity_ends
                # A start node that is itself a goal: the two stubs meet here.
                or (is_start and not _reverses(start_in.get(state), goal_out.get(state)))
                or (not is_start and came[state][1] == "via")
            ):
                total = g + goal[state][0]
                if total < g_score.get(_GOAL, math.inf) - 1e-12:
                    g_score[_GOAL] = total
                    came[_GOAL] = (state, "arrive")
                    heapq.heappush(heap, (total, counter, _GOAL))
                    counter += 1
            for nbr, elen in lattice.adj.get(key, ()):
                step_dir: Pt | None = None
                if parity_ends:
                    step_dir = _unit_step(lattice.node_point(key), lattice.node_point(nbr))
                    if is_start and _reverses(start_in.get(state), step_dir):
                        continue  # would double back along the start stub
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
                        if (
                            ok
                            and fat_ko is not None
                            and fat_ko.segment_blocked(pa, pb, layer, net, fat_ko_half)
                        ):
                            ok = False
                            ko_pruned = True
                    else:
                        ok, ko_hit = self._body_edge_legal(
                            edge, layer, net, half, clr, committed, extra=extra, pw=pw, ko=ko
                        )
                        if ko_hit:
                            ko_pruned = True
                    ok = ok and boundary.segment_clear(
                        lattice.node_point(edge[0]),
                        lattice.node_point(edge[1]),
                        half + extra_clearance,
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
                        if (
                            nok
                            and fat_ko is not None
                            and fat_ko.segment_blocked(npt, npt, layer, net, fat_ko_node_half)
                        ):
                            nok = False
                            ko_pruned = True
                    else:
                        nok, ko_hit = self._body_node_legal(
                            nbr, layer, net, half, clr, committed, extra=extra, pw=pw, ko=ko
                        )
                        if ko_hit:
                            ko_pruned = True
                    node_ok[nstate] = nok
                if not nok:
                    continue
                step = elen + present * history.get(("e", edge, layer), 0.0)
                if preferred_layers and layer not in preferred_layers:
                    step += elen * _NON_PREFERRED_LAYER_SURCHARGE
                step += copper_cost(lattice.node_point(key), lattice.node_point(nbr), layer, body_w)
                if guide is not None:
                    # Issue #6703: soft pull toward the partner leg's corridor.
                    step += guide.edge_cost(lattice.node_point(key), lattice.node_point(nbr), layer)
                tentative = g + step
                if (
                    parity_ends
                    and nstate in goal
                    and not _reverses(step_dir, goal_out.get(nstate))
                    and not passes_through(state, nstate)
                ):
                    total = tentative + goal[nstate][0]
                    if total < g_score.get(_GOAL, math.inf) - 1e-12:
                        g_score[_GOAL] = total
                        came[_GOAL] = (state, "arrive-edge")
                        arrive_node = nstate
                        heapq.heappush(heap, (total, counter, _GOAL))
                        counter += 1
                if tentative < g_score.get(nstate, math.inf) - 1e-12:
                    g_score[nstate] = tentative
                    came[nstate] = (state, "move")
                    path_vias[nstate] = path_vias.get(state)
                    heapq.heappush(heap, (tentative + h(nbr), counter, nstate))
                    counter += 1
            if allow_vias and self.num_layers > 1:
                vok = via_ok.get(key)
                if vok is None:
                    vok = self._via_ok(
                        key,
                        net,
                        committed,
                        via_in_pad_override=via_in_pad_override,
                        clearance=clr if net_class is not None else None,
                    )
                    via_ok[key] = vok
                if vok:
                    # Issue #6957: ``via_ok`` is memoised per node against
                    # COMMITTED copper only; this route's own earlier vias are
                    # path-dependent, so check them here, uncached (the
                    # lattice twin of the grid engines' #6464 / #6473 check).
                    vok = self._own_path_vias_clear(lattice.node_point(key), path_vias.get(state))
                if vok:
                    # Via edges join matching nodes on ADJACENT layers only
                    # (issue #4278 acceptance 6).  The emitted via is still a
                    # through-via; a multi-layer dip pays via_cost per hop.
                    for direction in (-1, 1):
                        nl = layer + direction
                        # Issue #4979: never LAND on a layer this net's class
                        # hard-blocks -- but do not let the block wall the net
                        # in either.  Step over any run of forbidden layers to
                        # the first allowed one: the emitted copper is a single
                        # through via (see ``_emit``), whose barrel crossing a
                        # forbidden plane is an antipad/clearance matter for
                        # DRC, not a layer-intent violation -- no copper is
                        # committed on the layers stepped over.  This keeps the
                        # lattice's adjacent-hop via model behaviourally equal
                        # to the grid backend's (``router/pathfinder.py``),
                        # whose via loop already offers every routable layer
                        # directly, so an F/B-only net can still cross F<->B on
                        # a 4-layer board instead of being stranded on the one
                        # outer layer its pads escaped onto.
                        while 0 <= nl < self.num_layers and nl in hard_avoided:
                            nl += direction
                            # The hard constraint removed a candidate landing
                            # layer from this search -- recorded so a later
                            # decline can name the layer constraint rather
                            # than reporting a bare "no-path" (same honesty
                            # contract as ``ko_pruned`` above: CONSTRAINED the
                            # search, not necessarily the sole cause).
                            layer_pruned = True
                        if nl < 0 or nl >= self.num_layers:
                            # Ran off the stack: this direction exists only
                            # through forbidden layers.
                            continue
                        nstate = (key, nl)
                        # A tapered goal reached by a via emits only its neck
                        # and the via on this layer, not a body-width segment.
                        # Keep this legality separate from the body-node cache:
                        # any later planar expansion must still clear at half.
                        tapered_goal = nstate in goal and width_b < body_w
                        landing_half = (
                            max(width_b, self.rules.via_diameter) / 2.0 if tapered_goal else half
                        )
                        landing_extra = max(0.0, landing_half + clr - self._agent_radius)
                        nok = None if tapered_goal else node_ok.get(nstate)
                        if nok is None:
                            kpt = lattice.node_point(key)
                            ko_hit = ko is not None and ko.segment_blocked(
                                kpt, kpt, nl, net, landing_half
                            )
                            if ko_hit:
                                ko_pruned = True
                            nok = (
                                not ko_hit
                                and not obstacles.node_blocked(key, nl, net)
                                and not (
                                    landing_extra > 0.0
                                    and obstacles.segment_blocked(kpt, kpt, nl, net, landing_extra)
                                )
                                and not (
                                    pw is not None
                                    and obstacles.pairwise_pad_blocked(
                                        kpt, kpt, nl, net, landing_half, landing_extra, pw
                                    )
                                )
                                and committed.node_clear(kpt, nl, net, landing_half, clr)
                            )
                            if not tapered_goal:
                                node_ok[nstate] = nok
                        if not nok:
                            continue
                        step = self.via_cost + present * history.get(("v", key), 0.0)
                        step += sum(
                            copper_cost(
                                lattice.node_point(key),
                                lattice.node_point(key),
                                li,
                                self.rules.via_diameter,
                            )
                            for li in range(self.num_layers)
                        )
                        tentative = g + step
                        if tentative < g_score.get(nstate, math.inf) - 1e-12:
                            g_score[nstate] = tentative
                            came[nstate] = (state, "via")
                            path_vias[nstate] = (lattice.node_point(key), path_vias.get(state))
                            heapq.heappush(heap, (tentative + h(key), counter, nstate))
                            counter += 1

        if end_state is None:
            # Issue #4605: when the keepout mask actually rejected resources
            # for this search, say so -- the area CONSTRAINED the search (it
            # pruned candidates), which is honest without claiming it was the
            # sole cause of the shortfall.
            if ko_pruned:
                return None, REASON_NO_PATH_KEEPOUT
            # Issue #6381: when every surviving escape stub of one end is a
            # dead end (no legal full-width edge or via out of its landing
            # node), THAT is why the search failed -- regardless of whether
            # the hard layer set also pruned stubs elsewhere.  Name it, so a
            # fine-pitch escape failure is not misreported as a layer
            # constraint (board 05 PHASE_B read ``no-path-layer-constrained``
            # because J2.2's inner-layer stubs were dropped, while the real
            # failure was U2.8's in-pad dead-end stubs).
            if not fat:

                def no_egress(stubs: list) -> bool:
                    return not any(
                        self._stub_has_egress(
                            stub,
                            net,
                            committed,
                            half,
                            clr,
                            net_class=net_class,
                            allow_vias=allow_vias,
                            via_in_pad_override=via_in_pad_override,
                        )
                        for stub in stubs
                    )

                if no_egress(stubs_a):
                    return None, "no-egress-start"
                if no_egress(stubs_b):
                    return None, "no-egress-end"
            # Issue #4979: same honesty contract for a hard layer constraint.
            # ``layer_pruned`` is set when the net's hard-avoided set actually
            # removed something from this search -- an escape stub, or a
            # candidate via landing layer -- so the decline names the layer
            # constraint as a CONTRIBUTING cause instead of reporting a bare
            # "no-path".  It is deliberately not a claim of sole causation
            # (the keepout wording above), and it is never a licence to ship
            # the forbidden copper: this branch returns no route at all.
            if layer_pruned:
                return None, REASON_NO_PATH_LAYER
            return None, REASON_NO_PATH

        # -- reconstruct ---------------------------------------------------
        chain: list[_State] = [end_state]
        moves: list[str] = []
        cur = end_state
        if end_pred is not None:
            # Issue #6975: an ``arrive-edge`` goal -- the last step is the
            # registered edge, not the goal node's own (possibly reversing)
            # best arrival.
            chain.append(end_pred)
            moves.append("move")
            cur = end_pred
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

    def _own_via_floor(self) -> float:
        """Centre-to-centre floor between two of ONE route's vias (issue #6957).

        A drill diameter plus the fab hole-to-hole minimum -- the same
        ``same_net_via_gap`` :class:`CommittedCopper` enforces against
        committed same-net vias -- floored by ``min_drill_clearance`` like the
        grid engines' ``_own_path_vias_clear`` (#6473).
        """
        rules = self.rules
        return rules.via_drill + max(
            rules.min_hole_to_hole, getattr(rules, "min_drill_clearance", 0.0)
        )

    def _own_path_vias_clear(self, site: Pt, chain: _ViaChain) -> bool:
        """May a via at ``site`` join the vias already on its path? (Issue #6957).

        ``chain`` is the path's earlier via sites (see ``path_vias`` in
        :meth:`_route_search`).  A via stack at one node (F -> In1 -> B) is
        one drill, so a site equal to ``site`` is skipped.  The comparison
        carries the lattice's usual :data:`LATTICE_CLEARANCE_EPSILON_MM`
        slack, so a pair exactly on the floor is legal.
        """
        floor = self._own_via_floor() - LATTICE_CLEARANCE_EPSILON_MM
        while chain is not None:
            other, chain = chain
            d = dist(site, other)
            if d > 1e-9 and d < floor:
                return False
        return True

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
        half: float = 0.0,
    ) -> list[Pt] | None:
        """Dogleg from the exact pad position to an emitted leg endpoint.

        Ordinary single-trace clearance (``extra = 0``) but with BOTH pair
        nets as "self" -- the attach necessarily runs between the pair's own
        pads.  Returns the accepted polyline (pad first) or ``None``.
        """
        from ..pairplan.attach import dogleg_attach_candidates
        from .coupled import pads_block_segment_grown

        if dist((pad.x, pad.y), target) <= 1e-9:
            return [(pad.x, pad.y)]
        blocker = ""
        for poly in dogleg_attach_candidates((pad.x, pad.y), target):
            ok = True
            for a, b in zip(poly, poly[1:], strict=False):
                if dist(a, b) <= 1e-9:
                    continue
                if pads_block_segment_grown(self.obstacles, a, b, layer, pair_nets, 0.0):
                    blocker = "pad"
                    ok = False
                    break
                if not committed.seg_clear(a, b, layer, net):
                    blocker = "committed-copper"
                    ok = False
                    break
                # Issue #6697: the other dogleg orientation may clear a
                # reserved via site / rule area the first one clips.
                if self._keepouts is not None and self._keepouts.segment_blocked(
                    a, b, layer, net, half
                ):
                    names = self._keepouts.blocking_names(a, b, layer, net, half)
                    blocker = "keepout " + ",".join(names)
                    ok = False
                    break
            if ok:
                return poly
        # Issue #6935: name what rejected the last candidate so a
        # ``pair-pad-attach`` decline says which check fired, and on what.
        self._attach_blocker = f"{pad.ref}.{pad.pin} on layer {layer}: {blocker}"
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
        layers: tuple[int, ...] | None = None,
        keep_clear: tuple[tuple[Pt, float], ...] = (),
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

        ``layers`` (issue #6444) overrides the layer the run is searched on.
        The fan-via gateway retry passes its gateway layer here, with the
        endpoint "pads" being the virtual via pads; ``None`` keeps the
        intersection of the four endpoint pads' layers.  ``keep_clear``
        (issue #6444) lists ``(via site, clearance)`` the fat SEARCH must
        treat as foreign obstacles although they belong to the pair's own
        nets: the gateway vias sit a via-to-via gap apart, wider than the
        pair pitch, so a centerline passing between them would lay each leg
        over the partner's via.  The finish gate still sees them as the pair's
        own copper (so each leg can attach to its via).
        """
        from .coupled import (
            assign_polarity,
            offset_polyline,
            seg_rect_dist,
            side_bit,
            trim_end_reversals,
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
        if layers is not None:
            common = tuple(sorted(set(common) & set(layers)))
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
            component_id=pc.pad_p_a.component_id,
            terminal_id=pc.pad_p_a.terminal_id,
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
            component_id=pc.pad_p_b.component_id,
            terminal_id=pc.pad_p_b.terminal_id,
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

        # Issue #6444: gateway vias are foreign to the fat search only (see
        # ``keep_clear``); a nested trial adds them under a sentinel net id
        # around each search call and rolls them back before the finish gate.
        def guard_open() -> tuple[int, int, int] | None:
            if not keep_clear:
                return None
            mark = committed.begin_trial()
            for site, clr in keep_clear:
                committed.add_via(site, _KEEP_CLEAR_NET, clr)
            return mark

        def guard_close(mark: tuple[int, int, int] | None) -> None:
            if mark is not None:
                committed.rollback_trial(mark)

        # Collect EVERY legal fat stub in the search radius, then downselect
        # for geometric diversity.  A count-capped nearest-first scan can
        # exhaust itself inside an enclosed pocket (the board-06 BGA guard
        # ring) and never reach the one dogleg that threads the way out.
        guard = guard_open()
        try:
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
        finally:
            guard_close(guard)
        stubs_a = _diverse_pair_stubs(raw_a, pc.mid_a, (pc.pad_p_a, pc.pad_n_a))
        if not stubs_a:
            return None, "pair-escape-a"
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

        from .coupled import clip_to_pad_entry, pads_block_segment_grown

        trace_w = getattr(pc.net_class, "trace_width", None) or self.rules.trace_width
        intra_clearance = pc.pitch - trace_w

        # Issue #6839: ``(partner pad is at end A, first centerline turn
        # next to that end)`` of the last ``pair-partner-pad`` decline.
        partner_fail: list[tuple[bool, Pt]] = []

        def finish(result: _RouteResult) -> tuple[_PairResult | None, str]:
            """Offset emission + verification of one fat centerline."""
            partner_fail.clear()
            if len(result.runs) != 1 or result.via_points:
                return None, "pair-not-planar"  # defensive: vias are off

            layer, raw_pts, _widths = result.runs[0]
            center = trim_end_reversals(raw_pts)
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
                attach_a = self._pair_attach(
                    pad_a, leg[0], layer, net, pair_nets, committed, trace_w / 2.0
                )
                if attach_a is None:
                    return None, "pair-pad-attach"
                attach_b = self._pair_attach(
                    pad_b, leg[-1], layer, net, pair_nets, committed, trace_w / 2.0
                )
                if attach_b is None:
                    return None, "pair-pad-attach"
                full = attach_a[:-1] + leg + list(reversed(attach_b))[1:]
                # Issue #6959: end each leg where it enters its own pad's
                # copper instead of running on to the pad centre.
                full = clip_to_pad_entry(full[::-1], pad_a, trace_w)[::-1]
                full = clip_to_pad_entry(full, pad_b, trace_w)

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
                    # Issue #4507: the fat centerline SEARCH deliberately skips
                    # HV pairwise widening (see ``coupled.committed_seg_clear_
                    # grown``'s docstring) so a genuine same-domain diff pair
                    # is never over-constrained by a multi-mm HV keep-out on
                    # top of its own pitch.  ``committed.seg_clear`` above
                    # already re-covers foreign COMMITTED trace/via copper
                    # pairwise-aware (it consults ``committed.pairwise``
                    # directly); ``pads_block_segment_grown`` two lines up
                    # never does -- it is the pure-scalar fat-pad check.  So a
                    # mapped foreign PAD's pairwise requirement was silently
                    # unchecked for the single-trace LEG this loop emits (the
                    # T4 residual's exact shape: a coupled/misdetected pair's
                    # trace passing a foreign HV pad at the scalar, not the
                    # pairwise, floor).  Checked at the LEG's own half-width
                    # with no extra surcharge, mirroring the non-fat search's
                    # own ``pairwise_pad_blocked`` call in ``_route_search``.
                    if self._pairwise is not None and self.obstacles.pairwise_pad_blocked(
                        a, b, layer, net, trace_w / 2.0, 0.0, self._pairwise
                    ):
                        return None, "pair-leg-pairwise-blocked"
                    # Issue #4605: the fat centerline search skips the keepout
                    # mask, so the EMITTED legs (offset body + attach doglegs)
                    # are verified against it here at their true half-width --
                    # a coupled pair never ships copper through a rule area.
                    if self._keepouts is not None and self._keepouts.segment_blocked(
                        a, b, layer, net, trace_w / 2.0
                    ):
                        return None, "pair-leg-keepout"
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
                    half_w, half_h = pad_half_extents(pad)
                    rect = (
                        pad.x - half_w,
                        pad.y - half_h,
                        pad.x + half_w,
                        pad.y + half_h,
                    )
                    for a, b in zip(full, full[1:], strict=False):
                        if dist(a, b) <= 1e-9:
                            continue
                        if seg_rect_dist(a, b, rect) < need:
                            # Issue #6839: remember WHICH end's first turn
                            # broke the floor so the caller can retry with
                            # the stubs through that turn removed.
                            partner_fail[:] = [
                                (
                                    pad.key in (pc.pad_p_a.key, pc.pad_n_a.key),
                                    center[1]
                                    if pad.key in (pc.pad_p_a.key, pc.pad_n_a.key)
                                    else center[-2],
                                )
                            ]
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
        #
        # Issue #6373: two different failures used to share the initial
        # ``pair-no-path`` value and were indistinguishable in the log.
        # ``pair-no-parity`` now means NO parity had stubs on both ends, so
        # no search ever ran; ``pair-no-path`` is reserved for an A* that
        # actually ran and was exhausted.
        reason = "pair-no-parity"
        for bit in sorted((True, False), key=parity_rank):
            if not by_bit_a[bit] or not by_bit_b[bit]:
                continue
            stubs_a_bit, stubs_b_bit = list(by_bit_a[bit]), list(by_bit_b[bit])
            # Issue #6839: a centerline whose first turn sits inside a pad
            # row breaks the partner-pad floor only after emission.  Retry
            # the search without the stubs through that turn (bounded) instead
            # of declining the pair outright.
            for _attempt in range(_PARTNER_PAD_RETRIES + 1):
                guard = guard_open()
                try:
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
                        stubs_override=(stubs_a_bit, stubs_b_bit),
                        # The exemption is for the perpendicular-gated stub
                        # doglegs ONLY: the A* body sees every pad -- including
                        # the pair's own -- as an obstacle, else the shortest fat
                        # path hugs an endpoint pad row and the emitted legs land
                        # on the partner's copper (board-06 MIPI lesson).
                        exempt_pads=frozenset(),
                    )
                finally:
                    guard_close(guard)
                if result is None:
                    reason = (
                        search_reason
                        if search_reason.startswith("pair-")
                        else f"pair-{search_reason}"
                    )
                    break
                pres, finish_reason = finish(result)
                if pres is not None:
                    return pres, "ok"
                reason = finish_reason
                if finish_reason != "pair-partner-pad" or not partner_fail:
                    break
                at_a, turn = partner_fail[0]
                pool = stubs_a_bit if at_a else stubs_b_bit
                kept = [s for s in pool if all(dist(v, turn) > 1e-6 for v in s[2])]
                if len(kept) == len(pool) or not kept:
                    break  # the turn is not on any stub: nothing to retry
                pool[:] = kept
        return None, reason

    # -- fan-via terminal gateways (issue #6444) --------------------------------

    def _gateway_pad_class_clear(
        self, a: Pt, b: Pt, layer: int | None, net: int, half: float, reqs: list[float] | None
    ) -> bool:
        """True when ``a-b`` keeps every other-net pad's OWN class clearance.

        The static masks space copper at the querying net's clearance; the
        pad side of a track-vs-pad pair is ``max(own, pad class)`` (#6395),
        which the post-route class-pad repair enforces only on single-ended
        copper.  Gateway copper is pair-owned and never moved, so it is held
        to the pad class here.  ``layer=None`` probes every layer (a via).
        """
        if reqs is None:
            return True
        from .coupled import seg_rect_dist

        reach = half + max(reqs, default=0.0) + 0.5
        x0, x1 = min(a[0], b[0]) - reach, max(a[0], b[0]) + reach
        y0, y1 = min(a[1], b[1]) - reach, max(a[1], b[1]) + reach
        for idx in self.obstacles.pads_near(x0, y0, x1, y1):
            pad = self.pads[idx]
            if pad.net == net:
                continue
            if layer is not None and layer not in self.obstacles.pad_layer_indices[idx]:
                continue
            hw, hh = pad_half_extents(pad)
            rect = (pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh)
            if seg_rect_dist(a, b, rect) < half + reqs[idx] - 1e-6:
                return False
        return True

    def _gateway_fan_ok(
        self,
        poly: list[Pt],
        layer: int,
        net: int,
        half: float,
        clr: float,
        committed: CommittedCopper,
        reqs: list[float] | None,
    ) -> bool:
        """Exact verification of one gateway fan leg (pad -> via, pad layer).

        The same gates a negotiated single-ended leg passes: Edge.Cuts, the
        static pad masks at the leg's class surcharge (the pair partner's
        pads are foreign here -- unlike the coupled body, a fan is a plain
        single trace), committed copper (which holds the partner's gateway
        via and fan during the check), keepout rule areas, HV pairwise pad
        keep-outs and the pad class floor.
        """
        from .coupled import pads_block_segment_grown

        extra = max(0.0, half + clr - self._agent_radius)
        for a, b in zip(poly, poly[1:], strict=False):
            if dist(a, b) <= 1e-9:
                continue
            if not self._escape_boundary.segment_clear(a, b, half):
                return False
            if pads_block_segment_grown(self.obstacles, a, b, layer, {net}, extra):
                return False
            if not committed.seg_clear(a, b, layer, net, half, clr):
                return False
            if self._keepouts is not None and self._keepouts.segment_blocked(
                a, b, layer, net, half
            ):
                return False
            if self._pairwise is not None and self.obstacles.pairwise_pad_blocked(
                a, b, layer, net, half, extra, self._pairwise
            ):
                return False
            if not self._gateway_pad_class_clear(a, b, layer, net, half, reqs):
                return False
        return True

    def _gateway_site_fans(
        self,
        site: GatewaySite,
        pad_p: Pad,
        pad_n: Pad,
        pad_layer: int,
        half: float,
        clr: float,
        committed: CommittedCopper,
        reqs: list[float] | None,
    ) -> tuple[list[Pt], list[Pt]] | None:
        """Legality oracle for one gateway site (``gateway.enumerate_gateways``).

        Checks, inside a rolled-back trial so each check sees the previous
        commit: P via, then N via (the via-to-via gap to P's), then P's fan
        (clear of N's via), then N's fan (clear of P's via and fan).  Via
        legality is :meth:`_via_ok_at` -- the negotiated-via rule, including
        the fab tier's via-in-pad gate -- plus the pad class floor.  Returns
        the two fan polylines, or ``None``.
        """
        via_r = self.rules.via_diameter / 2.0
        body = self._gateway_body_box(pad_p.ref)
        if body is not None and any(
            body[0] < v[0] < body[2] and body[1] < v[1] < body[3] for v in (site.via_p, site.via_n)
        ):
            return None
        mark = committed.begin_trial()
        try:
            for net, via in ((pad_p.net, site.via_p), (pad_n.net, site.via_n)):
                if not self._via_ok_at(via, net, committed, clearance=clr):
                    return None
                if not self._gateway_pad_class_clear(via, via, None, net, via_r, reqs):
                    return None
                committed.add_via(via, net, clr)
            fans: list[list[Pt]] = []
            for pad, via in ((pad_p, site.via_p), (pad_n, site.via_n)):
                accepted: list[Pt] | None = None
                for axis_first in (False, True):
                    poly = dogleg_points(pad.x, pad.y, via[0], via[1], axis_first=axis_first)
                    if self._gateway_fan_ok(poly, pad_layer, pad.net, half, clr, committed, reqs):
                        accepted = poly
                        break
                if accepted is None:
                    return None
                committed.add_run(pad_layer, accepted, pad.net, half, clr)
                fans.append(accepted)
            return fans[0], fans[1]
        finally:
            committed.rollback_trial(mark)

    def _gateway_body_box(self, ref: str | None) -> Rect | None:
        """Between-rows box a gateway via must stay OUT of, for part ``ref``.

        A fan via between the two pad rows of a dual-row part splits the
        part's own pours: on board 06rf a USB-C gateway between J1's A and B
        rows cut the F.Cu ``VBUS_USB`` pour in two and left the board with
        unconnected items.  So for a part whose SMD pads sit on exactly two
        distinct rows (or columns) -- a USB-C receptacle, a dual-row header,
        an SOIC -- the box spanning its pads is off-limits to gateway vias.
        Single-row parts (an HDMI receptacle's signal row), four-sided parts
        and area arrays (a dogbone via in a BGA ball field IS the standard
        escape; the pad masks keep it off every ball) are not restricted.
        ``None`` = no restriction.
        """
        if not ref:
            return None
        cache: dict[str, Rect | None] = self.__dict__.setdefault("_gateway_body_cache", {})
        if ref in cache:
            return cache[ref]
        # SMD pads only: a connector's through-hole shield/mounting pins sit
        # outside its signal rows and must not count as a row.
        pads = [p for p in self.pads if p.ref == ref and not p.through_hole and p.drill <= 0]
        box: Rect | None = None
        if len(pads) >= 4:
            xs = {round(p.x, 3) for p in pads}
            ys = {round(p.y, 3) for p in pads}
            if len(xs) == 2 or len(ys) == 2:
                box = (min(xs), min(ys), max(xs), max(ys))
        cache[ref] = box
        return box

    def _gateway_layers(self, pc: CoupledConnection) -> list[int]:
        """Candidate gateway layers of a segment, in index order.

        Every routable layer the pair's class may use that is NOT already a
        layer all four endpoint pads share (the planar attempt searched
        those).  Empty when this engine cannot emit a via for the class (a
        through via lands on both outer layers, see ``_route_search``'s
        via-span guard) -- the 2-layer / single-layer decline.
        """
        if self.num_layers < 2:
            return []
        blocked = self._hard_avoided_layers(pc.net_class)
        if blocked & {0, self.num_layers - 1}:
            return []
        try:
            routable = set(self.layer_stack.get_routable_indices())
        except Exception:
            routable = set(range(self.num_layers))
        planar = set.intersection(
            *(
                set(self._pad_layer_indices(p))
                for p in (pc.pad_p_a, pc.pad_n_a, pc.pad_p_b, pc.pad_n_b)
            )
        )
        return sorted(
            layer
            for layer in routable
            if 0 <= layer < self.num_layers and layer not in blocked and layer not in planar
        )

    def _route_pair_gateway(
        self,
        pc: CoupledConnection,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        deadline: float | None = None,
        vias_used: int = 0,
        only_layers: list[int] | None = None,
    ) -> tuple[_PairResult | None, str]:
        """Couple a declined segment on another layer through fan vias (#6444).

        For each candidate gateway layer ``L``: at each end whose pads do not
        own ``L``, enumerate legal gateways (one through via per net plus a
        verified pad-layer fan leg each, :mod:`.gateway`); then route the
        ``MAX_GATEWAY_COMBOS`` cheapest handedness-consistent end-A x end-B
        combinations with the unchanged pair agent (:meth:`_route_pair_impl`,
        ``layers=(L,)``) between virtual via pads, with both gateways
        committed in a trial so the coupled legs are verified against the
        partner's vias.  The first success is returned with its fans and vias
        as pair-owned copper; the trial is always rolled back (the caller
        commits the result, all or nothing -- #3906).

        P and N get the same via count by construction (one per gatewayed
        end each); a count above the protocol's ``max_vias_per_net``
        (:attr:`pair_gateway_limits`) declines before any search.  Issue #6834:
        the limit is per NET, so ``vias_used`` (the most vias either net of
        this pair already carries on its other chain segments this pass)
        counts against it too.  The fans
        are pruned to the protocol's per-terminal uncoupled budget.
        Diagnostics (layer, gateway per end, via counts, candidates per
        terminal) land in :attr:`pair_gateway_log` either way.
        """
        from ..pairplan.gateway import (
            enumerate_gateways,
            rank_gateway_combos,
            rank_through_combos,
        )

        net_p, net_n = pc.pad_p_a.net, pc.pad_n_a.net
        trace_w = getattr(pc.net_class, "trace_width", None) or self.rules.trace_width
        half = trace_w / 2.0
        _h, clr = self._conn_geometry(pc.net_class, net_p)
        clr = max(clr, self._conn_geometry(pc.net_class, net_n)[1])
        max_fan, max_vias = self.pair_gateway_limits.get(pc.key, (None, None))
        via_r = self.rules.via_diameter / 2.0
        via_gap = max(
            self._via_via_gap,
            self.rules.via_diameter + clr,
            self._same_net_via_gap,
        )
        reqs = None
        if self.pad_class_clearances is not None:
            from .class_pad_repair import pad_class_requirements

            reqs = pad_class_requirements(self)

        self.pair_gateway_ends.pop(pc.key, None)
        layers = self._gateway_layers(pc)
        if only_layers is not None:
            layers = [layer for layer in layers if layer in only_layers]
        if not layers:
            self.pair_gateway_log[pc.key] = "gateway: no other routable layer"
            return None, "pair-gateway-no-layer"

        ends = (
            (pc.pad_p_a, pc.pad_n_a, pc.mid_b),
            (pc.pad_p_b, pc.pad_n_b, pc.mid_a),
        )
        notes: list[str] = []
        reason = "pair-gateway-no-candidates"
        # Issue #6836: layers that reached the combination stage, kept for the
        # pass-under group walked after EVERY layer's ``against`` group.
        staged: list[tuple[int, str, list[list[Gateway | None]], int]] = []
        legal_ends: list[int | None] = [0, 0]
        # Issue #6956: every combination's decline, per group, so the line
        # names each blocker -- not only the last combination's.
        tally: dict[str, dict[str, int]] = {}
        blockers: dict[str, int] = {}

        def record(group: str, why: str) -> None:
            reasons = tally.setdefault(group, {})
            key = why.removeprefix("pair-")
            reasons[key] = reasons.get(key, 0) + 1
            if key == "pad-attach" and self._attach_blocker:
                blocker = self._attach_blocker
                blockers[blocker] = blockers.get(blocker, 0) + 1

        for layer in layers:
            lname = self._layer_name(layer)
            per_end: list[list[Gateway | None]] = []
            counts: list[str] = []
            for end_idx, (pad_p, pad_n, toward) in enumerate(ends):
                own = set(self._pad_layer_indices(pad_p)) & set(self._pad_layer_indices(pad_n))
                if layer in own:
                    per_end.append([None])
                    counts.append("pad")
                    legal_ends[end_idx] = None
                    continue
                if not own:
                    per_end.append([])
                    counts.append("0")
                    continue
                pad_layer = min(own)

                def check(
                    site: GatewaySite,
                    pad_p: Pad = pad_p,
                    pad_n: Pad = pad_n,
                    pad_layer: int = pad_layer,
                ) -> tuple[list[Pt], list[Pt]] | None:
                    return self._gateway_site_fans(
                        site, pad_p, pad_n, pad_layer, half, clr, committed, reqs
                    )

                found, legal = enumerate_gateways(
                    (pad_p.x, pad_p.y),
                    (pad_n.x, pad_n.y),
                    toward,
                    layer,
                    check,
                    pitch=pc.pitch,
                    via_gap=via_gap,
                    max_fan_mm=max_fan,
                )
                per_end.append(list(found))
                counts.append(f"{len(found)}/{legal}")
                have = legal_ends[end_idx]
                if have is not None:
                    legal_ends[end_idx] = max(have, legal)
            self.pair_gateway_ends[pc.key] = (legal_ends[0], legal_ends[1])
            gated = sum(1 for e in per_end if e and e[0] is not None)
            notes.append(f"{lname}: candidates A={counts[0]} B={counts[1]}")
            if gated == 0:
                continue
            if max_vias is not None and gated + vias_used > max_vias:
                reason = "pair-gateway-via-limit"
                notes[-1] += (
                    f" (needs {gated} via(s)/net"
                    f"{f' + {vias_used} on other segments' if vias_used else ''}"
                    f" > limit {max_vias})"
                )
                continue
            if not per_end[0] or not per_end[1]:
                continue
            reason = "pair-gateway-no-path"
            staged.append((layer, lname, per_end, gated))
            for ga, gb in rank_gateway_combos(per_end[0], per_end[1]):
                if deadline is not None and time.monotonic() >= deadline:
                    self.pair_gateway_log[pc.key] = "gateway: " + "; ".join(notes + ["deadline"])
                    return None, "pair-gateway-deadline"
                pres, why = self._try_gateway_combo(
                    pc,
                    layer,
                    (ga, gb),
                    committed=committed,
                    history=history,
                    present=present,
                    half=half,
                    clr=clr,
                    via_r=via_r,
                    reqs=reqs,
                )
                if pres is None:
                    reason = f"pair-gateway-{why.removeprefix('pair-')}"
                    record("against", why)
                    continue
                desc = []
                for tag, g in (("A", ga), ("B", gb)):
                    desc.append(f"{tag}: {'pad layer' if g is None else g.describe()}")
                pres.gateway = (
                    f"gateway {lname} ({'; '.join(desc)}); vias P/N={gated}/{gated}; "
                    + "; ".join(notes)
                )
                self.pair_gateway_log[pc.key] = pres.gateway
                return pres, "ok"
        # Issue #6836: the pass-under group.  Tried only after every
        # combination above (on every layer) declined, so a segment that
        # couples through an ``against`` combination keeps it -- and its
        # copper -- exactly.  A ``through`` end's body reaches its via pair
        # from the pad side, under pads that do not own the gateway layer
        # (06rf J1: from U1 under the B and A rows on B.Cu).
        for layer, lname, per_end, gated in staged:
            for combo in rank_through_combos(per_end[0], per_end[1]):
                if deadline is not None and time.monotonic() >= deadline:
                    self.pair_gateway_log[pc.key] = "gateway: " + "; ".join(notes + ["deadline"])
                    return None, "pair-gateway-deadline"
                ga, gb = combo.ga, combo.gb
                pres, why = self._try_gateway_combo(
                    pc,
                    layer,
                    (ga, gb),
                    committed=committed,
                    history=history,
                    present=present,
                    half=half,
                    clr=clr,
                    via_r=via_r,
                    reqs=reqs,
                    through=(combo.through_a, combo.through_b),
                )
                if pres is None:
                    reason = f"pair-gateway-pass-under-{why.removeprefix('pair-')}"
                    record("pass-under", why)
                    continue
                desc = []
                for tag, g, thr in (("A", ga, combo.through_a), ("B", gb, combo.through_b)):
                    text = "pad layer" if g is None else g.describe()
                    desc.append(f"{tag}: {text}{' pass-under' if thr else ''}")
                pres.gateway = (
                    f"gateway {lname} ({'; '.join(desc)}); vias P/N={gated}/{gated}; "
                    + "; ".join(notes)
                )
                self.pair_gateway_log[pc.key] = pres.gateway
                return pres, "ok"
        declined = f"gateway declined ({reason}): " + "; ".join(notes)
        if reason.endswith("pad-attach") and self._attach_blocker:
            declined += f"; pad-attach blocked at {self._attach_blocker}"
        summary = _gateway_decline_summary(tally, blockers)
        if summary:
            declined += f"; {summary}"
        self.pair_gateway_log[pc.key] = declined
        return None, reason

    def _try_gateway_combo(
        self,
        pc: CoupledConnection,
        layer: int,
        combo: tuple[Gateway | None, Gateway | None],
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        half: float,
        clr: float,
        via_r: float,
        reqs: list[float] | None,
        through: tuple[bool, bool] = (False, False),
    ) -> tuple[_PairResult | None, str]:
        """Route one (gateway A, gateway B) combination; see the caller.

        ``through`` (issue #6836) marks the ends whose body reaches the via
        pair from the pad side (pass-under): their anchor sits between the
        vias and the pads instead of beyond the vias.
        """
        ga, gb = combo
        ends = ((ga, pc.pad_p_a, pc.pad_n_a), (gb, pc.pad_p_b, pc.pad_n_b))
        mark = committed.begin_trial()
        try:
            # Commit end A, then RE-VERIFY end B against it (the two ends'
            # vias and fans were each checked alone at enumeration time).
            for g, pad_p, pad_n in ends:
                if g is None:
                    continue
                pad_layer = min(
                    set(self._pad_layer_indices(pad_p)) & set(self._pad_layer_indices(pad_n))
                )
                fans = self._gateway_site_fans(
                    g.site, pad_p, pad_n, pad_layer, half, clr, committed, reqs
                )
                if fans is None or (tuple(fans[0]), tuple(fans[1])) != (g.fan_p, g.fan_n):
                    return None, "pair-gateway-conflict"
                for net, via in ((pad_p.net, g.site.via_p), (pad_n.net, g.site.via_n)):
                    committed.add_via(via, net, clr)
                committed.add_run(pad_layer, list(g.fan_p), pad_p.net, half, clr)
                committed.add_run(pad_layer, list(g.fan_n), pad_n.net, half, clr)

            def vpad(g: Gateway | None, pad: Pad, via: Pt | None) -> Pad:
                if g is None or via is None:
                    return pad
                return replace(pad, x=via[0], y=via[1], width=0.0, height=0.0, through_hole=True)

            # The coupled body starts just beyond each via pair, along the
            # gateway heading, where its whole envelope clears both vias; the
            # unchanged ``_pair_attach`` doglegs join each via to its leg.
            reach = pc.pitch / 2.0 + half + clr + via_r + 1e-3
            anchors = [
                None if g is None else self._gateway_anchor(g.site, reach, through=thr)
                for g, thr in zip((ga, gb), through, strict=True)
            ]
            if any(g is not None and a is None for g, a in zip((ga, gb), anchors, strict=True)):
                return None, "pair-gateway-anchor"
            vpc = replace(
                pc,
                pad_p_a=vpad(ga, pc.pad_p_a, ga.site.via_p if ga else None),
                pad_n_a=vpad(ga, pc.pad_n_a, ga.site.via_n if ga else None),
                pad_p_b=vpad(gb, pc.pad_p_b, gb.site.via_p if gb else None),
                pad_n_b=vpad(gb, pc.pad_n_b, gb.site.via_n if gb else None),
                anchor_a=anchors[0] if ga is not None else pc.anchor_a,
                anchor_b=anchors[1] if gb is not None else pc.anchor_b,
            )
            keep = tuple(
                (site, clr)
                for g in (ga, gb)
                if g is not None
                for site in (g.site.via_p, g.site.via_n)
            )
            pres, reason = self._route_pair_impl(
                vpc,
                committed=committed,
                history=history,
                present=present,
                layers=(layer,),
                keep_clear=keep,
            )
        finally:
            committed.rollback_trial(mark)
        if pres is None:
            return None, reason

        # The coupled legs were verified against committed copper at the
        # board-global trace half-width; hold them to the class geometry
        # against the partner's gateway vias explicitly.
        vias: list[tuple[int, Pt]] = []
        for g, pad_p, pad_n in ends:
            if g is not None:
                vias.extend(((pad_p.net, g.site.via_p), (pad_n.net, g.site.via_n)))
        for net, _layer, pts in pres.runs:
            for a, b in zip(pts, pts[1:], strict=False):
                for vnet, site in vias:
                    if vnet == net:
                        continue
                    if seg_seg_dist(a, b, site, site) < half + via_r + clr - 1e-6:
                        return None, "pair-gateway-leg-via"
                if not self._gateway_pad_class_clear(a, b, layer, net, half, reqs):
                    return None, "pair-gateway-leg-pad-class"

        # Assemble the pair-owned copper: fan A + coupled leg + fan B per net.
        runs: list[tuple[int, int, list[Pt]]] = []
        routes: list[Route] = []
        trace_w = 2.0 * half
        for idx, (net, leg_layer, leg) in enumerate(pres.runs):
            pieces: list[tuple[int, list[Pt]]] = []
            pol_p = idx == 0
            for g, pad_p, pad_n in ends:
                if g is None:
                    continue
                pad_layer = min(
                    set(self._pad_layer_indices(pad_p)) & set(self._pad_layer_indices(pad_n))
                )
                pieces.append((pad_layer, list(g.fan_p if pol_p else g.fan_n)))
            pieces.append((leg_layer, leg))
            route = Route(net=net, net_name=pres.routes[idx].net_name)
            for piece_layer, poly in pieces:
                emitted = self._emit_leg(net, route.net_name, poly, piece_layer, trace_w)
                if emitted is None:
                    return None, "pair-gateway-empty-route"
                route.segments.extend(emitted.segments)
                runs.append((net, piece_layer, poly))
            top = self.layer_stack.index_to_layer_enum(0)
            bottom = self.layer_stack.index_to_layer_enum(self.num_layers - 1)
            for vnet, site in vias:
                if vnet != net:
                    continue
                route.vias.append(
                    Via(
                        x=site[0],
                        y=site[1],
                        drill=self.rules.via_drill,
                        diameter=self.rules.via_diameter,
                        layers=(top, bottom),
                        net=net,
                        net_name=route.net_name,
                    )
                )
            routes.append(route)
        if len(routes[0].vias) != len(routes[1].vias):
            return None, "pair-gateway-via-count"
        return _PairResult(routes=routes, runs=runs, resources=pres.resources, vias=vias), "ok"

    def _gateway_anchor(
        self, site: GatewaySite, reach: float, *, through: bool = False
    ) -> Pt | None:
        """Start point of a gateway's coupled body (issue #6444).

        The ideal start is :func:`_gateway_anchor` (just past the via pair
        along the heading).  It is snapped to the nearest lattice node that
        is still ``reach`` clear of both vias and ahead of their midpoint:
        an off-node start makes the fat stub a dogleg with a sub-0.01 mm jog
        whose ``+/- pitch/2`` offset collapses (``pair-offset-degenerate``),
        while a node start leaves straight octilinear stubs.  Falls back to
        the ideal point when no node qualifies within 1 mm of it.

        ``through`` (issue #6836, pass-under) mirrors all of it onto the
        reverse heading: the start sits on the pad side of the via pair.
        """
        from ..pairplan.gateway import HEADINGS

        ideal = _gateway_anchor(site, reach, through=through)
        if ideal is None:
            return None
        h = HEADINGS[site.heading]
        if through:
            h = (-h[0], -h[1])
        g = ((site.via_p[0] + site.via_n[0]) / 2.0, (site.via_p[1] + site.via_n[1]) / 2.0)
        best: tuple[float, Pt] | None = None
        for point in self.build().nodes.values():
            d = dist(point, ideal)
            if d > 1.0 or (best is not None and d >= best[0]):
                continue
            if (point[0] - g[0]) * h[0] + (point[1] - g[1]) * h[1] <= 1e-9:
                continue
            if dist(point, site.via_p) < reach or dist(point, site.via_n) < reach:
                continue
            best = (d, point)
        return ideal if best is None else best[1]

    # -- one-ended hybrid trunk + legs (issue #6446) ----------------------------

    def _pad_field_box(self, ref: str | None) -> Rect | None:
        """Box spanning every pad (extents included) of part ``ref``, or ``None``."""
        if not ref:
            return None
        cache: dict[str, Rect | None] = self.__dict__.setdefault("_pad_field_cache", {})
        if ref not in cache:
            boxes = []
            for pad in self.pads:
                if pad.ref == ref:
                    hw, hh = pad_half_extents(pad)
                    boxes.append((pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh))
            cache[ref] = (
                (
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                )
                if boxes
                else None
            )
        return cache[ref]

    def _search_hybrid_leg(
        self,
        item: Connection,
        strict: LatticePairwise | None,
        floor: float,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
    ) -> tuple[_RouteResult | None, str]:
        """One single-ended hybrid leg, held to every pad's own class.

        The same search a partner-aware fallback leg gets (see
        :meth:`_partner_aware_legs`): ``strict`` / ``floor`` come from
        :meth:`_strict_pad_pairwise`.
        """
        previous = self._pairwise
        if strict is not None:
            self._pairwise = strict
        self._search_clearance_floor = floor
        try:
            return self._search_single(
                item,
                committed=committed,
                history=history,
                present=present,
                escape_plan=escape_plan,
            )
        finally:
            self._pairwise = previous
            self._search_clearance_floor = 0.0

    def _route_pair_hybrid(
        self,
        pc: CoupledConnection,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
        deadline: float | None = None,
        vias_used: int = 0,
    ) -> tuple[_PairResult | None, str]:
        """Couple what can couple of a declined segment: trunk + legs (#6446).

        Runs after the planar attempt AND the 2a gateway retry declined.  The
        coupled **trunk** runs from the *easy end* (planar on its pad layer)
        to a **trunk start** outside the *hard end*'s pad field
        (:func:`~..pairplan.hybrid.propose_trunk_starts`), and each net
        reaches the start through its own single-ended leg (vias allowed),
        searched like a partner-aware fallback leg.

        The hard end comes from the gateway retry's per-end verdict
        (:attr:`pair_gateway_ends`): the end with no legal fan-via site on any
        layer tried.  When BOTH ends are hard the segment is left to the
        fallback (v1 hybrids one end only).  When neither is -- the gateway
        retry found sites at both ends but no body joined them (a BGA ring
        that only a single trace crosses) -- each end is tried as the hard one,
        the part with more pads (the area array) first.

        Per (trunk layer, start, P/N ordering), in a rolled-back trial: the
        trunk is routed by the unchanged pair agent between virtual site pads
        (:meth:`_try_hybrid_start`), then P's leg, then N's leg against the
        committed trunk.  The first candidate whose trunk is at least
        :data:`MIN_TRUNK_PITCHES` pitches long, whose legs each stay within the
        per-terminal cap (the declared ``max_uncoupled_per_terminal_mm``, else
        the :data:`HYBRID_MAX_LEG_MM` search bound) and whose vias fit the
        protocol's per-net limit wins; **an over-budget hybrid is never
        emitted**.  The caller commits the result, all or nothing (#3906).
        The outcome text for the log lands in :attr:`pair_hybrid_log` either
        way.  On success the second return value is the hard terminal
        (``"U4.B1"``, the outcome label's tail), else the decline reason.
        """
        key = pc.key
        verdict = self.pair_gateway_ends.get(key)
        if verdict is None:
            self.pair_hybrid_log[key] = (
                "hybrid declined (hybrid-no-verdict): the gateway retry recorded no per-end verdict"
            )
            return None, "pair-hybrid-no-verdict"
        zero = [legal == 0 for legal in verdict]
        if all(zero):
            self.pair_hybrid_log[key] = (
                f"hybrid declined (hybrid-both-ends-hard): both ends hard "
                f"({pc.pad_p_a.ref}.{pc.pad_p_a.pin} / {pc.pad_p_b.ref}.{pc.pad_p_b.pin}); "
                "v1 hybrids one end only"
            )
            return None, "pair-hybrid-both-ends-hard"
        if any(zero):
            order = [zero[0]]  # the end with no legal site is the hard one
        else:
            counts = [
                sum(1 for p in self.pads if p.ref == pad.ref) for pad in (pc.pad_p_a, pc.pad_p_b)
            ]
            order = [True, False] if counts[0] >= counts[1] else [False, True]
        notes: list[str] = []
        reason = "pair-hybrid-no-path"
        for hard_a in order:
            pres, why, note = self._route_pair_hybrid_end(
                pc,
                hard_a=hard_a,
                committed=committed,
                history=history,
                present=present,
                escape_plan=escape_plan,
                deadline=deadline,
                vias_used=vias_used,
            )
            if pres is not None:
                return pres, why
            notes.append(note)
            if reason == "pair-hybrid-no-path" or why == "pair-hybrid-leg-budget":
                reason = why
            if why == "pair-hybrid-deadline":
                break
        self.pair_hybrid_log[key] = (
            f"hybrid declined ({reason.removeprefix('pair-')}): " + "; ".join(notes)
        )
        return None, reason

    def _route_pair_hybrid_end(
        self,
        pc: CoupledConnection,
        *,
        hard_a: bool,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
        deadline: float | None,
        vias_used: int,
    ) -> tuple[_PairResult | None, str, str]:
        """One hybrid attempt with end A (``hard_a``) or end B as the hard one.

        Returns ``(result, hard terminal | reason, log note)``; the log line
        of a success is written here.  See :meth:`_route_pair_hybrid`.
        """
        from ..pairplan.hybrid import (
            HYBRID_MAX_LEG_MM,
            MAX_CONSECUTIVE_TRUNK_MISSES,
            MAX_TRUNK_SEARCHES,
            propose_trunk_starts,
        )

        key = pc.key
        if hard_a:
            hard_p, hard_n, easy_p, easy_n, easy_mid = (
                pc.pad_p_a,
                pc.pad_n_a,
                pc.pad_p_b,
                pc.pad_n_b,
                pc.mid_b,
            )
        else:
            hard_p, hard_n, easy_p, easy_n, easy_mid = (
                pc.pad_p_b,
                pc.pad_n_b,
                pc.pad_p_a,
                pc.pad_n_a,
                pc.mid_a,
            )
        terminal = f"{hard_p.ref}.{hard_p.pin}"
        field_box = self._pad_field_box(hard_p.ref)
        if field_box is None:
            return None, "pair-hybrid-no-pad-field", f"hard terminal {terminal} has no part"

        net_p, net_n = pc.pad_p_a.net, pc.pad_n_a.net
        trace_w = getattr(pc.net_class, "trace_width", None) or self.rules.trace_width
        half = trace_w / 2.0
        clr = max(
            self._conn_geometry(pc.net_class, net_p)[1],
            self._conn_geometry(pc.net_class, net_n)[1],
        )
        max_fan, max_vias = self.pair_gateway_limits.get(key, (None, None))
        cap = max_fan if max_fan is not None else HYBRID_MAX_LEG_MM
        cap_text = f"{cap:.1f} mm, {'declared' if max_fan is not None else 'engine bound'}"
        # The legs end ``site_gap`` apart; each site must clear the PARTNER's
        # trunk body (``pitch`` apart, so ``site_gap / 2 + pitch / 2`` from
        # it) by a full trace + clearance, or its leg could not reach it.
        site_gap = 2.0 * (trace_w + clr) - pc.pitch + 0.02
        starts = propose_trunk_starts(
            (hard_p.x, hard_p.y),
            (hard_n.x, hard_n.y),
            easy_mid,
            field_box,
            site_gap=site_gap,
            max_leg_mm=cap,
        )
        if not starts:
            return (
                None,
                "pair-hybrid-no-trunk-start",
                f"hard terminal {terminal}: no trunk start within {cap_text} of the pads",
            )

        blocked = self._hard_avoided_layers(pc.net_class)
        hard_layers = set(self._pad_layer_indices(hard_p)) & set(self._pad_layer_indices(hard_n))
        trunk_layers = sorted(
            (
                set(self._pad_layer_indices(easy_p))
                & set(self._pad_layer_indices(easy_n))
                & set(range(self.num_layers))
            )
            - blocked,
            key=lambda layer: (layer not in hard_layers, layer),
        )
        if not trunk_layers:
            return (
                None,
                "pair-hybrid-no-trunk-layer",
                f"hard terminal {terminal}: the easy end's pads share no usable layer",
            )

        items: dict[str, Connection] = {
            pol: ((key, pol), real, real, pc.net_class)
            for pol, real in (("P", hard_p), ("N", hard_n))
        }
        strict, floor = self._strict_pad_pairwise(pc, items)
        reqs = None
        if self.pad_class_clearances is not None:
            from .class_pad_repair import pad_class_requirements

            reqs = pad_class_requirements(self)
        shortest: tuple[float, float] | None = None
        reason = "pair-hybrid-no-path"
        searches = 0
        for layer in trunk_layers:
            misses = 0
            for start in starts:
                if deadline is not None and time.monotonic() >= deadline:
                    return None, "pair-hybrid-deadline", f"hard terminal {terminal}: deadline"
                # Bounded: one fat A* per trial, and a trunk that finds no
                # way several starts running will not find one farther out.
                if searches >= MAX_TRUNK_SEARCHES or misses >= MAX_CONSECUTIVE_TRUNK_MISSES:
                    break
                searches += 1
                pres, why, legs, trunk_mm = self._try_hybrid_start(
                    pc,
                    hard_a=hard_a,
                    layer=layer,
                    start=start,
                    committed=committed,
                    history=history,
                    present=present,
                    escape_plan=escape_plan,
                    strict=strict,
                    floor=floor,
                    reqs=reqs,
                    cap=cap,
                    max_vias=max_vias,
                    vias_used=vias_used,
                    half=half,
                    clr=clr,
                )
                # Only a failure of the trunk body counts (an exhausted A*, or a
                # body through a pad's class floor): the easy end itself is then
                # the probable blocker.  A site-local failure (pad attach,
                # intra-pair clearance) says nothing about a farther start.
                misses = (
                    misses + 1
                    if why.startswith(("pair-hybrid-trunk-no-path", "pair-hybrid-trunk-pad-class"))
                    else 0
                )

                if legs is not None and (shortest is None or max(legs) < max(shortest)):
                    shortest = legs
                if pres is None:
                    # An over-budget leg is the informative decline.
                    if why == "pair-hybrid-leg-budget" or reason == "pair-hybrid-no-path":
                        reason = why
                    continue
                lens = legs or (0.0, 0.0)
                self.pair_hybrid_log[key] = (
                    f"hybrid {self._layer_name(layer)} trunk from {terminal}: "
                    f"trunk {trunk_mm:.1f} mm, {start.describe()}; "
                    f"legs P/N={lens[0]:.1f}/{lens[1]:.1f} mm (cap {cap_text}); "
                    f"vias P/N={sum(1 for v, _s in pres.vias if v == net_p)}/"
                    f"{sum(1 for v, _s in pres.vias if v == net_n)}"
                )
                return pres, terminal, ""
        if shortest is not None:
            note = (
                f"hard terminal {terminal}: shortest legs found P/N="
                f"{shortest[0]:.1f}/{shortest[1]:.1f} mm vs cap {cap_text}"
            )
        else:
            note = (
                f"hard terminal {terminal}: no leg routed from {searches} trunk search(es) "
                f"({reason.removeprefix('pair-hybrid-')})"
            )
        return None, reason, note

    def _try_hybrid_start(
        self,
        pc: CoupledConnection,
        *,
        hard_a: bool,
        layer: int,
        start: Any,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
        strict: LatticePairwise | None,
        floor: float,
        reqs: list[float] | None,
        cap: float,
        max_vias: int | None,
        vias_used: int,
        half: float,
        clr: float,
    ) -> tuple[_PairResult | None, str, tuple[float, float] | None, float]:
        """One (trunk layer, trunk start, ordering) candidate; see the caller.

        Returns ``(result, reason, leg lengths, trunk length)``; the leg
        lengths are set once both legs routed (also on a budget decline, for
        the log).  Everything is rolled back; the caller commits the winner.
        """
        from ..pairplan.hybrid import MIN_TRUNK_PITCHES

        hard_p, hard_n = (pc.pad_p_a, pc.pad_n_a) if hard_a else (pc.pad_p_b, pc.pad_n_b)
        measure = self.pair_length_measure or _default_pair_length
        layer_enum = self.layer_stack.index_to_layer_enum(layer)

        def vsite(real: Pad, pt: Pt) -> Pad:
            return replace(
                real,
                x=pt[0],
                y=pt[1],
                width=0.0,
                height=0.0,
                layer=layer_enum,
                through_hole=False,
                drill=0.0,
            )

        # The coupled body starts at a lattice node beyond the sites along the
        # trunk heading (where its envelope clears both), joined to them by
        # the unchanged ``_pair_attach`` doglegs (a gateway's anchor, #6444).
        reach = dist(start.site_p, start.site_n) + pc.pitch / 2.0 + half
        placed = self._hybrid_anchor(start, reach)
        if placed is None:
            return None, "pair-hybrid-anchor", None, 0.0
        anchor, site_p, site_n = placed
        v_p, v_n = vsite(hard_p, site_p), vsite(hard_n, site_n)
        vpc = (
            replace(pc, pad_p_a=v_p, pad_n_a=v_n, anchor_a=anchor)
            if hard_a
            else replace(pc, pad_p_b=v_p, pad_n_b=v_n, anchor_b=anchor)
        )
        mark = committed.begin_trial()
        try:
            trunk, why = self._route_pair_impl(
                vpc, committed=committed, history=history, present=present, layers=(layer,)
            )
            if trunk is None:
                return None, f"pair-hybrid-trunk-{why.removeprefix('pair-')}", None, 0.0
            trunk_mm = min(
                sum(dist(a, b) for a, b in zip(pts, pts[1:], strict=False))
                for _n, _l, pts in trunk.runs
            )
            if trunk_mm < MIN_TRUNK_PITCHES * pc.pitch:
                return None, "pair-hybrid-trunk-short", None, trunk_mm
            # Pair-owned copper is never moved by the class-pad repair (and the
            # #6489 growth rounds cannot steer a fat trunk), so the trunk is
            # held to every pad's own class here, exactly as a gateway's body
            # is: a trunk that would leave a shortfall behind is declined by
            # name instead of coupling and being demoted after the fact.
            for net, run_layer, pts in trunk.runs:
                for a, b in zip(pts, pts[1:], strict=False):
                    if not self._gateway_pad_class_clear(a, b, run_layer, net, half, reqs):
                        return None, "pair-hybrid-trunk-pad-class", None, trunk_mm
            for net, run_layer, pts in trunk.runs:
                committed.add_run(run_layer, pts, net, half, clr)
            legs: dict[str, _RouteResult] = {}
            for pol, real, virt in (("P", hard_p, v_p), ("N", hard_n, v_n)):
                item: Connection = ((pc.key, pol), real, virt, pc.net_class)
                res, why = self._search_hybrid_leg(
                    item,
                    strict,
                    floor,
                    committed=committed,
                    history=history,
                    present=present,
                    escape_plan=escape_plan,
                )
                if res is None:
                    return None, f"pair-hybrid-leg-{why.removeprefix('pair-')}", None, trunk_mm
                end = res.runs[-1][1][-1] if res.runs and res.runs[-1][1] else None
                if end is None or dist(end, (virt.x, virt.y)) > 1e-6:
                    return None, "pair-hybrid-leg-end", None, trunk_mm
                legs[pol] = res
                self._commit_single(item, res, {}, committed)
        finally:
            committed.rollback_trial(mark)

        lens = (measure(legs["P"].route), measure(legs["N"].route))
        if max(lens) > cap + 1e-9:
            return None, "pair-hybrid-leg-budget", lens, trunk_mm
        vias_p, vias_n = len(legs["P"].route.vias), len(legs["N"].route.vias)
        if max_vias is not None and max(vias_p, vias_n) + vias_used > max_vias:
            return None, "pair-hybrid-via-limit", lens, trunk_mm

        # The legs too: the search held them to the pad classes, but a leg is
        # pair-owned from here on and no later repair may move it.
        for pol in ("P", "N"):
            leg_net = hard_p.net if pol == "P" else hard_n.net
            for run_layer, pts, widths in legs[pol].runs:
                for (a, b), w in zip(zip(pts, pts[1:], strict=False), widths, strict=False):
                    if dist(a, b) > 1e-9 and not self._gateway_pad_class_clear(
                        a, b, run_layer, leg_net, w / 2.0, reqs
                    ):
                        return None, "pair-hybrid-leg-pad-class", lens, trunk_mm

        # Junction: each leg against everything of the PARTNER's -- its trunk
        # and its leg.  The searches above held the legs to the class
        # clearance, but a hybrid leg that skims the partner is "within the
        # pair" to the checker, which wants the pair's own intra-pair gap
        # (``pitch - trace``, raised to the fab minimum), so that is the floor
        # here, with a micron of margin over the checker's rounding.
        need = 2.0 * half + max(clr, pc.pitch - 2.0 * half) + 1e-3
        partner: dict[int, list[tuple[Pt, Pt, int]]] = {}
        for net, run_layer, pts in trunk.runs:
            partner.setdefault(net, []).extend(
                (a, b, run_layer) for a, b in zip(pts, pts[1:], strict=False) if dist(a, b) > 1e-9
            )
        for pol, other_pol, other in (
            ("P", "N", pc.pad_n_a.net),
            ("N", "P", pc.pad_p_a.net),
        ):
            other_segs = list(partner[other])
            if pol == "P":  # N's leg is checked against P's once, here
                for run_layer, pts, _w in legs[other_pol].runs:
                    other_segs.extend(
                        (a, b, run_layer)
                        for a, b in zip(pts, pts[1:], strict=False)
                        if dist(a, b) > 1e-9
                    )
            for run_layer, pts, _w in legs[pol].runs:
                for a, b in zip(pts, pts[1:], strict=False):
                    for c, d, other_layer in other_segs:
                        if other_layer == run_layer and seg_seg_dist(a, b, c, d) < need:
                            return None, "pair-hybrid-junction", lens, trunk_mm

        routes: list[Route] = []
        runs: list[tuple[int, int, list[Pt]]] = []
        vias: list[tuple[int, Pt]] = []
        resources = set(trunk.resources)
        for idx, pol in enumerate(("P", "N")):
            leg = legs[pol]
            net = trunk.routes[idx].net
            route = Route(net=net, net_name=trunk.routes[idx].net_name)
            route.segments.extend(leg.route.segments)
            route.vias.extend(leg.route.vias)
            route.segments.extend(trunk.routes[idx].segments)
            routes.append(route)
            for run_layer, pts, _w in leg.runs:
                runs.append((net, run_layer, list(pts)))
            vias.extend((net, pt) for pt in leg.via_points)
            resources |= leg.resources
        runs.extend(trunk.runs)
        pres = _PairResult(routes=routes, runs=runs, resources=resources, vias=vias)
        return pres, "ok", lens, trunk_mm

    def _hybrid_anchor(self, start: Any, reach: float) -> tuple[Pt, Pt, Pt] | None:
        """``(anchor, site P, site N)`` of a hybrid trunk start (issue #6446).

        The coupled body must start at a lattice node (an off-node start makes
        the fat stub a dogleg whose ``+/- pitch/2`` offset collapses), and its
        centreline must leave the sites' midpoint exactly ALONG the heading so
        the two attach doglegs mirror each other and never dip under the pair
        pitch.  So the anchor is the lattice node nearest the ideal point
        ``reach`` ahead of the sites, and the sites then slide sideways with
        it (at most half a lattice cell) instead of the anchor sliding off the
        axis the way :meth:`_gateway_anchor` lets it.  ``None`` when no node
        within 1 mm lies at least ``reach`` ahead.
        """
        from ..pairplan.gateway import HEADINGS

        h = HEADINGS[start.heading]
        mid = (
            (start.site_p[0] + start.site_n[0]) / 2.0,
            (start.site_p[1] + start.site_n[1]) / 2.0,
        )
        ideal = (mid[0] + h[0] * reach, mid[1] + h[1] * reach)
        best: tuple[float, Pt] | None = None
        for point in self.build().nodes.values():
            d = dist(point, ideal)
            if d > 1.0 or (best is not None and d >= best[0]):
                continue
            if (point[0] - mid[0]) * h[0] + (point[1] - mid[1]) * h[1] < reach - 1e-9:
                continue
            best = (d, point)
        if best is None:
            return None
        node = best[1]
        ahead = (node[0] - mid[0]) * h[0] + (node[1] - mid[1]) * h[1]
        centre = (node[0] - h[0] * ahead, node[1] - h[1] * ahead)
        shift = (centre[0] - mid[0], centre[1] - mid[1])
        return (
            node,
            (start.site_p[0] + shift[0], start.site_p[1] + shift[1]),
            (start.site_n[0] + shift[0], start.site_n[1] + shift[1]),
        )

    def _layer_name(self, layer: int) -> str:
        try:
            enum = self.layer_stack.index_to_layer_enum(layer)
            return str(getattr(enum, "kicad_name", None) or enum)
        except Exception:
            return f"L{layer}"

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
        # Issue #6957: the route's own vias must be pairwise at or above the
        # hole-to-hole floor.  The search already refuses such a via; this
        # gate makes any regression decline loudly instead of shipping.
        sites = result.via_points
        if len(sites) > 1:
            floor = self._own_via_floor() - LATTICE_CLEARANCE_EPSILON_MM
            for i, a in enumerate(sites):
                for b in sites[i + 1 :]:
                    d = dist(a, b)
                    if d > 1e-9 and d < floor:
                        return "self-drc-own-via-hole-to-hole"
        for _layer_idx, points, _widths in result.runs:
            for a, b in zip(points, points[1:], strict=False):
                if dist(a, b) <= 1e-9:
                    continue
                for via_pt in result.via_points:
                    if seg_body_crosses_pt(a, b, via_pt):
                        return "self-drc-segment-via-coincident"
        return None

    def _search_single(
        self,
        item: Connection,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
        guide: PartnerGuide | None = None,
    ) -> tuple[_RouteResult | None, str]:
        """Search ONE single-ended connection without committing it.

        Returns ``(result, "ok")`` when the route passed the search AND the
        final self-DRC gate, else ``(None, reason)``.  ``guide`` (issue
        #6703) is the partner leg's soft corridor cost for a diff-pair
        fallback leg; ``None`` is the ordinary single-ended search.
        """
        _key, start, end, net_class = item
        options: dict[str, Any] = {"escape_plan": escape_plan} if escape_plan else {}
        if guide is not None:
            options["guide"] = guide
        result, reason = self._route_impl(
            start,
            end,
            net_class,
            committed=committed,
            history=history,
            present=present,
            **options,
        )
        if result is None:
            return None, reason
        # Final self-DRC gate (#4318): a route whose own segment body
        # runs across its own via center is malformed copper the
        # checker cannot repair -- decline it honestly rather than
        # ship a 0.000mm segment-to-via coincidence.
        self_reason = self._self_drc_reason(result)
        if self_reason is not None:
            return None, self_reason
        return result, "ok"

    def _commit_single(
        self,
        item: Connection,
        result: _RouteResult,
        routes: dict[object, Route],
        committed: CommittedCopper,
    ) -> None:
        """Record ``result`` under ``item``'s key and commit its copper."""
        key, start, _end, net_class = item
        routes[key] = result.route
        # Commit each segment at the width it was EMITTED at (issue
        # #4293): the widened lattice body is spaced against 2.6 mm
        # copper as 2.6 mm copper, and a tapered escape neck is spaced
        # honestly as neck copper -- never the taper cheating the
        # spacing model.  Clearance stays the class clearance (#4271).
        _half, clr = self._conn_geometry(net_class, start.net)
        for layer_idx, points, widths in result.runs:
            committed.add_run_widths(layer_idx, points, start.net, [w / 2.0 for w in widths], clr)
        for via_pt in result.via_points:
            committed.add_via(via_pt, start.net, clr)

    def _route_single_into(
        self,
        item: Connection,
        routes: dict[object, Route],
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
    ) -> str | None:
        """Route ONE single-ended connection and commit it (one pass).

        Shared by ordinary singles and the #6373 uncoupled-fallback pair legs
        so both go through the identical search + self-DRC + commit path.
        Returns ``None`` on success (``routes[key]`` set and the copper
        committed), else the decline reason -- nothing is committed then.
        """
        result, reason = self._search_single(
            item, committed=committed, history=history, present=present, escape_plan=escape_plan
        )
        if result is None:
            return reason
        self._commit_single(item, result, routes, committed)
        return None

    def _partner_guide(self, result: _RouteResult, pc: CoupledConnection) -> PartnerGuide:
        """The #6703 corridor cost built from one routed fallback leg."""
        pitch = max(pc.pitch, 1e-3)
        return PartnerGuide(
            result.runs,
            near=_GUIDE_NEAR_PITCHES * pitch,
            cap=_GUIDE_CAP_MM,
            weight=_GUIDE_WEIGHT,
        )

    def _fallback_items(self, pc: CoupledConnection) -> dict[str, Connection]:
        """The two single-ended leg connections of a declined pair (#6373)."""
        return {
            pol: ((pc.key, pol), pad_a, pad_b, pc.net_class)
            for pol, pad_a, pad_b in (
                ("P", pc.pad_p_a, pc.pad_p_b),
                ("N", pc.pad_n_a, pc.pad_n_b),
            )
        }

    def _pair_skew(self, legs: _FallbackLegs) -> float | None:
        """``|len_P - len_N|`` of a fully routed leg pair, else ``None``."""
        p_r, n_r = legs["P"][0], legs["N"][0]
        if p_r is None or n_r is None:
            return None
        measure = self.pair_length_measure or _default_pair_length
        return abs(measure(p_r.route) - measure(n_r.route))

    def _legs_shortfalls(self, pc: CoupledConnection, legs: _FallbackLegs) -> int:
        """Class-pad shortfalls of a leg pair (see :func:`class_pad_shortfalls`)."""
        from .class_pad_repair import class_pad_shortfalls

        total = 0
        for pol in ("P", "N"):
            res = legs[pol][0]
            if res is not None:
                memo = self._shortfall_memo
                hit = memo.get(id(res))
                if hit is None or hit[0] is not res:
                    hit = (res, class_pad_shortfalls(self, res.route, pc.net_class))
                    memo[id(res)] = hit
                total += hit[1]
        return total

    def _partner_blind_legs(
        self,
        pc: CoupledConnection,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
    ) -> _FallbackLegs:
        """The pre-#6703 fallback: P, then N, as unrelated nets (committed)."""
        items = self._fallback_items(pc)
        legs: _FallbackLegs = {}
        for pol in ("P", "N"):
            res, reason = self._search_single(
                items[pol],
                committed=committed,
                history=history,
                present=present,
                escape_plan=escape_plan,
            )
            if res is not None:
                self._commit_single(items[pol], res, {}, committed)
            legs[pol] = (res, reason)
        return legs

    def _strict_pad_pairwise(
        self, pc: CoupledConnection, items: dict[str, Connection]
    ) -> tuple[LatticePairwise | None, float]:
        """Per-pad class keep-out for a fallback pair's leg search (#6703).

        The lattice search holds every foreign pad only to the connection's
        own clearance; a pad whose class asks for more is fixed afterwards by
        the class-pad repair, which may barely move diff-pair copper (#6437).
        This projects the pads whose own class (:func:`pad_class_requirements`)
        is stricter than the leg's onto a :class:`LatticePairwise` keyed
        ``(leg net, pad net)``.  Installed only while a partner-aware leg is
        SEARCHED, it widens exactly those pads' keep-outs
        (:meth:`LatticeObstacleModel.pairwise_pad_blocked`); every other
        obstacle keeps the leg's own clearance.  Committed copper never
        carries it: the projection is not threaded into
        :class:`CommittedCopper`.

        Also returns the search clearance FLOOR: the strictest of those pad
        classes that is at most :data:`_SEARCH_FLOOR_MAX_FACTOR` times the
        legs' own clearance (``0.0`` when there is none).  The first cut of
        #6703 raised the floor to the strictest class on the board, so one
        outlier class (a high-voltage pad) held every obstacle of every
        candidate at its gap (PR #6735 review); such a pad is now kept clear
        through the projection only.

        ``(None, 0.0)`` when no pad is stricter than either leg (the searches
        are then exactly the partner-blind ones), when the router installed
        no pad classes, or when a real HV pairwise projection is already
        active (it governs pad clearance itself, and the repair is skipped
        there).
        """
        from .class_pad_repair import pad_class_requirements
        from .pairwise import LatticePairwise

        if self._pairwise is not None:
            return None, 0.0
        pad_req = pad_class_requirements(self)
        if not pad_req:
            return None, 0.0
        by_pad_net: dict[int, float] = {}
        for pad, req in zip(self.pads, pad_req, strict=False):
            if pad.net is None:
                continue
            if req > by_pad_net.get(pad.net, 0.0):
                by_pad_net[pad.net] = req
        required: dict[tuple[int, int], float] = {}
        max_by_net: dict[int, float] = {}
        for item in items.values():
            leg_net = item[1].net
            own = self._conn_geometry(pc.net_class, leg_net)[1]
            for pad_net, req in by_pad_net.items():
                if pad_net == leg_net or req <= own + 1e-9:
                    continue
                key = (leg_net, pad_net) if leg_net < pad_net else (pad_net, leg_net)
                required[key] = max(required.get(key, 0.0), req)
                max_by_net[leg_net] = max(max_by_net.get(leg_net, 0.0), req)
        if not required:
            return None, 0.0
        own_min = min(self._conn_geometry(pc.net_class, it[1].net)[1] for it in items.values())
        cap = _SEARCH_FLOOR_MAX_FACTOR * own_min + 1e-9
        floor = max((req for req in required.values() if req <= cap), default=0.0)
        return (
            LatticePairwise(required_by_pair=required, zones=(), max_by_net=max_by_net),
            floor,
        )

    def _partner_aware_legs(
        self,
        pc: CoupledConnection,
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
    ) -> tuple[_FallbackLegs, bool]:
        """Route a declined pair's two legs partner-aware (issue #6703; committed).

        Four candidates are built:

        * P unguided, then N unguided (the pre-#6703 order);
        * P unguided, then N guided by P's corridor (:class:`PartnerGuide`);
        * N unguided, then P unguided;
        * N unguided, then P guided by N's corridor.

        Every leg is searched holding each foreign pad at that pad's own class
        (see :meth:`_strict_pad_pairwise`).  The second leg of a candidate is searched against committed
        copper that holds the candidate's first leg (a
        :meth:`CommittedCopper.begin_trial` that is rolled back), so every
        candidate is legal exactly as it would be had it been routed alone.
        The winner has the most routed legs, then the fewest class-pad
        shortfalls, then the smallest skew (:meth:`_pair_skew`), then the
        smallest total length; ties keep the earlier candidate, so the
        selection is deterministic.  Its legs are committed.  Returns
        ``(legs, is_partner_blind)`` -- the flag is set when the winner is
        exactly the copper :meth:`_partner_blind_legs` would have produced.
        """
        items = self._fallback_items(pc)
        kw: dict[str, Any] = {
            "committed": committed,
            "history": history,
            "present": present,
            "escape_plan": escape_plan,
        }

        # A shortfall against a pad's own class left on a diff-pair net gets
        # the net demoted (the class-pad repair may barely move pair copper),
        # so the partner-aware legs keep every foreign pad at that pad's own
        # class while they are searched: their copper then needs no repair.
        # Two parts (see :meth:`_strict_pad_pairwise`):
        #
        # * every pad stricter than the leg is widened to its own class (a
        #   per-pad projection, whatever that class is);
        # * the leg's clearance to EVERY obstacle is raised to the strictest
        #   pad class that is at most ``_SEARCH_FLOOR_MAX_FACTOR`` times its
        #   own (board 07rf: 0.2 mm for a 0.1 mm TMDS leg).  The spacing that
        #   buys is what leaves room for the later TMDS legs there; an
        #   outlier class (a 1.5 mm HV pad) is held per pad only, so it can
        #   no longer push every candidate off the board.
        #
        # Committed copper keeps the leg's own clearance.
        strict, floor = self._strict_pad_pairwise(pc, items)

        def search(pol: str, guide: PartnerGuide | None = None) -> tuple[_RouteResult | None, str]:
            previous = self._pairwise
            if strict is not None:
                self._pairwise = strict
            self._search_clearance_floor = floor
            try:
                return self._search_single(items[pol], guide=guide, **kw)
            finally:
                self._pairwise = previous
                self._search_clearance_floor = 0.0

        candidates: list[_FallbackLegs] = []
        firsts: _FallbackLegs = {}
        for lead, follow in (("P", "N"), ("N", "P")):
            first = search(lead)
            firsts[lead] = first
            lead_res = first[0]
            if lead_res is None:
                continue
            mark = committed.begin_trial()
            try:
                self._commit_single(items[lead], lead_res, {}, committed)
                plain = search(follow)
                guided = search(follow, self._partner_guide(lead_res, pc))
            finally:
                committed.rollback_trial(mark)
            # The plain P-led candidate keeps the pre-#6703 order; listed
            # first so it wins every tie.
            candidates.append({lead: first, follow: plain})
            if guided[0] is not None:
                candidates.append({lead: first, follow: guided})
        if firsts["P"][0] is None:
            # P cannot route even alone: the pre-#6703 shape (N routed on
            # its own) leads, and wins unless an N-led candidate is no worse.
            candidates.insert(0, {"P": firsts["P"], "N": firsts["N"]})

        measure = self.pair_length_measure or _default_pair_length

        def rank(cand: _FallbackLegs) -> tuple[int, int, float, float]:
            p_r, n_r = cand["P"][0], cand["N"][0]
            routed = (p_r is not None) + (n_r is not None)
            short = self._legs_shortfalls(pc, cand)
            if p_r is None or n_r is None:
                return (-routed, short, 0.0, 0.0)
            lp, ln = measure(p_r.route), measure(n_r.route)
            # Rounded to 1 um so float noise never flips a tie.
            return (-routed, short, round(abs(lp - ln), 3), round(lp + ln, 3))

        best = min(candidates, key=rank)  # ``min`` keeps the first on ties
        for pol in ("P", "N"):
            res = best[pol][0]
            if res is not None:
                self._commit_single(items[pol], res, {}, committed)
        # ``candidates[0]`` is the partner-blind (pre-#6703) leg pair whenever
        # no pad needed widening (the searches were then the plain ones).
        return (
            {"P": best["P"], "N": best["N"]},
            best is candidates[0] and strict is None and floor == 0.0,
        )

    def _route_fallback_pairs_into(
        self,
        pairs: list[CoupledConnection],
        routes: dict[object, Route],
        *,
        committed: CommittedCopper,
        history: dict[Resource, float],
        present: float,
        escape_plan: dict[int, EscapeChoice],
        deadline: float | None = None,
    ) -> list[dict[str, tuple[Connection, str | None]]]:
        """Route every deferred fallback pair's legs (#6373), skew-aware (#6703).

        Pre-#6703 each leg was routed as an unrelated net (P, then N), so
        wherever a wall forced a detour the legs took different ways round
        it and the pair's skew grew by mm.  Each pair now picks its best
        partner-aware candidate (:meth:`_partner_aware_legs`).

        Pairs routed one after another compete for the same escape channels,
        so a pair's best choice can push a LATER pair's legs onto a worse
        detour, or off the board (07rf: TMDS_D1 matched by sending its N leg
        over B.Cu, where TMDS_D0's N leg then found no way at all).  So each
        pair's partner-aware choice is weighed by a rollout: the remaining
        pairs are routed partner-blind behind it, and the choice is kept only
        when the whole flush then scores better than with the pair's own
        partner-blind legs: more routed legs, or the same number with the
        summed skew at least :data:`_MIN_FLUSH_SKEW_GAIN_MM` smaller and no
        more class-pad shortfalls (:func:`~.class_pad_repair.class_pad_shortfalls`).
        Anything else keeps the pre-#6703 copper.  Shortfalls alone never
        justify the change: skew is what the partner-aware legs are for, and
        a partner-blind shortfall is still cleared by the #6489 pad-growth
        round.  On board 06rf (PR #6735 review), keeping legs that only dodged
        a shortfall, or cut the skew by 0.1 mm, moved a USB3_TX2 via onto
        B.Cu under J1, pushed USB3_TX1's partner-blind N leg across the spot
        the pour oracle bridges the two VBUS_USB islands at, and left one
        kicad-cli unconnected item that main does not have.  The comparison baseline at every step
        is the flush already secured, so the result never scores worse than
        the pre-#6703 flush from the same state.

        Bounded cost: only the first :data:`_MAX_PARTNER_AWARE_PAIRS` pairs
        are weighed this way, and once ``deadline`` (``time.monotonic()``)
        passes no further pair is -- every remaining pair keeps the
        partner-blind legs the rollout already computed, so an expired budget
        costs no new search.

        Commits the kept flush.  Returns, per pair, ``{"P"|"N": (leg_item,
        decline reason or None)}``.
        """
        kw: dict[str, Any] = {
            "committed": committed,
            "history": history,
            "present": present,
            "escape_plan": escape_plan,
        }

        def score(flush: list[_FallbackLegs]) -> tuple[int, float, int]:
            routed = sum(legs[pol][0] is not None for legs in flush for pol in ("P", "N"))
            short = sum(
                self._legs_shortfalls(pc, legs) for pc, legs in zip(pairs, flush, strict=True)
            )
            skews = [self._pair_skew(legs) for legs in flush]
            return (-routed, round(sum(sk for sk in skews if sk is not None), 3), short)

        def better(aware: tuple[int, float, int], blind: tuple[int, float, int]) -> bool:
            # More routed legs always wins.  Otherwise the partner-aware legs
            # must cut the summed skew by a material margin without adding a
            # class-pad shortfall: every leg it moves perturbs the rest of
            # the board, so a marginal gain is not worth the churn.
            if aware[0] != blind[0]:
                return aware[0] < blind[0]
            return aware[1] <= blind[1] - _MIN_FLUSH_SKEW_GAIN_MM and aware[2] <= blind[2]

        def recommit(pc: CoupledConnection, legs: _FallbackLegs) -> None:
            items = self._fallback_items(pc)
            for pol in ("P", "N"):
                res = legs[pol][0]
                if res is not None:
                    self._commit_single(items[pol], res, {}, committed)

        self._shortfall_memo = {}
        # ``rest`` is always the partner-blind routing of the pairs not yet
        # decided, under the copper committed so far; initially the whole
        # pre-#6703 flush.
        mark = committed.begin_trial()
        rest = [self._partner_blind_legs(pc, **kw) for pc in pairs]
        committed.rollback_trial(mark)
        chosen: list[_FallbackLegs] = []
        for i, pc in enumerate(pairs):
            if i >= _MAX_PARTNER_AWARE_PAIRS or (
                deadline is not None and time.monotonic() >= deadline
            ):
                # Out of budget: the remaining pairs keep their partner-blind
                # legs (``rest`` is exactly that routing under ``chosen``).
                for q, legs in zip(pairs[i:], rest, strict=True):
                    recommit(q, legs)
                chosen.extend(rest)
                break
            mark = committed.begin_trial()
            aware, is_blind = self._partner_aware_legs(pc, **kw)
            if is_blind:
                # Same copper as the partner-blind leg pair: nothing to weigh.
                committed.keep_trial(mark)
                chosen.append(aware)
                rest = rest[1:]
                continue
            inner = committed.begin_trial()
            aware_rest = [self._partner_blind_legs(q, **kw) for q in pairs[i + 1 :]]
            committed.rollback_trial(inner)
            if better(score([*chosen, aware, *aware_rest]), score([*chosen, *rest])):
                committed.keep_trial(mark)
                chosen.append(aware)
                rest = aware_rest
            else:
                committed.rollback_trial(mark)
                recommit(pc, rest[0])
                chosen.append(rest[0])
                rest = rest[1:]
        self._shortfall_memo = {}
        out: list[dict[str, tuple[Connection, str | None]]] = []
        for pc, legs in zip(pairs, chosen, strict=True):
            items = self._fallback_items(pc)
            per: dict[str, tuple[Connection, str | None]] = {}
            for pol in ("P", "N"):
                res, reason = legs[pol]
                if res is not None:
                    routes[items[pol][0]] = res.route
                    per[pol] = (items[pol], None)
                else:
                    per[pol] = (items[pol], reason)
            out.append(per)
        return out

    def route_netset(
        self,
        connections: list[Connection],
        **kwargs: Any,
    ) -> tuple[dict[object, Route], LatticeNegotiationStats]:
        """Negotiate with grown-pad rounds (#6489), guarding gateways (#6444).

        See :meth:`_route_netset_grown` for the negotiation itself.  Issue
        #6444: a gateway changes which corridors every later net finds, so a
        run that coupled a pair through a fan-via gateway but leaves a
        class-pad shortfall the repair could not clear (the post-route
        backstop would then demote that net -- board 06rf, USB3_TX2+) is
        re-negotiated once WITHOUT gateways and the better result kept: more
        routed connections, then fewer shortfalls; a tie keeps the gateway
        run.  A clean gateway run (the common case) costs nothing extra.
        """
        self.pair_gateway_rollback = None
        result = self._route_netset_grown(connections, **kwargs)
        rstats = self.class_pad_repair_stats
        left = rstats.total_remaining if rstats is not None else 0
        if (
            not kwargs.get("pair_gateways")
            or not self.pair_gateways
            or left <= 0
            or result[1].deadline_hit
        ):
            return result
        deadline = kwargs.get("deadline")
        if deadline is not None and time.monotonic() >= deadline:
            return result
        saved = (
            self.failure_reasons,
            self.pair_outcomes,
            self.pair_gateways,
            dict(self.pair_gateway_log),
            self.class_pad_repair_stats,
            dict(self._class_pad_growth),
            list(self.class_pad_growth_rounds),
            self._lattice,
            self._obstacles,
        )
        saved_hybrid = dict(self.pair_hybrid_log)
        if self._class_pad_growth:
            self._lattice = None  # rebuild without the gateway run's growth
            self._obstacles = None
        alt = self._route_netset_grown(connections, **{**kwargs, "pair_gateways": False})
        astats = self.class_pad_repair_stats
        alt_left = astats.total_remaining if astats is not None else 0
        if alt[1].routed > result[1].routed or (
            alt[1].routed == result[1].routed and alt_left < left
        ):
            self.pair_gateway_rollback = (
                f"gateway run left {left} class-pad shortfall(s); "
                f"re-negotiated without gateways ({alt_left} left): kept"
            )
            self.pair_gateway_log = {}  # the kept run attempted no gateway
            self.pair_hybrid_log = {}  # ... and so no hybrid (it follows the gateway)
            return alt
        (
            self.failure_reasons,
            self.pair_outcomes,
            self.pair_gateways,
            self.pair_gateway_log,
            self.class_pad_repair_stats,
            self._class_pad_growth,
            self.class_pad_growth_rounds,
            self._lattice,
            self._obstacles,
        ) = saved
        self.pair_hybrid_log = saved_hybrid
        self.pair_gateway_rollback = (
            f"gateway run left {left} class-pad shortfall(s); a run without gateways "
            f"was no better ({alt_left} left): gateways kept"
        )
        return result

    def _route_netset_grown(
        self,
        connections: list[Connection],
        **kwargs: Any,
    ) -> tuple[dict[object, Route], LatticeNegotiationStats]:
        """Negotiate, then re-negotiate once with grown pads if needed (#6489).

        :meth:`_route_netset_once` repairs class-pad shortfalls geometrically.
        A slot narrower than ``width + 2 x class`` has no legal translation, so
        the repair leaves it and the router's backstop would delete the whole
        net.  When shortfalls remain, exactly those pads' static keep-out grows
        to their class and the netset is negotiated again (bounded rounds); the
        new result is kept only if it routes at least as much with fewer
        shortfalls.  With nothing left the first result is returned untouched
        (no rebuild, ``lattice_builds`` unchanged).

        Issue #6670: in a pad array (a BGA's power balls, a connector's GND
        pins) a track pushed off one grown pad moves a lattice row over and
        lands just as close to the NEXT ungrown pad of the same part.  Board
        06rf's USB3 fallback legs walked U2's GND/+1V2 balls that way: two
        rounds cut 9 shortfalls to 7 to 2 and the backstop then deleted
        USB3_TX2- and USB3_RX2+.  So when a round's shortfalls land on a part
        whose pads were already grown, every pad of that part with a class
        above the base is tried grown at once.  If that attempt is rejected
        (it routes less, or leaves no fewer shortfalls), the round falls back
        to growing only the offending pads, as before, so a round is never
        worse than the pre-#6670 one.  A part seen for the first time grows
        only its offending pads, so a board that converges in one round is
        unchanged.  :attr:`class_pad_growth_rounds` records every attempt.
        """
        from .class_pad_repair import pad_class_requirements

        deadline = kwargs.get("deadline")
        self._class_pad_growth = {}
        self.class_pad_growth_rounds = []
        best = self._route_netset_once(connections, **kwargs)
        for _round in range(_MAX_PAD_GROWTH_ROUNDS):
            rstats = self.class_pad_repair_stats
            if rstats is None or not rstats.unrepaired_pads or best[1].deadline_hit:
                break
            wanted = {
                i: req
                for i, req in rstats.unrepaired_pads.items()
                if req > self._class_pad_growth.get(i, 0.0) + 1e-9
            }
            if not wanted:
                break
            grown_parts = {self.pads[i].ref for i in self._class_pad_growth if self.pads[i].ref}
            recurring = tuple(
                sorted({self.pads[i].ref for i in wanted if self.pads[i].ref in grown_parts})
            )
            attempts: list[tuple[dict[int, float], tuple[str, ...]]] = []

            def whole_part(parts: tuple[str, ...], wanted: dict[int, float] = wanted) -> dict:
                whole = dict(wanted)
                reqs = pad_class_requirements(self) or []
                base = self.rules.trace_clearance
                for j, req in enumerate(reqs):
                    if (
                        self.pads[j].ref in parts
                        and req > base + 1e-9
                        and req > max(whole.get(j, 0.0), self._class_pad_growth.get(j, 0.0)) + 1e-9
                    ):
                        whole[j] = req
                return whole

            if recurring:
                whole = whole_part(recurring)
                if len(whole) > len(wanted):
                    attempts.append((whole, recurring))
            # The targeted growth (the pre-#6670 round) is the fallback when a
            # whole-part growth is rejected, so a round is never worse than it.
            attempts.append((wanted, ()))
            # Issue #6444: a gateway reshapes the corridors the fallback legs
            # find, so a shortfall can land on a part for the FIRST time and
            # the targeted growth then fails to clear it (board 06rf's U2,
            # which main clears with a round-2 whole-part growth).  In a run
            # that coupled through a gateway, a rejected targeted growth is
            # followed by the whole-part growth of the offending parts; it is
            # kept only by the same rule (more routed, fewer shortfalls).
            if kwargs.get("pair_gateways") and self.pair_gateways and not recurring:
                first = tuple(sorted({self.pads[i].ref for i in wanted if self.pads[i].ref}))
                whole = whole_part(first) if first else wanted
                if len(whole) > len(wanted):
                    attempts.append((whole, first))
            kept = False
            for grow, parts in attempts:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                saved = (
                    self.failure_reasons,
                    self.pair_outcomes,
                    self.pair_gateways,
                    dict(self.pair_gateway_log),  # issue #6834
                    self.class_pad_repair_stats,
                    dict(self._class_pad_growth),
                    self._lattice,
                    self._obstacles,
                )
                saved_hybrid = dict(self.pair_hybrid_log)
                self._class_pad_growth.update(grow)
                self._lattice = None
                self._obstacles = None
                routes, stats = self._route_netset_once(connections, **kwargs)
                new_left = (
                    self.class_pad_repair_stats.total_remaining
                    if self.class_pad_repair_stats
                    else 0
                )
                old_left = saved[4].total_remaining if saved[4] else 0
                kept = stats.routed >= best[1].routed and new_left < old_left
                self.class_pad_growth_rounds.append(
                    PadGrowthRound(
                        grown=len(grow),
                        footprints=parts,
                        routed_before=best[1].routed,
                        routed_after=stats.routed,
                        left_before=old_left,
                        left_after=new_left,
                        kept=kept,
                    )
                )
                if kept:
                    best = (routes, replace(stats, lattice_builds=self.lattice_builds))
                    break
                (
                    self.failure_reasons,
                    self.pair_outcomes,
                    self.pair_gateways,
                    self.pair_gateway_log,
                    self.class_pad_repair_stats,
                    self._class_pad_growth,
                    self._lattice,
                    self._obstacles,
                ) = saved
                self.pair_hybrid_log = saved_hybrid
            if not kept:
                break
        routes, stats = best
        return routes, replace(stats, lattice_builds=self.lattice_builds)

    def _route_netset_once(
        self,
        connections: list[Connection],
        *,
        coupled: list[CoupledConnection] | None = None,
        fixed_copper: list[Route] | None = None,
        fixed_clearance: dict[int, float] | None = None,
        max_iterations: int = 8,
        present_cost_initial: float = 1.0,
        present_cost_growth: float = 1.6,
        history_increment_factor: float = 1.0,
        deadline: float | None = None,
        uncoupled_fallback: bool = False,
        pair_nets: Mapping[int, int] | None = None,
        pair_gateways: bool = False,
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

        Issue #6373: with ``uncoupled_fallback=False`` (the default, the
        #4270 v1 contract) a declined pair is never split -- it emits nothing
        and its main run stays open.  With ``uncoupled_fallback=True`` a pair
        whose coupled search declines has its two main-run legs
        (``pad_p_a -> pad_p_b`` and ``pad_n_a -> pad_n_b``) routed
        single-ended *in the same pass*, immediately after the decline (so
        they keep the pair's corridor priority over the singles).  Each leg
        goes through the ordinary :meth:`_route_impl` + :meth:`_self_drc_reason`
        gates, is keyed ``(pc.key, "P")`` / ``(pc.key, "N")`` exactly like a
        coupled success, and a leg that cannot route lands in
        :attr:`failure_reasons` under its own key -- nothing partial is ever
        emitted for a leg.  The pair's outcome is recorded as
        ``"uncoupled-fallback:<decline reason>"``.  Stats count a fallback
        pair as its two legs (``routed``/``total`` each gain one extra unit),
        and between passes a fallback pair whose coupled run is still
        geometrically possible on an empty board keeps accruing demand
        history, so negotiation may yet recover it as ``"coupled"`` (a pass
        with more coupled pairs wins a completion tie).

        The lattice is built **once** (:attr:`lattice_builds` == 1 across the
        whole negotiation); rip-up resets only the committed-copper model.
        Returns ``(routes_by_key, stats)`` for the best pass seen;
        :attr:`failure_reasons` carries the per-connection decline diagnosis
        for that pass.

        Issue #6437: ``pair_nets`` maps every detected diff-pair net id,
        engaged or not, to its partner's.  The post-negotiation pad-class repair
        (#6395) may move their single-ended copper only within a 0.01 mm skew
        budget per pair (P5's threshold).  The nets of every ``coupled`` entry are
        always added, so a direct caller stays safe.

        Issue #4355: ``fixed_copper`` is the preserved copper of NON-listed
        nets (``router.existing_routes`` minus the routable set).  It is
        pre-seeded into every per-pass :class:`CommittedCopper` model as an
        immovable hard obstacle, so a negotiated net routes AROUND it or is
        honestly declined -- it is never emitted overlapping foreign copper.

        Issue #4597: ``fixed_clearance`` is a plain ``{net_id: clearance}`` map
        giving each preserved net its net-class clearance, floored at
        ``rules.trace_clearance`` (a class may only GROW the gap).  Without it
        every preserved segment/via seeds at the board-global floor, so a
        multi-step ``--preserve-existing`` composition spaced new copper at the
        DRU floor from an HV net the map put at 2.0-3.2 mm -- the
        ``max(own_clr, stored_clr)`` term in :class:`CommittedCopper` collapsed
        to the routing net's own clearance.  ``None`` / ``{}`` / unmapped nets
        preserve the pre-#4597 behavior byte-for-byte.

        Issue #4472: ``deadline`` is an optional absolute
        :func:`time.monotonic` timestamp.  When set, the negotiation aborts as
        soon as it is reached -- checked before each iteration and before each
        per-connection search -- and returns the best routes found so far.
        This bounds a ``--complete`` completion pass so a pathological search
        aborts within a per-link budget instead of grinding (issue #4434).
        ``None`` preserves the pre-#4472 unbudgeted (iteration-capped) loop.

        Issue #6444: with ``pair_gateways=True`` a pair segment whose planar
        attempt declines with a reason in :data:`GATEWAY_RETRY_REASONS` gets
        one fan-via gateway retry (:meth:`_route_pair_gateway`).  The retries
        are DEFERRED until every pair of the pass has had its planar attempt
        (the #6697 discipline), so a gateway's vias never pre-block a pair
        that couples planar: those keep exactly the copper they get without
        the retry.  A gateway success is reported ``"coupled"`` (named in
        :attr:`pair_gateways`); a gateway decline keeps the original planar
        decline reason and the segment takes the usual fallback.  ``False``
        (the default) is the pre-#6444 negotiation byte-for-byte.
        """
        # Issue #4477: wall-clock the WHOLE negotiation (not just the
        # deadline check) so a report can honestly say "elapsed Xs of a Ys
        # budget" even on a run that never hits the deadline.
        start_time = time.monotonic()
        self._set_fixed_copper(fixed_copper, fixed_clearance)
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

        history = PhysicalHistory()
        escape_plan: dict[int, EscapeChoice] = {}
        best_routes: dict[object, Route] = {}
        best_reasons: dict[object, str] = {}
        best_pair_outcomes = SegmentOutcomes()
        best_pair_gateways: dict[object, str] = {}
        best_count = -1
        # Issue #6373: best-pass ranking ``(routed_items, coupled_count,
        # fallback_legs)``.  ``routed_items`` keeps its pre-#6373 meaning
        # (completed items: a coupled pair and a fully-routed fallback pair
        # each count once) and leads.  A completion tie then prefers more
        # COUPLED pairs -- so a later pass that recovers a congested fallback
        # pair as coupled replaces the earlier uncoupled pass -- and only
        # then more routed fallback legs (which rewards a partially-routed
        # fallback pair's lone leg; a full fallback pair is already in
        # ``routed_items``).  With ``uncoupled_fallback`` off the two
        # tie-break keys are pinned to 0, so the selection (and escape-plan
        # release) is exactly the pre-#6373 first-best-pass rule.
        # Issue #6378: a pair that passes through an interior part arrives as
        # one ``CoupledConnection`` per chain SEGMENT, so ``coupled_count``
        # counts coupled segments and ``routed_items`` counts each segment
        # once.  Both stay monotone in "more coupling is better": a pass that
        # couples more segments at equal completion still wins, and the
        # fallback / congested-probe logic below applies per segment.
        best_score: tuple[int, int, int, int] = (-1, -1, -1, -1)
        # Leg-unit accounting of the best pass for the honest stats.
        best_fallback_pairs = 0
        best_fallback_legs = 0
        best_hybrid_pairs = 0
        converged = False
        iterations_run = 0
        present = present_cost_initial

        deadline_hit = False
        for it in range(max_iterations):
            # Issue #4472: abort before starting a fresh pass once the per-link
            # budget is spent; the best pass seen so far is returned below.
            if deadline is not None and time.monotonic() >= deadline:
                deadline_hit = True
                # Issue #4477: if NO pass ever ran (the budget was already
                # spent before the first attempt -- e.g. an extremely tight
                # budget on a large cohort), ``best_reasons`` would otherwise
                # stay empty and the caller could not report WHY every link
                # is unroutable.  Synthesize an honest "never attempted"
                # decline for each connection so the report is never blank.
                if best_count < 0:
                    best_count = 0
                    for _length, kind, item in items:
                        key = item.key if kind == 1 else item[0]
                        best_reasons[key] = "deadline-exceeded"
                        if kind == 1:
                            best_pair_outcomes[key] = SegmentOutcome("deadline-exceeded")
                break
            iterations_run = it + 1
            committed = self._fresh_committed()
            routes: dict[object, Route] = {}
            reasons: dict[object, str] = {}
            pair_outcomes = SegmentOutcomes()
            routed_items = 0
            failed: list[tuple[int, Any]] = []
            # Issue #6373: per-pass uncoupled-fallback bookkeeping.
            fallback_pairs: list[Any] = []
            fallback_legs = 0
            coupled_count = 0
            hybrid_count = 0
            deferred_fallback: list[Any] = []
            # Issue #6444: declined segments awaiting their gateway retry.
            deferred_gateway: list[tuple[Any, str]] = []
            # Issue #6446: segments whose gateway retry declined, awaiting
            # their one-ended hybrid retry; and the ones that got one.
            deferred_hybrid: list[Any] = []
            hybrid_pairs: list[Any] = []
            pass_gateways: dict[object, str] = {}

            def commit_pair(item: Any, pres: _PairResult) -> None:
                routes[(item.key, "P")] = pres.routes[0]
                routes[(item.key, "N")] = pres.routes[1]
                # Legs are EMITTED at the pair class width (#4270), so
                # commit them at the same width + clearance (#4271) --
                # emission and spacing must agree.
                for leg_net, layer_idx, points in pres.runs:
                    # Issue #6243: each leg stores its own authored side.
                    half, clr = self._conn_geometry(item.net_class, leg_net)
                    committed.add_run(layer_idx, points, leg_net, half, clr)
                # Issue #6444: a gateway's fan vias are pair-owned copper too.
                for via_net, site in pres.vias:
                    _half, clr = self._conn_geometry(item.net_class, via_net)
                    committed.add_via(site, via_net, clr)

            def pair_vias_used(item: Any) -> int:
                # Issue #6834: vias per net already committed on the other
                # chain segments of ``item``'s pair this pass (gateway fans
                # and fallback-leg vias alike); the checker's limit is per net.
                sibling_keys = {
                    pc.key
                    for pc in (coupled or ())
                    if pc.pair_name == item.pair_name and pc.key != item.key
                }
                used = {"P": 0, "N": 0}
                for rkey, route in routes.items():
                    if isinstance(rkey, tuple) and len(rkey) == 2 and rkey[0] in sibling_keys:
                        used[rkey[1]] += len(route.vias)
                return max(used.values())

            def flush_gateways() -> None:
                nonlocal routed_items, coupled_count
                pending = list(deferred_gateway)
                deferred_gateway.clear()
                for item, preason in pending:
                    if deadline is not None and time.monotonic() >= deadline:
                        gres, _greason = None, "pair-gateway-deadline"
                    else:
                        gres, _greason = self._route_pair_gateway(
                            item,
                            committed=committed,
                            history=history,
                            present=present,
                            deadline=deadline,
                            vias_used=pair_vias_used(item),
                        )
                    if gres is None:
                        if not uncoupled_fallback:
                            reasons[item.key] = preason
                            failed.append((1, item))
                        elif hybrid_retry_reason(_greason):
                            deferred_hybrid.append(item)
                        continue
                    pass_gateways[item.key] = gres.gateway or "gateway"
                    pair_outcomes[item.key] = SegmentOutcome.coupled(pass_gateways[item.key])
                    coupled_count += 1
                    routed_items += 1
                    reasons.pop(item.key, None)
                    if item in deferred_fallback:
                        deferred_fallback.remove(item)
                    if item in fallback_pairs:
                        fallback_pairs.remove(item)
                    commit_pair(item, gres)

            def flush_hybrids() -> None:
                nonlocal routed_items, hybrid_count
                pending = list(deferred_hybrid)
                deferred_hybrid.clear()
                for item in pending:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    hres, terminal = self._route_pair_hybrid(
                        item,
                        committed=committed,
                        history=history,
                        present=present,
                        escape_plan=escape_plan,
                        deadline=deadline,
                        vias_used=pair_vias_used(item),
                    )
                    if hres is None:
                        continue  # keeps its uncoupled fallback
                    pair_outcomes[item.key] = SegmentOutcome.hybrid(terminal)
                    hybrid_count += 1
                    routed_items += 1
                    reasons.pop(item.key, None)
                    if item in deferred_fallback:
                        deferred_fallback.remove(item)
                    if item in fallback_pairs:
                        fallback_pairs.remove(item)
                    hybrid_pairs.append(item)
                    commit_pair(item, hres)

            def flush_fallbacks() -> None:
                nonlocal routed_items, fallback_legs
                flush_gateways()
                flush_hybrids()
                if not deferred_fallback:
                    return
                # Issue #6703: the legs are routed partner-aware so a pair's
                # two legs take the same way round an obstacle wall.
                flushed = self._route_fallback_pairs_into(
                    list(deferred_fallback),
                    routes,
                    committed=committed,
                    history=history,
                    present=present,
                    escape_plan=escape_plan,
                    deadline=deadline,
                )
                for legs in flushed:
                    legs_ok = 0
                    for pol in ("P", "N"):
                        leg_item, leg_reason = legs[pol]
                        if leg_reason is None:
                            legs_ok += 1
                        else:
                            reasons[leg_item[0]] = leg_reason
                            failed.append((0, leg_item))
                    fallback_legs += legs_ok
                    if legs_ok == 2:
                        routed_items += 1
                deferred_fallback.clear()

            for _idx, (_length, kind, item) in enumerate(items):
                # Issue #4472: abort mid-pass on the deadline too, so a single
                # slow search inside one pass cannot overrun the budget.
                if deadline is not None and time.monotonic() >= deadline:
                    deadline_hit = True
                    # Issue #4477: every connection from here to the end of
                    # this pass was never attempted -- stamp them honestly as
                    # "deadline-exceeded" rather than silently omitting them
                    # from ``reasons`` (which would make an unroutable link
                    # undiagnosable in the completion report).
                    for _length2, kind2, item2 in items[_idx:]:
                        key2 = item2.key if kind2 == 1 else item2[0]
                        reasons[key2] = "deadline-exceeded"
                        if kind2 == 1:
                            pair_outcomes[key2] = SegmentOutcome("deadline-exceeded")
                    break
                if kind == 1:
                    # Issue #6883: a class that prefers a layer none of the
                    # pads share (opted in via ``allow_plane_layers``) gets
                    # its gateway attempt BEFORE a planar pad-layer run
                    # commits; a soft cost bias cannot beat a successful
                    # planar run.  A decline falls through to the planar
                    # attempt and the usual fallback, unchanged.
                    # Issue #6824: the attempt is confined to the preferred
                    # layers, else the gateway search may land on a
                    # non-preferred layer (In1.Cu when In2.Cu is preferred).
                    pref_layers = (
                        sorted(
                            self._preferred_layers(item.net_class) & set(self._gateway_layers(item))
                        )
                        if pair_gateways and item.key not in self.pair_gateway_skip
                        else []
                    )
                    gateway_first = bool(pref_layers)
                    if gateway_first and not (
                        deadline is not None and time.monotonic() >= deadline
                    ):
                        gres, _greason = self._route_pair_gateway(
                            item,
                            committed=committed,
                            history=history,
                            present=present,
                            deadline=deadline,
                            vias_used=pair_vias_used(item),
                            only_layers=pref_layers,
                        )
                        if gres is not None:
                            pass_gateways[item.key] = gres.gateway or "gateway"
                            pair_outcomes[item.key] = SegmentOutcome.coupled(
                                pass_gateways[item.key]
                            )
                            coupled_count += 1
                            routed_items += 1
                            commit_pair(item, gres)
                            continue
                    pres, preason = self._route_pair_impl(
                        item, committed=committed, history=history, present=present
                    )
                    gateway_retry = (
                        pres is None
                        and pair_gateways
                        and not gateway_first
                        and preason in GATEWAY_RETRY_REASONS
                        and item.key not in self.pair_gateway_skip
                    )
                    if gateway_retry:
                        deferred_gateway.append((item, preason))
                    if pres is None and uncoupled_fallback:
                        # Issue #6373: a declined pair's main run is routed as
                        # two single-ended legs.  Issue #6697: DEFERRED until
                        # every pair has had its coupled attempt (see
                        # ``flush_fallbacks``), so one pair's uncoupled legs
                        # never pre-block a sibling pair's coupled corridor
                        # (board 03: USB_D[0]'s fallback USB_D- leg ran
                        # through USB_D[1]'s corridor).
                        pair_outcomes[item.key] = SegmentOutcome.fallback(preason)
                        fallback_pairs.append(item)
                        deferred_fallback.append(item)
                        continue
                    if pres is None:
                        pair_outcomes[item.key] = SegmentOutcome.declined(preason)
                        if gateway_retry:
                            continue  # reason/failed recorded by flush_gateways
                        reasons[item.key] = preason
                        failed.append((kind, item))
                        continue
                    pair_outcomes[item.key] = SegmentOutcome.coupled()
                    coupled_count += 1
                    routed_items += 1
                    commit_pair(item, pres)
                    continue
                flush_fallbacks()
                key = item[0]
                reason = self._route_single_into(
                    item,
                    routes,
                    committed=committed,
                    history=history,
                    present=present,
                    escape_plan=escape_plan,
                )
                if reason is not None:
                    reasons[key] = reason
                    failed.append((kind, item))
                    continue
                routed_items += 1

            flush_fallbacks()

            score = (
                (routed_items, coupled_count, hybrid_count, fallback_legs)
                if uncoupled_fallback
                else (routed_items, 0, 0, 0)
            )
            if escape_plan and score <= best_score:
                # A local reservation is a proposal, not a permanent lock.
                # Release it when its full pass fails to improve completion.
                escape_plan = {}

            if score > best_score:
                best_score = score
                best_count = routed_items
                best_routes = routes
                best_reasons = reasons
                best_pair_outcomes = pair_outcomes
                best_pair_gateways = pass_gateways
                best_fallback_pairs = len(fallback_pairs)
                best_fallback_legs = fallback_legs
                best_hybrid_pairs = hybrid_count

            # Issue #4472: a deadline-truncated pass is NOT convergence -- some
            # items were never attempted, so ``failed`` is incomplete.  Record
            # the best-so-far above, then stop without claiming convergence.
            if deadline_hit:
                break

            # Issue #6373: a pair served by the uncoupled fallback is routed,
            # but not the outcome negotiation is aiming for.  When its coupled
            # run is geometrically possible on an EMPTY board, the decline was
            # congestion: keep it in the demand set so history pressures the
            # occupants and a later pass can couple it.  A structurally blocked
            # pair (no fresh-board run either) adds no demand and does not
            # hold convergence back.
            # The empty-board probe IS the pair's desired corridor, so it is
            # kept and reused by the demand loop below instead of searching
            # the same fresh board a second time.
            congested_fallback: list[Any] = []
            fallback_probes: dict[object, Any] = {}
            if (fallback_pairs or hybrid_pairs) and it < max_iterations - 1:
                # Issue #6446: a hybrid is better than the fallback but still
                # not the coupled outcome negotiation aims for, so it stays in
                # the probe set and may be recovered as fully ``coupled``.
                for pc in [*fallback_pairs, *hybrid_pairs]:
                    probe, _r = self._route_pair_impl(
                        pc, committed=self._fresh_committed(), history=history, present=present
                    )
                    if probe is not None:
                        congested_fallback.append(pc)
                        fallback_probes[pc.key] = probe

            if not failed and not congested_fallback:
                converged = True
                break
            if it == max_iterations - 1:
                break
            failed.extend((1, pc) for pc in congested_fallback)

            if it == 0:
                escape_plan = plan_tapered_escapes(self, connections, deadline)

            # Failed-net demand: bump history on the resources each blocked
            # net WANTS (its corridor with no committed copper in the way) so
            # the occupying nets are pressured to detour next pass.
            increment = history_increment_factor * self.rules.cost_congestion * present
            for kind, item in failed:
                if kind == 1:
                    desired_pair = fallback_probes.get(item.key)
                    if desired_pair is None:
                        desired_pair, _r = self._route_pair_impl(
                            item,
                            committed=self._fresh_committed(),
                            history=history,
                            present=present,
                        )
                    desired_resources = desired_pair.resources if desired_pair is not None else None
                    if desired_pair is not None:
                        half, clearance = self._conn_geometry(item.net_class)
                        for leg_net, layer, points in desired_pair.runs:
                            for a, b in zip(points, points[1:], strict=False):
                                history.add_segment(
                                    a, b, layer, leg_net, half, clearance, increment
                                )
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
                    if desired is not None:
                        _half, clearance = self._conn_geometry(net_class)
                        for layer, points, widths in desired.runs:
                            for a, b, width in zip(points[:-1], points[1:], widths, strict=True):
                                history.add_segment(
                                    a, b, layer, start.net, width / 2, clearance, increment
                                )
                        for point in desired.via_points:
                            for layer in range(self.num_layers):
                                history.add_segment(
                                    point,
                                    point,
                                    layer,
                                    start.net,
                                    self.rules.via_diameter / 2,
                                    clearance,
                                    increment,
                                )
                if desired_resources is None:
                    continue
                for resource in desired_resources:
                    history[resource] = history.get(resource, 0.0) + increment
            present *= present_cost_growth

        self.failure_reasons = best_reasons
        self.pair_outcomes = best_pair_outcomes
        self.pair_gateways = best_pair_gateways
        # Issue #6395: hold the best pass's single-ended copper to each pad's
        # own net class (``max(own, pad)``).  Pair legs (coupled and
        # uncoupled-fallback) stay put and count as foreign copper.  Issue
        # #6437: the rest of a diff-pair NET's copper (extra-pad stubs, a
        # declined pair routed on the ordinary star) moves only within a
        # per-net length budget, so the pair's skew cannot drift.
        self.class_pad_repair_stats = None
        if self.pad_class_clearances is not None and best_routes:
            from .class_pad_repair import repair_class_pad_pairs

            net_classes: dict[object, object | None] = {c[0]: c[3] for c in connections}
            for pc in pairs:
                for pol in ("P", "N"):
                    net_classes[(pc.key, pol)] = pc.net_class
            pair_ids: dict[int, int] = dict(pair_nets or {})
            for pc in pairs:
                pair_ids[pc.pad_p_a.net] = pc.pad_n_a.net
                pair_ids[pc.pad_n_a.net] = pc.pad_p_a.net
            self.class_pad_repair_stats = repair_class_pad_pairs(
                self,
                best_routes,
                {key: net_classes.get(key) for key in best_routes},
                repairable={c[0] for c in connections} & set(best_routes),
                pair_nets=pair_ids,
            )
        # Issue #4477: the budget the CALLER granted for this call is the
        # remaining time between the deadline and this call's start -- an
        # honest "elapsed of a Ys budget" figure even when the deadline was
        # never reached.  ``None`` when the negotiation ran unbudgeted.
        budget_s = max(0.0, deadline - start_time) if deadline is not None else None
        # Issue #6373: a fallback pair is two single-ended legs, so it counts
        # as two units in both ``routed`` and ``total``.  ``best_count`` counts
        # a fully-routed fallback pair once; add its second leg, plus every
        # routed leg of a partially-routed fallback pair.
        routed_units = best_count if best_count >= 0 else 0
        if best_fallback_pairs:
            full_pairs = sum(
                1
                for pc in pairs
                if best_pair_outcomes.get(pc.key, "").startswith("uncoupled-fallback:")
                and (pc.key, "P") in best_routes
                and (pc.key, "N") in best_routes
            )
            routed_units += best_fallback_legs - full_pairs
        # Issue #6446: a hybrid pair is a trunk plus two single-ended legs, so
        # like a fallback pair it counts as two units -- swapping a fallback
        # for a hybrid must not read as a lost connection to the callers that
        # compare ``routed`` (the #6489 pad-growth rounds, the gateway rollback).
        routed_units += best_hybrid_pairs
        stats = LatticeNegotiationStats(
            iterations=iterations_run,
            converged=converged,
            routed=routed_units,
            total=len(connections) + len(pairs) + best_fallback_pairs + best_hybrid_pairs,
            lattice_builds=self.lattice_builds,
            deadline_hit=deadline_hit,
            elapsed_s=time.monotonic() - start_time,
            budget_s=budget_s,
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
