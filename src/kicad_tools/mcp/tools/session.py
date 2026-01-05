"""MCP tools for interactive placement session management.

Provides stateful session management for AI agents to interactively
refine component placement through a query-before-commit workflow.

Session Lifecycle:
    1. start_session() - Create a new session from a PCB file
    2. query_move() - Evaluate hypothetical moves (no state change)
    3. apply_move() - Apply a move (can be undone)
    4. undo_move() - Undo the last applied move
    5. commit_session() - Write all changes to file and close session
       OR rollback_session() - Discard all changes and close session

Example:
    >>> result = start_session("/path/to/board.kicad_pcb")
    >>> session_id = result.session_id
    >>>
    >>> # Query a move first
    >>> query = query_move(session_id, "C1", 45.0, 32.0)
    >>> if query.would_succeed and query.score_delta < 0:
    ...     apply_move(session_id, "C1", 45.0, 32.0)
    >>>
    >>> # Commit changes
    >>> commit_session(session_id)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.types import (
    ApplyMoveResult,
    CommitResult,
    ComponentPosition,
    QueryMoveResult,
    RollbackResult,
    RoutingImpactInfo,
    StartSessionResult,
    UndoResult,
    ViolationInfo,
)
from kicad_tools.optim.session import PlacementSession
from kicad_tools.schema.pcb import PCB

if TYPE_CHECKING:
    pass


@dataclass
class SessionMetadata:
    """Metadata for an active placement session.

    Attributes:
        session_id: Unique session identifier
        pcb_path: Path to the source PCB file
        session: The underlying PlacementSession object
        initial_score: Score at session start
    """

    session_id: str
    pcb_path: str
    session: PlacementSession
    initial_score: float


class SessionManager:
    """Manages active placement sessions.

    Provides a registry of active sessions indexed by session ID.
    Sessions are automatically cleaned up when committed or rolled back.
    """

    def __init__(self) -> None:
        """Initialize the session manager."""
        self._sessions: dict[str, SessionMetadata] = {}

    def create(self, pcb_path: str, fixed_refs: list[str] | None = None) -> SessionMetadata:
        """Create a new placement session.

        Args:
            pcb_path: Path to the PCB file
            fixed_refs: Optional list of component references to keep fixed

        Returns:
            SessionMetadata for the new session

        Raises:
            FileNotFoundError: If PCB file doesn't exist
            ParseError: If PCB file can't be parsed
        """
        path = Path(pcb_path)
        if not path.exists():
            raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

        if path.suffix != ".kicad_pcb":
            raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

        pcb = PCB.load(pcb_path)
        session = PlacementSession(pcb, fixed_refs=fixed_refs)

        session_id = str(uuid.uuid4())[:8]
        initial_score = session._compute_score()

        metadata = SessionMetadata(
            session_id=session_id,
            pcb_path=str(path.absolute()),
            session=session,
            initial_score=initial_score,
        )

        self._sessions[session_id] = metadata
        return metadata

    def get(self, session_id: str) -> SessionMetadata | None:
        """Get session metadata by ID.

        Args:
            session_id: The session identifier

        Returns:
            SessionMetadata or None if not found
        """
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        """Close and remove a session.

        Args:
            session_id: The session identifier

        Returns:
            True if session was closed, False if not found
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def list_sessions(self) -> list[str]:
        """List all active session IDs.

        Returns:
            List of session IDs
        """
        return list(self._sessions.keys())


# Global session manager instance
_session_manager = SessionManager()


def start_session(
    pcb_path: str,
    fixed_refs: list[str] | None = None,
) -> StartSessionResult:
    """Start a new placement refinement session.

    Creates a new session for interactively refining component placement.
    The session maintains state for query-before-commit operations.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        fixed_refs: Optional list of component references to keep fixed

    Returns:
        StartSessionResult with session ID and initial state

    Raises:
        FileNotFoundError: If PCB file doesn't exist
        ParseError: If PCB file can't be parsed
    """
    try:
        metadata = _session_manager.create(pcb_path, fixed_refs)
        session = metadata.session

        # Count components and fixed components
        component_count = len(session._optimizer.components)
        fixed_count = sum(1 for c in session._optimizer.components if c.fixed)
        if fixed_refs:
            fixed_count += len(fixed_refs)

        return StartSessionResult(
            success=True,
            session_id=metadata.session_id,
            component_count=component_count,
            fixed_count=fixed_count,
            initial_score=metadata.initial_score,
        )

    except KiCadFileNotFoundError as e:
        return StartSessionResult(success=False, error_message=str(e))
    except ParseError as e:
        return StartSessionResult(success=False, error_message=str(e))
    except Exception as e:
        return StartSessionResult(success=False, error_message=f"Failed to start session: {e}")


