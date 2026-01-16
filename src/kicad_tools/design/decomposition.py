"""
Command decomposition for multi-resolution design operations.

This module decomposes high-level design commands into sequences
of lower-level operations that can be applied to the PCB.

Example::

    from kicad_tools.design.decomposition import decompose_subsystem

    steps = decompose_subsystem(
        subsystem_type="power_supply",
        components=["U1", "C1", "C2"],
        anchor="U1",
        anchor_position=(20, 50),
        pcb=pcb,
    )
    for step in steps:
        print(f"{step.operation}: {step.description}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from kicad_tools.design.strategies import Placement, PlacementPlan, get_strategy
from kicad_tools.design.subsystems import (
    OptimizationGoal,
    SubsystemType,
    get_subsystem_definition,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


class OperationType(Enum):
    """Types of operations in a decomposed plan."""

    MOVE = "move"
    ROTATE = "rotate"
    VALIDATE = "validate"
    GROUP = "group"
    COMMENT = "comment"


@dataclass
class DecomposedStep:
    """A single step in a decomposed command sequence.

    Attributes:
        operation: Type of operation
        ref: Component reference (for MOVE/ROTATE)
        x: Target X position (for MOVE)
        y: Target Y position (for MOVE)
        rotation: Target rotation (for ROTATE)
        description: Human-readable description
        rationale: Why this step is needed
        dependencies: Steps that must be completed first
    """

    operation: OperationType
    ref: str = ""
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    description: str = ""
    rationale: str = ""
    dependencies: list[int] = field(default_factory=list)


@dataclass
class DecompositionResult:
    """Result of decomposing a high-level command.

    Attributes:
        steps: Ordered list of steps to execute
        subsystem_type: Type of subsystem being created
        anchor: Anchor component reference
        anchor_position: Anchor position
        total_components: Number of components involved
        warnings: Any warnings about the decomposition
        plan: The underlying placement plan
    """

    steps: list[DecomposedStep]
    subsystem_type: str
    anchor: str
    anchor_position: tuple[float, float]
    total_components: int
    warnings: list[str] = field(default_factory=list)
    plan: PlacementPlan | None = None


def decompose_subsystem(
    subsystem_type: str | SubsystemType,
    components: list[str],
    anchor: str,
    anchor_position: tuple[float, float],
    pcb: PCB,
    optimize_for: str | OptimizationGoal = OptimizationGoal.ROUTING,
    near_edge: str | None = None,
    near_component: str | None = None,
) -> DecompositionResult:
    """Decompose a subsystem placement into individual steps.

    This is the main entry point for decomposing high-level subsystem
    placement commands into executable steps.

    Args:
        subsystem_type: Type of subsystem (e.g., "power_supply")
        components: List of component references in the subsystem
        anchor: The anchor component reference
        anchor_position: (x, y) position for the anchor
        pcb: The PCB object for context
        optimize_for: Optimization goal
        near_edge: Optional edge constraint ("left", "right", "top", "bottom")
        near_component: Optional component to place near

    Returns:
        DecompositionResult with ordered steps

    Raises:
        ValueError: If subsystem type is invalid or anchor not in components
    """
    if isinstance(subsystem_type, str):
        subsystem_type = SubsystemType(subsystem_type)

    if isinstance(optimize_for, str):
        optimize_for = OptimizationGoal(optimize_for)

    # Validate inputs
    warnings: list[str] = []

    if anchor not in components:
        raise ValueError(f"Anchor '{anchor}' must be in components list")

    # Get subsystem definition
    definition = get_subsystem_definition(subsystem_type)

    # Apply edge constraints if specified
    final_anchor_position = anchor_position
    if near_edge:
        final_anchor_position = _adjust_for_edge(anchor_position, near_edge, pcb, warnings)

    # Apply near_component constraint if specified
    if near_component:
        final_anchor_position = _adjust_for_proximity(
            final_anchor_position, near_component, pcb, warnings
        )

    # Get strategy and compute placements
    strategy = get_strategy(subsystem_type)
    placements = strategy.compute_placements(
        components=components,
        anchor=anchor,
        anchor_position=final_anchor_position,
        pcb=pcb,
        optimize_for=optimize_for,
    )

    # Convert placements to steps
    steps = _placements_to_steps(placements, anchor, final_anchor_position, definition)

    # Create placement plan for reference
    plan = PlacementPlan(
        steps=[placements[ref] for ref in components if ref in placements],
        anchor=anchor,
        anchor_position=final_anchor_position,
        subsystem_type=subsystem_type.value,
        optimization_goal=optimize_for.value,
        warnings=warnings,
    )

    return DecompositionResult(
        steps=steps,
        subsystem_type=subsystem_type.value,
        anchor=anchor,
        anchor_position=final_anchor_position,
        total_components=len(components),
        warnings=warnings,
        plan=plan,
    )


def _adjust_for_edge(
    position: tuple[float, float],
    edge: str,
    pcb: PCB,
    warnings: list[str],
) -> tuple[float, float]:
    """Adjust position to be near a board edge.

    Args:
        position: Original position
        edge: Edge to place near ("left", "right", "top", "bottom")
        pcb: PCB for board outline
        warnings: List to append warnings to

    Returns:
        Adjusted position
    """
    outline = pcb.get_board_outline()
    if not outline:
        warnings.append("No board outline found, edge constraint ignored")
        return position

    min_x = min(p[0] for p in outline)
    max_x = max(p[0] for p in outline)
    min_y = min(p[1] for p in outline)
    max_y = max(p[1] for p in outline)

    edge_margin = 5.0  # mm from edge

    x, y = position

    if edge == "left":
        x = min_x + edge_margin
    elif edge == "right":
        x = max_x - edge_margin
    elif edge == "top":
        y = min_y + edge_margin
    elif edge == "bottom":
        y = max_y - edge_margin
    else:
        warnings.append(f"Unknown edge '{edge}', constraint ignored")

    return (x, y)


def _adjust_for_proximity(
    position: tuple[float, float],
    near_component: str,
    pcb: PCB,
    warnings: list[str],
) -> tuple[float, float]:
    """Adjust position to be near another component.

    Args:
        position: Original position
        near_component: Component reference to place near
        pcb: PCB for component lookup
        warnings: List to append warnings to

    Returns:
        Adjusted position
    """
    # Find the target component
    for fp in pcb.footprints:
        if fp.reference == near_component:
            # Place with some offset from the target
            target_x, target_y = fp.position
            offset = 10.0  # mm offset from target
            return (target_x + offset, target_y)

    warnings.append(f"Component '{near_component}' not found, proximity constraint ignored")
    return position


def _placements_to_steps(
    placements: dict[str, Placement],
    anchor: str,
    anchor_position: tuple[float, float],
    definition: object,
) -> list[DecomposedStep]:
    """Convert placement dictionary to ordered steps.

    Args:
        placements: Dictionary of placements
        anchor: Anchor component reference
        anchor_position: Anchor position
        definition: Subsystem definition

    Returns:
        Ordered list of steps
    """
    steps: list[DecomposedStep] = []

    # First step: place anchor
    if anchor in placements:
        anchor_placement = placements[anchor]
        steps.append(
            DecomposedStep(
                operation=OperationType.MOVE,
                ref=anchor,
                x=anchor_placement.x,
                y=anchor_placement.y,
                rotation=anchor_placement.rotation,
                description=f"Place anchor {anchor} at ({anchor_placement.x:.1f}, {anchor_placement.y:.1f})",
                rationale=anchor_placement.rationale or "Anchor for subsystem",
            )
        )

    # Add steps for other components
    for ref, placement in placements.items():
        if ref == anchor:
            continue

        steps.append(
            DecomposedStep(
                operation=OperationType.MOVE,
                ref=ref,
                x=placement.x,
                y=placement.y,
                rotation=placement.rotation,
                description=f"Move {ref} to ({placement.x:.1f}, {placement.y:.1f})",
                rationale=placement.rationale,
                dependencies=[0],  # Depends on anchor placement
            )
        )

    # Add validation step
    steps.append(
        DecomposedStep(
            operation=OperationType.VALIDATE,
            description="Validate subsystem placement against design rules",
            rationale="Ensure all placement rules are satisfied",
            dependencies=list(range(len(steps))),  # Depends on all previous steps
        )
    )

    return steps


def decompose_group(
    refs: list[str],
    strategy: str,
    anchor: str,
    anchor_position: tuple[float, float],
    pcb: PCB,
) -> DecompositionResult:
    """Decompose a component grouping operation.

    This handles the medium-level abstraction where components are
    grouped using a named strategy.

    Args:
        refs: List of component references to group
        strategy: Strategy name (e.g., "power_supply", "bypass")
        anchor: The anchor component reference
        anchor_position: Position for the anchor
        pcb: The PCB object

    Returns:
        DecompositionResult with ordered steps
    """
    # Map strategy names to subsystem types
    strategy_mapping = {
        "power_supply": SubsystemType.POWER_SUPPLY,
        "ldo": SubsystemType.POWER_SUPPLY,
        "buck": SubsystemType.POWER_SUPPLY,
        "boost": SubsystemType.POWER_SUPPLY,
        "mcu_core": SubsystemType.MCU_CORE,
        "bypass": SubsystemType.MCU_CORE,
        "crystal": SubsystemType.MCU_CORE,
        "connector": SubsystemType.CONNECTOR,
        "usb": SubsystemType.CONNECTOR,
        "uart": SubsystemType.CONNECTOR,
        "spi": SubsystemType.CONNECTOR,
    }

    subsystem_type = strategy_mapping.get(strategy.lower())
    if subsystem_type is None:
        raise ValueError(
            f"Unknown grouping strategy: {strategy}. "
            f"Valid strategies: {list(strategy_mapping.keys())}"
        )

    return decompose_subsystem(
        subsystem_type=subsystem_type,
        components=refs,
        anchor=anchor,
        anchor_position=anchor_position,
        pcb=pcb,
    )


__all__ = [
    "OperationType",
    "DecomposedStep",
    "DecompositionResult",
    "decompose_subsystem",
    "decompose_group",
]
