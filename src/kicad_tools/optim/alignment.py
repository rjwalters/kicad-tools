"""
Alignment constraints for organized component placement.

Provides alignment types and functions for:
- Grid snapping with configurable grid size
- Row/column alignment with tolerance
- Even distribution along axis
- Reference-based alignment

Example usage::

    from kicad_tools.optim import PlacementOptimizer
    from kicad_tools.optim.alignment import (
        AlignmentType,
        AlignmentConstraint,
        snap_to_grid,
        align_components,
        distribute_components,
    )

    # After optimization, snap to grid
    optimizer.run()
    snap_to_grid(optimizer, grid_mm=0.5, rotation_snap=90)

    # Align resistors in a row
    align_components(optimizer, ["R1", "R2", "R3", "R4"], axis="horizontal")

    # Distribute LEDs evenly
    distribute_components(optimizer, ["LED1", "LED2", "LED3", "LED4"],
                         axis="horizontal", spacing_mm=5.0)
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.optim.placement import PlacementOptimizer

__all__ = [
    "AlignmentType",
    "AlignmentConstraint",
    "snap_to_grid",
    "align_components",
    "distribute_components",
    "align_to_reference",
    "apply_alignment_constraints",
]


class AlignmentType(Enum):
    """Types of alignment constraints for component placement."""

    GRID = "grid"  # Snap to grid
    ROW = "row"  # Horizontal alignment (same Y)
    COLUMN = "column"  # Vertical alignment (same X)
    DISTRIBUTE = "distribute"  # Even spacing along axis
    REFERENCE = "reference"  # Align to reference component


@dataclass
class AlignmentConstraint:
    """
    An alignment constraint for component placement.

    Attributes:
        alignment_type: Type of alignment to apply
        components: List of component refs or patterns (e.g., "R*", "LED1")
        parameters: Type-specific parameters
    """

    alignment_type: AlignmentType
    components: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)

    def matches_ref(self, ref: str) -> bool:
        """Check if a component reference matches this constraint's patterns."""
        return any(fnmatch.fnmatch(ref, pattern) for pattern in self.components)


def _resolve_refs(
    optimizer: PlacementOptimizer,
    patterns: list[str],
) -> list[str]:
    """
    Resolve component reference patterns to actual refs.

    Args:
        optimizer: The placement optimizer with components
        patterns: List of patterns (e.g., ["R*", "C1", "C2"])

    Returns:
        List of matching component references
    """
    matched = []
    for comp in optimizer.components:
        for pattern in patterns:
            if fnmatch.fnmatch(comp.ref, pattern):
                matched.append(comp.ref)
                break
    return matched


def snap_to_grid(
    optimizer: PlacementOptimizer,
    grid_mm: float = 0.5,
    rotation_snap: int | None = 90,
) -> int:
    """
    Snap all component positions to a grid.

    Args:
        optimizer: The placement optimizer containing components
        grid_mm: Grid size in millimeters
        rotation_snap: Rotation snap in degrees (None to skip rotation snapping)

    Returns:
        Number of components snapped
    """
    if grid_mm <= 0:
        return 0

    count = 0
    for comp in optimizer.components:
        if comp.fixed:
            continue

        old_x, old_y = comp.x, comp.y
        old_rot = comp.rotation

        # Snap position
        comp.x = round(comp.x / grid_mm) * grid_mm
        comp.y = round(comp.y / grid_mm) * grid_mm
        comp.vx = 0.0
        comp.vy = 0.0

        # Snap rotation
        if rotation_snap is not None and rotation_snap > 0:
            comp.rotation = round(comp.rotation / rotation_snap) * rotation_snap % 360
            comp.angular_velocity = 0.0

        # Update pin positions after movement
        comp.update_pin_positions()

        if comp.x != old_x or comp.y != old_y or comp.rotation != old_rot:
            count += 1

    return count


