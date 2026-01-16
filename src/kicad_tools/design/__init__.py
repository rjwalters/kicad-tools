"""
Multi-resolution design abstraction layer for KiCad PCBs.

This module provides a high-level Design facade that allows agents to work
at multiple abstraction levels:

- **High level**: Declarative intent (e.g., "place power supply near left edge")
- **Medium level**: Guided optimization (e.g., "group these components with this strategy")
- **Low level**: Explicit control (e.g., "move U1 to (50, 30)")

Example::

    from kicad_tools.design import Design
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    design = Design(pcb)

    # HIGH LEVEL - Declarative intent
    result = design.add_subsystem(
        subsystem_type="power_supply",
        components=["U_REG", "C_IN", "C_OUT", "L1"],
        near_edge="left",
        optimize_for="thermal",
    )

    # MEDIUM LEVEL - Guided optimization
    result = design.group_components(
        refs=["U_REG", "C_IN", "C_OUT", "L1"],
        strategy="power_supply",
        anchor="U_REG",
        anchor_position=(20, 50),
    )

    # LOW LEVEL - Explicit control (existing API via session)
    session = design.session
    session.apply_move("U_REG", x=20, y=50)

    # Plan without applying
    plan = design.plan_subsystem(
        subsystem_type="power_supply",
        components=["U1", "C1", "C2"],
        anchor="U1",
        anchor_position=(20, 50),
    )
    print(plan.steps)  # See decomposed moves

    # Commit changes
    design.commit("output.kicad_pcb")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.design.decomposition import (
    DecomposedStep,
    DecompositionResult,
    OperationType,
    decompose_group,
    decompose_subsystem,
)
from kicad_tools.design.strategies import (
    Placement,
    PlacementPlan,
    PlacementStrategy,
    get_strategy,
)
from kicad_tools.design.subsystems import (
    OptimizationGoal,
    SubsystemDefinition,
    SubsystemType,
    get_subsystem_definition,
    list_subsystem_types,
)
from kicad_tools.design.validation import (
    AbstractionValidator,
    Subsystem,
    ValidationIssue,
    ValidationSeverity,
)
from kicad_tools.optim.session import PlacementSession

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


@dataclass
class SubsystemResult:
    """Result of adding a subsystem.

    Attributes:
        success: Whether the operation succeeded
        subsystem_name: Name assigned to the subsystem
        placements: Dictionary of component placements
        validation_issues: Any validation issues found
        warnings: General warnings
    """

    success: bool
    subsystem_name: str
    placements: dict[str, Placement] = field(default_factory=dict)
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class GroupResult:
    """Result of grouping components.

    Attributes:
        success: Whether the operation succeeded
        placements: Dictionary of component placements
        validation_issues: Any validation issues found
        warnings: General warnings
    """

    success: bool
    placements: dict[str, Placement] = field(default_factory=dict)
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Design:
    """High-level design interface for multi-resolution PCB design.

    The Design class provides a facade that allows working at multiple
    abstraction levels:

    - **High level**: `add_subsystem()` for declarative placement
    - **Medium level**: `group_components()` for guided optimization
    - **Low level**: Access to `session` for explicit control

    It maintains internal state about subsystems and validates that
    low-level operations don't violate high-level constraints.

    Attributes:
        pcb: The underlying PCB object
        session: PlacementSession for low-level operations
    """

    def __init__(
        self,
        pcb: PCB,
        fixed_refs: list[str] | None = None,
    ) -> None:
        """Initialize the Design facade.

        Args:
            pcb: The PCB to design
            fixed_refs: Component references to keep fixed (won't be moved)
        """
        self._pcb = pcb
        self._session = PlacementSession(pcb, fixed_refs=fixed_refs)
        self._validator = AbstractionValidator()
        self._subsystems: dict[str, Subsystem] = {}
        self._subsystem_counter = 0

    @property
    def pcb(self) -> PCB:
        """Get the underlying PCB object."""
        return self._pcb

    @property
    def session(self) -> PlacementSession:
        """Get the PlacementSession for low-level operations."""
        return self._session

    @property
    def subsystems(self) -> dict[str, Subsystem]:
        """Get all registered subsystems."""
        return self._subsystems.copy()

    def add_subsystem(
        self,
        subsystem_type: str,
        components: list[str],
        near_edge: str | None = None,
        near_component: str | None = None,
        optimize_for: str = "routing",
        anchor: str | None = None,
        anchor_position: tuple[float, float] | None = None,
    ) -> SubsystemResult:
        """Add a subsystem with automatic placement.

        This is the high-level API for placing a group of components
        as a functional subsystem. The system automatically:
        1. Identifies the pattern based on subsystem type
        2. Determines optimal anchor position based on constraints
        3. Calculates positions for all components
        4. Validates against design rules
        5. Applies the placements

        Args:
            subsystem_type: Type of subsystem ("power_supply", "mcu_core", "connector")
            components: List of component references in the subsystem
            near_edge: Optional edge constraint ("left", "right", "top", "bottom")
            near_component: Optional component to place near
            optimize_for: Optimization goal ("thermal", "routing", "compact", etc.)
            anchor: Optional explicit anchor component (auto-detected if not provided)
            anchor_position: Optional explicit anchor position (auto-calculated if not provided)

        Returns:
            SubsystemResult with placement details and any issues

        Raises:
            ValueError: If subsystem_type is invalid or components list is empty
        """
        if not components:
            raise ValueError("Components list cannot be empty")

        # Parse subsystem type
        try:
            st = SubsystemType(subsystem_type)
        except ValueError as e:
            valid_types = list_subsystem_types()
            raise ValueError(
                f"Unknown subsystem type: {subsystem_type}. Valid types: {valid_types}"
            ) from e

        # Get subsystem definition
        definition = get_subsystem_definition(st)

        # Determine anchor
        if anchor is None:
            anchor = self._detect_anchor(components, definition)

        if anchor not in components:
            raise ValueError(f"Anchor '{anchor}' must be in components list")

        # Determine anchor position
        if anchor_position is None:
            anchor_position = self._calculate_anchor_position(anchor, near_edge, near_component)

        # Decompose into steps
        decomposition = decompose_subsystem(
            subsystem_type=st,
            components=components,
            anchor=anchor,
            anchor_position=anchor_position,
            pcb=self._pcb,
            optimize_for=optimize_for,
            near_edge=near_edge,
            near_component=near_component,
        )

        # Apply placements
        warnings = list(decomposition.warnings)
        placements: dict[str, Placement] = {}

        for step in decomposition.steps:
            if step.operation == OperationType.MOVE:
                # Check for validation issues before applying
                move_issues = self._validator.validate_move(step.ref, step.x, step.y, self._pcb)
                if move_issues:
                    warnings.extend(issue.message for issue in move_issues)

                # Apply the move
                result = self._session.apply_move(step.ref, step.x, step.y, step.rotation)
                if result.success:
                    placements[step.ref] = Placement(
                        ref=step.ref,
                        x=step.x,
                        y=step.y,
                        rotation=step.rotation,
                        rationale=step.rationale,
                    )
                else:
                    warnings.append(f"Failed to move {step.ref}: {result.error_message}")

        # Register subsystem for validation
        self._subsystem_counter += 1
        subsystem_name = f"{subsystem_type}_{self._subsystem_counter}"

        subsystem = Subsystem(
            name=subsystem_name,
            subsystem_type=st,
            components=components,
            anchor=anchor,
            anchor_position=anchor_position,
            optimization_goal=OptimizationGoal(optimize_for),
        )
        self._subsystems[subsystem_name] = subsystem
        self._validator.register_subsystem(subsystem)

        # Validate final state
        validation_issues = self._validator.validate(self._pcb)

        return SubsystemResult(
            success=len(placements) == len(components),
            subsystem_name=subsystem_name,
            placements=placements,
            validation_issues=validation_issues,
            warnings=warnings,
        )

    def group_components(
        self,
        refs: list[str],
        strategy: str,
        anchor: str,
        anchor_position: tuple[float, float],
    ) -> GroupResult:
        """Group components using a placement strategy.

        This is the medium-level API where you specify the grouping
        strategy and anchor position explicitly.

        Args:
            refs: List of component references to group
            strategy: Strategy name (e.g., "power_supply", "bypass")
            anchor: The anchor component reference
            anchor_position: (x, y) position for the anchor

        Returns:
            GroupResult with placement details

        Raises:
            ValueError: If strategy is invalid or anchor not in refs
        """
        if anchor not in refs:
            raise ValueError(f"Anchor '{anchor}' must be in refs list")

        # Decompose using the strategy
        decomposition = decompose_group(
            refs=refs,
            strategy=strategy,
            anchor=anchor,
            anchor_position=anchor_position,
            pcb=self._pcb,
        )

        # Apply placements
        warnings = list(decomposition.warnings)
        placements: dict[str, Placement] = {}

        for step in decomposition.steps:
            if step.operation == OperationType.MOVE:
                result = self._session.apply_move(step.ref, step.x, step.y, step.rotation)
                if result.success:
                    placements[step.ref] = Placement(
                        ref=step.ref,
                        x=step.x,
                        y=step.y,
                        rotation=step.rotation,
                        rationale=step.rationale,
                    )
                else:
                    warnings.append(f"Failed to move {step.ref}: {result.error_message}")

        # Validate
        validation_issues = self._validator.validate(self._pcb)

        return GroupResult(
            success=len(placements) == len(refs),
            placements=placements,
            validation_issues=validation_issues,
            warnings=warnings,
        )

    def plan_subsystem(
        self,
        subsystem_type: str,
        components: list[str],
        anchor: str,
        anchor_position: tuple[float, float],
        near_edge: str | None = None,
        near_component: str | None = None,
        optimize_for: str = "routing",
    ) -> PlacementPlan:
        """Plan subsystem placement without applying.

        This allows agents to see the decomposed steps before
        committing to the placement.

        Args:
            subsystem_type: Type of subsystem
            components: List of component references
            anchor: The anchor component reference
            anchor_position: Position for the anchor
            near_edge: Optional edge constraint
            near_component: Optional component to place near
            optimize_for: Optimization goal

        Returns:
            PlacementPlan showing all steps
        """
        decomposition = decompose_subsystem(
            subsystem_type=subsystem_type,
            components=components,
            anchor=anchor,
            anchor_position=anchor_position,
            pcb=self._pcb,
            optimize_for=optimize_for,
            near_edge=near_edge,
            near_component=near_component,
        )

        # Convert decomposed steps to placement plan
        steps = []
        for step in decomposition.steps:
            if step.operation == OperationType.MOVE:
                steps.append(
                    Placement(
                        ref=step.ref,
                        x=step.x,
                        y=step.y,
                        rotation=step.rotation,
                        rationale=step.description,
                    )
                )

        return PlacementPlan(
            steps=steps,
            anchor=anchor,
            anchor_position=anchor_position,
            subsystem_type=subsystem_type,
            optimization_goal=optimize_for,
            warnings=decomposition.warnings,
        )

    def validate(self) -> list[ValidationIssue]:
        """Validate all subsystems against current PCB state.

        Returns:
            List of validation issues found
        """
        return self._validator.validate(self._pcb)

    def validate_move(
        self,
        ref: str,
        new_x: float,
        new_y: float,
    ) -> list[ValidationIssue]:
        """Check if a move would violate subsystem constraints.

        Call this before low-level moves to get warnings about
        potential subsystem constraint violations.

        Args:
            ref: Component reference to move
            new_x: New X position
            new_y: New Y position

        Returns:
            List of validation issues (warnings)
        """
        return self._validator.validate_move(ref, new_x, new_y, self._pcb)

    def commit(self, output_path: str | None = None) -> PCB:
        """Commit all changes and optionally save to file.

        Args:
            output_path: Optional path to save the PCB

        Returns:
            The modified PCB object
        """
        pcb = self._session.commit()
        if output_path:
            pcb.save(output_path)
        return pcb

    def _detect_anchor(
        self,
        components: list[str],
        definition: SubsystemDefinition,
    ) -> str:
        """Detect the anchor component from a list.

        Uses heuristics based on the subsystem type and component
        reference designators.

        Args:
            components: List of component references
            definition: Subsystem definition

        Returns:
            Best anchor component reference
        """
        # Prioritize ICs (U prefix) as anchors for most subsystems
        anchor_prefixes = {
            SubsystemType.POWER_SUPPLY: ["U", "IC"],
            SubsystemType.MCU_CORE: ["U", "IC"],
            SubsystemType.CONNECTOR: ["J", "P", "USB"],
            SubsystemType.TIMING: ["Y", "X"],
            SubsystemType.ANALOG_INPUT: ["U", "IC"],
            SubsystemType.INTERFACE: ["U", "IC"],
        }

        prefixes = anchor_prefixes.get(definition.subsystem_type, ["U"])

        for comp in components:
            for prefix in prefixes:
                if comp.upper().startswith(prefix):
                    return comp

        # Fall back to first component
        return components[0]

    def _calculate_anchor_position(
        self,
        anchor: str,
        near_edge: str | None,
        near_component: str | None,
    ) -> tuple[float, float]:
        """Calculate an anchor position based on constraints.

        Args:
            anchor: Anchor component reference
            near_edge: Optional edge constraint
            near_component: Optional component to place near

        Returns:
            (x, y) position for the anchor
        """
        # Get current position as default
        for fp in self._pcb.footprints:
            if fp.reference == anchor:
                base_pos = fp.position
                break
        else:
            # Component not found, use board center
            outline = self._pcb.get_board_outline()
            if outline:
                min_x = min(p[0] for p in outline)
                max_x = max(p[0] for p in outline)
                min_y = min(p[1] for p in outline)
                max_y = max(p[1] for p in outline)
                base_pos = ((min_x + max_x) / 2, (min_y + max_y) / 2)
            else:
                base_pos = (50.0, 50.0)

        # Apply edge constraint
        if near_edge:
            outline = self._pcb.get_board_outline()
            if outline:
                min_x = min(p[0] for p in outline)
                max_x = max(p[0] for p in outline)
                min_y = min(p[1] for p in outline)
                max_y = max(p[1] for p in outline)
                margin = 10.0

                if near_edge == "left":
                    base_pos = (min_x + margin, base_pos[1])
                elif near_edge == "right":
                    base_pos = (max_x - margin, base_pos[1])
                elif near_edge == "top":
                    base_pos = (base_pos[0], min_y + margin)
                elif near_edge == "bottom":
                    base_pos = (base_pos[0], max_y - margin)

        # Apply near_component constraint
        if near_component:
            for fp in self._pcb.footprints:
                if fp.reference == near_component:
                    # Place with offset from target
                    base_pos = (fp.position[0] + 15.0, fp.position[1])
                    break

        return base_pos


# Export public API
__all__ = [
    # Main class
    "Design",
    # Result types
    "SubsystemResult",
    "GroupResult",
    # Re-exports from submodules
    "Placement",
    "PlacementPlan",
    "PlacementStrategy",
    "SubsystemType",
    "SubsystemDefinition",
    "OptimizationGoal",
    "ValidationIssue",
    "ValidationSeverity",
    "Subsystem",
    "DecomposedStep",
    "DecompositionResult",
    "OperationType",
    # Utility functions
    "get_strategy",
    "get_subsystem_definition",
    "list_subsystem_types",
    "decompose_subsystem",
    "decompose_group",
]
