"""
Query functions for placement evaluation.

Provides high-level query functions for evaluating placement changes.
These functions wrap the PlacementSession API with common operations.

Example:
    >>> from kicad_tools.optim.query import query_position, query_swap
    >>> from kicad_tools.optim.session import PlacementSession
    >>> from kicad_tools.schema.pcb import PCB
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> session = PlacementSession(pcb)
    >>>
    >>> # Query moving a component
    >>> result = query_position(session, "C1", 45.0, 32.0)
    >>>
    >>> # Query swapping two components
    >>> result = query_swap(session, "C1", "C2")
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kicad_tools.optim.session import MoveResult, PlacementSuggestion, RoutingImpact, Violation

if TYPE_CHECKING:
    from kicad_tools.optim.session import PlacementSession

__all__ = [
    "query_position",
    "query_swap",
    "query_alignment",
    "find_best_position",
    "Rectangle",
    "process_json_request",
]


@dataclass
class Rectangle:
    """A rectangular region for constraining searches."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float

    def contains(self, x: float, y: float) -> bool:
        """Check if point is inside rectangle."""
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max

    @property
    def center(self) -> tuple[float, float]:
        """Get center of rectangle."""
        return ((self.x_min + self.x_max) / 2, (self.y_min + self.y_max) / 2)

    @property
    def width(self) -> float:
        """Get width of rectangle."""
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        """Get height of rectangle."""
        return self.y_max - self.y_min


def query_position(
    session: PlacementSession,
    ref: str,
    x: float,
    y: float,
    rotation: float | None = None,
) -> MoveResult:
    """
    Query impact of moving component to position.

    Args:
        session: Active placement session
        ref: Component reference designator
        x: Target X position in mm
        y: Target Y position in mm
        rotation: Target rotation in degrees (None = keep current)

    Returns:
        MoveResult with impact analysis
    """
    return session.query_move(ref, x, y, rotation)


def query_swap(session: PlacementSession, ref1: str, ref2: str) -> MoveResult:
    """
    Query impact of swapping two components.

    Args:
        session: Active placement session
        ref1: First component reference
        ref2: Second component reference

    Returns:
        MoveResult with combined impact of both moves
    """
    pos1 = session.get_component_position(ref1)
    pos2 = session.get_component_position(ref2)

    if not pos1:
        return MoveResult(success=False, error_message=f"Component '{ref1}' not found")
    if not pos2:
        return MoveResult(success=False, error_message=f"Component '{ref2}' not found")

    if pos1.get("fixed"):
        return MoveResult(success=False, error_message=f"Component '{ref1}' is fixed")
    if pos2.get("fixed"):
        return MoveResult(success=False, error_message=f"Component '{ref2}' is fixed")

    # Query moving ref1 to ref2's position
    result1 = session.query_move(ref1, pos2["x"], pos2["y"], pos2["rotation"])
    if not result1.success:
        return result1

    # For swap, we need to simulate the combined effect
    # This is a simplified approximation - in practice both moves happen together
    result2 = session.query_move(ref2, pos1["x"], pos1["y"], pos1["rotation"])
    if not result2.success:
        return result2

    # Combine results
    combined_violations = list(set(result1.new_violations + result2.new_violations))
    combined_resolved = list(set(result1.resolved_violations + result2.resolved_violations))
    combined_affected = list(set(result1.affected_components + result2.affected_components))

    # Combine routing impacts
    all_nets = list(
        set(result1.routing_impact.affected_nets + result2.routing_impact.affected_nets)
    )
    combined_routing = RoutingImpact(
        affected_nets=all_nets,
        estimated_length_change_mm=(
            result1.routing_impact.estimated_length_change_mm
            + result2.routing_impact.estimated_length_change_mm
        ),
        crossing_changes=(
            result1.routing_impact.crossing_changes + result2.routing_impact.crossing_changes
        ),
    )

    combined_warnings = list(set(result1.warnings + result2.warnings))

    return MoveResult(
        success=True,
        new_violations=combined_violations,
        resolved_violations=combined_resolved,
        affected_components=combined_affected,
        routing_impact=combined_routing,
        score_delta=result1.score_delta + result2.score_delta,
        warnings=combined_warnings,
    )