def align_components(
    optimizer: PlacementOptimizer,
    refs: list[str],
    axis: str = "horizontal",
    reference: str = "center",
    tolerance_mm: float = 0.1,
) -> int:
    """
    Align specified components on an axis.

    Args:
        optimizer: The placement optimizer containing components
        refs: List of component references or patterns to align
        axis: Alignment axis - "horizontal" (align Y) or "vertical" (align X)
        reference: Alignment reference point - "center", "top", "bottom", "left", "right"
        tolerance_mm: Tolerance for already-aligned components

    Returns:
        Number of components moved
    """
    # Resolve patterns to refs
    actual_refs = _resolve_refs(optimizer, refs)
    if len(actual_refs) < 2:
        return 0

    # Get components
    components = [optimizer.get_component(ref) for ref in actual_refs]
    components = [c for c in components if c is not None and not c.fixed]

    if len(components) < 2:
        return 0

    # Calculate target position based on reference
    if axis == "horizontal":
        # Align along Y axis (same Y position)
        if reference == "top":
            target = min(c.y - c.height / 2 for c in components)
            for c in components:
                c.y = target + c.height / 2
        elif reference == "bottom":
            target = max(c.y + c.height / 2 for c in components)
            for c in components:
                c.y = target - c.height / 2
        else:  # center
            target = sum(c.y for c in components) / len(components)
            for c in components:
                c.y = target
    else:
        # Align along X axis (same X position)
        if reference == "left":
            target = min(c.x - c.width / 2 for c in components)
            for c in components:
                c.x = target + c.width / 2
        elif reference == "right":
            target = max(c.x + c.width / 2 for c in components)
            for c in components:
                c.x = target - c.width / 2
        else:  # center
            target = sum(c.x for c in components) / len(components)
            for c in components:
                c.x = target

    # Update pin positions
    count = 0
    for c in components:
        c.vx = 0.0
        c.vy = 0.0
        c.update_pin_positions()
        count += 1

    return count


def distribute_components(
    optimizer: PlacementOptimizer,
    refs: list[str],
    axis: str = "horizontal",
    spacing_mm: float | None = None,
) -> int:
    """
    Distribute components evenly along an axis.

    Args:
        optimizer: The placement optimizer containing components
        refs: List of component references or patterns to distribute
        axis: Distribution axis - "horizontal" or "vertical"
        spacing_mm: Fixed spacing in mm, or None for automatic even distribution

    Returns:
        Number of components moved
    """
    # Resolve patterns to refs
    actual_refs = _resolve_refs(optimizer, refs)
    if len(actual_refs) < 2:
        return 0

    # Get components
    components = [optimizer.get_component(ref) for ref in actual_refs]
    components = [c for c in components if c is not None and not c.fixed]

    if len(components) < 2:
        return 0

    # Sort by current position along axis
    if axis == "horizontal":
        components.sort(key=lambda c: c.x)
    else:
        components.sort(key=lambda c: c.y)

    # Calculate spacing
    if spacing_mm is not None:
        # Use fixed spacing
        start_pos = components[0].x if axis == "horizontal" else components[0].y
        for i, comp in enumerate(components):
            if axis == "horizontal":
                comp.x = start_pos + i * spacing_mm
            else:
                comp.y = start_pos + i * spacing_mm
    else:
        # Distribute evenly between first and last
        if axis == "horizontal":
            start = components[0].x
            end = components[-1].x
            total_dist = end - start
            if total_dist > 0 and len(components) > 1:
                spacing = total_dist / (len(components) - 1)
                for i, comp in enumerate(components):
                    comp.x = start + i * spacing
        else:
            start = components[0].y
            end = components[-1].y
            total_dist = end - start
            if total_dist > 0 and len(components) > 1:
                spacing = total_dist / (len(components) - 1)
                for i, comp in enumerate(components):
                    comp.y = start + i * spacing

    # Update pin positions
    count = 0
    for c in components:
        c.vx = 0.0
        c.vy = 0.0
        c.update_pin_positions()
        count += 1

    return count


