"""MCP tools for high-level design operations.

Provides tools for multi-resolution design abstraction, allowing agents
to work at different levels of abstraction from high-level subsystem
placement to low-level component moves.

Example:
    >>> result = add_subsystem(
    ...     session_id="abc123",
    ...     subsystem_type="power_supply",
    ...     components=["U1", "C1", "C2"],
    ...     near_edge="left",
    ... )
    >>> print(result.subsystem_name)
    'power_supply_1'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.design import (
    Design,
    get_subsystem_definition,
    list_subsystem_types,
)
from kicad_tools.mcp.errors import SessionNotFoundError

if TYPE_CHECKING:
    pass


# Design session registry (separate from placement sessions)
_design_sessions: dict[str, Design] = {}


@dataclass
class AddSubsystemResult:
    """Result of adding a subsystem.

    Attributes:
        success: Whether the operation succeeded
        subsystem_name: Name assigned to the subsystem
        components_placed: Number of components successfully placed
        placements: List of placement details
        validation_warnings: Warnings from subsystem validation
        warnings: General warnings
        error: Error message if failed
    """

    success: bool
    subsystem_name: str = ""
    components_placed: int = 0
    placements: list[dict] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class GroupComponentsResult:
    """Result of grouping components.

    Attributes:
        success: Whether the operation succeeded
        components_placed: Number of components successfully placed
        placements: List of placement details
        warnings: General warnings
        error: Error message if failed
    """

    success: bool
    components_placed: int = 0
    placements: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class PlanSubsystemResult:
    """Result of planning a subsystem.

    Attributes:
        success: Whether planning succeeded
        steps: List of planned placement steps
        anchor: Anchor component
        anchor_position: Position for anchor
        subsystem_type: Type of subsystem
        optimization_goal: Goal used for optimization
        warnings: Any warnings about the plan
        error: Error message if failed
    """

    success: bool
    steps: list[dict] = field(default_factory=list)
    anchor: str = ""
    anchor_position: tuple[float, float] = (0.0, 0.0)
    subsystem_type: str = ""
    optimization_goal: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ListSubsystemTypesResult:
    """Result of listing subsystem types.

    Attributes:
        types: List of available subsystem types with descriptions
    """

    types: list[dict] = field(default_factory=list)


@dataclass
class ValidateDesignResult:
    """Result of validating design.

    Attributes:
        success: Whether validation passed
        issues: List of validation issues
        subsystem_count: Number of subsystems registered
    """

    success: bool
    issues: list[dict] = field(default_factory=list)
    subsystem_count: int = 0


def get_design_session(session_id: str) -> Design:
    """Get or create a Design session for a placement session.

    Args:
        session_id: Placement session ID

    Returns:
        Design instance for the session

    Raises:
        SessionNotFoundError: If session doesn't exist
    """
    from kicad_tools.mcp.tools.session import _sessions

    if session_id not in _sessions:
        raise SessionNotFoundError(f"Session not found: {session_id}")

    if session_id not in _design_sessions:
        # Create Design wrapper for existing session
        metadata = _sessions[session_id]
        design = Design(metadata.session.pcb)
        # Replace session with existing one
        design._session = metadata.session
        _design_sessions[session_id] = design

    return _design_sessions[session_id]


def add_subsystem(
    session_id: str,
    subsystem_type: str,
    components: list[str],
    near_edge: str | None = None,
    near_component: str | None = None,
    optimize_for: str = "routing",
    anchor: str | None = None,
    anchor_position: tuple[float, float] | None = None,
) -> AddSubsystemResult:
    """Add a subsystem with automatic placement.

    High-level API for placing a group of components as a functional
    subsystem. The system automatically calculates optimal positions
    based on subsystem type and constraints.

    Args:
        session_id: Active placement session ID
        subsystem_type: Type of subsystem ("power_supply", "mcu_core", "connector")
        components: List of component references in the subsystem
        near_edge: Optional edge constraint ("left", "right", "top", "bottom")
        near_component: Optional component to place near
        optimize_for: Optimization goal ("thermal", "routing", "compact")
        anchor: Optional explicit anchor component (auto-detected if not provided)
        anchor_position: Optional explicit anchor position (auto-calculated if not provided)

    Returns:
        AddSubsystemResult with placement details

    Raises:
        SessionNotFoundError: If session doesn't exist
        ValueError: If subsystem_type is invalid
    """
    try:
        design = get_design_session(session_id)
    except SessionNotFoundError as e:
        return AddSubsystemResult(success=False, error=str(e))

    try:
        result = design.add_subsystem(
            subsystem_type=subsystem_type,
            components=components,
            near_edge=near_edge,
            near_component=near_component,
            optimize_for=optimize_for,
            anchor=anchor,
            anchor_position=anchor_position,
        )

        return AddSubsystemResult(
            success=result.success,
            subsystem_name=result.subsystem_name,
            components_placed=len(result.placements),
            placements=[
                {
                    "ref": p.ref,
                    "x": round(p.x, 3),
                    "y": round(p.y, 3),
                    "rotation": p.rotation,
                    "rationale": p.rationale,
                }
                for p in result.placements.values()
            ],
            validation_warnings=[issue.message for issue in result.validation_issues],
            warnings=result.warnings,
        )
    except ValueError as e:
        return AddSubsystemResult(success=False, error=str(e))
    except Exception as e:
        return AddSubsystemResult(success=False, error=f"Unexpected error: {e}")


def group_components(
    session_id: str,
    refs: list[str],
    strategy: str,
    anchor: str,
    anchor_position: tuple[float, float],
) -> GroupComponentsResult:
    """Group components using a placement strategy.

    Medium-level API where you specify the grouping strategy and
    anchor position explicitly.

    Args:
        session_id: Active placement session ID
        refs: List of component references to group
        strategy: Strategy name (e.g., "power_supply", "bypass")
        anchor: The anchor component reference
        anchor_position: (x, y) position for the anchor in mm

    Returns:
        GroupComponentsResult with placement details

    Raises:
        SessionNotFoundError: If session doesn't exist
        ValueError: If strategy is invalid or anchor not in refs
    """
    try:
        design = get_design_session(session_id)
    except SessionNotFoundError as e:
        return GroupComponentsResult(success=False, error=str(e))

    try:
        result = design.group_components(
            refs=refs,
            strategy=strategy,
            anchor=anchor,
            anchor_position=anchor_position,
        )

        return GroupComponentsResult(
            success=result.success,
            components_placed=len(result.placements),
            placements=[
                {
                    "ref": p.ref,
                    "x": round(p.x, 3),
                    "y": round(p.y, 3),
                    "rotation": p.rotation,
                    "rationale": p.rationale,
                }
                for p in result.placements.values()
            ],
            warnings=result.warnings,
        )
    except ValueError as e:
        return GroupComponentsResult(success=False, error=str(e))
    except Exception as e:
        return GroupComponentsResult(success=False, error=f"Unexpected error: {e}")


def plan_subsystem(
    session_id: str,
    subsystem_type: str,
    components: list[str],
    anchor: str,
    anchor_position: tuple[float, float],
    near_edge: str | None = None,
    near_component: str | None = None,
    optimize_for: str = "routing",
) -> PlanSubsystemResult:
    """Plan subsystem placement without applying.

    Shows the decomposed steps that would be executed for a subsystem
    placement, allowing agents to review before committing.

    Args:
        session_id: Active placement session ID
        subsystem_type: Type of subsystem
        components: List of component references
        anchor: The anchor component reference
        anchor_position: (x, y) position for the anchor in mm
        near_edge: Optional edge constraint
        near_component: Optional component to place near
        optimize_for: Optimization goal

    Returns:
        PlanSubsystemResult with planned steps
    """
    try:
        design = get_design_session(session_id)
    except SessionNotFoundError as e:
        return PlanSubsystemResult(success=False, error=str(e))

    try:
        plan = design.plan_subsystem(
            subsystem_type=subsystem_type,
            components=components,
            anchor=anchor,
            anchor_position=anchor_position,
            near_edge=near_edge,
            near_component=near_component,
            optimize_for=optimize_for,
        )

        return PlanSubsystemResult(
            success=True,
            steps=[
                {
                    "ref": step.ref,
                    "x": round(step.x, 3),
                    "y": round(step.y, 3),
                    "rotation": step.rotation,
                    "rationale": step.rationale,
                }
                for step in plan.steps
            ],
            anchor=plan.anchor,
            anchor_position=plan.anchor_position,
            subsystem_type=plan.subsystem_type,
            optimization_goal=plan.optimization_goal,
            warnings=plan.warnings,
        )
    except ValueError as e:
        return PlanSubsystemResult(success=False, error=str(e))
    except Exception as e:
        return PlanSubsystemResult(success=False, error=f"Unexpected error: {e}")


def list_available_subsystem_types() -> ListSubsystemTypesResult:
    """List all available subsystem types.

    Returns information about each subsystem type including its
    patterns, optimization options, and typical components.

    Returns:
        ListSubsystemTypesResult with type information
    """
    types = []

    for type_name in list_subsystem_types():
        definition = get_subsystem_definition(type_name)
        types.append(
            {
                "type": type_name,
                "description": definition.description,
                "patterns": definition.patterns,
                "optimize_for": [g.value for g in definition.optimize_for],
                "anchor_role": definition.anchor_role,
                "typical_components": definition.typical_components,
            }
        )

    return ListSubsystemTypesResult(types=types)


def validate_design(session_id: str) -> ValidateDesignResult:
    """Validate all subsystems in a design session.

    Checks that all registered subsystems satisfy their placement
    constraints.

    Args:
        session_id: Active placement session ID

    Returns:
        ValidateDesignResult with validation status and issues
    """
    try:
        design = get_design_session(session_id)
    except SessionNotFoundError as e:
        return ValidateDesignResult(success=False, issues=[{"message": str(e)}])

    issues = design.validate()

    return ValidateDesignResult(
        success=len(issues) == 0,
        issues=[
            {
                "severity": issue.severity.value,
                "message": issue.message,
                "subsystem": issue.subsystem,
                "component": issue.component,
                "suggestion": issue.suggestion,
            }
            for issue in issues
        ],
        subsystem_count=len(design.subsystems),
    )


def validate_move(
    session_id: str,
    ref: str,
    new_x: float,
    new_y: float,
) -> ValidateDesignResult:
    """Check if a proposed move would violate subsystem constraints.

    Call this before low-level moves to get warnings about potential
    subsystem constraint violations.

    Args:
        session_id: Active placement session ID
        ref: Component reference to move
        new_x: New X position in mm
        new_y: New Y position in mm

    Returns:
        ValidateDesignResult with any constraint violations
    """
    try:
        design = get_design_session(session_id)
    except SessionNotFoundError as e:
        return ValidateDesignResult(success=False, issues=[{"message": str(e)}])

    issues = design.validate_move(ref, new_x, new_y)

    return ValidateDesignResult(
        success=len(issues) == 0,
        issues=[
            {
                "severity": issue.severity.value,
                "message": issue.message,
                "subsystem": issue.subsystem,
                "component": issue.component,
                "suggestion": issue.suggestion,
            }
            for issue in issues
        ],
        subsystem_count=len(design.subsystems),
    )


__all__ = [
    "add_subsystem",
    "group_components",
    "plan_subsystem",
    "list_available_subsystem_types",
    "validate_design",
    "validate_move",
    "AddSubsystemResult",
    "GroupComponentsResult",
    "PlanSubsystemResult",
    "ListSubsystemTypesResult",
    "ValidateDesignResult",
]
