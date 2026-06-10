"""Differential pair routing integration for the autorouter.

This module provides differential pair-aware routing functionality
that coordinates differential pair routing with the main autorouter.

Key features:
- Coupled A* pathfinding that routes both traces simultaneously
- Maintains constant spacing between P/N traces
- Length matching with serpentine compensation
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .core import Autorouter
    from .grid import RoutingGrid
    from .rules import DesignRules, NetClassRouting

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
from .path import calculate_route_length
from .primitives import Pad, Route, Segment, Via

logger = logging.getLogger(__name__)


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
        # Issue #3089: set when the most-recent ``route_coupled`` call
        # exited early due to ``timeout_seconds`` being exceeded.
        # Callers (``route_differential_pair_coupled``) read this to
        # distinguish a budget-exit (where the slow per-net independent
        # fallback would also blow the budget) from a true "no path
        # found" exit (where independent routing is still worth trying).
        # Reset to ``False`` at the start of every ``route_coupled``
        # invocation.
        self.last_timeout_exceeded: bool = False

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

        # Orthogonal moves only for differential pairs (diagonal moves
        # would complicate spacing maintenance)
        self.directions = [
            (1, 0),  # Right
            (-1, 0),  # Left
            (0, 1),  # Down
            (0, -1),  # Up
        ]

    def _is_cell_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if a cell is blocked for this net."""
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return True
        if layer < 0 or layer >= self.grid.num_layers:
            return True

        cell = self.grid.grid[layer][gy][gx]
        if cell.blocked:
            if cell.is_obstacle or cell.net != net:
                return True
        return False

    def _is_trace_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if placing a trace at this position would conflict."""
        for dy in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
            for dx in range(-self._trace_half_width_cells, self._trace_half_width_cells + 1):
                if self._is_cell_blocked(gx + dx, gy + dy, layer, net):
                    return True
        return False

    def _is_via_blocked(self, gx: int, gy: int, net: int) -> bool:
        """Check if placing a via at this position would conflict on any layer."""
        for layer in range(self.grid.num_layers):
            for dy in range(-self._via_half_cells, self._via_half_cells + 1):
                for dx in range(-self._via_half_cells, self._via_half_cells + 1):
                    if self._is_cell_blocked(gx + dx, gy + dy, layer, net):
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
        p_visited: frozenset[tuple[int, int, int]] | None = None,
        n_visited: frozenset[tuple[int, int, int]] | None = None,
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
                continue
            if not n_is_endpoint and self._is_trace_blocked(new_n.x, new_n.y, new_n.layer, n_net):
                continue

            # Calculate spacing between new positions
            spacing_dx = new_p.x - new_n.x
            spacing_dy = new_p.y - new_n.y
            new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)

            # Only accept moves that maintain target spacing (within tolerance).
            # Issue #2473: When the search is in the "approach" phase
            # near the goal pads, allow wider spacing variation so
            # mismatched source/sink pad pitches can converge.
            tolerance = 1 if not approach_relaxed else max(1, target_spacing_cells)
            if abs(new_spacing - target_spacing_cells) > tolerance:
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
                continue

            # Calculate cost
            new_direction = (dx, dy)
            cost = self.rules.cost_straight

            # Add turn penalty if direction changed
            if state.direction != (0, 0) and state.direction != new_direction:
                cost += self.rules.cost_turn

            new_state = CoupledState(new_p, new_n, new_direction)
            neighbors.append((new_state, cost, False))

        # Issue #2490: Asymmetric "converge" moves during approach
        # phase only.  When start and goal pad pitches differ
        # (e.g., USB device-side: MCU 0.8mm pitch -> USB-C 0.5mm
        # pitch), the symmetric step moves above preserve spacing
        # exactly, so the search can never land both traces on
        # endpoint cells whose pitch is narrower than the start
        # pitch.  Within the approach radius, allow one trace to
        # advance toward its goal while the other holds, which
        # closes the spacing one cell at a time.  Restricted to
        # the approach phase so the bulk of the run still
        # maintains constant spacing.
        if approach_relaxed and p_goal is not None and n_goal is not None:
            for dx, dy in self.directions:
                # P advances, N holds.
                cand_p = GridPos(state.p_pos.x + dx, state.p_pos.y + dy, state.p_pos.layer)
                cand_n = state.n_pos
                p_is_endpoint = self._is_at_goal(cand_p, p_goal) or self._is_at_goal(
                    cand_p, p_start
                )
                if p_is_endpoint or not self._is_trace_blocked(
                    cand_p.x, cand_p.y, cand_p.layer, p_net
                ):
                    spacing_dx = cand_p.x - cand_n.x
                    spacing_dy = cand_p.y - cand_n.y
                    new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
                    tolerance = max(1, target_spacing_cells)
                    if abs(new_spacing - target_spacing_cells) <= tolerance:
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
                            pass  # reject this candidate
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
                            pass  # reject this candidate
                        else:
                            # Direction tracking only reflects P's motion;
                            # tag with the new direction so the cost-of-turn
                            # logic still fires when the path bends.
                            cost = self.rules.cost_straight
                            if state.direction != (0, 0) and state.direction != (dx, dy):
                                cost += self.rules.cost_turn
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
                    continue
                spacing_dx = cand_p2.x - cand_n2.x
                spacing_dy = cand_p2.y - cand_n2.y
                new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
                tolerance = max(1, target_spacing_cells)
                if abs(new_spacing - target_spacing_cells) > tolerance:
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
                    continue
                cost = self.rules.cost_straight
                if state.direction != (0, 0) and state.direction != (dx, dy):
                    cost += self.rules.cost_turn
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
        effective_target_spacing = self.target_spacing_cells
        if actual_start_spacing > effective_target_spacing:
            effective_target_spacing = int(round(actual_start_spacing))

        # Issue #2490: Size the approach radius to accommodate the
        # full pitch transition between start and goal pads.  When
        # start and end pad pitches differ (USB device-side: MCU
        # 0.8mm pitch vs USB-C 0.5mm pitch), the legacy
        # ``max(target, 6)`` radius can be smaller than the
        # number of single-cell spacing reductions required to
        # converge, leaving the search no room to relax spacing
        # without exceeding the per-step tolerance.  Scale the
        # radius with the absolute spacing difference plus a small
        # buffer so each cell of the approach can drop spacing by
        # at most one cell.
        spacing_delta = int(round(abs(actual_start_spacing - actual_end_spacing)))
        effective_approach_radius = max(effective_target_spacing, 6, spacing_delta * 2 + 4)

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

        start_h = self._heuristic(start_state, p_goal_pos, n_goal_pos)
        start_node = CoupledNode(start_h, 0.0, start_state, seq=next(seq_counter))
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

            # Goal check - both traces must reach their goals
            p_at_goal = (
                current.state.p_pos.x == p_goal_pos.x and current.state.p_pos.y == p_goal_pos.y
            )
            n_at_goal = (
                current.state.n_pos.x == n_goal_pos.x and current.state.n_pos.y == n_goal_pos.y
            )

            if p_at_goal and n_at_goal:
                return self._reconstruct_coupled_routes(current, p_start, p_end, n_start, n_end)

            # Issue #3078: build path-history sets for the current
            # node by walking its parent chain.  These let
            # ``_get_coupled_neighbors`` reject moves that would put
            # one trace onto a cell the other (or it itself) has
            # already occupied -- the failure mode behind the
            # 36k-violation board 06 regression where asymmetric
            # moves let one trace loop around its partner.
            p_visited_cells: set[tuple[int, int, int]] = set()
            n_visited_cells: set[tuple[int, int, int]] = set()
            walker: CoupledNode | None = current
            while walker is not None:
                p_visited_cells.add(
                    (walker.state.p_pos.x, walker.state.p_pos.y, walker.state.p_pos.layer)
                )
                n_visited_cells.add(
                    (walker.state.n_pos.x, walker.state.n_pos.y, walker.state.n_pos.layer)
                )
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
                p_visited=p_visited_frozen,
                n_visited=n_visited_frozen,
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
                        continue

                neighbor_key = (new_state.p_pos, new_state.n_pos)
                if neighbor_key in closed_set:
                    continue

                new_g = current.g_score + cost

                if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                    g_scores[neighbor_key] = new_g
                    h = self._heuristic(new_state, p_goal_pos, n_goal_pos)
                    f = new_g + h

                    neighbor_node = CoupledNode(
                        f, new_g, new_state, current, is_via, seq=next(seq_counter)
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

    # Calculate serpentine parameters
    # Serpentine adds length = 2 * num_bends * amplitude
    # We want to add length_to_add, so:
    # amplitude = length_to_add / (2 * num_bends)
    # Use 4 bends as default
    num_bends = 4
    amplitude = max(min_amplitude, length_to_add / (2 * num_bends))

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
        )
    else:
        return create_serpentine(
            n_route,
            length_to_add,
            partner_route=p_route if intra_pair_clearance_mm is not None else None,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
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

    def _single_ended_guide_route(self, start_pad: Pad, end_pad: Pad) -> Route | None:
        """Route one side of a pair single-ended to seed a corridor mask.

        Issue #3439: the corridor-bounded coupled search needs a
        known-routable spatial path to dilate.  We use the autorouter's
        standard per-net pathfinder (C++-accelerated when available,
        10-100x faster than the pure-Python coupled A*) to find the
        P-side path.  The returned route is NOT committed to the grid
        or the route list -- it exists only to bound the coupled
        search's state space and is discarded afterwards.

        Returns ``None`` when no single-ended path exists (in which
        case the caller falls back to the unconstrained coupled
        search) or when the pathfinder raises.
        """
        try:
            return self.autorouter.router.route(start_pad, end_pad)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "corridor guide route raised for %r -> %r: %s",
                start_pad.net_name,
                end_pad.net_name,
                exc,
            )
            return None

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
        any_polarity_swap = any(s.polarity_swap for s in coupled_specs)

        # Create coupled pathfinder
        pathfinder = CoupledPathfinder(
            self.autorouter.grid,
            self.autorouter.rules,
            spacing_cells,
            net_class_map=self.autorouter.net_class_map,
            allow_swap_via=any_polarity_swap,
            min_spacing_cells=min_spacing_cells,
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

            guide_route = self._single_ended_guide_route(spec.p_start, spec.p_end)
            if guide_route is not None and guide_route.segments:
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
                # Half the per-pair budget for the corridor attempt;
                # the rest is reserved for the open-search fallback so
                # a corridor pathology can never starve the legacy
                # path entirely.
                corridor_budget = (
                    per_pair_timeout * 0.5 if per_pair_timeout is not None else None
                )
                result = pathfinder.route_coupled(
                    spec.p_start,
                    spec.p_end,
                    spec.n_start,
                    spec.n_end,
                    timeout_seconds=corridor_budget,
                    max_iterations_budget=per_pair_max_iterations,
                    corridor=corridor,
                )
                if result is not None:
                    coupled_phase = "corridor"

            if result is None:
                remaining_budget = per_pair_timeout
                if per_pair_timeout is not None:
                    remaining_budget = max(
                        1.0, per_pair_timeout - (time.monotonic() - spec_t0)
                    )
                result = pathfinder.route_coupled(
                    spec.p_start,
                    spec.p_end,
                    spec.n_start,
                    spec.n_end,
                    timeout_seconds=remaining_budget,
                    max_iterations_budget=per_pair_max_iterations,
                )

            spec_elapsed = time.monotonic() - spec_t0
            logger.info(
                "diffpair coupled timing: pair=%r phase=%s elapsed=%.2fs success=%s",
                pair.name,
                coupled_phase,
                spec_elapsed,
                result is not None,
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
                if pathfinder.last_timeout_exceeded:
                    print(
                        "    WARNING: Coupled routing budget exceeded "
                        f"({per_pair_timeout:.0f}s); skipping diff-pair "
                        "and leaving nets for the main strategy."
                    )
                    logger.warning(
                        "diffpair coupled-routing budget exceeded: pair=%r "
                        "p_net=%r n_net=%r budget=%.1fs",
                        pair.name,
                        pair.positive.net_name,
                        pair.negative.net_name,
                        float(per_pair_timeout) if per_pair_timeout else -1.0,
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
            severe_violation = (
                violation is not None
                and violation.actual_clearance_mm < 0.0
            )
            if severe_violation:
                assert violation is not None  # for type-checkers
                print(
                    f"    WARNING: Coupled route produced centerline overlap "
                    f"(worst={violation.actual_clearance_mm:+.3f}mm < 0); "
                    "rejecting coupled route and falling back to independent "
                    "routing."
                )
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
                    try:
                        self.autorouter.grid.unmark_route(prev_route)
                    except Exception:
                        pass
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
        if stub_specs:
            stub_routes = self._route_stub_edges(stub_specs)
            for r in stub_routes:
                if r.net == pair.positive.net_id:
                    p_routes.append(r)
                elif r.net == pair.negative.net_id:
                    n_routes.append(r)
                routes.append(r)

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
                    )

                    if matched:
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
            pre_retry_count = len(self._intra_clearance_violations)

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
                print(
                    f"  Diff pairs deferred to main strategy due to "
                    f"per-pair budget: {len(budget_exit_diff_nets) // 2}"
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
