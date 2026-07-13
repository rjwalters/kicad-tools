"""Differential pair routing integration for the autorouter.

This module provides differential pair-aware routing functionality
that coordinates differential pair routing with the main autorouter.

Key features:
- Coupled A* pathfinding that routes both traces simultaneously
- Maintains constant spacing between P/N traces
- Length matching with serpentine compensation
"""

from __future__ import annotations

import collections
import heapq
import itertools
import logging
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .core import Autorouter
    from .cpp_backend import CppCoupledPathfinder
    from .grid import RoutingGrid
    from .rules import DesignRules, NetClassRouting

import contextlib

from .diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    LengthMismatchWarning,
    analyze_differential_pairs,
    should_engage_coupled,
)
from .diffpair_detection import (
    detect_diff_pairs as _layered_detect_diff_pairs,
)
from .layers import Layer
from .observability import validate_net_connectivity
from .path import calculate_route_length
from .primitives import Pad, Route, Segment, Via
from .quantize import (
    OffAngleSegmentError,
    dogleg_points,
    verify_segment_45,
)

logger = logging.getLogger(__name__)

# Issue #3508: dev-only periodic A* pop trace (KCT_COUPLED_TRACE=1).
_COUPLED_TRACE = bool(os.environ.get("KCT_COUPLED_TRACE"))

# Issue #3508: weighted-A* factor used by ``route_differential_pair_coupled``
# when constructing the per-pair :class:`CoupledPathfinder`.  See the
# ``heuristic_weight`` rationale in ``CoupledPathfinder.__init__``.
# Env-overridable for experimentation (KCT_COUPLED_HEURISTIC_WEIGHT).
COUPLED_HEURISTIC_WEIGHT: float = float(os.environ.get("KCT_COUPLED_HEURISTIC_WEIGHT", "1.5"))

# Issue #3547: default per-pair ITERATION budget for the coupled search
# when the shadow constructor is OFF (the default) and the caller plumbed
# no explicit budget.  The weighted-A* upgrade (#3508) is gated behind
# ``enable_shadow_construction``; with the flag off the search falls back
# to classic optimal A* (``heuristic_weight=1.0``), which floods the
# cost_turn-deep f-plateaus and explores far more joint states than the
# weighted search before reaching the ``cols * rows * 4`` memory backstop.
# On a deferring fixture (e.g. the DQS-like polarity-swap) classic A*
# grinds the full backstop -- ~75k iterations, ~2x the weighted search's
# wall-clock -- pushing existing flag-off tests to the CI 60s timeout.
# The contract for the flag-off path is "may only DEFER" (it never
# attempts coupled convergence -- that is #3508/#3542), so an unbounded
# grind is itself a smell: cap the classic-A* search at a budget that
# lets genuinely fast-converging pairs succeed while a deferring pair
# bails promptly (sets ``last_timeout_exceeded`` -> independent
# fallback), restoring the pre-#3508 budget-exit behaviour.  Env-
# overridable for experimentation (KCT_COUPLED_FLAGOFF_MAX_ITERS).  Only
# applied when no explicit ``per_pair_max_iterations`` was plumbed --
# callers that set a budget (the re-route gate, board configs) keep it.
#
# Issue #3547 (doctor follow-up): the first cap (16000 total, 8000/phase)
# was BELOW the classic-A* convergence floor for at least one flag-off
# pair the search CAN solve -- the pitch-mismatch USB fixture
# (test_diffpair_npad.py::test_pitch_mismatch_diff_pair_routes,
# ``coupled_only=True`` so NO independent fallback) reached
# ``best_progress=2`` (two grid cells from the joint goal) then bailed at
# the 8000-iter corridor cap, returning ``[]``.  That changed the OUTCOME
# (a pair that used to route coupled produced no routes) rather than
# merely DEFERRING -- a flag-off contract violation.  Empirically the npad
# fixture converges between 9000 and 12000 iters/phase; the DQS-like
# deferring fixture grinds the full ``cols*rows*4`` (~75k) and lands ~5s
# on the CI no-coverage path regardless.  40000 total (20000/phase) sits
# in that wide window: ~1.7x above npad's convergence floor (npad passes
# in ~1.3s) while DQS still bails comfortably under the 60s CI timeout.
COUPLED_FLAGOFF_MAX_ITERATIONS: int = int(os.environ.get("KCT_COUPLED_FLAGOFF_MAX_ITERS", "40000"))

# Issue #3508: max joint remaining Manhattan distance (grid cells, max
# over the two heads) at which a budget-exited coupled search qualifies
# for the near-miss rescue (commit the coupled body, finish each side
# single-ended).  60 cells = 3 mm on a 0.05 mm grid -- generous against
# the measured 5-21-cell stalls, small against the 30-50 mm pair routes
# so the coupled-length fraction stays >> every continuity threshold.
NEAR_MISS_RESCUE_CELLS: int = int(os.environ.get("KCT_COUPLED_RESCUE_CELLS", "60"))

# Issue #3508: maximum length (mm) the shadow constructor may trim from
# EACH end of the offset polyline before tail-connecting to the pads.
# Endpoint zones are always contested by neighbour-pad clearance halos
# (connector pin rows, QFN/QFP rings), so some trim is expected; a trim
# beyond this bound means a mid-route obstacle wall and the shadow is
# declined for that side.  5 mm against board 06's 18-45 mm pair runs
# keeps the coupled-length fraction comfortably above the 0.7-0.9
# continuity thresholds.
_SHADOW_MAX_TRIM_MM: float = float(os.environ.get("KCT_SHADOW_MAX_TRIM_MM", "5.0"))

# Issue #3987 (unit 2a of #3921): hard per-pair wall-clock budget for the
# shadow-construction coupled attempt.  When ``enable_shadow_construction``
# is on, a pair is routed either as a validated parallel shadow (ms) or it
# is DEFERRED to the uncoupled fallback -- it must NOT fall through to the
# open joint-state A* search, which floods the cost_turn f-plateaus and
# drove the >1200s tail the #3986 board-06 measurements documented (6 of 9
# failed-shadow pairs each burned a ~45s corridor probe plus the negotiated
# 360s backstop).  This budget bounds the corridor probe + shadow
# construction per pair; on shadow failure the search fails fast to the
# uncoupled fallback without re-flooding the open A*.  Env-overridable for
# experimentation (KCT_SHADOW_PER_PAIR_BUDGET_S).
_SHADOW_PER_PAIR_BUDGET_S: float = float(os.environ.get("KCT_SHADOW_PER_PAIR_BUDGET_S", "30.0"))

# Issue #3990 (unit 2b of #3921): variable-gap parallel offset.  The
# geometric shadow constructor historically offset the WHOLE guide by a
# single constant center-to-center gap ``d = spacing_cells * resolution``.
# At the tightened 0.225-0.275 mm coupled widths this fixed gap is
# infeasible for 6/9 board-06 pairs: on inside curves the offset overlaps
# the partner (the -0.165..-0.275 mm ``self-check overlap`` events) and
# where the guide threaded a gap only wide enough for a zero-width
# centerline the offset crosses an obstacle (the ``mid-route blockage``
# events).  Because the diff-pair coupling constraint is a clearance BAND
# (a floor set by ``effective_intra_pair_clearance()`` and a ceiling set by
# the impedance tolerance), the offset gap may be varied PER SECTION within
# ``[d_min, d_max]`` to dodge both failure modes -- tighten toward
# ``d_min`` on an inside curve that would self-overlap, widen toward
# ``d_max`` to step around an obstacle -- while both legs stay inside the
# impedance band.  ``_SHADOW_GAP_BAND_STEPS`` is the number of candidate
# gaps probed per section (a small linear ladder from ``d_min`` to
# ``d_max``); the tightest feasible gap that clears both the partner and
# the grid is kept.  ``_SHADOW_GAP_MAX_TOL_FRAC`` caps how far above the
# nominal gap the ceiling may reach when the net class exposes no explicit
# impedance tolerance.  Env-overridable for bench experimentation.
_SHADOW_GAP_BAND_STEPS: int = int(os.environ.get("KCT_SHADOW_GAP_BAND_STEPS", "5"))
_SHADOW_GAP_MAX_TOL_FRAC: float = float(os.environ.get("KCT_SHADOW_GAP_MAX_TOL_FRAC", "0.15"))


# ---------------------------------------------------------------------------
# Issue #3023 Phase A: intra-pair clearance violation detection
# ---------------------------------------------------------------------------
#
# Phase A is observability-only.  After ``CoupledPathfinder`` produces a
# (p_route, n_route) for a diff pair, we re-check every same-layer
# segment-pair using ``segment_clearance`` against the per-pair
# ``NetClassRouting.effective_intra_pair_clearance()`` and emit a
# structured record (and a ``logger.info`` line) for any pair whose
# routed clearance is below the threshold.
#
# This is the SAME idiom ``match_pair_lengths`` already uses at
# diffpair_routing.py:1033-1053 to reject a serpentine bulge that would
# violate the partner; here we apply it to the post-coupling route as a
# diagnostic so Phase B (the fine-grid repair pass, separate PR) has a
# reproducible target list.
#
# Phase A explicitly does NOT modify any route.  All it does is:
#   1. compute per-segment-pair clearance,
#   2. report violations,
#   3. expose a public accessor for Phase B to consume.


@dataclass
class IntraPairClearanceViolation:
    """A routed intra-pair clearance violation on a differential pair.

    Phase A diagnostic record (Issue #3023): emitted when the routed
    edge-to-edge clearance between a P-segment and an N-segment on the
    same layer falls below the per-pair
    :meth:`NetClassRouting.effective_intra_pair_clearance`.

    Attributes:
        pair_name: Base name of the violating diff pair (e.g. ``"DQS0"``).
        positive_net_name: Net name of the P trace (for log grep-ability).
        negative_net_name: Net name of the N trace.
        expected_clearance_mm: The per-pair threshold (from
            ``NetClassRouting.effective_intra_pair_clearance()``).
        actual_clearance_mm: The minimum edge-to-edge clearance found
            across all same-layer segment pairs.  ``< expected_clearance_mm``.
        violation_magnitude_mm: ``expected_clearance_mm - actual_clearance_mm``
            (always positive when a violation is recorded).
        layer: KiCad layer name where the worst violation occurred.
        p_segment: The P-side segment involved in the worst violation.
        n_segment: The N-side segment involved in the worst violation.
        segment_violations: All same-layer (p_seg, n_seg, clearance) triples
            that fell below ``expected_clearance_mm``.  Phase B (repair
            pass) consumes this list to scope the corridor for the
            fine-grid sub-search.
    """

    pair_name: str
    positive_net_name: str
    negative_net_name: str
    expected_clearance_mm: float
    actual_clearance_mm: float
    violation_magnitude_mm: float
    layer: str
    p_segment: Segment
    n_segment: Segment
    segment_violations: list[tuple[Segment, Segment, float]] = field(default_factory=list)


def find_intra_pair_clearance_violations(
    p_route: Route,
    n_route: Route,
    threshold_mm: float,
    pair_name: str = "",
) -> IntraPairClearanceViolation | None:
    """Detect intra-pair clearance violations on a routed differential pair.

    Walks every same-layer (p-segment, n-segment) pair and computes the
    edge-to-edge clearance via :func:`core.geometry.segment_clearance`.
    Returns ``None`` when no violation is found, otherwise a single
    :class:`IntraPairClearanceViolation` summarising the worst case and
    listing every offending segment pair for downstream consumption.

    This is the SAME segment-clearance idiom ``match_pair_lengths`` uses
    at ``diffpair_routing.py:1033-1053`` to reject would-be serpentine
    bulges, lifted into a reusable detector so the route-time check in
    ``route_differential_pair_coupled`` and the post-route audit in
    :meth:`DiffPairRouter.intra_clearance_violations` share one
    implementation.

    Args:
        p_route: The positive trace route.
        n_route: The negative trace route.
        threshold_mm: The per-pair clearance floor.  Pass
            ``NetClassRouting.effective_intra_pair_clearance()`` from
            the route's net class -- NOT the global
            ``DifferentialPairRules.spacing``, which is a heuristic
            default that does not reflect the per-pair override.
        pair_name: Base pair name for the returned record (e.g.
            ``"DQS0"``).  Defaults to the empty string when the caller
            doesn't have a structured pair handy.

    Returns:
        ``None`` when every same-layer segment-pair meets the threshold;
        otherwise an :class:`IntraPairClearanceViolation` whose
        ``segment_violations`` list contains every offending pair and
        whose top-level fields summarise the worst case.
    """
    from kicad_tools.core.geometry import segment_clearance

    if p_route is None or n_route is None:
        return None
    if not p_route.segments or not n_route.segments:
        return None

    offenders: list[tuple[Segment, Segment, float]] = []
    worst_clearance = float("inf")
    worst_pair: tuple[Segment, Segment] | None = None

    for pseg in p_route.segments:
        for nseg in n_route.segments:
            if pseg.layer != nseg.layer:
                continue
            clearance = segment_clearance(
                pseg.x1,
                pseg.y1,
                pseg.x2,
                pseg.y2,
                pseg.width,
                nseg.x1,
                nseg.y1,
                nseg.x2,
                nseg.y2,
                nseg.width,
            )
            # 1e-9 tolerance matches the serpentine self-check at
            # diffpair_routing.py:1052 -- floating-point equality at the
            # threshold counts as compliant.
            if clearance + 1e-9 < threshold_mm:
                offenders.append((pseg, nseg, clearance))
                if clearance < worst_clearance:
                    worst_clearance = clearance
                    worst_pair = (pseg, nseg)

    if not offenders or worst_pair is None:
        return None

    worst_p, worst_n = worst_pair
    return IntraPairClearanceViolation(
        pair_name=pair_name,
        positive_net_name=p_route.net_name,
        negative_net_name=n_route.net_name,
        expected_clearance_mm=threshold_mm,
        actual_clearance_mm=worst_clearance,
        violation_magnitude_mm=threshold_mm - worst_clearance,
        layer=worst_p.layer.kicad_name
        if hasattr(worst_p.layer, "kicad_name")
        else str(worst_p.layer),
        p_segment=worst_p,
        n_segment=worst_n,
        segment_violations=offenders,
    )


class PairOrientation(Enum):
    """Orientation of the differential pair traces."""

    HORIZONTAL = "horizontal"  # P above N (or vice versa), traces run horizontally
    VERTICAL = "vertical"  # P left of N (or vice versa), traces run vertically


@dataclass
class GridPos:
    """Grid position for coupled routing."""

    x: int
    y: int
    layer: int

    def __hash__(self) -> int:
        return hash((self.x, self.y, self.layer))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GridPos):
            return NotImplemented
        return self.x == other.x and self.y == other.y and self.layer == other.layer

    def __add__(self, other: tuple[int, int, int]) -> GridPos:
        return GridPos(self.x + other[0], self.y + other[1], self.layer + other[2])


@dataclass
class CoupledState:
    """State for coupled differential pair A* search.

    Represents the position of both P and N traces simultaneously.
    Both traces must move together to maintain constant spacing.
    """

    p_pos: GridPos  # Positive trace position
    n_pos: GridPos  # Negative trace position
    direction: tuple[int, int]  # Current routing direction (dx, dy)

    def __hash__(self) -> int:
        return hash((self.p_pos, self.n_pos, self.direction))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CoupledState):
            return NotImplemented
        return (
            self.p_pos == other.p_pos
            and self.n_pos == other.n_pos
            and self.direction == other.direction
        )

    @property
    def spacing(self) -> float:
        """Calculate current spacing between P and N traces."""
        dx = self.p_pos.x - self.n_pos.x
        dy = self.p_pos.y - self.n_pos.y
        return math.sqrt(dx * dx + dy * dy)


@dataclass
class CoupledSegmentSpec:
    """Specification for a single coupled-routing segment.

    Issue #2473: For N-pad differential pairs (e.g., USB-C connectors),
    the pair-up step now produces a list of these specs rather than a
    flat 4-tuple, so the driver can run the coupled pathfinder for each
    spec and feed the leftover stub edges to the independent router.
    """

    p_start: Pad
    p_end: Pad
    n_start: Pad
    n_end: Pad
    # ``True`` when the orientation of (p_start, n_start) is the
    # mirror of (p_end, n_end) — i.e., the pair must perform a
    # coordinated layer-swap crossover during routing.  The
    # CoupledPathfinder consumes this hint to enable swap-via moves.
    polarity_swap: bool = False


@dataclass
class StubEdgeSpec:
    """Specification for a single-net stub edge.

    Issue #2473: When a differential pair has more than 2 pads on
    a side (e.g., USB-C A6 + B6 paralleled into the same net),
    the extra pads connect to the main coupled run via short
    independent stubs.  These are routed via the standard
    autorouter rather than the CoupledPathfinder.
    """

    start: Pad
    end: Pad


@dataclass(order=True)
class CoupledNode:
    """Node for coupled A* priority queue.

    Issue #3144: ``seq`` is a monotonic insertion counter assigned at
    push-time from a search-local counter.  It serves as a secondary
    tie-break key after ``f_score`` so that nodes with identical f-scores
    pop in a deterministic, insertion-order-preserving order.  Without
    this, ``heapq`` falls through to structural comparison on the next
    compared field, and a ``dataclass(order=True)`` containing
    ``CoupledState`` values with no explicit ``__lt__`` would raise
    ``TypeError``.  Previously all fields after ``f_score`` were
    ``compare=False``, which left the heap-ordering invariant entirely
    dependent on push order -- a non-deterministic property under CI
    load (the Python interpreter's heap reshuffle visits sibling
    f_score-equal nodes in an order that can vary with allocator state).

    A monotonic counter is cheap (one ``int`` compare per heap op) and
    eliminates this entire class of non-determinism without changing
    A*'s optimality guarantees.  The convention "lower seq wins on
    f_score tie" mirrors the C++ ``AStarNode::operator>`` invariant
    introduced in the same fix.

    Callers MUST pass ``seq`` explicitly at construction time
    (typically ``seq=next(seq_iter)`` where ``seq_iter = itertools.count()``
    is search-local).  A default of ``0`` is provided only to keep the
    dataclass declaration well-formed; constructing two nodes with the
    same default ``seq`` value would re-introduce the ordering ambiguity
    this field exists to eliminate.
    """

    f_score: float
    g_score: float = field(compare=False)
    state: CoupledState = field(compare=False)
    parent: CoupledNode | None = field(compare=False, default=None)
    via_from_parent: bool = field(compare=False, default=False)
    seq: int = field(compare=True, default=0)


def build_corridor_mask(
    grid: RoutingGrid,
    guide_route: Route,
    radius_cells: int,
    extra_cells: tuple[tuple[int, int], ...] = (),
) -> frozenset[tuple[int, int]]:
    """Build a layer-agnostic corridor mask from a single-ended guide route.

    Issue #3439: the coupled diff-pair A* searches the joint
    ``(p_pos, n_pos)`` product state space, which is roughly quadratic
    in the open-grid single-net search and intractable in pure Python
    on a 4-layer 110x95 mm grid (~14k iterations/min vs the millions
    needed).  Restricting both traces to a corridor dilated around a
    known-routable single-ended path (found by the C++-accelerated
    per-net router) converts the open 2D search into a near-1D one,
    which the pure-Python coupled search completes in seconds.

    The mask is intentionally layer-agnostic: the coupled search keeps
    full freedom to choose layers (paired vias) within the spatial
    tube.  This avoids over-constraining the pair to the guide path's
    exact layer sequence, which was computed for a single trace and may
    not have room for two.

    Args:
        grid: The routing grid (used for world->grid conversion and
            bounds clamping).
        guide_route: A single-ended :class:`Route` whose segments trace
            the spatial path to dilate (typically the P-side of the
            pair, routed by the standard per-net pathfinder WITHOUT
            being committed to the grid).
        radius_cells: Chebyshev dilation radius in grid cells.  Must be
            at least the pair's center-to-center spacing plus a few
            cells of maneuvering slack so the N trace fits alongside
            the guide path and the search can detour around local
            obstacles.
        extra_cells: Additional ``(x, y)`` grid cells to include (with
            the same dilation), e.g. the four pad endpoint cells so the
            N-side endpoints are always inside the corridor even when
            the start/end pad pitch exceeds ``radius_cells``.

    Returns:
        Frozenset of in-bounds ``(x, y)`` grid cells forming the
        corridor.
    """
    base_cells: set[tuple[int, int]] = set(extra_cells)

    for seg in guide_route.segments:
        gx1, gy1 = grid.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = grid.world_to_grid(seg.x2, seg.y2)
        steps = max(abs(gx2 - gx1), abs(gy2 - gy1))
        if steps == 0:
            base_cells.add((gx1, gy1))
            continue
        for i in range(steps + 1):
            t = i / steps
            base_cells.add(
                (
                    int(round(gx1 + (gx2 - gx1) * t)),
                    int(round(gy1 + (gy2 - gy1) * t)),
                )
            )

    for via in guide_route.vias:
        base_cells.add(grid.world_to_grid(via.x, via.y))

    radius = max(0, int(radius_cells))
    corridor: set[tuple[int, int]] = set()
    cols, rows = grid.cols, grid.rows
    for cx, cy in base_cells:
        for dx in range(-radius, radius + 1):
            x = cx + dx
            if x < 0 or x >= cols:
                continue
            for dy in range(-radius, radius + 1):
                y = cy + dy
                if 0 <= y < rows:
                    corridor.add((x, y))

    return frozenset(corridor)


