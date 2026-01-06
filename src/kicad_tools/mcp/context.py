"""Context persistence types for MCP sessions.

Provides extended session state that accumulates knowledge throughout the
design process, including decision tracking, learned preferences, and
efficient state summaries.

Example:
    >>> from kicad_tools.mcp.context import (
    ...     SessionContext,
    ...     Decision,
    ...     AgentPreferences,
    ...     StateSnapshot,
    ... )
    >>>
    >>> # Create a decision record
    >>> decision = Decision(
    ...     id="dec_001",
    ...     action="move",
    ...     target="C3",
    ...     rationale="Moving bypass cap closer to U1 VDD pin",
    ...     confidence=0.9,
    ... )
    >>>
    >>> # Create session context
    >>> context = SessionContext(
    ...     session_id="sess_abc123",
    ...     pcb_path="/path/to/board.kicad_pcb",
    ... )
    >>> context.decisions.append(decision)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # IntentDeclaration may be added later when fully integrated


def _now_iso() -> str:
    """Get current time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _generate_id(prefix: str = "") -> str:
    """Generate a unique ID with optional prefix."""
    uid = str(uuid.uuid4())[:8]
    return f"{prefix}_{uid}" if prefix else uid


# =============================================================================
# Decision Tracking Types
# =============================================================================


@dataclass
class Decision:
    """A tracked design decision.

    Records design decisions with rationale and outcome for learning
    and context persistence.

    Attributes:
        id: Unique identifier for this decision.
        action: Action type (e.g., "move", "route", "declare_intent").
        target: Target of action (component ref, net name, etc.).
        params: Action parameters specific to the action type.
        rationale: Why this decision was made (optional).
        alternatives_considered: Other options that were considered.
        outcome: Result of the decision ("success", "partial", "reverted").
        drc_impact: DRC delta from this decision.
        confidence: Agent's confidence in this decision (0.0-1.0).
        feedback: Human or validation feedback.
        timestamp: When this decision was recorded.
    """

    id: str
    action: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str | None = None
    alternatives_considered: list[dict[str, Any]] | None = None
    outcome: str = "pending"  # "success", "partial", "reverted", "pending"
    drc_impact: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.8
    feedback: str | None = None
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "action": self.action,
            "target": self.target,
            "params": self.params,
            "rationale": self.rationale,
            "alternatives_considered": self.alternatives_considered,
            "outcome": self.outcome,
            "drc_impact": self.drc_impact,
            "confidence": round(self.confidence, 2),
            "feedback": self.feedback,
        }

    def to_compact_dict(self) -> dict[str, Any]:
        """Convert to compact dictionary for summaries."""
        return {
            "id": self.id,
            "action": self.action,
            "target": self.target,
            "outcome": self.outcome,
            "confidence": round(self.confidence, 2),
        }

    @classmethod
    def create(
        cls,
        action: str,
        target: str,
        params: dict[str, Any] | None = None,
        rationale: str | None = None,
        alternatives: list[dict[str, Any]] | None = None,
        confidence: float = 0.8,
    ) -> Decision:
        """Factory method to create a new Decision with generated ID."""
        return cls(
            id=_generate_id("dec"),
            action=action,
            target=target,
            params=params or {},
            rationale=rationale,
            alternatives_considered=alternatives,
            confidence=confidence,
        )


# =============================================================================
# Preference Learning Types
# =============================================================================


@dataclass
class AgentPreferences:
    """Learned preferences from agent behavior.

    Captures patterns and preferences derived from observing
    agent decisions over time.

    Attributes:
        preferred_spacing: Typical component spacing used (mm).
        alignment_preference: Preferred alignment style.
        via_tolerance: How liberally vias are used.
        layer_preference: Preferred layer order for routing.
        density_vs_routability: Trade-off preference (0=sparse, 1=dense).
        cost_vs_performance: Trade-off preference (0=cheap, 1=performant).
        common_patterns: Patterns the agent uses frequently.
        avoided_patterns: Patterns the agent avoids.
    """

    # Placement preferences
    preferred_spacing: float = 2.5  # mm between components
    alignment_preference: str = "grid"  # "grid", "functional", "aesthetic"

    # Routing preferences
    via_tolerance: str = "moderate"  # "minimal", "moderate", "liberal"
    layer_preference: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])

    # Trade-off preferences (0.0 to 1.0 scale)
    density_vs_routability: float = 0.5  # 0=sparse, 1=dense
    cost_vs_performance: float = 0.5  # 0=cheap, 1=performant

    # Derived from history
    common_patterns: list[str] = field(default_factory=list)
    avoided_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "preferred_spacing": round(self.preferred_spacing, 2),
            "alignment_preference": self.alignment_preference,
            "via_tolerance": self.via_tolerance,
            "layer_preference": self.layer_preference,
            "density_vs_routability": round(self.density_vs_routability, 2),
            "cost_vs_performance": round(self.cost_vs_performance, 2),
            "common_patterns": self.common_patterns,
            "avoided_patterns": self.avoided_patterns,
        }


