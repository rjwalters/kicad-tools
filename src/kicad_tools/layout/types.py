"""
Types for layout preservation module.

Defines ComponentAddress dataclass for hierarchical component identification.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentAddress:
    """
    Hierarchical address for a component in a schematic.

    Uses atopile-inspired hierarchical addressing:
    - `C1` - Component in root sheet
    - `power.C1` - Component in power subsheet
    - `power.ldo.C1` - Component in ldo subsheet of power sheet

    Attributes:
        full_path: Complete hierarchical path (e.g., "power.ldo.C1")
        sheet_path: Path to the sheet containing the component (e.g., "power.ldo")
        local_ref: Local reference designator (e.g., "C1")
        uuid: KiCad internal UUID for the component
    """

    full_path: str
    sheet_path: str
    local_ref: str
    uuid: str

    def __post_init__(self):
        """Validate address format."""
        if not self.local_ref:
            raise ValueError("local_ref cannot be empty")
        if not self.uuid:
            raise ValueError("uuid cannot be empty")

    @classmethod
    def from_parts(
        cls,
        sheet_path: str,
        local_ref: str,
        uuid: str,
    ) -> ComponentAddress:
        """
        Create a ComponentAddress from its parts.

        Args:
            sheet_path: Path to the containing sheet (empty for root)
            local_ref: Local reference designator
            uuid: KiCad component UUID

        Returns:
            ComponentAddress instance
        """
        if sheet_path:
            full_path = f"{sheet_path}.{local_ref}"
        else:
            full_path = local_ref

        return cls(
            full_path=full_path,
            sheet_path=sheet_path,
            local_ref=local_ref,
            uuid=uuid,
        )

    @property
    def depth(self) -> int:
        """
        Get the depth of this component in the hierarchy.

        Root level components have depth 0.
        """
        if not self.sheet_path:
            return 0
        return self.sheet_path.count(".") + 1

    @property
    def parent_path(self) -> str:
        """
        Get the parent sheet path.

        Returns empty string if at root level or one level deep.
        """
        if not self.sheet_path:
            return ""
        parts = self.sheet_path.rsplit(".", 1)
        return parts[0] if len(parts) > 1 else ""

    def __str__(self) -> str:
        """Return the full path as string representation."""
        return self.full_path

    def __repr__(self) -> str:
        return f"ComponentAddress({self.full_path!r}, uuid={self.uuid!r})"