def query_move(
    session_id: str,
    ref: str,
    x: float,
    y: float,
    rotation: float | None = None,
) -> QueryMoveResult:
    """Query the impact of a hypothetical move without applying it.

    Evaluates what would happen if a component were moved to the
    specified position. Does not modify session state.

    Args:
        session_id: Session ID from start_session
        ref: Component reference designator (e.g., "C1", "R5")
        x: Target X position in millimeters
        y: Target Y position in millimeters
        rotation: Target rotation in degrees (None = keep current)

    Returns:
        QueryMoveResult with impact analysis
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return QueryMoveResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    session = metadata.session
    result = session.query_move(ref, x, y, rotation)

    if not result.success:
        return QueryMoveResult(
            success=False,
            error_message=result.error_message,
        )

    # Convert violations to MCP types
    new_violations = [
        ViolationInfo(
            type=v.type,
            description=v.description,
            severity=v.severity,
            component=v.component,
            location=v.location,
        )
        for v in result.new_violations
    ]

    resolved_violations = [
        ViolationInfo(
            type=v.type,
            description=v.description,
            severity=v.severity,
            component=v.component,
            location=v.location,
        )
        for v in result.resolved_violations
    ]

    routing_impact = RoutingImpactInfo(
        affected_nets=result.routing_impact.affected_nets,
        estimated_length_change_mm=result.routing_impact.estimated_length_change_mm,
        crossing_changes=result.routing_impact.crossing_changes,
    )

    return QueryMoveResult(
        success=True,
        would_succeed=True,
        score_delta=result.score_delta,
        new_violations=new_violations,
        resolved_violations=resolved_violations,
        affected_components=result.affected_components,
        routing_impact=routing_impact,
        warnings=result.warnings,
    )


def apply_move(
    session_id: str,
    ref: str,
    x: float,
    y: float,
    rotation: float | None = None,
) -> ApplyMoveResult:
    """Apply a move within the session.

    Moves a component to the specified position. The move can be
    undone with undo_move() and is not written to disk until
    commit_session() is called.

    Args:
        session_id: Session ID from start_session
        ref: Component reference designator
        x: New X position in millimeters
        y: New Y position in millimeters
        rotation: New rotation in degrees (None = keep current)

    Returns:
        ApplyMoveResult with updated state
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return ApplyMoveResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    session = metadata.session
    result = session.apply_move(ref, x, y, rotation)

    if not result.success:
        return ApplyMoveResult(
            success=False,
            error_message=result.error_message,
        )

    # Get updated component position
    comp_pos = session.get_component_position(ref)
    component = None
    if comp_pos:
        component = ComponentPosition(
            ref=comp_pos["ref"],
            x=comp_pos["x"],
            y=comp_pos["y"],
            rotation=comp_pos["rotation"],
            fixed=comp_pos["fixed"],
        )

    new_score = session._compute_score()

    return ApplyMoveResult(
        success=True,
        move_id=len(session.pending_moves),
        component=component,
        new_score=new_score,
        score_delta=result.score_delta,
        pending_moves=len(session.pending_moves),
    )


def undo_move(session_id: str) -> UndoResult:
    """Undo the last applied move.

    Restores the previous component position. Can be called
    multiple times to undo multiple moves.

    Args:
        session_id: Session ID from start_session

    Returns:
        UndoResult with restored state
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return UndoResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    session = metadata.session

    if not session.pending_moves:
        return UndoResult(
            success=False,
            error_message="No moves to undo",
        )

    # Get the move that will be undone to report restored position
    last_move = session.pending_moves[-1]

    if not session.undo():
        return UndoResult(
            success=False,
            error_message="Failed to undo move",
        )

    # Get restored component position
    comp_pos = session.get_component_position(last_move.ref)
    restored_component = None
    if comp_pos:
        restored_component = ComponentPosition(
            ref=comp_pos["ref"],
            x=comp_pos["x"],
            y=comp_pos["y"],
            rotation=comp_pos["rotation"],
            fixed=comp_pos["fixed"],
        )

    return UndoResult(
        success=True,
        restored_component=restored_component,
        pending_moves=len(session.pending_moves),
        current_score=session._compute_score(),
    )


def commit_session(
    session_id: str,
    output_path: str | None = None,
) -> CommitResult:
    """Commit all pending moves to PCB file.

    Writes all applied moves to the PCB file and closes the session.
    If output_path is not specified, overwrites the original file.

    Args:
        session_id: Session ID from start_session
        output_path: Output file path (None = overwrite original)

    Returns:
        CommitResult with summary of changes
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return CommitResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    session = metadata.session
    initial_score = metadata.initial_score
    moves_count = len(session.pending_moves)
    moved_refs = [m.ref for m in session.pending_moves]

    try:
        # Commit changes to PCB object
        pcb = session.commit()

        # Determine output path
        save_path = output_path or metadata.pcb_path

        # Save to file
        pcb.save(save_path)

        final_score = session._compute_score()

        # Close session
        _session_manager.close(session_id)

        return CommitResult(
            success=True,
            output_path=save_path,
            moves_applied=moves_count,
            initial_score=initial_score,
            final_score=final_score,
            score_improvement=initial_score - final_score,
            components_moved=moved_refs,
            session_closed=True,
        )

    except Exception as e:
        return CommitResult(
            success=False,
            error_message=f"Failed to commit session: {e}",
        )


def rollback_session(session_id: str) -> RollbackResult:
    """Discard all pending moves and close session.

    Reverts all applied moves and closes the session without
    writing any changes to disk.

    Args:
        session_id: Session ID from start_session

    Returns:
        RollbackResult confirming rollback
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return RollbackResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    session = metadata.session
    moves_count = len(session.pending_moves)

    # Rollback changes
    session.rollback()

    # Close session
    _session_manager.close(session_id)

    return RollbackResult(
        success=True,
        moves_discarded=moves_count,
        session_closed=True,
    )


# Export session manager for testing
def get_session_manager() -> SessionManager:
    """Get the global session manager instance.

    Returns:
        The global SessionManager instance
    """
    return _session_manager


def reset_session_manager() -> None:
    """Reset the global session manager (for testing)."""
    global _session_manager
    _session_manager = SessionManager()