# =============================================================================
# State Snapshot Types
# =============================================================================


@dataclass
class StateSnapshot:
    """Compressed state snapshot for efficient resumption.

    Captures essential board state at a point in time for
    efficient context restoration and LLM context optimization.

    Attributes:
        snapshot_id: Unique identifier for this snapshot.
        timestamp: When this snapshot was created.
        name: Optional human-readable name for the snapshot.
        component_positions: Map of ref -> (x, y, rotation).
        drc_violation_count: Number of DRC violations at snapshot time.
        intent_summary: Summary of declared intents (interface types only).
        changed_components: Components changed since last snapshot.
        decisions_since_last: Number of decisions since previous snapshot.
        score: Placement quality score at snapshot time.
    """

    snapshot_id: str
    timestamp: str = field(default_factory=_now_iso)
    name: str | None = None
    component_positions: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    drc_violation_count: int = 0
    intent_summary: list[str] = field(default_factory=list)
    changed_components: list[str] = field(default_factory=list)
    decisions_since_last: int = 0
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp": self.timestamp,
            "name": self.name,
            "component_positions": {
                ref: {"x": pos[0], "y": pos[1], "rotation": pos[2]}
                for ref, pos in self.component_positions.items()
            },
            "drc_violation_count": self.drc_violation_count,
            "intent_summary": self.intent_summary,
            "changed_components": self.changed_components,
            "decisions_since_last": self.decisions_since_last,
            "score": round(self.score, 4),
        }

    @classmethod
    def create(cls, name: str | None = None) -> StateSnapshot:
        """Factory method to create a new StateSnapshot with generated ID."""
        return cls(
            snapshot_id=_generate_id("snap"),
            name=name,
        )


# =============================================================================
# Session Context Type
# =============================================================================


