"""
Routing orchestration layer for coordinating multi-strategy routing.

This module provides the RoutingOrchestrator class that intelligently
selects and sequences routing strategies based on net characteristics,
design intent, and board complexity. It coordinates the various routing
capabilities (global router, hierarchical router, sub-grid router, escape
router, via conflict manager, trace clearance repair) into a unified
workflow.

The orchestrator provides an agent-first API that hides routing complexity
behind a single route_net() call while returning rich feedback for
debugging and optimization.

Example::

    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules

    orchestrator = RoutingOrchestrator(
        pcb=pcb,
        rules=DesignRules(),
        backend="cuda"  # Optional GPU acceleration
    )

    result = orchestrator.route_net(
        net="USB_D+",
        intent=NetIntent(is_differential=True, impedance=90)
    )

    if result.success:
        print(f"Routed with {result.metrics.via_count} vias")
        print(f"Strategy: {result.strategy_used.name}")
    else:
        print(f"Failed: {result.error_message}")
        for alt in result.alternative_strategies:
            print(f"  Try: {alt.strategy.name} - {alt.reason}")
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..design_intent import NetIntent
    from .primitives import PCB, Pad
    from .rules import DesignRules

from .adaptive import AdaptiveAutorouter
from .adaptive_grid import AdaptiveGridRouter, identify_fine_pitch_components
from .escape import EscapeRouter
from .global_router import GlobalRouter
from .region_graph import RegionGraph
from .strategies import (
    AlternativeStrategy,
    PerformanceStats,
    RepairAction,
    RoutingMetrics,
    RoutingResult,
    RoutingStrategy,
)
from .subgrid import SubGridRouter
from .via_conflict import ViaConflictManager

logger = logging.getLogger(__name__)


class RoutingOrchestrator:
    """Intelligently coordinate routing strategies based on net characteristics.

    The orchestrator analyzes net properties (pin pitch, differential pairs,
    dense areas, via conflicts) and automatically selects the optimal routing
    strategy sequence. This provides a single unified API for routing while
    leveraging all available routing capabilities.

    The orchestrator is designed for AI agents and provides rich structured
    feedback rather than simple success/failure boolean returns.

    Usage:
        orchestrator = RoutingOrchestrator(pcb, rules, backend="cuda")
        result = orchestrator.route_net(
            net="USB_D+",
            intent=NetIntent(is_differential=True)
        )

    Args:
        pcb: The PCB object containing board geometry and components
        rules: Design rules for routing (clearances, widths, etc.)
        backend: GPU acceleration backend ("cuda", "metal", "cpu", or None for auto)
        corridor_width: Default corridor width for global router in mm
        density_threshold: Grid cell utilization above this triggers sub-grid routing
        enable_repair: Whether to enable automatic clearance repair
        enable_via_conflict_resolution: Whether to enable via conflict resolution
    """

    def __init__(
        self,
        pcb: PCB,
        rules: DesignRules,
        backend: str | None = None,
        corridor_width: float = 0.5,
        density_threshold: float = 0.7,
        enable_repair: bool = True,
        enable_via_conflict_resolution: bool = True,
    ):
        self.pcb = pcb
        self.rules = rules
        self.backend = backend
        self.corridor_width = corridor_width
        self.density_threshold = density_threshold
        self.enable_repair = enable_repair
        self.enable_via_conflict_resolution = enable_via_conflict_resolution

        # Lazy-initialized routers (created on first use)
        self._global_router: GlobalRouter | None = None
        self._hierarchical: AdaptiveAutorouter | None = None
        self._subgrid: SubGridRouter | None = None
        self._escape: EscapeRouter | None = None
        self._via_manager: ViaConflictManager | None = None
        self._region_graph: RegionGraph | None = None

        logger.info(
            f"RoutingOrchestrator initialized: backend={backend}, "
            f"corridor_width={corridor_width}mm, density_threshold={density_threshold}"
        )

    def route_net(
        self,
        net: str | int,
        intent: NetIntent | None = None,
        pads: list[Pad] | None = None,
    ) -> RoutingResult:
        """Route a net using optimal strategy selection.

        This is the main entry point for the orchestrator. It analyzes the net
        characteristics and design intent, selects the optimal routing strategy,
        executes the routing, and returns rich feedback.

        Args:
            net: Net name or ID to route
            intent: Optional design intent (differential pairs, impedance control, etc.)
            pads: Optional list of pads for this net (if None, extracted from PCB)

        Returns:
            RoutingResult with success status, metrics, and rich feedback
        """
        start_time = time.time()
        perf = PerformanceStats(backend_type=self.backend or "cpu")

        # Phase 1: Strategy selection
        strategy_start = time.time()
        strategy = self._select_strategy(net, intent, pads)
        perf.strategy_selection_ms = (time.time() - strategy_start) * 1000

        logger.info(f"Routing net {net} with strategy {strategy.name}")

        # Phase 2: Execute routing with selected strategy
        routing_start = time.time()
        result = self._execute_strategy(net, strategy, intent, pads)
        perf.routing_ms = (time.time() - routing_start) * 1000

        # Phase 3: Post-route repair (if enabled and needed)
        if self.enable_repair and result.success and len(result.violations) > 0:
            repair_start = time.time()
            repair_count = self._apply_clearance_repair(result)
            perf.repair_ms = (time.time() - repair_start) * 1000
            result.metrics.repair_actions = repair_count

        # Update performance stats
        perf.total_time_ms = (time.time() - start_time) * 1000
        result.performance = perf

        logger.info(
            f"Routing complete: net={net}, strategy={strategy.name}, "
            f"success={result.success}, time={perf.total_time_ms:.1f}ms"
        )

        return result

    def _select_strategy(
        self,
        net: str | int,
        intent: NetIntent | None,
        pads: list[Pad] | None,
    ) -> RoutingStrategy:
        """Analyze net characteristics and select optimal routing strategy.

        Strategy selection heuristics (in priority order):
        1. Fine-pitch escape needed? -> ESCAPE_THEN_GLOBAL
        2. Differential pair? -> HIERARCHICAL_DIFF_PAIR
        3. Dense area (high grid utilization)? -> SUBGRID_ADAPTIVE
        4. Via conflicts detected? -> VIA_CONFLICT_RESOLUTION
        5. Default: GLOBAL_WITH_REPAIR

        Args:
            net: Net identifier
            intent: Optional design intent
            pads: Optional list of pads for this net

        Returns:
            Selected routing strategy
        """
        # Check 1: Fine-pitch escape routing needed?
        if pads and self._needs_escape_routing(pads):
            logger.debug(f"Net {net}: Fine-pitch pads detected, using escape routing")
            return RoutingStrategy.ESCAPE_THEN_GLOBAL

        # Check 2: Differential pair optimization?
        if intent and hasattr(intent, "is_differential") and intent.is_differential:
            logger.debug(f"Net {net}: Differential pair, using hierarchical routing")
            return RoutingStrategy.HIERARCHICAL_DIFF_PAIR

        # Check 3: Dense area requiring sub-grid routing?
        if pads and self._check_density(pads) > self.density_threshold:
            logger.debug(
                f"Net {net}: High density detected, using sub-grid adaptive routing"
            )
            return RoutingStrategy.SUBGRID_ADAPTIVE

        # Check 4: Via conflicts present?
        if self.enable_via_conflict_resolution and self._has_via_conflicts(net, pads):
            logger.debug(f"Net {net}: Via conflicts detected, using conflict resolution")
            return RoutingStrategy.VIA_CONFLICT_RESOLUTION

        # Default: Global router with optional repair
        logger.debug(f"Net {net}: Standard routing with global router")
        return RoutingStrategy.GLOBAL_WITH_REPAIR

    def _execute_strategy(
        self,
        net: str | int,
        strategy: RoutingStrategy,
        intent: NetIntent | None,
        pads: list[Pad] | None,
    ) -> RoutingResult:
        """Execute the selected routing strategy.

        Args:
            net: Net identifier
            strategy: Selected routing strategy
            intent: Optional design intent
            pads: Optional list of pads

        Returns:
            RoutingResult from the strategy execution
        """
        try:
            if strategy == RoutingStrategy.GLOBAL_WITH_REPAIR:
                return self._route_global(net, pads)

            elif strategy == RoutingStrategy.ESCAPE_THEN_GLOBAL:
                return self._route_escape_then_global(net, pads)

            elif strategy == RoutingStrategy.HIERARCHICAL_DIFF_PAIR:
                return self._route_hierarchical(net, intent, pads)

            elif strategy == RoutingStrategy.SUBGRID_ADAPTIVE:
                return self._route_subgrid_adaptive(net, pads)

            elif strategy == RoutingStrategy.VIA_CONFLICT_RESOLUTION:
                return self._route_with_via_resolution(net, pads)

            elif strategy == RoutingStrategy.FULL_PIPELINE:
                return self._route_full_pipeline(net, intent, pads)

            else:
                error_msg = f"Unknown strategy: {strategy}"
                logger.error(error_msg)
                return RoutingResult(
                    success=False,
                    net=net,
                    strategy_used=strategy,
                    error_message=error_msg,
                )

        except Exception as e:
            logger.exception(f"Strategy execution failed: {e}")
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=strategy,
                error_message=f"Routing failed: {str(e)}",
                alternative_strategies=self._suggest_alternatives(strategy),
            )

    def _needs_escape_routing(self, pads: list[Pad]) -> bool:
        """Check if any pads require escape routing (fine-pitch components).

        Args:
            pads: List of pads to analyze

        Returns:
            True if escape routing is needed
        """
        # Check for fine-pitch packages (pitch < 0.8mm typical for escape routing)
        if len(pads) < 2:
            return False

        # Calculate minimum pitch between adjacent pads
        min_pitch = float("inf")
        for i, pad1 in enumerate(pads):
            for pad2 in pads[i + 1 :]:
                dx = pad1.x - pad2.x
                dy = pad1.y - pad2.y
                distance = math.sqrt(dx * dx + dy * dy)
                min_pitch = min(min_pitch, distance)

        # Fine pitch threshold from design rules or default 0.8mm
        threshold = getattr(self.rules, "fine_pitch_threshold", 0.8)
        return min_pitch < threshold

    def _check_density(self, pads: list[Pad]) -> float:
        """Calculate routing density around pads (0.0 to 1.0).

        This is a simplified heuristic. In production, this would analyze
        actual grid cell utilization.

        Args:
            pads: List of pads to analyze

        Returns:
            Density metric (0.0 = sparse, 1.0 = very dense)
        """
        if len(pads) < 2:
            return 0.0

        # Calculate bounding box area
        min_x = min(p.x for p in pads)
        max_x = max(p.x for p in pads)
        min_y = min(p.y for p in pads)
        max_y = max(p.y for p in pads)

        area = (max_x - min_x) * (max_y - min_y)
        if area == 0:
            return 0.0

        # Estimate density as pads per square mm
        # This is a placeholder - real implementation would check grid utilization
        density = len(pads) / area
        return min(density / 10.0, 1.0)  # Normalize to 0-1 range

    def _has_via_conflicts(self, net: str | int, pads: list[Pad] | None) -> bool:
        """Check if there are existing via conflicts for this net.

        Args:
            net: Net identifier
            pads: Optional list of pads

        Returns:
            True if via conflicts detected
        """
        # Placeholder - real implementation would check via manager
        return False

    def _route_global(self, net: str | int, pads: list[Pad] | None) -> RoutingResult:
        """Execute global routing strategy.

        Args:
            net: Net identifier
            pads: Optional list of pads

        Returns:
            RoutingResult from global routing
        """
        # Placeholder implementation
        # Real implementation would instantiate and use GlobalRouter
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            metrics=RoutingMetrics(
                total_length_mm=10.5,
                via_count=2,
                layer_changes=1,
            ),
            warnings=["Global routing: placeholder implementation"],
        )

    def _route_escape_then_global(
        self, net: str | int, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute escape routing followed by global routing.

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult combining escape and global routing
        """
        # Placeholder implementation
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.ESCAPE_THEN_GLOBAL,
            metrics=RoutingMetrics(
                total_length_mm=12.0,
                via_count=3,
                layer_changes=2,
                escape_segments=4,
            ),
            warnings=["Escape routing: placeholder implementation"],
        )

    def _route_hierarchical(
        self, net: str | int, intent: NetIntent | None, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute hierarchical routing for differential pairs.

        Args:
            net: Net identifier
            intent: Design intent (should have is_differential=True)
            pads: List of pads

        Returns:
            RoutingResult from hierarchical routing
        """
        # Placeholder implementation
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
            metrics=RoutingMetrics(
                total_length_mm=15.0,
                via_count=2,
                layer_changes=1,
            ),
            warnings=["Hierarchical routing: placeholder implementation"],
        )

    def _route_subgrid_adaptive(
        self, net: str | int, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute sub-grid adaptive routing for dense areas.

        Uses AdaptiveGridRouter for two-phase routing:
        Phase 1: Fine-grid escape routing for off-grid pads
        Phase 2: Coarse-grid channel routing for all connections

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult from adaptive grid routing
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.SUBGRID_ADAPTIVE,
                error_message="No pads provided for sub-grid adaptive routing",
            )

        # Check if any pads are from fine-pitch components
        fine_components = identify_fine_pitch_components(
            pads,
            coarse_resolution=getattr(self.rules, "grid_resolution", 0.1),
        )

        escape_count = 0
        if fine_components:
            # Initialize sub-grid router for escape phase
            if self._subgrid is None:
                self._subgrid = SubGridRouter(
                    grid=self.pcb.grid if hasattr(self.pcb, "grid") else None,
                    rules=self.rules,
                )

            if self._subgrid.grid is not None:
                fine_pads = [p for p in pads if p.ref in fine_components]
                if fine_pads:
                    subgrid_result = self._subgrid.route_with_subgrid(fine_pads)
                    escape_count = subgrid_result.success_count

            logger.info(
                "Sub-grid adaptive: %d fine-pitch components, %d escapes generated",
                len(fine_components),
                escape_count,
            )

        # Phase 2 would be handled by the caller's main routing loop
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.SUBGRID_ADAPTIVE,
            metrics=RoutingMetrics(
                escape_segments=escape_count,
            ),
        )

    def _route_with_via_resolution(
        self, net: str | int, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute routing with via conflict resolution.

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult after via conflict resolution
        """
        # Placeholder implementation
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.VIA_CONFLICT_RESOLUTION,
            metrics=RoutingMetrics(
                total_length_mm=11.0,
                via_count=2,
                layer_changes=1,
            ),
            warnings=["Via conflict resolution: placeholder implementation"],
        )

    def _route_full_pipeline(
        self, net: str | int, intent: NetIntent | None, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute complete routing pipeline with all stages.

        Args:
            net: Net identifier
            intent: Optional design intent
            pads: List of pads

        Returns:
            RoutingResult from full pipeline
        """
        # Placeholder implementation
        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.FULL_PIPELINE,
            metrics=RoutingMetrics(
                total_length_mm=14.0,
                via_count=3,
                layer_changes=2,
                escape_segments=2,
                repair_actions=1,
            ),
            warnings=["Full pipeline: placeholder implementation"],
        )

    def _apply_clearance_repair(self, result: RoutingResult) -> int:
        """Apply automatic clearance repair to fix violations.

        Args:
            result: RoutingResult to repair (modified in place)

        Returns:
            Number of repairs applied
        """
        # Placeholder - real implementation would use ClearanceRepairer
        repairs_applied = len(result.violations)

        if repairs_applied > 0:
            result.repair_actions.append(
                RepairAction(
                    action_type="nudge",
                    target=f"{repairs_applied} clearance violations",
                    displacement_mm=0.05,
                    success=True,
                    notes="Placeholder: automatic clearance repair",
                )
            )

        return repairs_applied

    def _suggest_alternatives(self, failed_strategy: RoutingStrategy) -> list[AlternativeStrategy]:
        """Suggest alternative strategies when a strategy fails.

        Args:
            failed_strategy: The strategy that failed

        Returns:
            List of alternative strategies to try
        """
        alternatives = []

        # If global routing failed, try hierarchical
        if failed_strategy == RoutingStrategy.GLOBAL_WITH_REPAIR:
            alternatives.append(
                AlternativeStrategy(
                    strategy=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                    reason="Multi-resolution routing may find path where global failed",
                    estimated_cost=1.5,
                    success_probability=0.6,
                )
            )

        # Always suggest full pipeline as last resort
        if failed_strategy != RoutingStrategy.FULL_PIPELINE:
            alternatives.append(
                AlternativeStrategy(
                    strategy=RoutingStrategy.FULL_PIPELINE,
                    reason="Complete pipeline applies all routing techniques",
                    estimated_cost=2.0,
                    success_probability=0.7,
                )
            )

        return alternatives

    @property
    def global_router(self) -> GlobalRouter:
        """Lazy-initialized global router instance."""
        if self._global_router is None:
            if self._region_graph is None:
                # Placeholder - real implementation would extract board dimensions
                self._region_graph = RegionGraph(board_width=65, board_height=56)
            self._global_router = GlobalRouter(
                region_graph=self._region_graph,
                corridor_width=self.corridor_width,
            )
        return self._global_router

    @property
    def escape_router(self) -> EscapeRouter:
        """Lazy-initialized escape router instance."""
        if self._escape is None:
            # Placeholder - real implementation would need grid and rules
            pass
        return self._escape  # type: ignore

    @property
    def via_manager(self) -> ViaConflictManager:
        """Lazy-initialized via conflict manager instance."""
        if self._via_manager is None:
            # Placeholder - real implementation would need grid and rules
            pass
        return self._via_manager  # type: ignore
