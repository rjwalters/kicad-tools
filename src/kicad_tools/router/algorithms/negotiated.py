"""Negotiated congestion routing algorithm (PathFinder-style).

This module implements iterative rip-up and reroute with increasing
congestion penalties to resolve routing conflicts.

Adaptive parameter tuning (Issue #633) improves convergence by:
- Dynamically adjusting history increment based on progress
- Detecting oscillation patterns to escape local minima
- Adapting present cost factor based on congestion
- Early termination when no progress is being made
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..congestion_estimator import CongestionEstimator
    from ..grid import RoutingGrid
    from ..pathfinder import Router
    from ..primitives import Pad, Route, Via
    from ..rules import DesignRules, NetClassRouting

# Issue #2795: progress callback signature for visibility into long-running
# rip-up operations.  Callback receives a phase string and a metadata dict.
ProgressCallback = Callable[[str, dict], None]


# =========================================================================
# Adaptive Parameter Functions (Issue #633)
# =========================================================================


def calculate_history_increment(
    iteration: int,
    overflow_history: list[int],
    base_increment: float = 0.5,
) -> float:
    """Calculate adaptive history increment based on convergence progress.

    Args:
        iteration: Current iteration number (0-indexed)
        overflow_history: List of overflow values from previous iterations
        base_increment: Base history increment value (default: 0.5)

    Returns:
        Adjusted history increment value

    The increment is increased when:
    - Overflow is increasing (need more penalty to discourage congested areas)
    - Overflow is stagnant (stuck at local minimum, need stronger push)

    The increment is decreased when:
    - Close to convergence (gentle adjustments to avoid overshooting)
    """
    if len(overflow_history) < 2:
        return base_increment

    current = overflow_history[-1]
    previous = overflow_history[-2]

    # If overflow is increasing, be more aggressive with penalties
    if current > previous:
        return base_increment * 1.5

    # If overflow is stagnant (same value), try stronger penalty
    if current == previous:
        # Check for repeated stagnation
        stagnant_count = 1
        for i in range(len(overflow_history) - 2, max(0, len(overflow_history) - 5) - 1, -1):
            if overflow_history[i] == current:
                stagnant_count += 1
            else:
                break
        # Increase increment based on how long we've been stuck
        return base_increment * (1.0 + 0.5 * stagnant_count)

    # If overflow is decreasing and close to zero, be gentler
    if current < 5:
        return base_increment * 0.5

    # Normal progress - use base increment
    return base_increment


def detect_oscillation(overflow_history: list[int], window: int = 4) -> bool:
    """Detect if the router is oscillating between states.

    Args:
        overflow_history: List of overflow values from previous iterations
        window: Number of recent iterations to check (default: 4)

    Returns:
        True if oscillation is detected, False otherwise

    Detects:
    - A-B-A-B patterns (alternating between two values)
    - Complete stagnation (all same value for window iterations)
    - Bounded oscillation (only when the window does NOT contain a new
      global minimum, since a new minimum means the router is still
      making progress -- Issue #1823)
    """
    if len(overflow_history) < window:
        return False

    recent = overflow_history[-window:]

    # Issue #2262: If the most recent overflow is zero the router has
    # converged -- never report oscillation in this state.
    if recent[-1] == 0:
        return False

    # Issue #1823: If the recent window contains a new global minimum,
    # the router is still making progress -- do not declare oscillation.
    # For example, [21, 21, 8, 21] has overflow 8 as a new best, which
    # means the router found a better configuration even if it bounced back.
    best_overall = min(overflow_history)
    window_min = min(recent)
    window_has_new_minimum = window_min <= best_overall and window_min < min(
        overflow_history[:-window]
    ) if len(overflow_history) > window else False

    if window_has_new_minimum:
        return False

    # Check for exact A-B-A-B repetition pattern
    if window >= 4 and recent[0] == recent[2] and recent[1] == recent[3]:
        return True

    # Check for complete stagnation (all same value)
    # Zero-overflow stagnation is convergence, not oscillation (#2262)
    if len(set(recent)) == 1 and recent[0] > 0:
        return True

    # Check for bounded oscillation (values stay within small range)
    if window >= 4:
        unique_vals = set(recent)
        if len(unique_vals) <= 2 and min(recent) > 0:
            return True

    return False


def _is_monotonically_diverging(overflow_history: list[int], window: int = 3) -> bool:
    """Detect monotonically increasing overflow above the best-seen value.

    This catches diverging sequences like [90, 96, 88, 130, 148, 155] where
    the last ``window`` values are all strictly increasing and all above the
    overall best (minimum) value.  Standard oscillation detection misses this
    pattern because it looks for A-B-A-B cycles, and the half-split worsening
    check is defeated by an early dip that lands in the second half.

    Args:
        overflow_history: List of overflow values from previous iterations.
        window: Number of trailing values to inspect (default: 3).

    Returns:
        True if the last ``window`` values form a strictly increasing
        sequence that is entirely above the historical minimum.
    """
    if len(overflow_history) < window + 1:
        return False

    recent = overflow_history[-window:]
    best_seen = min(overflow_history)

    # All recent values must be strictly above the best seen
    if not all(v > best_seen for v in recent):
        return False

    # The recent window must be strictly increasing
    return all(recent[i] < recent[i + 1] for i in range(len(recent) - 1))


def should_terminate_early(
    overflow_history: list[int],
    iteration: int,
    min_iterations: int = 5,
    unrouted_count: int = 0,
) -> bool:
    """Decide if further iterations are futile and should terminate early.

    Args:
        overflow_history: List of overflow values from previous iterations
        iteration: Current iteration number
        min_iterations: Minimum iterations before considering early termination
        unrouted_count: Number of nets still unrouted. When overflow is 0 but
            unrouted nets remain, the router should continue to give
            neighborhood rip-up a chance to resolve them.

    Returns:
        True if should terminate early, False otherwise

    Terminates when:
    - No improvement in last 5 iterations (or 3 when overflow is low)
    - Monotonic divergence detected (overflow climbing away from best)
    - Oscillation detected
    - Overflow is getting worse over time
    """
    if iteration < min_iterations:
        return False

    # Issue #2297: When overflow is 0 but nets remain unrouted, do not
    # terminate early -- let neighborhood rip-up attempt to resolve them.
    if overflow_history and overflow_history[-1] == 0 and unrouted_count > 0:
        return False

    if len(overflow_history) < 5:
        return False

    # Issue #2295: Use a shorter stagnation window when overflow is low.
    # When only a few nets cause all overflow (e.g., overflow < 5), the
    # router often oscillates without resolving them.  A 3-iteration
    # window saves 2 full iterations of pointless rip-up (~240s on
    # high-pad-count boards like chorus-test-revA).
    stagnation_window = 5
    current_overflow = overflow_history[-1] if overflow_history else 0
    if current_overflow > 0 and current_overflow < 5 and len(overflow_history) >= 3:
        stagnation_window = 3

    recent = overflow_history[-stagnation_window:]

    # Issue #1823: Check if the recent window contains a new global minimum.
    # If so, skip the "no improvement" stagnation check (but still allow
    # other termination checks like monotonic divergence).
    best_overall = min(overflow_history)
    recent_has_new_global_min = False
    if min(recent) == best_overall and len(overflow_history) > stagnation_window:
        best_before_recent = min(overflow_history[:-stagnation_window])
        if min(recent) < best_before_recent:
            recent_has_new_global_min = True

    # No improvement in last N iterations (5 normally, 3 for low overflow).
    # When len(overflow_history) == stagnation_window there is no earlier
    # window; use the first recorded value as baseline instead of
    # float('inf') which would make this check unreachable and mask
    # stale-baseline divergence.
    # Issue #1823: Skip this check when recent window found a new global
    # minimum -- the router is still making progress.
    if not recent_has_new_global_min:
        if len(overflow_history) > stagnation_window:
            earlier = overflow_history[:-stagnation_window]
        else:
            earlier = overflow_history[:1]
        if min(recent) >= min(earlier):
            return True

    # Monotonic divergence: the last N values are strictly increasing and
    # all above the best-seen minimum.  This catches patterns like
    # [90, 96, 88, 130, 148, 155] that slip past the half-split check
    # because a single dip (88) in the second half keeps min(second_half)
    # low.
    if _is_monotonically_diverging(overflow_history, window=3):
        return True

    # Oscillating with no progress
    # Issue #1823: Skip this check when the recent 5-iteration window
    # contains a new global minimum -- the router recently made progress.
    if not recent_has_new_global_min and detect_oscillation(overflow_history, window=4):
        # Only terminate if we've tried enough and aren't improving
        if iteration >= min_iterations and min(recent) == min(overflow_history):
            return True

    # Overflow trending upward over time
    if len(overflow_history) >= 6:
        first_half = overflow_history[: len(overflow_history) // 2]
        second_half = overflow_history[len(overflow_history) // 2 :]
        if min(second_half) > min(first_half) * 1.2:
            # Getting worse, give up
            return True

    return False


def detect_ripup_stagnation(
    ripup_history: list[set[int]],
    overflow_history: list[int],
    *,
    overflow_delta_threshold: float = 0.20,
    jaccard_threshold: float = 0.8,
) -> bool:
    """Detect when negotiated rip-up keeps targeting the same nets without progress.

    Issue #2597.  This complements :func:`should_terminate_early`, which only
    inspects the overflow history.  On boards like chorus-test-revA the
    overflow trajectory is *strictly decreasing* (e.g. ``[30, 12, 10]``) so
    none of the standard stagnation checks fire — yet the rip-up cohort is
    identical across consecutive iterations and each new iteration only
    nibbles a few units off the overflow.  Rerouting the same six nets a
    fourth time is overwhelmingly likely to repeat the same near-identical
    paths and burn another ~per-net-timeout × N seconds before the wall-clock
    timeout fires.

    Stagnation is declared when **all** of the following hold:

    1. There are at least two complete iterations of rip-up history
       (``len(ripup_history) >= 2`` and ``len(overflow_history) >= 2``).
    2. The latest two rip-up sets ``A = ripup_history[-2]`` and
       ``B = ripup_history[-1]`` are highly similar:
       ``B`` is a non-empty subset of ``A``, **or** the Jaccard similarity
       ``|A ∩ B| / |A ∪ B|`` meets ``jaccard_threshold`` (default 0.8).
    3. Overflow improved by less than ``overflow_delta_threshold`` (default
       20 %): ``(overflow[-2] - overflow[-1]) / overflow[-2] <
       overflow_delta_threshold``.  A regression (overflow[-1] > overflow[-2])
       also counts as "less than threshold".
    4. The current overflow is still positive — at ``overflow == 0`` the
       router has converged and there is nothing to detect.

    Args:
        ripup_history: List of rip-up cohorts (one ``set[int]`` per
            negotiated outer iteration).  ``ripup_history[k]`` is the set
            of net IDs ripped up at the start of iteration ``k+1``.
        overflow_history: List of total overflow values (one per iteration,
            including the initial pass at index 0).  Must be at least the
            same length as ``ripup_history`` minus one initial-pass entry.
        overflow_delta_threshold: Minimum fractional improvement in overflow
            required to *avoid* a stagnation declaration.  Default 0.20
            (20 %).  Lower values are stricter (declare stagnation sooner).
        jaccard_threshold: Minimum Jaccard similarity between consecutive
            rip-up sets required to declare stagnation.  Default 0.8.  A
            strict subset relationship also satisfies this criterion
            regardless of the threshold.

    Returns:
        ``True`` if the negotiated outer loop should break out and let the
        existing best-state restore in ``two_phase.py`` hand back the
        previous (lower-overflow) iteration; ``False`` otherwise.

    Examples:
        >>> # chorus-test pattern: same 6 nets ripped up twice, overflow
        >>> # 30 -> 12 -> 10 (16 % improvement on iter 2)
        >>> detect_ripup_stagnation(
        ...     ripup_history=[{1, 2, 3, 4, 5, 6}, {1, 2, 3, 4, 5, 6}],
        ...     overflow_history=[30, 12, 10],
        ... )
        True

        >>> # Cohort churned -- different nets, no stagnation
        >>> detect_ripup_stagnation(
        ...     ripup_history=[{1, 2, 3}, {7, 8, 9}],
        ...     overflow_history=[30, 12, 10],
        ... )
        False

        >>> # Same cohort but >= 20 % overflow improvement
        >>> detect_ripup_stagnation(
        ...     ripup_history=[{1, 2, 3}, {1, 2, 3}],
        ...     overflow_history=[30, 12, 5],
        ... )
        False
    """
    # Criterion 1: need two complete rip-up + overflow iterations to compare.
    if len(ripup_history) < 2 or len(overflow_history) < 2:
        return False

    prev_ripup = ripup_history[-2]
    curr_ripup = ripup_history[-1]
    prev_overflow = overflow_history[-2]
    curr_overflow = overflow_history[-1]

    # Criterion 4: skip when converged (no work left to detect).
    if curr_overflow <= 0:
        return False

    # Empty cohorts indicate the iteration ripped up nothing -- not the
    # stagnation pattern this detector targets.
    if not prev_ripup or not curr_ripup:
        return False

    # Criterion 2: high overlap between consecutive rip-up cohorts.
    # Subset relationship (curr ⊆ prev) is always sufficient; otherwise
    # require Jaccard similarity >= threshold.
    if curr_ripup <= prev_ripup:
        cohort_stable = True
    else:
        intersection = len(curr_ripup & prev_ripup)
        union = len(curr_ripup | prev_ripup)
        jaccard = intersection / union if union else 0.0
        cohort_stable = jaccard >= jaccard_threshold

    if not cohort_stable:
        return False

    # Criterion 3: insufficient overflow improvement on this iteration.
    # If overflow regressed or held steady, treat that as 0 % improvement.
    if prev_overflow <= 0:
        # Cannot compute a fractional delta from a non-positive baseline;
        # fall back to "no improvement on a positive current overflow".
        return True

    improvement = (prev_overflow - curr_overflow) / prev_overflow
    return improvement < overflow_delta_threshold


def calculate_present_cost(
    iteration: int,
    total_iterations: int,
    overflow_ratio: float,
    base_cost: float = 0.5,
    *,
    exponential: bool = False,
    pres_fac_mult: float = 1.3,
    pres_fac_cap: float = 50.0,
) -> float:
    """Calculate adaptive present cost factor based on iteration and congestion.

    Args:
        iteration: Current iteration number (0-indexed)
        total_iterations: Maximum number of iterations
        overflow_ratio: Current overflow / total cells (congestion metric)
        base_cost: Base present cost value (default: 0.5)
        exponential: If True, use exponential escalation (OrthoRoute-style)
            instead of linear ramp.  Exponential pressure more aggressively
            forces nets away from congested areas in later iterations.
            Issue #2333.
        pres_fac_mult: Multiplicative factor applied per iteration in
            exponential mode.  Default 1.3 (30% increase per iteration).
        pres_fac_cap: Maximum present cost factor in exponential mode
            to prevent overshooting.  Default 50.0.

    Returns:
        Adjusted present cost factor

    The cost increases:
    - As iterations progress (more pressure over time)
    - When congestion is high (need to discourage contested resources)
    """
    if exponential:
        # OrthoRoute-style: base * mult^iteration, capped to prevent runaway
        raw = base_cost * (pres_fac_mult ** iteration)
        return min(raw, pres_fac_cap)

    # Linear mode (original behaviour)
    # Increase pressure as iterations progress (gradual ramp)
    progress_factor = 1.0 + (iteration / max(total_iterations, 1))

    # Higher cost when more congested
    congestion_factor = 1.0 + min(overflow_ratio * 2, 2.0)  # Cap at 3x

    return base_cost * progress_factor * congestion_factor


def calculate_congestion_tuned_params(
    overflow_ratio: float,
    base_pres_fac_mult: float = 1.3,
    base_history_increment: float = 0.5,
) -> tuple[float, float]:
    """Derive present-cost multiplier and history increment from congestion ratio.

    OrthoRoute-inspired auto-tuning: instead of using fixed parameters,
    scale them based on the actual congestion level so that convergence
    is less dependent on board characteristics.

    Args:
        overflow_ratio: Fraction of overused edges (overused / total cells).
        base_pres_fac_mult: Base exponential multiplier (default: 1.3).
        base_history_increment: Base history increment (default: 0.5).

    Returns:
        Tuple of (adjusted_pres_fac_mult, adjusted_history_increment).

    Issue #2333.
    """
    # When congestion is high (>10%), escalate more aggressively
    # When congestion is low (<1%), reduce escalation to fine-tune
    if overflow_ratio > 0.10:
        scale = 1.0 + min(overflow_ratio, 0.5)  # cap at 1.5x
    elif overflow_ratio < 0.01:
        scale = 0.7
    else:
        scale = 1.0

    adjusted_mult = 1.0 + (base_pres_fac_mult - 1.0) * scale
    adjusted_hist = base_history_increment * scale

    return adjusted_mult, adjusted_hist


# Resolution used when grouping vias by (x, y) for cross-route same-net
# deduplication.  Vias whose coordinates round to the same key (to the
# nearest micron) are treated as the same physical placement.
_VIA_DEDUP_RESOLUTION_MM = 0.001


def _dedupe_sibling_route_vias(
    routes: list[Route],
    *,
    resolution_mm: float = _VIA_DEDUP_RESOLUTION_MM,
) -> int:
    """Drop duplicate same-net vias across sibling Route objects (Issue #2481).

    A multi-pin net routed via RSMT decomposition emits one ``Route``
    object per Steiner edge, each carrying its own ``vias`` list.  When
    two RSMT edges meet at a Steiner tap and both edges require a layer
    transition there, they each insert a via at the same (x, y).  These
    duplicates survive into the post-route validator, where each pair of
    sibling vias against any cross-net via produces a separate
    via-vs-via violation -- inflating the reported defect count and, for
    the C++ side, polluting ``stored_vias_`` with redundant entries.

    This function keeps the first via per (rounded x, rounded y) across
    *all* the supplied sibling routes, expands its layer span to cover
    every duplicate, and removes the duplicates from their owning route.
    Segments are not touched: they already terminate at the Steiner tap
    coordinate, which still has a via after this dedup.

    Args:
        routes: List of sibling ``Route`` objects produced for a single
            net.  Mutated in place.
        resolution_mm: Coordinate rounding for the dedup key.  Defaults
            to 1 micron, well below the routing grid resolution.

    Returns:
        The number of duplicate vias removed across all routes.
    """
    if len(routes) < 2:
        return 0

    from ..layers import Layer

    inv_res = 1.0 / resolution_mm
    seen: dict[tuple[int, int], Via] = {}
    removed = 0

    for route in routes:
        if not route.vias:
            continue
        keep_indices: list[int] = []
        for idx, via in enumerate(route.vias):
            key = (
                int(round(via.x * inv_res)),
                int(round(via.y * inv_res)),
            )
            existing = seen.get(key)
            if existing is None:
                seen[key] = via
                keep_indices.append(idx)
            else:
                # Expand the surviving via's layer range to cover this
                # duplicate's layer range, in case the two edges had
                # different layer pairs at the same tap (mid-layer
                # buried via on one edge, full-stack on the other).
                min_layer = min(
                    existing.layers[0].value,
                    existing.layers[1].value,
                    via.layers[0].value,
                    via.layers[1].value,
                )
                max_layer = max(
                    existing.layers[0].value,
                    existing.layers[1].value,
                    via.layers[0].value,
                    via.layers[1].value,
                )
                if (
                    min_layer != existing.layers[0].value
                    or max_layer != existing.layers[1].value
                ):
                    existing.layers = (Layer(min_layer), Layer(max_layer))
                removed += 1

        if len(keep_indices) != len(route.vias):
            route.vias = [route.vias[k] for k in keep_indices]

    return removed


class NegotiatedRouter:
    """PathFinder-style negotiated congestion router.

    Routes all nets with temporary resource sharing allowed,
    then iteratively rips up and reroutes conflicting nets
    with increasing congestion penalties until convergence.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        router: Router,
        rules: DesignRules,
        net_class_map: dict[str, NetClassRouting],
        congestion_estimator: CongestionEstimator | None = None,
        congestion_weight: float = 0.5,
    ):
        """Initialize the negotiated router.

        Args:
            grid: The routing grid
            router: The pathfinding router
            rules: Design rules
            net_class_map: Net class routing rules
            congestion_estimator: Optional RUDY congestion estimator.  When
                provided, Steiner tree construction blends Manhattan distance
                with tile demand so that multi-terminal nets prefer
                less-congested paths.
            congestion_weight: Multiplier applied to the tile demand when
                computing the congestion-aware edge cost.  The weight is
                scaled internally by tile area so that demand (mm) and
                Manhattan distance (mm) are comparable.  A value of 0
                disables congestion influence.  Default is 0.5.
        """
        self.grid = grid
        self.router = router
        self.rules = rules
        self.net_class_map = net_class_map
        self.congestion_estimator = congestion_estimator
        self.congestion_weight = congestion_weight

        # Issue #2476: Set of (failed_net_id, blocking_via_net_id) pairs
        # collected from the C++ pathfinder's structured failure
        # diagnostics during route_net_negotiated().  The negotiated outer
        # loop drains this via :meth:`get_and_clear_via_blocking_nets` to
        # target rip-up at the specific net whose stored via blocked
        # progress, rather than blanket retry.
        self._last_via_blocking_nets: set[tuple[int, int]] = set()

        # Issue #2769: Set of net ids whose RSMT edge loop was aborted
        # because the cumulative per_net_timeout budget was exhausted
        # mid-loop.  Distinct from per-edge BLOCKED_PATH or VIA_BLOCKED
        # failures -- these nets ran out of wall-clock budget while still
        # progressing.  Drained by the outer loop via
        # :meth:`get_and_clear_timeout_failures` so audit logs can
        # differentiate "couldn't find path" from "ran out of net budget".
        self._last_timeout_failures: set[int] = set()

    def route_net_negotiated(
        self,
        pad_objs: list[Pad],
        present_cost_factor: float,
        mark_route_callback: callable,
        per_net_timeout: float | None = None,
        failure_callback: callable | None = None,
    ) -> list[Route]:
        """Route a single net in negotiated mode.

        Args:
            pad_objs: List of Pad objects to connect
            present_cost_factor: Multiplier for present sharing cost
            mark_route_callback: Callback to mark a route on the grid
            per_net_timeout: Optional wall-clock timeout in seconds that
                brackets THIS WHOLE NET (Issue #1605, fixed in Issue #2769).
                For multi-pin nets the budget is shared across all RSMT
                edges: edges run sequentially against a cumulative
                ``time.monotonic()`` deadline, and each edge receives the
                REMAINING budget.  Once exhausted, the remaining edges are
                short-circuited as ``_FAILURE_TIMEOUT`` (drainable via
                :meth:`get_and_clear_timeout_failures`).  For 2-pin nets
                the budget caps the single A* search.
            failure_callback: Optional callback to record routing failures.
                Called with (source_pad, target_pad) when routing fails
                (Issue #2425).  Also fired for edges short-circuited by
                cumulative-timeout exhaustion (Issue #2769) so the
                rip-up/retry layer in Issue #2476 sees them.

        Returns:
            List of routes created.  Partial nets are preserved: routes
            produced before a cumulative-timeout abort are NOT discarded
            (Issue #2769 acceptance criterion).

        Notes:
            Issue #2476: After every failed sub-route (RSMT edge or 2-pin
            connection), this method consults
            ``self.router.get_last_failure_info()`` and, when the C++
            pathfinder reports a ``FAILURE_VIA_VIA_BLOCKED`` diagnostic,
            records the offending stored-via net in
            ``self._last_via_blocking_nets``.  The negotiated outer loop
            uses that list (via :meth:`get_and_clear_via_blocking_nets`) to
            target rip-up at the specific blockers rather than blanket
            retry.

            Issue #2769: Cumulative-timeout aborts (whole-net budget
            exhausted) are tracked separately in
            ``self._last_timeout_failures`` so audit logs can differentiate
            "ran out of net budget" from "no path / via-blocked".
        """
        if len(pad_objs) < 2:
            return []

        routes: list[Route] = []

        if len(pad_objs) > 2:
            # RSMT-based routing with negotiated mode
            from .steiner import build_rsmt

            # Build congestion-aware cost function for Steiner tree
            # construction when a RUDY estimator is available.
            congestion_fn = None
            if (
                self.congestion_estimator is not None
                and self.congestion_weight > 0
            ):
                est = self.congestion_estimator
                tile_area = est.grid.tile_w * est.grid.tile_h
                # Scale weight by tile area so demand and Manhattan
                # distance are in comparable units (mm).
                scaled_weight = self.congestion_weight * tile_area

                def congestion_fn(
                    x1: float, y1: float, x2: float, y2: float
                ) -> float:
                    manhattan = abs(x1 - x2) + abs(y1 - y2)
                    mid_x = (x1 + x2) / 2
                    mid_y = (y1 + y2) / 2
                    col, row = est.grid.tile_at(mid_x, mid_y)
                    demand = est.get_tile_demand(row, col)
                    return manhattan + scaled_weight * demand

            pad_objs, rsmt_edges = build_rsmt(
                pad_objs, congestion_fn=congestion_fn
            )

            # Issue #2306: Incremental Steiner target-set expansion.
            # After routing each RSMT edge, collect the grid cells along
            # the routed path.  Subsequent edges can terminate A* early
            # when they reach any cell of the existing net tree, avoiding
            # full-grid searches for high-fanout nets like GNDD.
            routed_cells: set[tuple[int, int, int]] = set()

            # Issue #2769: ``per_net_timeout`` brackets the WHOLE net, not
            # each RSMT edge.  Compute a single cumulative deadline before
            # the loop and pass each edge the REMAINING budget so that:
            #   - the last edge sees per_net_timeout >= remaining          (<=
            #     per_net_timeout for an individual A* search invariant), and
            #   - sum of all edge wall-times <= per_net_timeout             (<=
            #     per_net_timeout for the whole-net invariant).
            # When the budget is exhausted mid-loop we short-circuit the
            # remaining edges, record them as timeout failures (distinct
            # from BLOCKED_PATH / VIA_VIA_BLOCKED), and ``continue`` so each
            # skipped edge still surfaces via ``failure_callback`` for the
            # rip-up / retry layer (Issue #2476).
            net_deadline = (
                time.monotonic() + per_net_timeout
                if per_net_timeout is not None
                else None
            )

            for i, j in rsmt_edges:
                source_pad = pad_objs[i]
                target_pad = pad_objs[j]

                if net_deadline is not None:
                    remaining = net_deadline - time.monotonic()
                    if remaining <= 0:
                        # Cumulative net-budget exhausted before this edge
                        # could be attempted.  Classify as _FAILURE_TIMEOUT
                        # (Issue #2610) so audit logs can differentiate this
                        # from a genuine BLOCKED_PATH, and ``continue`` so
                        # every skipped edge is still surfaced to the
                        # rip-up/retry layer via ``failure_callback``.
                        self._last_timeout_failures.add(source_pad.net)
                        if failure_callback is not None:
                            failure_callback(source_pad, target_pad)
                        continue
                    edge_timeout: float | None = remaining
                else:
                    edge_timeout = None

                route = self.router.route(
                    source_pad,
                    target_pad,
                    negotiated_mode=True,
                    present_cost_factor=present_cost_factor,
                    per_net_timeout=edge_timeout,
                    extra_goal_cells=routed_cells if routed_cells else None,
                )
                # Issue #2934: ``Route`` is a dataclass and therefore always
                # truthy regardless of segment count.  Defensive check for
                # an empty Route here in addition to the upstream rejection
                # in ``_reconstruct_route``: if either guard slips
                # (e.g., a future C++ backend path returns an empty Route),
                # this branch still records the failure and fires
                # ``failure_callback`` rather than silently dropping a
                # missing-connectivity sub-route into the result list.
                if route and (route.segments or route.vias):
                    mark_route_callback(route)
                    routes.append(route)
                    # Collect grid cells from the routed segments so later
                    # edges can terminate early upon reaching this tree.
                    self._collect_route_cells(route, routed_cells)
                else:
                    # Issue #2476: Capture structured via-blocked failure
                    # diagnostics from the cpp pathfinder so the negotiated
                    # outer loop can target rip-up at the specific blocker.
                    self._record_via_blocked_failure(source_pad.net)
                    if failure_callback is not None:
                        failure_callback(source_pad, target_pad)

            # Issue #2481: Dedupe vias at the same (x, y) location across
            # the multi-edge RSMT sub-routes for this net.  Two RSMT edges
            # that meet at a Steiner tap on the same layer pair would each
            # insert a via at that tap, producing duplicate same-net vias
            # that the post-route DRC counts as triple-pair violations.
            # We keep the first occurrence per (rounded) (x, y) and drop
            # the rest from sibling sub-routes.
            _dedupe_sibling_route_vias(routes)
        else:
            # 2-pin net
            route = self.router.route(
                pad_objs[0],
                pad_objs[1],
                negotiated_mode=True,
                present_cost_factor=present_cost_factor,
                per_net_timeout=per_net_timeout,
            )
            # Issue #2934: Defensive check for empty Routes; see comment on
            # the multi-edge RSMT path above for the rationale.
            if route and (route.segments or route.vias):
                mark_route_callback(route)
                routes.append(route)
            else:
                # Issue #2476: Capture cpp-side via-blocked diagnostic.
                self._record_via_blocked_failure(pad_objs[0].net)
                if failure_callback is not None:
                    failure_callback(pad_objs[0], pad_objs[1])

        return routes

    # =========================================================================
    # Via-blocked failure tracking (Issue #2476)
    # =========================================================================

    # Failure-reason constants mirroring router_cpp.FAILURE_*.
    # Hard-coded here so the Python module imports cleanly even when the C++
    # extension is unavailable; values must match ``cpp/include/types.hpp``.
    _FAILURE_NONE = 0
    _FAILURE_NO_PATH = 1
    _FAILURE_ITERATION_LIMIT = 2
    # Issue #2610: per-net wall-clock deadline (--per-net-timeout) was hit.
    _FAILURE_TIMEOUT = 3
    _FAILURE_VIA_VIA_BLOCKED = 5

    # Issue #2610: Human-readable labels for log differentiation.  Used by
    # describe_failure_reason() to emit "DAC_CLK aborted at iteration cap
    # (N iterations)" vs "DAC_CLK timed out at wall-clock deadline" vs
    # "DAC_CLK BLOCKED_PATH (open set drained)" rather than bucketing all
    # three into a generic BLOCKED_PATH log line.
    _FAILURE_REASON_LABELS = {
        _FAILURE_NONE: "none",
        _FAILURE_NO_PATH: "blocked_path",
        _FAILURE_ITERATION_LIMIT: "iteration_cap",
        _FAILURE_TIMEOUT: "wall_clock_timeout",
        _FAILURE_VIA_VIA_BLOCKED: "via_via_blocked",
    }

    @classmethod
    def describe_failure_reason(cls, info: dict | None) -> str:
        """Return a short label for the failure-info dict's ``failure_reason``.

        Issue #2610: Router logs use this to distinguish iteration-cap aborts
        from wall-clock timeouts from genuine BLOCKED_PATH, so users can tell
        whether to bump ``--per-net-timeout``, raise ``--max-search-iterations``,
        or accept that the net is geometrically unroutable.

        Args:
            info: Dict returned by ``CppPathfinder.get_last_failure_info()``,
                or ``None``.

        Returns:
            One of: ``"none"``, ``"blocked_path"``, ``"iteration_cap"``,
            ``"wall_clock_timeout"``, ``"via_via_blocked"``, ``"unknown"``.
        """
        if not info:
            return "none"
        reason = int(info.get("failure_reason") or 0)
        return cls._FAILURE_REASON_LABELS.get(reason, "unknown")

    def _record_via_blocked_failure(self, failed_net: int) -> None:
        """Capture a via-vs-via failure from the most recent route() call.

        Issue #2476: When a sub-route fails, ask the underlying router for
        structured failure diagnostics.  If the C++ pathfinder reports
        ``FAILURE_VIA_VIA_BLOCKED`` along with a non-zero
        ``blocking_via_net``, record the (failed_net, blocking_net) pair so
        the negotiated outer loop can rip up the specific blocker rather
        than blanket retry.

        This is a no-op when:
        - The router is the Python pathfinder (returns ``None``).
        - The failure was an unrelated grid-cell rejection (no actionable
          diagnostic).
        - The blocking net id is 0 (not a stored-via geometric reject).

        Args:
            failed_net: Net id of the net whose route failed.
        """
        get_info = getattr(self.router, "get_last_failure_info", None)
        if get_info is None:
            return
        info = get_info()
        if not info:
            return
        if info.get("failure_reason") != self._FAILURE_VIA_VIA_BLOCKED:
            return
        blocking_net = int(info.get("blocking_via_net") or 0)
        if blocking_net == 0 or blocking_net == failed_net:
            return
        self._last_via_blocking_nets.add((failed_net, blocking_net))

    def get_and_clear_via_blocking_nets(self) -> set[tuple[int, int]]:
        """Drain and return the set of (failed_net, blocking_net) pairs.

        Issue #2476: Each pair indicates that ``failed_net``'s A* search
        was rejected by ``blocking_net``'s stored via.  The negotiated
        outer loop rips up ``blocking_net`` and then retries
        ``failed_net``.  After draining, the internal set is cleared so
        subsequent iterations only see fresh diagnostics.

        Returns:
            A set of ``(failed_net, blocking_net)`` tuples.  Empty if no
            via-blocked failures have been recorded since the last drain.
        """
        result = self._last_via_blocking_nets
        self._last_via_blocking_nets = set()
        return result

    def get_and_clear_timeout_failures(self) -> set[int]:
        """Drain and return the set of net ids that hit a cumulative
        per-net timeout during the most recent ``route_net_negotiated``
        invocation(s).

        Issue #2769: When the cumulative ``per_net_timeout`` budget is
        exhausted mid-RSMT-loop, the remaining edges are short-circuited
        and the net id is recorded here so the negotiated outer loop can
        distinguish "ran out of net budget" from "found no path" in audit
        logs.  After draining, the internal set is cleared so subsequent
        iterations only see fresh diagnostics.

        Returns:
            A set of net ids.  Empty if no cumulative-timeout failures
            have been recorded since the last drain.
        """
        result = self._last_timeout_failures
        self._last_timeout_failures = set()
        return result

    def _collect_route_cells(
        self,
        route: Route,
        cell_set: set[tuple[int, int, int]],
    ) -> None:
        """Add grid cells covered by a route to *cell_set*.

        For each segment, walk grid cells between the two endpoints and
        insert ``(gx, gy, layer_index)`` tuples.  Via locations are added
        on all routable layers so the A* can connect through them.

        Issue #2306: Used by incremental Steiner routing to build the
        target-set for subsequent RSMT-edge A* searches.
        """
        for seg in route.segments:
            gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
            gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
            layer_idx = self.grid.layer_to_index(seg.layer.value)

            steps = max(abs(gx2 - gx1), abs(gy2 - gy1), 1)
            for s in range(steps + 1):
                t = s / steps
                gx = int(gx1 + t * (gx2 - gx1))
                gy = int(gy1 + t * (gy2 - gy1))
                cell_set.add((gx, gy, layer_idx))

        # Add via locations on all routable layers
        routable = self.grid.get_routable_indices()
        for via in route.vias:
            gx, gy = self.grid.world_to_grid(via.x, via.y)
            for li in routable:
                cell_set.add((gx, gy, li))

    def find_nets_through_overused_cells(
        self,
        net_routes: dict[int, list[Route]],
        overused_cells: list[tuple[int, int, int, int]],
    ) -> list[int]:
        """Find nets with routes passing through overused cells.

        Args:
            net_routes: Dictionary of net_id -> list of routes
            overused_cells: List of (gx, gy, layer, overflow) tuples

        Returns:
            List of net IDs that need rerouting
        """
        overused_set = {(x, y, layer) for x, y, layer, _ in overused_cells}
        nets_to_reroute: list[int] = []

        for net, routes in net_routes.items():
            needs_reroute = False
            for route in routes:
                for seg in route.segments:
                    # Check if segment passes through overused cell
                    gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
                    gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
                    layer = seg.layer.value

                    # Sample points along segment
                    steps = max(abs(gx2 - gx1), abs(gy2 - gy1), 1)
                    for i in range(steps + 1):
                        t = i / steps
                        gx = int(gx1 + t * (gx2 - gx1))
                        gy = int(gy1 + t * (gy2 - gy1))
                        if (gx, gy, layer) in overused_set:
                            needs_reroute = True
                            break
                    if needs_reroute:
                        break
                if needs_reroute:
                    break

            if needs_reroute:
                nets_to_reroute.append(net)

        return nets_to_reroute

    def rip_up_nets(
        self,
        nets: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
    ) -> None:
        """Rip up routes for specified nets.

        Args:
            nets: Net IDs to rip up
            net_routes: Dictionary of net_id -> list of routes
            routes_list: Master list of all routes
        """
        for net in nets:
            for route in net_routes.get(net, []):
                self.grid.unmark_route_usage(route)
                self.grid.unmark_route(route)
                if route in routes_list:
                    routes_list.remove(route)
            net_routes[net] = []

    def via_blocked_ripup(
        self,
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        present_cost_factor: float,
        mark_route_callback: callable,
        ripup_history: dict[int, int] | None = None,
        max_ripups_per_net: int = 3,
        per_net_timeout: float | None = None,
    ) -> tuple[int, int]:
        """Targeted rip-up driven by C++ via-vs-via failure diagnostics.

        Issue #2476: When the C++ A* search refuses every via candidate
        because of a stored-via clearance violation, it surfaces the
        offending stored-via net via ``RouteResult.blocking_via_net``.
        This method drains those (failed_net, blocking_net) pairs from
        :meth:`get_and_clear_via_blocking_nets`, rips up each blocking
        net, and routes the failed net first.  Displaced blockers are
        rerouted afterwards.

        Compared to :meth:`targeted_ripup`, the blocker set comes from a
        precise geometric diagnostic rather than a Bresenham line scan or
        relaxed A*, so we avoid the false "no direct blockers found" path
        that previously punted to a blanket retry on board 02.

        Args:
            net_routes: Dictionary of net_id -> list of routes.
            routes_list: Master list of all routes.
            pads_by_net: Dictionary of net_id -> list of pads.
            present_cost_factor: Current congestion cost factor.
            mark_route_callback: Callback to mark routes on the grid.
            ripup_history: Optional dict tracking per-net rip-up counts.
            max_ripups_per_net: Maximum rip-ups per net to prevent loops.
            per_net_timeout: Optional wall-clock timeout per A* search.

        Returns:
            Tuple of ``(resolved_count, attempted_count)`` -- how many
            failed nets routed successfully after the rip-up, out of how
            many distinct via-blocked failures were observed.
        """
        if ripup_history is None:
            ripup_history = {}

        pairs = self.get_and_clear_via_blocking_nets()
        if not pairs:
            return (0, 0)

        # Group blockers per failed net.  A single failed net may have
        # accumulated multiple distinct blocking-via observations across
        # its RSMT edges.
        blockers_by_failed: dict[int, set[int]] = {}
        for failed_net, blocking_net in pairs:
            blockers_by_failed.setdefault(failed_net, set()).add(blocking_net)

        resolved = 0
        attempted = 0

        for failed_net, blocking_nets in blockers_by_failed.items():
            attempted += 1

            # Filter blockers by ripup budget.
            nets_to_ripup: set[int] = set()
            for net in blocking_nets:
                if ripup_history.get(net, 0) < max_ripups_per_net:
                    nets_to_ripup.add(net)
                    ripup_history[net] = ripup_history.get(net, 0) + 1

            if not nets_to_ripup:
                # All blockers have hit their rip-up budget; skip this net
                # so we don't churn endlessly on the same conflict.
                continue

            # Rip up the specific blockers identified by the cpp search.
            self.rip_up_nets(list(nets_to_ripup), net_routes, routes_list)

            # Route the failed net first (priority over displaced nets).
            failed_pads = pads_by_net.get(failed_net, [])
            failed_net_success = False
            if failed_pads and len(failed_pads) >= 2:
                routes = self.route_net_negotiated(
                    failed_pads, present_cost_factor, mark_route_callback,
                    per_net_timeout=per_net_timeout,
                )
                if routes:
                    net_routes[failed_net] = routes
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)
                    failed_net_success = True

            # Re-route the displaced blockers.  Their routing may now use
            # different via positions that no longer collide with the
            # newly-routed failed net.
            for net in nets_to_ripup:
                net_pads = pads_by_net.get(net, [])
                if net_pads and len(net_pads) >= 2:
                    routes = self.route_net_negotiated(
                        net_pads, present_cost_factor, mark_route_callback,
                        per_net_timeout=per_net_timeout,
                    )
                    if routes:
                        net_routes[net] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            routes_list.append(route)

            if failed_net_success:
                resolved += 1

        return (resolved, attempted)

    def targeted_ripup(
        self,
        failed_net: int,
        blocking_nets: set[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        present_cost_factor: float,
        mark_route_callback: callable,
        ripup_history: dict[int, int] | None = None,
        max_ripups_per_net: int = 3,
        per_net_timeout: float | None = None,
        progress_callback: ProgressCallback | None = None,
        net_names: dict[int, str] | None = None,
    ) -> bool:
        """Perform targeted rip-up of blocking nets and re-route.

        Instead of ripping up all conflicting nets, this method only rips up
        the specific nets that are blocking the failed net's path, then
        re-routes the failed net first (giving it priority) followed by
        the displaced nets.

        Args:
            failed_net: Net ID that failed to route
            blocking_nets: Set of net IDs blocking the failed net's path
            net_routes: Dictionary of net_id -> list of routes
            routes_list: Master list of all routes
            pads_by_net: Dictionary of net_id -> list of pads for that net
            present_cost_factor: Current congestion cost factor
            mark_route_callback: Callback to mark routes on the grid
            ripup_history: Optional dict tracking ripup count per net
            max_ripups_per_net: Maximum times a net can be ripped up (prevents loops)
            per_net_timeout: Optional wall-clock timeout in seconds for each
                A* search (Issue #1605)
            progress_callback: Optional callback invoked before each
                ``route_net_negotiated`` call (failed net + each sibling).
                Receives ``(phase_label, info_dict)`` where ``info_dict``
                contains ``phase`` ("failed_net" or "sibling"), ``net``
                (net id), ``net_name`` (resolved via ``net_names``), ``index``
                (1-indexed position), ``total`` (failed net + siblings count),
                and ``elapsed`` (seconds since this call started).  Issue #2795:
                used by ``_attempt_blocked_component_ripup_negotiated`` to
                surface progress during what would otherwise be a silent
                multi-minute operation.
            net_names: Optional net-id-to-name mapping used to resolve human
                readable net names for the ``progress_callback`` payload.
                Falls back to ``Net_<id>`` when missing or absent.

        Returns:
            True if re-routing succeeded for all affected nets, False otherwise.

            Issue #2814: when the failed net's A* search returns ``None`` even
            with all sibling routes already removed from the grid, the blocker
            is geometric (pad-clearance keepouts, board-edge limits, fixed
            escape routes, component bodies) rather than sibling traces.  In
            this case we fast-fail: the sibling routes are restored using a
            short probe timeout (``min(per_net_timeout, 10s)``) so the grid
            is not left in a worse state than we found it, and we return
            ``False`` without re-routing the siblings at full
            ``per_net_timeout``.  The caller is expected to escalate to a
            different strategy (e.g. layer escalation, escape rework).  This
            drops wall-clock on geometric-blocker failures from
            ``(1 + N) * per_net_timeout`` to
            ``per_net_timeout + N * min(per_net_timeout, 10s)``.
        """
        if ripup_history is None:
            ripup_history = {}

        # Filter out nets that have been ripped up too many times
        # This prevents infinite loops where nets keep displacing each other
        nets_to_ripup: set[int] = set()
        for net in blocking_nets:
            if ripup_history.get(net, 0) < max_ripups_per_net:
                nets_to_ripup.add(net)
                ripup_history[net] = ripup_history.get(net, 0) + 1

        if not nets_to_ripup:
            # All blocking nets have reached their ripup limit
            # Fall back to normal routing with high congestion cost
            return False

        # Rip up only the blocking nets
        self.rip_up_nets(list(nets_to_ripup), net_routes, routes_list)

        # Issue #2795: emit progress before each long-running A* invocation.
        # Total work units = 1 (failed net) + N siblings, so index runs 1..total.
        total_steps = 1 + len(nets_to_ripup)
        start_time = time.time()

        def _emit_progress(phase: str, net_id: int, index: int) -> None:
            if progress_callback is None:
                return
            if net_names is not None:
                resolved_name = net_names.get(net_id, f"Net_{net_id}")
            else:
                resolved_name = f"Net_{net_id}"
            try:
                progress_callback(
                    "ripup_phase",
                    {
                        "phase": phase,
                        "net": net_id,
                        "net_name": resolved_name,
                        "index": index,
                        "total": total_steps,
                        "elapsed": time.time() - start_time,
                    },
                )
            except Exception:
                # Never let a buggy progress callback abort the rip-up.
                pass

        # Re-route the failed net first (it now has priority with cleared path)
        failed_pads = pads_by_net.get(failed_net, [])
        failed_net_success = False  # Issue #858: Track if failed net was routed
        if failed_pads and len(failed_pads) >= 2:
            _emit_progress("failed_net", failed_net, 1)
            routes = self.route_net_negotiated(
                failed_pads, present_cost_factor, mark_route_callback,
                per_net_timeout=per_net_timeout,
            )
            if routes:
                net_routes[failed_net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    routes_list.append(route)
                failed_net_success = True  # Failed net was successfully routed

        # Issue #2814: fast-fail when the failed net's A* still cannot find a
        # path with the sibling routes already cleared from the grid (see the
        # ``rip_up_nets`` call at the start of this method).  In that case the
        # blocker is geometric -- pad-clearance keepouts, board-edge limits,
        # fixed escape routes, or component bodies -- so re-routing every
        # sibling with a full ``per_net_timeout`` cannot possibly help and
        # would just waste ``len(siblings) * per_net_timeout`` wall-clock.
        # Restore the sibling routes with a short probe timeout so we don't
        # leave the grid in a worse state than we found it, then return
        # False to let the caller (e.g.
        # ``_attempt_blocked_component_ripup_negotiated``) escalate to a
        # different strategy (layer escalation, escape rework).
        if not failed_net_success:
            sibling_probe_timeout = (
                min(per_net_timeout, 10.0)
                if per_net_timeout is not None
                else 10.0
            )
            sibling_order = sorted(nets_to_ripup)
            for sibling_index, net in enumerate(sibling_order, start=2):
                net_pads = pads_by_net.get(net, [])
                if net_pads and len(net_pads) >= 2:
                    _emit_progress("sibling", net, sibling_index)
                    routes = self.route_net_negotiated(
                        net_pads, present_cost_factor, mark_route_callback,
                        per_net_timeout=sibling_probe_timeout,
                    )
                    if routes:
                        net_routes[net] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            routes_list.append(route)
            return False  # geometric blocker -- caller should escalate

        # Re-route the displaced nets
        success = failed_net_success  # Issue #858: Start with failed net success
        # Sort for deterministic progress ordering (set iteration is unordered).
        sibling_order = sorted(nets_to_ripup)
        for sibling_index, net in enumerate(sibling_order, start=2):
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                _emit_progress("sibling", net, sibling_index)
                routes = self.route_net_negotiated(
                    net_pads, present_cost_factor, mark_route_callback,
                    per_net_timeout=per_net_timeout,
                )
                if routes:
                    net_routes[net] = routes
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)
                else:
                    # Displaced net failed to re-route
                    success = False

        return success

    def find_blocking_nets_for_connection(
        self,
        source_pad: Pad,
        target_pad: Pad,
    ) -> set[int]:
        """Find nets blocking a specific connection.

        Uses the pathfinder's find_blocking_nets method to identify
        which nets are blocking the direct path between two pads.

        Args:
            source_pad: Starting pad
            target_pad: Ending pad

        Returns:
            Set of net IDs blocking the path
        """
        return self.router.find_blocking_nets(source_pad, target_pad)

    def find_blocking_nets_relaxed(
        self,
        failed_nets: list[int],
        pads_by_net: dict[int, list[Pad]],
        per_net_timeout: float | None = None,
    ) -> dict[int, int]:
        """Identify true blockers using relaxed A* (Issue #2274).

        For each unrouted net, temporarily unblocks all routed-net cells
        and runs A* to find a viable path.  Cells along that relaxed path
        that were originally occupied by routed nets reveal the *true*
        blockers.

        Args:
            failed_nets: List of net IDs that failed to route.
            pads_by_net: Mapping of net ID to its pads.
            per_net_timeout: Optional timeout per relaxed A* search.

        Returns:
            Dict mapping blocking net ID to the number of stuck nets it
            blocks (score).  Higher score = blocks more stuck nets.
        """
        blocker_scores: dict[int, int] = {}

        with self.grid.temporarily_unblock_routed_nets() as unblocker:
            saved_blocked = unblocker._saved_blocked
            saved_net = unblocker._saved_net

            for net_id in failed_nets:
                pads = pads_by_net.get(net_id, [])
                if len(pads) < 2:
                    continue

                # Try finding a relaxed path for the first pair of pads
                net_blockers: set[int] = set()
                for j in range(len(pads) - 1):
                    blockers = self.router.find_blocking_nets_relaxed(
                        pads[j],
                        pads[j + 1],
                        saved_blocked,
                        saved_net,
                        per_net_timeout=per_net_timeout,
                    )
                    net_blockers.update(blockers)

                for blocker in net_blockers:
                    blocker_scores[blocker] = blocker_scores.get(blocker, 0) + 1

        return blocker_scores

    def neighborhood_ripup(
        self,
        failed_nets: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        present_cost_factor: float,
        mark_route_callback: callable,
        stall_count: int = 0,
        per_net_timeout: float | None = None,
        max_attempts: int = 3,
        initial_radius_factor: float = 1.0,
        escalation_factor: float = 2.0,
        ripup_history: dict[int, int] | None = None,
        max_ripups_per_net: int = 5,
    ) -> tuple[bool, int]:
        """Perform neighborhood rip-up for stuck nets (Issue #2274).

        When negotiated routing stalls with 0 conflicts but unrouted nets
        remain, this method:
        1. Uses relaxed A* to identify true blocking nets
        2. Scores blockers by how many stuck nets they block
        3. Rips up highest-scoring blockers and their neighborhood
        4. Routes the stuck net first, then re-routes displaced nets

        The rip-up radius escalates on repeated stalls.

        Args:
            failed_nets: Nets that failed to route.
            net_routes: Dict of net_id -> list of routes.
            routes_list: Master route list.
            pads_by_net: Dict of net_id -> list of pads.
            present_cost_factor: Current congestion cost factor.
            mark_route_callback: Callback to mark routes on the grid.
            stall_count: How many consecutive stalls have occurred.
            per_net_timeout: Optional per-net A* timeout.
            max_attempts: Maximum rip-up attempts per call.
            initial_radius_factor: Initial bounding-box expansion factor.
            escalation_factor: Multiplier applied to radius on each stall.
            ripup_history: Optional dict tracking per-net rip-up counts.
            max_ripups_per_net: Maximum rip-ups per net to prevent loops.

        Returns:
            Tuple of (improved, new_routed_count) where improved is True if
            more nets were routed than before.
        """
        if ripup_history is None:
            ripup_history = {}

        def _count_routed() -> int:
            """Count nets that have at least one route."""
            return sum(1 for routes in net_routes.values() if routes)

        initial_routed = _count_routed()
        radius_factor = initial_radius_factor * (escalation_factor ** stall_count)
        attempts = 0

        # Step 1: Find true blockers via relaxed A*
        blocker_scores = self.find_blocking_nets_relaxed(
            failed_nets, pads_by_net, per_net_timeout=per_net_timeout,
        )

        if not blocker_scores:
            # No relaxed path found for any stuck net -- truly unroutable
            return False, _count_routed()

        # Sort blockers by score descending (block the most stuck nets first)
        sorted_blockers = sorted(blocker_scores.items(), key=lambda x: -x[1])

        for blocker_net, score in sorted_blockers:
            if attempts >= max_attempts:
                break

            # Check ripup budget
            if ripup_history.get(blocker_net, 0) >= max_ripups_per_net:
                continue

            attempts += 1
            ripup_history[blocker_net] = ripup_history.get(blocker_net, 0) + 1

            # Compute bounding box around the blocker's route and expand by radius
            blocker_routes = net_routes.get(blocker_net, [])
            if not blocker_routes:
                continue

            # Get bounding box of blocker net's routes
            min_gx = float("inf")
            min_gy = float("inf")
            max_gx = float("-inf")
            max_gy = float("-inf")
            for route in blocker_routes:
                for seg in route.segments:
                    gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
                    gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
                    min_gx = min(min_gx, gx1, gx2)
                    min_gy = min(min_gy, gy1, gy2)
                    max_gx = max(max_gx, gx1, gx2)
                    max_gy = max(max_gy, gy1, gy2)

            # Expand bounding box by radius
            bbox_w = max_gx - min_gx + 1
            bbox_h = max_gy - min_gy + 1
            expand = int(max(bbox_w, bbox_h) * radius_factor)
            bb_x1 = int(min_gx) - expand
            bb_y1 = int(min_gy) - expand
            bb_x2 = int(max_gx) + expand
            bb_y2 = int(max_gy) + expand

            # Find all routed nets passing through the expanded bounding box
            neighborhood_nets: set[int] = {blocker_net}
            for net_id, routes in net_routes.items():
                if net_id in neighborhood_nets:
                    continue
                for route in routes:
                    found = False
                    for seg in route.segments:
                        gx1, gy1 = self.grid.world_to_grid(seg.x1, seg.y1)
                        gx2, gy2 = self.grid.world_to_grid(seg.x2, seg.y2)
                        # Check if segment intersects the bounding box
                        if (
                            max(gx1, gx2) >= bb_x1
                            and min(gx1, gx2) <= bb_x2
                            and max(gy1, gy2) >= bb_y1
                            and min(gy1, gy2) <= bb_y2
                        ):
                            neighborhood_nets.add(net_id)
                            found = True
                            break
                    if found:
                        break

            # Identify which failed nets could benefit from this rip-up
            affected_failed = [
                n for n in failed_nets
                if n not in net_routes or not net_routes.get(n)
            ]

            # Rip up the neighborhood
            self.rip_up_nets(list(neighborhood_nets), net_routes, routes_list)

            # Route the failed nets first (priority)
            for fn in affected_failed:
                fn_pads = pads_by_net.get(fn, [])
                if fn_pads and len(fn_pads) >= 2:
                    routes = self.route_net_negotiated(
                        fn_pads, present_cost_factor, mark_route_callback,
                        per_net_timeout=per_net_timeout,
                    )
                    if routes:
                        net_routes[fn] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            routes_list.append(route)

            # Re-route the displaced nets
            for net_id in neighborhood_nets:
                if net_id in net_routes and net_routes[net_id]:
                    continue  # Already re-routed as a failed net
                net_pads = pads_by_net.get(net_id, [])
                if net_pads and len(net_pads) >= 2:
                    routes = self.route_net_negotiated(
                        net_pads, present_cost_factor, mark_route_callback,
                        per_net_timeout=per_net_timeout,
                    )
                    if routes:
                        net_routes[net_id] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            routes_list.append(route)

            # Check if we improved
            current_routed = _count_routed()
            if current_routed > initial_routed:
                return True, current_routed

        final_routed = _count_routed()
        return final_routed > initial_routed, final_routed

    def escape_local_minimum(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
        strategy_index: int = 0,
        per_net_timeout: float | None = None,
        escape_budget: float | None = None,
    ) -> tuple[bool, int, int]:
        """Try escape strategies when router is stuck oscillating.

        When the negotiated router is stuck cycling between states without
        making progress (detected via detect_oscillation), this method
        tries strategies starting from strategy_index, cycling through all
        remaining strategies until one succeeds or all are exhausted.

        Args:
            overflow_history: List of overflow values from previous iterations
            net_routes: Dictionary of net_id -> list of routes
            routes_list: Master list of all routes
            pads_by_net: Dictionary of net_id -> list of pads
            net_order: List of net IDs in routing priority order
            present_cost_factor: Current congestion cost factor
            mark_route_callback: Callback to mark routes on the grid
            strategy_index: Index of strategy to start from (cycles through available)
            per_net_timeout: Wall-clock timeout in seconds for each per-net A*
                search within escape strategies (Issue #2415)
            escape_budget: Wall-clock budget in seconds for the entire escape
                attempt across all strategies (Issue #2415). None means no limit.

        Returns:
            Tuple of (success, overflow, strategies_tried) where success indicates
            if overflow improved and strategies_tried is how many were attempted
        """
        strategies = [
            self._escape_shuffle_order,
            self._escape_reverse_order,
            self._escape_random_subset,
            self._escape_full_reorder,
        ]

        num_strategies = len(strategies)
        strategies_tried = 0
        escape_start = time.time()

        for i in range(num_strategies):
            # Check escape budget before starting each strategy
            if escape_budget is not None:
                if time.time() - escape_start >= escape_budget:
                    break

            idx = (strategy_index + i) % num_strategies
            strategy = strategies[idx]
            strategies_tried += 1

            success, new_overflow = strategy(
                overflow_history=overflow_history,
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                net_order=net_order,
                present_cost_factor=present_cost_factor,
                mark_route_callback=mark_route_callback,
                per_net_timeout=per_net_timeout,
                escape_budget_start=escape_start,
                escape_budget=escape_budget,
            )

            if success:
                return True, new_overflow, strategies_tried

        # All strategies exhausted (or budget expired) without success
        current_overflow = overflow_history[-1] if overflow_history else 0
        return False, current_overflow, strategies_tried

    def _escape_shuffle_order(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
        per_net_timeout: float | None = None,
        escape_budget_start: float | None = None,
        escape_budget: float | None = None,
    ) -> tuple[bool, int]:
        """Escape strategy: shuffle the net order randomly.

        Sometimes a different routing order can escape local minima by
        giving different nets priority.
        """
        # Get the conflicting nets
        overused = self.grid.find_overused_cells()
        nets_to_reroute = self.find_nets_through_overused_cells(net_routes, overused)

        if not nets_to_reroute:
            return False, overflow_history[-1] if overflow_history else 0

        # Shuffle the order of conflicting nets.
        # Issue #2589: uses the global ``random`` module.  This is the
        # primary source of run-to-run nondeterminism in the negotiated
        # router; ``kct route --seed N`` seeds the global RNG at startup
        # so this shuffle becomes deterministic for the same input.
        shuffled = nets_to_reroute.copy()
        random.shuffle(shuffled)

        # Rip up all conflicting nets
        self.rip_up_nets(shuffled, net_routes, routes_list)

        # Re-route in shuffled order with higher cost
        # Track successful re-routes to detect if any net was lost
        boosted_cost = present_cost_factor * 1.5
        rerouted_count = 0
        expected_count = 0
        for net in shuffled:
            # Check escape budget before each net (Issue #2415)
            if escape_budget is not None and escape_budget_start is not None:
                if time.time() - escape_budget_start >= escape_budget:
                    break
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                expected_count += 1
                routes = self.route_net_negotiated(
                    net_pads, boosted_cost, mark_route_callback,
                    per_net_timeout=per_net_timeout,
                )
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Accept escape if overflow improved and at least some nets survived
        # (Issue #762: require rerouted_count > 0 to avoid false success on total loss)
        # (Issue #1638: relaxed from requiring ALL nets to allow partial progress)
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow

    def _escape_reverse_order(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
        per_net_timeout: float | None = None,
        escape_budget_start: float | None = None,
        escape_budget: float | None = None,
    ) -> tuple[bool, int]:
        """Escape strategy: reverse the net order.

        Routing in reverse order gives previously low-priority nets
        first access to routing resources.
        """
        overused = self.grid.find_overused_cells()
        nets_to_reroute = self.find_nets_through_overused_cells(net_routes, overused)

        if not nets_to_reroute:
            return False, overflow_history[-1] if overflow_history else 0

        # Reverse the order
        reversed_order = list(reversed(nets_to_reroute))

        # Rip up all conflicting nets
        self.rip_up_nets(reversed_order, net_routes, routes_list)

        # Re-route in reversed order
        # Track successful re-routes to detect if any net was lost
        boosted_cost = present_cost_factor * 1.5
        rerouted_count = 0
        expected_count = 0
        for net in reversed_order:
            # Check escape budget before each net (Issue #2415)
            if escape_budget is not None and escape_budget_start is not None:
                if time.time() - escape_budget_start >= escape_budget:
                    break
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                expected_count += 1
                routes = self.route_net_negotiated(
                    net_pads, boosted_cost, mark_route_callback,
                    per_net_timeout=per_net_timeout,
                )
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Accept escape if overflow improved and at least some nets survived
        # (Issue #762: require rerouted_count > 0 to avoid false success on total loss)
        # (Issue #1638: relaxed from requiring ALL nets to allow partial progress)
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow

    def _escape_random_subset(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
        per_net_timeout: float | None = None,
        escape_budget_start: float | None = None,
        escape_budget: float | None = None,
    ) -> tuple[bool, int]:
        """Escape strategy: rip up and reroute a random subset of nets.

        By ripping up only a subset of conflicting nets, we may find
        a different combination that works.
        """
        overused = self.grid.find_overused_cells()
        nets_to_reroute = self.find_nets_through_overused_cells(net_routes, overused)

        if not nets_to_reroute:
            return False, overflow_history[-1] if overflow_history else 0

        # Select random subset (50-75% of conflicting nets).
        # Issue #2589: uses the global ``random`` module; deterministic
        # when the CLI seeds it via ``kct route --seed N``.
        subset_size = max(1, len(nets_to_reroute) * 2 // 3)
        subset = random.sample(nets_to_reroute, min(subset_size, len(nets_to_reroute)))

        # Rip up subset
        self.rip_up_nets(subset, net_routes, routes_list)

        # Re-route with much higher cost to force different paths
        # Track successful re-routes to detect if any net was lost
        boosted_cost = present_cost_factor * 2.0
        rerouted_count = 0
        expected_count = 0
        for net in subset:
            # Check escape budget before each net (Issue #2415)
            if escape_budget is not None and escape_budget_start is not None:
                if time.time() - escape_budget_start >= escape_budget:
                    break
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                expected_count += 1
                routes = self.route_net_negotiated(
                    net_pads, boosted_cost, mark_route_callback,
                    per_net_timeout=per_net_timeout,
                )
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Accept escape if overflow improved and at least some nets survived
        # (Issue #762: require rerouted_count > 0 to avoid false success on total loss)
        # (Issue #1638: relaxed from requiring ALL nets to allow partial progress)
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow

    def _escape_full_reorder(
        self,
        overflow_history: list[int],
        net_routes: dict[int, list[Route]],
        routes_list: list[Route],
        pads_by_net: dict[int, list[Pad]],
        net_order: list[int],
        present_cost_factor: float,
        mark_route_callback: callable,
        per_net_timeout: float | None = None,
        escape_budget_start: float | None = None,
        escape_budget: float | None = None,
    ) -> tuple[bool, int]:
        """Escape strategy: rip up ALL nets and reroute in alternative order.

        Issue #1823: The existing escape strategies only perturb nets passing
        through overused cells.  If the root ordering places net A before net B,
        and A's path blocks B's only viable route without itself being in an
        overused cell, none of the first three strategies will fix this.

        This strategy rips up every routed net and reroutes them all in a
        completely different order (reversed net_order).  It is more expensive
        but can escape ordering-dependent local minima.
        """
        all_nets = list(net_routes.keys())
        if not all_nets:
            return False, overflow_history[-1] if overflow_history else 0

        # Rip up ALL routed nets
        self.rip_up_nets(all_nets, net_routes, routes_list)

        # Build alternative order: reverse of the priority net_order,
        # then append any nets not in net_order.
        net_set = set(all_nets)
        ordered = [n for n in reversed(net_order) if n in net_set]
        remaining = [n for n in all_nets if n not in set(net_order)]
        # Issue #2589: uses the global ``random`` module; deterministic
        # when the CLI seeds it via ``kct route --seed N``.
        random.shuffle(remaining)
        reorder = ordered + remaining

        # Reroute all nets in the alternative order with boosted cost
        boosted_cost = present_cost_factor * 1.5
        rerouted_count = 0
        for net in reorder:
            # Check escape budget before each net (Issue #2415)
            if escape_budget is not None and escape_budget_start is not None:
                if time.time() - escape_budget_start >= escape_budget:
                    break
            net_pads = pads_by_net.get(net, [])
            if net_pads and len(net_pads) >= 2:
                routes = self.route_net_negotiated(
                    net_pads, boosted_cost, mark_route_callback,
                    per_net_timeout=per_net_timeout,
                )
                if routes:
                    net_routes[net] = routes
                    rerouted_count += 1
                    for route in routes:
                        self.grid.mark_route_usage(route)
                        routes_list.append(route)

        new_overflow = self.grid.get_total_overflow()
        best_overflow = min(overflow_history) if overflow_history else float("inf")

        # Accept if overflow improved and at least some nets survived
        return rerouted_count > 0 and new_overflow < best_overflow, new_overflow
