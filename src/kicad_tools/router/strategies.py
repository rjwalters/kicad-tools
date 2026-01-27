"""
Routing strategy enums and result types for the orchestration layer.

This module defines the routing strategies that can be selected by the
orchestrator, as well as rich result types that provide actionable feedback
to agents and users.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Segment, Via


class RoutingStrategy(Enum):
    """Available routing strategies that the orchestrator can select."""

    GLOBAL_WITH_REPAIR = auto()
    """Global router with optional clearance repair post-processing."""

    ESCAPE_THEN_GLOBAL = auto()
    """Escape routing for fine-pitch components, then global routing."""

    HIERARCHICAL_DIFF_PAIR = auto()
    """Hierarchical routing optimized for differential pairs."""

    SUBGRID_ADAPTIVE = auto()
    """Sub-grid routing for dense areas with adaptive grid refinement."""

    VIA_CONFLICT_RESOLUTION = auto()
    """Via conflict resolution followed by standard routing."""

    FULL_PIPELINE = auto()
    """Complete pipeline: escape → global → sub-grid → via resolution → repair."""


@dataclass
class RoutingMetrics:
    """Quantitative metrics for a routing result.

    Attributes:
        total_length_mm: Total trace length in millimeters
        via_count: Number of vias used
        layer_changes: Number of layer transitions
        clearance_margin_mm: Minimum clearance margin (clearance - min_spacing)
        grid_points_used: Number of routing grid cells occupied
        escape_segments: Number of escape segments generated
        repair_actions: Number of clearance repairs applied
    """

    total_length_mm: float = 0.0
    via_count: int = 0
    layer_changes: int = 0
    clearance_margin_mm: float = 0.0
    grid_points_used: int = 0
    escape_segments: int = 0
    repair_actions: int = 0


@dataclass
class PerformanceStats:
    """Performance statistics for a routing operation.

    Attributes:
        total_time_ms: Total routing time in milliseconds
        strategy_selection_ms: Time spent selecting strategy
        routing_ms: Time spent in actual routing algorithm
        repair_ms: Time spent in post-route repair
        gpu_utilized: Whether GPU acceleration was used
        backend_type: Backend type used (e.g., "cuda", "metal", "cpu")
    """

    total_time_ms: float = 0.0
    strategy_selection_ms: float = 0.0
    routing_ms: float = 0.0
    repair_ms: float = 0.0
    gpu_utilized: bool = False
    backend_type: str = "cpu"


@dataclass
class DRCViolation:
    """A design rule check violation found during or after routing.

    Attributes:
        violation_type: Type of violation (e.g., "clearance", "track_width")
        severity: Severity level ("error", "warning")
        location: (x, y) coordinates of the violation
        description: Human-readable description
        affected_nets: List of net names involved
    """

    violation_type: str
    severity: str
    location: tuple[float, float]
    description: str
    affected_nets: list[str] = field(default_factory=list)


@dataclass
class RepairAction:
    """A repair action taken to fix a violation.

    Attributes:
        action_type: Type of repair ("nudge", "reroute", "via_relocation")
        target: Description of what was repaired
        displacement_mm: Distance moved for nudge actions
        success: Whether the repair succeeded
        notes: Additional context about the repair
    """

    action_type: str
    target: str
    displacement_mm: float = 0.0
    success: bool = True
    notes: str = ""


@dataclass
class AlternativeStrategy:
    """An alternative routing strategy that could be tried.

    The orchestrator may suggest alternatives when the selected strategy
    fails or produces suboptimal results.

    Attributes:
        strategy: The alternative strategy enum
        reason: Why this alternative might work better
        estimated_cost: Relative cost estimate (higher = more expensive)
        success_probability: Estimated probability of success (0.0 to 1.0)
    """

    strategy: RoutingStrategy
    reason: str
    estimated_cost: float = 1.0
    success_probability: float = 0.5


@dataclass
class RoutingResult:
    """Complete result of a routing operation with rich feedback.

    This is the primary return type from the orchestrator. It includes
    not just success/failure, but detailed metrics, performance stats,
    violations, repairs, and suggestions for alternatives.

    Attributes:
        success: Whether routing succeeded
        net: Net identifier (name or ID)
        strategy_used: The strategy that was applied
        segments: List of trace segments created
        vias: List of vias created
        metrics: Quantitative routing metrics
        performance: Performance statistics
        violations: List of DRC violations found (if any)
        repair_actions: List of repairs applied (if any)
        alternative_strategies: Alternative strategies to try if this failed
        error_message: Error description if success=False
        warnings: Non-fatal warnings about the routing
    """

    success: bool
    net: str | int
    strategy_used: RoutingStrategy
    segments: list[Segment] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    metrics: RoutingMetrics = field(default_factory=RoutingMetrics)
    performance: PerformanceStats = field(default_factory=PerformanceStats)
    violations: list[DRCViolation] = field(default_factory=list)
    repair_actions: list[RepairAction] = field(default_factory=list)
    alternative_strategies: list[AlternativeStrategy] = field(default_factory=list)
    error_message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization (e.g., MCP tools).

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        return {
            "success": self.success,
            "net": str(self.net),
            "strategy_used": self.strategy_used.name,
            "metrics": {
                "total_length_mm": self.metrics.total_length_mm,
                "via_count": self.metrics.via_count,
                "layer_changes": self.metrics.layer_changes,
                "clearance_margin_mm": self.metrics.clearance_margin_mm,
                "grid_points_used": self.metrics.grid_points_used,
                "escape_segments": self.metrics.escape_segments,
                "repair_actions": self.metrics.repair_actions,
            },
            "performance": {
                "total_time_ms": self.performance.total_time_ms,
                "strategy_selection_ms": self.performance.strategy_selection_ms,
                "routing_ms": self.performance.routing_ms,
                "repair_ms": self.performance.repair_ms,
                "gpu_utilized": self.performance.gpu_utilized,
                "backend_type": self.performance.backend_type,
            },
            "violations": [
                {
                    "type": v.violation_type,
                    "severity": v.severity,
                    "location": v.location,
                    "description": v.description,
                    "affected_nets": v.affected_nets,
                }
                for v in self.violations
            ],
            "repair_actions": [
                {
                    "action_type": r.action_type,
                    "target": r.target,
                    "displacement_mm": r.displacement_mm,
                    "success": r.success,
                    "notes": r.notes,
                }
                for r in self.repair_actions
            ],
            "alternative_strategies": [
                {
                    "strategy": a.strategy.name,
                    "reason": a.reason,
                    "estimated_cost": a.estimated_cost,
                    "success_probability": a.success_probability,
                }
                for a in self.alternative_strategies
            ],
            "error_message": self.error_message,
            "warnings": self.warnings,
            "segment_count": len(self.segments),
            "via_count": len(self.vias),
        }
