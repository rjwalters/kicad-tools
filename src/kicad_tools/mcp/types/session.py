"""Session management types for MCP tools.

Provides dataclasses for placement session management including
moves, commits, rollbacks, and session status.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .drc_delta import DRCDeltaInfo, DRCSummary
from .intent import IntentStatus
from .warnings import PredictiveWarningInfo


@dataclass
class SessionInfo:
    """Information about an active placement session.

    Provides metadata and statistics about a placement session
    for monitoring and debugging purposes.

    Attributes:
        id: Unique session identifier (8-character UUID prefix).
        pcb_path: Path to the PCB file being edited.
        created_at: ISO 8601 timestamp when session was created.
        last_accessed: ISO 8601 timestamp when session was last accessed.
        pending_moves: Number of uncommitted component moves.
        components: Total number of components in the session.
        current_score: Current placement quality score (lower is better).
    """

    id: str
    pcb_path: str
    created_at: str  # ISO 8601 timestamp
    last_accessed: str  # ISO 8601 timestamp
    pending_moves: int
    components: int
    current_score: float

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "pcb_path": self.pcb_path,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "pending_moves": self.pending_moves,
            "components": self.components,
            "current_score": round(self.current_score, 4),
        }


@dataclass
class ComponentPosition:
    """Position information for a component.

    Attributes:
        ref: Component reference designator (e.g., "C1", "R5")
        x: X position in millimeters
        y: Y position in millimeters
        rotation: Rotation in degrees
        fixed: Whether component is fixed/locked
    """

    ref: str
    x: float
    y: float
    rotation: float
    fixed: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "ref": self.ref,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "rotation": round(self.rotation, 1),
            "fixed": self.fixed,
        }


@dataclass
class RoutingImpactInfo:
    """Routing impact information for a move.

    Attributes:
        affected_nets: List of nets affected by the move
        estimated_length_change_mm: Estimated change in routing length
        crossing_changes: Change in net crossing count
    """

    affected_nets: list[str] = field(default_factory=list)
    estimated_length_change_mm: float = 0.0
    crossing_changes: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "affected_nets": self.affected_nets,
            "estimated_length_change_mm": round(self.estimated_length_change_mm, 3),
            "crossing_changes": self.crossing_changes,
        }


@dataclass
class ViolationInfo:
    """Information about a placement constraint violation.

    Attributes:
        type: Violation type (e.g., "clearance", "overlap", "boundary")
        description: Human-readable description
        severity: Severity level ("error", "warning", "info")
        component: Component reference if applicable
        location: (x, y) location if applicable
    """

    type: str
    description: str
    severity: str = "error"
    component: str = ""
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "type": self.type,
            "description": self.description,
            "severity": self.severity,
            "component": self.component,
            "location": list(self.location) if self.location else None,
        }


@dataclass
class StartSessionResult:
    """Result of starting a placement session.

    Attributes:
        success: Whether session was started successfully
        session_id: Unique session identifier
        component_count: Number of components in the session
        fixed_count: Number of fixed (unmovable) components
        initial_score: Initial placement score
        error_message: Error message if success is False
    """

    success: bool
    session_id: str = ""
    component_count: int = 0
    fixed_count: int = 0
    initial_score: float = 0.0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "session_id": self.session_id,
            "component_count": self.component_count,
            "fixed_count": self.fixed_count,
            "initial_score": round(self.initial_score, 4),
            "error_message": self.error_message,
        }


@dataclass
class QueryMoveResult:
    """Result of querying a hypothetical move.

    Attributes:
        success: Whether the query was successful
        would_succeed: Whether applying this move would succeed
        score_delta: Change in placement score (negative = improvement)
        new_violations: New violations that would be created
        resolved_violations: Existing violations that would be resolved
        affected_components: Components that share nets with moved component
        routing_impact: Impact on routing
        warnings: Any warnings about the move
        error_message: Error message if success is False
        intent_status: Intent-aware status (if intents are declared)
        drc_preview: DRC delta preview (what would change)
        net_drc_change: Net change in DRC violations (-1 = improves DRC)
        recommendation: AI-friendly recommendation about the move
        predictions: Predictive warnings about potential future problems
    """

    success: bool
    would_succeed: bool = False
    score_delta: float = 0.0
    new_violations: list[ViolationInfo] = field(default_factory=list)
    resolved_violations: list[ViolationInfo] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)
    routing_impact: RoutingImpactInfo | None = None
    warnings: list[str] = field(default_factory=list)
    error_message: str | None = None
    intent_status: IntentStatus | None = None
    drc_preview: DRCDeltaInfo | None = None
    net_drc_change: int = 0
    recommendation: str = ""
    predictions: list[PredictiveWarningInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "success": self.success,
            "would_succeed": self.would_succeed,
            "score_delta": round(self.score_delta, 4),
            "new_violations": [v.to_dict() for v in self.new_violations],
            "resolved_violations": [v.to_dict() for v in self.resolved_violations],
            "affected_components": self.affected_components,
            "routing_impact": self.routing_impact.to_dict() if self.routing_impact else None,
            "warnings": self.warnings,
            "error_message": self.error_message,
            "drc_preview": self.drc_preview.to_dict() if self.drc_preview else None,
            "net_drc_change": self.net_drc_change,
            "recommendation": self.recommendation,
            "predictions": [p.to_dict() for p in self.predictions],
        }
        if self.intent_status is not None:
            result["intent_status"] = self.intent_status.to_dict()
        return result


@dataclass
class ApplyMoveResult:
    """Result of applying a move within a session.

    Attributes:
        success: Whether the move was applied successfully
        move_id: Index of this move for potential undo
        component: Updated component position
        new_score: New placement score after move
        score_delta: Change in placement score
        pending_moves: Total number of pending moves in session
        error_message: Error message if success is False
        intent_status: Intent-aware status (if intents are declared)
        drc: DRC delta showing new/resolved violations
        predictions: Predictive warnings about potential future problems
    """

    success: bool
    move_id: int = 0
    component: ComponentPosition | None = None
    new_score: float = 0.0
    score_delta: float = 0.0
    pending_moves: int = 0
    error_message: str | None = None
    intent_status: IntentStatus | None = None
    drc: DRCDeltaInfo | None = None
    predictions: list[PredictiveWarningInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result = {
            "success": self.success,
            "move_id": self.move_id,
            "component": self.component.to_dict() if self.component else None,
            "new_score": round(self.new_score, 4),
            "score_delta": round(self.score_delta, 4),
            "pending_moves": self.pending_moves,
            "error_message": self.error_message,
            "drc": self.drc.to_dict() if self.drc else None,
            "predictions": [p.to_dict() for p in self.predictions],
        }
        if self.intent_status is not None:
            result["intent_status"] = self.intent_status.to_dict()
        return result


@dataclass
class CommitResult:
    """Result of committing session changes to PCB file.

    Attributes:
        success: Whether changes were committed successfully
        output_path: Path to the saved PCB file
        moves_applied: Number of moves that were applied
        initial_score: Score at session start
        final_score: Score after all moves
        score_improvement: Total score improvement (positive = better)
        components_moved: List of component references that were moved
        session_closed: Whether the session was closed
        error_message: Error message if success is False
    """

    success: bool
    output_path: str = ""
    moves_applied: int = 0
    initial_score: float = 0.0
    final_score: float = 0.0
    score_improvement: float = 0.0
    components_moved: list[str] = field(default_factory=list)
    session_closed: bool = False
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "output_path": self.output_path,
            "moves_applied": self.moves_applied,
            "initial_score": round(self.initial_score, 4),
            "final_score": round(self.final_score, 4),
            "score_improvement": round(self.score_improvement, 4),
            "components_moved": self.components_moved,
            "session_closed": self.session_closed,
            "error_message": self.error_message,
        }


@dataclass
class RollbackResult:
    """Result of rolling back session changes.

    Attributes:
        success: Whether rollback was successful
        moves_discarded: Number of moves that were discarded
        session_closed: Whether the session was closed
        error_message: Error message if success is False
    """

    success: bool
    moves_discarded: int = 0
    session_closed: bool = False
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "moves_discarded": self.moves_discarded,
            "session_closed": self.session_closed,
            "error_message": self.error_message,
        }


@dataclass
class UndoResult:
    """Result of undoing the last move.

    Attributes:
        success: Whether undo was successful
        restored_component: Position of restored component
        pending_moves: Remaining pending moves
        current_score: Score after undo
        error_message: Error message if success is False
    """

    success: bool
    restored_component: ComponentPosition | None = None
    pending_moves: int = 0
    current_score: float = 0.0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "restored_component": (
                self.restored_component.to_dict() if self.restored_component else None
            ),
            "pending_moves": self.pending_moves,
            "current_score": round(self.current_score, 4),
            "error_message": self.error_message,
        }


@dataclass
class SessionStatusResult:
    """Result of session_status operation with DRC summary.

    Provides comprehensive session status including DRC state.

    Attributes:
        success: Whether the status query succeeded
        session_id: Session identifier
        pending_moves: Number of uncommitted moves
        current_score: Current placement score
        initial_score: Score at session start
        score_delta: Change in score (negative = improvement)
        drc_summary: Current DRC state summary
        error_message: Error message if success is False
    """

    success: bool
    session_id: str = ""
    pending_moves: int = 0
    current_score: float = 0.0
    initial_score: float = 0.0
    score_delta: float = 0.0
    drc_summary: DRCSummary | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "session_id": self.session_id,
            "pending_moves": self.pending_moves,
            "current_score": round(self.current_score, 4),
            "initial_score": round(self.initial_score, 4),
            "score_delta": round(self.score_delta, 4),
            "drc_summary": self.drc_summary.to_dict() if self.drc_summary else None,
            "error_message": self.error_message,
        }
