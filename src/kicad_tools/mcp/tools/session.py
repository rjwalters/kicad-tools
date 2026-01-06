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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.drc.predictive import PredictiveAnalyzer
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.intent import (
    REGISTRY,
    IntentDeclaration,
    create_intent_declaration,
)
from kicad_tools.mcp.types import (
    ApplyMoveResult,
    ClearIntentResult,
    CommitResult,
    ComponentPosition,
    ConstraintInfo,
    DeclareInterfaceResult,
    DeclarePowerRailResult,
    DRCDeltaInfo,
    DRCSummary,
    DRCViolationDetail,
    IntentInfo,
    IntentStatus,
    IntentViolation,
    ListIntentsResult,
    PredictiveWarningInfo,
    QueryMoveResult,
    RollbackResult,
    RoutingImpactInfo,
    SessionStatusResult,
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
        intents: List of declared design intents
    """

    session_id: str
    pcb_path: str
    session: PlacementSession
    initial_score: float
    intents: list[IntentDeclaration] = field(default_factory=list)

    def declare_intent(self, declaration: IntentDeclaration) -> None:
        """Add an intent declaration to the session."""
        self.intents.append(declaration)

    def get_constraints_for_net(self, net: str) -> list:
        """Get all constraints affecting a specific net."""
        constraints = []
        for intent in self.intents:
            if net in intent.nets:
                constraints.extend(intent.constraints)
        return constraints

    def get_all_derived_constraints(self) -> list:
        """Get all constraints from all intents."""
        constraints = []
        for intent in self.intents:
            constraints.extend(intent.constraints)
        return constraints

    def get_intents_for_net(self, net: str) -> list[IntentDeclaration]:
        """Get all intents that include a specific net."""
        return [intent for intent in self.intents if net in intent.nets]

    def clear_intents(
        self,
        interface_type: str | None = None,
        nets: list[str] | None = None,
    ) -> int:
        """Clear intent declarations matching the criteria.

        Args:
            interface_type: If provided, only clear intents of this type
            nets: If provided, only clear intents involving these nets

        Returns:
            Number of intents cleared
        """
        if interface_type is None and nets is None:
            # Clear all intents
            count = len(self.intents)
            self.intents = []
            return count

        # Filter intents to keep
        original_count = len(self.intents)
        new_intents = []

        for intent in self.intents:
            should_remove = False

            if interface_type is not None and intent.interface_type == interface_type:
                should_remove = True

            if nets is not None and any(net in intent.nets for net in nets):
                should_remove = True

            if not should_remove:
                new_intents.append(intent)

        self.intents = new_intents
        return original_count - len(self.intents)


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


def _convert_drc_delta(drc_delta) -> DRCDeltaInfo | None:
    """Convert internal DRCDelta to MCP DRCDeltaInfo type.

    Args:
        drc_delta: The DRCDelta from PlacementSession

    Returns:
        DRCDeltaInfo for MCP response, or None if drc_delta is None
    """
    if drc_delta is None:
        return None

    # Convert violations to MCP format
    new_violations = [
        DRCViolationDetail(
            id=v.id,
            type=v.rule_id,
            severity=v.severity or "error",
            message=v.message,
            components=list(v.items) if v.items else [],
            location=v.location,
        )
        for v in drc_delta.new_violations
    ]

    resolved_violations = [
        DRCViolationDetail(
            id=v.id,
            type=v.rule_id,
            severity=v.severity or "error",
            message=v.message,
            components=list(v.items) if v.items else [],
            location=v.location,
        )
        for v in drc_delta.resolved_violations
    ]

    # Calculate delta string - note: total_violations is set to 0 as delta doesn't track total
    # Use session_status for accurate total counts
    delta_str = (
        f"+{len(new_violations)} -{len(resolved_violations)} = {drc_delta.net_change:+d} net"
    )

    return DRCDeltaInfo(
        new_violations=new_violations,
        resolved_violations=resolved_violations,
        total_violations=0,  # Total not tracked in delta; use session_status for totals
        delta=delta_str,
        check_time_ms=drc_delta.check_time_ms,
    )


def _get_predictions(
    metadata: SessionMetadata,
    ref: str,
    new_pos: tuple[float, float],
) -> list[PredictiveWarningInfo]:
    """Get predictive warnings for a move.

    Args:
        metadata: Session metadata with intents
        ref: Component reference being moved
        new_pos: New position (x, y)

    Returns:
        List of predictive warnings converted to MCP types
    """
    analyzer = PredictiveAnalyzer(metadata.session, metadata.intents)
    warnings = analyzer.analyze_move(ref, new_pos)

    return [
        PredictiveWarningInfo(
            type=w.type,
            message=w.message,
            confidence=w.confidence,
            suggestion=w.suggestion,
            affected_nets=w.affected_nets,
            location=w.location,
        )
        for w in warnings
    ]


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

    # Get intent status if intents are declared
    intent_status = None
    if metadata.intents:
        intent_status = _get_intent_status_for_move(
            metadata=metadata,
            ref=ref,
            affected_nets=result.routing_impact.affected_nets,
        )

    # Convert DRC delta to MCP type
    drc_preview = _convert_drc_delta(result.drc_delta)

    # Get predictive warnings
    predictions = _get_predictions(metadata, ref, (x, y))

    # Calculate net DRC change and generate recommendation
    net_drc_change = 0
    recommendation = ""
    if result.drc_delta:
        net_drc_change = result.drc_delta.net_change
        # Generate AI-friendly recommendation
        if net_drc_change < 0:
            recommendation = f"RECOMMEND: Move resolves {-net_drc_change} DRC violation(s)"
        elif net_drc_change > 0:
            recommendation = f"CAUTION: Move creates {net_drc_change} new DRC violation(s)"
        elif result.score_delta < 0:
            recommendation = "NEUTRAL: Move improves placement score with no DRC change"
        else:
            recommendation = "NEUTRAL: Move has minimal impact on DRC and placement"

    # Add prediction warnings to recommendation if present
    if predictions:
        warning_types = {p.type for p in predictions}
        if "routing_difficulty" in warning_types:
            recommendation += " | WARNING: May increase routing difficulty"
        if "congestion" in warning_types:
            recommendation += " | WARNING: Area becoming congested"
        if "intent_risk" in warning_types:
            recommendation += " | WARNING: May affect design intent constraints"

    return QueryMoveResult(
        success=True,
        would_succeed=True,
        score_delta=result.score_delta,
        new_violations=new_violations,
        resolved_violations=resolved_violations,
        affected_components=result.affected_components,
        routing_impact=routing_impact,
        warnings=result.warnings,
        intent_status=intent_status,
        drc_preview=drc_preview,
        net_drc_change=net_drc_change,
        recommendation=recommendation,
        predictions=predictions,
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

    # Get intent status if intents are declared
    intent_status = None
    if metadata.intents:
        # Get affected nets from routing impact
        affected_nets = result.routing_impact.affected_nets if result.routing_impact else []
        intent_status = _get_intent_status_for_move(
            metadata=metadata,
            ref=ref,
            affected_nets=affected_nets,
        )

    # Convert DRC delta to MCP type
    drc = _convert_drc_delta(result.drc_delta)

    # Get predictive warnings
    predictions = _get_predictions(metadata, ref, (x, y))

    return ApplyMoveResult(
        success=True,
        move_id=len(session.pending_moves),
        component=component,
        new_score=new_score,
        score_delta=result.score_delta,
        pending_moves=len(session.pending_moves),
        intent_status=intent_status,
        drc=drc,
        predictions=predictions,
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


def session_status(session_id: str) -> SessionStatusResult:
    """Get comprehensive status of a placement session including DRC summary.

    Returns the current state of the session including pending moves,
    placement scores, and a complete DRC summary with violation counts
    and trend analysis.

    Args:
        session_id: Session ID from start_session

    Returns:
        SessionStatusResult with session state and DRC summary
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return SessionStatusResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    session = metadata.session
    status = session.get_status()
    drc_summary_data = session.get_drc_summary()

    # Convert DRC summary to MCP type
    drc_summary = DRCSummary(
        total_violations=drc_summary_data["total_violations"],
        by_severity=drc_summary_data["by_severity"],
        by_type=drc_summary_data["by_type"],
        trend=drc_summary_data["trend"],
        session_delta=drc_summary_data["session_delta"],
    )

    return SessionStatusResult(
        success=True,
        session_id=session_id,
        pending_moves=status["pending_moves"],
        current_score=status["current_score"],
        initial_score=status["initial_score"],
        score_delta=status["score_change"],
        drc_summary=drc_summary,
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


# =============================================================================
# Intent Declaration Tools
# =============================================================================


def declare_interface(
    session_id: str,
    interface_type: str,
    nets: list[str],
    params: dict | None = None,
) -> DeclareInterfaceResult:
    """Declare a design interface intent.

    Declares that a set of nets form a specific interface type, automatically
    deriving constraints from the interface specification. Subsequent operations
    will be aware of these declarations for constraint checking.

    Args:
        session_id: Active placement session ID
        interface_type: Interface type (e.g., "usb2_high_speed", "spi_fast", "i2c_standard")
        nets: Net names that form this interface
        params: Optional interface-specific parameters

    Returns:
        DeclareInterfaceResult with declaration status and derived constraints

    Example:
        >>> declare_interface(
        ...     session_id="sess_123",
        ...     interface_type="usb2_high_speed",
        ...     nets=["USB_D+", "USB_D-"]
        ... )
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return DeclareInterfaceResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    # Validate interface type exists
    spec = REGISTRY.get(interface_type)
    if spec is None:
        available = REGISTRY.list_interfaces()
        return DeclareInterfaceResult(
            success=False,
            error_message=(
                f"Unknown interface type: '{interface_type}'. "
                f"Available types: {', '.join(available) or '(none)'}"
            ),
        )

    # Validate nets for this interface
    validation_errors = spec.validate_nets(nets)
    if validation_errors:
        return DeclareInterfaceResult(
            success=False,
            error_message=f"Invalid nets for {interface_type}: {'; '.join(validation_errors)}",
        )

    try:
        # Create intent declaration with derived constraints
        declaration = create_intent_declaration(
            interface_type=interface_type,
            nets=nets,
            params=params,
            validate=False,  # Already validated above
        )

        # Add to session
        metadata.declare_intent(declaration)

        # Convert constraints to MCP types
        constraint_infos = [
            ConstraintInfo(
                type=c.type,
                params=dict(c.params) if c.params else {},
                source=c.source,
                severity=c.severity.value if hasattr(c.severity, "value") else str(c.severity),
            )
            for c in declaration.constraints
        ]

        # Check for warnings (e.g., duplicate declarations)
        warnings = []
        existing = [
            i
            for i in metadata.intents[:-1]  # Exclude the one we just added
            if i.interface_type == interface_type and any(n in i.nets for n in nets)
        ]
        if existing:
            warnings.append(
                f"Note: Similar intent already declared for {interface_type}. "
                "Multiple declarations for overlapping nets may cause constraint conflicts."
            )

        return DeclareInterfaceResult(
            success=True,
            declared=True,
            interface_type=interface_type,
            nets=nets,
            constraints=constraint_infos,
            warnings=warnings,
        )

    except ValueError as e:
        return DeclareInterfaceResult(
            success=False,
            error_message=str(e),
        )
    except Exception as e:
        return DeclareInterfaceResult(
            success=False,
            error_message=f"Failed to declare interface: {e}",
        )


def declare_power_rail(
    session_id: str,
    net: str,
    voltage: float,
    max_current: float = 0.5,
) -> DeclarePowerRailResult:
    """Declare a power rail with requirements.

    Declares a power rail net with voltage and current requirements,
    automatically deriving constraints for trace width and decoupling.

    Args:
        session_id: Active placement session ID
        net: Power net name (e.g., "VDD_3V3")
        voltage: Rail voltage
        max_current: Maximum expected current draw (default 0.5A)

    Returns:
        DeclarePowerRailResult with declaration status and derived constraints

    Example:
        >>> declare_power_rail(
        ...     session_id="sess_123",
        ...     net="VDD_3V3",
        ...     voltage=3.3,
        ...     max_current=0.5
        ... )
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return DeclarePowerRailResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    try:
        # Create power rail intent declaration
        declaration = create_intent_declaration(
            interface_type="power_rail",
            nets=[net],
            params={"voltage": voltage, "max_current": max_current},
        )

        # Add to session
        metadata.declare_intent(declaration)

        # Convert constraints to MCP types
        constraint_infos = [
            ConstraintInfo(
                type=c.type,
                params=dict(c.params) if c.params else {},
                source=c.source,
                severity=c.severity.value if hasattr(c.severity, "value") else str(c.severity),
            )
            for c in declaration.constraints
        ]

        return DeclarePowerRailResult(
            success=True,
            declared=True,
            net=net,
            voltage=voltage,
            max_current=max_current,
            constraints=constraint_infos,
        )

    except ValueError as e:
        return DeclarePowerRailResult(
            success=False,
            error_message=str(e),
        )
    except Exception as e:
        return DeclarePowerRailResult(
            success=False,
            error_message=f"Failed to declare power rail: {e}",
        )


def list_intents(session_id: str) -> ListIntentsResult:
    """List all declared intents in a session.

    Returns information about all intent declarations in the session,
    including the interface types, nets, and constraint counts.

    Args:
        session_id: Active placement session ID

    Returns:
        ListIntentsResult with all declared intents

    Example:
        >>> list_intents(session_id="sess_123")
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return ListIntentsResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    intent_infos = [
        IntentInfo(
            interface_type=intent.interface_type,
            nets=intent.nets,
            constraint_count=len(intent.constraints),
            metadata=dict(intent.metadata) if intent.metadata else {},
        )
        for intent in metadata.intents
    ]

    total_constraints = sum(len(intent.constraints) for intent in metadata.intents)

    return ListIntentsResult(
        success=True,
        intents=intent_infos,
        constraint_count=total_constraints,
    )


def clear_intent(
    session_id: str,
    interface_type: str | None = None,
    nets: list[str] | None = None,
) -> ClearIntentResult:
    """Remove intent declaration(s) from session.

    Clears intent declarations matching the specified criteria. If no criteria
    are provided, all intents are cleared.

    Args:
        session_id: Active placement session ID
        interface_type: If provided, only clear intents of this type
        nets: If provided, only clear intents involving these nets

    Returns:
        ClearIntentResult with counts of cleared and remaining intents

    Example:
        >>> # Clear all USB intents
        >>> clear_intent(session_id="sess_123", interface_type="usb2_high_speed")
        >>>
        >>> # Clear intents for specific nets
        >>> clear_intent(session_id="sess_123", nets=["USB_D+", "USB_D-"])
        >>>
        >>> # Clear all intents
        >>> clear_intent(session_id="sess_123")
    """
    metadata = _session_manager.get(session_id)
    if not metadata:
        return ClearIntentResult(
            success=False,
            error_message=f"Session not found: {session_id}",
        )

    cleared_count = metadata.clear_intents(interface_type=interface_type, nets=nets)

    return ClearIntentResult(
        success=True,
        cleared_count=cleared_count,
        remaining_count=len(metadata.intents),
    )


def _get_intent_status_for_move(
    metadata: SessionMetadata,
    ref: str,
    affected_nets: list[str],
) -> IntentStatus:
    """Get intent status for a move operation.

    Checks which intents are affected by a component move and reports
    any relevant warnings.

    Args:
        metadata: Session metadata with intents
        ref: Component reference being moved
        affected_nets: Nets connected to the component

    Returns:
        IntentStatus with violations, warnings, and affected intents
    """
    violations: list[IntentViolation] = []
    warnings: list[str] = []
    affected_intents: list[str] = []

    # Find intents affected by this move
    for intent in metadata.intents:
        # Check if any of the component's nets are part of this intent
        overlapping_nets = [net for net in affected_nets if net in intent.nets]
        if overlapping_nets:
            if intent.interface_type not in affected_intents:
                affected_intents.append(intent.interface_type)

            # Add warning about moving components in declared interfaces
            warnings.append(
                f"Component {ref} is connected to nets in {intent.interface_type} interface: "
                f"{', '.join(overlapping_nets)}"
            )

    return IntentStatus(
        violations=violations,
        warnings=warnings,
        affected_intents=affected_intents,
    )
