"""DRC fix suggestions with actionable remediation guidance.

Provides specific, actionable fix suggestions for DRC violations.
Each suggestion includes the action to take, target element,
parameters (like move distance), and human-readable description.

Example:
    >>> from kicad_tools.drc import DRCReport
    >>> from kicad_tools.drc.suggestions import generate_suggestions
    >>> report = DRCReport.load("board-drc.rpt")
    >>> suggestions = generate_suggestions(report)
    >>> for v, sug in suggestions.items():
    ...     print(f"{v}: {sug.description}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .violation import DRCViolation, ViolationType

__all__ = [
    "FixAction",
    "FixSuggestion",
    "generate_fix_suggestions",
    "calculate_clearance_fix",
    "direction_name",
]


class FixAction(Enum):
    """Types of fix actions that can be suggested."""

    MOVE = "move"  # Move element to new position
    REROUTE = "reroute"  # Reroute a net/trace
    RESIZE = "resize"  # Change size (width, diameter, etc.)
    DELETE = "delete"  # Remove the element
    CONNECT = "connect"  # Add connection between elements
    ADJUST_RULE = "adjust_rule"  # Modify design rule


@dataclass
class FixSuggestion:
    """Specific fix suggestion for a DRC violation.

    Attributes:
        action: The type of fix action (move, reroute, resize, etc.)
        target: The element to modify (reference designator, net name, etc.)
        parameters: Action-specific parameters (dx, dy, new_width, etc.)
        description: Human-readable description of the fix
        priority: Suggestion priority (1=highest, lower is better)
        complexity: Estimated fix complexity ("trivial", "easy", "moderate", "complex")
        alternatives: Alternative fix suggestions if primary doesn't work
    """

    action: FixAction
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    priority: int = 1
    complexity: str = "easy"
    alternatives: list[FixSuggestion] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "action": self.action.value,
            "target": self.target,
            "parameters": self.parameters,
            "description": self.description,
            "priority": self.priority,
            "complexity": self.complexity,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
        }

    def __str__(self) -> str:
        return self.description


def direction_name(dx: float, dy: float) -> str:
    """Get human-readable direction name from delta vector.

    Args:
        dx: X displacement (positive = right)
        dy: Y displacement (positive = down in KiCad coordinates)

    Returns:
        Direction name like "left", "up-right", etc.
    """
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return "in place"

    # Determine primary direction
    # Note: KiCad uses top-left origin, so positive Y is down
    directions = []

    if dy < -0.01:
        directions.append("up")
    elif dy > 0.01:
        directions.append("down")

    if dx < -0.01:
        directions.append("left")
    elif dx > 0.01:
        directions.append("right")

    return "-".join(directions) if directions else "in place"


def calculate_clearance_fix(
    violation: DRCViolation,
    margin: float = 0.1,
) -> FixSuggestion | None:
    """Calculate fix suggestion for a clearance violation.

    Args:
        violation: The clearance violation to fix
        margin: Extra margin to add beyond minimum clearance (mm)

    Returns:
        FixSuggestion with move parameters, or None if cannot calculate
    """
    if not violation.is_clearance:
        return None

    required = violation.required_value_mm
    actual = violation.actual_value_mm

    if required is None or actual is None:
        # Can't calculate without values
        return FixSuggestion(
            action=FixAction.REROUTE,
            target="affected traces",
            parameters={},
            description="Reroute traces to increase clearance",
            priority=2,
            complexity="moderate",
        )

    # Calculate how much clearance is needed
    delta = required - actual + margin

    # Get affected items from violation
    items = violation.items
    if len(items) >= 2:
        element1 = items[0]
        element2 = items[1]
    elif len(items) == 1:
        element1 = items[0]
        element2 = "adjacent element"
    else:
        element1 = "element"
        element2 = "adjacent element"

    # Try to determine move direction from locations
    locations = violation.locations
    if len(locations) >= 2:
        loc1 = locations[0]
        loc2 = locations[1]

        # Calculate push direction (away from element2)
        dx = loc1.x_mm - loc2.x_mm
        dy = loc1.y_mm - loc2.y_mm
        distance = math.sqrt(dx * dx + dy * dy)

        if distance > 0.001:
            # Normalize and scale by delta
            dx = (dx / distance) * delta
            dy = (dy / distance) * delta
        else:
            # Default to moving right if no direction
            dx = delta
            dy = 0.0

        direction = direction_name(dx, dy)

        return FixSuggestion(
            action=FixAction.MOVE,
            target=element1,
            parameters={
                "dx": round(dx, 3),
                "dy": round(dy, 3),
                "distance_mm": round(delta, 3),
            },
            description=f"Move {element1} {delta:.2f}mm {direction}",
            priority=1,
            complexity="easy",
            alternatives=[
                FixSuggestion(
                    action=FixAction.MOVE,
                    target=element2,
                    parameters={
                        "dx": round(-dx, 3),
                        "dy": round(-dy, 3),
                        "distance_mm": round(delta, 3),
                    },
                    description=f"Move {element2} {delta:.2f}mm {direction_name(-dx, -dy)}",
                    priority=2,
                    complexity="easy",
                ),
                FixSuggestion(
                    action=FixAction.REROUTE,
                    target=violation.nets[0] if violation.nets else "affected net",
                    parameters={},
                    description=f"Reroute net to avoid {element2}",
                    priority=3,
                    complexity="moderate",
                ),
            ],
        )

    # Fallback without location info
    return FixSuggestion(
        action=FixAction.MOVE,
        target=element1,
        parameters={"distance_mm": round(delta, 3)},
        description=f"Increase clearance by {delta:.2f}mm (move {element1} away from {element2})",
        priority=1,
        complexity="easy",
        alternatives=[
            FixSuggestion(
                action=FixAction.REROUTE,
                target=violation.nets[0] if violation.nets else "affected net",
                parameters={},
                description="Reroute traces to increase clearance",
                priority=2,
                complexity="moderate",
            ),
        ],
    )


def _calculate_track_width_fix(violation: DRCViolation) -> FixSuggestion | None:
    """Calculate fix for track width violation."""
    required = violation.required_value_mm
    actual = violation.actual_value_mm

    if required is None:
        return FixSuggestion(
            action=FixAction.RESIZE,
            target="track",
            parameters={},
            description="Increase track width to meet minimum requirement",
            priority=1,
            complexity="easy",
        )

    delta = required - (actual or 0)
    new_width = required

    # Extract net name if available
    net = violation.nets[0] if violation.nets else "affected net"

    return FixSuggestion(
        action=FixAction.RESIZE,
        target=net,
        parameters={
            "new_width_mm": round(new_width, 3),
            "increase_mm": round(delta, 3) if delta > 0 else 0,
        },
        description=f"Increase track width to {new_width:.3f}mm (+{delta:.3f}mm)",
        priority=1,
        complexity="easy",
    )


def _calculate_via_fix(violation: DRCViolation) -> FixSuggestion | None:
    """Calculate fix for via-related violations."""
    required = violation.required_value_mm
    actual = violation.actual_value_mm

    if violation.type == ViolationType.VIA_ANNULAR_WIDTH:
        # Need to increase via pad size or decrease drill
        if required is not None:
            delta = required - (actual or 0)
            return FixSuggestion(
                action=FixAction.RESIZE,
                target="via",
                parameters={
                    "increase_pad_mm": round(delta * 2, 3),  # Diameter increase
                },
                description=f"Increase via pad diameter by {delta * 2:.3f}mm",
                priority=1,
                complexity="easy",
                alternatives=[
                    FixSuggestion(
                        action=FixAction.RESIZE,
                        target="via",
                        parameters={"decrease_drill_mm": round(delta * 2, 3)},
                        description=f"Decrease via drill by {delta * 2:.3f}mm",
                        priority=2,
                        complexity="easy",
                    ),
                ],
            )

    if violation.type == ViolationType.VIA_HOLE_LARGER_THAN_PAD:
        return FixSuggestion(
            action=FixAction.RESIZE,
            target="via",
            parameters={},
            description="Increase via pad size or decrease drill size",
            priority=1,
            complexity="easy",
        )

    if violation.type in (
        ViolationType.MICRO_VIA_HOLE_TOO_SMALL,
        ViolationType.DRILL_HOLE_TOO_SMALL,
    ):
        if required is not None:
            return FixSuggestion(
                action=FixAction.RESIZE,
                target="via/hole",
                parameters={"min_drill_mm": round(required, 3)},
                description=f"Increase drill size to at least {required:.3f}mm",
                priority=1,
                complexity="easy",
            )

    return None


def _calculate_connection_fix(violation: DRCViolation) -> FixSuggestion | None:
    """Calculate fix for connection-related violations."""
    if violation.type == ViolationType.UNCONNECTED_ITEMS:
        net = violation.nets[0] if violation.nets else "unconnected net"
        items = violation.items

        if len(items) >= 2:
            description = f"Route connection between {items[0]} and {items[1]} on net {net}"
        elif items:
            description = f"Complete routing for {items[0]} on net {net}"
        else:
            description = f"Complete routing for net {net}"

        return FixSuggestion(
            action=FixAction.CONNECT,
            target=net,
            parameters={"items": items} if items else {},
            description=description,
            priority=1,
            complexity="moderate",
        )

    if violation.type == ViolationType.SHORTING_ITEMS:
        nets = violation.nets
        if len(nets) >= 2:
            description = f"Remove short between nets {nets[0]} and {nets[1]}"
        else:
            description = "Remove shorting trace/via"

        return FixSuggestion(
            action=FixAction.DELETE,
            target="shorting element",
            parameters={"nets": nets} if nets else {},
            description=description,
            priority=1,
            complexity="easy",
            alternatives=[
                FixSuggestion(
                    action=FixAction.REROUTE,
                    target=nets[0] if nets else "affected net",
                    parameters={},
                    description="Reroute to avoid short",
                    priority=2,
                    complexity="moderate",
                ),
            ],
        )

    return None


def _calculate_silkscreen_fix(violation: DRCViolation) -> FixSuggestion | None:
    """Calculate fix for silkscreen violations."""
    if violation.type == ViolationType.SILK_OVER_COPPER:
        return FixSuggestion(
            action=FixAction.MOVE,
            target="silkscreen element",
            parameters={},
            description="Move silkscreen away from copper/pads",
            priority=2,
            complexity="trivial",
            alternatives=[
                FixSuggestion(
                    action=FixAction.DELETE,
                    target="silkscreen element",
                    parameters={},
                    description="Remove silkscreen element if not needed",
                    priority=3,
                    complexity="trivial",
                ),
            ],
        )

    if violation.type == ViolationType.SILK_OVERLAP:
        return FixSuggestion(
            action=FixAction.MOVE,
            target="silkscreen element",
            parameters={},
            description="Move silkscreen elements to eliminate overlap",
            priority=2,
            complexity="trivial",
        )

    return None


def _calculate_edge_clearance_fix(violation: DRCViolation) -> FixSuggestion | None:
    """Calculate fix for copper-to-edge clearance violations."""
    required = violation.required_value_mm
    actual = violation.actual_value_mm

    if required is not None and actual is not None:
        delta = required - actual + 0.1  # Add margin

        return FixSuggestion(
            action=FixAction.MOVE,
            target="copper element",
            parameters={"distance_mm": round(delta, 3)},
            description=f"Move copper {delta:.2f}mm away from board edge",
            priority=1,
            complexity="easy",
            alternatives=[
                FixSuggestion(
                    action=FixAction.REROUTE,
                    target="edge traces",
                    parameters={},
                    description="Reroute traces away from board edge",
                    priority=2,
                    complexity="moderate",
                ),
            ],
        )

    return FixSuggestion(
        action=FixAction.MOVE,
        target="copper element",
        parameters={},
        description="Move copper away from board edge to meet clearance",
        priority=1,
        complexity="easy",
    )


def generate_fix_suggestions(
    violation: DRCViolation,
) -> FixSuggestion | None:
    """Generate fix suggestion for a DRC violation.

    Args:
        violation: The DRC violation to generate a fix for

    Returns:
        FixSuggestion with recommended fix, or None if no suggestion available
    """
    # Clearance violations
    if violation.type == ViolationType.CLEARANCE:
        return calculate_clearance_fix(violation)

    if violation.type == ViolationType.COPPER_EDGE_CLEARANCE:
        return _calculate_edge_clearance_fix(violation)

    # Track width
    if violation.type == ViolationType.TRACK_WIDTH:
        return _calculate_track_width_fix(violation)

    # Via issues
    if violation.type in (
        ViolationType.VIA_ANNULAR_WIDTH,
        ViolationType.VIA_HOLE_LARGER_THAN_PAD,
        ViolationType.MICRO_VIA_HOLE_TOO_SMALL,
        ViolationType.DRILL_HOLE_TOO_SMALL,
    ):
        return _calculate_via_fix(violation)

    # Connection issues
    if violation.type in (
        ViolationType.UNCONNECTED_ITEMS,
        ViolationType.SHORTING_ITEMS,
    ):
        return _calculate_connection_fix(violation)

    # Silkscreen
    if violation.type in (
        ViolationType.SILK_OVER_COPPER,
        ViolationType.SILK_OVERLAP,
    ):
        return _calculate_silkscreen_fix(violation)

    # Courtyard overlap
    if violation.type == ViolationType.COURTYARD_OVERLAP:
        items = violation.items
        if len(items) >= 2:
            return FixSuggestion(
                action=FixAction.MOVE,
                target=items[0],
                parameters={},
                description=f"Move {items[0]} to eliminate courtyard overlap with {items[1]}",
                priority=1,
                complexity="easy",
            )

    # Solder mask
    if violation.type == ViolationType.SOLDER_MASK_BRIDGE:
        return FixSuggestion(
            action=FixAction.MOVE,
            target="pad/via",
            parameters={},
            description="Increase spacing between pads to allow solder mask bridge",
            priority=1,
            complexity="moderate",
        )

    # Hole near hole
    if violation.type == ViolationType.HOLE_NEAR_HOLE:
        required = violation.required_value_mm
        actual = violation.actual_value_mm
        if required is not None and actual is not None:
            delta = required - actual + 0.1
            return FixSuggestion(
                action=FixAction.MOVE,
                target="hole/via",
                parameters={"distance_mm": round(delta, 3)},
                description=f"Move hole {delta:.2f}mm away from adjacent hole",
                priority=1,
                complexity="easy",
            )

    # Footprint issues
    if violation.type in (
        ViolationType.FOOTPRINT,
        ViolationType.DUPLICATE_FOOTPRINT,
        ViolationType.EXTRA_FOOTPRINT,
        ViolationType.MISSING_FOOTPRINT,
    ):
        return FixSuggestion(
            action=FixAction.ADJUST_RULE,
            target="schematic/footprint",
            parameters={},
            description="Review schematic and footprint assignments",
            priority=1,
            complexity="moderate",
        )

    # No specific suggestion available
    return None