def align_to_reference(
    optimizer: PlacementOptimizer,
    refs: list[str],
    reference_ref: str,
    edge: str = "left",
) -> int:
    """
    Align components to a reference component's edge.

    Args:
        optimizer: The placement optimizer containing components
        refs: List of component references to align
        reference_ref: Reference component to align to
        edge: Edge to align - "left", "right", "top", "bottom", "center_x", "center_y"

    Returns:
        Number of components moved
    """
    # Get reference component
    ref_comp = optimizer.get_component(reference_ref)
    if ref_comp is None:
        return 0

    # Resolve patterns to refs
    actual_refs = _resolve_refs(optimizer, refs)

    # Get components to align (excluding reference)
    components = [optimizer.get_component(ref) for ref in actual_refs]
    components = [c for c in components if c is not None and not c.fixed and c.ref != reference_ref]

    if not components:
        return 0

    # Calculate reference edge position
    if edge == "left":
        target_x = ref_comp.x - ref_comp.width / 2
        for c in components:
            c.x = target_x + c.width / 2
    elif edge == "right":
        target_x = ref_comp.x + ref_comp.width / 2
        for c in components:
            c.x = target_x - c.width / 2
    elif edge == "top":
        target_y = ref_comp.y - ref_comp.height / 2
        for c in components:
            c.y = target_y + c.height / 2
    elif edge == "bottom":
        target_y = ref_comp.y + ref_comp.height / 2
        for c in components:
            c.y = target_y - c.height / 2
    elif edge == "center_x":
        for c in components:
            c.x = ref_comp.x
    elif edge == "center_y":
        for c in components:
            c.y = ref_comp.y
    else:
        return 0

    # Update pin positions
    count = 0
    for c in components:
        c.vx = 0.0
        c.vy = 0.0
        c.update_pin_positions()
        count += 1

    return count


def apply_alignment_constraints(
    optimizer: PlacementOptimizer,
    constraints: list[AlignmentConstraint],
) -> dict[str, int]:
    """
    Apply multiple alignment constraints to an optimizer.

    Args:
        optimizer: The placement optimizer containing components
        constraints: List of alignment constraints to apply

    Returns:
        Dictionary mapping constraint type to number of components affected
    """
    results: dict[str, int] = {}

    for constraint in constraints:
        count = 0

        if constraint.alignment_type == AlignmentType.GRID:
            grid_mm = constraint.parameters.get("grid_mm", 0.5)
            rotation_snap = constraint.parameters.get("rotation_snap", 90)
            count = snap_to_grid(optimizer, grid_mm, rotation_snap)

        elif constraint.alignment_type == AlignmentType.ROW:
            tolerance = constraint.parameters.get("tolerance_mm", 0.1)
            reference = constraint.parameters.get("reference", "center")
            count = align_components(
                optimizer,
                constraint.components,
                axis="horizontal",
                reference=reference,
                tolerance_mm=tolerance,
            )

        elif constraint.alignment_type == AlignmentType.COLUMN:
            tolerance = constraint.parameters.get("tolerance_mm", 0.1)
            reference = constraint.parameters.get("reference", "center")
            count = align_components(
                optimizer,
                constraint.components,
                axis="vertical",
                reference=reference,
                tolerance_mm=tolerance,
            )

        elif constraint.alignment_type == AlignmentType.DISTRIBUTE:
            axis = constraint.parameters.get("axis", "horizontal")
            spacing = constraint.parameters.get("spacing_mm")
            count = distribute_components(optimizer, constraint.components, axis, spacing)

        elif constraint.alignment_type == AlignmentType.REFERENCE:
            ref = constraint.parameters.get("reference", "")
            edge = constraint.parameters.get("edge", "left")
            if ref:
                count = align_to_reference(optimizer, constraint.components, ref, edge)

        # Accumulate results by type
        type_name = constraint.alignment_type.value
        results[type_name] = results.get(type_name, 0) + count

    return results
