"""
Placement and routing optimization module.

Provides physics-based algorithms for:
- Component placement using force-directed simulation
- Charge-based repulsion from board/component outlines
- Spring-based attraction between net-connected pins

Example::

    from kicad_tools.optim import PlacementOptimizer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    optimizer = PlacementOptimizer.from_pcb(pcb)

    # Run simulation
    optimizer.run(iterations=1000, dt=0.01)

    # Get optimized placements
    for comp in optimizer.components:
        print(f"{comp.ref}: ({comp.x:.2f}, {comp.y:.2f}) @ {comp.rotation:.1f}°")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

__all__ = [
    "PlacementOptimizer",
    "RoutingOptimizer",
    "FigureOfMerit",
    "Vector2D",
    "Polygon",
    "Component",
    "Spring",
    "Keepout",
    "PlacementConfig",
]


@dataclass
class Vector2D:
    """2D vector for physics calculations."""

    x: float = 0.0
    y: float = 0.0

    def __add__(self, other: "Vector2D") -> "Vector2D":
        return Vector2D(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Vector2D") -> "Vector2D":
        return Vector2D(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> "Vector2D":
        return Vector2D(self.x * scalar, self.y * scalar)

    def __rmul__(self, scalar: float) -> "Vector2D":
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> "Vector2D":
        return Vector2D(self.x / scalar, self.y / scalar)

    def __neg__(self) -> "Vector2D":
        return Vector2D(-self.x, -self.y)

    def dot(self, other: "Vector2D") -> float:
        """Dot product."""
        return self.x * other.x + self.y * other.y

    def cross(self, other: "Vector2D") -> float:
        """2D cross product (returns scalar z-component)."""
        return self.x * other.y - self.y * other.x

    def magnitude(self) -> float:
        """Vector length."""
        return math.sqrt(self.x * self.x + self.y * self.y)

    def magnitude_squared(self) -> float:
        """Squared magnitude (faster, avoids sqrt)."""
        return self.x * self.x + self.y * self.y

    def normalized(self) -> "Vector2D":
        """Unit vector in same direction."""
        mag = self.magnitude()
        if mag < 1e-10:
            return Vector2D(0.0, 0.0)
        return self / mag

    def rotated(self, angle_deg: float) -> "Vector2D":
        """Rotate vector by angle in degrees."""
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        return Vector2D(
            self.x * cos_a - self.y * sin_a,
            self.x * sin_a + self.y * cos_a,
        )

    def perpendicular(self) -> "Vector2D":
        """Return perpendicular vector (90° CCW)."""
        return Vector2D(-self.y, self.x)


@dataclass
class Polygon:
    """
    Closed polygon represented as a list of vertices.

    Used for board outlines and component bounding boxes.
    Vertices should be in counter-clockwise order for outward-facing normals.
    """

    vertices: List[Vector2D] = field(default_factory=list)

    @classmethod
    def rectangle(cls, x: float, y: float, width: float, height: float) -> "Polygon":
        """Create a rectangle centered at (x, y)."""
        hw, hh = width / 2, height / 2
        return cls(
            vertices=[
                Vector2D(x - hw, y - hh),
                Vector2D(x + hw, y - hh),
                Vector2D(x + hw, y + hh),
                Vector2D(x - hw, y + hh),
            ]
        )

    @classmethod
    def circle(cls, x: float, y: float, radius: float, segments: int = 16) -> "Polygon":
        """Create a circle approximated as a polygon."""
        vertices = []
        for i in range(segments):
            angle = 2 * math.pi * i / segments
            vx = x + radius * math.cos(angle)
            vy = y + radius * math.sin(angle)
            vertices.append(Vector2D(vx, vy))
        return cls(vertices=vertices)

    @classmethod
    def from_footprint_bounds(
        cls, x: float, y: float, width: float, height: float, rotation: float = 0.0
    ) -> "Polygon":
        """Create rotated rectangle for a component footprint."""
        # Create centered rectangle
        hw, hh = width / 2, height / 2
        corners = [
            Vector2D(-hw, -hh),
            Vector2D(hw, -hh),
            Vector2D(hw, hh),
            Vector2D(-hw, hh),
        ]
        # Rotate and translate
        return cls(
            vertices=[v.rotated(rotation) + Vector2D(x, y) for v in corners]
        )

    def edges(self) -> Iterator[Tuple[Vector2D, Vector2D]]:
        """Iterate over edges as (start, end) pairs."""
        n = len(self.vertices)
        for i in range(n):
            yield self.vertices[i], self.vertices[(i + 1) % n]

    def centroid(self) -> Vector2D:
        """Compute polygon centroid."""
        if not self.vertices:
            return Vector2D(0.0, 0.0)
        cx = sum(v.x for v in self.vertices) / len(self.vertices)
        cy = sum(v.y for v in self.vertices) / len(self.vertices)
        return Vector2D(cx, cy)

    def area(self) -> float:
        """Compute signed area (positive for CCW vertices)."""
        n = len(self.vertices)
        if n < 3:
            return 0.0
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += self.vertices[i].x * self.vertices[j].y
            area -= self.vertices[j].x * self.vertices[i].y
        return area / 2

    def perimeter(self) -> float:
        """Compute polygon perimeter."""
        total = 0.0
        for v1, v2 in self.edges():
            total += (v2 - v1).magnitude()
        return total

    def contains_point(self, p: Vector2D) -> bool:
        """Test if point is inside polygon (ray casting)."""
        n = len(self.vertices)
        inside = False
        j = n - 1
        for i in range(n):
            vi, vj = self.vertices[i], self.vertices[j]
            if ((vi.y > p.y) != (vj.y > p.y)) and (
                p.x < (vj.x - vi.x) * (p.y - vi.y) / (vj.y - vi.y) + vi.x
            ):
                inside = not inside
            j = i
        return inside

    def translate(self, delta: Vector2D) -> "Polygon":
        """Return translated polygon."""
        return Polygon(vertices=[v + delta for v in self.vertices])

    def rotate_around(self, center: Vector2D, angle_deg: float) -> "Polygon":
        """Return polygon rotated around a center point."""
        return Polygon(
            vertices=[
                (v - center).rotated(angle_deg) + center for v in self.vertices
            ]
        )


@dataclass
class Pin:
    """A component pin with position and net assignment."""

    number: str
    x: float  # Absolute position
    y: float
    net: int = 0
    net_name: str = ""


@dataclass
class Component:
    """
    A placeable component with outline and pins.

    The component has a position (x, y) and rotation.
    Pin positions are stored in absolute coordinates and must be
    updated when the component moves.
    """

    ref: str  # Reference designator (e.g., "U1")
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0  # Current rotation (0, 90, 180, 270 when snapping)
    width: float = 1.0
    height: float = 1.0
    pins: List[Pin] = field(default_factory=list)
    fixed: bool = False  # If True, component doesn't move
    mass: float = 1.0  # For physics simulation

    # Physics state
    vx: float = 0.0  # Linear velocity
    vy: float = 0.0
    angular_velocity: float = 0.0  # Angular velocity (deg/step)

    # Store original relative pin positions for rotation
    _pin_offsets: List[Tuple[float, float]] = field(default_factory=list, repr=False)

    def __post_init__(self):
        """Initialize pin offsets from current positions."""
        if not self._pin_offsets and self.pins:
            self._compute_pin_offsets()

    def _compute_pin_offsets(self):
        """Compute pin offsets relative to component center at rotation=0."""
        # Current pin positions are absolute, compute relative offsets
        # accounting for current rotation
        cos_r = math.cos(math.radians(-self.rotation))  # Reverse rotation
        sin_r = math.sin(math.radians(-self.rotation))

        self._pin_offsets = []
        for pin in self.pins:
            # Get offset from component center
            dx = pin.x - self.x
            dy = pin.y - self.y
            # Rotate back to get offset at rotation=0
            ox = dx * cos_r - dy * sin_r
            oy = dx * sin_r + dy * cos_r
            self._pin_offsets.append((ox, oy))

    def update_pin_positions(self):
        """Update pin absolute positions based on current component position and rotation."""
        if not self._pin_offsets:
            self._compute_pin_offsets()

        cos_r = math.cos(math.radians(self.rotation))
        sin_r = math.sin(math.radians(self.rotation))

        for i, pin in enumerate(self.pins):
            ox, oy = self._pin_offsets[i]
            # Rotate offset by current rotation and add to component position
            pin.x = self.x + ox * cos_r - oy * sin_r
            pin.y = self.y + ox * sin_r + oy * cos_r

    def outline(self) -> Polygon:
        """Get current component outline polygon."""
        return Polygon.from_footprint_bounds(
            self.x, self.y, self.width, self.height, self.rotation
        )

    def position(self) -> Vector2D:
        """Get position as Vector2D."""
        return Vector2D(self.x, self.y)

    def velocity(self) -> Vector2D:
        """Get velocity as Vector2D."""
        return Vector2D(self.vx, self.vy)

    def apply_force(self, force: Vector2D, dt: float):
        """Apply force to update velocity (F = ma, a = F/m)."""
        if self.fixed:
            return
        ax = force.x / self.mass
        ay = force.y / self.mass
        self.vx += ax * dt
        self.vy += ay * dt

    def apply_torque(self, torque: float, dt: float):
        """Apply torque to update angular velocity."""
        if self.fixed:
            return
        # Moment of inertia for rectangle: I = m(w² + h²)/12
        inertia = self.mass * (self.width**2 + self.height**2) / 12
        angular_accel = torque / inertia
        self.angular_velocity += angular_accel * dt

    def compute_rotation_potential_torque(self, stiffness: float) -> float:
        """
        Compute torque from rotation potential with minima at 90° orientations.

        Uses E(θ) = -k * cos(4θ), so τ = -dE/dθ = -4k * sin(4θ)
        This creates energy wells at 0°, 90°, 180°, 270°.
        """
        if self.fixed:
            return 0.0
        # Convert to radians and multiply by 4 for 90° periodicity
        theta_rad = math.radians(self.rotation * 4)
        # Torque proportional to -sin(4θ), scaled by stiffness
        # Negative sign makes it a restoring torque toward nearest well
        return -stiffness * math.sin(theta_rad)

    def rotation_potential_energy(self, stiffness: float) -> float:
        """Compute rotation potential energy (minima at 90° slots)."""
        theta_rad = math.radians(self.rotation * 4)
        # E = -k * cos(4θ), shifted so minimum is 0
        return stiffness * (1 - math.cos(theta_rad))

    def update_position(self, dt: float, max_angular_velocity: float = 15.0):
        """Update position and rotation from velocities."""
        if self.fixed:
            return
        self.x += self.vx * dt
        self.y += self.vy * dt

        # Clamp and apply angular velocity
        if abs(self.angular_velocity) > max_angular_velocity:
            self.angular_velocity = math.copysign(max_angular_velocity, self.angular_velocity)
        self.rotation += self.angular_velocity * dt
        self.rotation = self.rotation % 360

    def apply_damping(self, linear: float, angular: float):
        """Apply velocity damping."""
        self.vx *= linear
        self.vy *= linear
        self.angular_velocity *= angular


@dataclass
class Spring:
    """
    A spring connecting two pins.

    Used to model net connections - pins on the same net should
    be pulled together to minimize wire length.
    """

    # Component references and pin numbers
    comp1_ref: str
    pin1_num: str
    comp2_ref: str
    pin2_num: str

    # Spring parameters
    stiffness: float = 1.0  # Spring constant k
    rest_length: float = 0.0  # Natural length (usually 0 for nets)
    net: int = 0
    net_name: str = ""


@dataclass
class Keepout:
    """
    A keepout zone where components cannot be placed.

    Modeled as a charged polygon that repels all components.
    Use for mounting holes, board edge clearances, or exclusion zones.
    """

    outline: Polygon
    charge_multiplier: float = 10.0  # Higher = stronger repulsion
    name: str = ""


@dataclass
class PlacementConfig:
    """Configuration for the placement optimizer."""

    # Charge parameters for repulsion
    charge_density: float = 100.0  # Charge per mm of edge
    min_distance: float = 0.5  # Minimum distance for force calculation (prevents singularity)
    edge_samples: int = 5  # Number of samples per edge for edge-to-edge forces

    # Spring parameters for attraction
    spring_stiffness: float = 10.0  # Default spring constant
    power_net_stiffness: float = 5.0  # Lower stiffness for power nets
    clock_net_stiffness: float = 20.0  # Higher stiffness for clock nets

    # Physics parameters
    damping: float = 0.95  # Linear velocity damping (0-1)
    angular_damping: float = 0.90  # Angular velocity damping
    max_velocity: float = 10.0  # Max velocity in mm/step

    # Rotation potential (torsion spring with 90° wells)
    rotation_stiffness: float = 10.0  # Torsion spring constant for 90° alignment
    allow_continuous_rotation: bool = False  # If True, use accumulated torque; else, continuous

    # Board boundary parameters
    boundary_charge: float = 200.0  # Extra charge on board edges
    boundary_margin: float = 1.0  # Minimum distance from board edge

    # Convergence
    energy_threshold: float = 0.01  # Stop when system energy below this
    velocity_threshold: float = 0.001  # Stop when max velocity below this

    # Grid snapping
    grid_size: float = 0.0  # Position grid in mm (0 = no snapping)
    rotation_grid: float = 90.0  # Rotation grid in degrees (90 for cardinal)


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
        config: Optional[PlacementConfig] = None,
    ):
        """
        Initialize the optimizer.

        Args:
            board_outline: Polygon defining the board boundary
            config: Optimization parameters
        """
        self.board_outline = board_outline
        self.config = config or PlacementConfig()
        self.components: List[Component] = []
        self.springs: List[Spring] = []
        self.keepouts: List[Keepout] = []
        self._component_map: Dict[str, Component] = {}

    @classmethod
    def from_pcb(
        cls,
        pcb: "PCB",  # type: ignore[name-defined]
        config: Optional[PlacementConfig] = None,
        fixed_refs: Optional[List[str]] = None,
    ) -> "PlacementOptimizer":
        """
        Create optimizer from a loaded PCB.

        Args:
            pcb: Loaded PCB object
            config: Optimization parameters
            fixed_refs: List of reference designators for fixed components
                       (e.g., ["J1", "J2"] for connectors)
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

        return optimizer

    @staticmethod
    def _extract_board_outline(pcb: "PCB") -> Optional[Polygon]:  # type: ignore[name-defined]
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
                        return Polygon(vertices=[
                            Vector2D(x1, y1),
                            Vector2D(x2, y1),
                            Vector2D(x2, y2),
                            Vector2D(x1, y2),
                        ])

        # Look for gr_line elements on Edge.Cuts to build outline
        edge_lines: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
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
        lines: List[Tuple[Tuple[float, float], Tuple[float, float]]]
    ) -> Optional[List[Tuple[float, float]]]:
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
                if abs(start[0] - current_end[0]) < tolerance and abs(start[1] - current_end[1]) < tolerance:
                    vertices.append(start)
                    current_end = end
                    used[i] = True
                    found = True
                    break
                elif abs(end[0] - current_end[0]) < tolerance and abs(end[1] - current_end[1]) < tolerance:
                    vertices.append(end)
                    current_end = start
                    used[i] = True
                    found = True
                    break

            if not found:
                break

        # Check if polygon closes
        if vertices and abs(vertices[0][0] - current_end[0]) < tolerance and abs(vertices[0][1] - current_end[1]) < tolerance:
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

    def get_component(self, ref: str) -> Optional[Component]:
        """Get component by reference."""
        return self._component_map.get(ref)

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

    def create_springs_from_nets(self):
        """Create springs connecting all pins on the same net."""
        # Group pins by net
        net_pins: Dict[int, List[Tuple[str, Pin]]] = {}
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

    def compute_edge_to_point_force(
        self, point: Vector2D, edge_start: Vector2D, edge_end: Vector2D, charge_density: float
    ) -> Vector2D:
        """
        Compute repulsion force on a point from a charged line segment.

        Uses linear charge density model with 1/r falloff.
        For a line segment with charge density λ, integrates the field.

        Args:
            point: Point being repelled
            edge_start: Start of charged edge
            edge_end: End of charged edge
            charge_density: Linear charge density λ

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

        # Force magnitude: λ * L / r (total charge / distance)
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
    ) -> Tuple[Vector2D, float]:
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

            # Torque: r × F where r is from edge1 center to sample point
            r = sample_point - edge1_center
            torque = r.cross(force)
            total_torque += torque

        return total_force, total_torque

    def compute_charge_force(self, point: Vector2D, edge_start: Vector2D, edge_end: Vector2D) -> Vector2D:
        """
        Compute repulsion force on a point from a charged line segment.

        Convenience wrapper for compute_edge_to_point_force using default charge density.
        """
        return self.compute_edge_to_point_force(point, edge_start, edge_end, self.config.charge_density)

    def compute_spring_force(self, spring: Spring) -> Tuple[Vector2D, Vector2D]:
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

    def compute_forces_and_torques(self) -> Tuple[Dict[str, Vector2D], Dict[str, float]]:
        """
        Compute net forces and torques on all components.

        Uses edge-to-edge charge interactions where each component's edges
        repel all other components' edges.

        Returns:
            Tuple of (forces dict, torques dict)
        """
        forces: Dict[str, Vector2D] = {comp.ref: Vector2D(0.0, 0.0) for comp in self.components}
        torques: Dict[str, float] = {comp.ref: 0.0 for comp in self.components}

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
                                e1_start, e1_end, e2_start, e2_end,
                                num_samples=self.config.edge_samples
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
                                e2_start, e2_end, e1_start, e1_end,
                                num_samples=self.config.edge_samples
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
                        e_start, e_end, b_start, b_end,
                        num_samples=self.config.edge_samples
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
                            e_start, e_end, k_start, k_end,
                            num_samples=self.config.edge_samples
                        )
                        # Keepouts always repel
                        force = force * keepout.charge_multiplier
                        forces[comp.ref] = forces[comp.ref] + force
                        edge_center = (e_start + e_end) * 0.5
                        r = edge_center - center
                        torques[comp.ref] += r.cross(force) + edge_torque * keepout.charge_multiplier

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

        # 4. Rotation potential torque (torsion spring toward 90° slots)
        for comp in self.components:
            if not comp.fixed:
                rot_torque = comp.compute_rotation_potential_torque(self.config.rotation_stiffness)
                torques[comp.ref] += rot_torque

        return forces, torques

    def compute_forces(self) -> Dict[str, Vector2D]:
        """Compute net forces on all components (legacy wrapper)."""
        forces, _ = self.compute_forces_and_torques()
        return forces

    def compute_torques(self) -> Dict[str, float]:
        """
        Compute torques on all components.

        Spring forces applied at pin positions create torque around component center.
        """
        torques: Dict[str, float] = {comp.ref: 0.0 for comp in self.components}

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

            # Torque = r × F (2D cross product gives scalar)
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
        - Linear kinetic energy (1/2 m v²)
        - Rotational kinetic energy (1/2 I ω²)
        - Spring potential energy (1/2 k x²)
        - Rotation potential energy (torsion spring toward 90° slots)
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

        # Rotation potential energy (torsion springs toward 90° slots)
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
        callback: Optional[callable] = None,
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

            if energy < self.config.energy_threshold and max_velocity < self.config.velocity_threshold:
                return i + 1

        return iterations

    def snap_rotations_to_90(self):
        """
        Force all components to exact 90° orientations.

        Call after optimization to ensure components are at 0°, 90°, 180°, or 270°.
        """
        self.snap_rotations(90.0)

    def snap_rotations(self, grid: Optional[float] = None):
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

    def snap_positions(self, grid: Optional[float] = None):
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

    def snap_to_grid(self, position_grid: Optional[float] = None, rotation_grid: Optional[float] = None):
        """
        Snap all components to position and rotation grids.

        Args:
            position_grid: Position grid in mm (default: config.grid_size)
            rotation_grid: Rotation grid in degrees (default: config.rotation_grid)
        """
        self.snap_positions(position_grid)
        self.snap_rotations(rotation_grid)

    def write_to_pcb(self, pcb: "PCB") -> int:  # type: ignore[name-defined]
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
                f"  {comp.ref:8s}: ({comp.x:7.2f}, {comp.y:7.2f}) @ {comp.rotation:6.1f}°"
            )

        return "\n".join(lines)


class RoutingOptimizer:
    """
    Routing parameter optimizer using metaheuristics.

    .. warning::
        Not yet implemented. Instantiation will raise NotImplementedError.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "RoutingOptimizer is not yet implemented in kicad_tools. "
            "This is an experimental module placeholder."
        )


class FigureOfMerit:
    """Figure of merit computation for routing/placement quality."""

    pass
