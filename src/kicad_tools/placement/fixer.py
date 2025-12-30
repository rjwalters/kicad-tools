"""Placement conflict resolution for KiCad PCBs.

Suggests and applies fixes for placement conflicts:
- Calculate minimum displacement to resolve conflicts
- Support constraint-based fixing (anchor components)
- Preview changes before applying
- Verify fixes don't create new conflicts
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

from .analyzer import DesignRules, PlacementAnalyzer
from .conflict import (
    Conflict,
    ConflictType,
    PlacementFix,
    Point,
)


class FixStrategy(Enum):
    """Strategy for resolving placement conflicts."""

    SPREAD = "spread"  # Move components apart minimally
    COMPACT = "compact"  # Minimize board area while fixing
    ANCHOR = "anchor"  # Keep specified components fixed


@dataclass
class FixResult:
    """Result of applying placement fixes."""

    success: bool
    fixes_applied: int
    new_conflicts: int
    message: str


class PlacementFixer:
    """Suggests and applies fixes for placement conflicts.

    Usage:
        fixer = PlacementFixer()
        fixes = fixer.suggest_fixes(conflicts)

        # Preview changes
        for fix in fixes:
            print(fix)

        # Apply fixes
        result = fixer.apply_fixes("board.kicad_pcb", fixes, "board-fixed.kicad_pcb")
    """

    def __init__(
        self,
        strategy: FixStrategy = FixStrategy.SPREAD,
        anchored: Optional[Set[str]] = None,
        verbose: bool = False,
    ):
        """Initialize fixer.

        Args:
            strategy: Strategy for resolving conflicts
            anchored: Set of component references to keep fixed
            verbose: Print progress messages
        """
        self.strategy = strategy
        self.anchored = anchored or set()
        self.verbose = verbose

    def suggest_fixes(
        self,
        conflicts: List[Conflict],
        analyzer: Optional[PlacementAnalyzer] = None,
    ) -> List[PlacementFix]:
        """Suggest fixes for a list of conflicts.

        Args:
            conflicts: List of conflicts to fix
            analyzer: PlacementAnalyzer with component data (optional, for verification)

        Returns:
            List of suggested fixes
        """
        fixes: List[PlacementFix] = []

        for conflict in conflicts:
            fix = self._suggest_fix_for_conflict(conflict, analyzer)
            if fix:
                fixes.append(fix)

        # Sort fixes by confidence (highest first)
        fixes.sort(key=lambda f: -f.confidence)

        # Check for conflicting fixes (same component moved differently)
        fixes = self._deduplicate_fixes(fixes)

        return fixes

    def _suggest_fix_for_conflict(
        self,
        conflict: Conflict,
        analyzer: Optional[PlacementAnalyzer],
    ) -> Optional[PlacementFix]:
        """Suggest a fix for a single conflict."""
        # Determine which component to move
        component_to_move = self._choose_component_to_move(conflict)
        if not component_to_move:
            return None

        # Calculate move vector based on conflict type
        move_vector = self._calculate_move_vector(conflict, component_to_move)
        if not move_vector:
            return None

        # Calculate confidence based on conflict type and move
        confidence = self._calculate_confidence(conflict, move_vector)

        # Get current position if analyzer is available
        new_position = None
        if analyzer:
            for comp in analyzer.get_components():
                if comp.reference == component_to_move:
                    new_position = Point(
                        comp.position.x + move_vector.x,
                        comp.position.y + move_vector.y,
                    )
                    break

        return PlacementFix(
            conflict=conflict,
            component=component_to_move,
            move_vector=move_vector,
            confidence=confidence,
            new_position=new_position,
        )

    def _choose_component_to_move(self, conflict: Conflict) -> Optional[str]:
        """Choose which component to move to resolve conflict."""
        c1, c2 = conflict.component1, conflict.component2

        # Edge conflicts: always move the component
        if conflict.type == ConflictType.EDGE_CLEARANCE:
            return c1

        # If one is anchored, move the other
        if c1 in self.anchored and c2 not in self.anchored:
            return c2
        if c2 in self.anchored and c1 not in self.anchored:
            return c1
        if c1 in self.anchored and c2 in self.anchored:
            return None  # Can't fix - both anchored

        # By default, move the "smaller" component (alphabetically later)
        # This is a heuristic - ICs tend to have lower reference numbers
        return c2 if c2 > c1 else c1

    def _calculate_move_vector(
        self,
        conflict: Conflict,
        component_to_move: str,
    ) -> Optional[Point]:
        """Calculate the move vector to resolve a conflict."""
        if conflict.type == ConflictType.COURTYARD_OVERLAP:
            return self._calc_courtyard_fix(conflict, component_to_move)
        elif conflict.type == ConflictType.PAD_CLEARANCE:
            return self._calc_pad_clearance_fix(conflict, component_to_move)
        elif conflict.type == ConflictType.HOLE_TO_HOLE:
            return self._calc_hole_fix(conflict, component_to_move)
        elif conflict.type == ConflictType.EDGE_CLEARANCE:
            return self._calc_edge_fix(conflict)
        else:
            return None

    def _calc_courtyard_fix(
        self, conflict: Conflict, component_to_move: str
    ) -> Optional[Point]:
        """Calculate move to fix courtyard overlap."""
        if not conflict.overlap_amount:
            return None

        # Calculate direction from conflict location to component
        # and move in that direction by overlap amount + margin
        overlap = conflict.overlap_amount
        margin = 0.1  # Add small margin

        # Simple heuristic: move along the axis with less overlap
        # This assumes the conflict location is at overlap center
        # Prefer horizontal movement
        if self.strategy == FixStrategy.SPREAD:
            move = overlap + margin
            # Determine direction based on which component we're moving
            sign = 1 if component_to_move == conflict.component2 else -1
            return Point(sign * move, 0)

        return Point(overlap + margin, 0)

    def _calc_pad_clearance_fix(
        self, conflict: Conflict, component_to_move: str
    ) -> Optional[Point]:
        """Calculate move to fix pad clearance violation."""
        if conflict.actual_clearance is None or conflict.required_clearance is None:
            return None

        # Gap needed
        gap = conflict.required_clearance - conflict.actual_clearance
        margin = 0.05  # Small margin

        # Move away from the conflict location
        move = gap + margin

        # Determine direction - move c2 to the right, c1 to the left
        sign = 1 if component_to_move == conflict.component2 else -1

        return Point(sign * move, 0)

    def _calc_hole_fix(
        self, conflict: Conflict, component_to_move: str
    ) -> Optional[Point]:
        """Calculate move to fix hole-to-hole violation."""
        if conflict.actual_clearance is None or conflict.required_clearance is None:
            return None

        # Gap needed
        gap = conflict.required_clearance - conflict.actual_clearance
        margin = 0.1  # Margin for holes

        move = gap + margin
        sign = 1 if component_to_move == conflict.component2 else -1

        return Point(sign * move, 0)

    def _calc_edge_fix(self, conflict: Conflict) -> Optional[Point]:
        """Calculate move to fix edge clearance violation."""
        if conflict.actual_clearance is None or conflict.required_clearance is None:
            return None

        gap = conflict.required_clearance - conflict.actual_clearance
        margin = 0.1

        move = gap + margin

        # Determine direction based on which edge
        edge_type = conflict.component2  # e.g., "left_edge"

        if "left" in edge_type:
            return Point(move, 0)  # Move right
        elif "right" in edge_type:
            return Point(-move, 0)  # Move left
        elif "top" in edge_type:
            return Point(0, move)  # Move down
        elif "bottom" in edge_type:
            return Point(0, -move)  # Move up

        return None

    def _calculate_confidence(self, conflict: Conflict, move: Point) -> float:
        """Calculate confidence score for a fix (0-1)."""
        # Base confidence by conflict type
        base = {
            ConflictType.EDGE_CLEARANCE: 0.9,  # High confidence
            ConflictType.COURTYARD_OVERLAP: 0.7,
            ConflictType.PAD_CLEARANCE: 0.6,
            ConflictType.HOLE_TO_HOLE: 0.5,
            ConflictType.SILKSCREEN_PAD: 0.4,
        }.get(conflict.type, 0.3)

        # Reduce confidence for large moves
        move_dist = math.sqrt(move.x**2 + move.y**2)
        if move_dist > 5.0:
            base *= 0.5
        elif move_dist > 2.0:
            base *= 0.8

        return min(base, 1.0)

    def _deduplicate_fixes(self, fixes: List[PlacementFix]) -> List[PlacementFix]:
        """Remove duplicate/conflicting fixes for the same component."""
        # Group by component
        by_component: Dict[str, List[PlacementFix]] = {}
        for fix in fixes:
            if fix.component not in by_component:
                by_component[fix.component] = []
            by_component[fix.component].append(fix)

        # For each component, keep only the highest confidence fix
        # or combine moves if they're compatible
        result: List[PlacementFix] = []
        for component, comp_fixes in by_component.items():
            if len(comp_fixes) == 1:
                result.append(comp_fixes[0])
            else:
                # Combine compatible fixes or take the best one
                combined = self._combine_fixes(comp_fixes)
                result.append(combined)

        return result

    def _combine_fixes(self, fixes: List[PlacementFix]) -> PlacementFix:
        """Combine multiple fixes for the same component."""
        # Sum the move vectors
        total_x = sum(f.move_vector.x for f in fixes)
        total_y = sum(f.move_vector.y for f in fixes)

        # Use the first conflict as reference
        best = max(fixes, key=lambda f: f.confidence)

        return PlacementFix(
            conflict=best.conflict,
            component=best.component,
            move_vector=Point(total_x, total_y),
            confidence=min(f.confidence for f in fixes) * 0.9,  # Reduce for combined
            new_position=None,  # Need to recalculate
            creates_new_conflicts=True,  # Mark as potentially problematic
        )

    def apply_fixes(
        self,
        pcb_path: str | Path,
        fixes: List[PlacementFix],
        output_path: Optional[str | Path] = None,
        dry_run: bool = False,
    ) -> FixResult:
        """Apply fixes to a PCB file.

        Args:
            pcb_path: Path to input .kicad_pcb file
            fixes: List of fixes to apply
            output_path: Output path (None = modify in place)
            dry_run: If True, don't write changes

        Returns:
            FixResult with success status and details
        """
        if not fixes:
            return FixResult(
                success=True,
                fixes_applied=0,
                new_conflicts=0,
                message="No fixes to apply",
            )

        pcb_path = Path(pcb_path)
        if output_path is None:
            output_path = pcb_path

        # Read PCB content
        content = pcb_path.read_text()

        # Apply each fix
        applied = 0
        for fix in fixes:
            new_content = self._apply_fix_to_content(content, fix)
            if new_content != content:
                content = new_content
                applied += 1

        if dry_run:
            return FixResult(
                success=True,
                fixes_applied=applied,
                new_conflicts=0,
                message=f"Would apply {applied} fixes (dry run)",
            )

        # Write output
        Path(output_path).write_text(content)

        # Verify - check for new conflicts
        analyzer = PlacementAnalyzer()
        new_conflicts = analyzer.find_conflicts(output_path)

        return FixResult(
            success=len(new_conflicts) == 0,
            fixes_applied=applied,
            new_conflicts=len(new_conflicts),
            message=f"Applied {applied} fixes, {len(new_conflicts)} conflicts remaining",
        )

    def _apply_fix_to_content(self, content: str, fix: PlacementFix) -> str:
        """Apply a single fix to PCB content.

        Modifies the (at x y) position of the component's footprint.
        """
        import re

        # Find the footprint for this component
        # Pattern: (footprint "..." ... (property "Reference" "REF") ... (at X Y [R]))
        # This is a simplified approach - a proper implementation would parse S-expressions

        # Build pattern to find this specific footprint
        # Look for footprint containing this reference
        ref = fix.component

        # Find position of this component's footprint and update the (at x y) line
        # This regex approach is fragile but works for simple cases

        # Pattern to match the footprint block with this reference
        fp_pattern = rf'(\(footprint\s+[^\)]+\s+\(layer\s+"[^"]+"\)\s+[^\)]*\(at\s+)([\d.-]+)\s+([\d.-]+)(\s+[\d.-]+)?(\).*?property\s+"Reference"\s+"{re.escape(ref)}")'

        def update_position(match):
            prefix = match.group(1)
            old_x = float(match.group(2))
            old_y = float(match.group(3))
            rotation = match.group(4) or ""
            suffix = match.group(5)

            new_x = old_x + fix.move_vector.x
            new_y = old_y + fix.move_vector.y

            return f"{prefix}{new_x:.4f} {new_y:.4f}{rotation}){suffix}"

        new_content = re.sub(fp_pattern, update_position, content, flags=re.DOTALL)

        return new_content

    def verify_fixes(
        self,
        pcb_path: str | Path,
        fixes: List[PlacementFix],
        rules: Optional[DesignRules] = None,
    ) -> List[Conflict]:
        """Verify fixes won't create new conflicts.

        Applies fixes to a temporary copy and checks for new conflicts.

        Args:
            pcb_path: Path to .kicad_pcb file
            fixes: Fixes to verify
            rules: Design rules for checking

        Returns:
            List of conflicts that would be created by these fixes
        """
        import shutil
        import tempfile

        pcb_path = Path(pcb_path)

        # Create temp copy
        with tempfile.NamedTemporaryFile(
            suffix=".kicad_pcb", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
            shutil.copy(pcb_path, tmp_path)

        try:
            # Apply fixes
            self.apply_fixes(tmp_path, fixes, tmp_path)

            # Check for conflicts
            analyzer = PlacementAnalyzer()
            return analyzer.find_conflicts(tmp_path, rules)
        finally:
            # Clean up
            tmp_path.unlink()

    def preview_fixes(
        self, fixes: List[PlacementFix]
    ) -> str:
        """Generate human-readable preview of fixes.

        Returns:
            Multi-line string describing fixes
        """
        if not fixes:
            return "No fixes suggested"

        lines = ["Suggested fixes:", ""]

        for i, fix in enumerate(fixes, 1):
            lines.append(f"{i}. {fix}")
            if fix.new_position:
                lines.append(
                    f"   New position: ({fix.new_position.x:.3f}, {fix.new_position.y:.3f})"
                )
            if fix.creates_new_conflicts:
                lines.append("   Warning: May create new conflicts")
            lines.append("")

        return "\n".join(lines)