def query_alignment(
    session: PlacementSession,
    refs: list[str],
    axis: str = "x",
    align_to: str = "center",
) -> MoveResult:
    """
    Query impact of aligning components along an axis.

    Args:
        session: Active placement session
        refs: List of component references to align
        axis: Axis to align on ("x" for vertical line, "y" for horizontal line)
        align_to: Alignment point ("center", "min", "max", or a specific value)

    Returns:
        MoveResult with combined impact of alignment moves
    """
    if len(refs) < 2:
        return MoveResult(success=False, error_message="Need at least 2 components to align")

    # Get current positions
    positions = []
    for ref in refs:
        pos = session.get_component_position(ref)
        if not pos:
            return MoveResult(success=False, error_message=f"Component '{ref}' not found")
        if pos.get("fixed"):
            return MoveResult(success=False, error_message=f"Component '{ref}' is fixed")
        positions.append(pos)

    # Calculate alignment target
    if axis == "x":
        values = [p["x"] for p in positions]
    else:
        values = [p["y"] for p in positions]

    if align_to == "center":
        target = sum(values) / len(values)
    elif align_to == "min":
        target = min(values)
    elif align_to == "max":
        target = max(values)
    else:
        try:
            target = float(align_to)
        except ValueError:
            return MoveResult(success=False, error_message=f"Invalid align_to value: {align_to}")

    # Query each alignment move
    combined_new_violations: list[Violation] = []
    combined_resolved: list[Violation] = []
    combined_affected: list[str] = []
    combined_nets: list[str] = []
    total_score_delta = 0.0
    total_length_change = 0.0
    warnings: list[str] = []

    for ref, pos in zip(refs, positions, strict=True):
        if axis == "x":
            new_x, new_y = target, pos["y"]
        else:
            new_x, new_y = pos["x"], target

        result = session.query_move(ref, new_x, new_y)
        if not result.success:
            return result

        combined_new_violations.extend(result.new_violations)
        combined_resolved.extend(result.resolved_violations)
        combined_affected.extend(result.affected_components)
        combined_nets.extend(result.routing_impact.affected_nets)
        total_score_delta += result.score_delta
        total_length_change += result.routing_impact.estimated_length_change_mm
        warnings.extend(result.warnings)

    # Deduplicate
    combined_new_violations = list(
        {(v.type, v.description): v for v in combined_new_violations}.values()
    )
    combined_resolved = list({(v.type, v.description): v for v in combined_resolved}.values())
    combined_affected = list(set(combined_affected))
    combined_nets = list(set(combined_nets))
    warnings = list(set(warnings))

    return MoveResult(
        success=True,
        new_violations=combined_new_violations,
        resolved_violations=combined_resolved,
        affected_components=combined_affected,
        routing_impact=RoutingImpact(
            affected_nets=combined_nets,
            estimated_length_change_mm=total_length_change,
        ),
        score_delta=total_score_delta,
        warnings=warnings,
    )


def find_best_position(
    session: PlacementSession,
    ref: str,
    region: Rectangle | None = None,
    num_suggestions: int = 5,
    grid_step: float = 2.5,
) -> list[PlacementSuggestion]:
    """
    Find best positions for component within optional region.

    Args:
        session: Active placement session
        ref: Component reference designator
        region: Optional rectangular region to constrain search
        num_suggestions: Maximum number of suggestions to return
        grid_step: Grid spacing for search in mm

    Returns:
        List of placement suggestions, sorted by score (best first)
    """
    comp_pos = session.get_component_position(ref)
    if not comp_pos:
        return []

    # If no region specified, search around current position
    if region is None:
        search_radius = 20.0
        region = Rectangle(
            x_min=comp_pos["x"] - search_radius,
            y_min=comp_pos["y"] - search_radius,
            x_max=comp_pos["x"] + search_radius,
            y_max=comp_pos["y"] + search_radius,
        )

    suggestions: list[PlacementSuggestion] = []

    # Grid search within region
    x = region.x_min
    while x <= region.x_max:
        y = region.y_min
        while y <= region.y_max:
            # Skip current position
            if abs(x - comp_pos["x"]) < 0.1 and abs(y - comp_pos["y"]) < 0.1:
                y += grid_step
                continue

            result = session.query_move(ref, x, y)
            if result.success and result.score_delta < 0:
                # Calculate distance from current position for rationale
                dist = math.sqrt((x - comp_pos["x"]) ** 2 + (y - comp_pos["y"]) ** 2)
                suggestions.append(
                    PlacementSuggestion(
                        x=x,
                        y=y,
                        rotation=comp_pos["rotation"],
                        score=-result.score_delta,  # Positive = improvement
                        rationale=f"Improves score by {-result.score_delta:.3f}, {dist:.1f}mm from current",
                    )
                )

            y += grid_step
        x += grid_step

    # Sort by score and limit
    suggestions.sort(key=lambda s: s.score, reverse=True)
    return suggestions[:num_suggestions]


