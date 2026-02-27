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
from .via_conflict import ViaConflictManager, ViaConflictStrategy

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

        Queries the ViaConflictManager to detect vias from other nets
        that block access to this net's pads.

        Args:
            net: Net identifier
            pads: Optional list of pads

        Returns:
            True if via conflicts detected
        """
        if pads is None or not pads:
            return False

        # Initialize via manager lazily if we have a grid
        if self._via_manager is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is None:
                return False
            self._via_manager = ViaConflictManager(grid=grid, rules=self.rules)

        net_id = net if isinstance(net, int) else 0
        for pad in pads:
            conflicts = self._via_manager.find_blocking_vias(
                pad=pad, pad_net=net_id
            )
            if conflicts:
                return True
        return False

    def _route_global(self, net: str | int, pads: list[Pad] | None) -> RoutingResult:
        """Execute global routing strategy.

        Uses the GlobalRouter to find a corridor assignment through the
        region graph, then converts the result to a RoutingResult with
        metrics computed from the corridor waypoints.

        Args:
            net: Net identifier
            pads: Optional list of pads

        Returns:
            RoutingResult from global routing
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
                error_message="Insufficient pads for global routing",
            )

        pad_positions = [(p.x, p.y) for p in pads]
        net_id = net if isinstance(net, int) else abs(hash(str(net))) % 100000 + 1

        assignment = self.global_router.route_net(net_id, pad_positions)

        if assignment is None:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
                error_message="Global router failed to find corridor assignment",
                alternative_strategies=self._suggest_alternatives(
                    RoutingStrategy.GLOBAL_WITH_REPAIR
                ),
            )

        # Calculate total length from corridor waypoints
        total_length = 0.0
        waypoints = assignment.waypoint_coords
        for i in range(len(waypoints) - 1):
            dx = waypoints[i + 1][0] - waypoints[i][0]
            dy = waypoints[i + 1][1] - waypoints[i][1]
            total_length += math.sqrt(dx * dx + dy * dy)

        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            metrics=RoutingMetrics(
                total_length_mm=total_length,
                via_count=0,
                layer_changes=0,
            ),
        )

    def _route_escape_then_global(
        self, net: str | int, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute escape routing followed by global routing.

        Phase 1: Uses EscapeRouter to generate escape routes for dense
        packages, freeing inner pins for routing.
        Phase 2: Uses GlobalRouter to route the remaining connections.

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult combining escape and global routing
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.ESCAPE_THEN_GLOBAL,
                error_message="Insufficient pads for escape routing",
            )

        escape_count = 0
        escape_vias = 0
        escape_segments_list: list = []
        escape_vias_list: list = []

        # Phase 1: Escape routing
        if self._escape is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is not None:
                self._escape = EscapeRouter(grid=grid, rules=self.rules)

        if self._escape is not None:
            package_info = self._escape.analyze_package(pads)
            if package_info.is_dense:
                escape_routes = self._escape.generate_escapes(package_info)
                escape_count = len(escape_routes)
                for er in escape_routes:
                    escape_segments_list.extend(er.segments)
                    if er.via is not None:
                        escape_vias_list.append(er.via)
                        escape_vias += 1

                logger.info(
                    "Escape routing: %d escape routes generated (%d with vias)",
                    escape_count,
                    escape_vias,
                )

        # Phase 2: Global routing for remaining connections
        global_result = self._route_global(net, pads)

        # Merge escape + global results
        return RoutingResult(
            success=global_result.success,
            net=net,
            strategy_used=RoutingStrategy.ESCAPE_THEN_GLOBAL,
            segments=escape_segments_list + global_result.segments,
            vias=escape_vias_list + global_result.vias,
            metrics=RoutingMetrics(
                total_length_mm=global_result.metrics.total_length_mm,
                via_count=global_result.metrics.via_count + escape_vias,
                layer_changes=global_result.metrics.layer_changes + escape_vias,
                escape_segments=escape_count,
            ),
        )

    def _route_hierarchical(
        self, net: str | int, intent: NetIntent | None, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute hierarchical routing for differential pairs.

        Uses AdaptiveAutorouter which automatically discovers the optimal
        layer count and routes using negotiated congestion resolution.
        Differential pair intent is passed through to guide routing.

        Args:
            net: Net identifier
            intent: Design intent (should have is_differential=True)
            pads: List of pads

        Returns:
            RoutingResult from hierarchical routing
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                error_message="Insufficient pads for hierarchical routing",
            )

        if self._hierarchical is None:
            width = getattr(self.pcb, "width", 65.0)
            height = getattr(self.pcb, "height", 56.0)

            # Build component data from available pads
            components_by_ref: dict[str, dict] = {}
            net_map: dict[str, int] = {}

            for pad in pads:
                ref = pad.ref or "U1"
                if ref not in components_by_ref:
                    components_by_ref[ref] = {
                        "ref": ref,
                        "x": pad.x,
                        "y": pad.y,
                        "rotation": 0,
                        "pads": [],
                    }
                comp = components_by_ref[ref]
                comp["pads"].append(
                    {
                        "number": pad.pin or str(len(comp["pads"]) + 1),
                        "x": pad.x - comp["x"],
                        "y": pad.y - comp["y"],
                        "net": pad.net_name or f"Net_{pad.net}",
                        "through_hole": pad.through_hole,
                    }
                )
                net_name = pad.net_name or f"Net_{pad.net}"
                if net_name not in net_map:
                    net_map[net_name] = pad.net

            # Skip power nets if differential pair intent
            skip_nets: list[str] = []
            if intent and hasattr(intent, "skip_nets"):
                skip_nets = intent.skip_nets

            self._hierarchical = AdaptiveAutorouter(
                width=width,
                height=height,
                components=list(components_by_ref.values()),
                net_map=net_map,
                rules=self.rules,
                verbose=False,
                skip_nets=skip_nets,
            )

        adaptive_result = self._hierarchical.route()

        # Convert AdaptiveAutorouter result to orchestrator RoutingResult
        total_length = 0.0
        via_count = 0
        all_segments: list = []
        all_vias: list = []

        for route in adaptive_result.routes:
            for seg in route.segments:
                length = math.sqrt(
                    (seg.x2 - seg.x1) ** 2 + (seg.y2 - seg.y1) ** 2
                )
                total_length += length
                all_segments.append(seg)
            via_count += len(route.vias)
            all_vias.extend(route.vias)

        return RoutingResult(
            success=adaptive_result.converged,
            net=net,
            strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
            segments=all_segments,
            vias=all_vias,
            metrics=RoutingMetrics(
                total_length_mm=total_length,
                via_count=via_count,
                layer_changes=via_count,
            ),
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

        Detects vias from other nets that block access to this net's pads,
        resolves them using relocation (falling back to rip-reroute), then
        routes the net using the global router.

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult after via conflict resolution
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.VIA_CONFLICT_RESOLUTION,
                error_message="Insufficient pads for via conflict resolution routing",
            )

        manager = self.via_manager
        if manager is None:
            # No grid available, fall back to global routing
            logger.warning(
                "Via conflict resolution requested but no routing grid available, "
                "falling back to global routing"
            )
            result = self._route_global(net, pads)
            result.strategy_used = RoutingStrategy.VIA_CONFLICT_RESOLUTION
            result.warnings.append(
                "Via conflict resolution unavailable (no routing grid); "
                "used global routing as fallback"
            )
            return result

        # Find all blocking vias for this net's pads
        net_id = net if isinstance(net, int) else 0
        all_conflicts = []
        for pad in pads:
            conflicts = manager.find_blocking_vias(pad=pad, pad_net=net_id)
            all_conflicts.extend(conflicts)

        # Deduplicate conflicts by via position
        seen_positions: set[tuple[float, float]] = set()
        unique_conflicts = []
        for conflict in all_conflicts:
            key = (round(conflict.via.x, 4), round(conflict.via.y, 4))
            if key not in seen_positions:
                seen_positions.add(key)
                unique_conflicts.append(conflict)

        # Resolve conflicts using RELOCATE strategy (with fallback to rip-reroute)
        resolutions = []
        if unique_conflicts:
            resolutions = manager.resolve_conflicts(
                unique_conflicts,
                strategy=ViaConflictStrategy.RELOCATE,
            )
            logger.info(
                "Via conflict resolution: %d conflicts found, %d resolutions attempted",
                len(unique_conflicts),
                len(resolutions),
            )

        # Route the net after conflict resolution
        result = self._route_global(net, pads)
        result.strategy_used = RoutingStrategy.VIA_CONFLICT_RESOLUTION

        # Add via conflict resolution info to result
        successful_resolutions = sum(
            1 for r in resolutions if getattr(r, "success", False)
        )
        if unique_conflicts:
            result.warnings.append(
                f"Via conflict resolution: {len(unique_conflicts)} conflicts found, "
                f"{successful_resolutions} resolved"
            )

        return result

    def _route_full_pipeline(
        self, net: str | int, intent: NetIntent | None, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute complete routing pipeline with all stages.

        Chains all routing strategies in sequence:
        1. Escape routing (if fine-pitch pads detected)
        2. Global routing for coarse path planning
        3. Sub-grid adaptive routing for dense areas
        4. Via conflict resolution (if conflicts detected)
        5. Clearance repair on final result

        Each phase handles failures gracefully — if an early phase fails,
        later phases still attempt routing. Metrics are aggregated across
        all phases.

        Args:
            net: Net identifier
            intent: Optional design intent
            pads: List of pads

        Returns:
            RoutingResult from full pipeline with aggregated metrics
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.FULL_PIPELINE,
                error_message="Insufficient pads for full pipeline routing",
            )

        strategies_used: list[str] = []
        all_segments: list = []
        all_vias: list = []
        all_warnings: list[str] = []
        total_length = 0.0
        total_via_count = 0
        total_layer_changes = 0
        total_escape_segments = 0
        total_repair_actions = 0
        pipeline_success = False

        # Phase 1: Escape routing (if fine-pitch pads detected)
        if self._needs_escape_routing(pads):
            try:
                escape_result = self._route_escape_then_global(net, pads)
                strategies_used.append("escape_then_global")
                if escape_result.success:
                    pipeline_success = True
                    all_segments.extend(escape_result.segments)
                    all_vias.extend(escape_result.vias)
                    total_length += escape_result.metrics.total_length_mm
                    total_via_count += escape_result.metrics.via_count
                    total_layer_changes += escape_result.metrics.layer_changes
                    total_escape_segments += escape_result.metrics.escape_segments
                else:
                    all_warnings.append(
                        f"Escape routing failed: {escape_result.error_message}"
                    )
            except Exception as e:
                logger.warning("Full pipeline: escape routing phase failed: %s", e)
                all_warnings.append(f"Escape routing exception: {e}")
        else:
            # Phase 2: Global routing (when escape routing is not needed)
            try:
                global_result = self._route_global(net, pads)
                strategies_used.append("global")
                if global_result.success:
                    pipeline_success = True
                    all_segments.extend(global_result.segments)
                    all_vias.extend(global_result.vias)
                    total_length += global_result.metrics.total_length_mm
                    total_via_count += global_result.metrics.via_count
                    total_layer_changes += global_result.metrics.layer_changes
                else:
                    all_warnings.append(
                        f"Global routing failed: {global_result.error_message}"
                    )
            except Exception as e:
                logger.warning("Full pipeline: global routing phase failed: %s", e)
                all_warnings.append(f"Global routing exception: {e}")

        # Phase 3: Sub-grid adaptive routing for dense areas
        if self._check_density(pads) > self.density_threshold:
            try:
                subgrid_result = self._route_subgrid_adaptive(net, pads)
                strategies_used.append("subgrid_adaptive")
                if subgrid_result.success:
                    total_escape_segments += subgrid_result.metrics.escape_segments
                else:
                    all_warnings.append(
                        f"Sub-grid adaptive failed: {subgrid_result.error_message}"
                    )
            except Exception as e:
                logger.warning(
                    "Full pipeline: sub-grid adaptive phase failed: %s", e
                )
                all_warnings.append(f"Sub-grid adaptive exception: {e}")

        # Phase 4: Via conflict resolution (if conflicts detected)
        if self.enable_via_conflict_resolution and self._has_via_conflicts(net, pads):
            try:
                via_result = self._route_with_via_resolution(net, pads)
                strategies_used.append("via_conflict_resolution")
                if via_result.success:
                    # Via resolution re-routes, so use its metrics if prior routing
                    # failed or if it produced a better result
                    if not pipeline_success:
                        pipeline_success = True
                        all_segments = list(via_result.segments)
                        all_vias = list(via_result.vias)
                        total_length = via_result.metrics.total_length_mm
                        total_via_count = via_result.metrics.via_count
                        total_layer_changes = via_result.metrics.layer_changes
                all_warnings.extend(via_result.warnings)
            except Exception as e:
                logger.warning(
                    "Full pipeline: via conflict resolution phase failed: %s", e
                )
                all_warnings.append(f"Via conflict resolution exception: {e}")

        # Build the result before clearance repair
        result = RoutingResult(
            success=pipeline_success,
            net=net,
            strategy_used=RoutingStrategy.FULL_PIPELINE,
            segments=all_segments,
            vias=all_vias,
            metrics=RoutingMetrics(
                total_length_mm=total_length,
                via_count=total_via_count,
                layer_changes=total_layer_changes,
                escape_segments=total_escape_segments,
                repair_actions=total_repair_actions,
            ),
            warnings=all_warnings,
        )

        if not pipeline_success:
            result.error_message = "All routing phases failed"
            result.alternative_strategies = self._suggest_alternatives(
                RoutingStrategy.FULL_PIPELINE
            )

        # Phase 5: Clearance repair on final result
        if pipeline_success and self.enable_repair and result.violations:
            try:
                repair_count = self._apply_clearance_repair(result)
                if repair_count > 0:
                    strategies_used.append("clearance_repair")
                    total_repair_actions += repair_count
                    result.metrics.repair_actions = total_repair_actions
            except Exception as e:
                logger.warning(
                    "Full pipeline: clearance repair phase failed: %s", e
                )
                result.warnings.append(f"Clearance repair exception: {e}")

        # Record which strategies were used
        if strategies_used:
            result.warnings.insert(
                0, f"Full pipeline phases: {', '.join(strategies_used)}"
            )

        logger.info(
            "Full pipeline complete: net=%s, success=%s, phases=%s",
            net,
            pipeline_success,
            strategies_used,
        )

        return result

    def _apply_clearance_repair(self, result: RoutingResult) -> int:
        """Apply automatic clearance repair to fix violations.

        Uses ClearanceRepairer to compute minimal displacements for traces
        and vias that violate clearance rules. Requires a PCB file path
        to be available on the PCB object.

        Args:
            result: RoutingResult to repair (modified in place)

        Returns:
            Number of repairs applied
        """
        if not result.violations:
            return 0

        pcb_path = getattr(self.pcb, "path", None)
        if pcb_path is None:
            logger.warning(
                "Clearance repair requested but no PCB file path available"
            )
            return 0

        from ..core.types import Severity
        from ..drc.repair_clearance import ClearanceRepairer
        from ..drc.report import DRCReport
        from ..drc.violation import DRCViolation as DrcViolation
        from ..drc.violation import Location, ViolationType

        # Convert orchestrator violations to DRC report format
        drc_violations = []
        for v in result.violations:
            drc_v = DrcViolation(
                type=ViolationType.from_string(v.violation_type),
                type_str=v.violation_type,
                severity=(
                    Severity.ERROR if v.severity == "error" else Severity.WARNING
                ),
                message=v.description,
                locations=[
                    Location(x_mm=v.location[0], y_mm=v.location[1]),
                ],
                nets=list(v.affected_nets),
            )
            drc_violations.append(drc_v)

        report = DRCReport(
            source_file=str(pcb_path),
            created_at=None,
            pcb_name="",
            violations=drc_violations,
        )

        try:
            repairer = ClearanceRepairer(pcb_path)
            repair_result = repairer.repair_from_report(report)

            # Convert repair nudges to RepairAction objects
            for nudge in repair_result.nudges:
                result.repair_actions.append(
                    RepairAction(
                        action_type="nudge",
                        target=(
                            f"{nudge.object_type} [{nudge.net_name}] "
                            f"at ({nudge.x:.4f}, {nudge.y:.4f})"
                        ),
                        displacement_mm=nudge.displacement_mm,
                        success=True,
                        notes=(
                            f"Clearance {nudge.old_clearance_mm:.4f} "
                            f"-> {nudge.new_clearance_mm:.4f}mm"
                        ),
                    )
                )

            if repair_result.repaired > 0:
                repairer.save()

            return repair_result.repaired

        except Exception as e:
            logger.warning("Clearance repair failed: %s", e)
            return 0

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
                board_width = getattr(self.pcb, "width", 65.0)
                board_height = getattr(self.pcb, "height", 56.0)
                self._region_graph = RegionGraph(
                    board_width=board_width, board_height=board_height
                )
            self._global_router = GlobalRouter(
                region_graph=self._region_graph,
                corridor_width=self.corridor_width,
            )
        return self._global_router

    @property
    def escape_router(self) -> EscapeRouter | None:
        """Lazy-initialized escape router instance.

        Returns None if the PCB does not expose a routing grid.
        """
        if self._escape is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is not None:
                self._escape = EscapeRouter(grid=grid, rules=self.rules)
        return self._escape

    @property
    def via_manager(self) -> ViaConflictManager | None:
        """Lazy-initialized via conflict manager instance.

        Returns None if the PCB does not expose a routing grid.
        """
        if self._via_manager is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is not None:
                self._via_manager = ViaConflictManager(grid=grid, rules=self.rules)
        return self._via_manager
