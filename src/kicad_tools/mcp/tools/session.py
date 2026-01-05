"""MCP tools for interactive placement sessions.

Provides start_session and query_move tools for AI agents to
interactively explore placement changes using query-before-commit
semantics.

Example:
    >>> result = start_session("/path/to/board.kicad_pcb", fixed_refs=["J1"])
    >>> print(f"Session ID: {result.session_id}")
    >>> query = query_move(result.session_id, "C1", 45.0, 32.0)
    >>> print(f"Score change: {query.score_delta}")
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.session_manager import (
    SessionExpiredError,
    SessionNotFoundError,
    get_session_manager,
)
from kicad_tools.mcp.types import (
    ComponentPosition,
    MoveQueryResult,
    SessionRoutingImpact,
    SessionStartResult,
    SessionViolation,
)

__all__ = ["start_session", "query_move"]


def start_session(
    pcb_path: str,
    fixed_refs: list[str] | None = None,
) -> SessionStartResult:
    """
    Start an interactive placement refinement session.

    Creates a new session for exploring placement changes with
    query-before-commit semantics. The session tracks all components,
    pending moves, and allows evaluating hypothetical changes before
    applying them.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        fixed_refs: Component references that should not be moved
                   (e.g., connectors with fixed mechanical positions)

    Returns:
        SessionStartResult with session ID and initial state

    Raises:
        FileNotFoundError: If PCB file doesn't exist
        ParseError: If PCB file cannot be parsed
    """
    # Validate path
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    # Create session
    manager = get_session_manager()
    info = manager.create(pcb_path, fixed_refs=fixed_refs)

    # Get session to extract component positions
    session = manager.get(info.id)

    # Convert component positions
    components = [
        ComponentPosition(
            ref=comp["ref"],
            x=comp["x"],
            y=comp["y"],
            rotation=comp["rotation"],
            fixed=comp["fixed"],
            width=comp["width"],
            height=comp["height"],
        )
        for comp in session.list_components()
    ]

    # Calculate expiration time (30 minutes from creation)
    expires_at = info.created_at + timedelta(minutes=30)

    return SessionStartResult(
        session_id=info.id,
        pcb_path=pcb_path,
        components=components,
        initial_score=session._initial_score,
        fixed_refs=fixed_refs or [],
        expires_at=expires_at.isoformat(),
    )


def query_move(
    session_id: str,
    ref: str,
    x: float,
    y: float,
    rotation: float | None = None,
) -> MoveQueryResult:
    """
    Evaluate a hypothetical component move without applying it.

    This tool allows AI agents to explore placement changes before
    committing to them. The move is evaluated but not applied to the
    session state, allowing multiple "what-if" queries.

    Args:
        session_id: Session ID from start_session
        ref: Component reference designator (e.g., "C1", "U3")
        x: New X position in millimeters
        y: New Y position in millimeters
        rotation: New rotation in degrees (None = keep current rotation)

    Returns:
        MoveQueryResult with impact analysis including:
        - Whether the move is valid
        - Score change (negative = improvement)
        - New violations created by the move
        - Existing violations resolved by the move
        - Affected components (connected via nets)
        - Routing impact estimate

    Raises:
        SessionNotFoundError: If session ID doesn't exist
        SessionExpiredError: If session has timed out
    """
    manager = get_session_manager()

    try:
        session = manager.get(session_id)
    except SessionNotFoundError as e:
        return MoveQueryResult(
            valid=False,
            error_message=str(e),
        )
    except SessionExpiredError as e:
        return MoveQueryResult(
            valid=False,
            error_message=str(e),
        )

    # Query the move using PlacementSession
    result = session.query_move(ref, x, y, rotation)

    # Convert violations
    new_violations = [
        SessionViolation(
            type=v.type,
            description=v.description,
            severity=v.severity,
            component=v.component,
            location=v.location,
        )
        for v in result.new_violations
    ]

    resolved_violations = [
        SessionViolation(
            type=v.type,
            description=v.description,
            severity=v.severity,
            component=v.component,
            location=v.location,
        )
        for v in result.resolved_violations
    ]

    # Convert routing impact
    routing_impact = SessionRoutingImpact(
        affected_nets=result.routing_impact.affected_nets,
        estimated_length_change_mm=result.routing_impact.estimated_length_change_mm,
        new_congestion_areas=list(result.routing_impact.new_congestion_areas),
        crossing_changes=result.routing_impact.crossing_changes,
    )

    return MoveQueryResult(
        valid=result.success,
        score_delta=result.score_delta,
        new_violations=new_violations,
        resolved_violations=resolved_violations,
        affected_components=result.affected_components,
        routing_impact=routing_impact,
        warnings=result.warnings,
        error_message=result.error_message if not result.success else None,
    )