class CoupledPathfinder:
    """A* pathfinder for coupled differential pair routing.

    Routes both P and N traces simultaneously, maintaining constant
    spacing between them throughout the path.
    """

    # Issue #3089: how often (in A* iterations) the wall-clock budget is
    # consulted inside ``route_coupled``.  ``time.monotonic()`` is fast
    # (~100 ns) but the coupled A* iteration body is heavy (path-history
    # walk + neighbour generation + heap push), so the per-iter cost is
    # 1-10 ms on board 06's BGA-49 escape.  Checking every 64 iterations
    # keeps the overhead < 0.01 % while bounding the late-exit lateness
    # to a few hundred milliseconds on any reasonable workload.  Must be
    # a power of two (the check uses a bitmask).
    _TIMEOUT_CHECK_INTERVAL = 64

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        target_spacing_cells: int,
        net_class_map: dict[str, NetClassRouting] | None = None,
        allow_swap_via: bool = False,
        min_spacing_cells: int = 0,
        heuristic_mode: Literal["manhattan_sum", "partner_aware"] = "partner_aware",
        spacing_penalty_factor: float = 0.25,
        heuristic_weight: float = 1.0,
    ):
        """Initialize coupled pathfinder.

        Args:
            grid: The routing grid
            rules: Design rules for routing
            target_spacing_cells: Target spacing between P/N in grid cells
            net_class_map: Optional net class map for per-net trace widths
            allow_swap_via: Issue #2473: When True, the pathfinder may
                place a paired layer-swap that exchanges the P/N grid
                positions across an inner layer.  Used when source and
                sink polarity orientations are mirrored (USB-C-shaped
                pads).
            min_spacing_cells: Issue #3012: Hard floor on the center-to-
                center spacing (in grid cells) the search will tolerate
                between P and N positions.  Derived in
                ``route_differential_pair_coupled`` from
                ``(trace_width + intra_pair_clearance) / grid.resolution``
                so that within-pair edge-to-edge clearance is preserved
                even when the approach-phase tolerance widens or the
                asymmetric "converge" moves fire.  Defaults to ``0``
                (legacy permissive behaviour) so callers that don't
                supply per-pair NetClassRouting are unaffected.
                Endpoint cells (start and goal pad positions) are
                exempt from the floor -- those are the cells the pads
                themselves occupy and the floor would otherwise
                disqualify the search's only chance to land.
            heuristic_mode: Issue #3115 (angle #5): Selects the A* heuristic
                used by :meth:`_heuristic`.

                * ``"manhattan_sum"`` (legacy):
                  ``(p_dist + n_dist) * cost_straight + layer_cost``.
                  Sums the Manhattan distance from each trace to its
                  goal -- biases the search away from partner-
                  synchronised moves because every symmetric step
                  reduces the sum by 2 while every asymmetric (P-only
                  or N-only) step reduces it by 1, even though both
                  cost the same on a coupled run.
                * ``"partner_aware"`` (default, Issue #3115): uses
                  ``max(p_dist, n_dist) * cost_straight + spacing_penalty
                  + layer_cost``.  Still admissible (every real path
                  must advance the slower trace at least ``max(p_dist,
                  n_dist)`` cells), but ranks partner-synchronised
                  moves higher in the priority queue.  Reduces the
                  asymmetric-escape pathology that produces the
                  ``diffpair_clearance_intra`` cluster on board 06.
                  ``spacing_penalty`` is a sub-cost-per-step term that
                  penalises states whose current center-to-center
                  spacing diverges from ``target_spacing_cells`` (see
                  ``spacing_penalty_factor``).
            spacing_penalty_factor: Issue #3115: Multiplier applied to
                the ``abs(current_spacing - target_spacing_cells) *
                cost_straight`` spacing penalty term in the
                ``"partner_aware"`` heuristic.  Bounded above by ``1.0``
                analytically (each cell-of-spacing-divergence costs at
                most one ``cost_straight`` of true path cost to correct
                via the asymmetric converge move), so any factor ``<=
                1.0`` keeps the heuristic admissible.  Default ``0.25``
                is the smallest value the synthetic-pair regression
                test under :mod:`tests.test_diffpair_phase_b` empirically
                showed lifts the asymmetric-escape case.  Ignored when
                ``heuristic_mode == "manhattan_sum"``.
        """
        self.grid = grid
        self.rules = rules
        self.target_spacing_cells = target_spacing_cells
        self.net_class_map = net_class_map or {}
        self.allow_swap_via = allow_swap_via
        # Issue #3012: store the within-pair spacing floor.  ``0`` means
        # no floor (legacy behaviour).
        self.min_spacing_cells = max(0, int(min_spacing_cells))
        # Issue #3115: heuristic mode + spacing-penalty factor.  Clamp
        # the factor to [0, 1] to preserve admissibility -- any value
        # > 1 risks ranking states whose required correction cost
        # exceeds the true remaining-path cost, which would let A*
        # return a sub-optimal route.
        if heuristic_mode not in ("manhattan_sum", "partner_aware"):
            raise ValueError(
                f"heuristic_mode must be 'manhattan_sum' or 'partner_aware'; got {heuristic_mode!r}"
            )
        self.heuristic_mode: Literal["manhattan_sum", "partner_aware"] = heuristic_mode
        self.spacing_penalty_factor = max(0.0, min(1.0, float(spacing_penalty_factor)))

        # Issue #3508: weighted-A* factor applied to the heuristic when
        # computing ``f = g + weight * h``.  ``1.0`` is classic optimal
        # A*.  Values > 1 trade path-cost optimality for search effort:
        # the coupled joint-state space has DEEP f-plateaus (a single
        # ``cost_turn`` = 5 shell can hold ~90k states on board 06's
        # 0.05 mm grid -- measured: MIPI_CLK needed ~95k iterations to
        # flood ONE such basin before escaping, then cruised 350 cells
        # in <17k).  Weighting the heuristic makes goal-ward gradient
        # dominate shell-flooding so converging pairs land within a
        # CI-affordable iteration budget.  Slightly longer paths are an
        # acceptable trade: pair length-matching is enforced post-route
        # (serpentine / Phase 3I tuner), and the corridor mask already
        # bounds how far from the guide path the route can wander.
        self.heuristic_weight = max(1.0, float(heuristic_weight))
        # Issue #3089: set when the most-recent ``route_coupled`` call
        # exited early due to ``timeout_seconds`` being exceeded.
        # Callers (``route_differential_pair_coupled``) read this to
        # distinguish a budget-exit (where the slow per-net independent
        # fallback would also blow the budget) from a true "no path
        # found" exit (where independent routing is still worth trying).
        # Reset to ``False`` at the start of every ``route_coupled``
        # invocation.
        self.last_timeout_exceeded: bool = False

        # Issue #3921: disambiguates WHICH budget fired when
        # ``last_timeout_exceeded`` is True.  ``route_coupled`` sets the
        # shared ``last_timeout_exceeded`` flag for both the iteration
        # budget (``max_iterations_budget``) and the wall-clock budget
        # (``timeout_seconds``), so the caller's budget-exit diagnostic
        # cannot tell a 0.3s iteration bail from a 120s wall-clock bail.
        # ``True`` means the ITERATION budget was the binding constraint;
        # ``False`` (with ``last_timeout_exceeded`` True) means the
        # wall-clock budget fired.  Reset to ``False`` at the start of
        # every ``route_coupled`` invocation.
        self.last_iteration_limited: bool = False

        # Issue #3473 (review of #3439): number of A* iterations the
        # most recent ``route_coupled`` call consumed.  The two-phase
        # corridor-then-open caller charges the corridor attempt's
        # iterations against the shared per-pair iteration budget,
        # mirroring the wall-clock split -- otherwise a failing pair
        # receives the FULL ``max_iterations_budget`` twice (observed
        # 4000+4000 on board 06, doubling the diff-pair phase).
        self.last_iterations: int = 0

        # Issue #3508: progress diagnostics for the most recent
        # ``route_coupled`` call (smallest joint remaining Manhattan
        # distance any popped state achieved, and the state/node
        # achieving it).  Lets budget-exit handlers distinguish
        # "almost converged" from "structurally stuck", and gives the
        # near-miss rescue (``_rescue_near_miss_coupled``) a parent
        # chain to reconstruct the partial coupled route from.
        self.last_best_progress: float = float("inf")
        self.last_best_state: CoupledState | None = None
        self.last_best_node: CoupledNode | None = None

        # Issue #3508: per-search move-rejection counters keyed by
        # rejection reason (sym/asym x blocked/spacing/floor/trail,
        # plus corridor pruning).  Reset by ``route_coupled``;
        # surfaced by the caller's budget-exit diagnostics so a
        # stalled search reports WHAT is pruning its frontier.
        self.last_rejections: dict[str, int] = collections.defaultdict(int)

        # Pre-calculate trace clearance radius
        self._trace_half_width_cells = max(
            1,
            math.ceil(
                (self.rules.trace_width / 2 + self.rules.trace_clearance) / self.grid.resolution
            ),
        )

        # Pre-calculate via blocking radius
        self._via_half_cells = max(
            1,
            math.ceil(
                (self.rules.via_diameter / 2 + self.rules.via_clearance) / self.grid.resolution
            ),
        )

        # Issue #3508: extra radial slack a via needs BEYOND the trace
        # envelope the grid cells already encode (see _is_via_blocked).
        self._via_extra_cells = max(
            1,
            math.ceil(
                max(
                    0.0,
                    (self.rules.via_diameter / 2 + self.rules.via_clearance)
                    - (self.rules.trace_width / 2 + self.rules.trace_clearance),
                )
                / self.grid.resolution
            ),
        )

        # Orthogonal moves only for differential pairs (diagonal moves
        # would complicate spacing maintenance)
        self.directions = [
            (1, 0),  # Right
            (-1, 0),  # Left
            (0, 1),  # Down
            (0, -1),  # Up
        ]

        # Issue #4065: opt-in flag for the C++ coupled joint-state A*.
        # Default ON when a C++ backend is present; the search still falls
        # back to pure Python for the v1-deferred features
        # (``allow_swap_via``, ``manhattan_sum``) and whenever construction
        # of the C++ pathfinder raises.  Env-overridable
        # (KCT_COUPLED_CPP=0) so measurement / parity tests can force the
        # Python path without monkeypatching.
        self._use_cpp_coupled = os.environ.get("KCT_COUPLED_CPP", "1") != "0"
        # Cached (CppGrid, CppCoupledPathfinder) keyed by grid identity so
        # the one-time grid marshalling + pathfinder construction is reused
        # across route_coupled calls on the same pathfinder instance
        # (mirrors how CppPathfinder is constructed once and reused).
        self._cpp_coupled_impl: CppCoupledPathfinder | None = None
        self._cpp_coupled_grid: RoutingGrid | None = None

    def _is_cell_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if a cell is blocked for this net.

        Issue #3508: own-net cells are passable, matching the per-net
        pathfinder's ``different_net = cell.net != routing_net``
        convention.  The previous implementation additionally rejected
        any ``is_obstacle`` cell -- but ``RoutingGrid._add_pad_unsafe``
        sets ``is_obstacle = True`` on a pad's OWN clearance-halo and
        metal cells (it exists to defeat the negotiated-mode
        ``static_blocks`` release loophole, #2915/#2940, with own-net
        passability explicitly preserved via the ``cell.net`` check in
        the main pathfinder).  ORing ``is_obstacle`` here made every
        pad unreachable for its own coupled route: the joint search
        stalled at exactly the halo boundary (5-7 cells out) on all 9
        board 06 pairs, which is why coupled convergence was 0/9 at
        ANY budget.  True obstacles (board edge, keepouts,
        ``add_obstacle`` regions) carry ``cell.net == 0`` and remain
        blocked for every signal net.
        """
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return True
        if layer < 0 or layer >= self.grid.num_layers:
            return True

        cell = self.grid.grid[layer][gy][gx]
        if cell.blocked and cell.net != net:
            return True
        return False

    def _is_trace_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if placing a trace centerline at this cell would conflict.

        Issue #3508: this checks ONLY the head cell, matching the per-net
        pathfinder's convention.  The grid already encodes the full
        centerline clearance envelope at obstacle-marking time: pads are
        dilated by ``trace_clearance + trace_width / 2``
        (``RoutingGrid._clearance_for_pin_pitch``) and committed routes
        by the equivalent trace halo, so a cell is unblocked exactly
        when a trace centerline there satisfies clearance.  The previous
        implementation swept an ADDITIONAL ``(2 * half_width + 1)^2``
        square (+/- ``ceil((trace_width/2 + clearance)/resolution)`` =
        5 cells on board 06) around the head -- double-counting the
        clearance envelope to an effective ~0.45 mm.  In open field that
        was merely conservative; entering any fine-pitch pad
        neighbourhood (QFN-32/QFN-24/BGA-49/USB-C/FFC -- BOTH endpoints
        of every board 06 pair) it formed an impassable wall ~1-2.5 mm
        short of the pads, which is why every coupled search stalled at
        an identical frontier regardless of iteration budget or
        tie-break order (best_progress 34-49 on PCIE/USB2 across
        FIFO/LIFO and 10k/90k-iteration runs).
        """
        return self._is_cell_blocked(gx, gy, layer, net)

    def _is_via_blocked(self, gx: int, gy: int, net: int) -> bool:
        """Check if placing a via at this position would conflict on any layer.

        Issue #3508: the swept radius is now the DIFFERENCE between the
        via envelope and the trace envelope the grid cells already
        carry (see ``_is_trace_blocked``), not the full via envelope.
        A grid cell is unblocked when a TRACE centerline is legal
        there; a via additionally needs
        ``(via_diameter/2 + via_clearance) - (trace_width/2 +
        trace_clearance)`` of extra radial slack, which is what
        ``_via_extra_cells`` captures.  The previous full-envelope
        sweep (+/-8 cells on every layer on board 06) double-counted
        the marked halo exactly like the trace check did.

        Issue #3508 (second pass): a via must additionally never land
        on PAD METAL -- even its OWN net's pad.  Own-net passability
        (see ``_is_cell_blocked``) made pad cells legal for the via
        predicate too, and the crossing-tail synthesizer promptly
        placed vias exactly on its own goal pads (measured: 2
        ``via_in_pad`` errors at J3-9 / J4-2 on the first #3508
        re-route; the jlcpcb standard tier does not support
        via-in-pad).  ``cell.pad_blocked`` marks cells whose extent
        overlaps continuous pad metal (#3233), so reject the via when
        any cell under its DRILL footprint is pad metal on any layer.
        """
        drill_cells = max(0, int(math.ceil((self.rules.via_drill / 2) / self.grid.resolution)))
        for layer in range(self.grid.num_layers):
            for dy in range(-self._via_extra_cells, self._via_extra_cells + 1):
                for dx in range(-self._via_extra_cells, self._via_extra_cells + 1):
                    if self._is_cell_blocked(gx + dx, gy + dy, layer, net):
                        return True
            # Issue #3508: no via-in-pad regardless of net ownership.
            for dy in range(-drill_cells, drill_cells + 1):
                for dx in range(-drill_cells, drill_cells + 1):
                    cgx, cgy = gx + dx, gy + dy
                    if not (0 <= cgx < self.grid.cols and 0 <= cgy < self.grid.rows):
                        return True
                    if self.grid.grid[layer][cgy][cgx].pad_blocked:
                        return True
        return False

    def _is_at_goal(self, pos: GridPos, goal: GridPos | None) -> bool:
        """Check if a grid position is at the goal (ignoring layer)."""
        if goal is None:
            return False
        return pos.x == goal.x and pos.y == goal.y

    def _get_coupled_neighbors(
        self,
        state: CoupledState,
        p_net: int,
        n_net: int,
        p_goal: GridPos | None = None,
        n_goal: GridPos | None = None,
        p_start: GridPos | None = None,
        n_start: GridPos | None = None,
        target_spacing_cells: int | None = None,
        approach_radius_override: int | None = None,
        departure_radius_override: int | None = None,
        p_visited: frozenset[tuple[int, int, int]] | None = None,
        n_visited: frozenset[tuple[int, int, int]] | None = None,
        p_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] | None = None,
        n_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] | None = None,
    ) -> list[tuple[CoupledState, float, bool]]:
        """Generate valid coupled moves maintaining spacing.

        Args:
            state: Current coupled search state.
            p_net: Net id for the positive trace.
            n_net: Net id for the negative trace.
            p_goal: Goal grid position for the positive trace.
            n_goal: Goal grid position for the negative trace.
            p_start: Start grid position for the positive trace.
            n_start: Start grid position for the negative trace.
            target_spacing_cells: Per-call effective target spacing
                in grid cells.  When ``None``, falls back to
                ``self.target_spacing_cells``.  Issue #2484: the
                effective spacing is threaded as a kwarg so that
                ``route_coupled`` can widen it for a single call
                (e.g. when start pads sit further apart than the
                configured spacing) without mutating instance state.
            approach_radius_override: Per-call effective approach
                radius (Manhattan distance, in grid cells, from each
                trace to its goal at which the pair-spacing tolerance
                is widened).  When ``None``, falls back to
                ``max(target_spacing_cells, 6)``.  Issue #2490: scaled
                up by ``route_coupled`` when start and end pad pitches
                differ so the search has room to converge from the
                wider start spacing to the narrower goal spacing
                (USB-C vs MCU).
            departure_radius_override: Issue #3508: mirror of
                ``approach_radius_override`` for the START side.
                Within this Manhattan radius of the start pads the
                spacing tolerance is widened the same way, so the
                pair can transition from the physical start pad
                pitch to the configured coupled spacing (the
                mid-route target no longer inherits the start
                pitch).  When ``None``, falls back to
                ``max(target_spacing_cells, 6)``.
            p_visited: Issue #3078: optional set of grid cells the
                positive trace has already occupied along the current
                A* parent chain.  Cells are encoded as
                ``(x, y, layer)`` tuples.  Used as a path-history
                self-intersection guard so the asymmetric
                P-advance/N-advance moves cannot let one trace loop
                around and re-cross either its own trail or the
                partner trace's trail at full spacing -- the failure
                mode behind the 36k ``diffpair_clearance_intra``
                regression on board 06 (Issue #3078).  When ``None``
                or empty, no path-history check fires (legacy
                permissive behaviour).
            n_visited: Issue #3078: companion to ``p_visited`` for the
                negative trace.  Same encoding and semantics.

        Returns list of (new_state, cost, is_via) tuples.
        """
        if target_spacing_cells is None:
            target_spacing_cells = self.target_spacing_cells

        # Issue #3078: path-history check helpers.  Cells are encoded
        # as ``(x, y, layer)`` tuples (matching the parent-chain
        # bookkeeping in ``route_coupled``).  An empty/None set means
        # "no history to check" -- legacy permissive behaviour for
        # callers that did not opt in.
        p_visited_set = p_visited if p_visited else frozenset()
        n_visited_set = n_visited if n_visited else frozenset()

        # Issue #3508: trail PROXIMITY guard.  The exact-cell guard
        # below only rejects landing ON the partner's trail; a landing
        # ONE cell away from it (0.05 mm centerline distance on board
        # 06) is copper overlap that the #3320 gate later rejects
        # wholesale (measured: MIPI_CLK converged but committed copper
        # had P passing 1-2 cells from N's earlier fan-out trail --
        # worst -0.175 mm with 5802 offending segment pairs).  The
        # spacing floor cannot catch this because it constrains the
        # SIMULTANEOUS head positions, not head-vs-historical-trail
        # distance.  Reject any advancing landing within
        # ``min_spacing_cells`` (Euclidean, same layer) of the
        # PARTNER's accumulated trail, using the spatial buckets the
        # caller built during its parent-chain walk.
        prox_r = self.min_spacing_cells
        prox_bucket = max(1, prox_r)
        prox_r_sq = float(prox_r * prox_r)

        def _too_close_to_trail(
            cell: tuple[int, int, int],
            buckets: dict[tuple[int, int], list[tuple[int, int, int]]] | None,
        ) -> bool:
            if not buckets or prox_r <= 1:
                return False
            cx, cy, clayer = cell
            bx, by = cx // prox_bucket, cy // prox_bucket
            for dbx in (-1, 0, 1):
                for dby in (-1, 0, 1):
                    for tx, ty, tlayer in buckets.get((bx + dbx, by + dby), ()):
                        if tlayer != clayer:
                            continue
                        dx = tx - cx
                        dy = ty - cy
                        if float(dx * dx + dy * dy) < prox_r_sq - 1e-9:
                            return True
            return False

        def _self_intersects(
            new_p_pos: GridPos,
            new_n_pos: GridPos,
            p_advances: bool,
            n_advances: bool,
            p_is_endpoint_cell: bool,
            n_is_endpoint_cell: bool,
        ) -> bool:
            """Reject moves that put one trace onto a cell the other
            (or it itself) has already occupied.

            Endpoint cells (start/goal pads) are exempt from the
            cross-trail check because the pad footprint legitimately
            sits on those cells regardless of routing history.  The
            self-loop check still fires at non-endpoint cells.
            """
            if not p_visited_set and not n_visited_set:
                return False
            p_key = (new_p_pos.x, new_p_pos.y, new_p_pos.layer)
            n_key = (new_n_pos.x, new_n_pos.y, new_n_pos.layer)
            # Cross-trail: the advancing trace lands on the partner's
            # accumulated path.  Skip when the landing cell is an
            # endpoint (pad cells are shared geometry, not a routing
            # collision).
            if p_advances and not p_is_endpoint_cell and p_key in n_visited_set:
                return True
            if n_advances and not n_is_endpoint_cell and n_key in p_visited_set:
                return True
            # Issue #3508: proximity variant of the cross-trail check.
            if (
                p_advances
                and not p_is_endpoint_cell
                and _too_close_to_trail(p_key, n_trail_buckets)
            ):
                return True
            if (
                n_advances
                and not n_is_endpoint_cell
                and _too_close_to_trail(n_key, p_trail_buckets)
            ):
                return True
            # Self-loop: the advancing trace re-enters a cell it has
            # already occupied on its own trail.  This is the
            # mechanism behind the 7-vs-1061 segment asymmetry on
            # USB3_RX1 (Issue #3078).
            if p_advances and not p_is_endpoint_cell and p_key in p_visited_set:
                return True
            if n_advances and not n_is_endpoint_cell and n_key in n_visited_set:
                return True
            return False

        neighbors: list[tuple[CoupledState, float, bool]] = []

        # Issue #2473: Relax the spacing constraint when both traces
        # are within an "approach radius" of their goal pads.  This
        # lets a coupled run that started at one pad pitch converge
        # onto a goal pair with a different pad pitch (e.g., USB-C
        # 0.5mm pad spacing reached from MCU 0.8mm pad spacing).  The
        # relaxation only fires near the goals so the bulk of the
        # run still maintains constant spacing.
        approach_relaxed = False
        if p_goal is not None and n_goal is not None:
            p_dist_to_goal = abs(state.p_pos.x - p_goal.x) + abs(state.p_pos.y - p_goal.y)
            n_dist_to_goal = abs(state.n_pos.x - n_goal.x) + abs(state.n_pos.y - n_goal.y)
            # Issue #2490: ``approach_radius`` is sized to admit the
            # *full* spacing transition between the start and goal
            # pad pitches.  When they match (e.g., both at the
            # connector), the legacy default of ``max(target, 6)``
            # is retained.  When they differ (USB device side: MCU
            # 0.8mm pitch vs USB-C 0.5mm pitch), ``route_coupled``
            # passes a wider override so the convergence zone has
            # room to reduce spacing one cell at a time without
            # exceeding the approach tolerance per step.
            if approach_radius_override is not None:
                approach_radius = approach_radius_override
            else:
                approach_radius = max(target_spacing_cells, 6)
            if p_dist_to_goal <= approach_radius and n_dist_to_goal <= approach_radius:
                approach_relaxed = True

        # Issue #3508: departure-phase relaxation -- the mirror of the
        # approach phase, anchored on the START pads.  The mid-route
        # spacing target is now the configured coupled spacing (it no
        # longer inherits the start pad pitch), so a pair leaving
        # wide-pitch connector pads needs the same widened tolerance
        # near the start that the approach phase grants near the goal,
        # or the very first symmetric step away from the pads would be
        # rejected for |pitch - target| > 1.
        departure_relaxed = False
        if p_start is not None and n_start is not None:
            p_dist_from_start = abs(state.p_pos.x - p_start.x) + abs(state.p_pos.y - p_start.y)
            n_dist_from_start = abs(state.n_pos.x - n_start.x) + abs(state.n_pos.y - n_start.y)
            if departure_radius_override is not None:
                departure_radius = departure_radius_override
            else:
                departure_radius = max(target_spacing_cells, 6)
            if p_dist_from_start <= departure_radius and n_dist_from_start <= departure_radius:
                departure_relaxed = True

        spacing_relaxed = approach_relaxed or departure_relaxed

        # Issue #3508: the relaxed tolerance must cover the FULL pitch
        # transition of whichever phase is active.  The legacy
        # ``max(1, target)`` only worked because the target itself had
        # been widened to the start pitch; with the configured coupled
        # target restored, a 20-cell USB-C pitch against a 7-cell
        # target needs tolerance >= 13 inside the endpoint zones.  The
        # phase radii are already sized as ``delta * 2 + 4`` so they
        # dominate the pitch delta by construction.
        relaxed_tolerance = max(1, target_spacing_cells)
        if approach_relaxed:
            relaxed_tolerance = max(relaxed_tolerance, approach_radius)
        if departure_relaxed:
            relaxed_tolerance = max(relaxed_tolerance, departure_radius)

        # Try moving both traces in the same direction
        for dx, dy in self.directions:
            new_p = GridPos(
                state.p_pos.x + dx,
                state.p_pos.y + dy,
                state.p_pos.layer,
            )
            new_n = GridPos(
                state.n_pos.x + dx,
                state.n_pos.y + dy,
                state.n_pos.layer,
            )

            # Check if both new positions are valid.  Issue #2473:
            # Skip the trace-blocked check when stepping into a
            # known goal or start cell — those cells host the pad
            # we are explicitly trying to land on, so the
            # half-width footprint of the partner pad must not
            # disqualify the move.
            p_is_endpoint = self._is_at_goal(new_p, p_goal) or self._is_at_goal(new_p, p_start)
            n_is_endpoint = self._is_at_goal(new_n, n_goal) or self._is_at_goal(new_n, n_start)
            if not p_is_endpoint and self._is_trace_blocked(new_p.x, new_p.y, new_p.layer, p_net):
                self.last_rejections["sym_blocked_p"] += 1
                continue
            if not n_is_endpoint and self._is_trace_blocked(new_n.x, new_n.y, new_n.layer, n_net):
                self.last_rejections["sym_blocked_n"] += 1
                continue

            # Calculate spacing between new positions
            spacing_dx = new_p.x - new_n.x
            spacing_dy = new_p.y - new_n.y
            new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)

            # Only accept moves that maintain target spacing (within tolerance).
            # Issue #2473: When the search is in the "approach" phase
            # near the goal pads (or, issue #3508, the "departure"
            # phase near the start pads), allow wider spacing variation
            # so mismatched pad pitches can converge to / diverge from
            # the coupled target.
            tolerance = relaxed_tolerance if spacing_relaxed else 1
            if abs(new_spacing - target_spacing_cells) > tolerance:
                self.last_rejections["sym_spacing"] += 1
                continue

            # Issue #3012: Hard floor on within-pair spacing.  Independent
            # of the approach-phase tolerance, the search must not place
            # P and N centerlines closer than
            # ``(trace_width + intra_pair_clearance) / grid.resolution``
            # cells apart, or post-route the partner-net edges overlap.
            # The floor is bypassed when BOTH new positions sit on
            # endpoint cells (start or goal pads) -- those cells are
            # owned by the pad footprints whose own spacing is set by
            # the physical board geometry, not the router.
            if self.min_spacing_cells > 0 and not (p_is_endpoint and n_is_endpoint):
                # Use a small epsilon so a Euclidean spacing of exactly
                # min_spacing_cells (axis-aligned) is accepted.
                if new_spacing + 1e-9 < self.min_spacing_cells:
                    self.last_rejections["sym_floor"] += 1
                    continue

            # Issue #3078: path-history self-intersection guard.  Both
            # traces advance in a symmetric move, so both are checked.
            if _self_intersects(
                new_p,
                new_n,
                p_advances=True,
                n_advances=True,
                p_is_endpoint_cell=p_is_endpoint,
                n_is_endpoint_cell=n_is_endpoint,
            ):
                self.last_rejections["sym_trail"] += 1
                continue

            # Calculate cost
            new_direction = (dx, dy)
            cost = self.rules.cost_straight

            # Add turn penalty if direction changed
            if state.direction != (0, 0) and state.direction != new_direction:
                cost += self.rules.cost_turn

            # Issue #4080: corridor attractor for the coupled joint-state
            # loop.  A symmetric move advances BOTH nets, so query each
            # landing cell against its own net (mirrors the single-ended
            # attractor in ``pathfinder.py``).  Clamped at 0 so the joint
            # step cost stays non-negative and A* admissibility holds.
            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                new_p.layer, new_p.x, new_p.y, p_net, self.rules.cost_corridor_attractor
            ) + self.grid.get_corridor_attractor_bonus(
                new_n.layer, new_n.x, new_n.y, n_net, self.rules.cost_corridor_attractor
            )
            if attractor_bonus > 0.0:
                cost = max(0.0, cost - attractor_bonus)

            new_state = CoupledState(new_p, new_n, new_direction)
            neighbors.append((new_state, cost, False))

        # Issue #2490: Asymmetric "converge" moves.  When start and
        # goal pad pitches differ (e.g., USB device-side: MCU 0.8mm
        # pitch -> USB-C 0.5mm pitch), the symmetric step moves above
        # preserve spacing exactly, so the search can never land both
        # traces on endpoint cells whose pitch is narrower than the
        # start pitch.  Allowing one trace to advance while the other
        # holds closes the spacing one cell at a time.
        #
        # Issue #3508: asymmetric moves are now allowed MID-ROUTE too
        # (originally #2490 restricted them to the approach phase).
        # Symmetric moves translate both heads by the same vector, so
        # the P->N offset vector is FROZEN for the whole mid-route --
        # the pair physically cannot turn a corner whose leg runs
        # parallel to that offset: the trailing trace must ride the
        # leading trace's trail and the #3078 path-history guard
        # (correctly) rejects it.  On board 06 this made 9/9 pairs
        # structurally infeasible for the coupled search (MIPI_CLK
        # burned 90k corridor-bounded iterations without converging;
        # the L-shaped FFC->IC route needs a leg parallel to the pad
        # offset).  Concentric corners need the offset vector to
        # ROTATE, which only asymmetric moves can do (the outer trace
        # walks a discrete arc around the holding inner trace).
        #
        # The #2490 restriction predates the guards that make
        # mid-route asymmetry safe: the ``min_spacing_cells`` hard
        # floor (#3012) prevents the spacing collapse, the
        # path-history guard (#3078) prevents the loop-around
        # pathology, and the mid-route tolerance here stays TIGHT
        # (+/-1 cell, same as the symmetric branch) -- the wide
        # ``max(1, target)`` tolerance still applies only inside the
        # approach radius.
        if p_goal is not None and n_goal is not None:
            asym_tolerance = relaxed_tolerance if spacing_relaxed else 1
            for dx, dy in self.directions:
                # P advances, N holds.
                cand_p = GridPos(state.p_pos.x + dx, state.p_pos.y + dy, state.p_pos.layer)
                cand_n = state.n_pos
                p_is_endpoint = self._is_at_goal(cand_p, p_goal) or self._is_at_goal(
                    cand_p, p_start
                )
                if not (
                    p_is_endpoint
                    or not self._is_trace_blocked(cand_p.x, cand_p.y, cand_p.layer, p_net)
                ):
                    self.last_rejections["asym_blocked_p"] += 1
                else:
                    spacing_dx = cand_p.x - cand_n.x
                    spacing_dy = cand_p.y - cand_n.y
                    new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
                    if abs(new_spacing - target_spacing_cells) > asym_tolerance:
                        self.last_rejections["asym_spacing_p"] += 1
                    else:
                        # Issue #3012: enforce the within-pair spacing
                        # floor in the asymmetric P-advance move.  The
                        # asymmetric moves only fire in the approach
                        # phase, where the legacy tolerance was wide
                        # enough to let centerlines coincide; without
                        # this floor we observe -0.150mm overlap on
                        # board 07 diff pairs.  Endpoint cells (P at
                        # its pad AND N at its pad) bypass the floor
                        # since the pads themselves define the spacing.
                        n_is_endpoint = self._is_at_goal(cand_n, n_goal) or self._is_at_goal(
                            cand_n, n_start
                        )
                        bypass_floor = p_is_endpoint and n_is_endpoint
                        if (
                            self.min_spacing_cells > 0
                            and not bypass_floor
                            and new_spacing + 1e-9 < self.min_spacing_cells
                        ):
                            self.last_rejections["asym_floor_p"] += 1
                        elif _self_intersects(
                            cand_p,
                            cand_n,
                            p_advances=True,
                            n_advances=False,
                            p_is_endpoint_cell=p_is_endpoint,
                            n_is_endpoint_cell=n_is_endpoint,
                        ):
                            # Issue #3078: P-advance must not land on
                            # N's accumulated trail or on its own
                            # accumulated trail.  Without this gate the
                            # asymmetric move lets P loop around N and
                            # re-converge from the opposite side,
                            # producing the centerline-coincident
                            # routes that DRC reports as -0.2mm
                            # intra-pair clearance.
                            self.last_rejections["asym_trail_p"] += 1
                        else:
                            # Direction tracking only reflects P's motion;
                            # tag with the new direction so the cost-of-turn
                            # logic still fires when the path bends.
                            cost = self.rules.cost_straight
                            if state.direction != (0, 0) and state.direction != (dx, dy):
                                cost += self.rules.cost_turn
                            # Issue #4080: corridor attractor (per-net) for
                            # the asymmetric P-advance move.  See the
                            # symmetric-move comment above.
                            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                                cand_p.layer,
                                cand_p.x,
                                cand_p.y,
                                p_net,
                                self.rules.cost_corridor_attractor,
                            ) + self.grid.get_corridor_attractor_bonus(
                                cand_n.layer,
                                cand_n.x,
                                cand_n.y,
                                n_net,
                                self.rules.cost_corridor_attractor,
                            )
                            if attractor_bonus > 0.0:
                                cost = max(0.0, cost - attractor_bonus)
                            new_state = CoupledState(cand_p, cand_n, (dx, dy))
                            neighbors.append((new_state, cost, False))

                # N advances, P holds.
                cand_p2 = state.p_pos
                cand_n2 = GridPos(state.n_pos.x + dx, state.n_pos.y + dy, state.n_pos.layer)
                n_is_endpoint = self._is_at_goal(cand_n2, n_goal) or self._is_at_goal(
                    cand_n2, n_start
                )
                if not n_is_endpoint and self._is_trace_blocked(
                    cand_n2.x, cand_n2.y, cand_n2.layer, n_net
                ):
                    self.last_rejections["asym_blocked_n"] += 1
                    continue
                spacing_dx = cand_p2.x - cand_n2.x
                spacing_dy = cand_p2.y - cand_n2.y
                new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
                if abs(new_spacing - target_spacing_cells) > asym_tolerance:
                    self.last_rejections["asym_spacing_n"] += 1
                    continue
                # Issue #3012: same within-pair spacing floor as the
                # P-advance branch.  P holds at its current position
                # here; the floor is bypassed only when P happens to
                # already be at its pad AND the candidate N is at its
                # pad.
                p_is_endpoint_held = self._is_at_goal(cand_p2, p_goal) or self._is_at_goal(
                    cand_p2, p_start
                )
                bypass_floor = p_is_endpoint_held and n_is_endpoint
                if (
                    self.min_spacing_cells > 0
                    and not bypass_floor
                    and new_spacing + 1e-9 < self.min_spacing_cells
                ):
                    self.last_rejections["asym_floor_n"] += 1
                    continue
                # Issue #3078: N-advance must not land on P's
                # accumulated trail or on its own accumulated trail.
                # See the P-advance branch above for the failure mode
                # this prevents (board 06 USB3_TX1+ 1063-segment
                # loop-around).
                if _self_intersects(
                    cand_p2,
                    cand_n2,
                    p_advances=False,
                    n_advances=True,
                    p_is_endpoint_cell=p_is_endpoint_held,
                    n_is_endpoint_cell=n_is_endpoint,
                ):
                    self.last_rejections["asym_trail_n"] += 1
                    continue
                cost = self.rules.cost_straight
                if state.direction != (0, 0) and state.direction != (dx, dy):
                    cost += self.rules.cost_turn
                # Issue #4080: corridor attractor (per-net) for the
                # asymmetric N-advance move.  See the symmetric-move
                # comment above.
                attractor_bonus = self.grid.get_corridor_attractor_bonus(
                    cand_p2.layer,
                    cand_p2.x,
                    cand_p2.y,
                    p_net,
                    self.rules.cost_corridor_attractor,
                ) + self.grid.get_corridor_attractor_bonus(
                    cand_n2.layer,
                    cand_n2.x,
                    cand_n2.y,
                    n_net,
                    self.rules.cost_corridor_attractor,
                )
                if attractor_bonus > 0.0:
                    cost = max(0.0, cost - attractor_bonus)
                new_state = CoupledState(cand_p2, cand_n2, (dx, dy))
                neighbors.append((new_state, cost, False))

        # Issue #2490: Endpoint via exception.  When the current state
        # sits exactly on a start or goal pad, the pad's footprint is
        # already part of the board geometry — the same cells that
        # ``_is_via_blocked`` would inspect are occupied by the pad
        # whose net we are trying to drop a via for.  Without this
        # exception, ``_is_via_blocked`` rejects via placement at the
        # source pad of the coupled run on dense pad fields (e.g.,
        # USB-C 0.5mm pitch), trapping the search on layer 0 even when
        # an inner/back layer is wide open.  We mirror the existing
        # trace-blocked exception at endpoints (lines 311-316).
        p_at_endpoint = self._is_at_goal(state.p_pos, p_goal) or self._is_at_goal(
            state.p_pos, p_start
        )
        n_at_endpoint = self._is_at_goal(state.n_pos, n_goal) or self._is_at_goal(
            state.n_pos, n_start
        )

        # Try layer change (via) - both traces must change layer together
        routable_layers = self.grid.get_routable_indices()
        for new_layer in routable_layers:
            if new_layer == state.p_pos.layer:
                continue

            # Check if vias can be placed at both positions.  Skip the
            # via-blocked check at endpoint pads — see comment above.
            if not p_at_endpoint and self._is_via_blocked(state.p_pos.x, state.p_pos.y, p_net):
                continue
            if not n_at_endpoint and self._is_via_blocked(state.n_pos.x, state.n_pos.y, n_net):
                continue

            new_p = GridPos(state.p_pos.x, state.p_pos.y, new_layer)
            new_n = GridPos(state.n_pos.x, state.n_pos.y, new_layer)

            # Check if new layer positions are valid
            if not p_at_endpoint and self._is_trace_blocked(new_p.x, new_p.y, new_p.layer, p_net):
                continue
            if not n_at_endpoint and self._is_trace_blocked(new_n.x, new_n.y, new_n.layer, n_net):
                continue

            # Via cost for both traces
            cost = self.rules.cost_via * 2

            # Issue #4080: corridor attractor on the via-drop destination
            # cells -- the reservation is what makes the coupled router
            # prefer to actually via-hop INTO the reserved channel
            # (mirrors the single-ended via-drop attractor in
            # ``pathfinder.py``).  Per-net query, clamped at 0.
            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                new_p.layer, new_p.x, new_p.y, p_net, self.rules.cost_corridor_attractor
            ) + self.grid.get_corridor_attractor_bonus(
                new_n.layer, new_n.x, new_n.y, n_net, self.rules.cost_corridor_attractor
            )
            if attractor_bonus > 0.0:
                cost = max(0.0, cost - attractor_bonus)

            new_state = CoupledState(new_p, new_n, state.direction)
            neighbors.append((new_state, cost, True))

        # Issue #2473: Swap-via move for polarity-swap crossover.  Both
        # traces drop a via at their current location, then re-emerge on
        # an inner layer with their grid positions exchanged.  This
        # supports USB-C-shaped pad layouts where the connector inverts
        # the differential polarity (D+/D- swap rows between A and B).
        if self.allow_swap_via:
            for new_layer in routable_layers:
                if new_layer == state.p_pos.layer:
                    continue

                # Both pads must be able to host a via at their current
                # position on every layer (the via spans through-hole).
                # Issue #2490: Endpoint pads are exempt — the pad
                # footprint already occupies the cells the via would
                # span.
                if not p_at_endpoint and self._is_via_blocked(state.p_pos.x, state.p_pos.y, p_net):
                    continue
                if not n_at_endpoint and self._is_via_blocked(state.n_pos.x, state.n_pos.y, n_net):
                    continue

                # After the swap, the P-trace continues from where N was,
                # and vice versa, on the new layer.
                swapped_p = GridPos(state.n_pos.x, state.n_pos.y, new_layer)
                swapped_n = GridPos(state.p_pos.x, state.p_pos.y, new_layer)

                if self._is_trace_blocked(swapped_p.x, swapped_p.y, swapped_p.layer, p_net):
                    continue
                if self._is_trace_blocked(swapped_n.x, swapped_n.y, swapped_n.layer, n_net):
                    continue

                # Higher cost than a normal via to discourage gratuitous
                # swaps when a straight path would suffice.
                cost = self.rules.cost_via * 3

                # Reset direction after the swap — the orientation has
                # inverted, so any prior straight-line streak is broken.
                new_state = CoupledState(swapped_p, swapped_n, (0, 0))
                neighbors.append((new_state, cost, True))

        return neighbors

    def _heuristic(
        self,
        state: CoupledState,
        p_goal: GridPos,
        n_goal: GridPos,
    ) -> float:
        """Calculate heuristic for coupled A* search.

        Two modes (selected at construction via ``heuristic_mode``):

        * ``"manhattan_sum"`` (legacy): returns
          ``(p_dist + n_dist) * cost_straight + layer_cost``.  This
          biases the priority queue against partner-synchronised
          moves: a symmetric step reduces the sum by 2 cost units,
          while an asymmetric P-only-or-N-only step reduces it by 1,
          even though both cost the same.  Net effect: A* preferentially
          extends asymmetric escape stubs that produce the
          ``diffpair_clearance_intra`` violations on board 06.
        * ``"partner_aware"`` (Issue #3115, angle #5, default):
          returns ``max(p_dist, n_dist) * cost_straight +
          spacing_penalty + layer_cost``.

          Admissibility argument (informal): every real path that
          reaches the goal must advance the *slower* of P/N by at
          least ``max(p_dist, n_dist)`` cells, so the ``max`` term
          never exceeds the true remaining path cost.  The
          ``spacing_penalty`` term costs at most
          ``abs(current_spacing - target_spacing_cells) *
          cost_straight * spacing_penalty_factor`` and the true cost
          of correcting that divergence requires at least
          ``abs(current_spacing - target_spacing_cells) *
          cost_straight`` (one asymmetric converge move per cell of
          divergence), so any ``spacing_penalty_factor <= 1.0`` keeps
          the heuristic admissible.  The ``layer_cost`` term is the
          same admissible per-trace via-cost the legacy heuristic
          uses.
        """
        # Manhattan distance for both traces
        p_dist = abs(state.p_pos.x - p_goal.x) + abs(state.p_pos.y - p_goal.y)
        n_dist = abs(state.n_pos.x - n_goal.x) + abs(state.n_pos.y - n_goal.y)

        # Layer change cost if needed
        layer_cost = 0.0
        if state.p_pos.layer != p_goal.layer:
            layer_cost += self.rules.cost_via
        if state.n_pos.layer != n_goal.layer:
            layer_cost += self.rules.cost_via

        if self.heuristic_mode == "manhattan_sum":
            return (p_dist + n_dist) * self.rules.cost_straight + layer_cost

        # heuristic_mode == "partner_aware"
        max_dist = max(p_dist, n_dist)
        # Spacing penalty -- ranks states whose current center-to-
        # center spacing diverges from the target lower in the heap.
        # We compute the Euclidean spacing rather than Manhattan
        # because the coupled-move tolerance check uses Euclidean
        # (see _get_coupled_neighbors at line ~604).
        spacing_dx = state.p_pos.x - state.n_pos.x
        spacing_dy = state.p_pos.y - state.n_pos.y
        current_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
        spacing_divergence = abs(current_spacing - self.target_spacing_cells)
        spacing_penalty = (
            spacing_divergence * self.rules.cost_straight * self.spacing_penalty_factor
        )
        return max_dist * self.rules.cost_straight + spacing_penalty + layer_cost

    def _cpp_coupled_available(self) -> bool:
        """Whether the C++ coupled search may handle THIS pathfinder.

        v1 scope (Issue #4065): the C++ port covers the ``partner_aware``
        heuristic with ``allow_swap_via`` off.  The legacy ``manhattan_sum``
        heuristic and the USB-C polarity-swap ``allow_swap_via`` move are
        deferred and stay on the pure-Python search.
        """
        if not self._use_cpp_coupled:
            return False
        if self.allow_swap_via:
            return False
        if self.heuristic_mode != "partner_aware":
            return False
        from .cpp_backend import is_cpp_available

        return is_cpp_available()

    def _get_cpp_coupled_impl(self) -> CppCoupledPathfinder | None:
        """Build (once) and return the cached C++ coupled pathfinder.

        Mirrors the ``CppPathfinder`` lifecycle: the ``CppGrid`` is
        marshalled from ``self.grid`` once and the C++ pathfinder is
        constructed once, then reused across ``route_coupled`` calls.
        Returns ``None`` when the backend is unavailable or construction
        raises (the caller then falls back to pure Python).
        """
        if self._cpp_coupled_impl is not None and self._cpp_coupled_grid is self.grid:
            return self._cpp_coupled_impl
        try:
            from .cpp_backend import CppCoupledPathfinder, CppGrid

            # Issue #4065 (reach-regression root cause): ``from_routing_grid``
            # unconditionally reassigns ``grid._cpp_grid = <new CppGrid>``
            # (cpp_backend.py, #2481 back-reference).  That back-reference is
            # the ONE the single-ended ``CppPathfinder`` relies on for rip-up
            # invalidation: ``RoutingGrid.unmark_route`` calls
            # ``self._cpp_grid.invalidate_stored_routes()`` so the C++
            # ``stored_vias_`` / ``stored_segments_`` snapshot no longer
            # references a ripped-up route.  The single-ended router marks its
            # routes on its OWN grid (``RoutingCore.router._grid``), a
            # DIFFERENT object.  When the coupled pre-phase builds its private
            # CppGrid here it HIJACKS ``grid._cpp_grid`` to point at the
            # coupled snapshot, so every subsequent negotiated-loop rip-up
            # invalidates the wrong grid and the single-ended router keeps
            # consulting stale via/segment blockers -- which is exactly why the
            # board-06 negotiated loop re-routed only 2/4 (vs 3/4 on the Python
            # baseline) at iter 2 and dropped USB3_RX1- (20/21 instead of
            # 21/21).  The coupled pathfinder needs its own CppGrid but must
            # NOT steal the single-ended router's paired back-reference, so
            # snapshot ``grid._cpp_grid`` and restore it afterwards.
            saved_cpp_grid = getattr(self.grid, "_cpp_grid", None)
            # Restore the single-ended router's back-reference (or ``None`` if
            # it had none) in a ``finally`` so it is restored even when
            # ``from_routing_grid`` raises AFTER hijacking ``grid._cpp_grid``
            # mid-copy (e.g. during the bulk cell copy or the Issue #4071
            # corridor-reservation marshalling): the coupled pathfinder keeps
            # ``cpp_grid`` in ``impl`` below, but the Python grid's
            # ``_cpp_grid`` invalidation hook must continue to target the
            # single-ended router's grid.  The outer ``try/except Exception``
            # still routes any raised exception to the Python fallback.
            try:
                cpp_grid = CppGrid.from_routing_grid(self.grid)
            finally:
                self.grid._cpp_grid = saved_cpp_grid
            impl = CppCoupledPathfinder(
                cpp_grid,
                self.rules,
                target_spacing_cells=self.target_spacing_cells,
                min_spacing_cells=self.min_spacing_cells,
                trace_half_width_cells=self._trace_half_width_cells,
                via_extra_cells=self._via_extra_cells,
                via_drill_cells=max(
                    0, int(math.ceil((self.rules.via_drill / 2) / self.grid.resolution))
                ),
                spacing_penalty_factor=self.spacing_penalty_factor,
                heuristic_weight=self.heuristic_weight,
            )
        except Exception:
            logger.debug("C++ coupled pathfinder construction failed; using Python", exc_info=True)
            self._use_cpp_coupled = False
            return None
        self._cpp_coupled_impl = impl
        self._cpp_coupled_grid = self.grid
        return impl

    def _try_cpp_route_coupled(
        self,
        *,
        p_start_pos: GridPos,
        n_start_pos: GridPos,
        p_goal_pos: GridPos,
        n_goal_pos: GridPos,
        start_layer: int,
        end_layer: int,
        p_net: int,
        n_net: int,
        effective_target_spacing: int,
        effective_approach_radius: int,
        effective_departure_radius: int,
        corridor: frozenset[tuple[int, int]] | None,
        timeout_seconds: float | None,
        max_iterations_budget: int | None,
    ) -> tuple[bool, tuple[Route, Route] | None] | None:
        """Attempt the coupled search via the C++ backend (Issue #4065).

        Returns ``None`` when the C++ path does not apply (backend absent /
        deferred feature / construction failed) -- the caller then runs the
        pure-Python A*.  Otherwise returns ``(handled=True, result)`` where
        ``result`` is the reconstructed ``(p_route, n_route)`` tuple or
        ``None`` (search failed / budget exit); the C++ diagnostics are
        written to ``self.last_*`` so budget-exit handling is unchanged.
        """
        if not self._cpp_coupled_available():
            return None
        impl = self._get_cpp_coupled_impl()
        if impl is None:
            return None

        # Marshal the corridor frozenset -> flat cols*rows bitset for O(1)
        # C++ membership (diffpair_routing.py:446 build_corridor_mask churn
        # -> a byte array here).  Empty list = no corridor.
        corridor_bitset: list[int] = []
        if corridor is not None:
            cols, rows = self.grid.cols, self.grid.rows
            bitset = bytearray(cols * rows)
            for cx, cy in corridor:
                if 0 <= cx < cols and 0 <= cy < rows:
                    bitset[cy * cols + cx] = 1
            corridor_bitset = list(bitset)

        routable_layers = list(self.grid.get_routable_indices())

        path, diagnostics = impl.route(
            p_start_xy=(p_start_pos.x, p_start_pos.y),
            n_start_xy=(n_start_pos.x, n_start_pos.y),
            start_layer=start_layer,
            p_goal_xy=(p_goal_pos.x, p_goal_pos.y),
            n_goal_xy=(n_goal_pos.x, n_goal_pos.y),
            end_layer=end_layer,
            p_net=p_net,
            n_net=n_net,
            effective_target_spacing=effective_target_spacing,
            effective_approach_radius=effective_approach_radius,
            effective_departure_radius=effective_departure_radius,
            routable_layers=routable_layers,
            corridor_bitset=corridor_bitset,
            max_iterations_budget=(
                max_iterations_budget
                if max_iterations_budget is not None and max_iterations_budget > 0
                else 0
            ),
            timeout_seconds=(
                float(timeout_seconds)
                if timeout_seconds is not None and timeout_seconds > 0
                else 0.0
            ),
        )

        # Mirror the diagnostic bookkeeping the Python loop maintains.
        self.last_iterations = int(diagnostics["iterations"])
        bp = diagnostics["best_progress"]
        self.last_best_progress = float("inf") if bp < 0 else float(bp)
        self.last_best_state = None
        self.last_best_node = None
        self.last_timeout_exceeded = bool(diagnostics["timeout_exceeded"])
        self.last_iteration_limited = bool(diagnostics["iteration_limited"])
        self.last_rejections = collections.defaultdict(int)

        if path is None:
            return True, None

        return True, self._reconstruct_coupled_routes_from_cpp_path(path)

    def _reconstruct_coupled_routes_from_cpp_path(
        self,
        path: list[tuple[int, int, int, int, int, int, bool]],
    ) -> tuple[Route, Route]:
        """Build (p_route, n_route) from a C++ joint grid-cell path.

        Produces the exact same ``p_path`` / ``n_path`` world-coordinate
        lists that ``_reconstruct_coupled_routes`` builds from the Python
        parent chain, then feeds them to the UNCHANGED
        ``_build_route_from_path`` -- so C++ and Python routes are
        byte-identical for the same joint path (Issue #4065).  The Pad
        identity for width/net/name is recovered from the endpoint cells
        via the stored ``_cpp_reconstruct_pads`` set by the caller.
        """
        p_start, p_end, n_start, n_end = self._cpp_reconstruct_pads
        p_route = Route(net=p_start.net, net_name=p_start.net_name)
        n_route = Route(net=n_start.net, net_name=n_start.net_name)

        p_path: list[tuple[float, float, int, bool]] = []
        n_path: list[tuple[float, float, int, bool]] = []
        for p_x, p_y, p_layer, n_x, n_y, n_layer, via_from_parent in path:
            p_wx, p_wy = self.grid.grid_to_world(p_x, p_y)
            n_wx, n_wy = self.grid.grid_to_world(n_x, n_y)
            p_path.append((p_wx, p_wy, p_layer, via_from_parent))
            n_path.append((n_wx, n_wy, n_layer, via_from_parent))

        self._build_route_from_path(p_route, p_path, p_start, p_end)
        self._build_route_from_path(n_route, n_path, n_start, n_end)
        return p_route, n_route

    def route_coupled(
        self,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
        timeout_seconds: float | None = None,
        max_iterations_budget: int | None = None,
        corridor: frozenset[tuple[int, int]] | None = None,
    ) -> tuple[Route, Route] | None:
        """Route a differential pair with coupled pathfinding.

        Args:
            p_start: Positive trace start pad
            p_end: Positive trace end pad
            n_start: Negative trace start pad
            n_end: Negative trace end pad
            timeout_seconds: Issue #3089: Optional wall-clock budget (in
                seconds) for the A* search.  When set, the
                ``while open_set`` loop checks ``time.monotonic()`` every
                ``_TIMEOUT_CHECK_INTERVAL`` iterations and returns
                ``None`` once the elapsed time exceeds the budget.  This
                lets callers (``route_differential_pair_coupled`` /
                ``route_all_with_diffpairs``) bound the per-pair cost
                without changing the algorithm.  The caller is expected
                to handle the ``None`` result the same way it handles
                an exhausted-search ``None`` (fall back to independent
                routing or log a skipped-budget diagnostic).
                ``None`` (default) preserves the legacy unbounded
                behaviour.
            max_iterations_budget: Issue #3144: Optional **iteration**
                budget.  When set, the search aborts (returns ``None``,
                sets ``last_timeout_exceeded=True``) once
                ``iterations >= max_iterations_budget``.  Unlike
                ``timeout_seconds``, the iteration budget is
                independent of CPU speed -- the same pair always exits
                the same way on a 2-core CI runner as on an 8-core
                development machine.  This eliminates the
                timing-dependent budget-classification non-determinism
                described in #3144 (different pairs land in coupled-vs-
                deferred buckets on different runs because the
                wall-clock deadline lands at different points in the
                search depending on runner load).  Whichever of
                ``timeout_seconds`` and ``max_iterations_budget`` fires
                first triggers the exit.  ``None`` (default) preserves
                wall-clock-only behaviour.  Note this is distinct from
                the unconditional ``max_iterations`` floor at line
                ``self.grid.cols * self.grid.rows * 4`` which is the
                memory backstop; ``max_iterations_budget`` is the
                user-tunable classifier and is expected to fire much
                earlier than the backstop.
            corridor: Issue #3439: Optional layer-agnostic corridor
                mask (set of ``(x, y)`` grid cells, typically built by
                :func:`build_corridor_mask` from a single-ended guide
                route).  When set, every generated neighbor state must
                place BOTH the P and N head positions inside the
                corridor; states outside are pruned before they reach
                the open set.  This converts the open joint-state
                search (quadratic in the single-net state space and
                intractable in pure Python on large boards) into a
                corridor-bounded near-1D search that completes in
                seconds.  Start/goal endpoint cells are exempt so an
                under-dilated corridor can never disqualify the only
                landing cells.  ``None`` (default) preserves the
                unconstrained legacy search.

        Returns:
            Tuple of (p_route, n_route) or None if routing failed (no
            path found, ``max_iterations`` exhausted,
            ``max_iterations_budget`` exceeded, or ``timeout_seconds``
            wall-clock budget exceeded).
        """
        # Issue #3089: reset the timeout-exit flag.  Callers consult
        # this immediately after ``route_coupled`` returns ``None`` to
        # decide whether to attempt an independent-routing fallback.
        self.last_timeout_exceeded = False
        # Issue #3921: reset the iteration-vs-wall-clock discriminator.
        self.last_iteration_limited = False
        # Issue #3473: reset the iteration counter for this call.
        self.last_iterations = 0
        # Issue #3508: best progress-toward-goal (joint Manhattan
        # remaining distance, max over the two heads) any popped state
        # achieved, plus the state that achieved it.  Consumed by the
        # caller's budget-exit diagnostics to distinguish "almost
        # converged, budget-starved" from "structurally stuck".
        self.last_best_progress: float = float("inf")
        self.last_best_state: CoupledState | None = None
        self.last_best_node: CoupledNode | None = None
        # Issue #3508: reset the per-search rejection counters.
        self.last_rejections = collections.defaultdict(int)

        # Issue #4065: stash the pads for the C++-path reconstruction helper
        # (it recovers width/net/name from the same Pad objects the Python
        # reconstruction uses, so routes are byte-identical).
        self._cpp_reconstruct_pads = (p_start, p_end, n_start, n_end)

        # Convert to grid coordinates
        p_start_gx, p_start_gy = self.grid.world_to_grid(p_start.x, p_start.y)
        p_end_gx, p_end_gy = self.grid.world_to_grid(p_end.x, p_end.y)
        n_start_gx, n_start_gy = self.grid.world_to_grid(n_start.x, n_start.y)
        n_end_gx, n_end_gy = self.grid.world_to_grid(n_end.x, n_end.y)

        # Determine start layer
        start_layer = self.grid.layer_to_index(p_start.layer.value)
        end_layer = self.grid.layer_to_index(p_end.layer.value)

        # Create start and goal states
        p_start_pos = GridPos(p_start_gx, p_start_gy, start_layer)
        n_start_pos = GridPos(n_start_gx, n_start_gy, start_layer)
        p_goal_pos = GridPos(p_end_gx, p_end_gy, end_layer)
        n_goal_pos = GridPos(n_end_gx, n_end_gy, end_layer)

        # Issue #2473: Derive the actual target spacing from the start
        # pad pair on the grid.  Real-world differential pairs (USB-C,
        # USB device-side connectors) often have pad spacing that
        # exceeds the manufacturer-minimum spacing configured on the
        # rules.  Using the configured spacing as a hard target prevents
        # the search from leaving the start state.  We honor the larger
        # of the configured spacing and the actual start-pad distance,
        # which keeps clearance valid while letting the coupled run
        # follow the natural pad pitch.
        #
        # Issue #2484: Keep this widened value as a per-call local
        # rather than mutating ``self.target_spacing_cells``.  The
        # previous implementation permanently widened the instance
        # attribute on the first wide-pad call and leaked the new
        # spacing into every subsequent ``route_coupled`` invocation
        # on the same pathfinder.
        actual_start_spacing = math.sqrt(
            (p_start_gx - n_start_gx) ** 2 + (p_start_gy - n_start_gy) ** 2
        )
        actual_end_spacing = math.sqrt((p_end_gx - n_end_gx) ** 2 + (p_end_gy - n_end_gy) ** 2)

        # Issue #3508: the mid-route spacing target is the CONFIGURED
        # coupled spacing, NOT the start pad pitch.  The legacy code
        # (#2473) widened ``effective_target_spacing`` to the start-pad
        # distance, which forced the pair to fly the ENTIRE route at
        # connector pitch (0.75-1.0 mm on board 06's FFC / USB-C
        # sources -- not electrically coupled at all) and then made the
        # endgame infeasible: a 16-20-cell-wide pair cannot thread the
        # dense pad field around the destination IC, and the
        # ``_heuristic`` spacing penalty (which uses the configured
        # ``self.target_spacing_cells``) actively fought the move
        # filter the whole way.  Instead, keep the configured target
        # and let the DEPARTURE phase below absorb the start-pitch
        # mismatch, exactly mirroring how the approach phase absorbs
        # the goal-pitch mismatch.
        effective_target_spacing = self.target_spacing_cells

        # Issue #2490: Size the approach radius to accommodate the
        # full pitch transition between the coupled target and the
        # goal pads.  The legacy ``max(target, 6)`` radius can be
        # smaller than the number of single-cell spacing reductions
        # required to converge, leaving the search no room to relax
        # spacing without exceeding the per-step tolerance.  Scale the
        # radius with the absolute spacing difference plus a small
        # buffer so each cell of the approach can change spacing by
        # at most one cell.
        end_spacing_delta = int(round(abs(actual_end_spacing - effective_target_spacing)))
        effective_approach_radius = max(effective_target_spacing, 6, end_spacing_delta * 2 + 4)

        # Issue #3508: departure radius -- the mirror of the approach
        # radius, sized by the start-pitch transition.  Within this
        # radius of the start pads the spacing tolerance is widened so
        # the pair can converge from the physical pad pitch down to
        # the coupled target one cell per step.
        start_spacing_delta = int(round(abs(actual_start_spacing - effective_target_spacing)))
        effective_departure_radius = max(effective_target_spacing, 6, start_spacing_delta * 2 + 4)

        # Issue #4065: try the C++ coupled joint-state A* first.  The C++
        # search consumes the SAME Grid3D the single-ended C++ pathfinder
        # uses and returns a joint grid-cell path we reconstruct below with
        # the UNCHANGED ``_build_route_from_path`` -- so C++ and Python
        # produce byte-identical Routes for the same joint path.  Preserved
        # as an optional accelerator: the pure-Python A* below is the
        # fallback, exercised when the backend is absent/stale, when the
        # v1-deferred features are requested (``allow_swap_via`` /
        # ``manhattan_sum``), or when ``_use_cpp_coupled`` is disabled.
        cpp_path = self._try_cpp_route_coupled(
            p_start_pos=p_start_pos,
            n_start_pos=n_start_pos,
            p_goal_pos=p_goal_pos,
            n_goal_pos=n_goal_pos,
            start_layer=start_layer,
            end_layer=end_layer,
            p_net=p_start.net,
            n_net=n_start.net,
            effective_target_spacing=effective_target_spacing,
            effective_approach_radius=effective_approach_radius,
            effective_departure_radius=effective_departure_radius,
            corridor=corridor,
            timeout_seconds=timeout_seconds,
            max_iterations_budget=max_iterations_budget,
        )
        if cpp_path is not None:
            handled, cpp_result = cpp_path
            if handled:
                # C++ owns this search (backend available + no deferred
                # feature).  ``cpp_result`` is the reconstructed
                # (p_route, n_route) tuple or None (search failed / budget
                # exit); either way we do NOT run the Python A* -- the
                # diagnostics on ``self`` were set by the wrapper.
                return cpp_result

        start_state = CoupledState(p_start_pos, n_start_pos, (0, 0))

        # Issue #3439: endpoint cells are exempt from the corridor
        # check -- the corridor builder includes them by construction,
        # but an under-dilated mask must never disqualify the search's
        # only landing cells.
        corridor_exempt: frozenset[tuple[int, int]] = frozenset(
            (
                (p_start_pos.x, p_start_pos.y),
                (p_goal_pos.x, p_goal_pos.y),
                (n_start_pos.x, n_start_pos.y),
                (n_goal_pos.x, n_goal_pos.y),
            )
        )

        # A* setup
        open_set: list[CoupledNode] = []
        closed_set: set[tuple[GridPos, GridPos]] = set()
        g_scores: dict[tuple[GridPos, GridPos], float] = {}

        # Issue #3144: monotonic insertion counter for deterministic
        # tie-breaking when ``f_score`` is equal between heap entries.
        # See ``CoupledNode`` docstring for the full rationale.  Using
        # ``itertools.count()`` keeps the hot path branch-free: every
        # ``CoupledNode`` constructor reads ``next(seq_counter)`` once.
        seq_counter = itertools.count()

        start_h = self.heuristic_weight * self._heuristic(start_state, p_goal_pos, n_goal_pos)
        # Issue #3508: LIFO tie-break (note the NEGATED counter).  The
        # #3144 fix introduced the monotonic counter for determinism
        # with FIFO semantics (oldest equal-f node pops first).  FIFO
        # explores f-score plateaus breadth-first: on a corridor-
        # bounded coupled search the plateau is the whole tube
        # cross-section x direction-history product, so the frontier
        # saturates laterally and the search burns its entire
        # iteration budget mid-tube (board 06: every pair, including
        # 90k-iteration corridor runs, exhausted budgets without
        # converging).  Popping the NEWEST equal-f node instead dives
        # depth-first along the most recently extended path -- on a
        # plateau this beelines toward the goal and only falls back
        # to sibling states when the dive hits an obstacle.  Equally
        # deterministic (the counter is still search-local and
        # monotonic); A* optimality is unaffected (tie-break order
        # among equal-f nodes never changes the returned path cost
        # with an admissible heuristic).
        start_node = CoupledNode(start_h, 0.0, start_state, seq=-next(seq_counter))
        heapq.heappush(open_set, start_node)
        g_scores[(p_start_pos, n_start_pos)] = 0.0

        max_iterations = self.grid.cols * self.grid.rows * 4
        iterations = 0

        # Issue #3089: wall-clock budget bookkeeping.  When
        # ``timeout_seconds`` is None, ``deadline`` stays None and the
        # branch below is skipped for every iteration.
        deadline: float | None = None
        if timeout_seconds is not None and timeout_seconds > 0:
            deadline = time.monotonic() + float(timeout_seconds)

        # Issue #3144: optional iteration budget for deterministic
        # budget classification.  ``None`` preserves wall-clock-only
        # behaviour; a positive int aborts the search the same way
        # the wall-clock branch does once ``iterations`` reaches it.
        iter_budget: int | None = (
            max_iterations_budget
            if max_iterations_budget is not None and max_iterations_budget > 0
            else None
        )

        while open_set and iterations < max_iterations:
            iterations += 1
            # Issue #3473: keep the public counter current on every
            # iteration so EVERY exit path (budget, timeout, goal,
            # exhausted open set, exceptions) reports the true cost.
            # A single attribute store is noise next to the heap and
            # parent-chain work in this loop body.
            self.last_iterations = iterations

            # Issue #3144: iteration budget classifier check.  Sits
            # adjacent to the wall-clock check so a single ``if`` body
            # owns the "abandon search and let caller dispatch
            # fallback" exit path.  Checked on every iteration (no
            # gating) because the check is a single integer compare.
            if iter_budget is not None and iterations >= iter_budget:
                logger.warning(
                    "CoupledPathfinder.route_coupled iteration budget "
                    "exceeded after %d iterations; abandoning "
                    "search (p_net=%r n_net=%r)",
                    iterations,
                    p_start.net_name,
                    n_start.net_name,
                )
                self.last_timeout_exceeded = True
                # Issue #3921: mark the ITERATION budget as the binding
                # constraint so the caller's diagnostic reports iteration
                # counts, not a misleading wall-clock-seconds figure.
                self.last_iteration_limited = True
                return None

            # Issue #3089: periodic wall-clock check.  Exits with ``None``
            # so the caller can fall through to its existing "coupled
            # routing failed" handler (which logs a structured message
            # and either tries independent routing or marks the pair as
            # skipped, depending on ``coupled_only``).
            if (
                deadline is not None
                and (iterations & (self._TIMEOUT_CHECK_INTERVAL - 1)) == 0
                and time.monotonic() >= deadline
            ):
                logger.warning(
                    "CoupledPathfinder.route_coupled wall-clock budget "
                    "exceeded after %.2fs (%d iterations); abandoning "
                    "search (p_net=%r n_net=%r)",
                    float(timeout_seconds),
                    iterations,
                    p_start.net_name,
                    n_start.net_name,
                )
                self.last_timeout_exceeded = True
                return None

            current = heapq.heappop(open_set)
            current_key = (current.state.p_pos, current.state.n_pos)

            if current_key in closed_set:
                continue
            closed_set.add(current_key)

            if _COUPLED_TRACE and iterations % 1000 == 0:
                print(
                    f"      [trace] it={iterations} f={current.f_score:.1f} "
                    f"g={current.g_score:.1f} open={len(open_set)} "
                    f"closed={len(closed_set)} p=({current.state.p_pos.x},"
                    f"{current.state.p_pos.y},{current.state.p_pos.layer}) "
                    f"n=({current.state.n_pos.x},{current.state.n_pos.y},"
                    f"{current.state.n_pos.layer})"
                )

            # Goal check - both traces must reach their goals
            p_at_goal = (
                current.state.p_pos.x == p_goal_pos.x and current.state.p_pos.y == p_goal_pos.y
            )
            n_at_goal = (
                current.state.n_pos.x == n_goal_pos.x and current.state.n_pos.y == n_goal_pos.y
            )

            if p_at_goal and n_at_goal:
                return self._reconstruct_coupled_routes(current, p_start, p_end, n_start, n_end)

            # Issue #3508: progress tracking for budget-exit diagnostics.
            # ``last_best_progress`` is the smallest joint remaining
            # distance any popped state achieved; a budget exit at high
            # remaining distance means the search is structurally stuck
            # (pinch point / frozen-offset corner), while a near-zero
            # value means it almost converged and a budget bump would
            # likely land it.
            _progress = max(
                abs(current.state.p_pos.x - p_goal_pos.x)
                + abs(current.state.p_pos.y - p_goal_pos.y),
                abs(current.state.n_pos.x - n_goal_pos.x)
                + abs(current.state.n_pos.y - n_goal_pos.y),
            )
            if _progress < self.last_best_progress:
                self.last_best_progress = _progress
                self.last_best_state = current.state
                self.last_best_node = current

            # Issue #3078: build path-history sets for the current
            # node by walking its parent chain.  These let
            # ``_get_coupled_neighbors`` reject moves that would put
            # one trace onto a cell the other (or it itself) has
            # already occupied -- the failure mode behind the
            # 36k-violation board 06 regression where asymmetric
            # moves let one trace loop around its partner.
            p_visited_cells: set[tuple[int, int, int]] = set()
            n_visited_cells: set[tuple[int, int, int]] = set()
            # Issue #3508: spatial buckets over the SAME trail cells for
            # the proximity guard (see ``_too_close_to_trail``).  Bucket
            # size = the proximity radius so any cell within the radius
            # of a candidate lives in one of the 3x3 neighbouring
            # buckets.
            prox_radius = self.min_spacing_cells
            p_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
            n_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
            bucket = max(1, prox_radius)
            walker: CoupledNode | None = current
            while walker is not None:
                p_cell = (walker.state.p_pos.x, walker.state.p_pos.y, walker.state.p_pos.layer)
                n_cell = (walker.state.n_pos.x, walker.state.n_pos.y, walker.state.n_pos.layer)
                p_visited_cells.add(p_cell)
                n_visited_cells.add(n_cell)
                if prox_radius > 1:
                    p_trail_buckets.setdefault(
                        (p_cell[0] // bucket, p_cell[1] // bucket), []
                    ).append(p_cell)
                    n_trail_buckets.setdefault(
                        (n_cell[0] // bucket, n_cell[1] // bucket), []
                    ).append(n_cell)
                walker = walker.parent
            # Endpoint pads are legitimate landing cells regardless of
            # history -- strip them so the check doesn't disqualify a
            # via at the source pad or a same-cell re-entry into the
            # goal pad.  (The neighbor-check helper also has an
            # endpoint exemption, but pre-filtering keeps the set
            # smaller and the intent more explicit.)
            for ep in (
                (p_start_pos.x, p_start_pos.y, p_start_pos.layer),
                (p_goal_pos.x, p_goal_pos.y, p_goal_pos.layer),
            ):
                p_visited_cells.discard(ep)
            for ep in (
                (n_start_pos.x, n_start_pos.y, n_start_pos.layer),
                (n_goal_pos.x, n_goal_pos.y, n_goal_pos.layer),
            ):
                n_visited_cells.discard(ep)
            p_visited_frozen = frozenset(p_visited_cells)
            n_visited_frozen = frozenset(n_visited_cells)

            # Explore neighbors
            for new_state, cost, is_via in self._get_coupled_neighbors(
                current.state,
                p_start.net,
                n_start.net,
                p_goal_pos,
                n_goal_pos,
                p_start_pos,
                n_start_pos,
                target_spacing_cells=effective_target_spacing,
                approach_radius_override=effective_approach_radius,
                departure_radius_override=effective_departure_radius,
                p_visited=p_visited_frozen,
                n_visited=n_visited_frozen,
                p_trail_buckets=p_trail_buckets,
                n_trail_buckets=n_trail_buckets,
            ):
                # Issue #3439: corridor-bounded search.  Prune any
                # state whose P or N head leaves the corridor mask
                # (endpoint cells exempt).  Layer is intentionally
                # ignored -- the corridor constrains the spatial tube,
                # not the layer choice.
                if corridor is not None:
                    p_xy = (new_state.p_pos.x, new_state.p_pos.y)
                    n_xy = (new_state.n_pos.x, new_state.n_pos.y)
                    if (p_xy not in corridor and p_xy not in corridor_exempt) or (
                        n_xy not in corridor and n_xy not in corridor_exempt
                    ):
                        self.last_rejections["corridor"] += 1
                        continue

                neighbor_key = (new_state.p_pos, new_state.n_pos)
                if neighbor_key in closed_set:
                    continue

                new_g = current.g_score + cost

                if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                    g_scores[neighbor_key] = new_g
                    h = self._heuristic(new_state, p_goal_pos, n_goal_pos)
                    # Issue #3508: weighted A* (see ``heuristic_weight``).
                    f = new_g + self.heuristic_weight * h

                    # Issue #3508: negated counter = LIFO tie-break on
                    # equal f -- see the start-node comment.
                    neighbor_node = CoupledNode(
                        f, new_g, new_state, current, is_via, seq=-next(seq_counter)
                    )
                    heapq.heappush(open_set, neighbor_node)

        # No path found
        return None

    def _reconstruct_coupled_routes(
        self,
        end_node: CoupledNode,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
    ) -> tuple[Route, Route]:
        """Reconstruct both routes from A* result."""
        p_route = Route(net=p_start.net, net_name=p_start.net_name)
        n_route = Route(net=n_start.net, net_name=n_start.net_name)

        # Collect path points
        p_path: list[tuple[float, float, int, bool]] = []
        n_path: list[tuple[float, float, int, bool]] = []

        node: CoupledNode | None = end_node
        while node:
            p_wx, p_wy = self.grid.grid_to_world(node.state.p_pos.x, node.state.p_pos.y)
            n_wx, n_wy = self.grid.grid_to_world(node.state.n_pos.x, node.state.n_pos.y)

            p_path.append((p_wx, p_wy, node.state.p_pos.layer, node.via_from_parent))
            n_path.append((n_wx, n_wy, node.state.n_pos.layer, node.via_from_parent))

            node = node.parent

        p_path.reverse()
        n_path.reverse()

        # Convert to segments and vias for P trace
        self._build_route_from_path(p_route, p_path, p_start, p_end)

        # Convert to segments and vias for N trace
        self._build_route_from_path(n_route, n_path, n_start, n_end)

        # Issue #3078: order-of-magnitude segment-count asymmetry
        # invariant.  When the A* asymmetric P/N-advance moves let one
        # trace loop around the other (the failure mode behind the 36k
        # ``diffpair_clearance_intra`` regression on board 06), the
        # reconstructed routes show segment counts that differ by
        # 100x or more (USB3_RX1: 7 vs 1061 in the bug report).  The
        # path-history guard added in this issue is supposed to make
        # that impossible -- this log line is a runtime canary that
        # surfaces a regression in the guard itself.  We log at WARN
        # (not raise) so a defect in the guard during production
        # routing does NOT crash the whole pipeline; the post-route
        # Phase A audit will still detect the resulting clearance
        # violations.
        p_seg_count = len(p_route.segments)
        n_seg_count = len(n_route.segments)
        if p_seg_count > 0 and n_seg_count > 0:
            ratio = max(p_seg_count, n_seg_count) / min(p_seg_count, n_seg_count)
            if ratio > 10.0:
                logger.warning(
                    "coupled-route segment-count asymmetry "
                    "(possible self-intersection bug): "
                    "p_net=%r segs=%d, n_net=%r segs=%d, ratio=%.1fx",
                    p_start.net_name,
                    p_seg_count,
                    n_start.net_name,
                    n_seg_count,
                    ratio,
                )

        return p_route, n_route

    def _get_trace_width_for_net(self, net_name: str) -> float:
        """Get the trace width for a net based on its net class.

        Args:
            net_name: Name of the net

        Returns:
            Trace width in mm
        """
        if self.net_class_map and net_name in self.net_class_map:
            return self.net_class_map[net_name].trace_width
        return self.rules.trace_width

    def _build_route_from_path(
        self,
        route: Route,
        path: list[tuple[float, float, int, bool]],
        start_pad: Pad,
        end_pad: Pad,
    ) -> None:
        """Build route segments and vias from path points."""
        if len(path) < 2:
            return

        # Issue #1543: Use net-class-aware trace width
        trace_width = self._get_trace_width_for_net(start_pad.net_name)
        current_x, current_y = start_pad.x, start_pad.y
        current_layer_idx = self.grid.layer_to_index(start_pad.layer.value)

        for wx, wy, layer_idx, is_via in path:
            if is_via:
                # Add via
                via = Via(
                    x=current_x,
                    y=current_y,
                    drill=self.rules.via_drill,
                    diameter=self.rules.via_diameter,
                    layers=(
                        Layer(self.grid.index_to_layer(current_layer_idx)),
                        Layer(self.grid.index_to_layer(layer_idx)),
                    ),
                    net=start_pad.net,
                    net_name=start_pad.net_name,
                )
                route.vias.append(via)
                current_layer_idx = layer_idx
            else:
                # Add segment if we've moved
                if abs(wx - current_x) > 0.01 or abs(wy - current_y) > 0.01:
                    seg = Segment(
                        x1=current_x,
                        y1=current_y,
                        x2=wx,
                        y2=wy,
                        width=trace_width,
                        layer=Layer(self.grid.index_to_layer(layer_idx)),
                        net=start_pad.net,
                        net_name=start_pad.net_name,
                    )
                    route.segments.append(seg)
                    current_x, current_y = wx, wy
                    current_layer_idx = layer_idx

        # Final segment to end pad
        if abs(end_pad.x - current_x) > 0.01 or abs(end_pad.y - current_y) > 0.01:
            seg = Segment(
                x1=current_x,
                y1=current_y,
                x2=end_pad.x,
                y2=end_pad.y,
                width=trace_width,
                layer=Layer(self.grid.index_to_layer(current_layer_idx)),
                net=start_pad.net,
                net_name=start_pad.net_name,
            )
            route.segments.append(seg)


def create_serpentine(
    route: Route,
    length_to_add: float,
    min_amplitude: float = 0.3,
    min_segment_length: float = 1.0,
    partner_route: Route | None = None,
    intra_pair_clearance_mm: float | None = None,
    grid: RoutingGrid | None = None,
) -> bool:
    """Add serpentine meander to a route to increase its length.

    Finds a suitable straight segment and replaces it with a serpentine
    pattern to add the required length.

    When ``partner_route`` and ``intra_pair_clearance_mm`` are both
    provided, the serpentine bulges AWAY from the partner trace (using
    the same ``_outer_normal_hint`` logic as the audited Phase 3I
    tuner) and is rejected via a DRC self-check (mirrors
    ``_post_insertion_clearance_ok``) before being committed to the
    route.  This prevents the inline shim from introducing
    ``diffpair_clearance_intra`` violations on tightly-spaced pairs
    (Issue #3003).

    Args:
        route: The route to modify
        length_to_add: Additional length needed in mm
        min_amplitude: Minimum serpentine amplitude in mm
        min_segment_length: Minimum segment length for serpentine in mm
        partner_route: Optional partner trace; when provided alongside
            ``intra_pair_clearance_mm`` the bulge direction is chosen
            away from the partner and a clearance check is run before
            committing.
        intra_pair_clearance_mm: Optional edge-to-edge clearance floor
            in mm.  Required for the clearance-aware path; ignored when
            ``partner_route`` is ``None``.
        grid: Issue #3508: optional routing grid.  When provided, every
            proposed serpentine segment is rasterised against the grid's
            clearance envelope and the serpentine is REJECTED if any
            covered cell is blocked for a foreign net.  The partner
            self-check below only protects against the partner's
            SEGMENTS; the grid check additionally protects against the
            partner's vias, other nets' committed copper, and pad
            clearance halos -- the measured failure mode on board 06 was
            one-sided serpentine combs landing on the partner's shadow
            vias (8 ``clearance_segment_via`` at -0.038 mm) and grazing
            neighbour pads (Issue #3508 first re-route attempt).

    Returns:
        True if serpentine was added, False if no suitable segment
        found OR the proposed bulge would violate the partner clearance.
    """
    if length_to_add <= 0:
        return False

    # Find the longest straight horizontal or vertical segment
    best_segment = None
    best_segment_idx = -1
    best_length = 0.0

    for i, seg in enumerate(route.segments):
        seg_dx = seg.x2 - seg.x1
        seg_dy = seg.y2 - seg.y1
        seg_length = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)

        # Only consider segments long enough for serpentine
        if seg_length < min_segment_length:
            continue

        # Prefer horizontal or vertical segments
        is_horizontal = abs(seg_dy) < 0.01
        is_vertical = abs(seg_dx) < 0.01

        if (is_horizontal or is_vertical) and seg_length > best_length:
            best_length = seg_length
            best_segment = seg
            best_segment_idx = i

    if best_segment is None:
        return False

    # Calculate serpentine parameters.
    #
    # Issue #3508: the bulges this function emits are TRIANGULAR (two
    # diagonal segments out to the bulge apex and back), so the length
    # a bend ADDS over the straight step it replaces is
    #
    #     added_per_bend = 2 * hypot(step/2, amplitude) - step
    #
    # The legacy ``amplitude = length_to_add / (2 * num_bends)``
    # assumed SQUARE bulges (added = 2 * amplitude per bend) and
    # under-delivered by up to an order of magnitude on long steps
    # (e.g. step 4 mm, amplitude 0.5 mm adds 0.124 mm/bend, not
    # 1.0 mm/bend) -- the shim then "succeeded" while leaving the pair
    # skewed.  Invert the triangular formula instead, and scale the
    # bend count with the available segment so amplitudes stay small.
    seg_len_initial = math.hypot(
        best_segment.x2 - best_segment.x1, best_segment.y2 - best_segment.y1
    )
    num_bends = max(2, min(12, int(seg_len_initial / 1.0)))
    step_est = seg_len_initial / (num_bends + 1)
    added_per_bend = length_to_add / num_bends
    amplitude = max(
        min_amplitude,
        math.sqrt(max(0.0, ((added_per_bend + step_est) / 2.0) ** 2 - (step_est / 2.0) ** 2)),
    )

    # Determine serpentine direction (perpendicular to segment)
    seg_dx = best_segment.x2 - best_segment.x1
    seg_dy = best_segment.y2 - best_segment.y1
    seg_length = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)

    # Normalize direction
    dir_x = seg_dx / seg_length
    dir_y = seg_dy / seg_length

    # Perpendicular direction for serpentine waves
    perp_x = -dir_y
    perp_y = dir_x

    # Issue #3003: when a partner trace is available, bias ``current_side``
    # so the bulge points AWAY from the partner.  The default of +1 (the
    # hardcoded pre-#3003 value) bulges blindly toward whichever side the
    # perpendicular happens to face, which on a tight diff pair lands the
    # serpentine right on top of the partner.  We reuse the same outer-
    # normal heuristic as the audited Phase 3I tuner
    # (``_outer_normal_hint`` in ``diffpair_length_tuning``): dot the
    # perpendicular against the unit vector from the partner's closest
    # point to the insertion segment's midpoint.  A positive dot means
    # +1 already points outward; a negative dot means we must start at
    # -1 to bulge outward.
    initial_side = 1
    if partner_route is not None and partner_route.segments:
        from .diffpair_length_tuning import _outer_normal_hint

        hint_x, hint_y = _outer_normal_hint(best_segment, partner_route)
        # Dot the (segment-frame) perpendicular against the outer normal.
        # Use the side whose perpendicular projection is non-negative.
        if perp_x * hint_x + perp_y * hint_y < 0.0:
            initial_side = -1

    # Create serpentine segments
    new_segments: list[Segment] = []
    step_length = seg_length / (num_bends + 1)

    current_x = best_segment.x1
    current_y = best_segment.y1
    current_side = initial_side  # Alternates between +1 and -1

    for bend in range(num_bends + 1):
        # Move to next point along the segment direction
        next_x = best_segment.x1 + dir_x * step_length * (bend + 1)
        next_y = best_segment.y1 + dir_y * step_length * (bend + 1)

        if bend < num_bends:
            # Add serpentine bulge
            bulge_x = current_x + dir_x * step_length / 2 + perp_x * amplitude * current_side
            bulge_y = current_y + dir_y * step_length / 2 + perp_y * amplitude * current_side

            # Segment to bulge
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=bulge_x,
                    y2=bulge_y,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

            # Segment from bulge to next point
            new_segments.append(
                Segment(
                    x1=bulge_x,
                    y1=bulge_y,
                    x2=next_x,
                    y2=next_y,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

            # Issue #3508: keep ALL bulges on the outward side when a
            # partner constraint is in play.  The legacy alternating
            # serpentine sends every other bulge TOWARD the partner;
            # for a coupled pair at the intentionally-tight intra gap
            # (0.075-0.1 mm edge-to-edge) any inward bulge violates the
            # clearance self-check below, so the shim always failed on
            # exactly the pairs that need it most (board 06 shadow
            # pairs, 2.5-4.5 mm trim/tail skew).  A one-sided comb adds
            # the same length per bend without ever approaching the
            # partner.
            if partner_route is None or intra_pair_clearance_mm is None:
                current_side *= -1  # Flip side for next bend
        else:
            # Final segment to end point
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=best_segment.x2,
                    y2=best_segment.y2,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

        current_x = next_x
        current_y = next_y

    # Issue #3003: DRC self-check before committing.  If the caller
    # supplied a partner route and an intra_pair_clearance threshold,
    # verify the new bulges do not violate that threshold against the
    # partner.  On rejection, leave the route untouched and report
    # failure -- the caller (match_pair_lengths -> route_differential_
    # pair_coupled) will fall through to the length-warning path, which
    # is a valid output (matches the no-suitable-segment branch).
    if partner_route is not None and intra_pair_clearance_mm is not None:
        from kicad_tools.core.geometry import segment_clearance

        for new_seg in new_segments:
            for pseg in partner_route.segments:
                if pseg.layer != new_seg.layer:
                    continue
                clearance = segment_clearance(
                    new_seg.x1,
                    new_seg.y1,
                    new_seg.x2,
                    new_seg.y2,
                    new_seg.width,
                    pseg.x1,
                    pseg.y1,
                    pseg.x2,
                    pseg.y2,
                    pseg.width,
                )
                if clearance + 1e-9 < intra_pair_clearance_mm:
                    return False

    # Issue #3508: grid self-check.  Rasterise every proposed segment
    # against the grid's clearance envelope (the same convention as
    # ``CoupledPathfinder._is_trace_blocked``: the grid already encodes
    # the full centerline clearance envelope at marking time, so a cell
    # is legal exactly when a trace centerline there satisfies
    # clearance).  Own-net cells are passable; anything else (partner
    # copper INCLUDING vias, other nets, pad halos, keepouts) rejects
    # the serpentine -- leaving the pair with a length-mismatch warning
    # is strictly better than committing clearance violations.
    if grid is not None:
        for new_seg in new_segments:
            li = grid.layer_to_index(new_seg.layer.value)
            sgx1, sgy1 = grid.world_to_grid(new_seg.x1, new_seg.y1)
            sgx2, sgy2 = grid.world_to_grid(new_seg.x2, new_seg.y2)
            steps = max(abs(sgx2 - sgx1), abs(sgy2 - sgy1))
            for i in range(steps + 1):
                t = i / steps if steps else 0.0
                gx = int(round(sgx1 + (sgx2 - sgx1) * t))
                gy = int(round(sgy1 + (sgy2 - sgy1) * t))
                if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                    return False
                cell = grid.grid[li][gy][gx]
                if cell.blocked and cell.net != route.net:
                    return False

    # Replace the original segment with serpentine segments
    route.segments = (
        route.segments[:best_segment_idx] + new_segments + route.segments[best_segment_idx + 1 :]
    )

    return True


def match_pair_lengths(
    p_route: Route,
    n_route: Route,
    max_delta: float,
    add_serpentines: bool = True,
    intra_pair_clearance_mm: float | None = None,
    grid: RoutingGrid | None = None,
) -> bool:
    """Match lengths of differential pair traces.

    Adds serpentine meander to the shorter trace to match lengths.

    Issue #3003: when ``intra_pair_clearance_mm`` is provided, the
    serpentine generator runs the clearance-aware path
    (bulge-away-from-partner + DRC self-check).  When omitted, the
    legacy unconditional bulge path is preserved for backward
    compatibility with callers that have no notion of intra-pair
    clearance.

    Args:
        p_route: Positive trace route
        n_route: Negative trace route
        max_delta: Maximum allowed length difference in mm
        add_serpentines: Whether to add serpentines (if False, just check)
        intra_pair_clearance_mm: Optional intra-pair clearance floor in
            mm; when provided the serpentine is bulged away from the
            partner and DRC-checked against it before commit.

    Returns:
        True if lengths are matched (within tolerance), False otherwise
        (either lengths still mismatched OR the proposed serpentine
        would violate the partner clearance and was rejected).
    """
    p_length = calculate_route_length([p_route])
    n_length = calculate_route_length([n_route])
    delta = abs(p_length - n_length)

    if delta <= max_delta:
        return True  # Already matched

    if not add_serpentines:
        return False  # Cannot match without serpentines

    # Add serpentine to shorter trace
    length_to_add = delta - max_delta * 0.5  # Leave some margin

    if p_length < n_length:
        return create_serpentine(
            p_route,
            length_to_add,
            partner_route=n_route if intra_pair_clearance_mm is not None else None,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
            grid=grid,
        )
    else:
        return create_serpentine(
            n_route,
            length_to_add,
            partner_route=p_route if intra_pair_clearance_mm is not None else None,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
            grid=grid,
        )


class DiffPairRouter:
    """Differential pair routing coordinator for the autorouter.

    Supports two routing modes:
    1. Coupled routing: Both traces routed simultaneously maintaining spacing
    2. Independent routing: Traces routed separately (fallback)
    """

    def __init__(self, autorouter: Autorouter):
        """Initialize differential pair router.

        Args:
            autorouter: Parent autorouter instance
        """
        self.autorouter = autorouter
        # Issue #3023 Phase A: rolling buffer of routed intra-pair
        # clearance violations detected during
        # ``route_differential_pair_coupled``.  Phase A is
        # observability-only; Phase B (separate PR) will consume this
        # list to drive a fine-grid repair sub-pass.
        self._intra_clearance_violations: list[IntraPairClearanceViolation] = []
        # Issue #3089: True iff the most-recent call to
        # ``route_differential_pair_coupled`` returned because the
        # inner ``CoupledPathfinder.route_coupled`` exceeded its
        # wall-clock budget (``per_pair_timeout``).  Used by
        # ``route_all_with_diffpairs`` to distinguish a budget-exit
        # (where the pair's nets should be deferred to the main
        # strategy) from a genuine no-path-found exit (where the
        # caller's existing handling is unchanged).
        self._last_pair_budget_exit: bool = False
        # Issue #3508: opt-in gate for the geometric shadow
        # constructor (see
        # ``DifferentialPairConfig.enable_shadow_construction`` for
        # the full rationale and the board 06 run-4 integration
        # measurements that keep this defaulted OFF).  Set from the
        # config by ``route_all_with_diffpairs``; tests may set it
        # directly.
        self.enable_shadow_construction: bool = False

    def _collect_existing_drills(self) -> list[tuple[float, float, float]]:
        """Assemble a board-wide drill registry for the hole-to-hole guard.

        Issue #3855: returns ``(x, y, drill_diameter)`` for every drilled
        hole the diff-pair fan-out via must keep clear of:

        * every through-hole pad (any net) -- ``pad.through_hole`` with a
          positive ``pad.drill``;
        * every via already committed to ``self.autorouter.routes`` (any
          net), including fan-out vias placed by earlier crossovers.

        The list is consulted edge-to-edge by
        :func:`kicad_tools.router.via_clearance.drill_hole_to_hole_clear`.
        Cheap to assemble (the fan-out path already iterates pads/routes
        for other reasons) and rebuilt per crossover so vias placed by
        prior crossovers are visible.
        """
        drills: list[tuple[float, float, float]] = []
        for pad in self.autorouter.pads.values():
            if getattr(pad, "through_hole", False) and pad.drill > 0:
                drills.append((pad.x, pad.y, pad.drill))
        for route in self.autorouter.routes:
            for via in route.vias:
                if via.drill > 0:
                    drills.append((via.x, via.y, via.drill))
        return drills

    def _resolve_detection_inputs(
        self,
    ) -> tuple[dict | None, dict[str, str] | None, list | None]:
        """Pull layered-detection context off the autorouter.

        Returns the ``(net_class_routing, net_to_class, kicad_groups)``
        triple needed by :func:`_layered_detect_diff_pairs`.  Supports
        both attribute conventions:

        * ``net_class_routing`` + ``net_to_class`` -- preferred (set by
          callers that have built a class-name-keyed map).
        * ``net_class_map`` -- the autorouter's per-net-name map; when
          present, we synthesise a ``net_to_class`` map from it so the
          explicit declaration path can be consulted.

        Issue #2638 / Epic #2556 Phase 2E: the explicit-declaration
        path in ``_gather_explicit_pairs`` was previously dead for
        callers that only set ``autorouter.net_class_map`` (the common
        case).  Phase 2E plumbs the fallback through so the
        engagement-layer single-ended refusal -- which depends on
        explicit pairs being detected -- can fire.
        """
        net_class_routing = getattr(self.autorouter, "net_class_routing", None)
        net_to_class = getattr(self.autorouter, "net_to_class", None)
        kicad_groups = getattr(self.autorouter, "kicad_diff_pair_groups", None)

        if net_class_routing is None:
            net_class_map = getattr(self.autorouter, "net_class_map", None)
            if net_class_map:
                net_class_routing = net_class_map
                if net_to_class is None:
                    # Synthesise a net_name -> class_name map.  We use
                    # the NetClassRouting.name attribute so the
                    # class-name-keyed lookup in _gather_explicit_pairs
                    # can find each entry under a stable key.  Because
                    # multiple net names may map to the same
                    # NetClassRouting instance, we also register each
                    # NetClassRouting under its own .name so
                    # _gather_explicit_pairs' subsequent lookup
                    # ``net_class_routing.get(class_name)`` succeeds.
                    synth_routing: dict = dict(net_class_map)
                    synth_to_class: dict[str, str] = {}
                    for net_name, nc in net_class_map.items():
                        cls_name = nc.name
                        synth_to_class[net_name] = cls_name
                        synth_routing.setdefault(cls_name, nc)
                    net_class_routing = synth_routing
                    net_to_class = synth_to_class

        return net_class_routing, net_to_class, kicad_groups

    def detect_differential_pairs(self) -> list[DifferentialPair]:
        """Detect differential pairs from net names.

        Issue #2558, Epic #2556 Phase 1B: this delegates to the layered
        detector (``diffpair_detection.detect_diff_pairs``) which
        consults explicit ``NetClassRouting.diffpair_partner`` and
        KiCad-group declarations in priority order before falling back
        to suffix inference.

        Issue #2638, Phase 2E: the layered-detection inputs are now
        pulled from either explicit ``net_class_routing`` /
        ``net_to_class`` attributes OR the autorouter's
        ``net_class_map`` (see :meth:`_resolve_detection_inputs`), so
        explicit declarations are honoured for both attribute shapes.
        """
        net_class_routing, net_to_class, kicad_groups = self._resolve_detection_inputs()

        detected = _layered_detect_diff_pairs(
            self.autorouter.net_names,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
            kicad_groups=kicad_groups,
        )
        return [d.pair for d in detected]

    def detect_differential_pairs_with_source(self) -> list[tuple[DifferentialPair, str]]:
        """Like :meth:`detect_differential_pairs`, but also report the
        detection source for each pair.

        Returns a list of ``(pair, source)`` tuples where ``source`` is
        one of ``"explicit"``, ``"kicad_group"``, ``"suffix"``.
        """
        net_class_routing, net_to_class, kicad_groups = self._resolve_detection_inputs()

        detected = _layered_detect_diff_pairs(
            self.autorouter.net_names,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
            kicad_groups=kicad_groups,
        )
        return [(d.pair, d.source.value) for d in detected]

    def analyze_differential_pairs(self) -> dict[str, any]:
        """Analyze net names for differential pairs."""
        return analyze_differential_pairs(self.autorouter.net_names)

    def _resolve_engagement(self, pair: DifferentialPair) -> tuple[bool, str]:
        """Resolve whether ``pair`` should engage CoupledPathfinder.

        Issue #2638, Epic #2556 Phase 2E: thin wrapper that pulls
        net-class context off the autorouter via
        :meth:`_resolve_detection_inputs` and defers to
        :func:`should_engage_coupled`.

        Returns:
            ``(engaged, reason)`` from :func:`should_engage_coupled`.
        """
        # Issue #3508: refuse coupled routing when either net already
        # carries committed copper (e.g. the recipe pre-routed a
        # chronically-stranded single before the pre-phase).  Coupled
        # routing would lay a SECOND copy of the pre-routed side; the
        # main strategy is the right tool for whatever remains.
        p_id, n_id = pair.get_net_ids()
        existing_nets = {r.net for r in self.autorouter.routes}
        if p_id in existing_nets or n_id in existing_nets:
            return False, "pre-routed copper present on a pair net"
        net_class_routing, net_to_class, _ = self._resolve_detection_inputs()
        return should_engage_coupled(pair, net_class_routing, net_to_class)

    def _get_pair_pads(self, pair: DifferentialPair) -> tuple[list[Pad], list[Pad]] | None:
        """Get pads for P and N nets of a differential pair.

        Returns:
            Tuple of (p_pads, n_pads) or None if pads not found
        """
        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        if p_net_id not in self.autorouter.nets:
            return None
        if n_net_id not in self.autorouter.nets:
            return None

        p_pad_keys = self.autorouter.nets[p_net_id]
        n_pad_keys = self.autorouter.nets[n_net_id]

        if len(p_pad_keys) < 2 or len(n_pad_keys) < 2:
            return None

        p_pads = [self.autorouter.pads[k] for k in p_pad_keys]
        n_pads = [self.autorouter.pads[k] for k in n_pad_keys]

        return p_pads, n_pads

    def _pair_pads_for_coupled_routing(
        self, p_pads: list[Pad], n_pads: list[Pad]
    ) -> list[tuple[Pad, Pad, Pad, Pad]]:
        """Pair up P and N pads for coupled routing.

        Matches P/N pads that are closest together as start/end pairs.

        Issue #2473: For pairs with more than 2 pads per net, this is now
        a thin wrapper around :meth:`_pair_pads_for_coupled_routing_npad`,
        which returns ``CoupledSegmentSpec`` and ``StubEdgeSpec`` objects.
        Callers that only need 2-pad behavior continue to receive a list
        of plain 4-tuples for backward compatibility.

        Returns:
            List of (p_start, p_end, n_start, n_end) tuples for the
            coupled segments only.  Stub edges (intra-net hops) are
            available via :meth:`_pair_pads_for_coupled_routing_npad`.
        """
        if len(p_pads) < 2 or len(n_pads) < 2:
            # Need at least one pad on each side to form a pair.
            return []

        if len(p_pads) == 2 and len(n_pads) == 2:
            # Fast path for the common 2-pad case — preserves the
            # exact pre-#2473 ordering for the regression test fixture.
            p0, p1 = p_pads[0], p_pads[1]
            n0, n1 = n_pads[0], n_pads[1]

            d_p0_n0 = math.sqrt((p0.x - n0.x) ** 2 + (p0.y - n0.y) ** 2)
            d_p0_n1 = math.sqrt((p0.x - n1.x) ** 2 + (p0.y - n1.y) ** 2)

            if d_p0_n0 < d_p0_n1:
                return [(p0, p1, n0, n1)]
            else:
                return [(p0, p1, n1, n0)]

        # N-pad path: build coupled segments via MST-style pairing.
        coupled, _stubs = self._pair_pads_for_coupled_routing_npad(p_pads, n_pads)
        return [(c.p_start, c.p_end, c.n_start, c.n_end) for c in coupled]

    @staticmethod
    def _pad_distance(a: Pad, b: Pad) -> float:
        """Euclidean distance between two pads (ignoring layer)."""
        return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)

    def _cluster_pads(self, pads: list[Pad], threshold: float) -> list[list[Pad]]:
        """Group pads into connected clusters by Euclidean proximity.

        Two pads are placed in the same cluster when their pad-center
        distance is below ``threshold`` (mm).  Used to identify "groups"
        of pads that share a side of the diff pair (e.g., the four
        USB-C pads on the connector are all within ~1 mm of each other,
        whereas the MCU pin is several mm away).
        """
        if not pads:
            return []

        # Union-find by index.
        parent = list(range(len(pads)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        for i in range(len(pads)):
            for j in range(i + 1, len(pads)):
                if self._pad_distance(pads[i], pads[j]) <= threshold:
                    union(i, j)

        groups: dict[int, list[Pad]] = {}
        for i, pad in enumerate(pads):
            r = find(i)
            groups.setdefault(r, []).append(pad)
        return list(groups.values())

    @staticmethod
    def _cluster_centroid(pads: list[Pad]) -> tuple[float, float]:
        """Return the centroid (mean position) of a pad cluster."""
        if not pads:
            return (0.0, 0.0)
        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)
        return (cx, cy)

    @staticmethod
    def _polarity_swap_between(p_start: Pad, n_start: Pad, p_end: Pad, n_end: Pad) -> bool:
        """Detect whether the orientation of the start pair is mirrored at the end.

        The differential pair ``(p, n)`` defines an oriented vector
        from N to P at each endpoint.  When the two oriented vectors
        point in opposite directions (dot product < 0), the pair must
        execute a coordinated layer-swap to maintain coupling.
        """
        sx = p_start.x - n_start.x
        sy = p_start.y - n_start.y
        ex = p_end.x - n_end.x
        ey = p_end.y - n_end.y
        dot = sx * ex + sy * ey
        return dot < 0.0

    def _pair_pads_for_coupled_routing_npad(
        self, p_pads: list[Pad], n_pads: list[Pad]
    ) -> tuple[list[CoupledSegmentSpec], list[StubEdgeSpec]]:
        """MST-based pad pairing for N-pad differential pairs.

        Issue #2473: When a differential-pair net has more than two
        pads (e.g., USB-C connectors paralleling top/bottom-side pads),
        this method:

        1. Clusters pads on each net by spatial proximity.  Pads
           within the same cluster are connected by short stub edges.
        2. Selects one "representative" pad per cluster (closest to
           the centroid of the corresponding cluster on the other net).
        3. Computes a minimum spanning tree over the representative
           pads' centroids to produce coupled segments connecting the
           clusters.

        Stub edges within a cluster are returned separately and
        routed independently after the coupled pass.

        Returns:
            Tuple of ``(coupled_segments, stub_edges)`` where each
            ``CoupledSegmentSpec`` is a side-to-side coupled run and
            each ``StubEdgeSpec`` is a single-net intra-cluster hop.
        """
        if len(p_pads) < 2 or len(n_pads) < 2:
            return [], []

        # Cluster threshold: pads within this distance share a "side".
        # USB-C A6 (y=105) and B6 (y=106) are 1 mm apart; the MCU pin is
        # 10+ mm away.  A 3 mm threshold cleanly separates them.
        cluster_threshold = 3.0

        p_clusters = self._cluster_pads(p_pads, cluster_threshold)
        n_clusters = self._cluster_pads(n_pads, cluster_threshold)

        # Each side must form the same number of clusters; otherwise
        # we cannot reliably pair them up and fall back to "treat
        # every pad as its own cluster" (still produces an MST).
        if len(p_clusters) != len(n_clusters):
            p_clusters = [[p] for p in p_pads]
            n_clusters = [[n] for n in n_pads]

        # Need at least two clusters per side to form a coupled run.
        if len(p_clusters) < 2 or len(n_clusters) < 2:
            return [], []

        # Compute centroids for matching clusters across nets.
        p_centroids = [self._cluster_centroid(c) for c in p_clusters]
        n_centroids = [self._cluster_centroid(c) for c in n_clusters]

        # Greedy match P-cluster -> nearest N-cluster centroid.  For
        # the test fixtures this is optimal (clusters are well
        # separated) and avoids the O(n!) cost of optimal assignment.
        n_assigned = [False] * len(n_clusters)
        cluster_pairs: list[tuple[list[Pad], list[Pad]]] = []
        for pi, (px, py) in enumerate(p_centroids):
            best_ni = -1
            best_dist = float("inf")
            for ni, (nx, ny) in enumerate(n_centroids):
                if n_assigned[ni]:
                    continue
                dist = math.sqrt((px - nx) ** 2 + (py - ny) ** 2)
                if dist < best_dist:
                    best_dist = dist
                    best_ni = ni
            if best_ni >= 0:
                n_assigned[best_ni] = True
                cluster_pairs.append((p_clusters[pi], n_clusters[best_ni]))

        # Within each matched cluster pair, pick the "representative"
        # P pad and N pad (closest pair across the two clusters) as the
        # endpoint of the coupled run.  Other pads in the cluster
        # become stub edges back to the representative.
        rep_pads: list[tuple[Pad, Pad]] = []  # (p_rep, n_rep)
        stub_edges: list[StubEdgeSpec] = []

        for p_cluster, n_cluster in cluster_pairs:
            best_pair: tuple[Pad, Pad] | None = None
            best_dist = float("inf")
            for p in p_cluster:
                for n in n_cluster:
                    d = self._pad_distance(p, n)
                    if d < best_dist:
                        best_dist = d
                        best_pair = (p, n)
            if best_pair is None:  # pragma: no cover — defensive
                continue
            p_rep, n_rep = best_pair
            rep_pads.append((p_rep, n_rep))

            for p in p_cluster:
                if p is not p_rep:
                    stub_edges.append(StubEdgeSpec(start=p_rep, end=p))
            for n in n_cluster:
                if n is not n_rep:
                    stub_edges.append(StubEdgeSpec(start=n_rep, end=n))

        if len(rep_pads) < 2:
            return [], stub_edges

        # MST over representative pad pairs.  Edge weight = sum of
        # P-trace and N-trace lengths for the coupled segment.  This
        # is the metric the test plan asks us to minimize ("greedy
        # nearest-neighbor pairing would lose").
        n_reps = len(rep_pads)
        edges: list[tuple[float, int, int]] = []
        for i in range(n_reps):
            for j in range(i + 1, n_reps):
                p_i, n_i = rep_pads[i]
                p_j, n_j = rep_pads[j]
                weight = self._pad_distance(p_i, p_j) + self._pad_distance(n_i, n_j)
                edges.append((weight, i, j))
        edges.sort(key=lambda e: e[0])

        # Kruskal's algorithm with union-find.
        parent = list(range(n_reps))

        def find(k: int) -> int:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        coupled_segments: list[CoupledSegmentSpec] = []
        for weight, i, j in edges:
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            parent[ri] = rj
            p_i, n_i = rep_pads[i]
            p_j, n_j = rep_pads[j]
            polarity_swap = self._polarity_swap_between(p_i, n_i, p_j, n_j)
            coupled_segments.append(
                CoupledSegmentSpec(
                    p_start=p_i,
                    p_end=p_j,
                    n_start=n_i,
                    n_end=n_j,
                    polarity_swap=polarity_swap,
                )
            )
            if len(coupled_segments) == n_reps - 1:
                break

        return coupled_segments, stub_edges

    def _route_stub_edges(self, stubs: list[StubEdgeSpec]) -> list[Route]:
        """Route intra-net stub edges via the autorouter's pad-to-pad pathfinder.

        Issue #2473: Stub edges are short single-net hops between pads
        in the same cluster (e.g., USB-C A6 -> B6 within USB_D+).  They
        do not need coupled routing because they are not coupled with
        any other net — they are a short continuation of a single
        polarity that has already been routed via the coupled run.

        Routes that fail are silently dropped: this is best-effort
        completion of the stub, and the main strategy can still pick
        them up afterwards.
        """
        results: list[Route] = []
        for stub in stubs:
            try:
                route = self.autorouter.router.route(stub.start, stub.end)
            except Exception as exc:  # pragma: no cover — defensive
                print(f"    WARNING: stub route raised: {exc}")
                route = None

            if route is None:
                # Issue #3508: synthesized-tail fallback.  The per-net
                # ``route()`` machinery declines sub-millimetre hops
                # whose endpoints sit inside pad clearance halos (e.g.
                # USB-C A6 -> B6 within a coupled pair's net) -- but
                # when the pre-phase claims the net as routed, the
                # negotiated main strategy SKIPS it (#2464) and the
                # stub is never completed (measured: USB2_D+ incomplete
                # at 18/21 reach on the first #3508 re-route).  The
                # geometric tail constructor validates straight /
                # dogleg / U-shaped candidates cell-by-cell against the
                # grid, which handles exactly this pad-halo geometry.
                try:
                    grid = self.autorouter.grid
                    pf = CoupledPathfinder(grid, self.autorouter.rules, 1)
                    layer_idx = grid.layer_to_index(stub.start.layer.value)
                    route = self._synthesize_tail(pf, stub.start, stub.end, layer_idx)
                    if route is None:
                        # Planar candidates exhausted (USB-C A6 -> B6 is
                        # fully fenced by the neighbouring pin halos on
                        # the surface layer): try the two-via
                        # layer-change tail.  ``partner_segments=[]``
                        # because a stub has no coupled partner to keep
                        # clear of -- the grid validation still applies.
                        route = self._synthesize_crossing_tail(
                            pf, stub.start, stub.end, layer_idx, []
                        )
                except Exception as exc:  # pragma: no cover — defensive
                    print(f"    WARNING: stub tail synthesis raised: {exc}")
                    route = None
                if route is not None:
                    print(
                        f"    Stub edge {stub.start.net_name} "
                        f"{stub.start.ref}.{stub.start.pin} -> "
                        f"{stub.end.ref}.{stub.end.pin} completed via "
                        f"synthesized tail (issue #3508)"
                    )

            if route is None:
                print(
                    f"    WARNING: stub edge {stub.start.net_name} "
                    f"{stub.start.ref}.{stub.start.pin} -> "
                    f"{stub.end.ref}.{stub.end.pin} failed (deferred to main strategy)"
                )
                continue

            # Use the autorouter's unified marking helper so both the
            # Python and C++ grids stay synchronized.
            self.autorouter._mark_route(route)
            self.autorouter.routes.append(route)
            results.append(route)
        return results

    def _remark_route_cells(self, route: Route) -> None:
        """Re-mark an ALREADY-COMMITTED route's cell envelope (issue #3508).

        Used after an in-place geometry mutation (the inline serpentine
        shim) on a route that ``autorouter._mark_route`` has already
        committed.  Cell marking is idempotent, so re-rasterising every
        segment simply adds the NEW copper's envelope; the
        non-idempotent bookkeeping (``grid.routes`` append, R-tree
        insertion) is intentionally NOT repeated because the route
        object is already registered.  The replaced straight chord's
        cells stay marked -- conservative (own-net) and harmless.

        Note the R-tree keeps the pre-mutation segment set; the grid
        CELLS are the collision source of truth for the negotiated
        router and ``GridCollisionChecker``, which is what the
        downstream passes use.
        """
        grid = self.autorouter.grid
        for seg in route.segments:
            total_clearance = seg.width / 2 + grid.rules.trace_clearance
            clearance_cells = int(total_clearance / grid.resolution) + 1
            # Mirror the #1666 grid-quantization safety margin used by
            # ``RoutingGrid.mark_route``.
            clearance_cells += 1
            grid._mark_segment(seg, clearance_cells=clearance_cells)
        self.autorouter._mark_route_on_cpp_grid(route)

    def _virtual_pad_at(self, template: Pad, wx: float, wy: float, layer_idx: int) -> Pad:
        """Virtual pad at an arbitrary board position (issue #3508).

        Used as the start/end anchor for synthesized tail routes and as
        the reconstruction end pad (so ``_build_route_from_path`` does
        not force a straight final jump onto the real pad).
        """
        grid = self.autorouter.grid
        return Pad(
            x=wx,
            y=wy,
            width=template.width,
            height=template.height,
            net=template.net,
            net_name=template.net_name,
            layer=Layer(grid.index_to_layer(layer_idx)),
            ref=template.ref,
            pin=template.pin,
        )

    def _segment_cells_clear(
        self,
        pathfinder: CoupledPathfinder,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        net: int,
    ) -> bool:
        """True when every grid cell under the segment is legal for ``net``.

        Issue #3508: the grid encodes the full centerline clearance
        envelope at obstacle-marking time (see ``_is_trace_blocked``),
        so a clear rasterisation implies a clearance-clear segment.
        """
        grid = self.autorouter.grid
        gx1, gy1 = grid.world_to_grid(x1, y1)
        gx2, gy2 = grid.world_to_grid(x2, y2)
        steps = max(abs(gx2 - gx1), abs(gy2 - gy1))
        for i in range(steps + 1):
            t = i / steps if steps else 0.0
            gx = int(round(gx1 + (gx2 - gx1) * t))
            gy = int(round(gy1 + (gy2 - gy1) * t))
            if pathfinder._is_cell_blocked(gx, gy, layer_idx, net):
                return False
        return True

    def _synthesize_tail(
        self, pathfinder: CoupledPathfinder, head: Pad, goal: Pad, layer_idx: int
    ) -> Route | None:
        """Geometric head->pad tail on the head's layer (issue #3508).

        The per-net ``route()`` machinery declines sub-millimetre hops
        whose endpoints sit inside pad clearance halos (measured: every
        board 06 rescue tail it was offered), so we draw the tail
        directly -- a straight segment, or an axis-aligned dogleg --
        and validate every covered grid cell with
        :meth:`_segment_cells_clear`.
        """
        grid = self.autorouter.grid
        goal_layer_idx = grid.layer_to_index(goal.layer.value)
        if goal_layer_idx != layer_idx:
            return None  # layer mismatch: leave to the A* fallback
        width = pathfinder._get_trace_width_for_net(head.net_name)
        layer = Layer(grid.index_to_layer(layer_idx))

        candidates: list[list[tuple[float, float, float, float]]] = [
            [(head.x, head.y, goal.x, goal.y)],  # direct
            [  # dogleg via (goal.x, head.y)
                (head.x, head.y, goal.x, head.y),
                (goal.x, head.y, goal.x, goal.y),
            ],
            [  # dogleg via (head.x, goal.y)
                (head.x, head.y, head.x, goal.y),
                (head.x, goal.y, goal.x, goal.y),
            ],
        ]
        # Issue #3508: U-shaped detours.  Neighbour-pad halos often
        # block both straight doglegs (e.g. a partner pad sitting
        # between the shadow head and its goal pad on a connector pin
        # row); a small perpendicular excursion around the blocker is
        # routinely legal.
        # Issue #3508 (second pass): offsets extended to +/-3.2 mm so
        # intra-connector stubs (USB-C A6 -> B6) can wrap around the
        # full pin-row halo band; every candidate is still validated
        # cell-by-cell so larger detours are safe.
        for off in (0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6, 2.4, -2.4, 3.2, -3.2):
            wy = head.y + off
            candidates.append(
                [
                    (head.x, head.y, head.x, wy),
                    (head.x, wy, goal.x, wy),
                    (goal.x, wy, goal.x, goal.y),
                ]
            )
            wx = head.x + off
            candidates.append(
                [
                    (head.x, head.y, wx, head.y),
                    (wx, head.y, wx, goal.y),
                    (wx, goal.y, goal.x, goal.y),
                ]
            )
        for segs in candidates:
            if all(
                self._segment_cells_clear(pathfinder, x1, y1, x2, y2, layer_idx, head.net)
                for x1, y1, x2, y2 in segs
            ):
                route = Route(net=head.net, net_name=head.net_name)
                for x1, y1, x2, y2 in segs:
                    if abs(x2 - x1) < 0.01 and abs(y2 - y1) < 0.01:
                        continue
                    route.segments.append(
                        Segment(
                            x1=x1,
                            y1=y1,
                            x2=x2,
                            y2=y2,
                            width=width,
                            layer=layer,
                            net=head.net,
                            net_name=head.net_name,
                        )
                    )
                if route.segments:
                    return route
        return None

    def _pair_seg_clearance(self, pathfinder: CoupledPathfinder, net_name: str) -> float:
        """Centerline distance bound between pair partners (issue #3508).

        Same-layer P/N copper must keep ``width/2 + intra_pair_clearance
        + width/2`` of centerline separation -- the intra-pair bound the
        diffpair DRC family checks, NOT the inter-net manufacturer
        clearance (using the latter rejects legitimately-coupled
        geometry: the coupled gap is intentionally tighter).
        """
        net_class_map = getattr(self.autorouter, "net_class_map", None) or {}
        nc = net_class_map.get(net_name)
        intra = (
            nc.effective_intra_pair_clearance()
            if nc is not None
            else self.autorouter.rules.trace_clearance
        )
        width = pathfinder._get_trace_width_for_net(net_name)
        return width + float(intra)

    @staticmethod
    def _point_segment_distance(px: float, py: float, seg: Segment) -> float:
        """Euclidean distance from a point to a segment's centerline."""
        vx = seg.x2 - seg.x1
        vy = seg.y2 - seg.y1
        wx = px - seg.x1
        wy = py - seg.y1
        denom = vx * vx + vy * vy
        if denom < 1e-12:
            return math.hypot(wx, wy)
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
        return math.hypot(px - (seg.x1 + t * vx), py - (seg.y1 + t * vy))

    def _min_distance_to_partner(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        partner_segments: list[Segment],
        layer: Layer | None,
        sample_step: float = 0.05,
    ) -> float:
        """Min centerline distance from a segment to partner copper.

        ``layer`` of ``None`` compares against ALL partner segments
        (used for via barrels, which span every layer); otherwise only
        same-layer partner segments are considered.
        """
        best = float("inf")
        seg_len = math.hypot(x2 - x1, y2 - y1)
        n_steps = max(1, int(math.ceil(seg_len / sample_step)))
        for ps in partner_segments:
            if layer is not None and ps.layer != layer:
                continue
            for i in range(n_steps + 1):
                t = i / n_steps
                d = self._point_segment_distance(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, ps)
                if d < best:
                    best = d
        return best

    def _pair_has_physical_overlap(self, p_route: Route, n_route: Route) -> bool:
        """True when P/N copper PHYSICALLY intersects (issue #3508).

        Mirrors the recipe-side shapely detector
        (``_repair_pair_overlap_solo``'s ``_sides_overlap``) that rips
        pairs in board 06's 6b repair: same-layer seg/seg overlap,
        via-barrel vs any-layer segments, and via-vs-via.  The #3320
        gate only sees same-layer SEGMENT pairs, so a crossing tail or
        shadow via overlapping the partner sailed through it and was
        ripped (de-coupled) downstream.  Running the full check at
        construction time keeps the committed pre-phase output
        rip-proof.
        """
        for a, b in ((p_route, n_route), (n_route, p_route)):
            for via in a.vias:
                bound_any = via.diameter / 2
                for seg in b.segments:
                    if self._point_segment_distance(via.x, via.y, seg) < bound_any + seg.width / 2:
                        return True
                for w in b.vias:
                    if math.hypot(via.x - w.x, via.y - w.y) < bound_any + w.diameter / 2:
                        return True
        for ps in p_route.segments:
            for ns in n_route.segments:
                if ps.layer != ns.layer:
                    continue
                if (
                    self._min_distance_to_partner(ps.x1, ps.y1, ps.x2, ps.y2, [ns], ps.layer)
                    < (ps.width + ns.width) / 2
                ):
                    return True
        return False

    def _synthesize_crossing_tail(
        self,
        pathfinder: CoupledPathfinder,
        head: Pad,
        goal: Pad,
        layer_idx: int,
        partner_segments: list[Segment],
    ) -> Route | None:
        """Two-via layer-change tail that may cross the partner guide.

        Issue #3508: polarity-swap pairs (and in-line connector exits)
        require the tail to cross the partner's path.  A same-layer
        crossing is a short, so the crossing portion dives to another
        routable layer between two vias.  Each candidate is validated
        cell-by-cell per layer, via positions are checked with the
        pathfinder's via predicate, and -- because the partner guide is
        NOT in the grid -- explicit geometric clearance against the
        partner segments is enforced: same-layer segment portions and
        via barrels keep ``via_diameter/2 + trace_clearance +
        partner_width/2`` of centerline distance.
        """
        grid = self.autorouter.grid
        rules = self.autorouter.rules
        goal_layer_idx = grid.layer_to_index(goal.layer.value)
        if goal_layer_idx != layer_idx:
            return None
        width = pathfinder._get_trace_width_for_net(head.net_name)
        partner_width = max((ps.width for ps in partner_segments), default=width)
        # Via barrel vs partner trace clearance bound (vias are not
        # pair members; the standard manufacturer clearance applies).
        via_clear = rules.via_diameter / 2 + rules.trace_clearance + partner_width / 2
        # Same-layer trace vs partner trace: the intra-pair bound.
        seg_clear = self._pair_seg_clearance(pathfinder, head.net_name)

        surface = Layer(grid.index_to_layer(layer_idx))
        routable = [li for li in grid.get_routable_indices() if li != layer_idx]
        if not routable:
            return None

        dx = goal.x - head.x
        dy = goal.y - head.y
        run = math.hypot(dx, dy)
        if run < 1e-9:
            return None
        ux, uy = dx / run, dy / run
        nxp, nyp = -uy, ux  # unit normal

        # Candidate via sites around each endpoint.  Tails are short
        # (sub-2 mm), so the two vias generally cannot sit ON the
        # head->goal line; lateral offsets give the crossover room.
        def _via_candidates(cx: float, cy: float, toward: float) -> list[tuple[float, float]]:
            out = []
            for a in (0.0, 0.5, 1.0):
                for b in (0.0, 0.6, -0.6, 1.2, -1.2):
                    out.append((cx + toward * ux * a + nxp * b, cy + toward * uy * a + nyp * b))
            return out

        # Issue #3855: board-wide drill registry (through-hole pads + all
        # committed vias, any net) so each fan-out via candidate can be
        # rejected when its drill would sit within ``min_hole_to_hole``
        # edge-to-edge of an existing drill.  Assembled once per crossover.
        from .via_clearance import drill_hole_to_hole_clear

        existing_drills = self._collect_existing_drills()
        min_h2h = getattr(rules, "min_hole_to_hole", 0.5)

        for v1 in _via_candidates(head.x, head.y, 1.0):
            for v2 in _via_candidates(goal.x, goal.y, -1.0):
                # Issue #3855: replace the hardcoded 0.6mm center-to-center
                # via-to-via check with an edge-to-edge ``min_hole_to_hole``
                # check.  This single crossover's two vias must clear each
                # other AND every other existing drill (other crossovers'
                # fan-out vias + through-hole pad drills, any net).
                edge_v1v2 = (
                    math.hypot(v2[0] - v1[0], v2[1] - v1[1])
                    - rules.via_drill / 2
                    - rules.via_drill / 2
                )
                if edge_v1v2 < min_h2h:
                    continue  # the two crossover vias too close drill-to-drill
                if not drill_hole_to_hole_clear(
                    v1[0], v1[1], rules.via_drill, existing_drills, min_h2h
                ):
                    continue
                if not drill_hole_to_hole_clear(
                    v2[0], v2[1], rules.via_drill, existing_drills, min_h2h
                ):
                    continue
                g1 = grid.world_to_grid(*v1)
                g2 = grid.world_to_grid(*v2)
                if pathfinder._is_via_blocked(g1[0], g1[1], head.net):
                    continue
                if pathfinder._is_via_blocked(g2[0], g2[1], head.net):
                    continue
                # Via barrels: distance to partner copper on ANY layer.
                if (
                    self._min_distance_to_partner(
                        v1[0], v1[1], v1[0], v1[1], partner_segments, None
                    )
                    < via_clear
                ):
                    continue
                if (
                    self._min_distance_to_partner(
                        v2[0], v2[1], v2[0], v2[1], partner_segments, None
                    )
                    < via_clear
                ):
                    continue
                # Surface stubs must stay clear of same-layer partner copper.
                if (
                    self._min_distance_to_partner(
                        head.x, head.y, v1[0], v1[1], partner_segments, surface
                    )
                    < seg_clear
                ):
                    continue
                if (
                    self._min_distance_to_partner(
                        v2[0], v2[1], goal.x, goal.y, partner_segments, surface
                    )
                    < seg_clear
                ):
                    continue
                if not self._segment_cells_clear(
                    pathfinder, head.x, head.y, v1[0], v1[1], layer_idx, head.net
                ):
                    continue
                if not self._segment_cells_clear(
                    pathfinder, v2[0], v2[1], goal.x, goal.y, layer_idx, head.net
                ):
                    continue
                for alt in routable:
                    if not self._segment_cells_clear(
                        pathfinder, v1[0], v1[1], v2[0], v2[1], alt, head.net
                    ):
                        continue
                    alt_layer = Layer(grid.index_to_layer(alt))
                    # Issue #3508 (second pass): the partner guide is
                    # NOT in the grid, so the alt-layer crossover must
                    # also be checked geometrically against partner
                    # copper ON THAT LAYER -- a via-bearing partner
                    # guide has inner-layer segments the cell check
                    # cannot see (measured: USB3_RX1/RX2 "physically
                    # overlapping copper" rips in the recipe's 6b
                    # repair even with nudge protection).
                    if (
                        self._min_distance_to_partner(
                            v1[0], v1[1], v2[0], v2[1], partner_segments, alt_layer
                        )
                        < seg_clear
                    ):
                        continue
                    route = Route(net=head.net, net_name=head.net_name)
                    if math.hypot(v1[0] - head.x, v1[1] - head.y) > 0.01:
                        route.segments.append(
                            Segment(
                                x1=head.x,
                                y1=head.y,
                                x2=v1[0],
                                y2=v1[1],
                                width=width,
                                layer=surface,
                                net=head.net,
                                net_name=head.net_name,
                            )
                        )
                    route.vias.append(
                        Via(
                            x=v1[0],
                            y=v1[1],
                            drill=rules.via_drill,
                            diameter=rules.via_diameter,
                            layers=(surface, alt_layer),
                            net=head.net,
                            net_name=head.net_name,
                        )
                    )
                    route.segments.append(
                        Segment(
                            x1=v1[0],
                            y1=v1[1],
                            x2=v2[0],
                            y2=v2[1],
                            width=width,
                            layer=alt_layer,
                            net=head.net,
                            net_name=head.net_name,
                        )
                    )
                    route.vias.append(
                        Via(
                            x=v2[0],
                            y=v2[1],
                            drill=rules.via_drill,
                            diameter=rules.via_diameter,
                            layers=(alt_layer, surface),
                            net=head.net,
                            net_name=head.net_name,
                        )
                    )
                    if math.hypot(goal.x - v2[0], goal.y - v2[1]) > 0.01:
                        route.segments.append(
                            Segment(
                                x1=v2[0],
                                y1=v2[1],
                                x2=goal.x,
                                y2=goal.y,
                                width=width,
                                layer=surface,
                                net=head.net,
                                net_name=head.net_name,
                            )
                        )
                    return route
        return None

    def _tail_route(
        self,
        pathfinder: CoupledPathfinder,
        head: Pad,
        goal: Pad,
        layer_idx: int,
        label: str,
        pair_name: str,
        partner_segments: list[Segment] | None = None,
    ) -> Route | None:
        """Head->pad completion: synthesized tail, then per-net A* fallback.

        Issue #3508: when ``partner_segments`` is provided, planar
        candidates whose copper would overlap the partner (which is NOT
        in the grid) are rejected geometrically, and a two-via crossing
        tail is attempted before giving up -- the polarity-swap pairs'
        terminal crossover.
        """
        tail = self._synthesize_tail(pathfinder, head, goal, layer_idx)
        if tail is not None and partner_segments:
            seg_clear = self._pair_seg_clearance(pathfinder, head.net_name)
            for seg in tail.segments:
                if (
                    self._min_distance_to_partner(
                        seg.x1, seg.y1, seg.x2, seg.y2, partner_segments, seg.layer
                    )
                    < seg_clear
                ):
                    tail = None
                    break
        if tail is None and partner_segments:
            tail = self._synthesize_crossing_tail(
                pathfinder, head, goal, layer_idx, partner_segments
            )
        if tail is None:
            tail = self._single_ended_guide_route(head, goal, per_net_timeout=10.0)
            if tail is not None and not tail.segments:
                tail = None
        if tail is None:
            print(
                f"    [coupled-rescue] {label} tail unroutable for {pair_name} "
                f"(head {head.x:.2f},{head.y:.2f} -> pad {goal.x:.2f},{goal.y:.2f})"
            )
        return tail

    def _quantize_shadow_segments(
        self,
        route: Route,
        pathfinder: CoupledPathfinder,
    ) -> None:
        """Rewrite off-angle shadow segments as 45-legal doglegs, in place.

        Issue #3987 (unit 2a of #3921).  The shadow guide is the C++
        on-grid per-net router's output, so every guide segment is already
        45-aligned; off-angle shadow copper comes only from three
        non-offset construction sites in :meth:`_shadow_route_pair`:

        1. the raw miter-apex join at guide corners (acute / mixed
           axis-diagonal turns land the apex off-grid, 3.7-11.9 deg off),
        2. the shadow-via jog segments (the via site is chosen from a
           lateral/stagger lattice that is not on the 8-direction set), and
        3. the pad-approach rescue tails (off-grid pad centres).

        Rather than dogleg each site individually, this pass lifts the
        battle-tested file-layer transform (:func:`quantize.dogleg_points`,
        #3532 / #3907) to the route layer: it walks the assembled
        ``route.segments`` once and replaces any segment whose displacement
        is off the {0, 45, 90, 135} set with a two-leg dogleg that shares
        both endpoints exactly.  This makes shadow copper 45-compliant by
        construction (census-clean, no ``OffAngleSegmentWarning``) with a
        single transform covering all three sites -- the #3975 pattern
        lifted from the file layer to the route layer.

        The pass is OBSTACLE-AWARE (mirroring the subgrid escape doglegs of
        #3975): a dogleg's perpendicular bulge is bounded by
        ``min(|dx|, |dy|)`` but can still touch copper, so each candidate
        variant's legs are re-rastered against ``pathfinder._is_cell_blocked``
        and the first variant whose legs are both clear is kept.  When
        neither the default nor the ``axis_first`` (outboard-bulge) variant
        clears, the original segment is left untouched -- the downstream
        self-check / physical-overlap gates and the emission census still
        apply, so a residual off-angle segment degrades gracefully rather
        than shipping a short.

        The alignment decision is made on the SERIALIZED (4-decimal) copper
        via :func:`quantize.verify_segment_45` -- the same predicate the
        emission census reads -- not on the raw analytic displacement.  A
        shadow body offset can be exactly 45-aligned analytically yet round
        to a 2-quantum-asymmetric diagonal (``dx=0.0501, dy=0.0499``, 0.11
        deg off), which the census flags; deciding on the raw floats would
        skip it.  Doglegging on the rounded endpoints yields one exact axis
        leg + one exact diagonal leg that both pass the census.
        """
        grid = self.autorouter.grid

        def _is_census_clean(x1: float, y1: float, x2: float, y2: float) -> bool:
            # True iff the SERIALIZED segment passes the emission census
            # (``verify_segment_45`` accepts axis/diagonal within one 0.1 um
            # quantum).  Forced strict so it raises rather than warns.
            try:
                verify_segment_45(x1, y1, x2, y2, strict=True)
            except OffAngleSegmentError:
                return False
            return True

        new_segments: list[Segment] = []
        for seg in route.segments:
            # Decide on the serialized (4dp) copper the census governs.
            rx1, ry1 = round(seg.x1, 4), round(seg.y1, 4)
            rx2, ry2 = round(seg.x2, 4), round(seg.y2, 4)
            if _is_census_clean(rx1, ry1, rx2, ry2):
                new_segments.append(seg)
                continue
            li = grid.layer_to_index(seg.layer.value)

            def _legs_clear(mid: tuple[float, float], _li: int = li, _seg: Segment = seg) -> bool:
                for x1, y1, x2, y2 in (
                    (_seg.x1, _seg.y1, mid[0], mid[1]),
                    (mid[0], mid[1], _seg.x2, _seg.y2),
                ):
                    seg_len = math.hypot(x2 - x1, y2 - y1)
                    if seg_len < 1e-9:
                        continue
                    n_steps = max(1, int(math.ceil(seg_len / grid.resolution)))
                    for i in range(n_steps + 1):
                        t = i / n_steps
                        gx, gy = grid.world_to_grid(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
                        if pathfinder._is_cell_blocked(gx, gy, _li, _seg.net):
                            return False
                return True

            chosen_mid: tuple[float, float] | None = None
            for axis_first in (False, True):
                # Dogleg on the ROUNDED endpoints so the two legs the census
                # reads are exactly axis / diagonal.
                pts = dogleg_points(rx1, ry1, rx2, ry2, axis_first=axis_first)
                if len(pts) != 3:
                    # Already aligned after rounding: keep as-is.
                    break
                mid = pts[1]
                # Only accept a variant whose BOTH legs are census-clean AND
                # obstacle-clear.
                if (
                    _is_census_clean(rx1, ry1, mid[0], mid[1])
                    and _is_census_clean(mid[0], mid[1], rx2, ry2)
                    and _legs_clear(mid)
                ):
                    chosen_mid = mid
                    break
            if chosen_mid is None:
                # No clean+clear dogleg variant -- keep the original segment;
                # the self-check / overlap gates and the emission census
                # still apply.  Graceful degradation, not a silent short.
                new_segments.append(seg)
                continue
            for x1, y1, x2, y2 in (
                (rx1, ry1, chosen_mid[0], chosen_mid[1]),
                (chosen_mid[0], chosen_mid[1], rx2, ry2),
            ):
                if math.hypot(x2 - x1, y2 - y1) < 1e-9:
                    continue
                new_segments.append(
                    Segment(
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        width=seg.width,
                        layer=seg.layer,
                        net=seg.net,
                        net_name=seg.net_name,
                    )
                )
        route.segments[:] = new_segments

    @staticmethod
    def _shadow_gap_ladder(d: float, d_min: float, d_max: float) -> list[float]:
        """Ordered candidate offset gaps for the variable-gap parallel offset.

        Issue #3990 (unit 2b of #3921).  Returns a list of center-to-center
        gaps to try for a single guide section, ordered by PREFERENCE:

        1. the nominal ``d`` first -- a section feasible at nominal keeps
           the exact fixed-gap geometry (so the easy pairs are unchanged),
        2. then TIGHTER gaps stepping down toward ``d_min`` -- the fix for
           inside-curve self-overlap (a smaller gap pulls the offset off the
           partner), tried before widening so the coupled gap stays as
           close to nominal as feasibility allows,
        3. then WIDER gaps stepping up toward ``d_max`` -- the fix for
           obstacle blockage (a larger gap steps the offset around copper
           the guide only cleared for a zero-width centerline).

        All returned gaps lie in ``[d_min, d_max]`` (the impedance band).
        ``_SHADOW_GAP_BAND_STEPS`` sets the ladder density.  When the band
        is degenerate (``d_max <= d_min`` or steps <= 1) only ``d`` is
        returned, collapsing to the fixed-gap constructor.
        """
        steps = max(1, _SHADOW_GAP_BAND_STEPS)
        if steps <= 1 or d_max - d_min < 1e-6:
            return [d]
        tighter: list[float] = []
        wider: list[float] = []
        # Uniform ladder resolution across the whole band.
        span = d_max - d_min
        inc = span / steps
        # Tighter rungs: from just below nominal down to d_min.
        g = d - inc
        while g >= d_min - 1e-9:
            tighter.append(max(d_min, g))
            g -= inc
        # Wider rungs: from just above nominal up to d_max.
        g = d + inc
        while g <= d_max + 1e-9:
            wider.append(min(d_max, g))
            g += inc
        ladder = [d]
        # Interleave tighter-first (prefer holding the coupling as tight as
        # feasibility allows), then wider fallbacks for obstacle stepping.
        ladder.extend(tighter)
        ladder.extend(wider)
        # De-dup while preserving order (float rungs can coincide at bounds).
        seen: list[float] = []
        for gv in ladder:
            if all(abs(gv - s) > 1e-9 for s in seen):
                seen.append(gv)
        return seen

    def _shadow_select_gap(
        self,
        seg: Segment,
        nx: float,
        ny: float,
        gap_ladder: list[float],
        layer_idx: int,
        s_net: int,
        pathfinder: CoupledPathfinder,
        guide_segs: list[Segment],
        min_center_dist: float,
    ) -> float:
        """Choose the per-section parallel-offset gap from the impedance band.

        Issue #3990 (unit 2b of #3921).  ``gap_ladder`` is the preference-
        ordered list of candidate center-to-center gaps (nominal first, then
        tighter, then wider -- all inside the impedance band).  This walks
        the ladder and returns the FIRST gap whose offset of ``seg`` (by
        ``gap * (nx, ny)``) is BOTH:

        * obstacle-clear -- every rastered cell of the offset segment is
          unblocked for ``s_net`` (dodges the ``mid-route blockage`` events
          where the guide threaded a zero-width-centerline gap the offset
          cannot fit through), AND
        * partner-clear -- the offset segment's minimum distance to the
          guide (partner) copper on this layer stays at or above
          ``min_center_dist`` (center-to-center), i.e. the coupled EDGE gap
          holds at or above the intra-pair clearance floor (dodges the
          inside-curve ``self-check overlap`` events).

        Because the ladder tries the nominal gap first and tighter rungs
        before wider ones, an easy segment keeps the exact nominal geometry,
        an inside-curve segment tightens just enough to pull off the
        partner, and an obstructed segment widens just enough to step
        around the obstacle -- always within ``[d_min, d_max]``.

        When NO ladder rung is feasible the nominal gap (the ladder head) is
        returned unchanged: the downstream self-check / physical-overlap
        gates and the trim logic still apply, so an infeasible section
        degrades to today's fixed-gap behaviour rather than shipping a
        violation.
        """
        grid = self.autorouter.grid
        step = grid.resolution
        for gap in gap_ladder:
            ax, ay = seg.x1 + gap * nx, seg.y1 + gap * ny
            bx, by = seg.x2 + gap * nx, seg.y2 + gap * ny
            # Obstacle raster over the candidate offset segment.
            seg_len = math.hypot(bx - ax, by - ay)
            if seg_len < 1e-9:
                return gap
            n_steps = max(1, int(math.ceil(seg_len / step)))
            blocked = False
            for i in range(n_steps + 1):
                t = i / n_steps
                gx, gy = grid.world_to_grid(ax + (bx - ax) * t, ay + (by - ay) * t)
                if pathfinder._is_cell_blocked(gx, gy, layer_idx, s_net):
                    blocked = True
                    break
            if blocked:
                continue
            # Partner-clearance: keep the coupled edge gap >= intra floor.
            if (
                self._min_distance_to_partner(ax, ay, bx, by, guide_segs, seg.layer)
                < min_center_dist
            ):
                continue
            return gap
        # No feasible rung: keep nominal (ladder head), degrade gracefully.
        return gap_ladder[0]

    def _shadow_route_pair(
        self,
        pair: DifferentialPair,
        spec: CoupledSegmentSpec,
        pathfinder: CoupledPathfinder,
        guide: Route,
        spacing_cells: int,
        swap_roles: bool = False,
    ) -> tuple[Route, Route] | None:
        """Construct the pair as guide + validated parallel shadow.

        Issue #3508: the joint-state coupled A* cannot afford board
        06's geometry even corridor-bounded and weighted (measured: a
        clearance-clean MIPI_CLK search exceeds 80k iterations without
        converging; the dirty 2.7k-iteration solution is rejected by
        the #3320 gate).  This constructor sidesteps the search
        entirely:

        1. One side is the single-ended guide route (C++-accelerated
           per-net A*; the P side by default, the N side when
           ``swap_roles``).
        2. The partner side is built GEOMETRICALLY: each single-layer
           SECTION of the guide polyline is offset perpendicular by
           the coupled center-to-center spacing (both lateral sides
           tried).  Where the guide changes layers, the shadow places
           its own via at a laterally-widened, longitudinally-staggered
           site so both the via-to-via (0.6 mm) and via-to-partner-
           trace (~0.49 mm) clearance bounds hold by construction.
           Every shadow segment is rasterised against the grid's
           clearance envelope; shadow via sites are checked with the
           pathfinder's via predicate.
        3. The body is TRIMMED at the two route ends (endpoint zones
           are always contested by connector/IC neighbour-pad halos)
           up to ``_SHADOW_MAX_TRIM_MM`` per end, and connects to the
           real pads via the rescue tail machinery (partner-aware,
           with a two-via crossing fallback for polarity-swap
           terminations).

        Returns ``(p_route, n_route)`` -- NOT committed; the caller
        runs the #3320 severe-overlap gate and the normal commit path.
        """
        if not guide.segments:
            return None
        grid = self.autorouter.grid
        rules = self.autorouter.rules
        if swap_roles:
            shadow_start, shadow_end = spec.p_start, spec.p_end
        else:
            shadow_start, shadow_end = spec.n_start, spec.n_end
        s_net = shadow_start.net
        s_net_name = shadow_start.net_name
        s_width = pathfinder._get_trace_width_for_net(s_net_name)
        d = spacing_cells * grid.resolution

        # Issue #3990 (unit 2b of #3921): the parallel-offset gap is a BAND,
        # not a single value.  ``d`` above is the nominal center-to-center
        # spacing; the offset for each guide section may vary within
        # ``[d_min, d_max]`` to dodge inside-curve self-overlap (tighten
        # toward ``d_min``) and obstacle blockages (widen toward ``d_max``)
        # while both legs stay inside the impedance tolerance band.
        #
        # Band source (authoritative):
        #   * ``d_min`` -- the intra-pair clearance FLOOR.  The center-to-
        #     center spacing must keep at least
        #     ``trace_width + effective_intra_pair_clearance()`` so the
        #     within-pair EDGE clearance holds; this is exactly the
        #     ``required_center_spacing`` the caller derives for
        #     ``min_spacing_cells`` (``route_differential_pair_coupled``).
        #     Read from the pair's ``NetClassRouting`` when available.
        #   * ``d_max`` -- the impedance CEILING.  Widening the gap lowers
        #     coupling and raises the differential impedance; the net
        #     class' ``impedance_tolerance_percent`` (the same tolerance the
        #     ``ImpedanceRule`` DRC fires on) bounds how far.  Differential
        #     impedance is monotone-increasing and near-linear in the gap
        #     for small deviations, so bounding the GAP deviation by that
        #     percentage is a conservative proxy that keeps the pair inside
        #     the impedance band.  Capped at ``_SHADOW_GAP_MAX_TOL_FRAC``.
        pair_nc = (getattr(self.autorouter, "net_class_map", None) or {}).get(spec.p_start.net_name)
        if pair_nc is not None:
            nc_trace_width = float(pair_nc.trace_width)
            intra_floor = float(pair_nc.effective_intra_pair_clearance())
            tol_frac = min(
                _SHADOW_GAP_MAX_TOL_FRAC,
                max(0.0, float(pair_nc.impedance_tolerance_percent) / 100.0),
            )
        else:
            nc_trace_width = float(s_width)
            intra_floor = float(rules.trace_clearance)
            tol_frac = _SHADOW_GAP_MAX_TOL_FRAC
        # Never let the floor exceed the nominal (a class whose min-spacing
        # already equals the nominal collapses the band to the single ``d``).
        d_min = min(d, nc_trace_width + intra_floor)
        d_max = d * (1.0 + tol_frac)
        # Candidate gaps: a linear ladder from ``d_min`` up to ``d_max``,
        # always including the nominal ``d`` and preferring the nominal so a
        # section that is feasible at nominal is unchanged (byte-for-byte
        # stable relative to the fixed-gap constructor for the easy pairs).
        gap_ladder = self._shadow_gap_ladder(d, d_min, d_max)
        # Shadow via lateral offset: the barrel must clear the guide
        # trace (via_r + clearance + guide_width/2), independent of the
        # tighter coupled gap d.
        guide_width = max((g.width for g in guide.segments), default=s_width)
        # Issue #3541: the perpendicular distance from the shadow via to
        # the partner (guide) copper must keep this bound everywhere, not
        # just at the projected guide-via point ``gv``.  The guide BENDS
        # at the layer change, so a nominal ``via_lateral`` offset taken
        # against the incoming leg's normal can still let the barrel
        # intersect the outgoing leg (measured: ~0.04 mm intersection at
        # board 06's 0.075-0.15 mm coupled gaps).  ``via_clear`` is the
        # same via-barrel-vs-partner bound the crossing-tail synthesizer
        # enforces (see ``_synthesize_crossing_tail``); we validate each
        # candidate site against the guide polyline with it and widen the
        # perpendicular spread (the ``lat_mult`` lattice) until it holds.
        via_clear = rules.via_diameter / 2 + rules.trace_clearance + guide_width / 2
        via_lateral = max(d, via_clear + 0.05)
        guide_segs = list(guide.segments)
        # Longitudinal stagger so shadow-via-to-guide-via >= via pitch.
        via_pitch = rules.via_diameter + rules.via_clearance
        stagger = max(0.0, math.sqrt(max(0.0, via_pitch**2 - via_lateral**2))) + 0.05

        # ------------------------------------------------------------
        # Parse the guide into ordered single-layer sections.  Route
        # segments/vias are emitted in path order by both
        # ``_build_route_from_path`` and the per-net pathfinder.
        # ------------------------------------------------------------
        sections: list[tuple[int, list[Segment]]] = []
        for seg in guide.segments:
            li = grid.layer_to_index(seg.layer.value)
            if not sections or sections[-1][0] != li:
                sections.append((li, []))
            sections[-1][1].append(seg)
        max_trim = _SHADOW_MAX_TRIM_MM

        for side in (1.0, -1.0):
            elements: list[tuple] = []  # ('seg', x1,y1,x2,y2,layer) | ('via', x,y,l0,l1)
            ok = True
            prev_pt: tuple[float, float] | None = None
            prev_layer: int | None = None
            prev_dir: tuple[float, float] | None = None
            for sec_layer, segs in sections:
                first_in_section = True
                for seg in segs:
                    ux = seg.x2 - seg.x1
                    uy = seg.y2 - seg.y1
                    length = math.hypot(ux, uy)
                    if length < 1e-9:
                        continue
                    ux /= length
                    uy /= length
                    nx = -uy * side
                    ny = ux * side
                    # Issue #3990 (unit 2b): pick the per-section offset gap
                    # from the impedance band.  Prefer the nominal ``d``;
                    # tighten (dodges inside-curve self-overlap) or widen
                    # (dodges obstacle blockage) only when the nominal offset
                    # is infeasible for THIS segment.  Feasibility is judged
                    # on the offset segment's grid raster (obstacle-clear)
                    # and its distance to the guide/partner copper (>= the
                    # intra-pair clearance floor, so the coupled edge gap
                    # holds).  Both bounds keep the pair inside the impedance
                    # band by construction (``gap_ladder`` rungs are all in
                    # ``[d_min, d_max]``).
                    seg_gap = self._shadow_select_gap(
                        seg,
                        nx,
                        ny,
                        gap_ladder,
                        sec_layer,
                        s_net,
                        pathfinder,
                        guide_segs,
                        intra_floor + s_width / 2.0 + guide_width / 2.0,
                    )
                    a = (seg.x1 + seg_gap * nx, seg.y1 + seg_gap * ny)
                    b = (seg.x2 + seg_gap * nx, seg.y2 + seg_gap * ny)
                    if prev_pt is not None and prev_layer is not None:
                        if first_in_section and prev_layer != sec_layer:
                            # Guide layer change: place the shadow via.
                            # Site: widen laterally to ``via_lateral``
                            # and stagger back along the incoming
                            # direction.
                            pux, puy = prev_dir if prev_dir else (ux, uy)
                            pnx, pny = -puy * side, pux * side
                            gv = (seg.x1, seg.y1)  # guide via position
                            placed = False
                            # Issue #3508 (second pass): widened site
                            # lattice -- lateral multipliers beyond 1.0
                            # rescue guide-via neighbourhoods where every
                            # minimum-lateral site is inside a pad halo
                            # (the FFC/J1 "no legal shadow-via site"
                            # failures).  Sites are still validated by
                            # the via predicate, and larger laterals only
                            # ADD clearance to the guide copper.
                            for lat_mult in (1.0, 1.5, 2.2):
                                for stag_mult in (1.0, 1.6, -1.0, -1.6, 2.4, -2.4):
                                    vx = (
                                        gv[0]
                                        + via_lateral * lat_mult * pnx
                                        - stagger * stag_mult * pux
                                    )
                                    vy = (
                                        gv[1]
                                        + via_lateral * lat_mult * pny
                                        - stagger * stag_mult * puy
                                    )
                                    gvx, gvy = grid.world_to_grid(vx, vy)
                                    if pathfinder._is_via_blocked(gvx, gvy, s_net):
                                        continue
                                    # Issue #3541: the guide is NOT in the
                                    # grid, so ``_is_via_blocked`` cannot
                                    # see a barrel grazing the partner.
                                    # The barrel offset is taken against
                                    # the INCOMING leg's normal, but the
                                    # guide BENDS at the via -- so a site
                                    # that clears the incoming leg can
                                    # still intersect the OUTGOING leg
                                    # when the guide turns toward the
                                    # shadow side (measured: ~0.04 mm
                                    # overlap at board 06's 0.075-0.15 mm
                                    # gaps).  Reject any candidate whose
                                    # barrel violates the via-vs-partner
                                    # clearance against the WHOLE guide
                                    # polyline (any layer -- the barrel
                                    # spans all layers); the lattice then
                                    # widens the perpendicular spread
                                    # (larger ``lat_mult``) until a site
                                    # clears every guide segment.
                                    if (
                                        self._min_distance_to_partner(
                                            vx, vy, vx, vy, guide_segs, None
                                        )
                                        < via_clear
                                    ):
                                        continue
                                    elements.append(
                                        ("seg", prev_pt[0], prev_pt[1], vx, vy, prev_layer)
                                    )
                                    elements.append(("via", vx, vy, prev_layer, sec_layer))
                                    elements.append(("seg", vx, vy, a[0], a[1], sec_layer))
                                    prev_pt = a
                                    placed = True
                                    break
                                if placed:
                                    break
                            if not placed:
                                ok = False
                                break
                        else:
                            gap = math.hypot(a[0] - prev_pt[0], a[1] - prev_pt[1])
                            if gap > grid.resolution / 2:
                                # Issue #3508: MITER the corner join.  A
                                # straight bevel chord between the two
                                # offset endpoints passes INSIDE the
                                # corner (d*cos(45deg) ~ 0.75*d at a 90
                                # degree turn), shaving the coupled gap
                                # below the intra-pair clearance -- the
                                # measured one-mild-violation-per-pair
                                # Phase B churn.  Extending both offset
                                # segments to their line intersection
                                # keeps the full offset everywhere.
                                mx = None
                                if elements and elements[-1][0] == "seg":
                                    pseg = elements[-1]
                                    d1x, d1y = pseg[3] - pseg[1], pseg[4] - pseg[2]
                                    d2x, d2y = b[0] - a[0], b[1] - a[1]
                                    denom = d1x * d2y - d1y * d2x
                                    if abs(denom) > 1e-9:
                                        t = (
                                            (a[0] - pseg[3]) * d2y - (a[1] - pseg[4]) * d2x
                                        ) / denom
                                        cand = (
                                            pseg[3] + d1x * t,
                                            pseg[4] + d1y * t,
                                        )
                                        # Bound the miter spike (sharp
                                        # angles) to ~2 gaps.
                                        if (
                                            math.hypot(
                                                cand[0] - prev_pt[0],
                                                cand[1] - prev_pt[1],
                                            )
                                            <= 2.0 * d + gap
                                        ):
                                            mx = cand
                                if mx is not None:
                                    pseg = elements[-1]
                                    elements[-1] = ("seg", pseg[1], pseg[2], mx[0], mx[1], pseg[5])
                                    a = mx
                                else:
                                    elements.append(
                                        ("seg", prev_pt[0], prev_pt[1], a[0], a[1], sec_layer)
                                    )
                    elements.append(("seg", a[0], a[1], b[0], b[1], sec_layer))
                    prev_pt = b
                    prev_layer = sec_layer
                    prev_dir = (ux, uy)
                    first_in_section = False
                if not ok:
                    break
            if not ok or prev_pt is None:
                print(
                    f"    [coupled-shadow] side={side:+.0f} no legal shadow-via "
                    f"site for {pair.name}"
                )
                continue

            # ------------------------------------------------------------
            # Validate + trim.  Blocked cells are tolerated only within
            # ``max_trim`` of either END of the whole shadow polyline;
            # any blockage in the interior (including via jogs) fails
            # this side.
            # ------------------------------------------------------------
            arc_total = sum(math.hypot(e[3] - e[1], e[4] - e[2]) for e in elements if e[0] == "seg")
            arc = 0.0
            interior_block = False
            step = grid.resolution
            blocked_arcs: list[float] = []
            for e in elements:
                if e[0] == "via":
                    continue
                _, x1, y1, x2, y2, li = e
                seg_len = math.hypot(x2 - x1, y2 - y1)
                if seg_len < 1e-9:
                    continue
                n_steps = max(1, int(math.ceil(seg_len / step)))
                for i in range(n_steps + 1):
                    t = i / n_steps
                    gx, gy = grid.world_to_grid(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
                    if pathfinder._is_cell_blocked(gx, gy, li, s_net):
                        blocked_arcs.append(arc + seg_len * t)
                arc += seg_len
            trim_start = 0.0
            trim_end = 0.0
            for ba in blocked_arcs:
                if ba <= max_trim and ba >= trim_start:
                    if ba <= max_trim:
                        trim_start = max(trim_start, ba)
                if ba >= arc_total - max_trim:
                    trim_end = max(trim_end, arc_total - ba)
            for ba in blocked_arcs:
                if ba > trim_start + 1e-9 and ba < arc_total - trim_end - 1e-9:
                    interior_block = True
                    print(
                        f"    [coupled-shadow] side={side:+.0f} mid-route "
                        f"blockage for {pair.name} at arc {ba:.2f}/"
                        f"{arc_total:.2f}mm"
                    )
                    break
            if interior_block:
                continue
            a0 = trim_start + 2 * step if trim_start > 0 else 0.0
            a1 = arc_total - trim_end - (2 * step if trim_end > 0 else 0.0)
            if a1 - a0 < 2.0:
                print(
                    f"    [coupled-shadow] side={side:+.0f} clear run too "
                    f"short for {pair.name} ({a1 - a0:.2f}mm)"
                )
                continue

            # Slice elements to [lo, hi] by arc length.  Vias are kept
            # only if inside the kept interval (vias always are: the
            # interior is blockage-free and trims are confined to the
            # ends, which lie in the first/last sections).
            def _slice_kept(lo_arc: float, hi_arc: float) -> list[tuple]:
                kept_: list[tuple] = []
                arc_ = 0.0
                for e_ in elements:
                    if e_[0] == "via":
                        if lo_arc <= arc_ <= hi_arc:
                            kept_.append(e_)
                        continue
                    _, ex1, ey1, ex2, ey2, eli = e_
                    sl = math.hypot(ex2 - ex1, ey2 - ey1)
                    if sl < 1e-9:
                        continue
                    lo_ = max(lo_arc, arc_)
                    hi_ = min(hi_arc, arc_ + sl)
                    if hi_ > lo_:
                        t_lo = (lo_ - arc_) / sl
                        t_hi = (hi_ - arc_) / sl
                        kept_.append(
                            (
                                "seg",
                                ex1 + (ex2 - ex1) * t_lo,
                                ey1 + (ey2 - ey1) * t_lo,
                                ex1 + (ex2 - ex1) * t_hi,
                                ey1 + (ey2 - ey1) * t_hi,
                                eli,
                            )
                        )
                    arc_ += sl
                return kept_

            # Issue #3508: anchor-stepping.  When the tail from the
            # body end to the pad is unroutable (the trimmed body end
            # can sit flush against a halo wall, leaving the tail no
            # legal first step), consume more of the body into the
            # tail and retry from a deeper anchor.
            partner_segs = list(guide.segments)
            start_tail = None
            a0_eff = a0
            for extra0 in (0.0, 0.7, 1.5, 3.0):
                if a0 + extra0 >= a1 - 2.0:
                    break
                kept_probe = _slice_kept(a0 + extra0, a1)
                seg_probe = [e for e in kept_probe if e[0] == "seg"]
                if not seg_probe:
                    break
                bh = (seg_probe[0][1], seg_probe[0][2], seg_probe[0][5])
                anchor = self._virtual_pad_at(shadow_start, bh[0], bh[1], bh[2])
                start_tail = self._tail_route(
                    pathfinder,
                    anchor,
                    shadow_start,
                    bh[2],
                    "shadow-start",
                    pair.name,
                    partner_segments=partner_segs,
                )
                if start_tail is not None:
                    a0_eff = a0 + extra0
                    break
            if start_tail is None:
                continue
            end_tail = None
            a1_eff = a1
            for extra1 in (0.0, 0.7, 1.5, 3.0):
                if a0_eff >= a1 - extra1 - 2.0:
                    break
                kept_probe = _slice_kept(a0_eff, a1 - extra1)
                seg_probe = [e for e in kept_probe if e[0] == "seg"]
                if not seg_probe:
                    break
                bt = (seg_probe[-1][3], seg_probe[-1][4], seg_probe[-1][5])
                anchor = self._virtual_pad_at(shadow_end, bt[0], bt[1], bt[2])
                end_tail = self._tail_route(
                    pathfinder,
                    anchor,
                    shadow_end,
                    bt[2],
                    "shadow-end",
                    pair.name,
                    partner_segments=partner_segs,
                )
                if end_tail is not None:
                    a1_eff = a1 - extra1
                    break
            if end_tail is None:
                continue
            kept = _slice_kept(a0_eff, a1_eff)
            seg_elements = [e for e in kept if e[0] == "seg"]
            if not seg_elements:
                continue

            shadow_route = Route(net=s_net, net_name=s_net_name)
            shadow_route.segments.extend(
                Segment(
                    x1=s.x2,
                    y1=s.y2,
                    x2=s.x1,
                    y2=s.y1,
                    width=s.width,
                    layer=s.layer,
                    net=s.net,
                    net_name=s.net_name,
                )
                for s in reversed(start_tail.segments)
            )
            shadow_route.vias.extend(start_tail.vias)
            for e in kept:
                if e[0] == "via":
                    _, vx, vy, l0, l1 = e
                    shadow_route.vias.append(
                        Via(
                            x=vx,
                            y=vy,
                            drill=rules.via_drill,
                            diameter=rules.via_diameter,
                            layers=(
                                Layer(grid.index_to_layer(l0)),
                                Layer(grid.index_to_layer(l1)),
                            ),
                            net=s_net,
                            net_name=s_net_name,
                        )
                    )
                    continue
                _, x1, y1, x2, y2, li = e
                if math.hypot(x2 - x1, y2 - y1) < 1e-6:
                    continue
                shadow_route.segments.append(
                    Segment(
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        width=s_width,
                        layer=Layer(grid.index_to_layer(li)),
                        net=s_net,
                        net_name=s_net_name,
                    )
                )
            shadow_route.segments.extend(end_tail.segments)
            shadow_route.vias.extend(end_tail.vias)

            # Issue #3987 (unit 2a of #3921): make the assembled shadow
            # copper 45-compliant BY CONSTRUCTION.  The guide (P side, or N
            # when swapped) is the C++ on-grid router's output and is
            # already 45-aligned; the geometric shadow (miter apex, via
            # jogs, pad-approach tails) is the only off-angle source.  Run
            # the dogleg pass over the assembled shadow segments BEFORE the
            # self-check / overlap gates below, so the doglegged geometry is
            # what those gates -- and the downstream emission census
            # (#3975) -- validate.  A residual off-angle segment (no clear
            # dogleg variant) degrades gracefully; it is not silently
            # shipped as a short.
            self._quantize_shadow_segments(shadow_route, pathfinder)

            guide_net_pad = spec.n_start if swap_roles else spec.p_start
            guide_route_obj = Route(net=guide_net_pad.net, net_name=guide_net_pad.net_name)
            guide_route_obj.segments.extend(guide.segments)
            guide_route_obj.vias.extend(guide.vias)

            if swap_roles:
                p_route, n_route = shadow_route, guide_route_obj
            else:
                p_route, n_route = guide_route_obj, shadow_route

            # Issue #3508: in-loop severity self-check (the same metric
            # as the caller's #3320 gate).  Tails are routed without
            # partner awareness, so a wrong-side body forces the tail
            # to cross the guide; instead of letting the caller's gate
            # reject the whole pair, fail THIS side over to the other.
            net_class_map = getattr(self.autorouter, "net_class_map", None) or {}
            nc = net_class_map.get(spec.p_start.net_name)
            threshold = (
                nc.effective_intra_pair_clearance()
                if nc is not None
                else self.autorouter.rules.trace_clearance
            )
            violation = find_intra_pair_clearance_violations(
                p_route, n_route, threshold_mm=threshold, pair_name=pair.name
            )
            if violation is not None and violation.actual_clearance_mm < 0.0:
                print(
                    f"    [coupled-shadow] side={side:+.0f} self-check overlap "
                    f"for {pair.name} "
                    f"(worst={violation.actual_clearance_mm:+.3f}mm); trying "
                    f"other side"
                )
                continue
            # Issue #3508 (second pass): full physical-overlap check
            # (via-vs-seg / via-vs-via / seg-vs-seg) mirroring the
            # recipe's 6b rip detector -- the segments-only check above
            # cannot see a crossing-tail via overlapping the partner.
            if self._pair_has_physical_overlap(p_route, n_route):
                print(
                    f"    [coupled-shadow] side={side:+.0f} physical "
                    f"P/N overlap (via-aware) for {pair.name}; trying "
                    f"other side"
                )
                continue
            return p_route, n_route

        return None

    def _rescue_near_miss_coupled(
        self,
        pair: DifferentialPair,
        spec: CoupledSegmentSpec,
        pathfinder: CoupledPathfinder,
    ) -> tuple[Route, Route] | None:
        """Complete a budget-exited coupled search that stalled near goal.

        Issue #3508: reconstructs the partial coupled route up to the
        search's best state (``pathfinder.last_best_node``) and routes
        the two remaining head->pad tails with the single-ended per-net
        router.  Returns ``(p_route, n_route)`` with the tails merged
        in, or ``None`` when either tail cannot be routed (callers then
        fall through to the legacy budget-exit handling).

        Nothing is committed to the grid here -- the caller runs the
        returned routes through the same #3320 severe-overlap gate and
        commit path as a normally-converged coupled result, so a rescue
        that produced crossing tails is rejected transactionally.
        """
        best = pathfinder.last_best_node
        if best is None:
            return None

        grid = self.autorouter.grid
        p_pos = best.state.p_pos
        n_pos = best.state.n_pos
        p_wx, p_wy = grid.grid_to_world(p_pos.x, p_pos.y)
        n_wx, n_wy = grid.grid_to_world(n_pos.x, n_pos.y)

        p_head = self._virtual_pad_at(spec.p_end, p_wx, p_wy, p_pos.layer)
        n_head = self._virtual_pad_at(spec.n_end, n_wx, n_wy, n_pos.layer)

        try:
            p_route, n_route = pathfinder._reconstruct_coupled_routes(
                best, spec.p_start, p_head, spec.n_start, n_head
            )
        except Exception as exc:  # pragma: no cover - defensive
            print(f"    [coupled-rescue] reconstruction failed for {pair.name}: {exc}")
            return None

        p_tail = self._tail_route(pathfinder, p_head, spec.p_end, p_pos.layer, "P", pair.name)
        if p_tail is None:
            return None
        n_tail = self._tail_route(pathfinder, n_head, spec.n_end, n_pos.layer, "N", pair.name)
        if n_tail is None:
            return None

        p_route.segments.extend(p_tail.segments)
        p_route.vias.extend(p_tail.vias)
        n_route.segments.extend(n_tail.segments)
        n_route.vias.extend(n_tail.vias)
        return p_route, n_route

    def _single_ended_guide_route(
        self,
        start_pad: Pad,
        end_pad: Pad,
        per_net_timeout: float | None = None,
    ) -> Route | None:
        """Route one side of a pair single-ended to seed a corridor mask.

        Issue #3439: the corridor-bounded coupled search needs a
        known-routable spatial path to dilate.  We use the autorouter's
        standard per-net pathfinder (C++-accelerated when available,
        10-100x faster than the pure-Python coupled A*) to find the
        P-side path.  The returned route is NOT committed to the grid
        or the route list -- it exists only to bound the coupled
        search's state space and is discarded afterwards.

        Issue #3473 (review of #3439): the probe is bounded by
        ``per_net_timeout``.  It is only a guide route -- if the
        single-ended path cannot be found quickly, the corridor
        attempt is skipped and the legacy open search gets the budget
        instead.  Without this, a hard P side (C++ search fails ->
        unbounded Python fallback) consumed nearly the whole per-pair
        budget on board 06 BEFORE either coupled attempt ran, leaving
        the open fallback just the 1.0s floor.

        Returns ``None`` when no single-ended path exists within the
        deadline (in which case the caller falls back to the
        unconstrained coupled search) or when the pathfinder raises.
        """
        try:
            return self.autorouter.router.route(start_pad, end_pad, per_net_timeout=per_net_timeout)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "corridor guide route raised for %r -> %r: %s",
                start_pad.net_name,
                end_pad.net_name,
                exc,
            )
            return None
        finally:
            # Issue #3473 (judge note on #3439): the C++ backend's
            # validation-failure path mutates persistent avoidance
            # costs (``_boost_avoidance_at``); normally cleared in
            # ``Autorouter._route_net``, which this probe bypasses.
            # Clear them here so a failed probe cannot leak
            # cost-shaping into subsequent searches.
            router = self.autorouter.router
            if hasattr(router, "clear_avoidance_costs"):
                router.clear_avoidance_costs()

    def route_differential_pair_coupled(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
        coupled_only: bool = False,
        extra_spacing_cells: int = 0,
        per_pair_timeout: float | None = None,
        per_pair_max_iterations: int | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair using coupled pathfinding.

        Routes both P and N traces simultaneously while maintaining
        constant spacing between them.

        Args:
            pair: The differential pair to route.
            spacing: Optional spacing override.
            coupled_only: Issue #2464: When True, do not fall back to
                independent routing if the coupled pathfinder cannot
                handle the pair (e.g., 3-pad nets, no path found).
                Returns ``([], None)`` instead.  Used by the diff-pair
                pre-pass so that pairs that cannot be coupled are left
                for the main strategy to route normally.
            extra_spacing_cells: Issue #3040 Phase B: additional grid
                cells to add to both the target ``spacing_cells`` and
                ``min_spacing_cells`` floor passed to the
                :class:`CoupledPathfinder`.  Used by the Phase B repair
                pass to widen the search's spacing target on retry when
                the first attempt produced an intra-pair clearance
                violation due to grid quantisation.  Each additional
                cell adds one ``grid.resolution`` of edge-to-edge
                separation, which is normally enough to push the
                routed clearance above the per-pair threshold.
                Default ``0`` preserves legacy behaviour.
            per_pair_timeout: Issue #3089: Optional wall-clock budget
                (seconds) passed through to
                :meth:`CoupledPathfinder.route_coupled` for each
                coupled-segment search this pair triggers.  ``None``
                preserves the legacy unbounded behaviour.  When the
                budget is exceeded the coupled search returns ``None``
                and this method falls through to the same
                "coupled routing failed" handler used for genuine
                no-path-found results (independent routing fallback
                when ``coupled_only=False``; ``([], None)`` return
                otherwise), so callers do not need a separate
                code-path for budget exits.
        """
        # Issue #3089: reset the budget-exit flag at the start of each
        # call so callers see only the most-recent invocation's state.
        self._last_pair_budget_exit = False

        if pair.rules is None:
            return [], None

        if spacing is None:
            spacing = pair.rules.spacing

        print(f"\n  Routing differential pair {pair} (coupled mode)")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        # Get pads
        pad_result = self._get_pair_pads(pair)
        if pad_result is None:
            print("    ERROR: Could not find pads for differential pair")
            return [], None

        p_pads, n_pads = pad_result

        # Issue #2473: Pair pads using the MST-based N-pad helper.
        # For 2-pad nets this still produces a single coupled segment
        # with no stubs; for 3+ pad nets (USB-C) it returns one or
        # more coupled segments plus the intra-cluster stub edges that
        # the independent router will handle after the coupled pass.
        if len(p_pads) == 2 and len(n_pads) == 2:
            # Backward-compatible fast path.
            legacy = self._pair_pads_for_coupled_routing(p_pads, n_pads)
            coupled_specs = []
            for ps, pe, ns, ne in legacy:
                # Issue #3012: detect polarity-swap in the 2-pad fast
                # path.  ``_pair_pads_for_coupled_routing`` returns the
                # pair ordered by start-pad proximity, but the *end*
                # pads can still flip polarity (board-07 DDR test
                # footprint inverts P/N row positions between the two
                # QFNs).  Without this detection the coupled search
                # tries to maintain constant spacing on a path whose
                # endpoints sit in mirror orientations -- impossible
                # without a swap-via -- and the search collapses the
                # spacing to 0 mid-run instead.  Mirrors the existing
                # detection in the npad path (line 1424).
                polarity_swap = self._polarity_swap_between(ps, ns, pe, ne)
                coupled_specs.append(
                    CoupledSegmentSpec(
                        p_start=ps,
                        p_end=pe,
                        n_start=ns,
                        n_end=ne,
                        polarity_swap=polarity_swap,
                    )
                )
            stub_specs: list[StubEdgeSpec] = []
        else:
            coupled_specs, stub_specs = self._pair_pads_for_coupled_routing_npad(p_pads, n_pads)

        if not coupled_specs:
            if coupled_only:
                print(
                    "    Skipping diff-pair pre-pass: complex pad configuration "
                    "(coupled pathfinder could not pair pads)"
                )
                return [], None
            print("    WARNING: Complex pad configuration, falling back to independent routing")
            return self.route_differential_pair_independent(pair, spacing)

        # Issue #3012: Calculate the effective spacing.  The legacy
        # behaviour used ``int(spacing / resolution)`` where ``spacing``
        # came from the per-type ``DifferentialPairRules.spacing``
        # default (0.15-0.2 mm) -- which is an EDGE-TO-EDGE clearance,
        # not a CENTER-TO-CENTER target.  When the pair's
        # ``NetClassRouting`` declares a richer ``intra_pair_clearance``
        # (board 07: 0.1 mm with 0.15 mm trace width), we derive the
        # center-to-center floor as ``trace_width + intra_pair_clearance``
        # and feed that as the spacing target so the search lays the
        # centerlines far enough apart for the partner-edge clearance to
        # hold post-route.  Use ``math.ceil`` instead of ``int`` so we
        # never round DOWN below the threshold.
        net_class_map = self.autorouter.net_class_map or {}
        pair_net_class = net_class_map.get(pair.positive.net_name)
        if pair_net_class is not None:
            pair_trace_width = float(pair_net_class.trace_width)
            pair_intra_clearance = float(pair_net_class.effective_intra_pair_clearance())
        else:
            pair_trace_width = float(self.autorouter.rules.trace_width)
            # Without a per-pair class, fall back to the legacy edge-to-
            # edge ``pair.rules.spacing`` interpretation by treating it
            # as the intra clearance.  This preserves the pre-#3012
            # behaviour for callers that don't supply a net_class_map.
            pair_intra_clearance = float(spacing)

        # Issue #4052: clamp the impedance-coupling gap out of the coupled
        # spacing target.  When a net class carries a ``target_diff_impedance``
        # the impedance resolver (``diffpair_impedance.py:524``) OVERWRITES
        # ``intra_pair_clearance`` with the physics-derived edge-to-edge
        # coupling gap needed to hit that impedance on the board's stackup
        # (board 07: 8.425 mm for loosely-coupled 100 ohm on a thick
        # 4-layer stack).  That gap is a *stackup impedance* quantity, NOT
        # a within-pair spacing floor: fed straight into the coupled
        # search's ``min_spacing_cells`` it demands the two centerlines sit
        # ~8.6 mm apart (87 cells at 0.1 mm), so every move from the
        # physical pad pitch (~1 mm) is rejected by the spacing floor and
        # the joint-state search dies at the start state in 4 iterations
        # (measured: ALL board-07 coupled pairs, ``sym_floor`` /
        # ``asym_floor_p`` / ``asym_floor_n`` rejections only,
        # ``best_progress`` never improving).
        #
        # Clamp is gated on ``target_diff_impedance`` being set -- that is
        # the SIGNAL that ``intra_pair_clearance`` was overwritten with a
        # stackup gap (``diffpair_impedance.resolve_impedance_driven_sizing``
        # only replaces the field when a target impedance drove the sizing,
        # ``used_target=True``).  A pair that legitimately DECLARES a wider
        # ``intra_pair_clearance`` (the #3012 case: board 07's 0.1 mm
        # within-pair clearance with a 0.15 mm trace) has no
        # ``target_diff_impedance`` and is left untouched, so its floor
        # still holds the declared within-pair separation post-route.  When
        # gated, clamp to the geometric ``trace_clearance`` floor (the
        # tightest DRC-legal within-pair separation), mirroring the
        # match-group tuner's identical impedance-gap mis-read fix in #3440.
        if getattr(pair_net_class, "target_diff_impedance", None) is not None:
            within_pair_clearance_floor = float(self.autorouter.rules.trace_clearance)
            pair_intra_clearance = min(pair_intra_clearance, within_pair_clearance_floor)

        required_center_spacing = pair_trace_width + pair_intra_clearance
        min_spacing_cells = max(
            1, math.ceil(required_center_spacing / self.autorouter.grid.resolution)
        )

        # Target spacing in grid cells.  Use the larger of the legacy
        # ``spacing/resolution`` value (which historically governed) and
        # the new floor so wider edge-to-edge targets still win when
        # set, and the floor never under-counts.
        legacy_spacing_cells = math.ceil(spacing / self.autorouter.grid.resolution)
        spacing_cells = max(legacy_spacing_cells, min_spacing_cells)

        # Issue #3040 Phase B: widen both the floor and the target by
        # the caller-supplied ``extra_spacing_cells`` on retry attempts.
        # Each additional cell maps to one ``grid.resolution`` of
        # additional center-to-center spacing, which directly translates
        # to that much extra edge-to-edge clearance once the route is
        # quantised back to world coordinates.
        if extra_spacing_cells > 0:
            min_spacing_cells += extra_spacing_cells
            spacing_cells += extra_spacing_cells

        # If any segment requires polarity-swap, enable swap-via moves.
        #
        # Issue #3508: swap-via moves are DISABLED.  The swap move
        # exchanges the two heads' exact grid positions onto one shared
        # new layer, so reconstruction emits the SAME A->B segment for
        # both nets (P: A->B, N: B->A) -- coincident copper, i.e. a
        # short.  Every swap-containing result was therefore rejected
        # by the #3320 severe-overlap gate at exactly
        # ``-trace_width`` (board 06 PCIE/USB3, board 07 DQS -- the
        # "swap-overlap gate" rejection documented in #3473).  With
        # mid-route asymmetric moves now enabled (this issue), a
        # polarity swap is achievable WITHOUT vias: the advancing
        # trace walks a discrete arc around its holding partner
        # (offset-vector rotation through 180 degrees), which the
        # trail-proximity guard keeps clearance-legal.  Re-enable only
        # after the swap reconstruction emits a genuine two-layer
        # crossover (staggered vias, partner segments on different
        # layers).
        any_polarity_swap = any(s.polarity_swap for s in coupled_specs)
        del any_polarity_swap  # documented above; swap moves disabled

        # Create coupled pathfinder.
        # Issue #3508: heuristic_weight > 1 (weighted A*) -- without it
        # the joint-state search floods cost_turn-deep f-plateaus
        # (~90k iterations for ONE 5-point shell on board 06) and no
        # CI-affordable iteration budget converges.  See the
        # ``heuristic_weight`` rationale in ``CoupledPathfinder``.
        #
        # Issue #3547: the weighted-A* search upgrade is gated behind
        # ``enable_shadow_construction``.  Weighting the heuristic changes
        # WHICH joint states the always-running coupled pre-phase explores
        # (goal-ward gradient dominates shell-flooding), so a search that
        # DEFERRED on the pre-#3508 baseline can CONVERGE with the flag
        # off -- committing a route where main deferred re-exposes the
        # gated hazards (#3542 corridor competition, #3544 pre-phase
        # seg-seg violations).  With the shadow constructor disabled
        # (default) fall back to classic optimal A* (``heuristic_weight=
        # 1.0``), the pre-#3508 search behaviour, so a flag-off run keeps
        # recipes on their pre-#3508 budget-exit path.
        coupled_heuristic_weight = (
            COUPLED_HEURISTIC_WEIGHT if self.enable_shadow_construction else 1.0
        )

        # Issue #3547: bound the flag-off classic-A* search so it DEFERS
        # promptly instead of grinding the ``cols * rows * 4`` memory
        # backstop.  With the shadow constructor OFF (default) the search
        # uses ``heuristic_weight=1.0`` (classic optimal A*), which on a
        # deferring fixture explores ~2x the joint states the weighted
        # search (#3508) did, pushing existing flag-off tests to the CI
        # 60s timeout (Judge note on #3547).  The flag-off contract is
        # "may only DEFER", so when the caller plumbed no explicit
        # iteration budget we supply ``COUPLED_FLAGOFF_MAX_ITERATIONS``:
        # fast-converging pairs (boards 03/06's open search) finish well
        # within it, while a deferring pair bails (sets
        # ``last_timeout_exceeded`` -> independent fallback) instead of
        # running the full optimal search to ~60s.  Flag-ON is UNCHANGED:
        # the shadow path keeps whatever budget the caller plumbed (the
        # re-route gate's per-pair budget), so this default never narrows
        # an opt-in run.  An explicit ``per_pair_max_iterations`` (board
        # configs, the re-route gate) always takes precedence.
        #
        # Issue #3921 (investigation): the curation comment proposed
        # raising this flag-off default to a FLOOR so board 06's explicit
        # ``per_pair_max_iterations=2000`` would be lifted to 40000.  That
        # was VERIFIED against the actual seed-42 bench and does NOT
        # restore convergence: at 20000 iters/phase the joint search's
        # best-progress plateaus identically to the 1000-iter run
        # (398->398, 61->64 cells from goal) while wall-time balloons
        # 562s -> >600s.  The reason is the ``heuristic_weight`` note
        # above: classic optimal A* (weight=1.0, the flag-off search)
        # floods cost_turn f-plateaus and "no CI-affordable iteration
        # budget converges".  The historical 6/9 convergence came from the
        # geometric SHADOW CONSTRUCTOR (``enable_shadow_construction=
        # True``), not the joint A* search -- so a budget floor is the
        # wrong lever and was dropped.  See the #3921 PR body for the
        # three-way measurement (floor / weighted / shadow).
        if (
            not self.enable_shadow_construction
            and (per_pair_max_iterations is None or per_pair_max_iterations <= 0)
            and COUPLED_FLAGOFF_MAX_ITERATIONS > 0
        ):
            per_pair_max_iterations = COUPLED_FLAGOFF_MAX_ITERATIONS

        pathfinder = CoupledPathfinder(
            self.autorouter.grid,
            self.autorouter.rules,
            spacing_cells,
            net_class_map=self.autorouter.net_class_map,
            allow_swap_via=False,  # Issue #3508: see rationale above
            min_spacing_cells=min_spacing_cells,
            heuristic_weight=coupled_heuristic_weight,
        )

        routes: list[Route] = []
        p_routes: list[Route] = []
        n_routes: list[Route] = []

        for spec in coupled_specs:
            polarity_marker = " (polarity-swap)" if spec.polarity_swap else ""
            print(
                f"    Routing {pair.positive.net_name}/{pair.negative.net_name}{polarity_marker}..."
            )

            # Issue #3439: corridor-bounded attempt first.  The open
            # joint-state coupled A* is pure Python and intractable on
            # large boards (~14k iterations/min on board 07's 4-layer
            # 110x95mm grid -- every pair blew its 60s budget).  Route
            # the P side single-ended via the (C++-accelerated) per-net
            # pathfinder, dilate its path into a spatial corridor, and
            # run the coupled search restricted to that corridor.  This
            # converts the open 2D product-space search into a near-1D
            # one that completes in seconds.  The guide route is NEVER
            # committed to the grid.  When the corridor attempt fails
            # (no guide path, corridor too tight for two traces, or
            # corridor budget exceeded) we fall back to the legacy
            # unconstrained search with the remaining per-pair budget,
            # preserving behaviour on boards where the open search
            # already converged (boards 03/06).
            spec_t0 = time.monotonic()
            result: tuple[Route, Route] | None = None
            coupled_phase = "open"
            # Issue #3473: iterations the corridor attempt consumed,
            # charged against the shared per-pair iteration budget so
            # the open fallback gets the REMAINDER (not a fresh full
            # budget -- the 4000+4000 double-spend on board 06).
            corridor_iterations_used = 0

            # Issue #3473: bound the probe.  It is only a guide route;
            # give it a small slice of the corridor half-budget (an
            # eighth of the per-pair budget, e.g. 7.5s of 60s).  If
            # the P side cannot be routed single-ended that quickly,
            # skip the corridor and hand the budget to the legacy
            # open search instead of burning it before either coupled
            # attempt runs.
            # Issue #3508: floor the probe budget at 45s (clamped to half
            # the per-pair budget) -- but ONLY when the shadow
            # constructor is enabled.  The #3473 eighth-of-budget bound
            # starved board 06's USB3 probes: their single-ended guide
            # routes need 30-37s (the C++ validation falls back to the
            # Python pathfinder on the J1 fan-out geometry), so at the
            # 60s per-pair budget the probe deadline (7.5s) always
            # fired, no corridor existed, and the USB3 pairs ran the
            # intractable open search only.  The expensive probe exists
            # to feed the shadow guide; with the shadow gated off
            # (default -- see ``enable_shadow_construction``) keep the
            # legacy eighth-of-budget bound so deferring pairs exit
            # quickly and the per-pair wall-clock matches the
            # pre-#3508 budget-exit behaviour (matters on slow 2-core
            # CI runners: 9 pairs x 45s probes is most of the re-route
            # gate's wall-clock budget).
            if per_pair_timeout is None:
                probe_timeout = None
            elif self.enable_shadow_construction:
                # Issue #3987 (unit 2a of #3921): a hard per-pair shadow
                # budget.  When shadow is ON the pair is shadow-or-uncoupled
                # (the joint-state fallback is gated OFF below), so the whole
                # coupled attempt must fit a small budget: cap the P guide
                # probe at ``_SHADOW_PER_PAIR_BUDGET_S`` (clamped to
                # ``per_pair_timeout``).  This bounds the >1200s #3986 tail
                # -- 6/9 failed-shadow pairs previously each burned a ~45s
                # probe before falling through to the flooded A*.
                probe_timeout = min(_SHADOW_PER_PAIR_BUDGET_S, per_pair_timeout)
            else:
                probe_timeout = per_pair_timeout * 0.125
            probe_t0 = time.monotonic()
            guide_route = self._single_ended_guide_route(
                spec.p_start, spec.p_end, per_net_timeout=probe_timeout
            )
            print(
                f"    [corridor-probe] guide_route="
                f"{'ok' if guide_route is not None and guide_route.segments else 'FAILED'} "
                f"elapsed={time.monotonic() - probe_t0:.2f}s "
                f"segments={len(guide_route.segments) if guide_route is not None else 0}"
            )
            # Issue #3508: geometric shadow construction FIRST.  When
            # the guide exists, building N as a validated parallel
            # offset of the guide is deterministic, takes milliseconds,
            # and produces coupled geometry by construction -- the
            # joint-state search below is the fallback for guides the
            # shadow cannot legally parallel (e.g., via-bearing guides
            # or one-sided obstacle walls).
            #
            # OPT-IN (``self.enable_shadow_construction``, default
            # False): the constructor's committed geometry is not yet
            # artifact-quality -- see the field rationale on
            # ``DifferentialPairConfig.enable_shadow_construction``
            # for the board 06 run-4 measurements (stranded shadow
            # tails, via-on-partner intersections, corridor
            # competition stranding later single-ended nets).
            if self.enable_shadow_construction and guide_route is not None and guide_route.segments:
                shadow = self._shadow_route_pair(pair, spec, pathfinder, guide_route, spacing_cells)
                if shadow is not None:
                    result = shadow
                    coupled_phase = "shadow"
                    print("    [coupled-shadow] pair constructed as guide + parallel shadow")

            if (
                result is None
                and self.enable_shadow_construction
                and guide_route is not None
                and guide_route.segments
            ):
                # Issue #3508: role-swapped shadow retry.  P's guide may
                # carry vias or hug a one-sided obstacle wall; the N
                # side's single-ended route can be shadowable when P's
                # is not (board 06 MIPI_D0 / USB2_D: the P guide takes a
                # 2-via detour while the N guide is planar).  Gated on
                # the P probe having SUCCEEDED: when the P side cannot
                # be single-ended-routed within the probe budget at all,
                # the N side (same endpoints geometry) will not be
                # either, and the retry would just burn a second probe
                # budget per deferred pair.
                # Issue #3987: bound the N (swapped) probe by the REMAINDER
                # of the hard per-pair shadow budget so the two probes
                # together cannot exceed ``_SHADOW_PER_PAIR_BUDGET_S`` --
                # the fail-fast contract is per PAIR, not per probe.
                n_probe_timeout = probe_timeout
                if per_pair_timeout is not None and probe_timeout is not None:
                    n_probe_timeout = max(
                        0.5,
                        min(
                            probe_timeout,
                            _SHADOW_PER_PAIR_BUDGET_S - (time.monotonic() - spec_t0),
                        ),
                    )
                n_guide = self._single_ended_guide_route(
                    spec.n_start, spec.n_end, per_net_timeout=n_probe_timeout
                )
                if n_guide is not None and n_guide.segments:
                    shadow = self._shadow_route_pair(
                        pair, spec, pathfinder, n_guide, spacing_cells, swap_roles=True
                    )
                    if shadow is not None:
                        result = shadow
                        coupled_phase = "shadow-swapped"
                        print(
                            "    [coupled-shadow] pair constructed as N guide + parallel P shadow"
                        )

            # Issue #3987 (unit 2a of #3921): when the shadow constructor is
            # ON, a pair is EITHER a validated parallel shadow (ms) OR it is
            # deferred to the uncoupled fallback.  It must NOT fall through
            # to the corridor / open joint-state A* below: those flood the
            # cost_turn f-plateaus (the #3954 bench disproved convergence at
            # 20x iterations) and the 6/9 failed-shadow pairs each burning a
            # corridor budget + the negotiated backstop is exactly the
            # >1200s tail the #3986 board-06 measurements documented.  A hard
            # per-pair shadow budget (``_SHADOW_PER_PAIR_BUDGET_S``) already
            # bounds the corridor probe + shadow construction above; here we
            # fail FAST to the uncoupled fallback -- shadow-or-uncoupled,
            # never shadow-then-flooded-A*.
            shadow_fail_fast = self.enable_shadow_construction
            if (
                result is None
                and not shadow_fail_fast
                and guide_route is not None
                and guide_route.segments
            ):
                grid = self.autorouter.grid
                resolution = grid.resolution
                start_spacing_cells = (
                    math.dist(
                        (spec.p_start.x, spec.p_start.y),
                        (spec.n_start.x, spec.n_start.y),
                    )
                    / resolution
                )
                end_spacing_cells = (
                    math.dist(
                        (spec.p_end.x, spec.p_end.y),
                        (spec.n_end.x, spec.n_end.y),
                    )
                    / resolution
                )
                # The corridor must admit the N trace alongside the
                # guide path at the WIDEST spacing the run will see
                # (start/end pad pitch can exceed the target), plus
                # maneuvering slack for local detours.
                corridor_radius = int(
                    math.ceil(max(spacing_cells, start_spacing_cells, end_spacing_cells))
                ) + max(6, spacing_cells)
                corridor = build_corridor_mask(
                    grid,
                    guide_route,
                    corridor_radius,
                    extra_cells=(
                        grid.world_to_grid(spec.p_start.x, spec.p_start.y),
                        grid.world_to_grid(spec.p_end.x, spec.p_end.y),
                        grid.world_to_grid(spec.n_start.x, spec.n_start.y),
                        grid.world_to_grid(spec.n_end.x, spec.n_end.y),
                    ),
                )
                # Half the per-pair budget for probe + corridor
                # attempt combined; the rest is reserved for the
                # open-search fallback so a corridor pathology can
                # never starve the legacy path entirely.  Issue #3473:
                # the probe's elapsed time is deducted from the
                # corridor half (it already counts against the pair
                # via ``spec_t0``), keeping probe+corridor <= ~50% of
                # the per-pair budget instead of 62.5%.
                corridor_budget: float | None = None
                if per_pair_timeout is not None:
                    corridor_budget = max(
                        0.5,
                        per_pair_timeout * 0.5 - (time.monotonic() - spec_t0),
                    )
                # Issue #3473: split the ITERATION budget the same way
                # as the wall-clock budget -- the corridor attempt gets
                # at most half, so a failing pair cannot spend the full
                # budget twice (4000 corridor + 4000 open on board 06).
                corridor_iteration_budget: int | None = None
                if per_pair_max_iterations is not None and per_pair_max_iterations > 0:
                    corridor_iteration_budget = max(1, per_pair_max_iterations // 2)
                result = pathfinder.route_coupled(
                    spec.p_start,
                    spec.p_end,
                    spec.n_start,
                    spec.n_end,
                    timeout_seconds=corridor_budget,
                    max_iterations_budget=corridor_iteration_budget,
                    corridor=corridor,
                )
                corridor_iterations_used = pathfinder.last_iterations
                if result is not None:
                    coupled_phase = "corridor"

            if result is None and not shadow_fail_fast:
                remaining_budget = per_pair_timeout
                if per_pair_timeout is not None:
                    remaining_budget = max(1.0, per_pair_timeout - (time.monotonic() - spec_t0))
                # Issue #3473: the open fallback gets the REMAINDER of
                # the shared iteration budget.  Because the corridor
                # attempt was capped at half, the fallback always
                # retains at least ~half -- mirroring the wall-clock
                # arithmetic above.
                remaining_iterations = per_pair_max_iterations
                if per_pair_max_iterations is not None and per_pair_max_iterations > 0:
                    remaining_iterations = max(
                        1, per_pair_max_iterations - corridor_iterations_used
                    )
                result = pathfinder.route_coupled(
                    spec.p_start,
                    spec.p_end,
                    spec.n_start,
                    spec.n_end,
                    timeout_seconds=remaining_budget,
                    max_iterations_budget=remaining_iterations,
                )

            spec_elapsed = time.monotonic() - spec_t0
            logger.info(
                "diffpair coupled timing: pair=%r phase=%s elapsed=%.2fs success=%s",
                pair.name,
                coupled_phase,
                spec_elapsed,
                result is not None,
            )
            # Issue #3508: stdout visibility for the per-pair outcome.
            # The board recipes are print-based (INFO logging is not
            # configured), so without this line the only stdout signal
            # for a failing pair is the budget-exceeded warning -- the
            # corridor/open phase split and the iteration cost (the two
            # knobs recipe authors tune) were invisible in CI logs.
            best_state = pathfinder.last_best_state
            print(
                f"    [coupled-timing] phase={coupled_phase} "
                f"elapsed={spec_elapsed:.2f}s "
                f"corridor_iters={corridor_iterations_used} "
                f"last_iters={pathfinder.last_iterations} "
                f"best_progress={pathfinder.last_best_progress} "
                f"best_state={best_state} "
                f"rejections={dict(pathfinder.last_rejections)} "
                f"success={result is not None}"
            )

            # Issue #3508: near-miss rescue.  The weighted corridor-
            # bounded coupled search reliably traverses the route body
            # but stalls in the final pad-landing needle-eye: the heads
            # arrive within a few-hundred-micron Manhattan distance of
            # the goal pads, where interleaved foreign-pad clearance
            # halos leave only one runway per pad, the pair must
            # asymmetrically spread from the coupled spacing back to
            # the goal pad pitch inside that lattice, and the #3078
            # path-history guard turns every runway probe into a
            # dead-end (backing out retraces the head's own trail).
            # Measured on board 06: 8/9 pairs stall at best_progress
            # 5-21 cells after covering 95%+ of the route.  Rather
            # than make the joint search solve the landing, commit the
            # coupled body to the best state and finish each side with
            # the single-ended per-net router, which lands on pads
            # routinely.  The resulting tail (<= ~2 mm of a 30-50 mm
            # route) keeps the coupled-length fraction far above every
            # ``coupled_continuity_threshold`` in use (0.7-0.9).
            # Issue #3547: the near-miss rescue commits a coupled body +
            # single-ended tails for a search that DEFERRED on the
            # pre-#3508 baseline.  Committing where main deferred
            # re-exposes the exact hazards the gate exists to suppress
            # (#3542 corridor competition stranding singles, #3544
            # pre-phase copper seg-seg violations).  Gate the rescue on
            # ``enable_shadow_construction`` so a flag-off run never
            # invokes it -- the pre-phase may only defer, matching the
            # pre-#3508 budget-exit behaviour.
            if (
                self.enable_shadow_construction
                and result is None
                and pathfinder.last_best_node is not None
            ):
                if pathfinder.last_best_progress <= NEAR_MISS_RESCUE_CELLS:
                    rescue = self._rescue_near_miss_coupled(pair, spec, pathfinder)
                    if rescue is not None:
                        result = rescue
                        coupled_phase += "+rescue"
                        print(
                            f"    [coupled-rescue] completed pair via "
                            f"near-miss rescue (progress="
                            f"{pathfinder.last_best_progress} cells)"
                        )

            if result is None:
                # Issue #3089: ``None`` may indicate (a) no path found,
                # (b) max-iterations exhausted, or (c) the new per-pair
                # wall-clock budget was exceeded.  CoupledPathfinder.
                # route_coupled emits a structured ``logger.warning``
                # for case (c) and sets ``last_timeout_exceeded=True``.
                #
                # When the budget fired, do NOT attempt an independent-
                # routing fallback: the per-net A* on the same congested
                # BGA-49 escape geometry is the slowest single-net case
                # in the router (the per-net router has its own internal
                # timeout but it is much larger than the coupled
                # budget) and will blow the whole-run wall-clock budget.
                # Instead, surface a clean "skipped: budget exceeded"
                # diagnostic via the intra-clearance-violation buffer
                # (so Phase B's repair pass still sees a buffer entry)
                # and return ``([], None)`` so the main strategy picks
                # up these nets normally.  This mirrors the AC of
                # #3089: "with at least one pair surfacing a clean
                # 'skipped: budget exceeded' diagnostic and continuing".
                # Issue #3547: the "skip the independent fallback" exit
                # below exists to protect a caller-supplied WALL-CLOCK
                # budget (``per_pair_timeout``): the per-net A* on a
                # congested BGA-escape pair is the slowest single-net
                # case and would blow the whole-run budget.  But when the
                # only budget in force is the flag-off iteration default
                # (``COUPLED_FLAGOFF_MAX_ITERATIONS``, no
                # ``per_pair_timeout``), there is no whole-run wall-clock
                # contract to protect, and the pre-#3508 behaviour on a
                # deferring fixture was to fall through to the independent
                # fallback (the DQS-like polarity-swap test asserts this).
                # So only take the skip-fallback exit when a wall-clock
                # budget was actually plumbed; otherwise let the search
                # DEFER to the independent fallback below.
                if pathfinder.last_timeout_exceeded and per_pair_timeout is not None:
                    # Issue #3921: report WHICH budget actually fired.
                    # ``route_coupled`` raises ``last_timeout_exceeded``
                    # for both the iteration budget and the wall-clock
                    # budget, so the old message hard-coded the
                    # ``per_pair_timeout`` seconds ("budget exceeded
                    # (120s)") even when the iteration budget bailed the
                    # search in 0.3s.  ``last_iteration_limited``
                    # disambiguates; surface the actual iteration count
                    # and the per-phase split so the exit reason is not
                    # opaque.
                    if pathfinder.last_iteration_limited:
                        # ``per_pair_max_iterations`` is the total budget;
                        # the two-phase caller splits it ~half corridor /
                        # half open (see ``corridor_iteration_budget``).
                        total_budget = per_pair_max_iterations
                        phase_budget = (
                            max(1, total_budget // 2)
                            if total_budget is not None and total_budget > 0
                            else None
                        )
                        budget_desc = (
                            f"iteration budget exceeded "
                            f"({pathfinder.last_iterations} iters; "
                            f"phase cap {phase_budget}, total {total_budget}) "
                            f"in {spec_elapsed:.1f}s"
                            if phase_budget is not None
                            else f"iteration budget exceeded "
                            f"({pathfinder.last_iterations} iters) "
                            f"in {spec_elapsed:.1f}s"
                        )
                    else:
                        budget_desc = (
                            f"wall-clock budget exceeded "
                            f"({per_pair_timeout:.0f}s; "
                            f"{pathfinder.last_iterations} iters)"
                        )
                    print(
                        f"    WARNING: Coupled routing {budget_desc}; "
                        "skipping diff-pair and leaving nets for the "
                        "main strategy."
                    )
                    logger.warning(
                        "diffpair coupled-routing budget exceeded: pair=%r "
                        "p_net=%r n_net=%r reason=%s iters=%d "
                        "wall_budget=%.1fs elapsed=%.2fs",
                        pair.name,
                        pair.positive.net_name,
                        pair.negative.net_name,
                        "iteration" if pathfinder.last_iteration_limited else "wall-clock",
                        pathfinder.last_iterations,
                        float(per_pair_timeout) if per_pair_timeout else -1.0,
                        spec_elapsed,
                    )
                    self._last_pair_budget_exit = True
                    return [], None
                if coupled_only:
                    print("    Skipping diff-pair pre-pass: coupled pathfinder found no path")
                    return [], None
                print("    WARNING: Coupled routing failed, falling back to independent routing")
                return self.route_differential_pair_independent(pair, spacing)

            p_route, n_route = result

            # Issue #3320: Pre-mark intra-pair clearance audit.  Before
            # we commit the coupled-route to the grid and the route list,
            # check whether the reconstructed geometry actually meets the
            # per-pair clearance threshold.  A SEVERE violation
            # (centerlines overlap by more than ``trace_width / 2`` --
            # i.e., the partner trace's centerline lies on or inside our
            # trace's body) means the coupled search produced an
            # unrouteable swap-via / crossover geometry that the
            # ``min_spacing_cells`` floor (PR #3022) could not prevent.
            # The canonical failure mode is the board-07 DQS_N/DQS_P
            # pair: the polarity-swap-via at the U1 vias places both
            # traces with swapped y-coordinates on the same inner layer,
            # producing a long diagonal that crosses the partner's start
            # cell with -0.150 mm edge-to-edge clearance (the full trace
            # width).  When this happens we reject the coupled route,
            # do NOT commit it to the grid, and fall back to the
            # independent router which routes P and N as separate
            # single-ended nets -- a worse outcome for skew but a
            # routable one that doesn't produce shorting overlaps.
            violation = find_intra_pair_clearance_violations(
                p_route,
                n_route,
                threshold_mm=pair_intra_clearance,
                pair_name=pair.name,
            )
            # Severity gate: any actual centerline overlap (negative
            # edge-to-edge clearance) is "severe" -- the partner trace's
            # body literally intersects ours.  Pure quantization slack
            # (clearance in ``[0, threshold)``) is logged but kept
            # because the trace-optimizer / serpentine shim can still
            # nudge it into compliance.
            severe_violation = violation is not None and violation.actual_clearance_mm < 0.0
            # Issue #3508 (second pass): the segments-only check above
            # cannot see via-vs-segment / via-vs-via physical overlap
            # (e.g. a crossing-tail via on the partner's inner-layer
            # copper).  Treat those as severe too -- the recipe's 6b
            # repair would otherwise rip one side and de-couple the
            # pair downstream.
            if not severe_violation and self._pair_has_physical_overlap(p_route, n_route):
                print(
                    "    WARNING: Coupled route has via-aware physical "
                    "P/N overlap; rejecting coupled route."
                )
                severe_violation = True
                if violation is None:
                    # Synthesize nothing -- the handler below only needs
                    # the flag; guard its violation-specific logging.
                    pass
            if severe_violation:
                # Issue #3508: ``violation`` may be ``None`` when the
                # rejection came from the via-aware physical-overlap
                # check (no same-layer segment pair under threshold).
                worst = violation.actual_clearance_mm if violation is not None else float("nan")
                print(
                    f"    WARNING: Coupled route produced centerline overlap "
                    f"(worst={worst:+.3f}mm < 0); "
                    "rejecting coupled route and falling back to independent "
                    "routing."
                )
                if violation is not None:
                    logger.warning(
                        "diffpair coupled-route REJECTED due to centerline overlap: "
                        "pair=%r p_net=%r n_net=%r worst_clearance=%.4fmm "
                        "threshold=%.4fmm offending_segments=%d",
                        violation.pair_name,
                        violation.positive_net_name,
                        violation.negative_net_name,
                        violation.actual_clearance_mm,
                        violation.expected_clearance_mm,
                        len(violation.segment_violations),
                    )
                if _COUPLED_TRACE and violation is not None:
                    print(
                        f"      [overlap-debug] layer={violation.layer} "
                        f"p_seg=({violation.p_segment.x1:.2f},"
                        f"{violation.p_segment.y1:.2f})->"
                        f"({violation.p_segment.x2:.2f},{violation.p_segment.y2:.2f}) "
                        f"n_seg=({violation.n_segment.x1:.2f},"
                        f"{violation.n_segment.y1:.2f})->"
                        f"({violation.n_segment.x2:.2f},{violation.n_segment.y2:.2f})"
                    )
                # Do NOT commit p_route/n_route to grid or
                # ``autorouter.routes``.  Fall back to independent
                # routing for the whole pair (single source of truth
                # for the fallback path).  For the n-pad case where
                # earlier specs in this loop may already have committed
                # routes, unmark them and remove from the autorouter's
                # route list so the independent router starts from a
                # clean grid state for this pair.  ``coupled_only``
                # callers short-circuit out without a fallback -- they
                # will see the pair as unrouted and the negotiated
                # strategy picks it up on the main pass.
                for prev_route in routes:
                    with contextlib.suppress(Exception):
                        self.autorouter.grid.unmark_route(prev_route)
                    if prev_route in self.autorouter.routes:
                        self.autorouter.routes.remove(prev_route)
                if coupled_only:
                    print(
                        "    Skipping diff-pair pre-pass: coupled route "
                        "rejected (centerline overlap) and ``coupled_only`` "
                        "is set."
                    )
                    return [], None
                return self.route_differential_pair_independent(pair, spacing)

            # Mark routes on grid (use the unified helper that updates
            # both the Python and C++ grids — issue #1250).
            self.autorouter._mark_route(p_route)
            self.autorouter._mark_route(n_route)
            self.autorouter.routes.append(p_route)
            self.autorouter.routes.append(n_route)

            p_routes.append(p_route)
            n_routes.append(n_route)
            routes.extend([p_route, n_route])

            # Issue #3023 Phase A: per-spec intra-pair clearance audit.
            # The CoupledPathfinder's ``min_spacing_cells`` floor is a
            # center-to-center grid-cell count -- it does NOT guarantee
            # edge-to-edge clearance once the route is quantised back to
            # world coordinates (the 434-violation residual on board 07
            # is this quantisation gap).  Re-check the actual routed
            # segments against the per-pair threshold and emit a
            # diagnostic so Phase B (fine-grid repair, separate PR) can
            # rip-and-replace just the offenders.  No behavioural change
            # here -- detection only.  Note: severe overlaps that would
            # have triggered the #3320 rejection above never reach this
            # diagnostic because they fall back to independent routing.
            if violation is not None:
                logger.info(
                    "diffpair intra-clearance violation: pair=%r "
                    "p_net=%r n_net=%r threshold=%.4fmm "
                    "worst_actual=%.4fmm magnitude=%.4fmm "
                    "layer=%r offending_segments=%d",
                    violation.pair_name,
                    violation.positive_net_name,
                    violation.negative_net_name,
                    violation.expected_clearance_mm,
                    violation.actual_clearance_mm,
                    violation.violation_magnitude_mm,
                    violation.layer,
                    len(violation.segment_violations),
                )
                self._intra_clearance_violations.append(violation)

        # Issue #2473: Route stub edges (intra-net hops within a
        # cluster, e.g., USB-C A6 -> B6) using the independent router.
        # These are short, no coupling required, and the autorouter has
        # better access to obstacle-aware A* than the coupled pathfinder.
        #
        # Issue #3508: failed stub edges are recorded in
        # ``self._last_stub_failed_nets`` so the pre-pass aggregator can
        # leave the affected NET in the main strategy's routable set.
        # Previously the "deferred to main strategy" warning was a lie:
        # the net was still claimed as coupled-routed (#2464 reserve),
        # the negotiated loop skipped it, and the stub hop was never
        # routed (measured: USB2_D+ incomplete -> 18/21 reach; the
        # committed-artifact solution for A6->B6 is a ~12mm
        # under-connector wrap on In1.Cu that only the main strategy's
        # full A* can produce).
        self._last_stub_failed_nets: set[int] = set()
        if stub_specs:
            stub_routes = self._route_stub_edges(stub_specs)
            expected_per_net = collections.Counter(s.start.net for s in stub_specs)
            routed_per_net = collections.Counter(r.net for r in stub_routes)
            for stub_net, expected_count in expected_per_net.items():
                if routed_per_net.get(stub_net, 0) < expected_count:
                    self._last_stub_failed_nets.add(stub_net)
            for r in stub_routes:
                if r.net == pair.positive.net_id:
                    p_routes.append(r)
                elif r.net == pair.negative.net_id:
                    n_routes.append(r)
                routes.append(r)

        # Issue #3540: transactional pad-connectivity claim.  The shadow
        # constructor (and its rescue-tail / stub-edge machinery) can
        # commit copper that fails to actually REACH a goal pad -- a
        # parallel-offset tail that exhausts its anchor-stepping budget,
        # or a stub edge that the independent router could not land --
        # while the per-spec commit above has already marked that copper
        # on the grid.  Left as-is the caller claims the pair's nets
        # (#2464 reserve), the negotiated main strategy skips them, and
        # the stranded pads are unreachable for the rest of the pipeline
        # (measured board 06 run-4: USB3_RX1+/USB3_RX2+ shipped "1 of 2
        # pads stranded" with no warning).  A pair that claims-but-strands
        # costs REACH; a pair that defers cleanly costs only QUALITY --
        # and reach is the asserted contract.  So before returning the
        # pair's routes (which is what the caller turns into a net claim),
        # verify every pad of BOTH nets is in a single connected component
        # of the committed copper.  On any gap, rip the pair's copper off
        # the grid + route list and defer the WHOLE pair: return
        # ``([], None)`` (the caller never claims) or fall through to the
        # single-ended independent router (``coupled_only=False``).
        #
        # Gated on ``enable_shadow_construction`` so a flag-off run -- whose
        # contract is "may only defer, never commit where main would
        # defer" -- is behaviourally unchanged: with the flag off the
        # shadow/rescue paths are inert, so the committed routes here are
        # the coupled body that already passed the #3320 severe-overlap
        # gate, and re-deferring a clean body would needlessly lose reach.
        if self.enable_shadow_construction and routes:
            net_pads_for_check: dict[int, list[Pad]] = {}
            for net_id in (pair.positive.net_id, pair.negative.net_id):
                pad_keys = self.autorouter.nets.get(net_id, [])
                net_pads_for_check[net_id] = [
                    self.autorouter.pads[k] for k in pad_keys if k in self.autorouter.pads
                ]
            conn = validate_net_connectivity(routes, net_pads_for_check)
            stranded_nets = [
                net_id for net_id, info in conn.items() if not info.get("connected", False)
            ]
            if stranded_nets:
                for net_id in stranded_nets:
                    info = conn[net_id]
                    print(
                        f"    WARNING: [coupled-shadow] pair {pair.name} net "
                        f"{net_id} stranded "
                        f"({info.get('connected_pads', 0)}/"
                        f"{info.get('total_pads', 0)} pads reached); "
                        "ripping pair copper and deferring the whole pair."
                    )
                    logger.warning(
                        "diffpair shadow claim NOT transactional -- rolling back: "
                        "pair=%r net=%r connected_pads=%d total_pads=%d",
                        pair.name,
                        net_id,
                        info.get("connected_pads", 0),
                        info.get("total_pads", 0),
                    )
                # Roll back: unmark every committed route for this pair and
                # drop it from the autorouter's route list, leaving a clean
                # grid for whichever fallback handles the pair next.  No
                # net is claimed because we return without the pair's
                # routes.
                for committed in routes:
                    with contextlib.suppress(Exception):
                        self.autorouter.grid.unmark_route(committed)
                    if committed in self.autorouter.routes:
                        self.autorouter.routes.remove(committed)
                if coupled_only:
                    print(
                        "    Skipping diff-pair pre-pass: shadow-constructed "
                        "pair stranded goal pads (transactional rollback)."
                    )
                    return [], None
                return self.route_differential_pair_independent(pair, spacing)

        # Calculate lengths
        p_length = calculate_route_length(p_routes)
        n_length = calculate_route_length(n_routes)
        pair.routed_length_p = p_length
        pair.routed_length_n = n_length

        print(f"      P length: {p_length:.3f}mm")
        print(f"      N length: {n_length:.3f}mm")

        # Check and apply length matching
        delta = pair.length_delta
        warning = None

        if delta > pair.rules.max_length_delta:
            # Issue #3003: gate the inline serpentine shim on
            # ``length_critical=True``.  The intent is that length
            # matching for length-critical pairs is performed by the
            # audited Phase 3I tuner (``tune_diff_pair_skew``), which
            # already runs an outer-normal bulge + post-insertion DRC
            # self-check.  For pairs that are NOT length_critical the
            # shim used to bulge blindly into the partner trace,
            # producing ``diffpair_clearance_intra`` violations on
            # tightly-spaced pairs.
            #
            # Look up the per-pair ``NetClassRouting`` via the autorouter's
            # ``net_class_map`` (keyed by positive net name -- both halves
            # share the same class).  When no class is configured (the
            # synthetic-test case) we default to length_critical=True so
            # the legacy code path remains exercised, but we still pass
            # ``intra_pair_clearance_mm`` so the bulge is partner-aware.
            net_class_map = getattr(self.autorouter, "net_class_map", None) or {}
            net_class = net_class_map.get(pair.positive.net_name)
            if net_class is not None:
                length_critical = bool(net_class.length_critical)
                intra_clearance = net_class.effective_intra_pair_clearance()
            else:
                length_critical = True
                intra_clearance = self.autorouter.rules.trace_clearance

            if not length_critical:
                print(
                    f"    Length mismatch: {delta:.3f}mm; "
                    f"net class {pair.positive.net_name!r} is NOT length_critical, "
                    "skipping inline serpentine (Phase 3I tuner will handle "
                    "this pair if --length-match-diffpairs is enabled)."
                )
            else:
                print(f"    Length mismatch: {delta:.3f}mm, attempting serpentine...")

                # Try to add serpentine to shorter route
                if p_routes and n_routes:
                    matched = match_pair_lengths(
                        p_routes[0],
                        n_routes[0],
                        pair.rules.max_length_delta,
                        add_serpentines=True,
                        intra_pair_clearance_mm=intra_clearance,
                        # Issue #3508: grid-validate the bulges (foreign
                        # copper, partner vias, pad halos) -- see
                        # ``create_serpentine``.
                        grid=self.autorouter.grid,
                    )

                    if matched:
                        # Issue #3508: the serpentine mutated a route
                        # AFTER it was marked on the grid (the commit at
                        # the top of this method), so the grid does not
                        # know about the bulge copper -- the negotiated
                        # main strategy then routes other nets straight
                        # through it (measured: 106 seg-seg violations
                        # across 10 nets on the first #3508 re-route).
                        # Re-mark the cells (idempotent; bookkeeping
                        # like ``grid.routes``/R-tree insertion is NOT
                        # repeated -- the route object is already
                        # registered, only its cell envelope changed).
                        self._remark_route_cells(p_routes[0])
                        self._remark_route_cells(n_routes[0])
                        # Recalculate lengths
                        p_length = calculate_route_length(p_routes)
                        n_length = calculate_route_length(n_routes)
                        pair.routed_length_p = p_length
                        pair.routed_length_n = n_length
                        delta = pair.length_delta
                        print(f"    After serpentine: delta={delta:.3f}mm")
                    else:
                        print(
                            "    Serpentine rejected (no suitable segment OR "
                            "would violate intra-pair clearance); falling through "
                            "to length-mismatch warning."
                        )

        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def route_differential_pair_independent(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair with independent routing (fallback).

        Routes P and N traces separately using the standard router.
        """
        if pair.rules is None:
            return [], None

        if spacing is None:
            spacing = pair.rules.spacing

        routes: list[Route] = []
        print(f"\n  Routing differential pair {pair} (independent mode)")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        print(f"    Routing {pair.positive.net_name} (P)...")
        p_routes = self.autorouter.route_net(p_net_id)
        routes.extend(p_routes)

        p_length = calculate_route_length(p_routes)
        pair.routed_length_p = p_length
        print(f"      Length: {p_length:.3f}mm")

        print(f"    Routing {pair.negative.net_name} (N)...")
        n_routes = self.autorouter.route_net(n_net_id)
        routes.extend(n_routes)

        n_length = calculate_route_length(n_routes)
        pair.routed_length_n = n_length
        print(f"      Length: {n_length:.3f}mm")

        delta = pair.length_delta
        warning = None
        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def intra_clearance_violations(self) -> list[IntraPairClearanceViolation]:
        """Return routed intra-pair clearance violations (Issue #3023 Phase A).

        Returns the rolling buffer of violations recorded by
        :meth:`route_differential_pair_coupled` since this
        :class:`DiffPairRouter` was constructed.  Each entry corresponds
        to one ``CoupledPathfinder``-routed (P, N) pair whose post-route
        edge-to-edge clearance dropped below the per-pair
        ``NetClassRouting.effective_intra_pair_clearance()``.

        Phase A is detection-only -- this method exists so Phase B (the
        fine-grid sub-pass, separate PR) and external tooling (DRC
        reports, e2e tests on board 07) can audit how many violations
        the coupled router emitted without re-running the geometry
        check.

        Returns:
            A shallow copy of the violation buffer.  Empty when no
            coupled diff-pair routes have been laid down or when every
            pair satisfied its per-pair clearance threshold.  Callers
            MUST NOT mutate the returned list to clear state; use
            :meth:`reset_intra_clearance_violations` instead.
        """
        return list(self._intra_clearance_violations)

    def reset_intra_clearance_violations(self) -> None:
        """Discard the buffered Phase A clearance-violation records.

        Used by tests that exercise multiple
        ``route_differential_pair_coupled`` calls on a single
        :class:`DiffPairRouter` instance and want a clean baseline
        between cases.  Not intended for production callers; the buffer
        is intentionally additive over a single Autorouter session so
        the post-routing audit sees every coupled pair.
        """
        self._intra_clearance_violations.clear()

    def _route_pair_on_fine_grid(
        self,
        pair: DifferentialPair,
        spacing_override: float | None,
        extra_spacing_cells: int,
        per_pair_timeout: float | None,
        resolution_factor: float = 0.5,
    ) -> tuple[list[Route], object | None]:
        """Issue #3115 Phase B fine-grid sub-pass: re-route a pair on a finer grid.

        Builds a bbox-scoped routing grid whose resolution is
        ``resolution_factor x main_grid.resolution`` (default half-pitch),
        re-marks the obstacles/pads/foreign routes the main grid carries
        in that bounding box, then runs :class:`CoupledPathfinder` against
        the fine grid.  The resulting routes are returned in world
        coordinates so the caller can mark them on the main grid.

        Targets the angle-#1 root cause flagged in Issue #3115: grid
        quantisation of asymmetric escape stubs prevents the main-grid
        ``extra_spacing_cells`` retry from producing equal-length P/N
        landings.  A finer grid resolution gives the coupled search
        sub-cell-aware moves so the partner-aware exit cell can land on
        an evenly-clearance position without changing the corridor A*
        algorithm.

        Args:
            pair: The differential pair to re-route.
            spacing_override: Optional spacing override (from
                ``diffpair_config.spacing``); ``None`` uses the pair's
                own rules.
            extra_spacing_cells: Additional grid cells of spacing
                widening, same semantics as
                :meth:`route_differential_pair_coupled`.  At the
                fine-grid resolution one cell is half as wide as at the
                main-grid resolution, so a value of e.g. ``2`` on a
                half-pitch fine grid only widens by ``1 x main cell``.
                The caller should compensate by passing a larger
                value when re-using the main-grid floor.
            per_pair_timeout: Wall-clock budget forwarded to
                :meth:`CoupledPathfinder.route_coupled`.
            resolution_factor: Fine-grid resolution multiplier.  Default
                ``0.5`` (half-pitch).

        Returns:
            ``(routes, warning)`` matching the legacy
            :meth:`route_differential_pair_coupled` return shape.
            ``([], None)`` if pads cannot be resolved, the fine-grid
            bounding box is degenerate, or the coupled search returns
            no path.  Successful routes are NOT marked on either grid
            by this helper; the caller is responsible for the
            ``autorouter._mark_route()`` + ``autorouter.routes.append()``
            handoff so post-route bookkeeping (intra-clearance audit,
            length matching) stays consistent with the main-grid path.
        """
        from .grid import RoutingGrid
        from .rules import DesignRules

        if pair.rules is None:
            return [], None

        # Resolve the pads we need to route between.
        pad_result = self._get_pair_pads(pair)
        if pad_result is None:
            return [], None
        p_pads, n_pads = pad_result
        if not p_pads or not n_pads:
            return [], None

        main_grid = self.autorouter.grid
        fine_resolution = max(
            main_grid.resolution * resolution_factor,
            # Don't go below 0.01mm; below that the pathfinder cost
            # explodes faster than the resolution helps geometry.
            0.01,
        )

        # Compute bounding box covering the pair's pads with a margin
        # equal to the main-grid spacing target so the search has room
        # to maneuver around adjacent obstacles.
        all_xs = [p.x for p in p_pads + n_pads]
        all_ys = [p.y for p in p_pads + n_pads]
        margin = max(
            2.0,  # at least 2mm of breathing room
            6.0 * main_grid.resolution,  # or six main-grid cells
        )
        bbox_min_x = min(all_xs) - margin
        bbox_min_y = min(all_ys) - margin
        bbox_max_x = max(all_xs) + margin
        bbox_max_y = max(all_ys) + margin

        # Clamp to the main grid's footprint so we don't run off the board.
        bbox_min_x = max(bbox_min_x, main_grid.origin_x)
        bbox_min_y = max(bbox_min_y, main_grid.origin_y)
        bbox_max_x = min(bbox_max_x, main_grid.origin_x + main_grid.width)
        bbox_max_y = min(bbox_max_y, main_grid.origin_y + main_grid.height)

        fine_width = bbox_max_x - bbox_min_x
        fine_height = bbox_max_y - bbox_min_y

        if fine_width <= 0 or fine_height <= 0:
            # Degenerate bounding box; nothing to route on.
            return [], None

        # Safety check on grid size: a half-pitch grid quadruples the
        # cell count, so cap the fine-grid size to avoid pathological
        # memory use on large pairs (e.g., edge-to-edge mini-PCIe).
        # The main-grid fine-grid pass in ``core.py:11652`` uses
        # 16M cells; we use a tighter 4M cap because this is per-pair
        # and runs inside a per-pair timeout.
        num_layers = main_grid.num_layers
        estimated_cells = (
            (fine_width / fine_resolution) * (fine_height / fine_resolution) * num_layers
        )
        max_fine_cells = 4_000_000
        if estimated_cells > max_fine_cells:
            scale = (estimated_cells / max_fine_cells) ** 0.5
            fine_resolution = fine_resolution * scale
            logger.info(
                "Phase B fine-grid: scaling resolution up to %.4fmm to fit %d-cell cap (pair=%r)",
                fine_resolution,
                max_fine_cells,
                pair.name,
            )

        # Build a fresh design rules object that mirrors the main rules
        # but uses the fine resolution.  Mirrors the pattern at
        # ``core.py:11673``.
        main_rules = self.autorouter.rules
        fine_rules = DesignRules(
            grid_resolution=fine_resolution,
            trace_width=main_rules.trace_width,
            trace_clearance=main_rules.trace_clearance,
            via_drill=main_rules.via_drill,
            via_diameter=main_rules.via_diameter,
            via_clearance=main_rules.via_clearance,
            manufacturer=main_rules.manufacturer,
        )

        fine_grid = RoutingGrid(
            width=fine_width,
            height=fine_height,
            rules=fine_rules,
            origin_x=bbox_min_x,
            origin_y=bbox_min_y,
            layer_stack=main_grid.layer_stack,
            resolution_override=fine_resolution,
        )

        # Mirror autorouter pads onto the fine grid so the coupled
        # search sees the same obstacle field.  This includes BOTH the
        # pair's own pads (their cells must be reachable for the same
        # net) and other nets' pads in the bounding box (must be
        # blocked).
        pitches = self.autorouter.component_pitches
        pad_refs_in_pair = {(p.ref, p.pin) for p in p_pads + n_pads}
        # Add the pair's own pads first so net ownership is correct.
        for pad in p_pads + n_pads:
            fine_grid.add_pad(pad, pin_pitch=pitches.get(pad.ref))
        # Add foreign pads that fall in the bounding box.
        for (ref, pin), pad in self.autorouter.pads.items():
            if (ref, pin) in pad_refs_in_pair:
                continue
            if bbox_min_x <= pad.x <= bbox_max_x and bbox_min_y <= pad.y <= bbox_max_y:
                fine_grid.add_pad(pad, pin_pitch=pitches.get(pad.ref))

        # Re-mark all currently-committed routes (foreign nets) on the
        # fine grid so the coupled search avoids them.  The pair's own
        # routes were already ripped up by the caller before invoking
        # this helper.
        pair_p_net, pair_n_net = pair.get_net_ids()
        for route in self.autorouter.routes:
            if route.net == pair_p_net or route.net == pair_n_net:
                continue
            fine_grid.mark_route(route)

        # Compute the same center-to-center spacing the main path uses
        # at line 2095-2140, but in fine-grid cells.
        if spacing_override is None:
            spacing = pair.rules.spacing
        else:
            spacing = spacing_override

        net_class_map = self.autorouter.net_class_map or {}
        pair_net_class = net_class_map.get(pair.positive.net_name)
        if pair_net_class is not None:
            pair_trace_width = float(pair_net_class.trace_width)
            pair_intra_clearance = float(pair_net_class.effective_intra_pair_clearance())
        else:
            pair_trace_width = float(self.autorouter.rules.trace_width)
            pair_intra_clearance = float(spacing)

        required_center_spacing = pair_trace_width + pair_intra_clearance
        min_spacing_cells = max(1, math.ceil(required_center_spacing / fine_resolution))
        legacy_spacing_cells = math.ceil(spacing / fine_resolution)
        spacing_cells = max(legacy_spacing_cells, min_spacing_cells)

        if extra_spacing_cells > 0:
            min_spacing_cells += extra_spacing_cells
            spacing_cells += extra_spacing_cells

        # Build the fine-grid coupled pathfinder.
        pathfinder = CoupledPathfinder(
            fine_grid,
            fine_rules,
            spacing_cells,
            net_class_map=self.autorouter.net_class_map,
            allow_swap_via=False,  # synthetic asymmetric-pad case rarely needs it
            min_spacing_cells=min_spacing_cells,
        )

        # Pair the pads with the same MST/legacy logic as
        # route_differential_pair_coupled so the spec ordering
        # matches what the main-grid path would produce.
        if len(p_pads) == 2 and len(n_pads) == 2:
            legacy_specs = self._pair_pads_for_coupled_routing(p_pads, n_pads)
            specs = []
            for ps, pe, ns, ne in legacy_specs:
                specs.append((ps, pe, ns, ne))
        else:
            coupled_specs, _stub_specs = self._pair_pads_for_coupled_routing_npad(p_pads, n_pads)
            specs = [(s.p_start, s.p_end, s.n_start, s.n_end) for s in coupled_specs]

        if not specs:
            return [], None

        produced_routes: list[Route] = []
        for ps, pe, ns, ne in specs:
            result = pathfinder.route_coupled(ps, pe, ns, ne, timeout_seconds=per_pair_timeout)
            if result is None:
                # Failed on at least one spec -- abandon the fine-grid
                # attempt entirely (the caller will try the next angle
                # or restore the original routes).
                return [], None
            p_route, n_route = result
            produced_routes.append(p_route)
            produced_routes.append(n_route)

        return produced_routes, None

    def repair_intra_clearance_violations(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        max_retries_per_pair: int = 2,
        enable_fine_grid_pass: bool = True,
    ) -> int:
        """Issue #3040 Phase B: rip-up and retry pairs with intra-clearance violations.

        For each pair recorded in ``self._intra_clearance_violations``
        (Phase A detection), this method:

          1. Removes the offending P/N routes from the autorouter's
             route list and unmarks them from the grid.
          2. Re-invokes :meth:`route_differential_pair_coupled` with a
             progressively wider ``extra_spacing_cells`` (1 cell on
             attempt 1, 2 cells on attempt 2) so the
             :class:`CoupledPathfinder` lays the centerlines further
             apart -- enough additional spacing to recover the
             edge-to-edge clearance lost to grid quantisation in the
             first attempt.
          3. Issue #3115: when the main-grid retries fail and
             ``enable_fine_grid_pass`` is True, runs a fine-grid
             sub-pass (half the main resolution, scoped to the pair's
             bounding box) that re-routes the pair against the
             quantisation-sensitive escape geometry the wider-spacing
             retries cannot fix.  Targets the angle-#1 root cause of
             asymmetric pad heights producing unequal escape stubs
             that the main grid pitch cannot equalise.
          4. Re-checks the new pair via
             ``find_intra_pair_clearance_violations`` and accepts the
             retry only if the violation is resolved.  If every retry
             (main-grid and fine-grid) still violates (or the
             pathfinder finds no path), the original routes are
             restored and the pair remains flagged for the
             :func:`~kicad_tools.router.io.validate_routes` safety net.

        Args:
            diffpair_config: The same configuration used by the original
                ``route_all_with_diffpairs`` call (so per-pair rules and
                spacing carry over).  May be ``None`` if no special
                configuration is in effect.
            max_retries_per_pair: Hard cap on main-grid retry attempts
                per pair to prevent infinite loops on pathologically
                tight escapes.  Default ``2``; each attempt widens
                spacing by one additional grid cell over the prior
                attempt.  The optional fine-grid sub-pass is in
                addition to these main-grid attempts.
            enable_fine_grid_pass: Issue #3115: when True (default),
                perform a fine-grid sub-pass after the main-grid retries
                exhaust.  Set False to retain the legacy Phase B
                behaviour (main-grid retries only) for tests that pin
                that contract.

        Returns:
            The number of pairs whose violation was resolved by the
            repair pass.  ``0`` means either no violations were
            present, or every retry failed to find a compliant route.
        """
        violations = list(self._intra_clearance_violations)
        if not violations:
            return 0

        # Build a lookup from net names back to the DifferentialPair
        # objects so we can re-invoke routing.  Filter to engaged pairs
        # so we don't accidentally re-route a pair that the engagement
        # gate refused.
        diff_pairs_with_source = self.detect_differential_pairs_with_source()
        all_pairs = [p for p, _ in diff_pairs_with_source]

        # Apply diffpair_config rules so the retry uses the same rules
        # as the original pass.
        if diffpair_config is not None and diffpair_config.enabled:
            for pair in all_pairs:
                if pair.rules is not None:
                    pair.rules = diffpair_config.get_rules(pair.pair_type)

        pair_by_net: dict[str, DifferentialPair] = {}
        for pair in all_pairs:
            pair_by_net[pair.positive.net_name] = pair
            pair_by_net[pair.negative.net_name] = pair

        # Group violations by pair (same pair may appear multiple times
        # if there were multiple coupled specs).  Use the pair's positive
        # net name as the stable key.
        violations_by_pair: dict[str, list[IntraPairClearanceViolation]] = {}
        for v in violations:
            key = v.positive_net_name
            violations_by_pair.setdefault(key, []).append(v)

        resolved_pairs = 0

        for p_net_name, pair_violations in violations_by_pair.items():
            pair = pair_by_net.get(p_net_name)
            if pair is None:
                logger.warning(
                    "Phase B repair: cannot find DifferentialPair for net %r; "
                    "leaving violation in place for validate_routes() safety net.",
                    p_net_name,
                )
                continue

            p_id, n_id = pair.get_net_ids()
            n_net_name = pair_violations[0].negative_net_name

            # Issue #3508: defensive pair-identity check.  The lookup
            # above re-runs pair DETECTION, which can disagree with the
            # pairing the violation was recorded against (observed on
            # board 06: a violation recorded for USB3_RX1+/USB3_RX1-
            # resolved to a DifferentialPair object whose negative side
            # was USB3_TX1-).  Re-routing such a cross-pair would rip
            # up and re-couple nets from two DIFFERENT pairs.  Skip and
            # leave the violation for the validate_routes() safety net.
            if {pair.positive.net_name, pair.negative.net_name} != {
                p_net_name,
                n_net_name,
            }:
                logger.warning(
                    "Phase B repair: detection re-paired %r with %r but the "
                    "violation was recorded against %r/%r; skipping repair "
                    "for this pair.",
                    pair.positive.net_name,
                    pair.negative.net_name,
                    p_net_name,
                    n_net_name,
                )
                continue

            # Snapshot current routes for this pair so we can either
            # rip them up cleanly or restore them on failure.
            current_p_routes = [r for r in self.autorouter.routes if r.net == p_id]
            current_n_routes = [r for r in self.autorouter.routes if r.net == n_id]

            if not current_p_routes and not current_n_routes:
                # Nothing to repair (pair must have been ripped up
                # already by some other repair pass).
                continue

            print(
                f"\n  Phase B repair: {p_net_name}/{n_net_name} "
                f"({len(pair_violations)} violation(s), retrying with wider spacing)"
            )

            # Rip up the original routes.
            for route in list(current_p_routes):
                self.autorouter.grid.unmark_route(route)
                if route in self.autorouter.routes:
                    self.autorouter.routes.remove(route)
            for route in list(current_n_routes):
                self.autorouter.grid.unmark_route(route)
                if route in self.autorouter.routes:
                    self.autorouter.routes.remove(route)

            # Remember which violations correspond to this pair so we
            # can prune them from the buffer on success.
            ids_to_remove = {id(v) for v in pair_violations}

            # Bounded retry loop with progressively wider spacing.
            retry_succeeded = False
            spacing_override = (
                diffpair_config.spacing
                if diffpair_config is not None and diffpair_config.enabled
                else None
            )

            # Snapshot the violation count BEFORE the retry so we can
            # detect new violations introduced by this attempt.
            len(self._intra_clearance_violations)

            for attempt in range(1, max_retries_per_pair + 1):
                # Clear violations from prior retry on this pair so the
                # new attempt's audit is the only entry we examine.
                # We restore unrelated entries below.
                snapshot = list(self._intra_clearance_violations)
                # Keep all violations that are NOT from this pair.
                self._intra_clearance_violations = [
                    v for v in snapshot if v.positive_net_name != p_net_name
                ]

                print(
                    f"    Phase B attempt {attempt}/{max_retries_per_pair}: "
                    f"extra_spacing_cells={attempt}"
                )
                # Issue #3089: forward a tightened per-pair wall-clock
                # budget so Phase B retries cannot stall the run with
                # the same BGA-49-escape pathology that triggered the
                # first-pass budget exit.  Phase B retries are
                # known-likely-to-fail when the violation persists
                # across attempts; we cap each retry at half the
                # configured budget so the worst-case repair-loop
                # cost is bounded at
                # ``violating_pairs * max_retries_per_pair * (budget / 2)``.
                phase_b_timeout: float | None = None
                if diffpair_config is not None and diffpair_config.per_pair_timeout:
                    phase_b_timeout = max(2.0, diffpair_config.per_pair_timeout / 2.0)
                retry_routes, _retry_warning = self.route_differential_pair_coupled(
                    pair,
                    spacing=spacing_override,
                    coupled_only=True,
                    extra_spacing_cells=attempt,
                    per_pair_timeout=phase_b_timeout,
                )

                # Capture any new violations the audit recorded for this
                # pair during the retry.
                new_violations_for_pair = [
                    v for v in self._intra_clearance_violations if v.positive_net_name == p_net_name
                ]

                if retry_routes and not new_violations_for_pair:
                    # Retry succeeded and no new violations.
                    print(
                        f"    Phase B succeeded: {p_net_name}/{n_net_name} "
                        f"clean after {attempt} attempt(s)."
                    )
                    retry_succeeded = True
                    # Mark the resolved violations for removal.
                    for v in pair_violations:
                        ids_to_remove.add(id(v))
                    break

                # Retry produced no path or still violates -- rip the
                # retry routes up and try again (or fall through).
                retry_p_routes = [r for r in self.autorouter.routes if r.net == p_id]
                retry_n_routes = [r for r in self.autorouter.routes if r.net == n_id]
                for route in retry_p_routes:
                    self.autorouter.grid.unmark_route(route)
                    if route in self.autorouter.routes:
                        self.autorouter.routes.remove(route)
                for route in retry_n_routes:
                    self.autorouter.grid.unmark_route(route)
                    if route in self.autorouter.routes:
                        self.autorouter.routes.remove(route)

            # Issue #3115 Phase B fine-grid sub-pass: when the main-grid
            # ``extra_spacing_cells`` retries have all failed, give the
            # pair one last chance on a half-pitch grid.  This targets
            # the asymmetric-pad-escape pathology where the main-grid
            # quantisation forces unequal P/N stubs that the
            # wider-spacing search cannot equalise.
            if not retry_succeeded and enable_fine_grid_pass:
                # Clear out this pair's violations from prior attempts
                # so the new audit is the only signal we look at.
                self._intra_clearance_violations = [
                    v for v in self._intra_clearance_violations if v.positive_net_name != p_net_name
                ]

                fine_grid_timeout: float | None = None
                if diffpair_config is not None and diffpair_config.per_pair_timeout:
                    # Reuse the half-budget cap from the main-grid
                    # retries; the fine grid is more expensive but
                    # the bbox is narrowly scoped.
                    fine_grid_timeout = max(2.0, diffpair_config.per_pair_timeout / 2.0)

                print(
                    f"    Phase B fine-grid sub-pass: {p_net_name}/{n_net_name} on half-pitch grid"
                )
                try:
                    fine_routes, _fine_warning = self._route_pair_on_fine_grid(
                        pair,
                        spacing_override=spacing_override,
                        # Modest widening on the fine grid: one main-grid
                        # cell == two fine-grid cells; carry one fine
                        # cell of extra spacing.
                        extra_spacing_cells=1,
                        per_pair_timeout=fine_grid_timeout,
                        resolution_factor=0.5,
                    )
                except Exception as e:
                    # The fine-grid sub-pass is best-effort.  If it
                    # raises (cell-count cap, grid-init failure, etc.)
                    # we fall through to the original-route restore
                    # path so the board state stays consistent.
                    logger.warning(
                        "Phase B fine-grid sub-pass raised an unexpected "
                        "exception (pair=%r): %s; falling back to "
                        "main-grid violation state.",
                        pair.name,
                        e,
                    )
                    fine_routes = []

                if fine_routes:
                    # Audit the fine-grid routes against the same
                    # threshold the original detector used so we don't
                    # accept routes that are STILL in violation.
                    fine_p_routes = [r for r in fine_routes if r.net == p_id]
                    fine_n_routes = [r for r in fine_routes if r.net == n_id]
                    if fine_p_routes and fine_n_routes:
                        # Use the first (longest) p/n routes for the
                        # detector -- matches the 2-pad fast path.
                        fine_violation = find_intra_pair_clearance_violations(
                            fine_p_routes[0],
                            fine_n_routes[0],
                            threshold_mm=pair_violations[0].expected_clearance_mm,
                            pair_name=pair.name,
                        )
                        if fine_violation is None:
                            # Clean!  Mark on the main grid and accept.
                            for route in fine_routes:
                                self.autorouter._mark_route(route)
                                self.autorouter.routes.append(route)
                            print(
                                f"    Phase B fine-grid sub-pass succeeded: "
                                f"{p_net_name}/{n_net_name} clean."
                            )
                            retry_succeeded = True
                            for v in pair_violations:
                                ids_to_remove.add(id(v))
                        else:
                            print(
                                f"    Phase B fine-grid sub-pass still violates: "
                                f"actual={fine_violation.actual_clearance_mm:.4f}mm "
                                f"threshold={fine_violation.expected_clearance_mm:.4f}mm"
                            )

            if retry_succeeded:
                resolved_pairs += 1
                # Remove all original (and replaced) violations for
                # this pair from the buffer.  The retry's audit will
                # have already inserted the new (clean) state.
                self._intra_clearance_violations = [
                    v for v in self._intra_clearance_violations if id(v) not in ids_to_remove
                ]
            else:
                # Restore the original routes so the board is no worse
                # off than before (and the violation remains in the
                # buffer for the validate_routes() safety net).
                logger.warning(
                    "Phase B repair: %s/%s still violates after %d attempt(s); "
                    "restoring original routes and leaving violation for "
                    "validate_routes() safety net.",
                    p_net_name,
                    n_net_name,
                    max_retries_per_pair,
                )
                print(
                    f"    Phase B failed: {p_net_name}/{n_net_name} still violates "
                    f"after {max_retries_per_pair} attempt(s); restoring original routes."
                )
                for route in current_p_routes:
                    self.autorouter._mark_route(route)
                    self.autorouter.routes.append(route)
                for route in current_n_routes:
                    self.autorouter._mark_route(route)
                    self.autorouter.routes.append(route)
                # Restore the original violation records for this pair
                # if the retry attempts removed them.
                for v in pair_violations:
                    if v not in self._intra_clearance_violations:
                        self._intra_clearance_violations.append(v)

        if resolved_pairs:
            print(
                f"\n  Phase B repair complete: {resolved_pairs}/"
                f"{len(violations_by_pair)} pair(s) repaired."
            )

        return resolved_pairs

    def route_differential_pair(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
        use_coupled_routing: bool = True,
        per_pair_timeout: float | None = None,
        per_pair_max_iterations: int | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair.

        Args:
            pair: The differential pair to route
            spacing: Override spacing (uses pair rules if None)
            use_coupled_routing: If True, use coupled A* routing.
                                If False, use independent routing.
            per_pair_timeout: Issue #3089: Optional per-pair wall-clock
                budget (seconds) forwarded to
                :meth:`route_differential_pair_coupled` when
                ``use_coupled_routing`` is ``True``.  Ignored when the
                independent fallback runs.
            per_pair_max_iterations: Issue #3144: Optional per-pair
                iteration budget forwarded the same way; see
                :class:`DifferentialPairConfig` for rationale.

        Returns:
            Tuple of (routes, warning) where warning is set if
            length matching failed.
        """
        if use_coupled_routing:
            return self.route_differential_pair_coupled(
                pair,
                spacing,
                per_pair_timeout=per_pair_timeout,
                per_pair_max_iterations=per_pair_max_iterations,
            )
        else:
            return self.route_differential_pair_independent(pair, spacing)

    def route_diffpair_prepass(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning], set[int]]:
        """Route only the differential pairs, leaving other nets to a follow-up strategy.

        Issue #2464: This is a pre-pass that the main routing strategies
        (negotiated, monte-carlo, evolutionary) can run before their normal
        flow.  Diff-pair traces are routed via the CoupledPathfinder and
        marked on the grid, after which the main strategy routes the
        remaining nets.

        Args:
            diffpair_config: Configuration for diff-pair routing.  If None
                or ``enabled`` is False, this method is a no-op.

        Returns:
            ``(routes, warnings, diff_net_ids)`` where:
              - ``routes`` is the list of routes produced for the diff pairs.
              - ``warnings`` is the list of length-mismatch warnings.
              - ``diff_net_ids`` is the set of net IDs that were successfully
                routed (and should therefore be skipped by the follow-up
                strategy).
        """
        if diffpair_config is None or not diffpair_config.enabled:
            return [], [], set()

        diff_pairs_with_source = self.detect_differential_pairs_with_source()
        diff_pairs = [p for p, _ in diff_pairs_with_source]
        if not diff_pairs:
            print("  No differential pairs detected")
            return [], [], set()

        print("\n=== Differential Pair Pre-Pass (Issue #2464) ===")
        print(f"  Detected {len(diff_pairs)} differential pairs:")
        for pair, source in diff_pairs_with_source:
            msg = f"    - {pair}: {pair.pair_type.value} (source: {source})"
            print(msg)
            logger.info("[diffpair-pre-pass] %s", msg.strip())

        for pair in diff_pairs:
            if pair.rules is not None:
                pair.rules = diffpair_config.get_rules(pair.pair_type)

        all_routes: list[Route] = []
        warnings: list[LengthMismatchWarning] = []
        routed_net_ids: set[int] = set()

        for pair in diff_pairs:
            p_id, n_id = pair.get_net_ids()
            # Issue #2638, Epic #2556 Phase 2E: engagement gate.  When the
            # pair's net class has not opted in via ``coupled_routing=True``,
            # or when the pair is single-ended-by-spec (USB-C CC1/CC2,
            # SBU1/SBU2 — the #2527 lesson), refuse coupled routing and
            # let the pair fall through to the main strategy.
            engaged, reason = self._resolve_engagement(pair)
            if not engaged:
                msg = f"[diffpair-engage] refused {pair}: {reason}"
                print(f"  {msg}")
                logger.info(msg)
                continue
            # Issue #2464: Use coupled_only=True so that the pre-pass is a
            # no-op for pairs that the CoupledPathfinder cannot handle.
            # Those pairs are left for the main strategy (negotiated/MC/GA)
            # to route in its normal flow, which avoids producing partial
            # routes that the main strategy would then refuse to complete.
            pair_routes, warning = self.route_differential_pair_coupled(
                pair,
                diffpair_config.spacing,
                coupled_only=True,
            )
            if pair_routes:
                routed_for_net: dict[int, int] = {}
                for r in pair_routes:
                    routed_for_net[r.net] = routed_for_net.get(r.net, 0) + 1
                if routed_for_net.get(p_id, 0) > 0 and routed_for_net.get(n_id, 0) > 0:
                    routed_net_ids.add(p_id)
                    routed_net_ids.add(n_id)
                    # Issue #3508: see route_all_with_diffpairs -- a net
                    # with a failed stub edge stays routable for the
                    # main strategy.
                    for incomplete in getattr(self, "_last_stub_failed_nets", set()) & {p_id, n_id}:
                        routed_net_ids.discard(incomplete)

            all_routes.extend(pair_routes)
            if warning:
                warnings.append(warning)

        unrouted_pairs = [p for p in diff_pairs if p.get_net_ids()[0] not in routed_net_ids]
        if all_routes:
            print(
                f"  Diff-pair pre-pass produced {len(all_routes)} routes "
                f"covering {len(routed_net_ids)} nets"
            )
        if unrouted_pairs:
            print(f"  Diff pairs falling through to main strategy: {len(unrouted_pairs)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings, routed_net_ids

    def route_all_with_diffpairs(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        net_order: list[int] | None = None,
        non_diffpair_strategy: object = None,
        coupled_only: bool = False,
        per_pair_timeout: float | None = None,
        per_pair_max_iterations: int | None = None,
        aggregate_timeout: float | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning]]:
        """Route all nets with differential pair-aware routing.

        Differential pairs are routed first (they're most constrained),
        then remaining nets are routed using the standard router.

        Args:
            diffpair_config: Configuration for diff-pair routing.
            net_order: Optional explicit net ordering (basic strategy only).
            non_diffpair_strategy: Optional callable that routes non-diff-pair
                nets.  When provided (Issue #2464), the callable is invoked
                after the diff-pair pass and is expected to return a list of
                routes for the remaining nets.  When None, falls back to
                per-net basic routing via :meth:`Autorouter.route_net`.
            coupled_only: Issue #2464: When True, the diff-pair pass only
                produces routes for pairs that the CoupledPathfinder can
                handle; pairs with unsupported pad configurations (e.g.,
                3-pad nets) are deferred to the main strategy.  When
                False (default), preserves the legacy fall-back to
                independent routing.
            per_pair_timeout: Issue #3089: Optional per-pair wall-clock
                budget (seconds) for the inner
                :meth:`CoupledPathfinder.route_coupled` A*.  Forwarded
                through :meth:`route_differential_pair_coupled` (and the
                two-arg ``route_differential_pair`` indirection) so
                callers like ``boards/06-diffpair-test/generate_design.py``
                can bound any single coupled search and fall through to
                independent routing for pairs whose BGA-49 escape (USB3
                SS on board 06's J3/J4) would otherwise consume the
                whole CI budget.  Takes precedence over
                ``DifferentialPairConfig.per_pair_timeout`` when both
                are supplied; ``None`` defers to the config value, and
                if that is also ``None`` the legacy unbounded
                behaviour is preserved.
            aggregate_timeout: Issue #3439: Optional wall-clock budget
                (seconds) for the ENTIRE coupled diff-pair phase.  Once
                exhausted, all remaining pairs are deferred to the main
                strategy WITHOUT attempting coupled routing (the same
                budget-exit path as per-pair exits), so a board full of
                pathological pairs can never burn
                ``num_pairs * per_pair_timeout`` of the outer routing
                budget before the single-ended fallback runs -- the
                board-07 7/31-reach collapse.  Per-pair budgets are
                additionally clamped to the remaining aggregate budget.
                Takes precedence over
                ``DifferentialPairConfig.aggregate_timeout``; ``None``
                defers to the config value, and if that is also
                ``None`` the legacy per-pair-only behaviour is
                preserved.
        """
        # Issue #3089: prefer the explicit kwarg, otherwise fall back to
        # the config field so callers configuring everything via
        # ``DifferentialPairConfig(per_pair_timeout=60.0)`` work without
        # also having to pass the kwarg.
        effective_per_pair_timeout = per_pair_timeout
        if effective_per_pair_timeout is None and diffpair_config is not None:
            effective_per_pair_timeout = diffpair_config.per_pair_timeout
        # Issue #3144: same precedence pattern for the iteration budget.
        effective_per_pair_max_iterations = per_pair_max_iterations
        if effective_per_pair_max_iterations is None and diffpair_config is not None:
            effective_per_pair_max_iterations = diffpair_config.per_pair_max_iterations
        # Issue #3439: same precedence pattern for the aggregate budget.
        effective_aggregate_timeout = aggregate_timeout
        if effective_aggregate_timeout is None and diffpair_config is not None:
            effective_aggregate_timeout = getattr(diffpair_config, "aggregate_timeout", None)
        # Issue #3508: thread the shadow-constructor opt-in through to
        # ``route_differential_pair_coupled`` (instance attribute --
        # the coupled entry point has no config handle).
        if diffpair_config is not None:
            self.enable_shadow_construction = bool(
                getattr(diffpair_config, "enable_shadow_construction", False)
            )
        if diffpair_config is None or not diffpair_config.enabled:
            return self.autorouter.route_all(net_order), []

        print("\n=== Differential Pair Routing ===")

        diff_pairs_with_source = self.detect_differential_pairs_with_source()
        diff_pairs = [p for p, _ in diff_pairs_with_source]
        diff_net_ids: set[int] = set()

        if diff_pairs:
            print(f"  Detected {len(diff_pairs)} differential pairs:")
            for pair, source in diff_pairs_with_source:
                msg = f"    - {pair}: {pair.pair_type.value} (source: {source})"
                print(msg)
                logger.info("[diffpair-routing] %s", msg.strip())
                p_id, n_id = pair.get_net_ids()
                diff_net_ids.add(p_id)
                diff_net_ids.add(n_id)
        else:
            print("  No differential pairs detected")
            return self.autorouter.route_all(net_order), []

        for pair in diff_pairs:
            if pair.rules is not None:
                pair.rules = diffpair_config.get_rules(pair.pair_type)

        print("\n--- Routing differential pairs first (most constrained) ---")
        all_routes: list[Route] = []
        warnings: list[LengthMismatchWarning] = []
        # Track diff-pair nets that we successfully routed so the
        # caller can decide which nets to leave for the main strategy.
        coupled_routed_nets: set[int] = set()

        refused_diff_nets: set[int] = set()
        # Issue #3089: track diff-pair nets whose coupled search hit the
        # ``per_pair_timeout`` budget.  Same handling as refused-engagement
        # nets: drop them from ``diff_net_ids`` so the main strategy picks
        # them up normally (the per-net C++ A* router is the right tool
        # for any single net the coupled search couldn't converge on).
        budget_exit_diff_nets: set[int] = set()
        # Issue #3439: aggregate coupled-phase deadline.  When set, the
        # whole pair loop must finish by this time; pairs that would
        # start after the deadline (or with <0.5s of budget left) are
        # deferred to the main strategy via the budget-exit path so a
        # failed coupled pre-pass can never starve the single-ended
        # fallback of wall-clock budget (the board-07 7/31 collapse).
        coupled_phase_deadline: float | None = None
        if effective_aggregate_timeout is not None and effective_aggregate_timeout > 0:
            coupled_phase_deadline = time.monotonic() + float(effective_aggregate_timeout)
        aggregate_deferred_pairs = 0
        for pair in diff_pairs:
            p_id, n_id = pair.get_net_ids()
            # Issue #2638, Epic #2556 Phase 2E: engagement gate.  Refuse
            # coupled routing when the pair's net class has not opted in
            # (default ``coupled_routing=False``) or when the pair is
            # single-ended-by-spec (#2527 lesson).  Refused pairs fall
            # through to the main strategy (their net IDs are removed
            # from ``diff_net_ids`` below so the main strategy picks
            # them up normally).
            engaged, reason = self._resolve_engagement(pair)
            if not engaged:
                msg = f"[diffpair-engage] refused {pair}: {reason}"
                print(f"  {msg}")
                logger.info(msg)
                refused_diff_nets.add(p_id)
                refused_diff_nets.add(n_id)
                continue
            # Issue #3439: aggregate budget check + per-pair clamp.
            pair_timeout = effective_per_pair_timeout
            if coupled_phase_deadline is not None:
                aggregate_remaining = coupled_phase_deadline - time.monotonic()
                if aggregate_remaining <= 0.5:
                    if aggregate_deferred_pairs == 0:
                        logger.warning(
                            "DIFFPAIR_AGGREGATE_BUDGET_EXCEEDED: coupled "
                            "diff-pair phase consumed its %.1fs aggregate "
                            "budget; deferring remaining pairs to the main "
                            "strategy (issue #3439)",
                            float(effective_aggregate_timeout),
                        )
                    print(
                        f"  [diffpair-aggregate] budget exhausted; deferring "
                        f"{pair} to main strategy"
                    )
                    budget_exit_diff_nets.add(p_id)
                    budget_exit_diff_nets.add(n_id)
                    aggregate_deferred_pairs += 1
                    continue
                pair_timeout = (
                    min(pair_timeout, aggregate_remaining)
                    if pair_timeout is not None
                    else aggregate_remaining
                )
            if coupled_only:
                pair_routes, warning = self.route_differential_pair_coupled(
                    pair,
                    diffpair_config.spacing,
                    coupled_only=True,
                    per_pair_timeout=pair_timeout,
                    per_pair_max_iterations=effective_per_pair_max_iterations,
                )
            else:
                pair_routes, warning = self.route_differential_pair(
                    pair,
                    diffpair_config.spacing,
                    use_coupled_routing=True,  # Use coupled routing by default
                    per_pair_timeout=pair_timeout,
                    per_pair_max_iterations=effective_per_pair_max_iterations,
                )
            # Issue #3089: detect budget-exit via the pair's last_budget_exit
            # flag (set by route_differential_pair_coupled when the inner
            # CoupledPathfinder's last_timeout_exceeded fired and we
            # returned [], None to skip the slow independent fallback).
            # This is more direct than inferring from pair_routes being
            # empty (which could also mean engagement refusal or
            # independent fallback that found nothing).
            if not pair_routes and self._last_pair_budget_exit:
                budget_exit_diff_nets.add(p_id)
                budget_exit_diff_nets.add(n_id)
            if pair_routes:
                routed_for_net: dict[int, int] = {}
                for r in pair_routes:
                    routed_for_net[r.net] = routed_for_net.get(r.net, 0) + 1
                if routed_for_net.get(p_id, 0) > 0 and routed_for_net.get(n_id, 0) > 0:
                    coupled_routed_nets.add(p_id)
                    coupled_routed_nets.add(n_id)
                    # Issue #3508: a net whose intra-cluster stub edge
                    # failed is INCOMPLETE -- leave it routable so the
                    # main strategy can finish it (its committed coupled
                    # copper stays on the grid; the negotiated router
                    # connects the remaining pad through/around it).
                    stub_failed = getattr(self, "_last_stub_failed_nets", set())
                    for incomplete in stub_failed & {p_id, n_id}:
                        coupled_routed_nets.discard(incomplete)
                        print(
                            f"    [diffpair-stub] net {incomplete} has an "
                            f"unrouted stub edge; returning it to the main "
                            f"strategy (issue #3508)"
                        )
            all_routes.extend(pair_routes)
            if warning:
                warnings.append(warning)

        if aggregate_deferred_pairs:
            print(
                f"  [diffpair-aggregate] deferred {aggregate_deferred_pairs} "
                f"pair(s) to main strategy after the aggregate coupled-phase "
                f"budget ({effective_aggregate_timeout:.1f}s) was exhausted"
            )

        # Issue #2464: When coupled_only=True, only the nets actually
        # routed by the CoupledPathfinder are reserved.  Pairs that fell
        # through (e.g., 3-pad nets) remain in the routable set so the
        # main strategy can pick them up.
        if coupled_only:
            diff_net_ids = coupled_routed_nets
        else:
            if refused_diff_nets:
                # Issue #2638 Phase 2E: engagement-refused pairs produced no
                # routes here; drop their nets from ``diff_net_ids`` so the
                # main strategy routes them normally.
                diff_net_ids = diff_net_ids - refused_diff_nets
            if budget_exit_diff_nets:
                # Issue #3089: coupled-routing-budget-exit pairs also
                # produced no routes; drop their nets from
                # ``diff_net_ids`` so the main strategy's per-net A*
                # (C++-accelerated) routes them normally.  Without this
                # the budget-exit nets would be excluded from the
                # main strategy AND have no coupled routes, leaving
                # them unrouted in the final PCB.
                # Issue #3473 (cosmetic): aggregate-deferred pairs are
                # also in ``budget_exit_diff_nets`` but already have
                # their own "[diffpair-aggregate] deferred N pair(s)"
                # print above; report only the genuinely per-pair
                # budget exits under the per-pair label.
                per_pair_deferred = max(
                    0, len(budget_exit_diff_nets) // 2 - aggregate_deferred_pairs
                )
                if per_pair_deferred:
                    print(
                        f"  Diff pairs deferred to main strategy due to "
                        f"per-pair budget: {per_pair_deferred}"
                    )
                diff_net_ids = diff_net_ids - budget_exit_diff_nets

        # Issue #3270: Surface budget-exit diff-pair nets to the
        # Autorouter so ``_get_net_priority`` can promote them to the
        # head of the non-diff main strategy's net order.  Without this
        # the budget-exit pair lands last and routes against a heavily
        # colonised grid; on board 06 seed=42 USB3_TX1+/U2.B2 then
        # bursts the per-net timeout (60s observed vs 30s budget) and
        # exhausts the strategy wall-clock before reaching MIPI_RST.
        # The set is cleared after the strategy returns to keep the
        # promotion local to this invocation.
        self.autorouter._budget_exit_diff_nets = set(budget_exit_diff_nets)

        non_diff_nets = [n for n in self.autorouter.nets if n not in diff_net_ids and n != 0]
        if non_diff_nets:
            print(f"\n--- Routing {len(non_diff_nets)} non-differential nets ---")
            if non_diffpair_strategy is not None:
                # Issue #2464: Delegate non-diff-pair routing to the caller's
                # strategy (negotiated, MC, GA, etc.).  The callable is
                # responsible for routing every net in self.autorouter.nets;
                # diff-pair nets are filtered by the caller's net selection
                # since their pads are already marked as routed on the grid.
                try:
                    strategy_routes = non_diffpair_strategy()
                finally:
                    # Issue #3270: Clear the budget-exit promotion set so
                    # subsequent ``route_all`` / ``route_all_negotiated``
                    # invocations on the same autorouter inherit the
                    # default priority ordering (no leak across calls).
                    self.autorouter._budget_exit_diff_nets = set()
                # Filter out any routes for diff-pair nets that the strategy
                # may have re-routed (shouldn't happen if grid marking is
                # correct, but defend against it).
                for r in strategy_routes:
                    if r.net not in diff_net_ids:
                        all_routes.append(r)
            else:
                if net_order:
                    non_diff_order = [n for n in net_order if n in non_diff_nets]
                else:
                    non_diff_order = sorted(
                        non_diff_nets, key=lambda n: self.autorouter._get_net_priority(n)
                    )

                try:
                    for net in non_diff_order:
                        routes = self.autorouter.route_net(net)
                        all_routes.extend(routes)
                        if routes:
                            print(
                                f"  Net {net}: {len(routes)} routes, "
                                f"{sum(len(r.segments) for r in routes)} segments"
                            )
                finally:
                    # Issue #3270: clear the promotion set on the
                    # legacy per-net path too -- the priority lift
                    # is meaningful only for this strategy invocation.
                    self.autorouter._budget_exit_diff_nets = set()

        print("\n=== Differential Pair Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Differential pair nets: {len(diff_net_ids)}")
        print(f"  Other nets: {len(non_diff_nets)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings
