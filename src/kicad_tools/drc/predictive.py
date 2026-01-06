"""Predictive analysis for component placement.

This module provides predictive warnings that anticipate problems before
they become DRC violations. When an agent moves a component, it warns
about potential routing difficulties, congestion, and intent violations.

Example:
    >>> from kicad_tools.drc.predictive import PredictiveAnalyzer
    >>> from kicad_tools.optim.session import PlacementSession
    >>>
    >>> session = PlacementSession(pcb)
    >>> analyzer = PredictiveAnalyzer(session)
    >>>
    >>> # Analyze a proposed move
    >>> warnings = analyzer.analyze_move("U1", (50.0, 30.0))
    >>> for w in warnings:
    ...     print(f"{w.type}: {w.message}")
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.drc.incremental import Rectangle

if TYPE_CHECKING:
    from kicad_tools.intent.types import IntentDeclaration
    from kicad_tools.optim.session import PlacementSession


@dataclass
class PredictiveWarning:
    """A warning about potential future problems.

    Predictive warnings anticipate issues that may arise from a component
    move, such as routing difficulties, congestion, or intent risks.

    Attributes:
        type: Warning type identifier:
            - "routing_difficulty": Move makes routing harder
            - "congestion": Area becoming too dense
            - "thermal": Thermal management concerns
            - "intent_risk": May violate declared design intents
        message: Human-readable description of the warning
        confidence: Confidence level from 0.0 to 1.0
        suggestion: Optional suggestion to avoid the problem
        affected_nets: Net names affected by this warning
        location: (x, y) position where issue may occur
    """

    type: str
    message: str
    confidence: float
    suggestion: str | None = None
    affected_nets: list[str] = field(default_factory=list)
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result: dict = {
            "type": self.type,
            "message": self.message,
            "confidence": round(self.confidence, 2),
            "affected_nets": self.affected_nets,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        if self.location:
            result["location"] = {"x": round(self.location[0], 3), "y": round(self.location[1], 3)}
        return result


class PredictiveAnalyzer:
    """Analyzes moves for potential future problems.

    Provides predictive warnings about routing difficulty, congestion,
    thermal concerns, and intent risks before moves are applied.

    Performance target: analysis adds <10ms to response time.

    Example:
        >>> analyzer = PredictiveAnalyzer(session, intents)
        >>> warnings = analyzer.analyze_move("U1", (50.0, 30.0))
    """

    # Configuration thresholds
    DIFFICULTY_INCREASE_THRESHOLD = 1.5  # 50% harder to route
    CONGESTION_UTILIZATION_THRESHOLD = 0.8  # 80% routing utilization
    LENGTH_MATCH_TOLERANCE = 0.15  # 15% length variation threshold
    MIN_CONFIDENCE_THRESHOLD = 0.5  # Minimum confidence to report

    # Congestion analysis parameters
    CONGESTION_AREA_SIZE = 10.0  # mm - size of area to check for congestion
    PINS_PER_MM2_WARNING = 2.0  # Pin density threshold for congestion warning

    def __init__(
        self,
        session: PlacementSession,
        intents: list[IntentDeclaration] | None = None,
    ) -> None:
        """Initialize the predictive analyzer.

        Args:
            session: The placement session to analyze
            intents: Optional list of declared design intents
        """
        self.session = session
        self.intents = intents or []
        self._optimizer = session._optimizer
        self._drc_engine = session._drc_engine

    def analyze_move(
        self,
        ref: str,
        new_pos: tuple[float, float],
    ) -> list[PredictiveWarning]:
        """Analyze a proposed move for potential problems.

        Performs predictive analysis to warn about potential issues
        that may arise from moving a component.

        Args:
            ref: Component reference designator
            new_pos: New (x, y) position in mm

        Returns:
            List of predictive warnings, filtered by confidence threshold
        """
        start_time = time.perf_counter()
        warnings: list[PredictiveWarning] = []

        comp = self._optimizer.get_component(ref)
        if not comp:
            return warnings

        current_pos = (comp.x, comp.y)

        # Check routing difficulty
        warnings.extend(self._check_routing_difficulty(ref, current_pos, new_pos))

        # Check congestion
        warnings.extend(self._check_congestion(ref, new_pos))

        # Check thermal implications
        warnings.extend(self._check_thermal(ref, new_pos))

        # Check intent risks
        if self.intents:
            warnings.extend(self._check_intent_risks(ref, new_pos))

        # Filter by confidence threshold
        warnings = [w for w in warnings if w.confidence >= self.MIN_CONFIDENCE_THRESHOLD]

        # Store analysis time for debugging
        self._last_analysis_time_ms = (time.perf_counter() - start_time) * 1000

        return warnings

    def _check_routing_difficulty(
        self,
        ref: str,
        current_pos: tuple[float, float],
        new_pos: tuple[float, float],
    ) -> list[PredictiveWarning]:
        """Estimate routing difficulty change.

        Analyzes how the move affects routing complexity for connected nets.
        Considers Manhattan distance, obstacles, and congestion.

        Args:
            ref: Component reference
            current_pos: Current (x, y) position
            new_pos: New (x, y) position

        Returns:
            List of routing difficulty warnings
        """
        warnings: list[PredictiveWarning] = []
        comp = self._optimizer.get_component(ref)
        if not comp:
            return warnings

        # Get connected nets
        connected_nets = self._get_connected_nets(ref)

        for net_name in connected_nets:
            # Calculate routing difficulty at old and new positions
            old_difficulty = self._estimate_route_difficulty(net_name, current_pos)
            new_difficulty = self._estimate_route_difficulty(net_name, new_pos)

            if old_difficulty <= 0:
                continue

            difficulty_ratio = new_difficulty / old_difficulty

            if difficulty_ratio > self.DIFFICULTY_INCREASE_THRESHOLD:
                # Calculate confidence based on how much harder it is
                confidence = min(0.9, 0.5 + (difficulty_ratio - 1.5) * 0.2)

                # Generate suggestion
                suggestion = self._suggest_better_position(ref, net_name, current_pos, new_pos)

                warnings.append(
                    PredictiveWarning(
                        type="routing_difficulty",
                        message=f"Routing {net_name} will be significantly harder from this position",
                        confidence=confidence,
                        suggestion=suggestion,
                        affected_nets=[net_name],
                        location=new_pos,
                    )
                )

        return warnings

    def _estimate_route_difficulty(
        self,
        net_name: str,
        component_pos: tuple[float, float],
    ) -> float:
        """Estimate routing difficulty for a net.

        Factors:
        - Manhattan distance to other net endpoints
        - Number of obstacles in path
        - Congestion in routing channels

        Args:
            net_name: Name of the net
            component_pos: Position of the component

        Returns:
            Difficulty score (higher = harder to route)
        """
        # Find other components connected to this net
        endpoints = self._get_net_endpoints(net_name, exclude_pos=component_pos)
        if not endpoints:
            return 0.0

        # Base difficulty from total Manhattan distance
        total_distance = sum(
            abs(component_pos[0] - ep[0]) + abs(component_pos[1] - ep[1]) for ep in endpoints
        )

        # Obstacle penalty - count components in routing path
        obstacle_penalty = 0.0
        for ep in endpoints:
            path_bounds = self._compute_routing_bounds(component_pos, ep)
            obstacles = self._query_spatial_index(path_bounds)
            obstacle_penalty += len(obstacles) * 0.5

        # Congestion penalty - net density in routing area
        congestion_penalty = 0.0
        for ep in endpoints:
            path_bounds = self._compute_routing_bounds(component_pos, ep)
            congestion = self._estimate_congestion(path_bounds)
            congestion_penalty += congestion * 2.0

        return total_distance + obstacle_penalty + congestion_penalty

    def _check_congestion(
        self,
        ref: str,
        new_pos: tuple[float, float],
    ) -> list[PredictiveWarning]:
        """Check if move increases local congestion dangerously.

        Analyzes component and pin density in the target area.

        Args:
            ref: Component reference
            new_pos: New (x, y) position

        Returns:
            List of congestion warnings
        """
        warnings: list[PredictiveWarning] = []

        # Define area around new position
        half_size = self.CONGESTION_AREA_SIZE / 2
        area = Rectangle(
            new_pos[0] - half_size,
            new_pos[1] - half_size,
            new_pos[0] + half_size,
            new_pos[1] + half_size,
        )

        # Query nearby components
        nearby_refs = self._query_spatial_index(area)
        # Exclude the component being moved
        nearby_refs = [r for r in nearby_refs if r != ref]

        # Count pins in area
        pin_count = 0
        for nearby_ref in nearby_refs:
            nearby_comp = self._optimizer.get_component(nearby_ref)
            if nearby_comp:
                pin_count += len(nearby_comp.pins)

        # Add pins from the component being moved
        comp = self._optimizer.get_component(ref)
        if comp:
            pin_count += len(comp.pins)

        # Calculate pin density
        area_mm2 = self.CONGESTION_AREA_SIZE * self.CONGESTION_AREA_SIZE
        pin_density = pin_count / area_mm2

        # Estimate routing channel capacity and utilization
        channel_capacity = self._estimate_channel_capacity(area)
        required_capacity = self._estimate_required_capacity(nearby_refs + [ref])

        if channel_capacity > 0:
            utilization = required_capacity / channel_capacity

            if utilization > self.CONGESTION_UTILIZATION_THRESHOLD:
                utilization_pct = int(utilization * 100)

                # Calculate confidence based on how congested
                confidence = min(0.85, 0.5 + (utilization - 0.8) * 1.5)

                warnings.append(
                    PredictiveWarning(
                        type="congestion",
                        message=(
                            f"Area around ({new_pos[0]:.1f}, {new_pos[1]:.1f}) is becoming "
                            f"congested ({utilization_pct}% routing utilization)"
                        ),
                        confidence=confidence,
                        suggestion="Consider spreading components or using inner layers",
                        affected_nets=self._get_nets_in_area(area),
                        location=new_pos,
                    )
                )
        elif pin_density > self.PINS_PER_MM2_WARNING:
            # Fallback to pin density check
            warnings.append(
                PredictiveWarning(
                    type="congestion",
                    message=(
                        f"High pin density ({pin_density:.1f} pins/mm²) around "
                        f"({new_pos[0]:.1f}, {new_pos[1]:.1f})"
                    ),
                    confidence=0.6,
                    suggestion="Consider spreading components for easier routing",
                    affected_nets=self._get_nets_in_area(area),
                    location=new_pos,
                )
            )

        return warnings

    def _check_thermal(
        self,
        ref: str,
        new_pos: tuple[float, float],
    ) -> list[PredictiveWarning]:
        """Check if move has thermal implications.

        Currently a placeholder for future thermal analysis.
        Could check for:
        - Moving heat sources close together
        - Moving heat sources away from thermal vias
        - Moving heat sources near heat-sensitive components

        Args:
            ref: Component reference
            new_pos: New (x, y) position

        Returns:
            List of thermal warnings (currently empty)
        """
        # Thermal analysis is a future enhancement
        # For now, return empty list
        return []

    def _check_intent_risks(
        self,
        ref: str,
        new_pos: tuple[float, float],
    ) -> list[PredictiveWarning]:
        """Check if move risks violating declared intents.

        Analyzes impact on length matching, differential pairs,
        and other interface constraints.

        Args:
            ref: Component reference
            new_pos: New (x, y) position

        Returns:
            List of intent risk warnings
        """
        warnings: list[PredictiveWarning] = []
        comp = self._optimizer.get_component(ref)
        if not comp:
            return warnings

        current_pos = (comp.x, comp.y)
        connected_nets = set(self._get_connected_nets(ref))

        for intent in self.intents:
            # Check if this component's nets are part of the intent
            overlapping_nets = connected_nets.intersection(intent.nets)
            if not overlapping_nets:
                continue

            # Check length matching constraints
            for constraint in intent.constraints:
                c_type = constraint.type
                if c_type == "length_match":
                    warning = self._check_length_match_risk(
                        intent, current_pos, new_pos, list(overlapping_nets)
                    )
                    if warning:
                        warnings.append(warning)

                elif c_type == "differential_pair":
                    warning = self._check_differential_pair_risk(
                        intent, ref, new_pos, list(overlapping_nets)
                    )
                    if warning:
                        warnings.append(warning)

        return warnings

    def _check_length_match_risk(
        self,
        intent: IntentDeclaration,
        current_pos: tuple[float, float],
        new_pos: tuple[float, float],
        affected_nets: list[str],
    ) -> PredictiveWarning | None:
        """Check if move puts length matching at risk.

        Args:
            intent: The intent declaration with length match constraint
            current_pos: Current component position
            new_pos: New component position
            affected_nets: Nets affected by this move

        Returns:
            Warning if length matching is at risk, None otherwise
        """
        # Estimate current lengths
        current_lengths = {}
        for net in intent.nets:
            current_lengths[net] = self._estimate_net_length(net)

        if not current_lengths:
            return None

        # Calculate move delta
        dx = new_pos[0] - current_pos[0]
        dy = new_pos[1] - current_pos[1]
        move_distance = math.sqrt(dx * dx + dy * dy)

        # Project how lengths would change
        # Affected nets will change by approximately the move distance
        projected_lengths = dict(current_lengths)
        for net in affected_nets:
            if net in projected_lengths:
                # Rough estimate: length changes by move distance
                projected_lengths[net] += move_distance

        # Check if length matching is at risk
        if len(projected_lengths) < 2:
            return None

        max_length = max(projected_lengths.values())
        min_length = min(projected_lengths.values())

        if min_length <= 0:
            return None

        length_variation = (max_length - min_length) / min_length

        if length_variation > self.LENGTH_MATCH_TOLERANCE:
            return PredictiveWarning(
                type="intent_risk",
                message=(
                    f"This move may make {intent.interface_type} length matching difficult "
                    f"(projected {length_variation * 100:.0f}% variation)"
                ),
                confidence=0.6,
                suggestion="Keep matched traces similar length",
                affected_nets=intent.nets,
                location=new_pos,
            )

        return None

    def _check_differential_pair_risk(
        self,
        intent: IntentDeclaration,
        ref: str,
        new_pos: tuple[float, float],
        affected_nets: list[str],
    ) -> PredictiveWarning | None:
        """Check if move puts differential pair constraints at risk.

        Args:
            intent: The intent declaration with differential pair constraint
            ref: Component reference being moved
            new_pos: New component position
            affected_nets: Nets affected by this move

        Returns:
            Warning if differential pair is at risk, None otherwise
        """
        # For differential pairs, we want the endpoints to stay close
        # Check if moving one component spreads the pair endpoints apart
        if len(intent.nets) < 2:
            return None

        # Get endpoints for both nets of the pair
        net1, net2 = intent.nets[0], intent.nets[1]
        endpoints1 = self._get_net_endpoints(net1)
        endpoints2 = self._get_net_endpoints(net2)

        if not endpoints1 or not endpoints2:
            return None

        # Check if move significantly increases distance between pair endpoints
        # This is a simplified check - real implementation would be more sophisticated
        for ep1 in endpoints1:
            for ep2 in endpoints2:
                current_dist = math.sqrt((ep1[0] - ep2[0]) ** 2 + (ep1[1] - ep2[1]) ** 2)
                # If any current distance is already large, warn about further spread
                if current_dist > 5.0:  # 5mm threshold
                    return PredictiveWarning(
                        type="intent_risk",
                        message=(
                            f"Differential pair {intent.interface_type} endpoints may spread "
                            f"further apart, affecting signal integrity"
                        ),
                        confidence=0.55,
                        suggestion="Keep differential pair endpoints close together",
                        affected_nets=intent.nets,
                        location=new_pos,
                    )

        return None

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_connected_nets(self, ref: str) -> list[str]:
        """Get net names connected to a component."""
        comp = self._optimizer.get_component(ref)
        if not comp:
            return []

        nets = set()
        for pin in comp.pins:
            if pin.net_name:
                nets.add(pin.net_name)

        return list(nets)

    def _get_net_endpoints(
        self,
        net_name: str,
        exclude_pos: tuple[float, float] | None = None,
    ) -> list[tuple[float, float]]:
        """Get endpoint positions for a net.

        Args:
            net_name: Name of the net
            exclude_pos: Optional position to exclude from results

        Returns:
            List of (x, y) positions of net endpoints
        """
        endpoints: list[tuple[float, float]] = []

        for comp in self._optimizer.components:
            for pin in comp.pins:
                if pin.net_name == net_name:
                    pos = (comp.x + pin.x, comp.y + pin.y)
                    if exclude_pos is None or (
                        abs(pos[0] - exclude_pos[0]) > 0.1 or abs(pos[1] - exclude_pos[1]) > 0.1
                    ):
                        endpoints.append(pos)

        return endpoints

    def _compute_routing_bounds(
        self,
        pos1: tuple[float, float],
        pos2: tuple[float, float],
    ) -> Rectangle:
        """Compute bounding box for routing between two points.

        Adds margin for routing flexibility.

        Args:
            pos1: First position
            pos2: Second position

        Returns:
            Rectangle bounding box for routing area
        """
        margin = 2.0  # mm routing margin
        return Rectangle(
            min(pos1[0], pos2[0]) - margin,
            min(pos1[1], pos2[1]) - margin,
            max(pos1[0], pos2[0]) + margin,
            max(pos1[1], pos2[1]) + margin,
        )

    def _query_spatial_index(self, bounds: Rectangle) -> list[str]:
        """Query spatial index for components in bounds.

        Args:
            bounds: Rectangle to query

        Returns:
            List of component references in the area
        """
        if self._drc_engine.state is None:
            return []
        return self._drc_engine.state.spatial_index.query(bounds)

    def _estimate_congestion(self, bounds: Rectangle) -> float:
        """Estimate routing congestion in an area.

        Args:
            bounds: Area to check

        Returns:
            Congestion score from 0.0 to 1.0
        """
        nearby = self._query_spatial_index(bounds)

        # Count total pins in area
        pin_count = 0
        for ref in nearby:
            comp = self._optimizer.get_component(ref)
            if comp:
                pin_count += len(comp.pins)

        # Normalize by area
        area = bounds.width * bounds.height
        if area <= 0:
            return 0.0

        # Rough estimate: each pin needs ~1mm² of routing space
        estimated_required = pin_count * 1.0
        return min(1.0, estimated_required / area)

    def _estimate_channel_capacity(self, area: Rectangle) -> float:
        """Estimate routing channel capacity for an area.

        Args:
            area: Area to analyze

        Returns:
            Estimated capacity in routing units
        """
        # Simplified model: capacity based on area and assumed trace width
        trace_width = 0.2  # mm typical trace width
        spacing = 0.15  # mm typical spacing

        # Estimate number of traces that could fit
        width_traces = area.width / (trace_width + spacing)
        height_traces = area.height / (trace_width + spacing)

        # Capacity is roughly the number of trace-crossings possible
        return width_traces * height_traces * 0.5  # 50% efficiency factor

    def _estimate_required_capacity(self, refs: list[str]) -> float:
        """Estimate required routing capacity for components.

        Args:
            refs: Component references to analyze

        Returns:
            Estimated required capacity in routing units
        """
        total_nets = set()

        for ref in refs:
            comp = self._optimizer.get_component(ref)
            if comp:
                for pin in comp.pins:
                    if pin.net_name and pin.net_name not in ("GND", "VCC", "VDD"):
                        total_nets.add(pin.net_name)

        # Each net needs at least 1 routing channel
        return len(total_nets)

    def _get_nets_in_area(self, bounds: Rectangle) -> list[str]:
        """Get all nets with endpoints in an area.

        Args:
            bounds: Area to check

        Returns:
            List of net names in the area
        """
        nets = set()

        nearby = self._query_spatial_index(bounds)
        for ref in nearby:
            comp = self._optimizer.get_component(ref)
            if comp:
                for pin in comp.pins:
                    if pin.net_name:
                        nets.add(pin.net_name)

        return list(nets)

    def _suggest_better_position(
        self,
        ref: str,
        net_name: str,
        current_pos: tuple[float, float],
        proposed_pos: tuple[float, float],
    ) -> str | None:
        """Suggest a better position that maintains routing ease.

        Args:
            ref: Component reference
            net_name: Net that would be harder to route
            current_pos: Current component position
            proposed_pos: Proposed new position

        Returns:
            Suggestion string, or None if no good suggestion
        """
        # Calculate move direction
        dx = proposed_pos[0] - current_pos[0]
        dy = proposed_pos[1] - current_pos[1]

        if abs(dx) > abs(dy):
            direction = "left" if dx < 0 else "right"
            return f"Consider moving less far {direction} to maintain clear routing channel"
        else:
            direction = "up" if dy > 0 else "down"
            return f"Consider moving less far {direction} to maintain clear routing channel"

    def _estimate_net_length(self, net_name: str) -> float:
        """Estimate total length of a net.

        Uses minimum spanning tree approximation.

        Args:
            net_name: Name of the net

        Returns:
            Estimated length in mm
        """
        endpoints = self._get_net_endpoints(net_name)
        if len(endpoints) < 2:
            return 0.0

        # Simple approximation: sum of Manhattan distances from centroid
        cx = sum(p[0] for p in endpoints) / len(endpoints)
        cy = sum(p[1] for p in endpoints) / len(endpoints)

        total_length = 0.0
        for ep in endpoints:
            total_length += abs(ep[0] - cx) + abs(ep[1] - cy)

        return total_length


__all__ = [
    "PredictiveAnalyzer",
    "PredictiveWarning",
]
