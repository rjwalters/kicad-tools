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
import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

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
    """Node for coupled A* priority queue."""

    f_score: float
    g_score: float = field(compare=False)
    state: CoupledState = field(compare=False)
    parent: CoupledNode | None = field(compare=False, default=None)
    via_from_parent: bool = field(compare=False, default=False)


class CoupledPathfinder:
    """A* pathfinder for coupled differential pair routing.

    Routes both P and N traces simultaneously, maintaining constant
    spacing between them throughout the path.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        target_spacing_cells: int,
        net_class_map: dict[str, NetClassRouting] | None = None,
        allow_swap_via: bool = False,
        min_spacing_cells: int = 0,
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
        """
        self.grid = grid
        self.rules = rules
        self.target_spacing_cells = target_spacing_cells
        self.net_class_map = net_class_map or {}
        self.allow_swap_via = allow_swap_via
        # Issue #3012: store the within-pair spacing floor.  ``0`` means
        # no floor (legacy behaviour).
        self.min_spacing_cells = max(0, int(min_spacing_cells))

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

        Returns list of (new_state, cost, is_via) tuples.
        """
        if target_spacing_cells is None:
            target_spacing_cells = self.target_spacing_cells

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
        """Calculate heuristic for coupled A* search."""
        # Manhattan distance for both traces
        p_dist = abs(state.p_pos.x - p_goal.x) + abs(state.p_pos.y - p_goal.y)
        n_dist = abs(state.n_pos.x - n_goal.x) + abs(state.n_pos.y - n_goal.y)

        # Layer change cost if needed
        layer_cost = 0.0
        if state.p_pos.layer != p_goal.layer:
            layer_cost += self.rules.cost_via
        if state.n_pos.layer != n_goal.layer:
            layer_cost += self.rules.cost_via

        return (p_dist + n_dist) * self.rules.cost_straight + layer_cost

    def route_coupled(
        self,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
    ) -> tuple[Route, Route] | None:
        """Route a differential pair with coupled pathfinding.

        Args:
            p_start: Positive trace start pad
            p_end: Positive trace end pad
            n_start: Negative trace start pad
            n_end: Negative trace end pad

        Returns:
            Tuple of (p_route, n_route) or None if routing failed
        """
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

        # A* setup
        open_set: list[CoupledNode] = []
        closed_set: set[tuple[GridPos, GridPos]] = set()
        g_scores: dict[tuple[GridPos, GridPos], float] = {}

        start_h = self._heuristic(start_state, p_goal_pos, n_goal_pos)
        start_node = CoupledNode(start_h, 0.0, start_state)
        heapq.heappush(open_set, start_node)
        g_scores[(p_start_pos, n_start_pos)] = 0.0

        max_iterations = self.grid.cols * self.grid.rows * 4
        iterations = 0

        while open_set and iterations < max_iterations:
            iterations += 1

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
            ):
                neighbor_key = (new_state.p_pos, new_state.n_pos)
                if neighbor_key in closed_set:
                    continue

                new_g = current.g_score + cost

                if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                    g_scores[neighbor_key] = new_g
                    h = self._heuristic(new_state, p_goal_pos, n_goal_pos)
                    f = new_g + h

                    neighbor_node = CoupledNode(f, new_g, new_state, current, is_via)
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

    def route_differential_pair_coupled(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
        coupled_only: bool = False,
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
        """
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

            result = pathfinder.route_coupled(spec.p_start, spec.p_end, spec.n_start, spec.n_end)

            if result is None:
                if coupled_only:
                    print("    Skipping diff-pair pre-pass: coupled pathfinder found no path")
                    return [], None
                print("    WARNING: Coupled routing failed, falling back to independent routing")
                return self.route_differential_pair_independent(pair, spacing)

            p_route, n_route = result

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
            # here -- detection only.
            violation = find_intra_pair_clearance_violations(
                p_route,
                n_route,
                threshold_mm=pair_intra_clearance,
                pair_name=pair.name,
            )
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

    def route_differential_pair(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
        use_coupled_routing: bool = True,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair.

        Args:
            pair: The differential pair to route
            spacing: Override spacing (uses pair rules if None)
            use_coupled_routing: If True, use coupled A* routing.
                                If False, use independent routing.

        Returns:
            Tuple of (routes, warning) where warning is set if
            length matching failed.
        """
        if use_coupled_routing:
            return self.route_differential_pair_coupled(pair, spacing)
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
        """
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
            if coupled_only:
                pair_routes, warning = self.route_differential_pair_coupled(
                    pair,
                    diffpair_config.spacing,
                    coupled_only=True,
                )
            else:
                pair_routes, warning = self.route_differential_pair(
                    pair,
                    diffpair_config.spacing,
                    use_coupled_routing=True,  # Use coupled routing by default
                )
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

        # Issue #2464: When coupled_only=True, only the nets actually
        # routed by the CoupledPathfinder are reserved.  Pairs that fell
        # through (e.g., 3-pad nets) remain in the routable set so the
        # main strategy can pick them up.
        if coupled_only:
            diff_net_ids = coupled_routed_nets
        elif refused_diff_nets:
            # Issue #2638 Phase 2E: engagement-refused pairs produced no
            # routes here; drop their nets from ``diff_net_ids`` so the
            # main strategy routes them normally.  Non-refused pairs
            # remain in ``diff_net_ids`` to preserve the pre-2638 behavior
            # of treating coupled-routing fallbacks (independent routing
            # inside route_differential_pair) as "handled".
            diff_net_ids = diff_net_ids - refused_diff_nets

        non_diff_nets = [n for n in self.autorouter.nets if n not in diff_net_ids and n != 0]
        if non_diff_nets:
            print(f"\n--- Routing {len(non_diff_nets)} non-differential nets ---")
            if non_diffpair_strategy is not None:
                # Issue #2464: Delegate non-diff-pair routing to the caller's
                # strategy (negotiated, MC, GA, etc.).  The callable is
                # responsible for routing every net in self.autorouter.nets;
                # diff-pair nets are filtered by the caller's net selection
                # since their pads are already marked as routed on the grid.
                strategy_routes = non_diffpair_strategy()
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

                for net in non_diff_order:
                    routes = self.autorouter.route_net(net)
                    all_routes.extend(routes)
                    if routes:
                        print(
                            f"  Net {net}: {len(routes)} routes, "
                            f"{sum(len(r.segments) for r in routes)} segments"
                        )

        print("\n=== Differential Pair Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Differential pair nets: {len(diff_net_ids)}")
        print(f"  Other nets: {len(non_diff_nets)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings
