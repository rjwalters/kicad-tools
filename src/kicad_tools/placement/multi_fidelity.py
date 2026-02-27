"""Multi-fidelity evaluation pipeline for placement scoring.

Evaluates placements at different accuracy/cost tradeoffs. The optimizer
starts with cheap evaluations for broad exploration and switches to
expensive evaluations for promising candidates.

Fidelity Levels
---------------

=====  ==========================  ===========  ==============================
Level  Method                      Approx Cost  Use
=====  ==========================  ===========  ==============================
0      HPWL + overlap + boundary   ~1 ms        Broad exploration
1      + DRC clearance checking    ~10 ms       Promising region refinement
2      + Global trial routing      ~100 ms      Routability verification
3      + Full detailed routing     ~1 s         Final validation
=====  ==========================  ===========  ==============================

Usage::

    from kicad_tools.placement.multi_fidelity import (
        evaluate_placement_multifidelity,
        FidelityLevel,
        FidelityConfig,
    )

    result = evaluate_placement_multifidelity(
        placements, component_defs, nets, board, fidelity=FidelityLevel.DRC,
    )
    print(result.score.total, result.fidelity, result.cost)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Callable, Protocol, Sequence

from .cost import (
    BoardOutline,
    ComponentPlacement,
    CostBreakdown,
    CostMode,
    Net,
    PlacementCostConfig,
    PlacementScore,
    _lexicographic_score,
    _weighted_sum_score,
    compute_area,
    compute_boundary_violation,
    compute_overlap,
    compute_wirelength,
)
from .drc import DrcResult, check_placement_drc
from .vector import ComponentDef, PlacedComponent

if TYPE_CHECKING:
    from kicad_tools.router.global_router import GlobalRouter
    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules


# ---------------------------------------------------------------------------
# Fidelity level definitions
# ---------------------------------------------------------------------------


class FidelityLevel(IntEnum):
    """Evaluation fidelity levels, ordered from cheapest to most expensive.

    Each successive level includes all checks from previous levels plus
    additional, more expensive analysis.
    """

    HPWL = 0
    """Fidelity 0: HPWL wirelength + overlap + boundary checks only (~1 ms)."""

    DRC = 1
    """Fidelity 1: Adds DRC courtyard/pad clearance checking (~10 ms)."""

    GLOBAL_ROUTE = 2
    """Fidelity 2: Adds global router routability check (~100 ms)."""

    FULL_ROUTE = 3
    """Fidelity 3: Adds full detailed routing attempt (~1 s)."""


#: Relative cost weights for each fidelity level.  Used by the adaptive
#: selector to budget evaluation effort.
FIDELITY_COST: dict[FidelityLevel, int] = {
    FidelityLevel.HPWL: 1,
    FidelityLevel.DRC: 10,
    FidelityLevel.GLOBAL_ROUTE: 100,
    FidelityLevel.FULL_ROUTE: 1000,
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutabilityResult:
    """Summary of routability analysis from global or full routing.

    Attributes:
        routed_nets: Number of nets successfully routed.
        failed_nets: Number of nets that failed to route.
        routability_ratio: Fraction of nets that routed (0.0--1.0).
        congestion_score: Optional congestion metric from the router.
    """

    routed_nets: int = 0
    failed_nets: int = 0
    routability_ratio: float = 1.0
    congestion_score: float = 0.0


@dataclass(frozen=True)
class FidelityResult:
    """Result of a multi-fidelity placement evaluation.

    Attributes:
        score: Composite placement score (lower is better).
        fidelity: The fidelity level at which this evaluation was performed.
        cost: Relative cost weight of this evaluation.
        wall_time_ms: Actual wall-clock time of the evaluation in milliseconds.
        drc_result: DRC result (populated at fidelity >= 1).
        routability: Routability analysis (populated at fidelity >= 2).
    """

    score: PlacementScore
    fidelity: FidelityLevel
    cost: int
    wall_time_ms: float
    drc_result: DrcResult | None = None
    routability: RoutabilityResult | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FidelityConfig:
    """Configuration for multi-fidelity evaluation.

    Attributes:
        cost_config: Underlying cost function configuration.
        drc_violation_weight: Weight applied to DRC violation distance when
            folded into the composite score at fidelity >= 1.
        routability_weight: Weight applied to (1 - routability_ratio) when
            folded into the composite score at fidelity >= 2.
        footprint_sizes: Optional map from reference to (width, height) for
            the fidelity-0 simple cost functions.  Not needed when using
            ``component_defs`` (fidelity >= 1 uses component defs directly).
    """

    cost_config: PlacementCostConfig = field(default_factory=PlacementCostConfig)
    drc_violation_weight: float = 1e4
    routability_weight: float = 1e3
    footprint_sizes: dict[str, tuple[float, float]] | None = None


# ---------------------------------------------------------------------------
# Adaptive fidelity selection protocol
# ---------------------------------------------------------------------------


class FidelitySelector(Protocol):
    """Protocol for adaptive fidelity selection.

    Implementations decide which fidelity level to use for a given
    evaluation based on the optimizer's state (iteration count,
    current best score, etc.).
    """

    def select_fidelity(
        self,
        iteration: int,
        current_best: PlacementScore | None,
        budget_remaining: float,
    ) -> FidelityLevel:
        """Select the fidelity level for the next evaluation.

        Args:
            iteration: Current optimizer iteration number.
            current_best: Best score found so far (or None on first call).
            budget_remaining: Fraction of total evaluation budget remaining
                (1.0 at start, 0.0 when exhausted).

        Returns:
            The fidelity level to use for the next evaluation.
        """
        ...  # pragma: no cover


class DefaultFidelitySelector:
    """Simple adaptive fidelity selector based on budget thresholds.

    Uses cheap evaluations (fidelity 0) for the majority of the budget,
    switching to higher fidelity as the budget decreases and the search
    converges.

    Args:
        thresholds: Mapping of budget-remaining thresholds to fidelity
            levels.  When the budget drops below a threshold, the
            corresponding fidelity level (or higher) is used.
    """

    def __init__(
        self,
        thresholds: dict[float, FidelityLevel] | None = None,
    ) -> None:
        if thresholds is None:
            thresholds = {
                0.75: FidelityLevel.HPWL,
                0.50: FidelityLevel.DRC,
                0.20: FidelityLevel.GLOBAL_ROUTE,
                0.05: FidelityLevel.FULL_ROUTE,
            }
        # Sort thresholds descending so we check the highest threshold first.
        self._thresholds = sorted(thresholds.items(), reverse=True)

    def select_fidelity(
        self,
        iteration: int,
        current_best: PlacementScore | None,
        budget_remaining: float,
    ) -> FidelityLevel:
        """Select fidelity based on remaining budget.

        Thresholds are sorted ascending and checked from the lowest
        (most expensive fidelity) upward.  The most expensive fidelity
        whose threshold the budget has dropped below is selected.
        If budget_remaining is above all thresholds, the cheapest
        fidelity level is returned.

        With the default thresholds ``{0.75: HPWL, 0.50: DRC,
        0.20: GLOBAL_ROUTE, 0.05: FULL_ROUTE}``:

        - ``budget > 0.75``  -->  ``HPWL``
        - ``0.50 < budget <= 0.75``  -->  ``HPWL``
        - ``0.20 < budget <= 0.50``  -->  ``DRC``
        - ``0.05 < budget <= 0.20``  -->  ``GLOBAL_ROUTE``
        - ``budget <= 0.05``  -->  ``FULL_ROUTE``
        """
        # _thresholds sorted descending: [(0.75, HPWL), (0.50, DRC), ...]
        # We want the *lowest* threshold that budget_remaining is at or below.
        # Walk from lowest threshold upward (reversed = ascending order).
        for threshold, fidelity in reversed(self._thresholds):
            if budget_remaining <= threshold:
                return fidelity
        # Budget is above all thresholds -- use the cheapest fidelity
        return self._thresholds[0][1]


# ---------------------------------------------------------------------------
# Internal evaluation helpers
# ---------------------------------------------------------------------------


def _evaluate_fidelity_0(
    placements: Sequence[ComponentPlacement],
    nets: Sequence[Net],
    board: BoardOutline,
    config: FidelityConfig,
) -> tuple[CostBreakdown, bool]:
    """Fidelity 0: HPWL + overlap + boundary only.

    Uses the simple component-center functions from cost.py.
    """
    wirelength = compute_wirelength(placements, nets)
    overlap = compute_overlap(placements, config.footprint_sizes)
    boundary = compute_boundary_violation(placements, board, config.footprint_sizes)
    area = compute_area(placements)

    breakdown = CostBreakdown(
        wirelength=wirelength,
        overlap=overlap,
        boundary=boundary,
        drc=0.0,
        area=area,
    )
    is_feasible = overlap == 0.0 and boundary == 0.0
    return breakdown, is_feasible


def _evaluate_fidelity_1(
    placements_rich: Sequence[PlacedComponent],
    component_defs: Sequence[ComponentDef],
    nets: Sequence[Net],
    board: BoardOutline,
    config: FidelityConfig,
    design_rules: DesignRules | None,
) -> tuple[CostBreakdown, bool, DrcResult | None]:
    """Fidelity 1: Fidelity 0 + DRC clearance checking.

    Uses the richer PlacedComponent type with transformed pads for DRC.
    Falls back to fidelity 0 DRC score (0) if no design rules provided.
    """
    # First compute fidelity-0 metrics using simple placement types
    simple_placements = [
        ComponentPlacement(
            reference=p.reference,
            x=p.x,
            y=p.y,
            rotation=p.rotation,
        )
        for p in placements_rich
    ]

    wirelength = compute_wirelength(simple_placements, nets)
    overlap = compute_overlap(simple_placements, config.footprint_sizes)
    boundary = compute_boundary_violation(simple_placements, board, config.footprint_sizes)
    area = compute_area(simple_placements)

    drc_result: DrcResult | None = None
    drc_score = 0.0

    if design_rules is not None:
        drc_result = check_placement_drc(
            placements_rich,
            component_defs,
            design_rules,
            nets=nets,
        )
        drc_score = drc_result.total_violation_distance

    breakdown = CostBreakdown(
        wirelength=wirelength,
        overlap=overlap,
        boundary=boundary,
        drc=drc_score,
        area=area,
    )
    is_feasible = overlap == 0.0 and boundary == 0.0 and drc_score == 0.0
    return breakdown, is_feasible, drc_result


def _compute_score(
    breakdown: CostBreakdown,
    is_feasible: bool,
    config: FidelityConfig,
    routability: RoutabilityResult | None = None,
) -> float:
    """Compute the composite scalar score from a cost breakdown.

    Augments the base cost function score with optional routability penalty.
    """
    cost_config = config.cost_config

    if cost_config.mode == CostMode.LEXICOGRAPHIC:
        base = _lexicographic_score(breakdown, cost_config, is_feasible)
    else:
        base = _weighted_sum_score(breakdown, cost_config)

    # Add routability penalty if available
    if routability is not None and routability.routability_ratio < 1.0:
        unrouted_fraction = 1.0 - routability.routability_ratio
        base += config.routability_weight * unrouted_fraction

    return base


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_placement_multifidelity(
    placements: Sequence[ComponentPlacement] | Sequence[PlacedComponent],
    nets: Sequence[Net],
    board: BoardOutline,
    fidelity: FidelityLevel | int = FidelityLevel.HPWL,
    config: FidelityConfig | None = None,
    component_defs: Sequence[ComponentDef] | None = None,
    design_rules: DesignRules | None = None,
    global_router: GlobalRouter | None = None,
    orchestrator: RoutingOrchestrator | None = None,
) -> FidelityResult:
    """Evaluate a placement at the specified fidelity level.

    Each fidelity level includes all checks from lower levels and adds
    progressively more expensive analysis.

    Args:
        placements: Component positions.  For fidelity 0, simple
            :class:`ComponentPlacement` objects suffice.  For fidelity >= 1,
            :class:`PlacedComponent` objects with transformed pads are required.
        nets: Net connectivity for wirelength estimation.
        board: Board outline for boundary checking.
        fidelity: Evaluation fidelity level (0--3).
        config: Multi-fidelity configuration.  Uses defaults if ``None``.
        component_defs: Static component definitions (required for fidelity >= 1).
        design_rules: Design rules for DRC (required for fidelity >= 1).
        global_router: Pre-configured GlobalRouter (required for fidelity >= 2).
        orchestrator: Pre-configured RoutingOrchestrator (required for fidelity 3).

    Returns:
        :class:`FidelityResult` with score, timing, and level-specific details.

    Raises:
        ValueError: If required arguments for the requested fidelity level
            are not provided.
    """
    if config is None:
        config = FidelityConfig()

    fidelity = FidelityLevel(int(fidelity))

    # Validate requirements for each fidelity level
    if fidelity >= FidelityLevel.DRC:
        if component_defs is None:
            raise ValueError("component_defs is required for fidelity >= 1 (DRC)")
        if design_rules is None:
            raise ValueError("design_rules is required for fidelity >= 1 (DRC)")

    if fidelity >= FidelityLevel.GLOBAL_ROUTE:
        if global_router is None:
            raise ValueError("global_router is required for fidelity >= 2 (GLOBAL_ROUTE)")

    if fidelity >= FidelityLevel.FULL_ROUTE:
        if orchestrator is None:
            raise ValueError("orchestrator is required for fidelity >= 3 (FULL_ROUTE)")

    t_start = time.perf_counter()

    drc_result: DrcResult | None = None
    routability: RoutabilityResult | None = None

    if fidelity == FidelityLevel.HPWL:
        # --- Fidelity 0: cheap HPWL + overlap + boundary ---
        # Accept either simple or rich placements at fidelity 0
        simple_placements: Sequence[ComponentPlacement]
        if placements and isinstance(placements[0], PlacedComponent):
            simple_placements = [
                ComponentPlacement(
                    reference=p.reference,  # type: ignore[union-attr]
                    x=p.x,  # type: ignore[union-attr]
                    y=p.y,  # type: ignore[union-attr]
                    rotation=p.rotation,  # type: ignore[union-attr]
                )
                for p in placements
            ]
        else:
            simple_placements = placements  # type: ignore[assignment]

        breakdown, is_feasible = _evaluate_fidelity_0(simple_placements, nets, board, config)

    else:
        # Fidelity >= 1 requires PlacedComponent
        rich_placements: Sequence[PlacedComponent]
        if placements and not isinstance(placements[0], PlacedComponent):
            raise ValueError(
                "PlacedComponent objects (with transformed pads) are required "
                "for fidelity >= 1.  Use placement.vector.decode() to convert."
            )
        rich_placements = placements  # type: ignore[assignment]
        assert component_defs is not None  # validated above

        # --- Fidelity 1: + DRC ---
        breakdown, is_feasible, drc_result = _evaluate_fidelity_1(
            rich_placements,
            component_defs,
            nets,
            board,
            config,
            design_rules,
        )

        # --- Fidelity 2: + global routing ---
        if fidelity >= FidelityLevel.GLOBAL_ROUTE and global_router is not None:
            routability = _evaluate_global_routing(global_router, nets)
            # Update feasibility if routing failed
            if routability.routability_ratio < 1.0:
                is_feasible = False

        # --- Fidelity 3: + full detailed routing ---
        if fidelity >= FidelityLevel.FULL_ROUTE and orchestrator is not None:
            full_routability = _evaluate_full_routing(orchestrator, nets)
            # Full routing result replaces global routing result
            routability = full_routability
            if routability.routability_ratio < 1.0:
                is_feasible = False

    total = _compute_score(breakdown, is_feasible, config, routability)

    score = PlacementScore(
        total=total,
        breakdown=breakdown,
        is_feasible=is_feasible,
    )

    t_end = time.perf_counter()
    wall_time_ms = (t_end - t_start) * 1000.0

    return FidelityResult(
        score=score,
        fidelity=fidelity,
        cost=FIDELITY_COST[fidelity],
        wall_time_ms=wall_time_ms,
        drc_result=drc_result,
        routability=routability,
    )


def _evaluate_global_routing(
    global_router: GlobalRouter,
    nets: Sequence[Net],
) -> RoutabilityResult:
    """Run global routing and summarize routability.

    Converts placement-level Net objects into the integer net IDs expected
    by the GlobalRouter, then runs route_all and summarizes results.
    """
    # GlobalRouter.route_all expects a list of integer net IDs and pad info.
    # For the placement evaluator, we do a simplified routability check:
    # we count nets and check what fraction the global router can assign
    # corridors for.
    try:
        # Build net_ids from the sequential index of each net
        net_ids = list(range(len(nets)))
        result = global_router.route_all(net_ids, [])

        total = len(nets)
        failed = len(result.failed_nets)
        routed = total - failed

        ratio = routed / total if total > 0 else 1.0

        return RoutabilityResult(
            routed_nets=routed,
            failed_nets=failed,
            routability_ratio=ratio,
            congestion_score=0.0,
        )
    except Exception:
        # If global routing fails entirely, report zero routability
        return RoutabilityResult(
            routed_nets=0,
            failed_nets=len(nets),
            routability_ratio=0.0,
            congestion_score=1.0,
        )


def _evaluate_full_routing(
    orchestrator: RoutingOrchestrator,
    nets: Sequence[Net],
) -> RoutabilityResult:
    """Run full detailed routing and summarize routability.

    Attempts to route each net through the orchestrator and reports the
    fraction of nets that route successfully.
    """
    total = len(nets)
    if total == 0:
        return RoutabilityResult(
            routed_nets=0,
            failed_nets=0,
            routability_ratio=1.0,
        )

    routed = 0
    failed = 0

    for net in nets:
        try:
            result = orchestrator.route_net(net=net.name)
            if result.success:
                routed += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    ratio = routed / total if total > 0 else 1.0

    return RoutabilityResult(
        routed_nets=routed,
        failed_nets=failed,
        routability_ratio=ratio,
    )


# ---------------------------------------------------------------------------
# Convenience: fixed-fidelity evaluation function
# ---------------------------------------------------------------------------


def make_fixed_fidelity_evaluator(
    fidelity: FidelityLevel | int,
    nets: Sequence[Net],
    board: BoardOutline,
    config: FidelityConfig | None = None,
    component_defs: Sequence[ComponentDef] | None = None,
    design_rules: DesignRules | None = None,
    global_router: GlobalRouter | None = None,
    orchestrator: RoutingOrchestrator | None = None,
) -> Callable[[Sequence[ComponentPlacement] | Sequence[PlacedComponent]], FidelityResult]:
    """Create a fixed-fidelity evaluator closure for use with optimizers.

    Returns a callable that accepts only placements and returns a
    :class:`FidelityResult`.  All other parameters are captured in the
    closure.

    Args:
        fidelity: Fixed fidelity level for all evaluations.
        nets: Net connectivity.
        board: Board outline.
        config: Multi-fidelity configuration.
        component_defs: Component definitions (fidelity >= 1).
        design_rules: Design rules (fidelity >= 1).
        global_router: Global router instance (fidelity >= 2).
        orchestrator: Routing orchestrator (fidelity >= 3).

    Returns:
        A callable ``(placements) -> FidelityResult``.
    """

    def _evaluate(
        placements: Sequence[ComponentPlacement] | Sequence[PlacedComponent],
    ) -> FidelityResult:
        return evaluate_placement_multifidelity(
            placements=placements,
            nets=nets,
            board=board,
            fidelity=fidelity,
            config=config,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=global_router,
            orchestrator=orchestrator,
        )

    return _evaluate


def make_adaptive_evaluator(
    selector: FidelitySelector,
    nets: Sequence[Net],
    board: BoardOutline,
    config: FidelityConfig | None = None,
    component_defs: Sequence[ComponentDef] | None = None,
    design_rules: DesignRules | None = None,
    global_router: GlobalRouter | None = None,
    orchestrator: RoutingOrchestrator | None = None,
    total_budget: float = 1.0,
) -> Callable[
    [Sequence[ComponentPlacement] | Sequence[PlacedComponent], int],
    FidelityResult,
]:
    """Create an adaptive evaluator that selects fidelity per-call.

    The returned callable accepts placements and an iteration number.
    It tracks evaluation budget spent and delegates fidelity selection
    to the provided :class:`FidelitySelector`.

    Args:
        selector: Adaptive fidelity selection strategy.
        nets: Net connectivity.
        board: Board outline.
        config: Multi-fidelity configuration.
        component_defs: Component definitions (fidelity >= 1).
        design_rules: Design rules (fidelity >= 1).
        global_router: Global router instance (fidelity >= 2).
        orchestrator: Routing orchestrator (fidelity >= 3).
        total_budget: Total evaluation budget in abstract cost units.

    Returns:
        A callable ``(placements, iteration) -> FidelityResult``.
    """
    # Mutable state shared across calls via list wrappers
    _budget_spent = [0.0]
    _best_score: list[PlacementScore | None] = [None]

    def _evaluate(
        placements: Sequence[ComponentPlacement] | Sequence[PlacedComponent],
        iteration: int,
    ) -> FidelityResult:
        budget_remaining = max(0.0, 1.0 - _budget_spent[0] / total_budget)

        fidelity = selector.select_fidelity(
            iteration=iteration,
            current_best=_best_score[0],
            budget_remaining=budget_remaining,
        )

        result = evaluate_placement_multifidelity(
            placements=placements,
            nets=nets,
            board=board,
            fidelity=fidelity,
            config=config,
            component_defs=component_defs,
            design_rules=design_rules,
            global_router=global_router,
            orchestrator=orchestrator,
        )

        # Update state
        _budget_spent[0] += FIDELITY_COST[result.fidelity]
        if _best_score[0] is None or result.score.total < _best_score[0].total:
            _best_score[0] = result.score

        return result

    return _evaluate