def process_json_request(session: PlacementSession, request: str | dict) -> str:
    """
    Process a JSON API request and return JSON response.

    This is the main entry point for agent JSON communication.

    Request format:
        {
            "action": "query_move" | "apply_move" | "query_swap" | "query_alignment" |
                      "find_best_position" | "undo" | "commit" | "rollback" |
                      "get_status" | "get_position" | "list_components",
            "reference": "C1",           # For single-component actions
            "references": ["C1", "C2"],  # For multi-component actions
            "x": 45.0,                   # Position
            "y": 32.0,
            "rotation": 90,              # Optional
            "axis": "x",                 # For alignment
            "region": {...},             # For find_best_position
            ...
        }

    Response format:
        {
            "success": true/false,
            "result": {...},
            "error": "..."  # If success is false
        }

    Args:
        session: Active placement session
        request: JSON string or dictionary

    Returns:
        JSON response string
    """
    if isinstance(request, str):
        try:
            req = json.loads(request)
        except json.JSONDecodeError as e:
            return json.dumps({"success": False, "error": f"Invalid JSON: {e}"})
    else:
        req = request

    action = req.get("action", "").lower()

    try:
        if action == "query_move":
            ref = req.get("reference", "")
            x = float(req.get("x", 0))
            y = float(req.get("y", 0))
            rotation = req.get("rotation")
            if rotation is not None:
                rotation = float(rotation)
            result = session.query_move(ref, x, y, rotation)
            return json.dumps({"success": True, "result": result.to_dict()})

        elif action == "apply_move":
            ref = req.get("reference", "")
            x = float(req.get("x", 0))
            y = float(req.get("y", 0))
            rotation = req.get("rotation")
            if rotation is not None:
                rotation = float(rotation)
            result = session.apply_move(ref, x, y, rotation)
            return json.dumps({"success": True, "result": result.to_dict()})

        elif action == "query_swap":
            refs = req.get("references", [])
            if len(refs) != 2:
                return json.dumps(
                    {"success": False, "error": "query_swap requires exactly 2 references"}
                )
            result = query_swap(session, refs[0], refs[1])
            return json.dumps({"success": True, "result": result.to_dict()})

        elif action == "query_alignment":
            refs = req.get("references", [])
            axis = req.get("axis", "x")
            align_to = req.get("align_to", "center")
            result = query_alignment(session, refs, axis, align_to)
            return json.dumps({"success": True, "result": result.to_dict()})

        elif action == "find_best_position":
            ref = req.get("reference", "")
            region_data = req.get("region")
            region = None
            if region_data:
                region = Rectangle(
                    x_min=float(region_data.get("x_min", 0)),
                    y_min=float(region_data.get("y_min", 0)),
                    x_max=float(region_data.get("x_max", 0)),
                    y_max=float(region_data.get("y_max", 0)),
                )
            num_suggestions = int(req.get("num_suggestions", 5))
            suggestions = find_best_position(session, ref, region, num_suggestions)
            return json.dumps(
                {
                    "success": True,
                    "result": {"suggestions": [s.to_dict() for s in suggestions]},
                }
            )

        elif action == "undo":
            success = session.undo()
            return json.dumps({"success": True, "result": {"undone": success}})

        elif action == "commit":
            session.commit()
            return json.dumps({"success": True, "result": {"committed": True}})

        elif action == "rollback":
            session.rollback()
            return json.dumps({"success": True, "result": {"rolled_back": True}})

        elif action == "get_status":
            status = session.get_status()
            return json.dumps({"success": True, "result": status})

        elif action == "get_position":
            ref = req.get("reference", "")
            pos = session.get_component_position(ref)
            if pos:
                return json.dumps({"success": True, "result": pos})
            else:
                return json.dumps({"success": False, "error": f"Component '{ref}' not found"})

        elif action == "list_components":
            components = session.list_components()
            return json.dumps({"success": True, "result": {"components": components}})

        elif action == "get_suggestions":
            ref = req.get("reference", "")
            num = int(req.get("num_suggestions", 5))
            suggestions = session.get_suggestions(ref, num)
            return json.dumps(
                {
                    "success": True,
                    "result": {"suggestions": [s.to_dict() for s in suggestions]},
                }
            )

        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
