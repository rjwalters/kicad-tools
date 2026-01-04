"""
Constraint conflict detection for PCB design.

Detects conflicts between placement/routing constraints like "keepout overlaps
required via" and provides resolution guidance. Enables agents to understand
and resolve constraint conflicts during optimization.

Example usage::

    from kicad_tools.constraints.conflict import (
        ConstraintConflictDetector,
        ConflictType,
    )
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.optim.keepout import detect_keepout_zones

    pcb = PCB.load("board.kicad_pcb")
    detector = ConstraintConflictDetector()

    # Load constraints
    keepouts = detect_keepout_zones(pcb)
    groupings = load_grouping_constraints("constraints.yaml")

    # Detect conflicts
    conflicts = detector.detect(
        keepout_zones=keepouts,
        grouping_constraints=groupings,
        pcb=pcb,
    )

    for conflict in conflicts:
        print(f"Conflict: {conflict.description}")
        for resolution in conflict.resolutions:
            print(f"  Option: {resolution.action} - {resolution.trade_off}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.constraints.locks import RegionConstraint
    from kicad_tools.optim.constraints import GroupingConstraint
    from kicad_tools.optim.keepout import KeepoutZone
    from kicad_tools.schema.pcb import PCB


__all__ = [
    "ConflictType",
    "ConflictResolution",
    "ConstraintConflict",
    "ConstraintConflictDetector",
]


class ConflictType(Enum):
    """Types of constraint conflicts."""

    OVERLAP = "overlap"  # Two constraints have overlapping regions
    CONTRADICTION = "contradiction"  # Constraints require mutually exclusive states
    IMPOSSIBLE = "impossible"  # No valid solution exists


@dataclass
class ConflictResolution:
    """
    A possible resolution for a constraint conflict.

    Provides guidance on how to resolve a conflict, including what action
    to take and what trade-offs are involved.
    """

    action: str  # What to do (e.g., "shrink_keepout", "reroute_signal")
    description: str  # Human-readable explanation
    trade_off: str  # What you lose by choosing this resolution
    priority: int = 0  # Higher = more preferred (0 = neutral)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "action": self.action,
            "description": self.description,
            "trade_off": self.trade_off,
            "priority": self.priority,
        }


@dataclass
class ConstraintConflict:
    """
    A conflict between two or more constraints.

    Records the conflicting constraints, the type of conflict, and
    possible resolutions with their trade-offs.
    """

    constraint1_type: str  # "keepout", "grouping", "region", "routing"
    constraint1_name: str
    constraint2_type: str
    constraint2_name: str
    conflict_type: ConflictType
    description: str
    location: tuple[float, float] | None = None  # Conflict location (x, y) in mm
    priority_winner: str | None = None  # Which constraint should win, if determinable
    resolutions: list[ConflictResolution] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "constraint1": {
                "type": self.constraint1_type,
                "name": self.constraint1_name,
            },
            "constraint2": {
                "type": self.constraint2_type,
                "name": self.constraint2_name,
            },
            "conflict_type": self.conflict_type.value,
            "description": self.description,
            "location": self.location,
            "priority_winner": self.priority_winner,
            "resolutions": [r.to_dict() for r in self.resolutions],
        }


class ConstraintConflictDetector:
    """
    Detects conflicts between constraints.

    Analyzes different types of constraints (keepout zones, grouping rules,
    region constraints, routing requirements) and identifies where they
    conflict with each other.

    Example::

        detector = ConstraintConflictDetector()
        conflicts = detector.detect(
            keepout_zones=keepouts,
            grouping_constraints=groups,
            region_constraints=regions,
            pcb=pcb,
        )
    """

    def detect(
        self,
        keepout_zones: list[KeepoutZone] | None = None,
        grouping_constraints: list[GroupingConstraint] | None = None,
        region_constraints: list[RegionConstraint] | None = None,
        pcb: PCB | None = None,
    ) -> list[ConstraintConflict]:
        """
        Detect all constraint conflicts.

        Args:
            keepout_zones: List of keepout zones from optim.keepout
            grouping_constraints: List of grouping constraints from optim.constraints
            region_constraints: List of region constraints from constraints.locks
            pcb: PCB object for context (component positions, board outline)

        Returns:
            List of detected ConstraintConflict objects
        """
        conflicts: list[ConstraintConflict] = []
        keepout_zones = keepout_zones or []
        grouping_constraints = grouping_constraints or []
        region_constraints = region_constraints or []

        # Check keepout vs keepout conflicts
        for k1, k2 in combinations(keepout_zones, 2):
            conflict = self._check_keepout_vs_keepout(k1, k2)
            if conflict:
                conflict.resolutions = self._find_keepout_keepout_resolutions(k1, k2)
                conflicts.append(conflict)

        # Check keepout vs grouping conflicts
        for keepout in keepout_zones:
            for grouping in grouping_constraints:
                conflict = self._check_keepout_vs_grouping(keepout, grouping, pcb)
                if conflict:
                    conflict.resolutions = self._find_keepout_grouping_resolutions(
                        keepout, grouping
                    )
                    conflicts.append(conflict)

        # Check keepout vs region conflicts
        for keepout in keepout_zones:
            for region in region_constraints:
                conflict = self._check_keepout_vs_region(keepout, region)
                if conflict:
                    conflict.resolutions = self._find_keepout_region_resolutions(keepout, region)
                    conflicts.append(conflict)

        # Check grouping vs region conflicts (edge placement)
        for grouping in grouping_constraints:
            for region in region_constraints:
                conflict = self._check_grouping_vs_region(grouping, region, pcb)
                if conflict:
                    conflict.resolutions = self._find_grouping_region_resolutions(grouping, region)
                    conflicts.append(conflict)

        # Check grouping vs grouping conflicts
        for g1, g2 in combinations(grouping_constraints, 2):
            conflict = self._check_grouping_vs_grouping(g1, g2, pcb)
            if conflict:
                conflict.resolutions = self._find_grouping_grouping_resolutions(g1, g2)
                conflicts.append(conflict)

        return conflicts

    def _check_keepout_vs_keepout(
        self,
        k1: KeepoutZone,
        k2: KeepoutZone,
    ) -> ConstraintConflict | None:
        """Check if two keepout zones overlap."""
        # Get polygons
        poly1 = k1.get_expanded_polygon()
        poly2 = k2.get_expanded_polygon()

        # Check for overlap by testing if any vertex of one is inside the other
        overlap_point = None
        for v in poly1.vertices:
            if poly2.contains_point(v):
                overlap_point = (v.x, v.y)
                break
        if not overlap_point:
            for v in poly2.vertices:
                if poly1.contains_point(v):
                    overlap_point = (v.x, v.y)
                    break

        if overlap_point:
            return ConstraintConflict(
                constraint1_type="keepout",
                constraint1_name=k1.name,
                constraint2_type="keepout",
                constraint2_name=k2.name,
                conflict_type=ConflictType.OVERLAP,
                description=f"Keepout zones '{k1.name}' and '{k2.name}' overlap",
                location=overlap_point,
            )
        return None

    def _check_keepout_vs_grouping(
        self,
        keepout: KeepoutZone,
        grouping: GroupingConstraint,
        pcb: PCB | None,
    ) -> ConstraintConflict | None:
        """Check if a keepout zone conflicts with a grouping constraint."""
        if pcb is None:
            return None

        # Get component positions for grouping members
        member_positions: dict[str, tuple[float, float]] = {}
        for fp in pcb.footprints:
            if fp.reference in grouping.members:
                member_positions[fp.reference] = fp.position

        if not member_positions:
            return None

        # Check if the keepout overlaps with the bounding box of the group
        # or if any member must be placed in the keepout
        keepout_poly = keepout.get_expanded_polygon()

        # Check for components that are currently in the keepout
        components_in_keepout = []
        for ref, (x, y) in member_positions.items():
            from kicad_tools.optim.geometry import Vector2D

            if keepout_poly.contains_point(Vector2D(x, y)):
                components_in_keepout.append(ref)

        if components_in_keepout:
            # Check if there's a max_distance constraint that would require
            # staying near the keepout
            for constraint in grouping.constraints:
                if constraint.constraint_type.value == "max_distance":
                    anchor = constraint.parameters.get("anchor")
                    if anchor in member_positions:
                        anchor_x, anchor_y = member_positions[anchor]
                        from kicad_tools.optim.geometry import Vector2D

                        if keepout_poly.contains_point(Vector2D(anchor_x, anchor_y)):
                            return ConstraintConflict(
                                constraint1_type="keepout",
                                constraint1_name=keepout.name,
                                constraint2_type="grouping",
                                constraint2_name=grouping.name,
                                conflict_type=ConflictType.CONTRADICTION,
                                description=(
                                    f"Group '{grouping.name}' anchor '{anchor}' is inside "
                                    f"keepout '{keepout.name}', but group members must stay nearby"
                                ),
                                location=member_positions[anchor],
                            )

        return None

    def _check_keepout_vs_region(
        self,
        keepout: KeepoutZone,
        region: RegionConstraint,
    ) -> ConstraintConflict | None:
        """Check if a keepout zone conflicts with a region constraint."""
        # Get region bounds
        bounds = region.bounds
        region_min_x = bounds.get("x_min", float("-inf"))
        region_max_x = bounds.get("x_max", float("inf"))
        region_min_y = bounds.get("y_min", float("-inf"))
        region_max_y = bounds.get("y_max", float("inf"))

        # Get keepout polygon
        keepout_poly = keepout.get_expanded_polygon()
        keepout_vertices = [(v.x, v.y) for v in keepout_poly.vertices]

        if not keepout_vertices:
            return None

        # Check if keepout overlaps with region bounds
        keepout_min_x = min(v[0] for v in keepout_vertices)
        keepout_max_x = max(v[0] for v in keepout_vertices)
        keepout_min_y = min(v[1] for v in keepout_vertices)
        keepout_max_y = max(v[1] for v in keepout_vertices)

        # Check for bounding box overlap
        x_overlap = keepout_min_x <= region_max_x and keepout_max_x >= region_min_x
        y_overlap = keepout_min_y <= region_max_y and keepout_max_y >= region_min_y

        if x_overlap and y_overlap:
            # Calculate overlap center
            overlap_x = (max(keepout_min_x, region_min_x) + min(keepout_max_x, region_max_x)) / 2
            overlap_y = (max(keepout_min_y, region_min_y) + min(keepout_max_y, region_max_y)) / 2

            return ConstraintConflict(
                constraint1_type="keepout",
                constraint1_name=keepout.name,
                constraint2_type="region",
                constraint2_name=region.name,
                conflict_type=ConflictType.OVERLAP,
                description=(
                    f"Keepout '{keepout.name}' overlaps with region '{region.name}' "
                    f"({region.reason})"
                ),
                location=(overlap_x, overlap_y),
            )

        return None

    def _check_grouping_vs_region(
        self,
        grouping: GroupingConstraint,
        region: RegionConstraint,
        pcb: PCB | None,
    ) -> ConstraintConflict | None:
        """Check if a grouping constraint conflicts with a region constraint."""
        if pcb is None:
            return None

        # Get member positions
        member_positions: dict[str, tuple[float, float]] = {}
        for fp in pcb.footprints:
            if fp.reference in grouping.members:
                member_positions[fp.reference] = fp.position

        if not member_positions:
            return None

        # Check for conflicts where:
        # 1. A component is in the disallowed list but is inside the region
        # 2. A component needs to be in a specific position (grouping) but region forbids it

        for ref, (x, y) in member_positions.items():
            in_region = region.contains_point(x, y)
            is_disallowed = ref in region.disallowed_components

            if in_region and is_disallowed:
                return ConstraintConflict(
                    constraint1_type="grouping",
                    constraint1_name=grouping.name,
                    constraint2_type="region",
                    constraint2_name=region.name,
                    conflict_type=ConflictType.CONTRADICTION,
                    description=(
                        f"Component '{ref}' in group '{grouping.name}' is inside "
                        f"region '{region.name}' but is in the disallowed list"
                    ),
                    location=(x, y),
                )

        return None

    def _check_grouping_vs_grouping(
        self,
        g1: GroupingConstraint,
        g2: GroupingConstraint,
        pcb: PCB | None,
    ) -> ConstraintConflict | None:
        """Check if two grouping constraints conflict."""
        # Check for shared members with conflicting requirements
        shared_members = set(g1.members) & set(g2.members)
        if not shared_members:
            return None

        # Check for conflicting constraints on shared members
        for member in shared_members:
            # Check for conflicting max_distance constraints
            g1_anchors = []
            g2_anchors = []

            for c in g1.constraints:
                if c.constraint_type.value == "max_distance":
                    g1_anchors.append(
                        (c.parameters.get("anchor"), c.parameters.get("radius_mm", 0))
                    )

            for c in g2.constraints:
                if c.constraint_type.value == "max_distance":
                    g2_anchors.append(
                        (c.parameters.get("anchor"), c.parameters.get("radius_mm", 0))
                    )

            # If both groups have different anchors and the anchors are far apart,
            # the shared member can't satisfy both constraints
            if g1_anchors and g2_anchors and pcb:
                for anchor1, radius1 in g1_anchors:
                    for anchor2, radius2 in g2_anchors:
                        if anchor1 and anchor2 and anchor1 != anchor2:
                            # Find positions of anchors
                            pos1 = pos2 = None
                            for fp in pcb.footprints:
                                if fp.reference == anchor1:
                                    pos1 = fp.position
                                if fp.reference == anchor2:
                                    pos2 = fp.position

                            if pos1 and pos2:
                                dist = math.sqrt(
                                    (pos1[0] - pos2[0]) ** 2 + (pos1[1] - pos2[1]) ** 2
                                )
                                if dist > radius1 + radius2:
                                    return ConstraintConflict(
                                        constraint1_type="grouping",
                                        constraint1_name=g1.name,
                                        constraint2_type="grouping",
                                        constraint2_name=g2.name,
                                        conflict_type=ConflictType.IMPOSSIBLE,
                                        description=(
                                            f"Component '{member}' cannot satisfy both groups: "
                                            f"must be within {radius1}mm of '{anchor1}' ({g1.name}) "
                                            f"and within {radius2}mm of '{anchor2}' ({g2.name}), "
                                            f"but anchors are {dist:.1f}mm apart"
                                        ),
                                        location=pos1,
                                    )

        return None

    def _find_keepout_keepout_resolutions(
        self,
        k1: KeepoutZone,
        k2: KeepoutZone,
    ) -> list[ConflictResolution]:
        """Find resolutions for keepout vs keepout conflict."""
        resolutions = []

        # Option 1: Shrink one keepout
        resolutions.append(
            ConflictResolution(
                action="shrink_keepout",
                description=f"Reduce the size of '{k1.name}' to eliminate overlap",
                trade_off="Less protected area around the original keepout zone",
                priority=1,
            )
        )

        # Option 2: Shrink the other keepout
        resolutions.append(
            ConflictResolution(
                action="shrink_keepout",
                description=f"Reduce the size of '{k2.name}' to eliminate overlap",
                trade_off="Less protected area around the original keepout zone",
                priority=1,
            )
        )

        # Option 3: Merge into single keepout
        resolutions.append(
            ConflictResolution(
                action="merge_keepouts",
                description="Combine both keepouts into a single larger zone",
                trade_off="Increased restricted area, may limit placement options",
                priority=0,
            )
        )

        # Option 4: Remove one keepout
        resolutions.append(
            ConflictResolution(
                action="remove_keepout",
                description=f"Remove '{k2.name}' if it's less critical",
                trade_off="Complete loss of protection from the removed zone",
                priority=-1,
            )
        )

        return resolutions

    def _find_keepout_grouping_resolutions(
        self,
        keepout: KeepoutZone,
        grouping: GroupingConstraint,
    ) -> list[ConflictResolution]:
        """Find resolutions for keepout vs grouping conflict."""
        return [
            ConflictResolution(
                action="shrink_keepout",
                description=f"Reduce '{keepout.name}' to allow group members",
                trade_off="Less protection in the keepout area",
                priority=1,
            ),
            ConflictResolution(
                action="relax_grouping",
                description=f"Increase max_distance for '{grouping.name}'",
                trade_off="Group members may be placed further from anchor",
                priority=0,
            ),
            ConflictResolution(
                action="move_group",
                description=f"Move entire group '{grouping.name}' away from keepout",
                trade_off="Group may end up in less optimal location",
                priority=0,
            ),
            ConflictResolution(
                action="redesign_grouping",
                description="Split group into multiple smaller groups",
                trade_off="More complex constraint management",
                priority=-1,
            ),
        ]

    def _find_keepout_region_resolutions(
        self,
        keepout: KeepoutZone,
        region: RegionConstraint,
    ) -> list[ConflictResolution]:
        """Find resolutions for keepout vs region conflict."""
        return [
            ConflictResolution(
                action="shrink_keepout",
                description=f"Reduce '{keepout.name}' to fit within region boundaries",
                trade_off="Less protected area",
                priority=1,
            ),
            ConflictResolution(
                action="adjust_region",
                description=f"Modify '{region.name}' bounds to avoid keepout",
                trade_off="Region may not cover intended area",
                priority=0,
            ),
            ConflictResolution(
                action="allow_exception",
                description=f"Add exception in '{region.name}' for keepout area",
                trade_off="More complex region definition",
                priority=0,
            ),
        ]

    def _find_grouping_region_resolutions(
        self,
        grouping: GroupingConstraint,
        region: RegionConstraint,
    ) -> list[ConflictResolution]:
        """Find resolutions for grouping vs region conflict."""
        return [
            ConflictResolution(
                action="update_region_allowlist",
                description=f"Add group members to '{region.name}' allowed list",
                trade_off="Region constraint becomes less strict",
                priority=1,
            ),
            ConflictResolution(
                action="move_group_to_edge",
                description=f"Relocate '{grouping.name}' to board edge/allowed area",
                trade_off="May affect signal routing or thermal performance",
                priority=0,
            ),
            ConflictResolution(
                action="relax_grouping",
                description="Remove conflicting component from group",
                trade_off="Group may not achieve intended purpose",
                priority=-1,
            ),
        ]

    def _find_grouping_grouping_resolutions(
        self,
        g1: GroupingConstraint,
        g2: GroupingConstraint,
    ) -> list[ConflictResolution]:
        """Find resolutions for grouping vs grouping conflict."""
        return [
            ConflictResolution(
                action="increase_radii",
                description="Increase max_distance for both groups",
                trade_off="Groups become less tightly coupled",
                priority=1,
            ),
            ConflictResolution(
                action="move_anchor",
                description=f"Move anchor of '{g1.name}' closer to '{g2.name}'",
                trade_off="May affect component positioning strategy",
                priority=0,
            ),
            ConflictResolution(
                action="remove_from_group",
                description="Remove shared component from one of the groups",
                trade_off="One group loses intended member",
                priority=-1,
            ),
            ConflictResolution(
                action="merge_groups",
                description="Combine both groups into a single larger group",
                trade_off="More complex constraint, may be harder to satisfy",
                priority=-1,
            ),
        ]
