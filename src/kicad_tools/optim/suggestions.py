"""
Placement suggestions with rationale for agent integration.

Provides explainable placement suggestions that describe why components
are positioned where they are and what alternatives exist. Designed for
LLM agents to understand and reason about placement decisions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from kicad_tools.optim.components import Component
from kicad_tools.optim.geometry import Vector2D

if TYPE_CHECKING:
    from kicad_tools.optim.placement import PlacementOptimizer
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "RationaleType",
    "ForceContribution",
    "AlternativePosition",
    "PlacementSuggestion",
    "generate_placement_suggestions",
    "explain_placement",
    "suggest_improvement",
]


class RationaleType(Enum):
    """Categories of placement rationale."""

    FUNCTIONAL_CLUSTER = "functional_cluster"
    SIGNAL_INTEGRITY = "signal_integrity"
    THERMAL = "thermal"
    EDGE_PLACEMENT = "edge_placement"
    ALIGNMENT = "alignment"
    KEEPOUT_AVOIDANCE = "keepout_avoidance"
    ROUTING_EASE = "routing_ease"
    NET_CONNECTION = "net_connection"
    COMPONENT_SPACING = "component_spacing"
    FIXED_CONSTRAINT = "fixed_constraint"


@dataclass
class ForceContribution:
    """
    A single force contribution to a component's placement.

    Tracks why each force is applied during optimization,
    enabling detailed rationale generation.
    """

    source: str  # e.g., "spring_to_U1", "repulsion_from_Y1", "boundary_edge"
    force_vector: tuple[float, float]
    rationale_type: RationaleType
    description: str

    def magnitude(self) -> float:
        """Calculate force magnitude."""
        return math.sqrt(self.force_vector[0] ** 2 + self.force_vector[1] ** 2)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "source": self.source,
            "force_x": self.force_vector[0],
            "force_y": self.force_vector[1],
            "magnitude": self.magnitude(),
            "rationale_type": self.rationale_type.value,
            "description": self.description,
        }


@dataclass
class AlternativePosition:
    """An alternative position for a component with tradeoff explanation."""

    x: float
    y: float
    rotation: float
    score: float  # 0.0-1.0, higher is better
    tradeoff: str  # What's worse about this position

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "rotation": round(self.rotation, 1),
            "score": round(self.score, 3),
            "tradeoff": self.tradeoff,
        }


@dataclass
class PlacementSuggestion:
    """
    A placement suggestion with full rationale for a component.

    Provides suggested position, confidence, human-readable reasons,
    constraint satisfaction info, and alternative positions.
    """

    reference: str  # Component reference designator
    suggested_x: float
    suggested_y: float
    suggested_rotation: float
    confidence: float  # 0.0 - 1.0
    rationale: list[str]  # Human-readable reasons
    constraints_satisfied: list[str]
    constraints_violated: list[str]
    alternatives: list[AlternativePosition] = field(default_factory=list)
    force_contributions: list[ForceContribution] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "reference": self.reference,
            "suggested_x": round(self.suggested_x, 3),
            "suggested_y": round(self.suggested_y, 3),
            "suggested_rotation": round(self.suggested_rotation, 1),
            "confidence": round(self.confidence, 3),
            "rationale": self.rationale,
            "constraints_satisfied": self.constraints_satisfied,
            "constraints_violated": self.constraints_violated,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
        }


def _calculate_confidence(suggestion: PlacementSuggestion) -> float:
    """
    Calculate confidence based on constraint satisfaction.

    Returns:
        Float between 0.0 and 1.0 indicating confidence level.
    """
    satisfied = len(suggestion.constraints_satisfied)
    violated = len(suggestion.constraints_violated)
    total = satisfied + violated
    if total == 0:
        return 0.5  # No constraints = neutral confidence
    return satisfied / total


def _get_connected_components(
    optimizer: PlacementOptimizer, ref: str
) -> list[tuple[str, str, float]]:
    """
    Get components connected to the given component via nets.

    Returns:
        List of (component_ref, net_name, distance) tuples.
    """
    comp = optimizer.get_component(ref)
    if not comp:
        return []

    connected: list[tuple[str, str, float]] = []
    for spring in optimizer.springs:
        other_ref = None
        if spring.comp1_ref == ref:
            other_ref = spring.comp2_ref
        elif spring.comp2_ref == ref:
            other_ref = spring.comp1_ref

        if other_ref:
            other = optimizer.get_component(other_ref)
            if other:
                dx = other.x - comp.x
                dy = other.y - comp.y
                distance = math.sqrt(dx * dx + dy * dy)
                connected.append((other_ref, spring.net_name, distance))

    return connected


def _identify_component_type(ref: str, comp: Component) -> str:
    """Identify component type from reference designator."""
    prefix = "".join(c for c in ref if c.isalpha())
    types = {
        "C": "capacitor",
        "R": "resistor",
        "L": "inductor",
        "U": "IC",
        "Q": "transistor",
        "D": "diode",
        "LED": "LED",
        "Y": "crystal",
        "J": "connector",
        "P": "connector",
        "H": "mounting hole",
        "MH": "mounting hole",
        "SW": "switch",
        "F": "fuse",
        "FB": "ferrite bead",
    }
    return types.get(prefix, "component")


def _check_bypass_capacitor(
    optimizer: PlacementOptimizer, ref: str, comp: Component
) -> tuple[bool, str | None, float]:
    """
    Check if component is a bypass capacitor near an IC.

    Returns:
        (is_bypass, ic_ref, distance) tuple.
    """
    if not ref.startswith("C"):
        return False, None, 0.0

    connected = _get_connected_components(optimizer, ref)
    for other_ref, net_name, distance in connected:
        # Check if connected to power net and near an IC
        net_lower = net_name.lower()
        is_power = any(
            p in net_lower for p in ["vcc", "vdd", "gnd", "+3", "+5", "+12", "pwr", "v+", "v-"]
        )
        if is_power and other_ref.startswith("U") and distance < 10:
            return True, other_ref, distance

    return False, None, 0.0


def _find_aligned_components(
    optimizer: PlacementOptimizer, ref: str, tolerance: float = 0.5
) -> list[str]:
    """Find components aligned horizontally or vertically with the given component."""
    comp = optimizer.get_component(ref)
    if not comp:
        return []

    aligned = []
    for other in optimizer.components:
        if other.ref == ref:
            continue
        # Check horizontal alignment
        if abs(other.y - comp.y) < tolerance or abs(other.x - comp.x) < tolerance:
            aligned.append(other.ref)

    return aligned


def _check_edge_placement(
    optimizer: PlacementOptimizer, ref: str, edge_threshold: float = 5.0
) -> tuple[bool, str]:
    """
    Check if component is near board edge.

    Returns:
        (is_near_edge, edge_description) tuple.
    """
    comp = optimizer.get_component(ref)
    if not comp:
        return False, ""

    # Get board bounds from outline
    vertices = optimizer.board_outline.vertices
    if not vertices:
        return False, ""

    min_x = min(v.x for v in vertices)
    max_x = max(v.x for v in vertices)
    min_y = min(v.y for v in vertices)
    max_y = max(v.y for v in vertices)

    edges_near = []
    if comp.x - min_x < edge_threshold:
        edges_near.append("left")
    if max_x - comp.x < edge_threshold:
        edges_near.append("right")
    if comp.y - min_y < edge_threshold:
        edges_near.append("top")
    if max_y - comp.y < edge_threshold:
        edges_near.append("bottom")

    if edges_near:
        return True, " and ".join(edges_near) + " edge"
    return False, ""


def _check_keepout_avoidance(optimizer: PlacementOptimizer, ref: str) -> list[str]:
    """Check which keepout zones the component is avoiding."""
    comp = optimizer.get_component(ref)
    if not comp:
        return []

    avoided = []
    for keepout in optimizer.keepouts:
        # Check distance to keepout
        center = comp.position()
        # Simple check: if keepout has a name, check if we're outside it
        if keepout.name:
            # Check if component center is outside keepout
            if not keepout.outline.contains_point(center):
                avoided.append(keepout.name)

    return avoided


def _generate_rationale(
    optimizer: PlacementOptimizer, ref: str
) -> tuple[list[str], list[str], list[str], list[ForceContribution]]:
    """
    Generate rationale, constraints satisfied/violated, and force contributions.

    Returns:
        (rationale, constraints_satisfied, constraints_violated, force_contributions) tuple.
    """
    comp = optimizer.get_component(ref)
    if not comp:
        return [], [], [], []

    rationale: list[str] = []
    satisfied: list[str] = []
    violated: list[str] = []
    forces: list[ForceContribution] = []

    comp_type = _identify_component_type(ref, comp)

    # Check if fixed
    if comp.fixed:
        rationale.append(f"Fixed {comp_type} (connector/mounting hole)")
        satisfied.append(f"fixed_constraint: {ref} position locked")
        forces.append(
            ForceContribution(
                source=f"fixed_{ref}",
                force_vector=(0.0, 0.0),
                rationale_type=RationaleType.FIXED_CONSTRAINT,
                description="Component is fixed in place",
            )
        )
        return rationale, satisfied, violated, forces

    # Check bypass capacitor placement
    is_bypass, ic_ref, distance = _check_bypass_capacitor(optimizer, ref, comp)
    if is_bypass and ic_ref:
        rationale.append(f"Bypass capacitor for {ic_ref} ({distance:.1f}mm away)")
        if distance < 5:
            satisfied.append(f"functional_cluster: within 5mm of {ic_ref}")
        else:
            violated.append(f"functional_cluster: should be within 5mm of {ic_ref}")
        forces.append(
            ForceContribution(
                source=f"spring_to_{ic_ref}",
                force_vector=(0.0, 0.0),  # Would need actual force tracking
                rationale_type=RationaleType.FUNCTIONAL_CLUSTER,
                description=f"Net connection pulling toward {ic_ref}",
            )
        )

    # Check alignment with other components
    aligned = _find_aligned_components(optimizer, ref)
    if aligned:
        aligned_str = ", ".join(aligned[:3])
        if len(aligned) > 3:
            aligned_str += f" and {len(aligned) - 3} more"
        rationale.append(f"Aligned with {aligned_str}")
        satisfied.append(f"alignment: horizontal/vertical with {aligned_str}")

    # Check edge placement
    is_near_edge, edge_desc = _check_edge_placement(optimizer, ref)
    if is_near_edge:
        # Connectors and thermal components should be at edge
        if ref.startswith(("J", "P", "Q")):
            rationale.append(f"Positioned at board {edge_desc} for accessibility")
            satisfied.append(f"edge_placement: at {edge_desc}")
        else:
            rationale.append(f"Near {edge_desc}")

    # Check keepout avoidance
    avoided_keepouts = _check_keepout_avoidance(optimizer, ref)
    for keepout_name in avoided_keepouts:
        rationale.append(f"Avoiding keepout zone: {keepout_name}")
        satisfied.append(f"keepout_avoidance: outside {keepout_name}")

    # Check net connections for routing
    connected = _get_connected_components(optimizer, ref)
    if connected:
        nearby = [c for c in connected if c[2] < 15]
        if nearby:
            net_refs = [f"{c[0]} ({c[1]})" for c in nearby[:3]]
            rationale.append(f"Connected to: {', '.join(net_refs)}")
            for other_ref, net_name, dist in nearby:
                satisfied.append(f"routing_ease: {net_name} to {other_ref} ({dist:.1f}mm)")
                forces.append(
                    ForceContribution(
                        source=f"spring_to_{other_ref}",
                        force_vector=(0.0, 0.0),
                        rationale_type=RationaleType.NET_CONNECTION,
                        description=f"Net {net_name} pulling toward {other_ref}",
                    )
                )

    # If no specific rationale, add generic
    if not rationale:
        rationale.append(f"Optimized position for {comp_type}")

    return rationale, satisfied, violated, forces


def _generate_alternatives(
    optimizer: PlacementOptimizer, ref: str, num_alternatives: int = 3
) -> list[AlternativePosition]:
    """
    Generate alternative positions for a component.

    Considers nearby positions with different rotation angles.
    """
    comp = optimizer.get_component(ref)
    if not comp or comp.fixed:
        return []

    alternatives: list[AlternativePosition] = []
    original_x, original_y, original_rot = comp.x, comp.y, comp.rotation

    # Try different rotations at current position
    rotations = [0, 90, 180, 270]
    for rot in rotations:
        if rot == original_rot:
            continue

        # Score based on how close to original orientation
        rot_diff = abs(rot - original_rot)
        if rot_diff > 180:
            rot_diff = 360 - rot_diff
        score = 1.0 - (rot_diff / 360) * 0.2  # Small penalty for rotation

        alternatives.append(
            AlternativePosition(
                x=original_x,
                y=original_y,
                rotation=rot,
                score=score,
                tradeoff=f"Different orientation ({rot}° vs {original_rot}°)",
            )
        )

    # Try small position offsets
    offsets = [(2.0, 0), (-2.0, 0), (0, 2.0), (0, -2.0)]
    for dx, dy in offsets:
        new_x = original_x + dx
        new_y = original_y + dy

        # Check if still inside board
        new_pos = Vector2D(new_x, new_y)
        if optimizer.board_outline.contains_point(new_pos):
            distance = math.sqrt(dx * dx + dy * dy)
            score = 0.8 - distance * 0.05  # Penalty for distance

            direction = "right" if dx > 0 else "left" if dx < 0 else "down" if dy > 0 else "up"
            alternatives.append(
                AlternativePosition(
                    x=new_x,
                    y=new_y,
                    rotation=original_rot,
                    score=max(0.1, score),
                    tradeoff=f"{distance:.1f}mm {direction} from optimal",
                )
            )

    # Sort by score and return top N
    alternatives.sort(key=lambda a: a.score, reverse=True)
    return alternatives[:num_alternatives]


def generate_placement_suggestions(
    pcb: PCB | None = None,
    optimizer: PlacementOptimizer | None = None,
    constraints: list | None = None,
) -> dict[str, PlacementSuggestion]:
    """
    Generate placement suggestions with rationale for all components.

    Args:
        pcb: PCB object (will create optimizer from it if optimizer not provided)
        optimizer: Pre-configured PlacementOptimizer
        constraints: Optional list of additional constraints

    Returns:
        Dictionary mapping component reference to PlacementSuggestion.
    """
    from kicad_tools.optim.placement import PlacementOptimizer

    if optimizer is None and pcb is None:
        raise ValueError("Either pcb or optimizer must be provided")

    if optimizer is None:
        optimizer = PlacementOptimizer.from_pcb(pcb)

    suggestions: dict[str, PlacementSuggestion] = {}

    for comp in optimizer.components:
        rationale, satisfied, violated, forces = _generate_rationale(optimizer, comp.ref)
        alternatives = _generate_alternatives(optimizer, comp.ref)

        suggestion = PlacementSuggestion(
            reference=comp.ref,
            suggested_x=comp.x,
            suggested_y=comp.y,
            suggested_rotation=comp.rotation,
            confidence=0.0,  # Will be calculated
            rationale=rationale,
            constraints_satisfied=satisfied,
            constraints_violated=violated,
            alternatives=alternatives,
            force_contributions=forces,
        )

        # Calculate confidence
        suggestion.confidence = _calculate_confidence(suggestion)

        suggestions[comp.ref] = suggestion

    return suggestions


def explain_placement(
    pcb: PCB | None = None,
    optimizer: PlacementOptimizer | None = None,
    reference: str = "",
) -> PlacementSuggestion | None:
    """
    Explain why a component is in its current position.

    Args:
        pcb: PCB object
        optimizer: Pre-configured PlacementOptimizer
        reference: Component reference designator

    Returns:
        PlacementSuggestion for the component, or None if not found.
    """
    from kicad_tools.optim.placement import PlacementOptimizer

    if optimizer is None and pcb is None:
        raise ValueError("Either pcb or optimizer must be provided")

    if optimizer is None:
        optimizer = PlacementOptimizer.from_pcb(pcb)

    comp = optimizer.get_component(reference)
    if not comp:
        return None

    rationale, satisfied, violated, forces = _generate_rationale(optimizer, reference)
    alternatives = _generate_alternatives(optimizer, reference)

    suggestion = PlacementSuggestion(
        reference=reference,
        suggested_x=comp.x,
        suggested_y=comp.y,
        suggested_rotation=comp.rotation,
        confidence=0.0,
        rationale=rationale,
        constraints_satisfied=satisfied,
        constraints_violated=violated,
        alternatives=alternatives,
        force_contributions=forces,
    )

    suggestion.confidence = _calculate_confidence(suggestion)
    return suggestion


def suggest_improvement(
    pcb: PCB | None = None,
    optimizer: PlacementOptimizer | None = None,
    reference: str = "",
) -> PlacementSuggestion | None:
    """
    Suggest improved position for a component, if any.

    This runs a local optimization to see if a better position exists.

    Args:
        pcb: PCB object
        optimizer: Pre-configured PlacementOptimizer
        reference: Component reference designator

    Returns:
        PlacementSuggestion with improved position, or None if no improvement found.
    """
    from kicad_tools.optim.placement import PlacementOptimizer

    if optimizer is None and pcb is None:
        raise ValueError("Either pcb or optimizer must be provided")

    if optimizer is None:
        optimizer = PlacementOptimizer.from_pcb(pcb)

    comp = optimizer.get_component(reference)
    if not comp or comp.fixed:
        return None

    # Store original position
    original_x, original_y, original_rot = comp.x, comp.y, comp.rotation
    original_energy = optimizer.compute_energy()

    # Run a few iterations of local optimization
    optimizer.run(iterations=50, dt=0.01)

    # Check if position improved
    new_energy = optimizer.compute_energy()
    improved = new_energy < original_energy * 0.95  # At least 5% improvement

    if improved:
        rationale, satisfied, violated, forces = _generate_rationale(optimizer, reference)

        dx = comp.x - original_x
        dy = comp.y - original_y
        distance = math.sqrt(dx * dx + dy * dy)

        improvement_rationale = [
            f"Moved {distance:.1f}mm from original position",
            f"Energy reduced by {((original_energy - new_energy) / original_energy) * 100:.1f}%",
        ]
        rationale = improvement_rationale + rationale

        suggestion = PlacementSuggestion(
            reference=reference,
            suggested_x=comp.x,
            suggested_y=comp.y,
            suggested_rotation=comp.rotation,
            confidence=0.0,
            rationale=rationale,
            constraints_satisfied=satisfied,
            constraints_violated=violated,
            alternatives=[
                AlternativePosition(
                    x=original_x,
                    y=original_y,
                    rotation=original_rot,
                    score=0.7,
                    tradeoff="Original position (higher energy)",
                )
            ],
            force_contributions=forces,
        )

        suggestion.confidence = _calculate_confidence(suggestion)
        return suggestion

    # Restore original position if no improvement
    comp.x, comp.y, comp.rotation = original_x, original_y, original_rot
    comp.update_pin_positions()

    return None
