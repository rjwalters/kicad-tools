"""
Data models for placement optimization.

This module provides the core data structures used in placement optimization:
pins, components, springs (net connections), keepout zones, and configuration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_tools.optim.geometry import Polygon, Vector2D

__all__ = ["Pin", "Component", "Spring", "Keepout", "PlacementConfig"]


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
    pins: list[Pin] = field(default_factory=list)
    fixed: bool = False  # If True, component doesn't move
    mass: float = 1.0  # For physics simulation

    # Physics state
    vx: float = 0.0  # Linear velocity
    vy: float = 0.0
    angular_velocity: float = 0.0  # Angular velocity (deg/step)

    # Store original relative pin positions for rotation
    _pin_offsets: list[tuple[float, float]] = field(default_factory=list, repr=False)

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
        return Polygon.from_footprint_bounds(self.x, self.y, self.width, self.height, self.rotation)

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
        # Moment of inertia for rectangle: I = m(w^2 + h^2)/12
        inertia = self.mass * (self.width**2 + self.height**2) / 12
        angular_accel = torque / inertia
        self.angular_velocity += angular_accel * dt

    def compute_rotation_potential_torque(self, stiffness: float) -> float:
        """
        Compute torque from rotation potential with minima at 90 degree orientations.

        Uses E(theta) = -k * cos(4*theta), so tau = -dE/d*theta = -4k * sin(4*theta)
        This creates energy wells at 0, 90, 180, 270 degrees.
        """
        if self.fixed:
            return 0.0
        # Convert to radians and multiply by 4 for 90 degree periodicity
        theta_rad = math.radians(self.rotation * 4)
        # Torque proportional to -sin(4*theta), scaled by stiffness
        # Negative sign makes it a restoring torque toward nearest well
        return -stiffness * math.sin(theta_rad)

    def rotation_potential_energy(self, stiffness: float) -> float:
        """Compute rotation potential energy (minima at 90 degree slots)."""
        theta_rad = math.radians(self.rotation * 4)
        # E = -k * cos(4*theta), shifted so minimum is 0
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

    # Rotation potential (torsion spring with 90 degree wells)
    rotation_stiffness: float = 10.0  # Torsion spring constant for 90 degree alignment
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
