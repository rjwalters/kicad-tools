"""
Configuration classes for placement optimization.

Provides configuration dataclasses that control optimizer behavior,
including physics parameters, spring stiffness, and convergence criteria.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PlacementConfig"]


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

    # Rotation potential (torsion spring with 90 deg wells)
    rotation_stiffness: float = 10.0  # Torsion spring constant for 90 deg alignment
    allow_continuous_rotation: bool = False  # If True, use accumulated torque; else, continuous

    # Board boundary parameters
    boundary_charge: float = 200.0  # Extra charge on board edges
    boundary_margin: float = 1.0  # Minimum distance from board edge

    # Edge constraint parameters
    edge_stiffness: float = 50.0  # Spring constant for edge constraints

    # Convergence
    energy_threshold: float = 0.01  # Stop when system energy below this
    velocity_threshold: float = 0.001  # Stop when max velocity below this

    # Grid snapping
    grid_size: float = 0.0  # Position grid in mm (0 = no snapping)
    rotation_grid: float = 90.0  # Rotation grid in degrees (90 for cardinal)

    # Functional clustering
    cluster_stiffness: float = 50.0  # Strong spring for cluster member attraction
    cluster_enabled: bool = False  # Enable automatic cluster detection

    # Thermal awareness
    thermal_enabled: bool = False  # Enable thermal-aware placement
    thermal_separation_mm: float = (
        15.0  # Min distance between heat sources and sensitive components
    )
    thermal_edge_preference_mm: float = 10.0  # Max distance from edge for heat sources
    thermal_repulsion_strength: float = 500.0  # Repulsion between heat sources and sensitive
    thermal_edge_attraction: float = 50.0  # Edge attraction for heat sources
