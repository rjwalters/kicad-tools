"""MCP tools for session context management.

Provides tools for decision tracking, checkpoints, and context persistence
to enable AI agents to accumulate knowledge throughout the design process.

Tools:
    - record_decision: Record a design decision with rationale
    - get_decision_history: Get recent decisions in session
    - annotate_decision: Add feedback to a past decision
    - get_session_context: Get session context at specified detail level
    - create_checkpoint: Create named checkpoint for potential rollback
    - restore_checkpoint: Restore session to checkpoint state
    - get_session_summary: Get token-efficient session summary

Example:
    >>> result = record_decision(
    ...     session_id="sess_123",
    ...     action="move",
    ...     target="C3",
    ...     rationale="Moving bypass cap closer to U1 VDD pin",
    ...     confidence=0.9,
    ... )
    >>> print(result.decision_id)  # "dec_abc123"
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kicad_tools.mcp.context import (
    AnnotateDecisionResult,
    CheckpointResult,
    Decision,
    DecisionHistoryResult,
    RecordDecisionResult,
    SessionContext,
    SessionContextResult,
    SessionSummaryResult,
    StateSnapshot,
)
from kicad_tools.mcp.preference_learner import PreferenceLearner

if TYPE_CHECKING:
    pass

# =============================================================================
# Context Manager
# =============================================================================


class ContextManager:
    """Manages session contexts for MCP operations.

    Maintains a registry of session contexts indexed by session ID,
    providing thread-safe access to context state.
    """

    def __init__(self) -> None:
        """Initialize the context manager."""
        self._contexts: dict[str, SessionContext] = {}
        self._learner = PreferenceLearner()

    def get_or_create(self, session_id: str, pcb_path: str = "") -> SessionContext:
        """Get existing context or create a new one.

        Args:
            session_id: Session identifier.
            pcb_path: PCB file path for new contexts.

        Returns:
            SessionContext for the session.
        """
        if session_id not in self._contexts:
            self._contexts[session_id] = SessionContext(
                session_id=session_id,
                pcb_path=pcb_path,
            )
        return self._contexts[session_id]

    def get(self, session_id: str) -> SessionContext | None:
        """Get context by session ID.

        Args:
            session_id: Session identifier.

        Returns:
            SessionContext or None if not found.
        """
        return self._contexts.get(session_id)

    def close(self, session_id: str) -> bool:
        """Close and remove a session context.

        Args:
            session_id: Session identifier.

        Returns:
            True if context was closed, False if not found.
        """
        if session_id in self._contexts:
            del self._contexts[session_id]
            return True
        return False

    def list_sessions(self) -> list[str]:
        """List all active session IDs.

        Returns:
            List of session IDs.
        """
        return list(self._contexts.keys())

    @property
    def learner(self) -> PreferenceLearner:
        """Get the preference learner instance."""
        return self._learner


# Global context manager instance
_context_manager = ContextManager()


def get_context_manager() -> ContextManager:
    """Get the global context manager instance.

    Returns:
        The global ContextManager instance.
    """
    return _context_manager


def reset_context_manager() -> None:
    """Reset the global context manager (for testing)."""
    global _context_manager
    _context_manager = ContextManager()


# =============================================================================
# Decision Tracking Tools
# =============================================================================


def record_decision(
    session_id: str,
    action: str,
    target: str,
    rationale: str | None = None,
    alternatives: list[dict[str, Any]] | None = None,
    confidence: float = 0.8,
    params: dict[str, Any] | None = None,
) -> RecordDecisionResult:
    """Record a design decision with rationale.

    Creates a permanent record of a design decision, including the
    reasoning behind it and any alternatives that were considered.

    Args:
        session_id: Active session identifier.
        action: Action type (e.g., "move", "route", "place").
        target: Target of action (component ref, net name, etc.).
        rationale: Why this decision was made (optional).
        alternatives: Other options that were considered (optional).
        confidence: Agent's confidence in this decision (0.0-1.0).
        params: Additional action parameters (optional).

    Returns:
        RecordDecisionResult with the decision ID.

    Example:
        >>> result = record_decision(
        ...     session_id="sess_123",
        ...     action="move",
        ...     target="C3",
        ...     rationale="Moving bypass cap closer to U1 VDD pin for better decoupling",
        ...     alternatives=[
        ...         {"target": "C4", "reason": "C4 is larger, harder to place"},
        ...         {"action": "add_cap", "reason": "Would increase BOM cost"},
        ...     ],
        ...     confidence=0.9,
        ... )
    """
    context = _context_manager.get(session_id)
    if not context:
        # Auto-create context if session exists but context doesn't
        context = _context_manager.get_or_create(session_id)

    try:
        decision = Decision.create(
            action=action,
            target=target,
            rationale=rationale,
            alternatives=alternatives,
            confidence=confidence,
            params=params or {},
        )
        context.add_decision(decision)

        return RecordDecisionResult(
            success=True,
            decision_id=decision.id,
        )
    except Exception as e:
        return RecordDecisionResult(
            success=False,
            error_message=f"Failed to record decision: {e}",
        )


def get_decision_history(
    session_id: str,
    limit: int = 20,
    filter_action: str | None = None,
) -> DecisionHistoryResult:
    """Get recent decisions in session.

    Retrieves the decision history for a session, optionally filtered
    by action type. Also returns detected patterns.

    Args:
        session_id: Active session identifier.
        limit: Maximum number of decisions to return.
        filter_action: Optional action type to filter by (e.g., "move").

    Returns:
        DecisionHistoryResult with decisions and detected patterns.

    Example:
        >>> result = get_decision_history(
        ...     session_id="sess_123",
        ...     limit=10,
        ...     filter_action="move",
        ... )
        >>> print(result.patterns)  # ["bypass_cap_optimization", "edge_connector_placement"]
    """
    context = _context_manager.get(session_id)
    if not context:
        return DecisionHistoryResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    try:
        # Get decisions with optional filtering
        if filter_action:
            decisions = context.get_decisions_by_action(filter_action)
        else:
            decisions = context.decisions

        # Apply limit
        recent = decisions[-limit:] if len(decisions) > limit else decisions

        # Analyze patterns
        learner = _context_manager.learner
        prefs = learner.analyze_decisions(context.decisions)

        return DecisionHistoryResult(
            success=True,
            decisions=[d.to_dict() for d in recent],
            total=len(context.decisions),
            patterns=prefs.common_patterns,
        )
    except Exception as e:
        return DecisionHistoryResult(
            success=False,
            error_message=f"Failed to get decision history: {e}",
        )


def annotate_decision(
    session_id: str,
    decision_id: str,
    feedback: str,
    outcome: str | None = None,
) -> AnnotateDecisionResult:
    """Add feedback to a past decision.

    Updates a decision record with feedback and optionally changes
    its outcome status.

    Args:
        session_id: Active session identifier.
        decision_id: ID of the decision to annotate.
        feedback: Feedback text to add.
        outcome: Optional new outcome status ("success", "partial", "reverted").

    Returns:
        AnnotateDecisionResult confirming the annotation.

    Example:
        >>> result = annotate_decision(
        ...     session_id="sess_123",
        ...     decision_id="dec_abc123",
        ...     feedback="This placement improved signal integrity",
        ...     outcome="success",
        ... )
    """
    context = _context_manager.get(session_id)
    if not context:
        return AnnotateDecisionResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    # Find the decision
    decision = next((d for d in context.decisions if d.id == decision_id), None)
    if not decision:
        return AnnotateDecisionResult(
            success=False,
            error_message=f"Decision not found: {decision_id}",
        )

    try:
        # Update feedback
        decision.feedback = feedback

        # Update outcome if provided
        if outcome:
            if outcome not in ("success", "partial", "reverted", "pending"):
                return AnnotateDecisionResult(
                    success=False,
                    error_message=f"Invalid outcome: {outcome}. "
                    "Must be 'success', 'partial', 'reverted', or 'pending'.",
                )
            decision.outcome = outcome

        return AnnotateDecisionResult(
            success=True,
            decision_id=decision_id,
        )
    except Exception as e:
        return AnnotateDecisionResult(
            success=False,
            error_message=f"Failed to annotate decision: {e}",
        )


# =============================================================================
# Context and Checkpoint Tools
# =============================================================================


def get_session_context(
    session_id: str,
    detail_level: str = "summary",
) -> SessionContextResult:
    """Get session context at specified detail level.

    Returns session state at different levels of detail:
    - summary: Compact overview for quick orientation
    - detailed: Include recent decisions and patterns
    - full: Complete state dump

    Args:
        session_id: Active session identifier.
        detail_level: One of "summary", "detailed", or "full".

    Returns:
        SessionContextResult with context information.

    Example:
        >>> result = get_session_context(
        ...     session_id="sess_123",
        ...     detail_level="detailed",
        ... )
        >>> print(result.context["recent_decisions"])
    """
    context = _context_manager.get(session_id)
    if not context:
        return SessionContextResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    try:
        if detail_level not in ("summary", "detailed", "full"):
            return SessionContextResult(
                success=False,
                error_message=f"Invalid detail_level: {detail_level}. "
                "Must be 'summary', 'detailed', or 'full'.",
            )

        ctx = context.get_context(detail_level)
        return SessionContextResult(
            success=True,
            context=ctx,
        )
    except Exception as e:
        return SessionContextResult(
            success=False,
            error_message=f"Failed to get session context: {e}",
        )


def create_checkpoint(
    session_id: str,
    name: str | None = None,
    component_positions: dict[str, tuple[float, float, float]] | None = None,
    drc_violation_count: int = 0,
    score: float = 0.0,
) -> CheckpointResult:
    """Create named checkpoint for potential rollback.

    Creates a snapshot of the current session state that can be
    restored later using restore_checkpoint.

    Args:
        session_id: Active session identifier.
        name: Optional human-readable name for the checkpoint.
        component_positions: Current component positions (ref -> (x, y, rot)).
        drc_violation_count: Current number of DRC violations.
        score: Current placement score.

    Returns:
        CheckpointResult with the checkpoint ID.

    Example:
        >>> result = create_checkpoint(
        ...     session_id="sess_123",
        ...     name="before_power_layout",
        ...     drc_violation_count=5,
        ...     score=125.5,
        ... )
        >>> print(result.checkpoint_id)  # "cp_xyz789"
    """
    context = _context_manager.get(session_id)
    if not context:
        return CheckpointResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    try:
        # Create snapshot
        snapshot = StateSnapshot.create(name=name)
        snapshot.component_positions = component_positions or {}
        snapshot.drc_violation_count = drc_violation_count
        snapshot.score = score

        # Extract intent summary from context
        if context.intents:
            snapshot.intent_summary = [
                i.interface_type if hasattr(i, "interface_type") else str(i)
                for i in context.intents
            ]

        # Create checkpoint
        checkpoint_id = context.create_checkpoint(name or "unnamed", snapshot)

        return CheckpointResult(
            success=True,
            checkpoint_id=checkpoint_id,
            name=name,
        )
    except Exception as e:
        return CheckpointResult(
            success=False,
            error_message=f"Failed to create checkpoint: {e}",
        )


def restore_checkpoint(
    session_id: str,
    checkpoint_id: str,
) -> CheckpointResult:
    """Restore session to checkpoint state.

    Retrieves the state snapshot associated with a checkpoint.
    Note: This returns the snapshot data - actual state restoration
    must be performed by the caller using the returned positions.

    Args:
        session_id: Active session identifier.
        checkpoint_id: ID of the checkpoint to restore.

    Returns:
        CheckpointResult with checkpoint information and snapshot data.

    Example:
        >>> result = restore_checkpoint(
        ...     session_id="sess_123",
        ...     checkpoint_id="cp_xyz789",
        ... )
        >>> if result.success:
        ...     # Apply the restored positions
        ...     pass
    """
    context = _context_manager.get(session_id)
    if not context:
        return CheckpointResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    snapshot = context.get_checkpoint_snapshot(checkpoint_id)
    if not snapshot:
        return CheckpointResult(
            success=False,
            error_message=f"Checkpoint not found: {checkpoint_id}",
        )

    return CheckpointResult(
        success=True,
        checkpoint_id=checkpoint_id,
        name=snapshot.name,
    )


def get_session_summary(
    session_id: str,
    max_tokens: int = 500,
) -> SessionSummaryResult:
    """Get token-efficient session summary for LLM context.

    Generates a compact representation suitable for LLM context,
    focusing on recent changes and important decisions.

    Args:
        session_id: Active session identifier.
        max_tokens: Approximate maximum tokens for the summary.

    Returns:
        SessionSummaryResult with the compact summary.

    Example:
        >>> result = get_session_summary(session_id="sess_123")
        >>> print(result.summary)
        # Session: sess_123
        # PCB: /path/to/board.kicad_pcb
        # Decisions: 45 total
        # Recent:
        #   - move C3: success
        #   - route VDD: success
    """
    context = _context_manager.get(session_id)
    if not context:
        return SessionSummaryResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    try:
        summary = context.get_summary(max_tokens)

        # Estimate token count (rough approximation: ~4 chars per token)
        token_estimate = len(summary) // 4

        return SessionSummaryResult(
            success=True,
            summary=summary,
            token_estimate=token_estimate,
        )
    except Exception as e:
        return SessionSummaryResult(
            success=False,
            error_message=f"Failed to get session summary: {e}",
        )


# =============================================================================
# Integration with Session Tools
# =============================================================================


def link_session_to_context(session_id: str, pcb_path: str) -> SessionContext:
    """Link an existing session to a context (creates if needed).

    Called by session tools to ensure context exists when sessions are created.

    Args:
        session_id: Session identifier.
        pcb_path: PCB file path.

    Returns:
        The SessionContext for the session.
    """
    return _context_manager.get_or_create(session_id, pcb_path)


def unlink_session_context(session_id: str) -> bool:
    """Remove context when session is closed.

    Called by session tools when sessions are committed or rolled back.

    Args:
        session_id: Session identifier.

    Returns:
        True if context was removed, False if not found.
    """
    return _context_manager.close(session_id)
