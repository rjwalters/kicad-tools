"""Subcircuit layout extraction and application.

Provides functionality to extract relative layouts from subcircuits
and apply them to new positions, preserving internal component relationships.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types import ComponentOffset, SubcircuitLayout

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB, Footprint


@dataclass
class ComponentInfo:
    """Information about a component for subcircuit extraction.

    Attributes:
        reference: Full reference designator (e.g., "U3", "C1")
        local_ref: Reference relative to subcircuit (same as reference for now)
        position: Tuple of (x, y) position in mm
        rotation: Rotation in degrees
        footprint_name: Name of the footprint
    """

    reference: str
    local_ref: str
    position: tuple[float, float]
    rotation: float
    footprint_name: str

    @classmethod
    def from_footprint(cls, fp: Footprint) -> ComponentInfo:
        """Create ComponentInfo from a Footprint.

        Args:
            fp: The PCB footprint

        Returns:
            ComponentInfo instance
        """
        return cls(
            reference=fp.reference,
            local_ref=fp.reference,
            position=fp.position,
            rotation=fp.rotation,
            footprint_name=fp.name,
        )


class SubcircuitExtractor:
    """Extracts subcircuit layouts with anchor-relative offsets.

    This class analyzes a group of components and extracts their relative
    positions using an anchor component as the reference point.

    The anchor selection prioritizes:
    1. ICs (Uxx) - typically the main component
    2. Transistors (Qxx) - common for discrete circuits
    3. Largest passive by footprint size

    Example:
        >>> extractor = SubcircuitExtractor()
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> layout = extractor.extract(
        ...     pcb,
        ...     component_refs=["U3", "C1", "C2", "R1"],
        ...     subcircuit_path="power.ldo"
        ... )
        >>> print(layout.anchor_ref)  # "U3"
        >>> print(layout.offsets["C1"])  # ComponentOffset with relative position
    """

    # Anchor priority by reference prefix (higher = more preferred)
    ANCHOR_PRIORITY = {
        "U": 100,  # ICs - most preferred
        "Q": 90,  # Transistors
        "D": 80,  # Diodes
        "L": 70,  # Inductors
        "T": 60,  # Transformers
        "C": 50,  # Capacitors
        "R": 40,  # Resistors
    }

    def __init__(
        self,
        anchor_selector: Callable[[Sequence[ComponentInfo]], ComponentInfo] | None = None,
    ):
        """Initialize the extractor.

        Args:
            anchor_selector: Optional custom function to select anchor component.
                             If not provided, uses default priority-based selection.
        """
        self._anchor_selector = anchor_selector or self._default_anchor_selector

    def extract(
        self,
        pcb: PCB,
        component_refs: Sequence[str],
        subcircuit_path: str = "",
    ) -> SubcircuitLayout:
        """Extract subcircuit layout with anchor-relative offsets.

        Args:
            pcb: The PCB to extract from
            component_refs: List of reference designators in the subcircuit
            subcircuit_path: Hierarchical path for the subcircuit (optional)

        Returns:
            SubcircuitLayout with anchor and component offsets

        Raises:
            ValueError: If no components found or refs list is empty
        """
        if not component_refs:
            raise ValueError("component_refs cannot be empty")

        # Gather component info
        components: list[ComponentInfo] = []
        for ref in component_refs:
            fp = pcb.get_footprint(ref)
            if fp is not None:
                components.append(ComponentInfo.from_footprint(fp))

        if not components:
            raise ValueError(f"No components found for refs: {component_refs}")

        # Select anchor
        anchor = self._anchor_selector(components)

        # Get anchor position
        anchor_pos = (anchor.position[0], anchor.position[1], anchor.rotation)

        # Calculate offsets for other components
        offsets: dict[str, ComponentOffset] = {}
        layer = "F.Cu"  # Default, will be set from first component

        for comp in components:
            # Get layer from first component
            fp = pcb.get_footprint(comp.reference)
            if fp is not None:
                layer = fp.layer

            if comp.reference == anchor.reference:
                continue

            # Calculate offset relative to anchor
            dx = comp.position[0] - anchor.position[0]
            dy = comp.position[1] - anchor.position[1]
            rotation_delta = comp.rotation - anchor.rotation

            # Normalize rotation delta to [-180, 180)
            while rotation_delta >= 180:
                rotation_delta -= 360
            while rotation_delta < -180:
                rotation_delta += 360

            offsets[comp.local_ref] = ComponentOffset(
                ref=comp.local_ref,
                dx=dx,
                dy=dy,
                rotation_delta=rotation_delta,
            )

        return SubcircuitLayout(
            path=subcircuit_path,
            anchor_ref=anchor.reference,
            anchor_position=anchor_pos,
            offsets=offsets,
            layer=layer,
        )

    def extract_by_pattern(
        self,
        pcb: PCB,
        pattern: str,
        subcircuit_path: str = "",
    ) -> SubcircuitLayout:
        """Extract subcircuit layout by matching component references.

        Args:
            pcb: The PCB to extract from
            pattern: Regex pattern to match reference designators
            subcircuit_path: Hierarchical path for the subcircuit

        Returns:
            SubcircuitLayout with anchor and component offsets

        Raises:
            ValueError: If no components match the pattern
        """
        regex = re.compile(pattern)
        matching_refs = [fp.reference for fp in pcb.footprints if regex.match(fp.reference)]

        if not matching_refs:
            raise ValueError(f"No components match pattern: {pattern}")

        return self.extract(pcb, matching_refs, subcircuit_path)

    def _default_anchor_selector(self, components: Sequence[ComponentInfo]) -> ComponentInfo:
        """Select anchor component using default priority rules.

        Priority:
        1. Reference prefix priority (ICs > transistors > passives)
        2. Within same prefix, lower number (U1 before U2)

        Args:
            components: List of components to choose from

        Returns:
            Selected anchor component
        """
        if len(components) == 1:
            return components[0]

        def priority_key(comp: ComponentInfo) -> tuple[int, int]:
            # Extract prefix and number from reference
            match = re.match(r"([A-Z]+)(\d+)", comp.reference)
            if match:
                prefix = match.group(1)
                number = int(match.group(2))
                priority = self.ANCHOR_PRIORITY.get(prefix, 0)
                return (-priority, number)  # Negative priority for descending sort
            return (0, 0)

        sorted_components = sorted(components, key=priority_key)
        return sorted_components[0]


def apply_subcircuit(
    pcb: PCB,
    layout: SubcircuitLayout,
    new_anchor_position: tuple[float, float, float],
    ref_mapping: dict[str, str] | None = None,
) -> dict[str, tuple[float, float, float]]:
    """Apply subcircuit layout to new position with optional ref remapping.

    Places all components in the subcircuit at their new positions relative
    to the new anchor position, preserving the relative layout.

    Args:
        pcb: The PCB to modify
        layout: The subcircuit layout to apply
        new_anchor_position: New (x, y, rotation) for the anchor component
        ref_mapping: Optional mapping from layout refs to actual PCB refs.
                     Used when instantiating multiple copies with different
                     reference designators.

    Returns:
        Dictionary mapping refs to their new (x, y, rotation) positions

    Example:
        >>> # Apply original layout to new position
        >>> apply_subcircuit(pcb, ldo_layout, (80.0, 40.0, 90.0))

        >>> # Apply layout with remapped references (for multiple instances)
        >>> apply_subcircuit(
        ...     pcb, ldo_layout, (80.0, 40.0, 0.0),
        ...     ref_mapping={"U3": "U5", "C1": "C10", "C2": "C11"}
        ... )
    """
    ref_mapping = ref_mapping or {}
    new_positions: dict[str, tuple[float, float, float]] = {}

    # Create layout with new anchor position
    new_layout = layout.with_anchor_position(new_anchor_position)

    # Get all new positions
    all_positions = new_layout.get_all_positions()

    # Apply to PCB
    for layout_ref, position in all_positions.items():
        # Get the actual PCB reference (may be remapped)
        pcb_ref = ref_mapping.get(layout_ref, layout_ref)

        # Update position in PCB
        x, y, rotation = position
        pcb.update_footprint_position(pcb_ref, x, y, rotation)

        new_positions[pcb_ref] = position

    return new_positions


def rotate_point(
    x: float, y: float, angle_deg: float, origin_x: float = 0.0, origin_y: float = 0.0
) -> tuple[float, float]:
    """Rotate a point around an origin.

    Args:
        x: X coordinate of point
        y: Y coordinate of point
        angle_deg: Rotation angle in degrees (counter-clockwise)
        origin_x: X coordinate of rotation origin
        origin_y: Y coordinate of rotation origin

    Returns:
        Tuple of (new_x, new_y)
    """
    if angle_deg == 0:
        return x, y

    # Translate to origin
    dx = x - origin_x
    dy = y - origin_y

    # Rotate
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)

    rotated_x = dx * cos_a - dy * sin_a
    rotated_y = dx * sin_a + dy * cos_a

    # Translate back
    return origin_x + rotated_x, origin_y + rotated_y
