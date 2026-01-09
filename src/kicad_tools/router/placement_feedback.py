"""
Closed-loop placement-routing feedback.

This module implements the feedback loop between routing failures and
placement optimization, enabling automatic recovery from placement-induced
routing failures.
"""

from __future__ import annotations

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
class PlacementFeedbackResult:
    """Result of the placement-routing feedback loop.

    Attributes:
        success: Whether all nets were successfully routed.
        routes: Final list of routes.
        iterations: Number of iterations performed.
        adjustments: List of placement adjustments made.
        failed_nets: Net IDs that remain unrouted.
        total_components_moved: Total number of components moved.
    """

    success: bool
    routes: list[Route]
    iterations: int
    adjustments: list[PlacementAdjustment] = field(default_factory=list)
    failed_nets: list[int] = field(default_factory=list)
    total_components_moved: int = 0

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = [
            "Placement-Routing Feedback Result:",
            f"  Success: {self.success}",
            f"  Iterations: {self.iterations}",
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
    ):
        """Initialize the feedback loop.

        Args:
            router: The autorouter to use.
            pcb: The PCB to modify placement on. If None, only routing
                strategies will be attempted (no placement adjustment).
            verbose: Whether to print progress information.
        """
        self.router = router
        self.pcb = pcb
        self.verbose = verbose
        self._strategy_generator = StrategyGenerator()
        self._strategy_applicator = StrategyApplicator()

    def run(
        self,
        max_adjustments: int = 3,
        use_negotiated: bool = True,
        min_confidence: float = 0.5,
    ) -> PlacementFeedbackResult:
        """Run the placement-routing feedback loop.

        Args:
            max_adjustments: Maximum number of placement adjustments to try.
            use_negotiated: Whether to use negotiated congestion routing.
            min_confidence: Minimum confidence required to apply a strategy.

        Returns:
            PlacementFeedbackResult with the final routing state.
        """
        adjustments: list[PlacementAdjustment] = []
        total_moved = 0

        if self.verbose:
            print("\n=== Placement-Routing Feedback Loop ===")
            print(f"  Max adjustments: {max_adjustments}")
            print(f"  Use negotiated routing: {use_negotiated}")

        for iteration in range(max_adjustments + 1):
            if self.verbose:
                print(f"\n--- Iteration {iteration} ---")

            # Clear previous routes and reset grid
            self._clear_routes()

            # Attempt routing
            if use_negotiated:
                routes = self.router.route_all_negotiated()
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
                if strategy.type in [
                    StrategyType.MOVE_COMPONENT,
                    StrategyType.MOVE_MULTIPLE,
                ]:
                    if strategy.confidence >= min_confidence:
                        # Check if safe to apply
                        if self._strategy_applicator.is_safe_to_apply(strategy, self.pcb):
                            all_strategies.append(strategy)

        if not all_strategies:
            return None

        # Sort by confidence (highest first) and pick the best
        all_strategies.sort(key=lambda s: -s.confidence)
        return all_strategies[0]

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
