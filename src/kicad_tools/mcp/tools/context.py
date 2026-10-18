"""MCP tools for session context management.

Provides tools for decision tracking, checkpoints, and context persistence
to enable AI agents to accumulate knowledge throughout the design process.

Tools:
    - record_decision: Record a design decision with rationale
    - get_decision_history: Get recent decisions in session
    - annotate_decision: Add feedback to a past decision
    - get_session_context: Get session context at specified detail level
    - create_checkpoint: Create named checkpoint for potential rollback
    - restore_checkpoint: Restore the placement session to a checkpoint
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
    SessionContextResult,
    SessionSummaryResult,
    StateSnapshot,
)
from kicad_tools.mcp.preference_learner import PreferenceLearner

if TYPE_CHECKING:
    from kicad_tools.mcp.tools.session import SessionMetadata

# =============================================================================
# Session resolution
# =============================================================================

# Shared learner. Session contexts live on ``SessionMetadata`` (one store).
_learner = PreferenceLearner()


def _resolve(session_id: str) -> SessionMetadata | None:
    """Resolve a session ID through the placement ``SessionManager``.

    Intents are bound by reference at call time (``clear_intents`` rebinds
    the list on the metadata, so a copy taken earlier would go stale).
    """
    from kicad_tools.mcp.tools.session import get_session_manager

    metadata = get_session_manager().get(session_id)
    if metadata is not None:
        metadata.context.intents = metadata.intents
    return metadata


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
    metadata = _resolve(session_id)
    if metadata is None:
        return RecordDecisionResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )
    context = metadata.context

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
    metadata = _resolve(session_id)
    if metadata is None:
        return DecisionHistoryResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )
    context = metadata.context

    try:
        # Get decisions with optional filtering
        if filter_action:
            decisions = context.get_decisions_by_action(filter_action)
        else:
            decisions = context.decisions

        # Apply limit
        recent = decisions[-limit:] if len(decisions) > limit else decisions

        # Analyze patterns
        prefs = _learner.analyze_decisions(context.decisions)

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
    metadata = _resolve(session_id)
    if metadata is None:
        return AnnotateDecisionResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )
    context = metadata.context

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
    metadata = _resolve(session_id)
    if metadata is None:
        return SessionContextResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )
    context = metadata.context

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
) -> CheckpointResult:
    """Create named checkpoint for potential rollback.

    Captures the live placement state (move-stack depth, every component
    position, score, violation counts and intent summary) so that
    ``restore_checkpoint`` can actually restore it.

    Args:
        session_id: Active session identifier.
        name: Optional human-readable name for the checkpoint.

    Returns:
        CheckpointResult with the checkpoint ID.

    Example:
        >>> result = create_checkpoint(session_id="sess_123", name="before_power_layout")
        >>> print(result.checkpoint_id)  # "cp_xyz789"
    """
    metadata = _resolve(session_id)
    if metadata is None:
        return CheckpointResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )
    context = metadata.context
    session = metadata.session

    try:
        state = session._capture_state()
        snapshot = StateSnapshot.create(name=name)
        snapshot.component_positions = dict(state.positions)
        snapshot.score = state.score
        snapshot.drc_violation_count = session.get_drc_summary()["total_violations"]
        snapshot.pending_moves = len(session.pending_moves)
        snapshot.moves = [(m.ref, m.new_x, m.new_y, m.new_rotation) for m in session.pending_moves]

        if context.intents:
            snapshot.intent_summary = [
                i.interface_type if hasattr(i, "interface_type") else str(i)
                for i in context.intents
            ]

        checkpoint_id = context.create_checkpoint(name or "unnamed", snapshot)

        return CheckpointResult(
            success=True,
            checkpoint_id=checkpoint_id,
            name=name,
            positions=snapshot.component_positions,
            pending_moves=snapshot.pending_moves,
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
    """Restore the placement session to a checkpoint.

    If the current move stack still starts with the checkpoint's moves, the
    extra moves are undone. Otherwise (the caller undid past the checkpoint
    and then made new moves) each component whose position differs from the
    snapshot is moved back with ``apply_move``, which keeps the change
    undoable and commit-consistent. Success is reported only when every live
    position equals the snapshot afterwards.

    Args:
        session_id: Active session identifier.
        checkpoint_id: ID of the checkpoint to restore.

    Returns:
        CheckpointResult with the restored positions and move-stack depth.
    """
    metadata = _resolve(session_id)
    if metadata is None:
        return CheckpointResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    snapshot = metadata.context.get_checkpoint_snapshot(checkpoint_id)
    if not snapshot:
        return CheckpointResult(
            success=False,
            error_message=f"Checkpoint not found: {checkpoint_id}",
        )

    session = metadata.session
    depth = snapshot.pending_moves
    try:
        current = [(m.ref, m.new_x, m.new_y, m.new_rotation) for m in session.pending_moves]
        if len(current) >= depth and current[:depth] == snapshot.moves:
            while len(session.pending_moves) > depth:
                if not session.undo():
                    break
        else:
            live = session._capture_state().positions
            for ref, target in snapshot.component_positions.items():
                if live.get(ref) != target:
                    session.apply_move(ref, target[0], target[1], target[2])

        live = session._capture_state().positions
        mismatched = sorted(
            ref for ref, pos in snapshot.component_positions.items() if live.get(ref) != pos
        )
        if mismatched:
            return CheckpointResult(
                success=False,
                checkpoint_id=checkpoint_id,
                name=snapshot.name,
                error_message=f"Restore did not reproduce snapshot for: {', '.join(mismatched)}",
            )
        return CheckpointResult(
            success=True,
            checkpoint_id=checkpoint_id,
            name=snapshot.name,
            positions=live,
            pending_moves=len(session.pending_moves),
        )
    except Exception as e:
        return CheckpointResult(
            success=False,
            error_message=f"Failed to restore checkpoint: {e}",
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
    metadata = _resolve(session_id)
    if metadata is None:
        return SessionSummaryResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )
    context = metadata.context

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
