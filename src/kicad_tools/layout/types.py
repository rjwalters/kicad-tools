"""Layout preservation type definitions.

Provides data structures for subcircuit layout extraction and application,
enabling relative positioning of components within subcircuits.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ComponentOffset:
    """Offset of a component relative to a subcircuit's anchor.

    Stores position and rotation relative to the anchor component,
    allowing the subcircuit to be placed at any position while
    preserving internal component relationships.

    Attributes:
        ref: Local reference designator (e.g., "C1", "R2")
        dx: X offset from anchor position in mm
        dy: Y offset from anchor position in mm
        rotation_delta: Rotation relative to anchor in degrees
    """

    ref: str
    dx: float
    dy: float
    rotation_delta: float = 0.0

    def rotated(self, angle_deg: float) -> tuple[float, float]:
        """Get offset rotated by given angle.

        Args:
            angle_deg: Rotation angle in degrees

        Returns:
            Tuple of (rotated_dx, rotated_dy)
        """
        import math

        if angle_deg == 0:
            return self.dx, self.dy

        rad = math.radians(angle_deg)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)

        rotated_dx = self.dx * cos_a - self.dy * sin_a
        rotated_dy = self.dx * sin_a + self.dy * cos_a

        return rotated_dx, rotated_dy


@dataclass
class SubcircuitLayout:
    """Layout of a subcircuit with anchor-relative positioning.

    Represents the spatial arrangement of components within a subcircuit,
    using an anchor component as the reference point. All other components
    are stored as offsets from this anchor.

    This allows subcircuits to be:
    - Moved to new locations while preserving internal layout
    - Rotated as a unit (90, 180, 270 degrees)
    - Instantiated multiple times with consistent spacing

    Attributes:
        path: Hierarchical path to the subcircuit (e.g., "power.ldo")
        anchor_ref: Reference of the anchor component (e.g., "U1")
        anchor_position: Tuple of (x, y, rotation) for the anchor
        offsets: Dictionary mapping local refs to their ComponentOffset
        layer: PCB layer the subcircuit is on (e.g., "F.Cu")

    Example:
        >>> layout = SubcircuitLayout(
        ...     path="power.ldo",
        ...     anchor_ref="U3",
        ...     anchor_position=(50.0, 30.0, 0.0),
        ...     offsets={
        ...         "C1": ComponentOffset("C1", -2.0, -1.5, 0.0),
        ...         "C2": ComponentOffset("C2", 2.0, -1.5, 0.0),
        ...     }
        ... )
    """

    path: str
    anchor_ref: str
    anchor_position: tuple[float, float, float]  # x, y, rotation
    offsets: dict[str, ComponentOffset] = field(default_factory=dict)
    layer: str = "F.Cu"

    @property
    def component_count(self) -> int:
        """Total number of components (anchor + offsets)."""
        return 1 + len(self.offsets)

    @property
    def component_refs(self) -> list[str]:
        """List of all component references in the subcircuit."""
        return [self.anchor_ref] + list(self.offsets.keys())

    def get_position(self, ref: str) -> tuple[float, float, float] | None:
        """Get absolute position for a component in this layout.

        Args:
            ref: Local reference designator

        Returns:
            Tuple of (x, y, rotation) or None if not found
        """
        if ref == self.anchor_ref:
            return self.anchor_position

        offset = self.offsets.get(ref)
        if offset is None:
            return None

        anchor_x, anchor_y, anchor_rot = self.anchor_position

        # Rotate offset by anchor rotation
        rotated_dx, rotated_dy = offset.rotated(anchor_rot)

        return (
            anchor_x + rotated_dx,
            anchor_y + rotated_dy,
            (anchor_rot + offset.rotation_delta) % 360,
        )

    def with_anchor_position(
        self, new_position: tuple[float, float, float]
    ) -> SubcircuitLayout:
        """Create a copy of this layout with a new anchor position.

        Args:
            new_position: New (x, y, rotation) for the anchor

        Returns:
            New SubcircuitLayout with updated anchor position
        """
        return SubcircuitLayout(
            path=self.path,
            anchor_ref=self.anchor_ref,
            anchor_position=new_position,
            offsets=dict(self.offsets),
            layer=self.layer,
        )

    def get_all_positions(self) -> dict[str, tuple[float, float, float]]:
        """Get absolute positions for all components.

        Returns:
            Dictionary mapping refs to (x, y, rotation) tuples
        """
        positions: dict[str, tuple[float, float, float]] = {}

        for ref in self.component_refs:
            pos = self.get_position(ref)
            if pos is not None:
                positions[ref] = pos

        return positions