@dataclass
class SessionContext:
    """Persistent context for a design session.

    Accumulates knowledge throughout the design process including
    decisions, preferences, and state snapshots.

    Attributes:
        session_id: Unique session identifier.
        pcb_path: Path to the PCB file.
        created_at: When the session was created.
        intents: List of declared design intents.
        decisions: History of design decisions.
        preferences: Learned agent preferences.
        snapshots: State snapshots for efficient resumption.
        checkpoints: Named checkpoints for rollback.
    """

    session_id: str
    pcb_path: str
    created_at: str = field(default_factory=_now_iso)

    # Intent tracking
    intents: list[Any] = field(default_factory=list)  # list[IntentDeclaration]

    # Decision history
    decisions: list[Decision] = field(default_factory=list)

    # Learned preferences
    preferences: AgentPreferences = field(default_factory=AgentPreferences)

    # State snapshots
    snapshots: list[StateSnapshot] = field(default_factory=list)

    # Named checkpoints (subset of snapshots with names)
    checkpoints: dict[str, str] = field(default_factory=dict)  # checkpoint_id -> snapshot_id

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "pcb_path": self.pcb_path,
            "created_at": self.created_at,
            "intents": [
                i.interface_type if hasattr(i, "interface_type") else str(i) for i in self.intents
            ],
            "decisions": [d.to_dict() for d in self.decisions],
            "preferences": self.preferences.to_dict(),
            "snapshots": [s.to_dict() for s in self.snapshots],
            "checkpoints": self.checkpoints,
        }

    def add_decision(self, decision: Decision) -> None:
        """Add a decision to the history."""
        self.decisions.append(decision)

    def get_recent_decisions(self, limit: int = 10) -> list[Decision]:
        """Get the most recent decisions."""
        return self.decisions[-limit:] if self.decisions else []

    def get_decisions_by_action(self, action: str) -> list[Decision]:
        """Get all decisions of a specific action type."""
        return [d for d in self.decisions if d.action == action]

    def add_snapshot(self, snapshot: StateSnapshot) -> None:
        """Add a state snapshot."""
        snapshot.decisions_since_last = self._decisions_since_last_snapshot()
        self.snapshots.append(snapshot)

    def _decisions_since_last_snapshot(self) -> int:
        """Count decisions since the last snapshot."""
        if not self.snapshots:
            return len(self.decisions)
        last_snapshot_time = self.snapshots[-1].timestamp
        return sum(1 for d in self.decisions if d.timestamp > last_snapshot_time)

    def create_checkpoint(self, name: str, snapshot: StateSnapshot) -> str:
        """Create a named checkpoint from a snapshot."""
        checkpoint_id = _generate_id("cp")
        snapshot.name = name
        self.add_snapshot(snapshot)
        self.checkpoints[checkpoint_id] = snapshot.snapshot_id
        return checkpoint_id

    def get_checkpoint_snapshot(self, checkpoint_id: str) -> StateSnapshot | None:
        """Get the snapshot associated with a checkpoint."""
        snapshot_id = self.checkpoints.get(checkpoint_id)
        if not snapshot_id:
            return None
        return next((s for s in self.snapshots if s.snapshot_id == snapshot_id), None)

    def get_summary(self, max_tokens: int = 500) -> str:
        """Get token-efficient session summary for LLM context.

        Optimized to fit within the specified token budget while
        capturing essential state.

        Args:
            max_tokens: Approximate maximum tokens for the summary.

        Returns:
            Compact string summary of the session state.
        """
        summary_parts = []

        # Core session info
        summary_parts.append(f"Session: {self.session_id}")
        summary_parts.append(f"PCB: {self.pcb_path}")

        # Intent summary
        if self.intents:
            intent_types = [
                i.interface_type if hasattr(i, "interface_type") else str(i) for i in self.intents
            ]
            summary_parts.append(f"Intents: {', '.join(intent_types[:5])}")

        # Decision summary
        if self.decisions:
            summary_parts.append(f"Decisions: {len(self.decisions)} total")

            # Recent decisions (last 5)
            recent = self.get_recent_decisions(5)
            if recent:
                summary_parts.append("Recent:")
                for d in recent:
                    summary_parts.append(f"  - {d.action} {d.target}: {d.outcome}")

        # Pattern summary
        if self.preferences.common_patterns:
            patterns = self.preferences.common_patterns[:3]
            summary_parts.append(f"Patterns: {', '.join(patterns)}")

        # Latest snapshot info
        if self.snapshots:
            latest = self.snapshots[-1]
            summary_parts.append(f"DRC violations: {latest.drc_violation_count}")
            summary_parts.append(f"Score: {latest.score:.2f}")

        return "\n".join(summary_parts)

    def get_context(self, detail_level: str = "summary") -> dict[str, Any]:
        """Get session context at specified detail level.

        Args:
            detail_level: One of "summary", "detailed", or "full".

        Returns:
            Context dictionary appropriate for the detail level.
        """
        if detail_level == "summary":
            return {
                "session_id": self.session_id,
                "pcb_path": self.pcb_path,
                "intent_count": len(self.intents),
                "intents": [
                    i.interface_type if hasattr(i, "interface_type") else str(i)
                    for i in self.intents[:5]
                ],
                "decision_count": len(self.decisions),
                "checkpoint_count": len(self.checkpoints),
                "time_elapsed": self._get_elapsed_time(),
            }
        elif detail_level == "detailed":
            return {
                **self.get_context("summary"),
                "recent_decisions": [d.to_compact_dict() for d in self.get_recent_decisions(10)],
                "patterns_used": self.preferences.common_patterns,
                "preferences": {
                    "spacing": self.preferences.preferred_spacing,
                    "alignment": self.preferences.alignment_preference,
                    "via_tolerance": self.preferences.via_tolerance,
                },
            }
        else:  # full
            return self.to_dict()

    def _get_elapsed_time(self) -> str:
        """Get elapsed time since session creation."""
        try:
            created = datetime.fromisoformat(self.created_at)
            now = datetime.now(timezone.utc)
            elapsed = now - created
            hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours > 0:
                return f"{hours}h {minutes}m"
            elif minutes > 0:
                return f"{minutes}m {seconds}s"
            else:
                return f"{seconds}s"
        except (ValueError, TypeError):
            return "unknown"


# =============================================================================
# MCP Tool Result Types
# =============================================================================


@dataclass
class RecordDecisionResult:
    """Result of recording a decision."""

    success: bool
    decision_id: str = ""
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "decision_id": self.decision_id,
            "error_message": self.error_message,
        }


@dataclass
class DecisionHistoryResult:
    """Result of getting decision history."""

    success: bool
    decisions: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    patterns: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "decisions": self.decisions,
            "total": self.total,
            "patterns": self.patterns,
            "error_message": self.error_message,
        }


@dataclass
class AnnotateDecisionResult:
    """Result of annotating a decision."""

    success: bool
    decision_id: str = ""
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "decision_id": self.decision_id,
            "error_message": self.error_message,
        }


@dataclass
class SessionContextResult:
    """Result of getting session context."""

    success: bool
    context: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {"success": self.success}
        if self.success:
            result.update(self.context)
        else:
            result["error_message"] = self.error_message
        return result


@dataclass
class CheckpointResult:
    """Result of creating or restoring a checkpoint."""

    success: bool
    checkpoint_id: str = ""
    name: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "checkpoint_id": self.checkpoint_id,
            "name": self.name,
            "error_message": self.error_message,
        }


@dataclass
class SessionSummaryResult:
    """Result of getting token-efficient session summary."""

    success: bool
    summary: str = ""
    token_estimate: int = 0
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "summary": self.summary,
            "token_estimate": self.token_estimate,
            "error_message": self.error_message,
        }
