"""
Force-directed placement optimizer.

Provides the PlacementOptimizer class that uses a physics simulation to
optimize component placement on a PCB. Components repel each other via
electrostatic-like charges while net connections act as springs pulling
connected pins together.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from kicad_tools.optim.components import Component, FunctionalCluster, Keepout, Pin, Spring
from kicad_tools.optim.config import PlacementConfig
from kicad_tools.optim.constraints import (
    ConstraintType,
    ConstraintViolation,
    GroupingConstraint,
    validate_grouping_constraints,
)
from kicad_tools.optim.edge_placement import (
    BoardEdges,
    EdgeConstraint,
    compute_edge_force,
    detect_edge_components,
)
from kicad_tools.optim.geometry import Polygon, Vector2D
from kicad_tools.optim.thermal import (
    ThermalClass,
    ThermalConfig,
    ThermalConstraint,
    ThermalProperties,
    classify_thermal_properties,
    detect_thermal_constraints,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = ["PlacementOptimizer"]


class PlacementOptimizer:
    """
    Component placement optimizer using force-directed simulation.

    Uses a physics model where:
    - Board edges and component outlines have constant linear charge density
    - Same-sign charges repel (keeps components apart and on board)
    - Net connections are modeled as springs (pulls connected pins together)

    The simulation runs until equilibrium or max iterations reached.
    """

    def __init__(
        self,
        board_outline: Polygon,
        config: PlacementConfig | None = None,
    ):
        """
        Initialize the optimizer.

        Args:
            board_outline: Polygon defining the board boundary
            config: Optimization parameters
        """
        self.board_outline = board_outline
        self.config = config or PlacementConfig()
        self.components: list[Component] = []
        self.springs: list[Spring] = []
        self.keepouts: list[Keepout] = []
        self.clusters: list[FunctionalCluster] = []
        self.grouping_constraints: list[GroupingConstraint] = []
        self._component_map: dict[str, Component] = {}
        self.thermal_constraints: list[ThermalConstraint] = []
        self._thermal_props: dict[str, ThermalProperties] = {}

        # Edge constraints
        self.board_edges = BoardEdges.from_polygon(board_outline)
        self._edge_constraints: dict[str, EdgeConstraint] = {}

    @classmethod
    def from_pcb(
        cls,
        pcb: PCB,
        config: PlacementConfig | None = None,
        fixed_refs: list[str] | None = None,
        enable_clustering: bool | None = None,
        edge_detect: bool = False,
        edge_constraints: list[EdgeConstraint] | None = None,
    ) -> PlacementOptimizer:
        """
        Create optimizer from a loaded PCB.

        Args:
            pcb: Loaded PCB object
            config: Optimization parameters
            fixed_refs: List of reference designators for fixed components
                       (e.g., ["J1", "J2"] for connectors)
            enable_clustering: If True, detect and add functional clusters.
                              If None, uses config.cluster_enabled.
            edge_detect: If True, auto-detect edge components (connectors, etc.)
            edge_constraints: Manual list of edge constraints to apply
        """
        fixed_refs = set(fixed_refs or [])

        # Try to extract board outline from Edge.Cuts layer
        board = cls._extract_board_outline(pcb)

        if board is None:
            # Fall back to estimating from component positions
            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")

            for fp in pcb.footprints:
                x, y = fp.position
                min_x = min(min_x, x - 10)
                max_x = max(max_x, x + 10)
                min_y = min(min_y, y - 10)
                max_y = max(max_y, y + 10)

            if min_x == float("inf"):
                # No footprints, use default
                board = Polygon.rectangle(100, 100, 100, 80)
            else:
                board = Polygon.rectangle(
                    (min_x + max_x) / 2,
                    (min_y + max_y) / 2,
                    max_x - min_x,
                    max_y - min_y,
                )

        optimizer = cls(board, config)

        # Add components
        for fp in pcb.footprints:
            # Estimate component size from pads
            if fp.pads:
                pad_xs = [p.position[0] for p in fp.pads]
                pad_ys = [p.position[1] for p in fp.pads]
                width = max(pad_xs) - min(pad_xs) + 2.0
                height = max(pad_ys) - min(pad_ys) + 2.0
            else:
                width, height = 2.0, 2.0

            # Mark connectors, mounting holes, etc. as fixed
            is_fixed = fp.reference in fixed_refs
            # Auto-detect connectors and mounting holes
            if not is_fixed:
                ref_prefix = "".join(c for c in fp.reference if c.isalpha())
                if ref_prefix in ("J", "H", "MH"):  # Connectors and mounting holes
                    is_fixed = True

            comp = Component(
                ref=fp.reference,
                x=fp.position[0],
                y=fp.position[1],
                rotation=fp.rotation,
                width=max(width, 1.0),
                height=max(height, 1.0),
                fixed=is_fixed,
                pins=[
                    Pin(
                        number=p.number,
                        x=fp.position[0] + p.position[0],
                        y=fp.position[1] + p.position[1],
                        net=p.net_number,
                        net_name=p.net_name,
                    )
                    for p in fp.pads
                ],
            )
            optimizer.add_component(comp)

        # Create springs for nets
        optimizer.create_springs_from_nets()

        # Detect and add functional clusters if enabled
        use_clustering = (
            enable_clustering if enable_clustering is not None else optimizer.config.cluster_enabled
        )
        if use_clustering:
            from kicad_tools.optim.clustering import detect_functional_clusters

            clusters = detect_functional_clusters(optimizer.components)
            for cluster in clusters:
                optimizer.add_cluster(cluster)

        # Apply edge constraints
        all_constraints: list[EdgeConstraint] = []

        if edge_detect:
            all_constraints.extend(detect_edge_components(pcb))

        if edge_constraints:
            all_constraints.extend(edge_constraints)

        for constraint in all_constraints:
            optimizer.add_edge_constraint(constraint)

        # Apply thermal properties if enabled
        if config and config.thermal_enabled:
            optimizer.apply_thermal_properties(pcb)

        return optimizer

    @staticmethod
    def _extract_board_outline(pcb: PCB) -> Polygon | None:
        """
        Extract board outline from Edge.Cuts layer.

        Attempts to find rectangular outline from gr_rect or gr_line elements.
        Returns None if no outline found.
        """
        # Access the raw SExp to find Edge.Cuts graphics
        sexp = pcb._sexp

        # Look for gr_rect on Edge.Cuts (simple rectangular boards)
        for child in sexp.iter_children():
            if child.tag == "gr_rect":
                layer = child.find("layer")
                if layer and layer.get_string(0) == "Edge.Cuts":
                    start = child.find("start")
                    end = child.find("end")
                    if start and end:
                        x1 = start.get_float(0) or 0.0
                        y1 = start.get_float(1) or 0.0
                        x2 = end.get_float(0) or 0.0
                        y2 = end.get_float(1) or 0.0
                        return Polygon(
                            vertices=[
                                Vector2D(x1, y1),
                                Vector2D(x2, y1),
                                Vector2D(x2, y2),
                                Vector2D(x1, y2),
                            ]
                        )

        # Look for gr_line elements on Edge.Cuts to build outline
        edge_lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for child in sexp.iter_children():
            if child.tag == "gr_line":
                layer = child.find("layer")
                if layer and layer.get_string(0) == "Edge.Cuts":
                    start = child.find("start")
                    end = child.find("end")
                    if start and end:
                        x1 = start.get_float(0) or 0.0
                        y1 = start.get_float(1) or 0.0
                        x2 = end.get_float(0) or 0.0
                        y2 = end.get_float(1) or 0.0
                        edge_lines.append(((x1, y1), (x2, y2)))

        if len(edge_lines) >= 4:
            # Try to chain the lines into a closed polygon
            vertices = PlacementOptimizer._chain_lines_to_polygon(edge_lines)
            if vertices:
                return Polygon(vertices=[Vector2D(x, y) for x, y in vertices])

        return None

    @staticmethod
    def _chain_lines_to_polygon(
        lines: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> list[tuple[float, float]] | None:
        """Chain line segments into a closed polygon."""
        if not lines:
            return None

        tolerance = 0.01  # mm tolerance for point matching
        vertices = []
        used = [False] * len(lines)

        # Start with first line
        vertices.append(lines[0][0])
        current_end = lines[0][1]
        used[0] = True

        while True:
            found = False
            for i, (start, end) in enumerate(lines):
                if used[i]:
                    continue

                # Check if this line continues from current_end
                if (
                    abs(start[0] - current_end[0]) < tolerance
                    and abs(start[1] - current_end[1]) < tolerance
                ):
                    vertices.append(start)
                    current_end = end
                    used[i] = True
                    found = True
                    break
                elif (
                    abs(end[0] - current_end[0]) < tolerance
                    and abs(end[1] - current_end[1]) < tolerance
                ):
                    vertices.append(end)
                    current_end = start
                    used[i] = True
                    found = True
                    break

            if not found:
                break

        # Check if polygon closes
        if (
            vertices
            and abs(vertices[0][0] - current_end[0]) < tolerance
            and abs(vertices[0][1] - current_end[1]) < tolerance
        ):
            return vertices

        # Polygon didn't close, return bounding box
        if lines:
            all_x = [p[0] for line in lines for p in line]
            all_y = [p[1] for line in lines for p in line]
            return [
                (min(all_x), min(all_y)),
                (max(all_x), min(all_y)),
                (max(all_x), max(all_y)),
                (min(all_x), max(all_y)),
            ]

        return None

    def add_component(self, comp: Component):
        """Add a component to the optimizer."""
        self.components.append(comp)
        self._component_map[comp.ref] = comp

    def get_component(self, ref: str) -> Component | None:
        """Get component by reference."""
        return self._component_map.get(ref)

    def add_grouping_constraint(self, constraint: GroupingConstraint) -> None:
        """
        Add a grouping constraint to the optimizer.

        Constraints define spatial relationships between component groups
        and are enforced as soft penalty forces during optimization.

        Args:
            constraint: Grouping constraint to add
        """
        self.grouping_constraints.append(constraint)

    def add_grouping_constraints(self, constraints: list[GroupingConstraint]) -> None:
        """
        Add multiple grouping constraints to the optimizer.

        Args:
            constraints: List of grouping constraints to add
        """
        self.grouping_constraints.extend(constraints)

    def validate_constraints(self) -> list[ConstraintViolation]:
        """
        Check if current placement satisfies all grouping constraints.

        Returns:
            List of constraint violations (empty if all satisfied)
        """
        return validate_grouping_constraints(self.components, self.grouping_constraints)

    def add_keepout(
        self, outline: Polygon, charge_multiplier: float = 10.0, name: str = ""
    ) -> Keepout:
        """
        Add a keepout zone that repels components.

        Args:
            outline: Polygon defining the keepout area
            charge_multiplier: Repulsion strength relative to normal edges
            name: Optional name for the keepout

        Returns:
            The created Keepout object
        """
        keepout = Keepout(outline=outline, charge_multiplier=charge_multiplier, name=name)
        self.keepouts.append(keepout)
        return keepout

    def add_keepout_circle(
        self, x: float, y: float, radius: float, charge_multiplier: float = 10.0, name: str = ""
    ) -> Keepout:
        """
        Add a circular keepout zone (e.g., for mounting holes).

        Args:
            x, y: Center position
            radius: Keepout radius in mm
            charge_multiplier: Repulsion strength
            name: Optional name

        Returns:
            The created Keepout object
        """
        outline = Polygon.circle(x, y, radius)
        return self.add_keepout(outline, charge_multiplier, name)

    def add_edge_constraint(self, constraint: EdgeConstraint) -> None:
        """
        Add an edge constraint for a component.

        Edge constraints keep components at board edges during optimization.
        Useful for connectors, mounting holes, and other edge-accessible components.

        Args:
            constraint: EdgeConstraint specifying component and edge behavior
        """
        self._edge_constraints[constraint.reference] = constraint

        # Also set on the component if it exists
        comp = self._component_map.get(constraint.reference)
        if comp:
            comp.edge_constraint = constraint

    def add_edge_constraints(self, constraints: list[EdgeConstraint]) -> None:
        """
        Add multiple edge constraints.

        Args:
            constraints: List of EdgeConstraint objects
        """
        for constraint in constraints:
            self.add_edge_constraint(constraint)

    def get_edge_constraint(self, ref: str) -> EdgeConstraint | None:
        """Get edge constraint for a component by reference."""
        return self._edge_constraints.get(ref)

    @property
    def edge_constrained_components(self) -> list[Component]:
        """Get list of components with edge constraints."""
        return [comp for comp in self.components if comp.ref in self._edge_constraints]

    def create_springs_from_nets(self):
        """Create springs connecting all pins on the same net."""
        # Group pins by net
        net_pins: dict[int, list[tuple[str, Pin]]] = {}
        for comp in self.components:
            for pin in comp.pins:
                if pin.net > 0:  # Skip unconnected pins
                    if pin.net not in net_pins:
                        net_pins[pin.net] = []
                    net_pins[pin.net].append((comp.ref, pin))

        # Create springs between all pairs (star topology)
        # Could also use minimum spanning tree for fewer springs
        for net, pins in net_pins.items():
            if len(pins) < 2:
                continue

            # Determine spring stiffness based on net type
            net_name = pins[0][1].net_name
            if self._is_power_net(net_name):
                stiffness = self.config.power_net_stiffness
            elif self._is_clock_net(net_name):
                stiffness = self.config.clock_net_stiffness
            else:
                stiffness = self.config.spring_stiffness

            # Create spring from first pin to all others (star topology)
            ref0, pin0 = pins[0]
            for ref1, pin1 in pins[1:]:
                spring = Spring(
                    comp1_ref=ref0,
                    pin1_num=pin0.number,
                    comp2_ref=ref1,
                    pin2_num=pin1.number,
                    stiffness=stiffness,
                    rest_length=0.0,
                    net=net,
                    net_name=net_name,
                )
                self.springs.append(spring)

    def _is_power_net(self, name: str) -> bool:
        """Check if net is a power net."""
        name_lower = name.lower()
        return any(
            p in name_lower
            for p in ["vcc", "vdd", "gnd", "+3", "+5", "+12", "-12", "pwr", "v+", "v-"]
        )

    def _is_clock_net(self, name: str) -> bool:
        """Check if net is a clock net."""
        name_lower = name.lower()
        return any(p in name_lower for p in ["clk", "clock", "mclk", "sclk", "bclk", "xtal"])

    def add_cluster(self, cluster: FunctionalCluster):
        """
        Add a functional cluster to the optimizer.

        Creates strong springs between the cluster anchor and all member
        components to keep them close together during optimization.

        Args:
            cluster: FunctionalCluster defining the group
        """
        self.clusters.append(cluster)
        self._create_cluster_springs(cluster)

    def _create_cluster_springs(self, cluster: FunctionalCluster):
        """
        Create springs to enforce cluster proximity constraints.

        Creates strong springs from anchor component center to each member
        component center. These springs have higher stiffness than normal
        net connections to ensure cluster members stay close together.

        Args:
            cluster: FunctionalCluster to create springs for
        """
        anchor_comp = self._component_map.get(cluster.anchor)
        if not anchor_comp:
            return

        # Find the anchor pin to use (if specified) or use component center
        anchor_pin = None
        if cluster.anchor_pin:
            anchor_pin = next(
                (p for p in anchor_comp.pins if p.number == cluster.anchor_pin),
                None,
            )

        # If no specific anchor pin, use the first pin as proxy for center
        if anchor_pin is None and anchor_comp.pins:
            anchor_pin = anchor_comp.pins[0]

        if anchor_pin is None:
            return

        # Create strong springs from anchor to each member
        for member_ref in cluster.members:
            member_comp = self._component_map.get(member_ref)
            if not member_comp or not member_comp.pins:
                continue

            # Use first pin of member component
            member_pin = member_comp.pins[0]

            # Create cluster spring with high stiffness
            spring = Spring(
                comp1_ref=cluster.anchor,
                pin1_num=anchor_pin.number,
                comp2_ref=member_ref,
                pin2_num=member_pin.number,
                stiffness=self.config.cluster_stiffness,
                rest_length=0.0,  # Want them as close as possible
                net=-1,  # Special marker for cluster springs
                net_name=f"_cluster_{cluster.cluster_type.value}",
            )
            self.springs.append(spring)

    def get_clusters(self) -> list[FunctionalCluster]:
        """Get all registered functional clusters."""
        return list(self.clusters)

    def validate_cluster_distances(self) -> list[tuple[str, str, float, float]]:
        """
        Check if cluster members are within their max distance constraints.

        Returns:
            List of (cluster_anchor, member_ref, actual_distance, max_distance)
            tuples for violations.
        """
        violations = []

        for cluster in self.clusters:
            anchor_comp = self._component_map.get(cluster.anchor)
            if not anchor_comp:
                continue

            anchor_pos = Vector2D(anchor_comp.x, anchor_comp.y)

            for member_ref in cluster.members:
                member_comp = self._component_map.get(member_ref)
                if not member_comp:
                    continue

                member_pos = Vector2D(member_comp.x, member_comp.y)
                distance = (member_pos - anchor_pos).magnitude()

                if distance > cluster.max_distance_mm:
                    violations.append(
                        (cluster.anchor, member_ref, distance, cluster.max_distance_mm)
                    )

        return violations

    def apply_thermal_properties(self, pcb: PCB):
        """
        Apply thermal classification to all components.

        Classifies components as heat sources, heat-sensitive, or neutral,
        and detects thermal constraints.

        Args:
            pcb: The PCB object used to create this optimizer
        """
        # Classify components
        self._thermal_props = classify_thermal_properties(pcb)

        # Apply to components
        for comp in self.components:
            if comp.ref in self._thermal_props:
                comp.thermal_properties = self._thermal_props[comp.ref]

        # Detect constraints
        thermal_config = ThermalConfig(
            heat_source_separation_mm=self.config.thermal_separation_mm,
            edge_preference_max_mm=self.config.thermal_edge_preference_mm,
            thermal_repulsion_strength=self.config.thermal_repulsion_strength,
            edge_attraction_strength=self.config.thermal_edge_attraction,
        )
        self.thermal_constraints = detect_thermal_constraints(
            pcb, self._thermal_props, thermal_config
        )

    def get_heat_sources(self) -> list[Component]:
        """Get all components classified as heat sources."""
        return [
            comp
            for comp in self.components
            if comp.thermal_properties
            and comp.thermal_properties.thermal_class == ThermalClass.HEAT_SOURCE
        ]

    def get_heat_sensitive(self) -> list[Component]:
        """Get all components classified as heat-sensitive."""
        return [
            comp
            for comp in self.components
            if comp.thermal_properties
            and comp.thermal_properties.thermal_class == ThermalClass.HEAT_SENSITIVE
        ]

    def compute_thermal_forces(self) -> dict[str, Vector2D]:
        """
        Compute thermal-related forces on all components.

        Includes:
        - Repulsion between heat sources and heat-sensitive components
        - Edge attraction for heat sources (they should be near board edges)

        Returns:
            Dictionary mapping component reference to force vector
        """
        forces: dict[str, Vector2D] = {comp.ref: Vector2D(0.0, 0.0) for comp in self.components}

        if not self.config.thermal_enabled:
            return forces

        heat_sources = self.get_heat_sources()
        heat_sensitive = self.get_heat_sensitive()

        # 1. Repulsion between heat sources and sensitive components
        for source in heat_sources:
            if source.fixed:
                continue
            source_pos = source.position()

            for sensitive in heat_sensitive:
                sensitive_pos = sensitive.position()
                delta = source_pos - sensitive_pos
                distance = delta.magnitude()

                # Clamp minimum distance
                distance = max(distance, self.config.min_distance)

                # Strong repulsion when closer than thermal_separation_mm
                if distance < self.config.thermal_separation_mm:
                    # Force magnitude inversely proportional to distance^2
                    force_mag = self.config.thermal_repulsion_strength / (distance * distance)
                    force = delta.normalized() * force_mag

                    # Apply to source (push away from sensitive)
                    forces[source.ref] = forces[source.ref] + force

                    # Also push sensitive away if not fixed
                    if not sensitive.fixed:
                        forces[sensitive.ref] = forces[sensitive.ref] - force

        # 2. Edge attraction for heat sources
        for source in heat_sources:
            if source.fixed:
                continue
            source_pos = source.position()

            # Find nearest edge and distance
            min_edge_dist = float("inf")
            nearest_edge_point = source_pos

            for edge_start, edge_end in self.board_outline.edges():
                # Project point onto edge
                edge = edge_end - edge_start
                edge_len = edge.magnitude()
                if edge_len < 1e-10:
                    continue

                to_point = source_pos - edge_start
                t = to_point.dot(edge) / (edge_len * edge_len)
                t = max(0.0, min(1.0, t))

                closest = edge_start + edge * t
                dist = (source_pos - closest).magnitude()

                if dist < min_edge_dist:
                    min_edge_dist = dist
                    nearest_edge_point = closest

            # Apply attraction force toward edge if far from edge
            if min_edge_dist > self.config.thermal_edge_preference_mm:
                # Force toward nearest edge point
                to_edge = nearest_edge_point - source_pos
                distance = to_edge.magnitude()
                if distance > 0.1:
                    # Gentle attraction, stronger when farther
                    excess_dist = min_edge_dist - self.config.thermal_edge_preference_mm
                    force_mag = self.config.thermal_edge_attraction * excess_dist
                    force = to_edge.normalized() * force_mag
                    forces[source.ref] = forces[source.ref] + force

        return forces

    def compute_edge_to_point_force(
        self, point: Vector2D, edge_start: Vector2D, edge_end: Vector2D, charge_density: float
    ) -> Vector2D:
        """
        Compute repulsion force on a point from a charged line segment.

        Uses linear charge density model with 1/r falloff.
        For a line segment with charge density lambda, integrates the field.

        Args:
            point: Point being repelled
            edge_start: Start of charged edge
            edge_end: End of charged edge
            charge_density: Linear charge density lambda

        Returns:
            Force vector on the point
        """
        edge = edge_end - edge_start
        edge_len = edge.magnitude()
        if edge_len < 1e-10:
            return Vector2D(0.0, 0.0)

        # Vector from edge start to point
        to_point = point - edge_start

        # Project point onto edge line
        t = to_point.dot(edge) / (edge_len * edge_len)
        t = max(0.0, min(1.0, t))  # Clamp to edge

        # Closest point on edge
        closest = edge_start + edge * t

        # Vector from closest point to test point
        displacement = point - closest
        distance = displacement.magnitude()

        # Clamp minimum distance to prevent singularity
        distance = max(distance, self.config.min_distance)

        # Force magnitude: lambda * L / r (total charge / distance)
        force_mag = charge_density * edge_len / distance

        # Force direction: away from edge
        return displacement.normalized() * force_mag

    def compute_edge_to_edge_force(
        self,
        edge1_start: Vector2D,
        edge1_end: Vector2D,
        edge2_start: Vector2D,
        edge2_end: Vector2D,
        num_samples: int = 5,
    ) -> tuple[Vector2D, float]:
        """
        Compute repulsion force and torque between two charged edges.

        Discretizes edge1 into sample points and computes force from edge2
        on each sample. Returns net force and torque about edge1's center.

        Args:
            edge1_start, edge1_end: First edge (receives force)
            edge2_start, edge2_end: Second edge (source of field)
            num_samples: Number of sample points on edge1

        Returns:
            Tuple of (net force on edge1, torque about edge1 center)
        """
        edge1 = edge1_end - edge1_start
        edge1_len = edge1.magnitude()
        if edge1_len < 1e-10:
            return Vector2D(0.0, 0.0), 0.0

        edge1_center = (edge1_start + edge1_end) * 0.5
        total_force = Vector2D(0.0, 0.0)
        total_torque = 0.0

        # Sample points along edge1
        for i in range(num_samples):
            t = (i + 0.5) / num_samples
            sample_point = edge1_start + edge1 * t

            # Force on this sample point from edge2
            # Use charge density scaled by sample fraction
            sample_charge = self.config.charge_density * edge1_len / num_samples
            force = self.compute_edge_to_point_force(
                sample_point, edge2_start, edge2_end, sample_charge
            )

            total_force = total_force + force

            # Torque: r x F where r is from edge1 center to sample point
            r = sample_point - edge1_center
            torque = r.cross(force)
            total_torque += torque

        return total_force, total_torque

    def compute_charge_force(
        self, point: Vector2D, edge_start: Vector2D, edge_end: Vector2D
    ) -> Vector2D:
        """
        Compute repulsion force on a point from a charged line segment.

        Convenience wrapper for compute_edge_to_point_force using default charge density.
        """
        return self.compute_edge_to_point_force(
            point, edge_start, edge_end, self.config.charge_density
        )

    def compute_spring_force(self, spring: Spring) -> tuple[Vector2D, Vector2D]:
        """
        Compute spring forces between two pins.

        Uses Hooke's law: F = -k(x - x0)

        Args:
            spring: Spring connecting two pins

        Returns:
            Tuple of (force on comp1, force on comp2)
        """
        comp1 = self._component_map.get(spring.comp1_ref)
        comp2 = self._component_map.get(spring.comp2_ref)

        if not comp1 or not comp2:
            return Vector2D(0.0, 0.0), Vector2D(0.0, 0.0)

        # Find pins
        pin1 = next((p for p in comp1.pins if p.number == spring.pin1_num), None)
        pin2 = next((p for p in comp2.pins if p.number == spring.pin2_num), None)

        if not pin1 or not pin2:
            return Vector2D(0.0, 0.0), Vector2D(0.0, 0.0)

        # Vector from pin1 to pin2
        p1 = Vector2D(pin1.x, pin1.y)
        p2 = Vector2D(pin2.x, pin2.y)
        delta = p2 - p1
        distance = delta.magnitude()

        if distance < 1e-10:
            return Vector2D(0.0, 0.0), Vector2D(0.0, 0.0)

        # Spring extension
        extension = distance - spring.rest_length

        # Force magnitude (positive = attraction when extended)
        force_mag = spring.stiffness * extension

        # Direction from p1 to p2
        direction = delta.normalized()

        # Force on comp1 is toward comp2 (pulls together)
        force1 = direction * force_mag
        force2 = -force1

        return force1, force2

    def compute_constraint_forces(
        self,
        constraint_stiffness: float = 50.0,
    ) -> dict[str, Vector2D]:
        """
        Compute penalty forces from grouping constraints.

        Constraints act as soft springs that pull components toward
        satisfying the constraint conditions.

        Args:
            constraint_stiffness: Stiffness of constraint penalty springs

        Returns:
            Dict mapping component ref to constraint penalty force
        """
        forces: dict[str, Vector2D] = {comp.ref: Vector2D(0.0, 0.0) for comp in self.components}

        if not self.grouping_constraints:
            return forces

        all_refs = list(self._component_map.keys())

        for group in self.grouping_constraints:
            members = group.get_resolved_members(all_refs)
            member_comps = [
                self._component_map[ref] for ref in members if ref in self._component_map
            ]

            if len(member_comps) < 2:
                continue

            for constraint in group.constraints:
                constraint_forces = self._compute_single_constraint_forces(
                    constraint, member_comps, constraint_stiffness
                )
                for ref, force in constraint_forces.items():
                    forces[ref] = forces[ref] + force

        return forces

    def _compute_single_constraint_forces(
        self,
        constraint,
        members: list[Component],
        stiffness: float,
    ) -> dict[str, Vector2D]:
        """Compute forces for a single constraint."""
        forces: dict[str, Vector2D] = {}

        if constraint.constraint_type == ConstraintType.MAX_DISTANCE:
            forces = self._constraint_force_max_distance(constraint, members, stiffness)
        elif constraint.constraint_type == ConstraintType.ALIGNMENT:
            forces = self._constraint_force_alignment(constraint, members, stiffness)
        elif constraint.constraint_type == ConstraintType.ORDERING:
            forces = self._constraint_force_ordering(constraint, members, stiffness)
        elif constraint.constraint_type == ConstraintType.WITHIN_BOX:
            forces = self._constraint_force_within_box(constraint, members, stiffness)
        elif constraint.constraint_type == ConstraintType.RELATIVE_POSITION:
            forces = self._constraint_force_relative_position(constraint, members, stiffness)

        return forces

    def _constraint_force_max_distance(
        self,
        constraint,
        members: list[Component],
        stiffness: float,
    ) -> dict[str, Vector2D]:
        """Compute penalty force for max_distance constraint."""
        forces = {}
        params = constraint.parameters
        anchor_ref = params["anchor"]
        radius = params["radius_mm"]

        anchor = self._component_map.get(anchor_ref)
        if not anchor:
            return forces

        for comp in members:
            if comp.ref == anchor_ref:
                continue

            dx = comp.x - anchor.x
            dy = comp.y - anchor.y
            dist = math.sqrt(dx * dx + dy * dy)

            if dist > radius and dist > 1e-10:
                # Pull toward anchor
                excess = dist - radius
                direction = Vector2D(-dx / dist, -dy / dist)
                forces[comp.ref] = direction * (stiffness * excess)

        return forces

    def _constraint_force_alignment(
        self,
        constraint,
        members: list[Component],
        stiffness: float,
    ) -> dict[str, Vector2D]:
        """Compute penalty force for alignment constraint."""
        forces = {}
        params = constraint.parameters
        axis = params["axis"]

        if len(members) < 2:
            return forces

        # Compute target position (mean)
        if axis == "horizontal":
            target = sum(comp.y for comp in members) / len(members)
            for comp in members:
                deviation = target - comp.y
                forces[comp.ref] = Vector2D(0.0, stiffness * deviation)
        else:  # vertical
            target = sum(comp.x for comp in members) / len(members)
            for comp in members:
                deviation = target - comp.x
                forces[comp.ref] = Vector2D(stiffness * deviation, 0.0)

        return forces

    def _constraint_force_ordering(
        self,
        constraint,
        members: list[Component],
        stiffness: float,
    ) -> dict[str, Vector2D]:
        """Compute penalty force for ordering constraint."""
        forces = {}
        params = constraint.parameters
        axis = params["axis"]
        expected_order = params["order"]

        comp_by_ref = {comp.ref: comp for comp in members}
        ordered_comps = [(ref, comp_by_ref[ref]) for ref in expected_order if ref in comp_by_ref]

        if len(ordered_comps) < 2:
            return forces

        # Check adjacent pairs and push to correct order
        for i in range(len(ordered_comps) - 1):
            ref1, comp1 = ordered_comps[i]
            ref2, comp2 = ordered_comps[i + 1]

            if axis == "horizontal":
                if comp1.x >= comp2.x:
                    # Push comp1 left, comp2 right
                    overlap = comp1.x - comp2.x + 1.0  # Small margin
                    forces[ref1] = forces.get(ref1, Vector2D(0, 0)) + Vector2D(
                        -stiffness * overlap / 2, 0
                    )
                    forces[ref2] = forces.get(ref2, Vector2D(0, 0)) + Vector2D(
                        stiffness * overlap / 2, 0
                    )
            else:  # vertical
                if comp1.y >= comp2.y:
                    overlap = comp1.y - comp2.y + 1.0
                    forces[ref1] = forces.get(ref1, Vector2D(0, 0)) + Vector2D(
                        0, -stiffness * overlap / 2
                    )
                    forces[ref2] = forces.get(ref2, Vector2D(0, 0)) + Vector2D(
                        0, stiffness * overlap / 2
                    )

        return forces

    def _constraint_force_within_box(
        self,
        constraint,
        members: list[Component],
        stiffness: float,
    ) -> dict[str, Vector2D]:
        """Compute penalty force for within_box constraint."""
        forces = {}
        params = constraint.parameters
        box_x = params["x"]
        box_y = params["y"]
        box_width = params["width"]
        box_height = params["height"]

        x_min, x_max = box_x, box_x + box_width
        y_min, y_max = box_y, box_y + box_height

        for comp in members:
            fx, fy = 0.0, 0.0

            if comp.x < x_min:
                fx = stiffness * (x_min - comp.x)
            elif comp.x > x_max:
                fx = stiffness * (x_max - comp.x)

            if comp.y < y_min:
                fy = stiffness * (y_min - comp.y)
            elif comp.y > y_max:
                fy = stiffness * (y_max - comp.y)

            if fx != 0.0 or fy != 0.0:
                forces[comp.ref] = Vector2D(fx, fy)

        return forces

    def _constraint_force_relative_position(
        self,
        constraint,
        members: list[Component],
        stiffness: float,
    ) -> dict[str, Vector2D]:
        """Compute penalty force for relative_position constraint."""
        forces = {}
        params = constraint.parameters
        reference_ref = params["reference"]
        dx = params["dx"]
        dy = params["dy"]

        reference = self._component_map.get(reference_ref)
        if not reference:
            return forces

        expected_x = reference.x + dx
        expected_y = reference.y + dy

        for comp in members:
            if comp.ref == reference_ref:
                continue

            # Pull toward expected position
            error_x = expected_x - comp.x
            error_y = expected_y - comp.y

            forces[comp.ref] = Vector2D(stiffness * error_x, stiffness * error_y)

        return forces

    def compute_boundary_force(self, point: Vector2D) -> Vector2D:
        """
        Compute force keeping point inside board boundary.

        Board edges have charge that repels components, keeping them inside.
        The repulsion is stronger when closer to edges.
        """
        total_force = Vector2D(0.0, 0.0)

        # Check if inside board
        inside = self.board_outline.contains_point(point)

        for edge_start, edge_end in self.board_outline.edges():
            # Compute repulsion from edge (points away from edge)
            force = self.compute_charge_force(point, edge_start, edge_end)

            # Scale by boundary charge strength
            scale = self.config.boundary_charge / self.config.charge_density

            if inside:
                # Inside board: edge charges repel component away from edges
                # This keeps components away from the board perimeter
                total_force = total_force + force * scale
            else:
                # Outside board: very strong force to push back inside
                # Invert the force direction to push toward board center
                total_force = total_force - force * scale * 10

        return total_force

    def compute_forces_and_torques(self) -> tuple[dict[str, Vector2D], dict[str, float]]:
        """
        Compute net forces and torques on all components.

        Uses edge-to-edge charge interactions where each component's edges
        repel all other components' edges.

        Returns:
            Tuple of (forces dict, torques dict)
        """
        forces: dict[str, Vector2D] = {comp.ref: Vector2D(0.0, 0.0) for comp in self.components}
        torques: dict[str, float] = {comp.ref: 0.0 for comp in self.components}

        # 1. Component-component repulsion (edge-to-edge charges)
        for i, comp1 in enumerate(self.components):
            outline1 = comp1.outline()
            center1 = comp1.position()

            for j, comp2 in enumerate(self.components):
                if i >= j:
                    continue

                outline2 = comp2.outline()
                center2 = comp2.position()

                # For each edge of comp1, compute force from all edges of comp2
                if not comp1.fixed:
                    for e1_start, e1_end in outline1.edges():
                        for e2_start, e2_end in outline2.edges():
                            force, edge_torque = self.compute_edge_to_edge_force(
                                e1_start,
                                e1_end,
                                e2_start,
                                e2_end,
                                num_samples=self.config.edge_samples,
                            )
                            forces[comp1.ref] = forces[comp1.ref] + force
                            # Convert edge torque to component torque
                            edge_center = (e1_start + e1_end) * 0.5
                            r = edge_center - center1
                            torques[comp1.ref] += r.cross(force) + edge_torque

                # Symmetric: forces on comp2 from comp1's edges
                if not comp2.fixed:
                    for e2_start, e2_end in outline2.edges():
                        for e1_start, e1_end in outline1.edges():
                            force, edge_torque = self.compute_edge_to_edge_force(
                                e2_start,
                                e2_end,
                                e1_start,
                                e1_end,
                                num_samples=self.config.edge_samples,
                            )
                            forces[comp2.ref] = forces[comp2.ref] + force
                            edge_center = (e2_start + e2_end) * 0.5
                            r = edge_center - center2
                            torques[comp2.ref] += r.cross(force) + edge_torque

        # 2. Board boundary forces (edge-to-edge with board)
        for comp in self.components:
            if comp.fixed:
                continue
            outline = comp.outline()
            center = comp.position()
            inside = self.board_outline.contains_point(center)
            scale = self.config.boundary_charge / self.config.charge_density

            for e_start, e_end in outline.edges():
                for b_start, b_end in self.board_outline.edges():
                    force, edge_torque = self.compute_edge_to_edge_force(
                        e_start, e_end, b_start, b_end, num_samples=self.config.edge_samples
                    )
                    # Board edges repel to keep components inside
                    if inside:
                        force = force * scale
                    else:
                        # Strong repulsion to push back inside
                        force = force * (-scale * 10)

                    forces[comp.ref] = forces[comp.ref] + force
                    edge_center = (e_start + e_end) * 0.5
                    r = edge_center - center
                    torques[comp.ref] += r.cross(force) + edge_torque * scale

        # 3. Keepout zone forces
        for keepout in self.keepouts:
            for comp in self.components:
                if comp.fixed:
                    continue
                outline = comp.outline()
                center = comp.position()

                for e_start, e_end in outline.edges():
                    for k_start, k_end in keepout.outline.edges():
                        force, edge_torque = self.compute_edge_to_edge_force(
                            e_start, e_end, k_start, k_end, num_samples=self.config.edge_samples
                        )
                        # Keepouts always repel
                        force = force * keepout.charge_multiplier
                        forces[comp.ref] = forces[comp.ref] + force
                        edge_center = (e_start + e_end) * 0.5
                        r = edge_center - center
                        torques[comp.ref] += (
                            r.cross(force) + edge_torque * keepout.charge_multiplier
                        )

        # 4. Spring forces (net connections)
        for spring in self.springs:
            force1, force2 = self.compute_spring_force(spring)

            comp1 = self._component_map.get(spring.comp1_ref)
            comp2 = self._component_map.get(spring.comp2_ref)

            if comp1 and not comp1.fixed:
                forces[comp1.ref] = forces[comp1.ref] + force1
                # Spring torque from pin position
                pin1 = next((p for p in comp1.pins if p.number == spring.pin1_num), None)
                if pin1:
                    r = Vector2D(pin1.x, pin1.y) - comp1.position()
                    torques[comp1.ref] += r.cross(force1)

            if comp2 and not comp2.fixed:
                forces[comp2.ref] = forces[comp2.ref] + force2
                pin2 = next((p for p in comp2.pins if p.number == spring.pin2_num), None)
                if pin2:
                    r = Vector2D(pin2.x, pin2.y) - comp2.position()
                    torques[comp2.ref] += r.cross(force2)

        # 5. Grouping constraint forces
        if self.grouping_constraints:
            constraint_forces = self.compute_constraint_forces()
            for ref, force in constraint_forces.items():
                comp = self._component_map.get(ref)
                if comp and not comp.fixed:
                    forces[ref] = forces[ref] + force

        # 6. Thermal forces (heat source repulsion and edge attraction)
        if self.config.thermal_enabled:
            thermal_forces = self.compute_thermal_forces()
            for ref, force in thermal_forces.items():
                forces[ref] = forces[ref] + force

        # 7. Rotation potential torque (torsion spring toward 90 deg slots)
        for comp in self.components:
            if not comp.fixed:
                rot_torque = comp.compute_rotation_potential_torque(self.config.rotation_stiffness)
                torques[comp.ref] += rot_torque

        # 7. Edge constraint forces
        for comp in self.components:
            if comp.fixed:
                continue

            constraint = self._edge_constraints.get(comp.ref)
            if constraint:
                edge_force, _ = compute_edge_force(
                    comp, constraint, self.board_edges, stiffness=self.config.edge_stiffness
                )
                forces[comp.ref] = forces[comp.ref] + edge_force

        return forces, torques

    def compute_forces(self) -> dict[str, Vector2D]:
        """Compute net forces on all components (legacy wrapper)."""
        forces, _ = self.compute_forces_and_torques()
        return forces

    def compute_torques(self) -> dict[str, float]:
        """
        Compute torques on all components.

        Spring forces applied at pin positions create torque around component center.
        """
        torques: dict[str, float] = {comp.ref: 0.0 for comp in self.components}

        for spring in self.springs:
            comp1 = self._component_map.get(spring.comp1_ref)
            comp2 = self._component_map.get(spring.comp2_ref)

            if not comp1 or not comp2:
                continue

            pin1 = next((p for p in comp1.pins if p.number == spring.pin1_num), None)
            pin2 = next((p for p in comp2.pins if p.number == spring.pin2_num), None)

            if not pin1 or not pin2:
                continue

            # Spring force
            p1 = Vector2D(pin1.x, pin1.y)
            p2 = Vector2D(pin2.x, pin2.y)
            delta = p2 - p1
            distance = delta.magnitude()

            if distance < 1e-10:
                continue

            extension = distance - spring.rest_length
            force_mag = spring.stiffness * extension
            direction = delta.normalized()

            # Torque = r x F (2D cross product gives scalar)
            if not comp1.fixed:
                r1 = p1 - comp1.position()
                force1 = direction * force_mag
                torque1 = r1.cross(force1)
                torques[comp1.ref] += torque1

            if not comp2.fixed:
                r2 = p2 - comp2.position()
                force2 = -direction * force_mag
                torque2 = r2.cross(force2)
                torques[comp2.ref] += torque2

        return torques

    def compute_energy(self) -> float:
        """
        Compute total system energy (kinetic + potential).

        Includes:
        - Linear kinetic energy (1/2 m v^2)
        - Rotational kinetic energy (1/2 I omega^2)
        - Spring potential energy (1/2 k x^2)
        - Rotation potential energy (torsion spring toward 90 deg slots)
        """
        kinetic = 0.0
        for comp in self.components:
            if not comp.fixed:
                # Linear kinetic energy
                kinetic += 0.5 * comp.mass * (comp.vx**2 + comp.vy**2)
                # Rotational kinetic energy
                inertia = comp.mass * (comp.width**2 + comp.height**2) / 12
                omega_rad = math.radians(comp.angular_velocity)
                kinetic += 0.5 * inertia * omega_rad**2

        potential = 0.0

        # Spring potential energy
        for spring in self.springs:
            comp1 = self._component_map.get(spring.comp1_ref)
            comp2 = self._component_map.get(spring.comp2_ref)

            if not comp1 or not comp2:
                continue

            pin1 = next((p for p in comp1.pins if p.number == spring.pin1_num), None)
            pin2 = next((p for p in comp2.pins if p.number == spring.pin2_num), None)

            if not pin1 or not pin2:
                continue

            dx = pin2.x - pin1.x
            dy = pin2.y - pin1.y
            distance = math.sqrt(dx * dx + dy * dy)
            extension = distance - spring.rest_length
            potential += 0.5 * spring.stiffness * extension * extension

        # Rotation potential energy (torsion springs toward 90 deg slots)
        for comp in self.components:
            if not comp.fixed:
                potential += comp.rotation_potential_energy(self.config.rotation_stiffness)

        return kinetic + potential

    def step(self, dt: float):
        """
        Perform one simulation step.

        Args:
            dt: Time step size
        """
        # Compute forces and torques together (more efficient)
        forces, torques = self.compute_forces_and_torques()

        # Update velocities and apply forces/torques
        for comp in self.components:
            if comp.fixed:
                continue

            force = forces[comp.ref]
            torque = torques[comp.ref]

            comp.apply_force(force, dt)
            comp.apply_torque(torque, dt)

            # Clamp linear velocity
            speed = math.sqrt(comp.vx**2 + comp.vy**2)
            if speed > self.config.max_velocity:
                scale = self.config.max_velocity / speed
                comp.vx *= scale
                comp.vy *= scale

            # Apply damping
            comp.apply_damping(self.config.damping, self.config.angular_damping)

        # Update positions and rotations
        for comp in self.components:
            if comp.fixed:
                continue

            # Update position and rotation
            comp.update_position(dt)

            # Update pin positions based on new position and rotation
            comp.update_pin_positions()

    def run(
        self,
        iterations: int = 1000,
        dt: float = 0.01,
        callback: callable | None = None,
    ) -> int:
        """
        Run the optimization simulation.

        Args:
            iterations: Maximum number of iterations
            dt: Time step size
            callback: Optional function called each iteration with (iteration, energy)

        Returns:
            Number of iterations run
        """
        for i in range(iterations):
            self.step(dt)

            energy = self.compute_energy()

            if callback:
                callback(i, energy)

            # Check convergence
            max_velocity = 0.0
            for comp in self.components:
                if not comp.fixed:
                    speed = math.sqrt(comp.vx**2 + comp.vy**2)
                    max_velocity = max(max_velocity, speed)

            if (
                energy < self.config.energy_threshold
                and max_velocity < self.config.velocity_threshold
            ):
                return i + 1

        return iterations

    def snap_rotations_to_90(self):
        """
        Force all components to exact 90 deg orientations.

        Call after optimization to ensure components are at 0 deg, 90 deg, 180 deg, or 270 deg.
        """
        self.snap_rotations(90.0)

    def snap_rotations(self, grid: float | None = None):
        """
        Snap all component rotations to nearest grid angle.

        Args:
            grid: Rotation grid in degrees (default: config.rotation_grid)
        """
        grid = grid or self.config.rotation_grid
        if grid <= 0:
            return

        for comp in self.components:
            if comp.fixed:
                continue
            # Snap to nearest grid angle
            slot = round(comp.rotation / grid) * grid % 360
            comp.rotation = slot
            comp.angular_velocity = 0.0
            comp.update_pin_positions()

    def snap_positions(self, grid: float | None = None):
        """
        Snap all component positions to nearest grid point.

        Args:
            grid: Position grid in mm (default: config.grid_size)
        """
        grid = grid or self.config.grid_size
        if grid <= 0:
            return

        for comp in self.components:
            if comp.fixed:
                continue
            # Snap to nearest grid point
            comp.x = round(comp.x / grid) * grid
            comp.y = round(comp.y / grid) * grid
            comp.vx = 0.0
            comp.vy = 0.0
            comp.update_pin_positions()

    def snap_to_grid(self, position_grid: float | None = None, rotation_grid: float | None = None):
        """
        Snap all components to position and rotation grids.

        Args:
            position_grid: Position grid in mm (default: config.grid_size)
            rotation_grid: Rotation grid in degrees (default: config.rotation_grid)
        """
        self.snap_positions(position_grid)
        self.snap_rotations(rotation_grid)

    def write_to_pcb(self, pcb: PCB) -> int:
        """
        Write optimized component positions back to a PCB object.

        Updates the footprint positions in the PCB's S-expression tree.
        After calling this, use pcb.save() to write to a file.

        Args:
            pcb: PCB object to update

        Returns:
            Number of components successfully updated
        """
        updated = 0
        for comp in self.components:
            if pcb.update_footprint_position(comp.ref, comp.x, comp.y, comp.rotation):
                updated += 1
        return updated

    def total_wire_length(self) -> float:
        """Compute total estimated wire length (spring lengths)."""
        total = 0.0
        for spring in self.springs:
            comp1 = self._component_map.get(spring.comp1_ref)
            comp2 = self._component_map.get(spring.comp2_ref)

            if not comp1 or not comp2:
                continue

            pin1 = next((p for p in comp1.pins if p.number == spring.pin1_num), None)
            pin2 = next((p for p in comp2.pins if p.number == spring.pin2_num), None)

            if not pin1 or not pin2:
                continue

            dx = pin2.x - pin1.x
            dy = pin2.y - pin1.y
            total += math.sqrt(dx * dx + dy * dy)

        return total

    def report(self) -> str:
        """Generate a text report of current placement."""
        lines = [
            "Placement Optimizer Report",
            "=" * 40,
            f"Components: {len(self.components)}",
            f"Springs (net connections): {len(self.springs)}",
            f"Total wire length: {self.total_wire_length():.2f} mm",
            f"System energy: {self.compute_energy():.4f}",
            "",
            "Component Positions:",
            "-" * 40,
        ]

        for comp in sorted(self.components, key=lambda c: c.ref):
            lines.append(
                f"  {comp.ref:8s}: ({comp.x:7.2f}, {comp.y:7.2f}) @ {comp.rotation:6.1f} deg"
            )

        return "\n".join(lines)
