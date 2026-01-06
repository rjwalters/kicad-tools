"""
Strategy generation for failure recovery.

This module provides the StrategyGenerator class which analyzes failures
and generates concrete resolution strategies with actions and side effects.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .types import (
    Action,
    BlockingElement,
    Difficulty,
    FailureAnalysis,
    FailureCause,
    Rectangle,
    ResolutionStrategy,
    SideEffect,
    StrategyType,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


class StrategyGenerator:
    """Generates resolution strategies from failure analysis.

    Given a failure analysis, this class generates multiple ranked strategies
    for resolving the failure, each with concrete actions and side effect
    analysis.

    Example::

        generator = StrategyGenerator()
        strategies = generator.generate_strategies(pcb, failure_analysis)

        for strategy in strategies:
            print(f"{strategy.type.value}: {strategy.confidence:.2f} confidence")
            for action in strategy.actions:
                print(f"  - {action.type} {action.target}")
    """

    # Distance thresholds in mm
    BYPASS_CAP_DISTANCE_THRESHOLD = 5.0
    CLUSTER_DISTANCE_THRESHOLD = 10.0

    def generate_strategies(self, pcb: PCB, failure: FailureAnalysis) -> list[ResolutionStrategy]:
        """Generate ranked strategies to resolve failure.

        Args:
            pcb: The PCB being analyzed.
            failure: Detailed failure analysis.

        Returns:
            List of resolution strategies, ranked by (difficulty, confidence).
        """
        strategies: list[ResolutionStrategy] = []

        # Strategy 1: Move blocking components
        if failure.has_movable_blockers:
            strategies.extend(self._generate_move_strategies(pcb, failure))

        # Strategy 2: Add vias to change layers
        if failure.root_cause in [FailureCause.CONGESTION, FailureCause.BLOCKED_PATH]:
            strategies.extend(self._generate_via_strategies(pcb, failure))

        # Strategy 3: Reroute blocking nets
        if failure.has_reroutable_nets:
            strategies.extend(self._generate_reroute_strategies(pcb, failure))

        # Strategy 4: Spread components (for congestion)
        if failure.root_cause == FailureCause.CONGESTION:
            strategies.extend(self._generate_spread_strategies(pcb, failure))

        # Strategy 5: Change layer (for layer conflicts)
        if failure.root_cause == FailureCause.LAYER_CONFLICT:
            strategies.extend(self._generate_layer_change_strategies(pcb, failure))

        # Strategy 6: Manual intervention (fallback)
        if not strategies or failure.root_cause in [
            FailureCause.KEEPOUT,
            FailureCause.LENGTH_CONSTRAINT,
            FailureCause.DIFFERENTIAL_PAIR,
        ]:
            strategies.append(self._generate_manual_strategy(failure))

        # Rank by (difficulty, confidence, side_effects)
        return self._rank_strategies(strategies)

    def _generate_move_strategies(
        self, pcb: PCB, failure: FailureAnalysis
    ) -> list[ResolutionStrategy]:
        """Generate component move strategies.

        Analyzes blocking elements and suggests moving movable components
        to clear the routing path.
        """
        strategies: list[ResolutionStrategy] = []

        for blocker in failure.blocking_elements:
            if not blocker.movable or blocker.type != "component":
                continue

            if blocker.ref is None:
                continue

            # Find good positions for this component
            candidates = self._find_move_candidates(pcb, blocker, failure.failure_area)

            for candidate in candidates[:3]:  # Top 3 positions
                # Analyze side effects
                side_effects = self._analyze_move_side_effects(
                    pcb, blocker.ref, candidate["position"]
                )

                # Get nets connected to this component
                affected_nets = self._get_component_nets(pcb, blocker.ref)

                strategies.append(
                    ResolutionStrategy(
                        type=StrategyType.MOVE_COMPONENT,
                        difficulty=self._assess_move_difficulty(side_effects),
                        confidence=candidate["confidence"],
                        actions=[
                            Action(
                                type="move",
                                target=blocker.ref,
                                params={
                                    "x": candidate["position"][0],
                                    "y": candidate["position"][1],
                                },
                            )
                        ],
                        side_effects=side_effects,
                        affected_components=[blocker.ref],
                        affected_nets=affected_nets,
                        estimated_improvement=candidate["improvement"],
                    )
                )

        return strategies

    def _generate_via_strategies(
        self, pcb: PCB, failure: FailureAnalysis
    ) -> list[ResolutionStrategy]:
        """Generate via addition strategies.

        Suggests adding vias to route on different layers when
        the current layer is congested or blocked.
        """
        strategies: list[ResolutionStrategy] = []

        if failure.net is None:
            return strategies

        # Find viable via positions
        via_positions = self._find_via_positions(pcb, failure)

        for pos, target_layer in via_positions[:2]:
            side_effects = [
                SideEffect(
                    description="Uses via budget (adds 1 via)",
                    severity="info",
                    mitigatable=False,
                ),
                SideEffect(
                    description=f"Route continues on {target_layer}",
                    severity="info",
                    mitigatable=False,
                ),
            ]

            strategies.append(
                ResolutionStrategy(
                    type=StrategyType.ADD_VIA,
                    difficulty=Difficulty.MEDIUM,
                    confidence=0.8,
                    actions=[
                        Action(
                            type="add_via",
                            target=failure.net,
                            params={"x": pos[0], "y": pos[1], "layer": target_layer},
                        )
                    ],
                    side_effects=side_effects,
                    affected_components=[],
                    affected_nets=[failure.net],
                    estimated_improvement=0.7,
                )
            )

        return strategies

    def _generate_reroute_strategies(
        self, pcb: PCB, failure: FailureAnalysis
    ) -> list[ResolutionStrategy]:
        """Generate net rerouting strategies.

        Suggests rerouting blocking traces to free up the path.
        """
        strategies: list[ResolutionStrategy] = []

        # Find blocking traces that could be rerouted
        blocking_nets: set[str] = set()
        for el in failure.blocking_elements:
            if el.type == "trace" and el.net and el.net != failure.net:
                blocking_nets.add(el.net)

        for net in list(blocking_nets)[:3]:  # Limit to top 3
            side_effects = [
                SideEffect(
                    description=f"Rerouting {net} may affect signal timing",
                    severity="warning",
                    mitigatable=True,
                ),
                SideEffect(
                    description="Trace length may change",
                    severity="info",
                    mitigatable=True,
                ),
            ]

            strategies.append(
                ResolutionStrategy(
                    type=StrategyType.REROUTE_NET,
                    difficulty=Difficulty.MEDIUM,
                    confidence=0.6,
                    actions=[
                        Action(
                            type="reroute",
                            target=net,
                            params={"avoid_area": failure.failure_area.to_dict()},
                        )
                    ],
                    side_effects=side_effects,
                    affected_components=[],
                    affected_nets=[net],
                    estimated_improvement=0.65,
                )
            )

        return strategies

    def _generate_spread_strategies(
        self, pcb: PCB, failure: FailureAnalysis
    ) -> list[ResolutionStrategy]:
        """Generate component spreading strategies for congestion relief.

        When an area is congested, suggests moving multiple components
        outward to reduce density.
        """
        strategies: list[ResolutionStrategy] = []

        # Find all components in the congested area
        components_in_area: list[str] = []
        for el in failure.blocking_elements:
            if el.type == "component" and el.ref and el.movable:
                components_in_area.append(el.ref)

        if len(components_in_area) < 2:
            return strategies

        # Generate spread actions
        center = failure.failure_area.center
        actions: list[Action] = []
        affected_nets: list[str] = []

        for ref in components_in_area[:4]:  # Limit to 4 components
            fp = self._find_footprint(pcb, ref)
            if fp is None:
                continue

            # Calculate spread vector (away from center)
            dx = fp.position[0] - center[0]
            dy = fp.position[1] - center[1]
            dist = math.sqrt(dx * dx + dy * dy)

            if dist < 0.1:
                # Component at center, spread in arbitrary direction
                dx, dy = 1.0, 0.0
                dist = 1.0

            # Normalize and scale
            spread_dist = 2.0  # mm
            new_x = fp.position[0] + (dx / dist) * spread_dist
            new_y = fp.position[1] + (dy / dist) * spread_dist

            actions.append(
                Action(
                    type="move",
                    target=ref,
                    params={"x": new_x, "y": new_y},
                )
            )

            # Collect affected nets
            affected_nets.extend(self._get_component_nets(pcb, ref))

        if actions:
            strategies.append(
                ResolutionStrategy(
                    type=StrategyType.MOVE_MULTIPLE,
                    difficulty=Difficulty.HARD,
                    confidence=0.7,
                    actions=actions,
                    side_effects=[
                        SideEffect(
                            description=f"Moving {len(actions)} components will require rerouting",
                            severity="warning",
                            mitigatable=True,
                        ),
                        SideEffect(
                            description="Board area usage may increase",
                            severity="info",
                            mitigatable=False,
                        ),
                    ],
                    affected_components=components_in_area[: len(actions)],
                    affected_nets=list(set(affected_nets)),
                    estimated_improvement=0.8,
                )
            )

        return strategies

    def _generate_layer_change_strategies(
        self, pcb: PCB, failure: FailureAnalysis
    ) -> list[ResolutionStrategy]:
        """Generate layer change strategies.

        Suggests routing on a different layer when the current
        layer has conflicts.
        """
        strategies: list[ResolutionStrategy] = []

        if failure.net is None:
            return strategies

        # Get available copper layers
        copper_layers = self._get_copper_layers(pcb)

        for layer in copper_layers[:2]:  # Suggest top 2 alternatives
            strategies.append(
                ResolutionStrategy(
                    type=StrategyType.CHANGE_LAYER,
                    difficulty=Difficulty.EASY,
                    confidence=0.75,
                    actions=[
                        Action(
                            type="change_layer",
                            target=failure.net,
                            params={"layer": layer},
                        )
                    ],
                    side_effects=[
                        SideEffect(
                            description=f"Route will use {layer} instead",
                            severity="info",
                            mitigatable=False,
                        ),
                    ],
                    affected_components=[],
                    affected_nets=[failure.net],
                    estimated_improvement=0.7,
                )
            )

        return strategies

    def _generate_manual_strategy(self, failure: FailureAnalysis) -> ResolutionStrategy:
        """Generate manual intervention strategy as fallback.

        Used when automated strategies are not available or
        when the failure requires human judgment.
        """
        description = {
            FailureCause.KEEPOUT: "Routing crosses keepout zone; review keepout boundaries",
            FailureCause.LENGTH_CONSTRAINT: "Cannot meet length constraints; review requirements",
            FailureCause.DIFFERENTIAL_PAIR: "Cannot maintain differential pair spacing; review layout",
            FailureCause.PIN_ACCESS: "Cannot access pin; consider component rotation or repositioning",
        }.get(
            failure.root_cause,
            "Complex failure requiring manual review and intervention",
        )

        return ResolutionStrategy(
            type=StrategyType.MANUAL_INTERVENTION,
            difficulty=Difficulty.EXPERT,
            confidence=0.5,
            actions=[
                Action(
                    type="manual",
                    target=failure.net or "board",
                    params={
                        "location": {
                            "x": failure.failure_location[0],
                            "y": failure.failure_location[1],
                        },
                        "description": description,
                    },
                )
            ],
            side_effects=[
                SideEffect(
                    description="Requires human expertise to resolve",
                    severity="warning",
                    mitigatable=False,
                ),
            ],
            affected_components=[el.ref for el in failure.blocking_elements if el.ref],
            affected_nets=[failure.net] if failure.net else [],
            estimated_improvement=0.5,
        )

    def _find_move_candidates(
        self, pcb: PCB, blocker: BlockingElement, failure_area: Rectangle
    ) -> list[dict[str, Any]]:
        """Find candidate positions for moving a component.

        Looks for positions that:
        1. Clear the failure area
        2. Don't create new conflicts
        3. Maintain functional groupings
        """
        candidates: list[dict[str, Any]] = []

        if blocker.ref is None:
            return candidates

        fp = self._find_footprint(pcb, blocker.ref)
        if fp is None:
            return candidates

        # Get component dimensions
        comp_width = blocker.bounds.width
        comp_height = blocker.bounds.height

        # Generate candidate positions around the failure area
        offsets = [
            (failure_area.width + comp_width, 0),  # Right
            (-(failure_area.width + comp_width), 0),  # Left
            (0, failure_area.height + comp_height),  # Below
            (0, -(failure_area.height + comp_height)),  # Above
            (failure_area.width + comp_width, failure_area.height + comp_height),  # Diagonal
        ]

        for dx, dy in offsets:
            new_x = fp.position[0] + dx
            new_y = fp.position[1] + dy

            # Check if position is valid (simple bounds check)
            confidence = 0.85 if abs(dx) + abs(dy) < 10 else 0.7
            improvement = 0.9 if not failure_area.contains_point(new_x, new_y) else 0.5

            candidates.append(
                {
                    "position": (new_x, new_y),
                    "confidence": confidence,
                    "improvement": improvement,
                }
            )

        # Sort by improvement
        candidates.sort(key=lambda c: c["improvement"], reverse=True)
        return candidates

    def _find_via_positions(
        self, pcb: PCB, failure: FailureAnalysis
    ) -> list[tuple[tuple[float, float], str]]:
        """Find viable via positions.

        Returns positions just before the failure point where
        a via could be placed to change layers.
        """
        positions: list[tuple[tuple[float, float], str]] = []

        # Calculate position before failure point
        fx, fy = failure.failure_location
        area = failure.failure_area

        # Suggest vias at corners of failure area
        via_candidates = [
            (area.min_x - 1.0, fy),
            (area.max_x + 1.0, fy),
            (fx, area.min_y - 1.0),
            (fx, area.max_y + 1.0),
        ]

        # Get alternate layers
        copper_layers = self._get_copper_layers(pcb)

        for pos in via_candidates[:2]:
            for layer in copper_layers[:1]:
                positions.append((pos, layer))

        return positions

    def _analyze_move_side_effects(
        self, pcb: PCB, ref: str, new_pos: tuple[float, float]
    ) -> list[SideEffect]:
        """Analyze side effects of moving a component.

        Checks for:
        - Breaking functional groups
        - Degrading decoupling effectiveness
        - Requiring trace rerouting
        """
        effects: list[SideEffect] = []
        fp = self._find_footprint(pcb, ref)

        if fp is None:
            return effects

        # Check if component is a bypass capacitor
        if self._is_bypass_cap(ref):
            ic_ref = self._find_decoupled_ic(pcb, ref)
            if ic_ref:
                ic_fp = self._find_footprint(pcb, ic_ref)
                if ic_fp:
                    new_distance = math.sqrt(
                        (new_pos[0] - ic_fp.position[0]) ** 2
                        + (new_pos[1] - ic_fp.position[1]) ** 2
                    )
                    if new_distance > self.BYPASS_CAP_DISTANCE_THRESHOLD:
                        effects.append(
                            SideEffect(
                                description=f"Increases distance from {ic_ref}, may affect decoupling",
                                severity="warning",
                                mitigatable=False,
                            )
                        )

        # Check if part of a functional group
        cluster = self._get_component_cluster(pcb, ref)
        if cluster:
            # Check if move breaks cluster
            cluster_center = self._compute_cluster_center(pcb, cluster)
            if cluster_center:
                dist_to_cluster = math.sqrt(
                    (new_pos[0] - cluster_center[0]) ** 2 + (new_pos[1] - cluster_center[1]) ** 2
                )
                if dist_to_cluster > self.CLUSTER_DISTANCE_THRESHOLD:
                    effects.append(
                        SideEffect(
                            description=f"Separates {ref} from functional group",
                            severity="warning",
                            mitigatable=True,
                        )
                    )

        # Always add rerouting side effect for component moves
        effects.append(
            SideEffect(
                description="May require rerouting connected traces",
                severity="info",
                mitigatable=True,
            )
        )

        return effects

    def _assess_move_difficulty(self, side_effects: list[SideEffect]) -> Difficulty:
        """Assess difficulty based on side effects."""
        risk_count = sum(1 for e in side_effects if e.severity == "risk")
        warning_count = sum(1 for e in side_effects if e.severity == "warning")

        if risk_count > 0:
            return Difficulty.HARD
        if warning_count >= 2:
            return Difficulty.MEDIUM
        if warning_count >= 1:
            return Difficulty.EASY
        return Difficulty.TRIVIAL

    def _rank_strategies(self, strategies: list[ResolutionStrategy]) -> list[ResolutionStrategy]:
        """Rank strategies by difficulty, confidence, and side effects.

        Strategies are sorted to prefer:
        1. Lower difficulty
        2. Higher confidence
        3. Fewer side effects
        """
        difficulty_order = {
            Difficulty.TRIVIAL: 0,
            Difficulty.EASY: 1,
            Difficulty.MEDIUM: 2,
            Difficulty.HARD: 3,
            Difficulty.EXPERT: 4,
        }

        def score(s: ResolutionStrategy) -> tuple[int, float, int]:
            return (
                difficulty_order[s.difficulty],
                -s.confidence,  # Higher confidence is better
                len(s.side_effects),
            )

        return sorted(strategies, key=score)

    # Helper methods

    def _find_footprint(self, pcb: PCB, ref: str) -> Any | None:
        """Find a footprint by reference designator."""
        for fp in pcb.footprints:
            if fp.reference == ref:
                return fp
        return None

    def _get_component_nets(self, pcb: PCB, ref: str) -> list[str]:
        """Get all nets connected to a component."""
        nets: set[str] = set()
        fp = self._find_footprint(pcb, ref)

        if fp:
            for pad in fp.pads:
                if pad.net_name:
                    nets.add(pad.net_name)

        return list(nets)

    def _get_copper_layers(self, pcb: PCB) -> list[str]:
        """Get list of copper layer names."""
        layers = []
        for layer in pcb.layers.values():
            if layer.type in ("signal", "power") and "Cu" in layer.name:
                layers.append(layer.name)
        return layers if layers else ["F.Cu", "B.Cu"]

    def _is_bypass_cap(self, ref: str) -> bool:
        """Check if a reference designator is likely a bypass capacitor."""
        return ref.upper().startswith("C")

    def _find_decoupled_ic(self, pcb: PCB, cap_ref: str) -> str | None:
        """Find the IC that a bypass cap is decoupling.

        Looks for the nearest IC (U prefix) sharing power/ground nets.
        """
        cap_fp = self._find_footprint(pcb, cap_ref)
        if cap_fp is None:
            return None

        # Get nets on the capacitor
        cap_nets = set()
        for pad in cap_fp.pads:
            if pad.net_name:
                cap_nets.add(pad.net_name)

        # Find nearest IC sharing a net
        nearest_ic: str | None = None
        nearest_dist = float("inf")

        for fp in pcb.footprints:
            if not fp.reference.upper().startswith("U"):
                continue

            # Check if shares a power/ground net
            for pad in fp.pads:
                if pad.net_name in cap_nets:
                    dist = math.sqrt(
                        (fp.position[0] - cap_fp.position[0]) ** 2
                        + (fp.position[1] - cap_fp.position[1]) ** 2
                    )
                    if dist < nearest_dist:
                        nearest_dist = dist
                        nearest_ic = fp.reference
                    break

        return nearest_ic

    def _get_component_cluster(self, pcb: PCB, ref: str) -> list[str] | None:
        """Get the functional cluster a component belongs to.

        Returns None if component doesn't belong to a cluster.
        Simple heuristic: components within CLUSTER_DISTANCE_THRESHOLD
        sharing common nets form a cluster.
        """
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return None

        # Get component nets
        component_nets = set(self._get_component_nets(pcb, ref))
        if not component_nets:
            return None

        # Find nearby components with shared nets
        cluster = [ref]
        for other_fp in pcb.footprints:
            if other_fp.reference == ref:
                continue

            # Check distance
            dist = math.sqrt(
                (other_fp.position[0] - fp.position[0]) ** 2
                + (other_fp.position[1] - fp.position[1]) ** 2
            )
            if dist > self.CLUSTER_DISTANCE_THRESHOLD:
                continue

            # Check for shared nets
            other_nets = set()
            for pad in other_fp.pads:
                if pad.net_name:
                    other_nets.add(pad.net_name)

            if component_nets & other_nets:
                cluster.append(other_fp.reference)

        return cluster if len(cluster) > 1 else None

    def _compute_cluster_center(self, pcb: PCB, cluster: list[str]) -> tuple[float, float] | None:
        """Compute the center point of a component cluster."""
        if not cluster:
            return None

        x_sum = 0.0
        y_sum = 0.0
        count = 0

        for ref in cluster:
            fp = self._find_footprint(pcb, ref)
            if fp:
                x_sum += fp.position[0]
                y_sum += fp.position[1]
                count += 1

        if count == 0:
            return None

        return (x_sum / count, y_sum / count)
