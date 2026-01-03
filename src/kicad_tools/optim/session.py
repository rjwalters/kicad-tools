"""
Interactive placement refinement session.

Provides a stateful API for agents to explore placement changes through
query-before-commit semantics. Agents can evaluate hypothetical moves,
see the impact on routing and constraints, then decide whether to apply.

Example:
    >>> from kicad_tools.optim.session import PlacementSession
    >>> from kicad_tools.schema.pcb import PCB
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> session = PlacementSession(pcb)
    >>>
    >>> # Query impact of moving C1
    >>> result = session.query_move("C1", 45.0, 32.0)
    >>> print(f"Score delta: {result.score_delta}")
    >>>
    >>> if result.success:
    ...     session.apply_move("C1", 45.0, 32.0)
    >>>
    >>> # Commit changes to PCB
    >>> updated_pcb = session.commit()
    >>> updated_pcb.save("optimized.kicad_pcb")
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.constraints import ConstraintManager
from kicad_tools.optim.components import Component
from kicad_tools.optim.placement import PlacementOptimizer

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "PlacementSession",
    "MoveResult",
    "RoutingImpact",
    "PlacementSuggestion",
    "SessionState",
    "Move",
    "Violation",
]


@dataclass
class Violation:
    """A placement constraint violation."""

    type: str  # e.g., "clearance", "overlap", "boundary", "cluster_distance"
    description: str
    severity: str = "error"  # "error", "warning", "info"
    component: str = ""
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "description": self.description,
            "severity": self.severity,
            "component": self.component,
            "location": list(self.location) if self.location else None,
        }


@dataclass
class RoutingImpact:
    """Impact of a move on routing."""

    affected_nets: list[str] = field(default_factory=list)
    estimated_length_change_mm: float = 0.0  # Positive = longer
    new_congestion_areas: list[tuple[float, float]] = field(default_factory=list)
    crossing_changes: int = 0  # Net crossing count change

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "affected_nets": self.affected_nets,
            "estimated_length_change_mm": round(self.estimated_length_change_mm, 3),
            "new_congestion_areas": [list(c) for c in self.new_congestion_areas],
            "crossing_changes": self.crossing_changes,
        }


@dataclass
class MoveResult:
    """Result of evaluating or applying a move."""

    success: bool
    new_violations: list[Violation] = field(default_factory=list)
    resolved_violations: list[Violation] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)
    routing_impact: RoutingImpact = field(default_factory=RoutingImpact)
    score_delta: float = 0.0  # Change in overall placement score
    warnings: list[str] = field(default_factory=list)
    error_message: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "new_violations": [v.to_dict() for v in self.new_violations],
            "resolved_violations": [v.to_dict() for v in self.resolved_violations],
            "affected_components": self.affected_components,
            "routing_impact": self.routing_impact.to_dict(),
            "score_delta": round(self.score_delta, 4),
            "warnings": self.warnings,
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class PlacementSuggestion:
    """A suggested position for a component."""

    x: float
    y: float
    rotation: float = 0.0
    score: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "rotation": round(self.rotation, 1),
            "score": round(self.score, 4),
            "rationale": self.rationale,
        }


@dataclass
class Move:
    """A recorded move for undo/redo."""

    ref: str
    old_x: float
    old_y: float
    old_rotation: float
    new_x: float
    new_y: float
    new_rotation: float

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "ref": self.ref,
            "old_position": {"x": self.old_x, "y": self.old_y, "rotation": self.old_rotation},
            "new_position": {"x": self.new_x, "y": self.new_y, "rotation": self.new_rotation},
        }


@dataclass
class SessionState:
    """Snapshot of session state for history."""

    positions: dict[str, tuple[float, float, float]]  # ref -> (x, y, rotation)
    violations: list[Violation] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "positions": {
                ref: {"x": pos[0], "y": pos[1], "rotation": pos[2]}
                for ref, pos in self.positions.items()
            },
            "violations": [v.to_dict() for v in self.violations],
            "score": round(self.score, 4),
        }


class PlacementSession:
    """
    Interactive placement refinement session.

    Provides a stateful API for agents to explore placement changes:
    - Query moves before applying (what-if analysis)
    - Track pending changes with undo/redo
    - Commit or rollback changes atomically
    - Get suggestions for component placement

    Example:
        >>> session = PlacementSession(pcb)
        >>> result = session.query_move("C1", 45.0, 32.0)
        >>> if result.success:
        ...     session.apply_move("C1", 45.0, 32.0)
        >>> session.commit()
    """

    def __init__(
        self,
        pcb: PCB,
        constraints: ConstraintManager | None = None,
        fixed_refs: list[str] | None = None,
    ):
        """
        Initialize a placement session.

        Args:
            pcb: The PCB to work with
            constraints: Optional constraint manager for validation
            fixed_refs: Optional list of component references that shouldn't move
        """
        self.pcb = pcb
        self.constraints = constraints or ConstraintManager()
        self._fixed_refs = set(fixed_refs or [])

        # Create optimizer from PCB for physics/scoring
        self._optimizer = PlacementOptimizer.from_pcb(pcb, fixed_refs=list(self._fixed_refs))

        # Track pending moves (not yet committed to PCB)
        self.pending_moves: list[Move] = []

        # History for undo (states before each move)
        self.history: list[SessionState] = []

        # Store initial state
        self._initial_state = self._capture_state()
        self._initial_score = self._compute_score()

    def _capture_state(self) -> SessionState:
        """Capture current state of all components."""
        positions = {}
        for comp in self._optimizer.components:
            positions[comp.ref] = (comp.x, comp.y, comp.rotation)

        violations = self._check_violations()
        score = self._compute_score()

        return SessionState(positions=positions, violations=violations, score=score)

    def _compute_score(self) -> float:
        """Compute placement score (lower is better)."""
        # Use total wire length as primary metric
        wire_length = self._optimizer.total_wire_length()
        # Add energy as secondary (captures component spacing)
        energy = self._optimizer.compute_energy()
        # Normalize and combine
        return wire_length + energy * 0.1

    def _check_violations(self) -> list[Violation]:
        """Check for placement violations."""
        violations: list[Violation] = []

        # Check constraint violations
        constraint_violations = self.constraints.validate_placement(self._optimizer)
        for cv in constraint_violations:
            violations.append(
                Violation(
                    type=cv.constraint_type,
                    description=cv.message,
                    severity=cv.severity,
                    component=cv.constraint_name,
                    location=cv.location,
                )
            )

        # Check boundary violations
        for comp in self._optimizer.components:
            if not self._optimizer.board_outline.contains_point(comp.position()):
                violations.append(
                    Violation(
                        type="boundary",
                        description=f"{comp.ref} is outside board boundary",
                        severity="error",
                        component=comp.ref,
                        location=(comp.x, comp.y),
                    )
                )

        # Check for overlaps (simplified - could use more sophisticated collision detection)
        comps = self._optimizer.components
        for i, comp1 in enumerate(comps):
            for comp2 in comps[i + 1 :]:
                if self._components_overlap(comp1, comp2):
                    violations.append(
                        Violation(
                            type="overlap",
                            description=f"{comp1.ref} overlaps with {comp2.ref}",
                            severity="error",
                            component=comp1.ref,
                            location=(comp1.x, comp1.y),
                        )
                    )

        return violations

    def _components_overlap(self, comp1: Component, comp2: Component) -> bool:
        """Check if two components overlap (simplified AABB check)."""
        # Get axis-aligned bounding boxes
        half_w1, half_h1 = comp1.width / 2, comp1.height / 2
        half_w2, half_h2 = comp2.width / 2, comp2.height / 2

        # Account for rotation (simplified - uses max dimension)
        if comp1.rotation % 180 != 0:
            half_w1, half_h1 = half_h1, half_w1
        if comp2.rotation % 180 != 0:
            half_w2, half_h2 = half_h2, half_w2

        dx = abs(comp1.x - comp2.x)
        dy = abs(comp1.y - comp2.y)

        # Add small clearance
        clearance = 0.5  # mm

        return dx < (half_w1 + half_w2 + clearance) and dy < (half_h1 + half_h2 + clearance)

    def _get_affected_components(self, ref: str) -> list[str]:
        """Get components that share nets with the given component."""
        affected = set()
        comp = self._optimizer.get_component(ref)
        if not comp:
            return []

        # Get nets connected to this component
        comp_nets = {pin.net for pin in comp.pins if pin.net > 0}

        # Find other components on the same nets
        for other in self._optimizer.components:
            if other.ref == ref:
                continue
            for pin in other.pins:
                if pin.net in comp_nets:
                    affected.add(other.ref)
                    break

        return sorted(affected)

    def _get_affected_nets(self, ref: str) -> list[str]:
        """Get net names connected to a component."""
        comp = self._optimizer.get_component(ref)
        if not comp:
            return []

        nets = set()
        for pin in comp.pins:
            if pin.net_name:
                nets.add(pin.net_name)

        return sorted(nets)

    def _estimate_routing_impact(
        self,
        ref: str,
        old_x: float,
        old_y: float,
        new_x: float,
        new_y: float,
    ) -> RoutingImpact:
        """Estimate the routing impact of a move."""
        affected_nets = self._get_affected_nets(ref)

        # Estimate wire length change using Manhattan distance
        # For each spring connected to this component, estimate length change
        comp = self._optimizer.get_component(ref)
        if not comp:
            return RoutingImpact(affected_nets=affected_nets)

        length_change = 0.0
        for spring in self._optimizer.springs:
            if spring.comp1_ref != ref and spring.comp2_ref != ref:
                continue

            # Get the other component
            other_ref = spring.comp2_ref if spring.comp1_ref == ref else spring.comp1_ref
            other = self._optimizer.get_component(other_ref)
            if not other:
                continue

            # Calculate old and new distances
            old_dist = math.sqrt((old_x - other.x) ** 2 + (old_y - other.y) ** 2)
            new_dist = math.sqrt((new_x - other.x) ** 2 + (new_y - other.y) ** 2)
            length_change += new_dist - old_dist

        return RoutingImpact(
            affected_nets=affected_nets,
            estimated_length_change_mm=length_change,
        )

    def query_move(
        self,
        ref: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> MoveResult:
        """
        Evaluate hypothetical move without applying it.

        Args:
            ref: Component reference designator
            x: New X position in mm
            y: New Y position in mm
            rotation: New rotation in degrees (None = keep current)

        Returns:
            MoveResult with impact analysis
        """
        comp = self._optimizer.get_component(ref)
        if not comp:
            return MoveResult(
                success=False,
                error_message=f"Component '{ref}' not found",
            )

        if comp.fixed or ref in self._fixed_refs:
            return MoveResult(
                success=False,
                error_message=f"Component '{ref}' is fixed and cannot be moved",
            )

        # Store original state
        old_x, old_y, old_rot = comp.x, comp.y, comp.rotation
        new_rot = rotation if rotation is not None else old_rot

        # Temporarily apply the move
        comp.x, comp.y, comp.rotation = x, y, new_rot
        comp.update_pin_positions()

        # Check violations after move
        new_violations = self._check_violations()
        old_state = self._capture_state()
        old_violations = old_state.violations

        # Calculate which violations are new vs resolved
        old_violation_set = {(v.type, v.description) for v in old_violations}
        new_violation_set = {(v.type, v.description) for v in new_violations}

        truly_new = [v for v in new_violations if (v.type, v.description) not in old_violation_set]
        resolved = [v for v in old_violations if (v.type, v.description) not in new_violation_set]

        # Calculate score delta
        new_score = self._compute_score()
        score_delta = new_score - old_state.score

        # Estimate routing impact
        routing_impact = self._estimate_routing_impact(ref, old_x, old_y, x, y)

        # Get affected components
        affected = self._get_affected_components(ref)

        # Restore original state
        comp.x, comp.y, comp.rotation = old_x, old_y, old_rot
        comp.update_pin_positions()

        # Check for warnings
        warnings = []
        if routing_impact.estimated_length_change_mm > 5.0:
            warnings.append(
                f"Move increases routing length by {routing_impact.estimated_length_change_mm:.1f}mm"
            )
        if truly_new:
            warnings.append(f"Move creates {len(truly_new)} new violation(s)")

        return MoveResult(
            success=True,
            new_violations=truly_new,
            resolved_violations=resolved,
            affected_components=affected,
            routing_impact=routing_impact,
            score_delta=score_delta,
            warnings=warnings,
        )

    def apply_move(
        self,
        ref: str,
        x: float,
        y: float,
        rotation: float | None = None,
    ) -> MoveResult:
        """
        Apply move and add to pending changes.

        Args:
            ref: Component reference designator
            x: New X position in mm
            y: New Y position in mm
            rotation: New rotation in degrees (None = keep current)

        Returns:
            MoveResult with impact analysis
        """
        # First query to check validity
        result = self.query_move(ref, x, y, rotation)
        if not result.success:
            return result

        comp = self._optimizer.get_component(ref)
        if not comp:
            return MoveResult(success=False, error_message=f"Component '{ref}' not found")

        # Save state for undo
        self.history.append(self._capture_state())

        # Record the move
        old_x, old_y, old_rot = comp.x, comp.y, comp.rotation
        new_rot = rotation if rotation is not None else old_rot

        move = Move(
            ref=ref,
            old_x=old_x,
            old_y=old_y,
            old_rotation=old_rot,
            new_x=x,
            new_y=y,
            new_rotation=new_rot,
        )
        self.pending_moves.append(move)

        # Apply the move
        comp.x, comp.y, comp.rotation = x, y, new_rot
        comp.update_pin_positions()

        return result

    def undo(self) -> bool:
        """
        Undo last move.

        Returns:
            True if undo was performed, False if no history
        """
        if not self.history:
            return False

        # Restore previous state
        state = self.history.pop()
        for ref, (x, y, rot) in state.positions.items():
            comp = self._optimizer.get_component(ref)
            if comp:
                comp.x, comp.y, comp.rotation = x, y, rot
                comp.update_pin_positions()

        # Remove the last pending move
        if self.pending_moves:
            self.pending_moves.pop()

        return True

    def commit(self) -> PCB:
        """
        Apply all pending moves to PCB.

        Returns:
            Updated PCB object
        """
        # Write optimizer positions to PCB
        self._optimizer.write_to_pcb(self.pcb)

        # Clear pending state
        self.pending_moves.clear()
        self.history.clear()
        self._initial_state = self._capture_state()
        self._initial_score = self._compute_score()

        return self.pcb

    def rollback(self) -> None:
        """Discard all pending moves and restore initial state."""
        # Restore initial positions
        for ref, (x, y, rot) in self._initial_state.positions.items():
            comp = self._optimizer.get_component(ref)
            if comp:
                comp.x, comp.y, comp.rotation = x, y, rot
                comp.update_pin_positions()

        # Clear pending state
        self.pending_moves.clear()
        self.history.clear()

    def get_suggestions(
        self,
        ref: str,
        num_suggestions: int = 5,
        search_radius: float = 20.0,
    ) -> list[PlacementSuggestion]:
        """
        Get suggested positions for component.

        Uses a grid search within a radius to find positions that
        improve the placement score.

        Args:
            ref: Component reference designator
            num_suggestions: Maximum number of suggestions to return
            search_radius: Radius in mm to search around current position

        Returns:
            List of placement suggestions, sorted by score (best first)
        """
        comp = self._optimizer.get_component(ref)
        if not comp:
            return []

        suggestions = []
        grid_step = 2.5  # mm
        current_score = self._compute_score()

        # Save current position
        orig_x, orig_y, orig_rot = comp.x, comp.y, comp.rotation

        # Grid search
        for dx in range(-int(search_radius), int(search_radius) + 1, int(grid_step)):
            for dy in range(-int(search_radius), int(search_radius) + 1, int(grid_step)):
                if dx == 0 and dy == 0:
                    continue

                dist = math.sqrt(dx * dx + dy * dy)
                if dist > search_radius:
                    continue

                new_x = orig_x + dx
                new_y = orig_y + dy

                # Try position
                comp.x, comp.y = new_x, new_y
                comp.update_pin_positions()

                # Check if valid (inside boundary, no severe overlaps)
                if not self._optimizer.board_outline.contains_point(comp.position()):
                    comp.x, comp.y = orig_x, orig_y
                    comp.update_pin_positions()
                    continue

                # Compute score
                score = self._compute_score()
                score_delta = score - current_score

                # Only suggest improvements
                if score_delta < 0:
                    suggestions.append(
                        PlacementSuggestion(
                            x=new_x,
                            y=new_y,
                            rotation=orig_rot,
                            score=-score_delta,  # Positive = improvement
                            rationale=f"Reduces score by {-score_delta:.3f}",
                        )
                    )

        # Restore position
        comp.x, comp.y, comp.rotation = orig_x, orig_y, orig_rot
        comp.update_pin_positions()

        # Sort by score (best improvements first) and limit
        suggestions.sort(key=lambda s: s.score, reverse=True)
        return suggestions[:num_suggestions]

    def get_status(self) -> dict:
        """Get current session status."""
        current_state = self._capture_state()
        return {
            "pending_moves": len(self.pending_moves),
            "history_depth": len(self.history),
            "current_score": round(current_state.score, 4),
            "initial_score": round(self._initial_score, 4),
            "score_change": round(current_state.score - self._initial_score, 4),
            "violations": len(current_state.violations),
            "components": len(self._optimizer.components),
        }

    def get_component_position(self, ref: str) -> dict | None:
        """Get current position of a component."""
        comp = self._optimizer.get_component(ref)
        if not comp:
            return None
        return {
            "ref": ref,
            "x": round(comp.x, 3),
            "y": round(comp.y, 3),
            "rotation": round(comp.rotation, 1),
            "fixed": comp.fixed or ref in self._fixed_refs,
        }

    def list_components(self) -> list[dict]:
        """List all components with their current positions."""
        return [
            {
                "ref": comp.ref,
                "x": round(comp.x, 3),
                "y": round(comp.y, 3),
                "rotation": round(comp.rotation, 1),
                "fixed": comp.fixed or comp.ref in self._fixed_refs,
                "width": round(comp.width, 3),
                "height": round(comp.height, 3),
            }
            for comp in sorted(self._optimizer.components, key=lambda c: c.ref)
        ]
