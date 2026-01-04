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
        anchored: set[str] | None = None,
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
        conflicts: list[Conflict],
        analyzer: PlacementAnalyzer | None = None,
    ) -> list[PlacementFix]:
        """Suggest fixes for a list of conflicts.

        Args:
            conflicts: List of conflicts to fix
            analyzer: PlacementAnalyzer with component data (optional, for verification)

        Returns:
            List of suggested fixes
        """
        fixes: list[PlacementFix] = []

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
        analyzer: PlacementAnalyzer | None,
    ) -> PlacementFix | None:
        """Suggest a fix for a single conflict."""
        # Determine which component to move
        component_to_move = self._choose_component_to_move(conflict)
        if not component_to_move:
            return None

        # Calculate move vector based on conflict type
        move_vector = self._calculate_move_vector(conflict, component_to_move, analyzer)
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

    def _choose_component_to_move(self, conflict: Conflict) -> str | None:
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
        analyzer: PlacementAnalyzer | None = None,
    ) -> Point | None:
        """Calculate the move vector to resolve a conflict."""
        if conflict.type == ConflictType.COURTYARD_OVERLAP:
            return self._calc_courtyard_fix(conflict, component_to_move, analyzer)
        elif conflict.type == ConflictType.PAD_CLEARANCE:
            return self._calc_pad_clearance_fix(conflict, component_to_move, analyzer)
        elif conflict.type == ConflictType.HOLE_TO_HOLE:
            return self._calc_hole_fix(conflict, component_to_move, analyzer)
        elif conflict.type == ConflictType.EDGE_CLEARANCE:
            return self._calc_edge_fix(conflict)
        else:
            return None

    def _get_component_position(
        self,
        reference: str,
        analyzer: PlacementAnalyzer | None,
    ) -> Point | None:
        """Get the position of a component by reference.

        Args:
            reference: Component reference designator (e.g., "R1")
            analyzer: PlacementAnalyzer with component data

        Returns:
            Component center position, or None if not found
        """
        if not analyzer:
            return None
        for comp in analyzer.get_components():
            if comp.reference == reference:
                return comp.position
        return None

    def _get_direction_vector(
        self,
        from_pos: Point,
        to_pos: Point,
    ) -> tuple[float, float] | None:
        """Calculate unit direction vector from one point to another.

        Args:
            from_pos: Starting point
            to_pos: Target point

        Returns:
            Tuple of (dx, dy) normalized to unit length, or None if points coincide
        """
        dx = to_pos.x - from_pos.x
        dy = to_pos.y - from_pos.y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist < 1e-6:
            return None

        return (dx / dist, dy / dist)

    def _calc_courtyard_fix(
        self,
        conflict: Conflict,
        component_to_move: str,
        analyzer: PlacementAnalyzer | None = None,
    ) -> Point | None:
        """Calculate move to fix courtyard overlap.

        Uses 2D displacement when component positions are available,
        calculating the optimal direction based on component centroids.
        Falls back to X-only movement when geometry data is unavailable.
        """
        if not conflict.overlap_amount:
            return None

        overlap = conflict.overlap_amount
        margin = 0.1  # Add small margin
        move_dist = overlap + margin

        # Determine direction sign based on which component we're moving
        sign = 1 if component_to_move == conflict.component2 else -1

        # Try to calculate 2D direction using component positions
        if analyzer:
            pos1 = self._get_component_position(conflict.component1, analyzer)
            pos2 = self._get_component_position(conflict.component2, analyzer)

            if pos1 and pos2:
                # Calculate direction from comp1 to comp2
                direction = self._get_direction_vector(pos1, pos2)
                if direction:
                    dx, dy = direction
                    # Move along the line connecting centroids
                    return Point(sign * move_dist * dx, sign * move_dist * dy)

        # Fallback: X-only movement (backward compatible)
        if self.strategy == FixStrategy.SPREAD:
            return Point(sign * move_dist, 0)

        return Point(move_dist, 0)

    def _calc_pad_clearance_fix(
        self,
        conflict: Conflict,
        component_to_move: str,
        analyzer: PlacementAnalyzer | None = None,
    ) -> Point | None:
        """Calculate move to fix pad clearance violation.

        Uses 2D displacement when component positions are available,
        moving away from the conflict location along the vector to component center.
        Falls back to X-only movement when geometry data is unavailable.
        """
        if conflict.actual_clearance is None or conflict.required_clearance is None:
            return None

        # Gap needed
        gap = conflict.required_clearance - conflict.actual_clearance
        margin = 0.05  # Small margin
        move_dist = gap + margin

        # Determine direction sign based on which component we're moving
        sign = 1 if component_to_move == conflict.component2 else -1

        # Try to calculate 2D direction from conflict location to component center
        if analyzer:
            comp_pos = self._get_component_position(component_to_move, analyzer)
            if comp_pos:
                # Calculate direction from conflict location to component
                direction = self._get_direction_vector(conflict.location, comp_pos)
                if direction:
                    dx, dy = direction
                    # Move component away from conflict (along the direction to its center)
                    return Point(move_dist * dx, move_dist * dy)

        # Fallback: X-only movement (backward compatible)
        return Point(sign * move_dist, 0)

    def _calc_hole_fix(
        self,
        conflict: Conflict,
        component_to_move: str,
        analyzer: PlacementAnalyzer | None = None,
    ) -> Point | None:
        """Calculate move to fix hole-to-hole violation.

        Uses 2D displacement when component positions are available,
        moving away from the conflict location along the vector to component center.
        Falls back to X-only movement when geometry data is unavailable.
        """
        if conflict.actual_clearance is None or conflict.required_clearance is None:
            return None

        # Gap needed
        gap = conflict.required_clearance - conflict.actual_clearance
        margin = 0.1  # Margin for holes
        move_dist = gap + margin

        # Determine direction sign based on which component we're moving
        sign = 1 if component_to_move == conflict.component2 else -1

        # Try to calculate 2D direction from conflict location to component center
        if analyzer:
            comp_pos = self._get_component_position(component_to_move, analyzer)
            if comp_pos:
                # Calculate direction from conflict location to component
                direction = self._get_direction_vector(conflict.location, comp_pos)
                if direction:
                    dx, dy = direction
                    # Move component away from conflict (along the direction to its center)
                    return Point(move_dist * dx, move_dist * dy)

        # Fallback: X-only movement (backward compatible)
        return Point(sign * move_dist, 0)

    def _calc_edge_fix(self, conflict: Conflict) -> Point | None:
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

    def _deduplicate_fixes(self, fixes: list[PlacementFix]) -> list[PlacementFix]:
        """Remove duplicate/conflicting fixes for the same component."""
        # Group by component
        by_component: dict[str, list[PlacementFix]] = {}
        for fix in fixes:
            if fix.component not in by_component:
                by_component[fix.component] = []
            by_component[fix.component].append(fix)

        # For each component, keep only the highest confidence fix
        # or combine moves if they're compatible
        result: list[PlacementFix] = []
        for _component, comp_fixes in by_component.items():
            if len(comp_fixes) == 1:
                result.append(comp_fixes[0])
            else:
                # Combine compatible fixes or take the best one
                combined = self._combine_fixes(comp_fixes)
                result.append(combined)

        return result

    def _combine_fixes(self, fixes: list[PlacementFix]) -> PlacementFix:
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
        fixes: list[PlacementFix],
        output_path: str | Path | None = None,
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

        ref = fix.component

        # Pattern to match the footprint block with this reference.
        # Key fixes:
        # 1. Use "[^"]+" for quoted footprint name (not [^\)]+)
        # 2. Use [\s\S]*? for lazy match across nested S-expressions (not [^\)]*)
        # 3. The suffix includes the closing ), so don't add another in replacement
        #
        # Structure: (footprint "name" (layer "...") ... (at X Y [R]) ... property "Reference" "REF" ...)
        fp_pattern = rf'(\(footprint\s+"[^"]+"\s+\(layer\s+"[^"]+"\)[\s\S]*?\(at\s+)([\d.-]+)\s+([\d.-]+)(\s+[\d.-]+)?(\)[\s\S]*?property\s+"Reference"\s+"{re.escape(ref)}")'

        def update_position(match):
            prefix = match.group(1)
            old_x = float(match.group(2))
            old_y = float(match.group(3))
            rotation = match.group(4) or ""
            suffix = match.group(5)  # Already includes closing )

            new_x = old_x + fix.move_vector.x
            new_y = old_y + fix.move_vector.y

            # Note: suffix starts with ) so don't add another
            return f"{prefix}{new_x:.4f} {new_y:.4f}{rotation}{suffix}"

        new_content = re.sub(fp_pattern, update_position, content, flags=re.DOTALL)

        return new_content

    def verify_fixes(
        self,
        pcb_path: str | Path,
        fixes: list[PlacementFix],
        rules: DesignRules | None = None,
    ) -> list[Conflict]:
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
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as tmp:
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

    def preview_fixes(self, fixes: list[PlacementFix]) -> str:
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
