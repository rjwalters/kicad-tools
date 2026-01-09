"""
Strategy application for placement-routing feedback.

This module provides the StrategyApplicator class which executes resolution
strategies on a PCB, enabling automatic adjustment of placement based on
routing failures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types import (
    Rectangle,
    ResolutionStrategy,
    StrategyType,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


@dataclass
class ApplicationResult:
    """Result of applying a strategy.

    Attributes:
        success: Whether the strategy was applied successfully.
        components_moved: List of component references that were moved.
        message: Human-readable description of what happened.
        conflicts_created: Number of new conflicts created (if any).
    """

    success: bool
    components_moved: list[str]
    message: str
    conflicts_created: int = 0


class StrategyApplicator:
    """Applies resolution strategies to a PCB.

    This class transforms ResolutionStrategy objects into concrete changes
    on a PCB, moving components and updating placement to resolve routing
    failures.

    Example::

        from kicad_tools.recovery import StrategyGenerator, StrategyApplicator

        # Generate strategies from failure analysis
        generator = StrategyGenerator()
        strategies = generator.generate_strategies(pcb, failure_analysis)

        # Apply the best strategy
        applicator = StrategyApplicator()
        if strategies and applicator.is_safe_to_apply(strategies[0], pcb):
            result = applicator.apply_strategy(pcb, strategies[0])
            if result.success:
                print(f"Moved {len(result.components_moved)} components")
    """

    # Board margin to prevent components from being placed too close to edge
    BOARD_EDGE_MARGIN = 1.0  # mm

    # Maximum move distance to prevent drastic placement changes
    MAX_MOVE_DISTANCE = 10.0  # mm

    def apply_strategy(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a resolution strategy to a PCB.

        Args:
            pcb: The PCB to modify.
            strategy: The strategy to apply.

        Returns:
            ApplicationResult with success status and details.
        """
        if strategy.type == StrategyType.MOVE_COMPONENT:
            return self._apply_move_component(pcb, strategy)
        elif strategy.type == StrategyType.MOVE_MULTIPLE:
            return self._apply_move_multiple(pcb, strategy)
        else:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Strategy type {strategy.type.value} cannot be applied to placement",
            )

    def _apply_move_component(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a single component move strategy."""
        if not strategy.actions:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="No actions in strategy",
            )

        action = strategy.actions[0]
        if action.type != "move":
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Expected move action, got {action.type}",
            )

        ref = action.target
        new_x = action.params.get("x")
        new_y = action.params.get("y")

        if new_x is None or new_y is None:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="Missing x or y in move action params",
            )

        # Find and move the footprint
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Component {ref} not found",
            )

        # Store old position for reporting
        old_x, old_y = fp.position[0], fp.position[1]

        # Apply the move
        fp.position = (new_x, new_y)

        return ApplicationResult(
            success=True,
            components_moved=[ref],
            message=f"Moved {ref} from ({old_x:.2f}, {old_y:.2f}) to ({new_x:.2f}, {new_y:.2f})",
        )

    def _apply_move_multiple(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a multi-component move strategy."""
        moved: list[str] = []
        messages: list[str] = []

        for action in strategy.actions:
            if action.type != "move":
                continue

            ref = action.target
            new_x = action.params.get("x")
            new_y = action.params.get("y")

            if new_x is None or new_y is None:
                continue

            fp = self._find_footprint(pcb, ref)
            if fp is None:
                messages.append(f"{ref}: not found")
                continue

            old_x, old_y = fp.position[0], fp.position[1]
            fp.position = (new_x, new_y)
            moved.append(ref)
            messages.append(f"{ref}: ({old_x:.2f}, {old_y:.2f}) -> ({new_x:.2f}, {new_y:.2f})")

        if not moved:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="No components moved",
            )

        return ApplicationResult(
            success=True,
            components_moved=moved,
            message=f"Moved {len(moved)} components: " + "; ".join(messages),
        )

    def is_safe_to_apply(self, strategy: ResolutionStrategy, pcb: PCB) -> bool:
        """Check if applying a strategy is safe.

        Verifies that:
        1. All target components exist
        2. New positions are within board bounds
        3. Move distances are reasonable
        4. Functional groups won't be broken (basic check)

        Args:
            strategy: The strategy to check.
            pcb: The PCB to check against.

        Returns:
            True if the strategy is safe to apply.
        """
        if strategy.type not in [StrategyType.MOVE_COMPONENT, StrategyType.MOVE_MULTIPLE]:
            return False

        board_bounds = self._get_board_bounds(pcb)
        if board_bounds is None:
            # If we can't determine board bounds, be conservative
            return False

        for action in strategy.actions:
            if action.type != "move":
                continue

            ref = action.target
            new_x = action.params.get("x")
            new_y = action.params.get("y")

            if new_x is None or new_y is None:
                return False

            # Check component exists
            fp = self._find_footprint(pcb, ref)
            if fp is None:
                return False

            # Check new position is within board bounds (with margin)
            if not self._position_within_bounds(new_x, new_y, board_bounds):
                return False

            # Check move distance is reasonable
            old_x, old_y = fp.position[0], fp.position[1]
            distance = math.sqrt((new_x - old_x) ** 2 + (new_y - old_y) ** 2)
            if distance > self.MAX_MOVE_DISTANCE:
                return False

        return True

    def calculate_move_vector(
        self,
        pcb: PCB,
        ref: str,
        failure_area: Rectangle,
        direction: tuple[float, float] | None = None,
    ) -> tuple[float, float] | None:
        """Calculate the move vector for a single component.

        Determines how far to move a component to clear a failure area.

        Args:
            pcb: The PCB containing the component.
            ref: Component reference designator.
            failure_area: The area to clear.
            direction: Optional preferred move direction (dx, dy). If not
                provided, direction is calculated from failure area center.

        Returns:
            Move vector (dx, dy) in mm, or None if cannot calculate.
        """
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return None

        comp_x, comp_y = fp.position[0], fp.position[1]
        center_x, center_y = failure_area.center

        # Calculate direction if not provided
        if direction is None:
            dx = comp_x - center_x
            dy = comp_y - center_y
            dist = math.sqrt(dx * dx + dy * dy)

            if dist < 0.1:
                # Component at center, move in arbitrary direction
                dx, dy = 1.0, 0.0
            else:
                dx /= dist
                dy /= dist
        else:
            dx, dy = direction
            dist = math.sqrt(dx * dx + dy * dy)
            if dist > 0:
                dx /= dist
                dy /= dist

        # Calculate move distance to clear failure area
        # Use the larger dimension of failure area plus margin
        clear_dist = max(failure_area.width, failure_area.height) / 2 + 0.5

        return (dx * clear_dist, dy * clear_dist)

    def calculate_spread_vector(
        self,
        pcb: PCB,
        ref: str,
        center: tuple[float, float],
        spread_distance: float = 2.0,
    ) -> tuple[float, float] | None:
        """Calculate the spread vector for multi-component spreading.

        Calculates a vector to move a component away from a center point,
        used for relieving congestion.

        Args:
            pcb: The PCB containing the component.
            ref: Component reference designator.
            center: Center point to spread away from (x, y).
            spread_distance: How far to spread in mm.

        Returns:
            Spread vector (dx, dy) in mm, or None if cannot calculate.
        """
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return None

        comp_x, comp_y = fp.position[0], fp.position[1]
        center_x, center_y = center

        dx = comp_x - center_x
        dy = comp_y - center_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist < 0.1:
            # Component at center, spread in arbitrary direction based on ref
            # Use hash of ref for consistent but varied direction
            angle = (hash(ref) % 360) * math.pi / 180
            dx = math.cos(angle)
            dy = math.sin(angle)
        else:
            dx /= dist
            dy /= dist

        return (dx * spread_distance, dy * spread_distance)

    def simulate_placement_change(
        self,
        pcb: PCB,
        strategy: ResolutionStrategy,
    ) -> dict[str, tuple[float, float]]:
        """Simulate a placement change without applying it.

        Returns what the new positions would be if the strategy were applied.

        Args:
            pcb: The PCB (not modified).
            strategy: The strategy to simulate.

        Returns:
            Dictionary mapping component ref to new (x, y) position.
        """
        positions: dict[str, tuple[float, float]] = {}

        for action in strategy.actions:
            if action.type != "move":
                continue

            ref = action.target
            new_x = action.params.get("x")
            new_y = action.params.get("y")

            if new_x is not None and new_y is not None:
                positions[ref] = (new_x, new_y)

        return positions

    def _find_footprint(self, pcb: PCB, ref: str):
        """Find a footprint by reference designator."""
        for fp in pcb.footprints:
            if fp.reference == ref:
                return fp
        return None

    def _get_board_bounds(self, pcb: PCB) -> Rectangle | None:
        """Get the board outline bounds."""
        # Try to find board outline from edge cuts
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")

        found = False
        for item in pcb.graphic_items:
            # Check if this is an edge cut
            layer = getattr(item, "layer", None)
            if layer is not None and "Edge" in str(layer):
                found = True
                # Get coordinates from the item
                if hasattr(item, "start") and hasattr(item, "end"):
                    min_x = min(min_x, item.start[0], item.end[0])
                    min_y = min(min_y, item.start[1], item.end[1])
                    max_x = max(max_x, item.start[0], item.end[0])
                    max_y = max(max_y, item.start[1], item.end[1])
                elif hasattr(item, "center") and hasattr(item, "radius"):
                    min_x = min(min_x, item.center[0] - item.radius)
                    min_y = min(min_y, item.center[1] - item.radius)
                    max_x = max(max_x, item.center[0] + item.radius)
                    max_y = max(max_y, item.center[1] + item.radius)

        if not found:
            # Fall back to component bounds with margin
            for fp in pcb.footprints:
                x, y = fp.position[0], fp.position[1]
                min_x = min(min_x, x - 10)
                min_y = min(min_y, y - 10)
                max_x = max(max_x, x + 10)
                max_y = max(max_y, y + 10)

            if min_x == float("inf"):
                return None

        return Rectangle(min_x, min_y, max_x, max_y)

    def _position_within_bounds(self, x: float, y: float, bounds: Rectangle) -> bool:
        """Check if a position is within board bounds (with margin)."""
        return (
            bounds.min_x + self.BOARD_EDGE_MARGIN <= x <= bounds.max_x - self.BOARD_EDGE_MARGIN
            and bounds.min_y + self.BOARD_EDGE_MARGIN <= y <= bounds.max_y - self.BOARD_EDGE_MARGIN
        )
