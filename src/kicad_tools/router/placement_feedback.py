"""
Closed-loop placement-routing feedback.

This module implements the feedback loop between routing failures and
placement optimization, enabling automatic recovery from placement-induced
routing failures.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.primitives import Route
    from kicad_tools.schema.pcb import PCB

from kicad_tools.recovery import (
    ApplicationResult,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyGenerator,
    StrategyType,
)


def detect_pf_stagnation(
    routed_history: list[int],
    *,
    patience: int = 3,
) -> bool:
    """Return True if the last ``patience`` iterations all match the same routed count.

    This is the placement-feedback-layer analogue of the inner-router rip-up
    cohort stagnation detector added in #2597.  It compares the rolling
    history of "fully routed net count" across consecutive outer
    ``PlacementFeedbackLoop`` iterations and signals stagnation when no
    progress has been made for ``patience`` iterations.

    Need at least ``patience + 1`` entries to make a stagnation call -- the
    first entry establishes a baseline and the next ``patience`` entries
    must all match it.  Returns False on shorter histories.

    The check uses strict equality on the last ``patience + 1`` entries
    (``len(set(window)) == 1``).  A single non-matching entry anywhere in
    the window resets the stagnation signal, even if the most recent
    entries are flat (i.e. a recent improvement followed by flat counts is
    not yet stagnated; the helper waits until ``patience + 1`` flat entries
    line up).

    Args:
        routed_history: Per-iteration fully-routed-net counts, oldest first.
        patience: Number of *consecutive identical follow-up* iterations
            required to declare stagnation.  Default 3 means "baseline + 3
            unchanged iterations" => 4 total entries with the same count.

    Returns:
        True if the most recent ``patience + 1`` entries all share a single
        value; False otherwise (including when history is too short).

    Examples:
        >>> detect_pf_stagnation([46, 46, 46, 46], patience=3)
        True
        >>> detect_pf_stagnation([40, 43, 44, 45], patience=3)
        False
        >>> detect_pf_stagnation([46, 46, 46], patience=3)
        False
        >>> detect_pf_stagnation([46, 46, 46], patience=2)
        True
    """
    if patience < 1:
        return False
    if len(routed_history) < patience + 1:
        return False
    last_window = routed_history[-(patience + 1):]
    return len(set(last_window)) == 1


@dataclass
class PlacementAdjustment:
    """Record of a placement adjustment made during feedback loop.

    Attributes:
        iteration: Which iteration the adjustment was made.
        strategy: The strategy that was applied.
        result: Result of applying the strategy.
        failed_nets_before: Net IDs that failed before this adjustment.
        failed_nets_after: Net IDs that failed after this adjustment.
    """

    iteration: int
    strategy: ResolutionStrategy
    result: ApplicationResult
    failed_nets_before: list[int]
    failed_nets_after: list[int]


@dataclass
class PlacementDiffEntry:
    """A single component placement change observed during feedback.

    Captures the original position (before any feedback adjustments)
    and the final position (after all adjustments).  Used to build the
    ``<output>_placement_diff.json`` artifact that lets a human review
    what moved before accepting the result.
    """

    ref: str
    old_xy: tuple[float, float]
    new_xy: tuple[float, float]
    rotation_delta: float = 0.0

    @property
    def distance_mm(self) -> float:
        """Euclidean distance moved, in mm."""
        dx = self.new_xy[0] - self.old_xy[0]
        dy = self.new_xy[1] - self.old_xy[1]
        return math.hypot(dx, dy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "old_xy": [self.old_xy[0], self.old_xy[1]],
            "new_xy": [self.new_xy[0], self.new_xy[1]],
            "rotation_delta": self.rotation_delta,
            "distance_mm": self.distance_mm,
        }


@dataclass
class PlacementFeedbackResult:
    """Result of the placement-routing feedback loop.

    Attributes:
        success: Whether all nets were successfully routed.
        routes: Final list of routes.
        iterations: Number of iterations performed.
        adjustments: List of placement adjustments made.
        failed_nets: Net IDs that remain unrouted.
        total_components_moved: Total number of components moved.
        placement_diff: Per-component before/after positions for every
            component that moved at any point during the loop.  This is
            distinct from ``adjustments`` (which records each step) --
            ``placement_diff`` collapses multiple moves of the same
            component into a single before/after entry suitable for
            JSON-serializing as a diff artifact.
        exit_reason: Why the outer feedback loop terminated.  One of:

            * ``"pf_converged"`` -- ``failed_nets`` was empty after a
              routing pass; the loop achieved 100% connectivity.
            * ``"pf_max_iter"`` -- the loop reached
              ``max_adjustments + 1`` iterations without converging, or
              hit a non-stagnation early-exit condition (no PCB
              provided, no suitable strategy found).  This is the
              backwards-compatible default so old callers reading the
              field always see something sensible.
            * ``"pf_stagnated"`` -- ``detect_pf_stagnation`` fired:
              fully-routed-net count was unchanged for
              ``stagnation_patience`` consecutive iterations.  See
              #2606.
            * ``"pf_timeout"`` -- the optional outer wall-clock budget
              passed via ``outer_timeout`` was exceeded between
              iterations.

            Symmetric with the ``route_all_negotiated`` callback's
            ``converged``/``stagnated``/``timeout`` strings (#2597) but
            distinguished by the ``pf_`` prefix so callers / CI can tell
            the layers apart.
    """

    success: bool
    routes: list[Route]
    iterations: int
    adjustments: list[PlacementAdjustment] = field(default_factory=list)
    failed_nets: list[int] = field(default_factory=list)
    total_components_moved: int = 0
    placement_diff: list[PlacementDiffEntry] = field(default_factory=list)
    exit_reason: str = "pf_max_iter"

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = [
            "Placement-Routing Feedback Result:",
            f"  Success: {self.success}",
            f"  Iterations: {self.iterations}",
            f"  Exit reason: {self.exit_reason}",
            f"  Routes: {len(self.routes)}",
            f"  Components moved: {self.total_components_moved}",
            f"  Failed nets: {len(self.failed_nets)}",
        ]
        if self.adjustments:
            lines.append("  Adjustments:")
            for adj in self.adjustments:
                lines.append(
                    f"    Iter {adj.iteration}: {adj.result.message} "
                    f"({len(adj.failed_nets_before)} -> {len(adj.failed_nets_after)} failures)"
                )
        return "\n".join(lines)


class PlacementFeedbackLoop:
    """Implements closed-loop placement-routing feedback.

    This class orchestrates the feedback loop between routing failures
    and placement optimization. When routing fails, it:
    1. Analyzes the failures to determine root causes
    2. Generates placement strategies to resolve failures
    3. Applies the best strategy to adjust placement
    4. Clears routes and retries routing
    5. Repeats until success or max iterations reached

    Example::

        from kicad_tools.router import Autorouter

        router = Autorouter(100, 100)
        # ... add components and pads ...

        # Route with automatic placement feedback
        result = router.route_with_placement_feedback(
            pcb=pcb,
            max_adjustments=3,
        )

        if result.success:
            print(f"Routed successfully after {result.iterations} iterations")
        else:
            print(f"Failed to route {len(result.failed_nets)} nets")
    """

    def __init__(
        self,
        router: Autorouter,
        pcb: PCB | None = None,
        verbose: bool = True,
        fixed_refs: set[str] | list[str] | None = None,
        max_movement: float | None = 5.0,
    ):
        """Initialize the feedback loop.

        Args:
            router: The autorouter to use.
            pcb: The PCB to modify placement on. If None, only routing
                strategies will be attempted (no placement adjustment).
            verbose: Whether to print progress information.
            fixed_refs: Optional set/list of component references that
                must NOT move during the feedback loop.  Strategies
                whose ``affected_components`` intersect this set are
                filtered out before application.  Typically populated
                with connectors (J*), mechanically-fixed parts, and any
                IC the caller has hand-placed (e.g. fine-pitch packages
                where the human chose the position).  Default: empty.
            max_movement: Hard cap on per-component movement distance,
                in mm.  Strategies that would move any component by
                more than this distance are filtered out.  Set to None
                to disable the cap.  Default: 5.0mm.
        """
        self.router = router
        self.pcb = pcb
        self.verbose = verbose
        self.fixed_refs: set[str] = set(fixed_refs or [])
        self.max_movement: float | None = max_movement
        self._strategy_generator = StrategyGenerator()
        self._strategy_applicator = StrategyApplicator()
        # Snapshot of original positions, populated lazily the first time
        # a strategy is applied.  Used to build the placement diff.
        self._original_positions: dict[str, tuple[tuple[float, float], float]] = {}

    def run(
        self,
        max_adjustments: int = 3,
        use_negotiated: bool = True,
        min_confidence: float = 0.5,
        timeout: float | None = None,
        per_net_timeout: float | None = None,
    ) -> PlacementFeedbackResult:
        """Run the placement-routing feedback loop.

        Args:
            max_adjustments: Maximum number of placement adjustments to try.
            use_negotiated: Whether to use negotiated congestion routing.
            min_confidence: Minimum confidence required to apply a strategy.
            timeout: Optional total routing budget per iteration, in seconds.
                Forwarded to ``Autorouter.route_all_negotiated`` so each
                re-route inside the loop respects the same wall-time budget
                the caller used for the initial routing pass.  Default: no
                limit.
            per_net_timeout: Optional per-net timeout, in seconds.  Same
                semantics as the equivalent CLI flag.  Default: no limit.

        Returns:
            PlacementFeedbackResult with the final routing state.
        """
        adjustments: list[PlacementAdjustment] = []
        total_moved = 0

        if self.verbose:
            print("\n=== Placement-Routing Feedback Loop ===")
            print(f"  Max adjustments: {max_adjustments}")
            print(f"  Use negotiated routing: {use_negotiated}")
            if self.fixed_refs:
                anchored_str = ", ".join(sorted(self.fixed_refs))
                print(f"  Anchored refs:   {anchored_str}")
            if self.max_movement is not None:
                print(f"  Max movement:    {self.max_movement:.2f}mm")

        # Build kwargs for negotiated routing once -- avoids passing
        # None when the underlying API expects positional defaults and
        # keeps the per-iteration call site readable.
        negotiated_kwargs: dict[str, Any] = {}
        if timeout is not None:
            negotiated_kwargs["timeout"] = timeout
        if per_net_timeout is not None:
            negotiated_kwargs["per_net_timeout"] = per_net_timeout

        for iteration in range(max_adjustments + 1):
            if self.verbose:
                print(f"\n--- Iteration {iteration} ---")

            # Clear previous routes and reset grid
            self._clear_routes()

            # Attempt routing
            if use_negotiated:
                routes = self.router.route_all_negotiated(**negotiated_kwargs)
            else:
                routes = self.router.route_all()

            # Check for failures
            failed_nets = self.router.get_failed_nets()

            if self.verbose:
                routed_count = len(self.router.nets) - len(failed_nets) - 1  # -1 for net 0
                print(f"  Routed: {routed_count}/{len(self.router.nets) - 1} nets")
                print(f"  Failed: {len(failed_nets)} nets")

            # Success - all nets routed
            if not failed_nets:
                if self.verbose:
                    print("\n✓ All nets routed successfully!")
                return PlacementFeedbackResult(
                    success=True,
                    routes=routes,
                    iterations=iteration + 1,
                    adjustments=adjustments,
                    failed_nets=[],
                    total_components_moved=total_moved,
                    placement_diff=self._build_placement_diff(),
                )

            # Check if we can make placement adjustments
            if self.pcb is None:
                if self.verbose:
                    print("  Cannot adjust placement (no PCB provided)")
                break

            if iteration >= max_adjustments:
                if self.verbose:
                    print("  Reached maximum adjustment iterations")
                break

            # Analyze failures and generate strategies
            strategy = self._find_best_placement_strategy(failed_nets, min_confidence)

            if strategy is None:
                if self.verbose:
                    print("  No suitable placement strategy found")
                break

            # Apply the strategy
            if self.verbose:
                print(f"  Applying strategy: {strategy.type.value}")
                print(f"    Confidence: {strategy.confidence:.2f}")
                print(f"    Difficulty: {strategy.difficulty.value}")

            # Snapshot original positions for every component this
            # strategy might touch -- BEFORE applying.  We only record
            # the first observation per ref so subsequent moves of the
            # same ref still diff against the true original.
            self._snapshot_positions(strategy.affected_components)

            result = self._strategy_applicator.apply_strategy(self.pcb, strategy)

            if not result.success:
                if self.verbose:
                    print(f"  Strategy application failed: {result.message}")
                continue

            if self.verbose:
                print(f"  ✓ {result.message}")

            # Record the adjustment
            adjustment = PlacementAdjustment(
                iteration=iteration,
                strategy=strategy,
                result=result,
                failed_nets_before=failed_nets.copy(),
                failed_nets_after=[],  # Will be updated after next routing attempt
            )
            adjustments.append(adjustment)
            total_moved += len(result.components_moved)

        # Final failure analysis
        failed_nets = self.router.get_failed_nets()
        if adjustments:
            adjustments[-1].failed_nets_after = failed_nets

        if self.verbose:
            print("\n=== Feedback Loop Complete ===")
            print(f"  Final failed nets: {len(failed_nets)}")
            print(f"  Total components moved: {total_moved}")
            print(f"  Total iterations: {iteration + 1}")

        return PlacementFeedbackResult(
            success=len(failed_nets) == 0,
            routes=list(self.router.routes),
            iterations=iteration + 1,
            adjustments=adjustments,
            failed_nets=failed_nets,
            total_components_moved=total_moved,
            placement_diff=self._build_placement_diff(),
        )

    def _clear_routes(self) -> None:
        """Clear all routes and reset the routing grid."""
        self.router._reset_for_new_trial()

    def _find_best_placement_strategy(
        self,
        failed_nets: list[int],
        min_confidence: float,
    ) -> ResolutionStrategy | None:
        """Find the best placement strategy to resolve routing failures.

        Args:
            failed_nets: List of net IDs that failed to route.
            min_confidence: Minimum confidence required.

        Returns:
            Best placement strategy, or None if none suitable.
        """
        if self.pcb is None:
            return None

        all_strategies: list[ResolutionStrategy] = []

        # Analyze each failed net and collect strategies
        for net_id in failed_nets[:5]:  # Limit analysis to first 5 failures
            analysis = self.router.analyze_routing_failure(net_id)
            if analysis is None:
                continue

            strategies = self._strategy_generator.generate_strategies(self.pcb, analysis)

            # Filter to placement-related strategies
            for strategy in strategies:
                if strategy.type not in [
                    StrategyType.MOVE_COMPONENT,
                    StrategyType.MOVE_MULTIPLE,
                ]:
                    continue
                if strategy.confidence < min_confidence:
                    continue
                # Reject strategies that touch any anchored ref.
                if self._strategy_touches_fixed_refs(strategy):
                    continue
                # Reject strategies that exceed the max movement budget.
                if not self._strategy_within_movement_budget(strategy):
                    continue
                # Check if safe to apply (board bounds, etc.)
                if not self._strategy_applicator.is_safe_to_apply(strategy, self.pcb):
                    continue
                all_strategies.append(strategy)

        if not all_strategies:
            return None

        # Sort by confidence (highest first) and pick the best
        all_strategies.sort(key=lambda s: -s.confidence)
        return all_strategies[0]

    def _strategy_touches_fixed_refs(self, strategy: ResolutionStrategy) -> bool:
        """Return True if any affected component is in ``fixed_refs``."""
        if not self.fixed_refs:
            return False
        for ref in strategy.affected_components:
            if ref in self.fixed_refs:
                return True
        # Defensive: also check action targets in case affected_components
        # was not populated by a custom generator.
        for action in strategy.actions:
            if action.type == "move" and action.target in self.fixed_refs:
                return True
        return False

    def _strategy_within_movement_budget(self, strategy: ResolutionStrategy) -> bool:
        """Return True if every move action stays within ``max_movement``.

        For any ``move`` action with ``x``/``y`` parameters, computes the
        Euclidean distance from the component's current position to the
        proposed position and checks it against ``self.max_movement``.
        Strategies whose actions exceed the cap are rejected so the
        feedback loop never produces drastic placement changes.
        """
        if self.max_movement is None or self.pcb is None:
            return True
        for action in strategy.actions:
            if action.type != "move":
                continue
            ref = action.target
            new_x = action.params.get("x")
            new_y = action.params.get("y")
            if new_x is None or new_y is None:
                continue
            fp = self._find_footprint(ref)
            if fp is None:
                continue
            old_x, old_y = fp.position[0], fp.position[1]
            distance = math.hypot(new_x - old_x, new_y - old_y)
            if distance > self.max_movement:
                return False
        return True

    def _find_footprint(self, ref: str) -> Any | None:
        """Locate a footprint by reference on the PCB."""
        if self.pcb is None:
            return None
        for fp in getattr(self.pcb, "footprints", []):
            if getattr(fp, "reference", None) == ref:
                return fp
        return None

    def _snapshot_positions(self, refs: list[str]) -> None:
        """Record the original (x, y, rotation) for each ref, once."""
        if self.pcb is None:
            return
        for ref in refs:
            if ref in self._original_positions:
                continue
            fp = self._find_footprint(ref)
            if fp is None:
                continue
            pos = fp.position
            rotation = float(getattr(fp, "rotation", 0.0))
            self._original_positions[ref] = ((pos[0], pos[1]), rotation)

    def _build_placement_diff(self) -> list[PlacementDiffEntry]:
        """Build the placement diff from snapshotted original positions.

        Compares each snapshot against the current PCB state and emits
        a ``PlacementDiffEntry`` for every component whose position or
        rotation actually changed.  Components that the loop touched
        but reverted (or that the strategy applicator failed to move)
        are skipped.
        """
        if self.pcb is None:
            return []
        diff: list[PlacementDiffEntry] = []
        for ref, (old_xy, old_rot) in self._original_positions.items():
            fp = self._find_footprint(ref)
            if fp is None:
                continue
            new_xy = (fp.position[0], fp.position[1])
            new_rot = float(getattr(fp, "rotation", 0.0))
            rotation_delta = new_rot - old_rot
            moved = (
                abs(new_xy[0] - old_xy[0]) > 1e-6
                or abs(new_xy[1] - old_xy[1]) > 1e-6
                or abs(rotation_delta) > 1e-6
            )
            if not moved:
                continue
            diff.append(
                PlacementDiffEntry(
                    ref=ref,
                    old_xy=old_xy,
                    new_xy=new_xy,
                    rotation_delta=rotation_delta,
                )
            )
        # Sort by largest distance first so the most impactful moves
        # appear at the top of the JSON artifact.
        diff.sort(key=lambda e: e.distance_mm, reverse=True)
        return diff

    def analyze_placement_impact(
        self,
        strategy: ResolutionStrategy,
    ) -> dict[str, Any]:
        """Analyze the potential impact of a placement strategy.

        Args:
            strategy: The strategy to analyze.

        Returns:
            Dictionary with impact analysis including:
            - components_affected: Number of components affected
            - nets_affected: List of nets that would need rerouting
            - estimated_improvement: Expected improvement score
            - risks: List of potential risks
        """
        if self.pcb is None:
            return {"error": "No PCB provided"}

        components = strategy.affected_components
        nets = strategy.affected_nets
        risks = [effect.description for effect in strategy.side_effects]

        return {
            "components_affected": len(components),
            "component_refs": components,
            "nets_affected": len(nets),
            "net_names": nets,
            "estimated_improvement": strategy.estimated_improvement,
            "confidence": strategy.confidence,
            "difficulty": strategy.difficulty.value,
            "risks": risks,
        }
