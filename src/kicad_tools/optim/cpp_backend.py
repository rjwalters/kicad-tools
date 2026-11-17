"""
C++ force engine backend with Python fallback.

This module provides a unified interface to the force-directed placement
engine that automatically uses the C++ implementation when available,
falling back to pure Python.

The C++ backend keeps the full N^2 edge-to-edge repulsion loop in C++,
avoiding per-element Python overhead for significant speedup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.optim.components import Component
    from kicad_tools.optim.config import PlacementConfig
    from kicad_tools.optim.geometry import Polygon, Vector2D

# Try to import C++ module
_CPP_IMPORT_ERROR: str | None = None
try:
    from kicad_tools.placement import placement_cpp

    _CPP_AVAILABLE = True
except ImportError as e:
    _CPP_AVAILABLE = False
    _CPP_IMPORT_ERROR = str(e)
    placement_cpp = None  # type: ignore


def is_cpp_available() -> bool:
    """Check if the C++ force engine backend is available."""
    return _CPP_AVAILABLE


def get_cpp_unavailable_reason() -> str | None:
    """Get the reason why C++ backend is unavailable."""
    if _CPP_AVAILABLE:
        return None
    return _CPP_IMPORT_ERROR


def _marshal_components(
    components: list[Component],
) -> tuple[list[float], list[float], list[float], list[int], list[bool]]:
    """Convert component data to flat arrays for C++ engine.

    Returns:
        Tuple of (positions_x, positions_y, edges_flat, edge_offsets, fixed_mask).
        edges_flat contains [sx, sy, ex, ey] for each edge of each component.
        edge_offsets[i] is the starting edge index for component i.
    """
    positions_x: list[float] = []
    positions_y: list[float] = []
    edges_flat: list[float] = []
    edge_offsets: list[int] = [0]
    fixed_mask: list[bool] = []

    edge_count = 0
    for comp in components:
        pos = comp.position()
        positions_x.append(pos.x)
        positions_y.append(pos.y)
        fixed_mask.append(comp.fixed)

        outline = comp.outline()
        for e_start, e_end in outline.edges():
            edges_flat.extend([e_start.x, e_start.y, e_end.x, e_end.y])
            edge_count += 1
        edge_offsets.append(edge_count)

    return positions_x, positions_y, edges_flat, edge_offsets, fixed_mask


def _marshal_board_edges(
    board_outline: Polygon,
) -> tuple[list[float], int]:
    """Convert board outline to flat edge array.

    Returns:
        Tuple of (board_edges_flat, n_board_edges).
    """
    board_edges: list[float] = []
    n_board_edges = 0
    for e_start, e_end in board_outline.edges():
        board_edges.extend([e_start.x, e_start.y, e_end.x, e_end.y])
        n_board_edges += 1
    return board_edges, n_board_edges


def compute_repulsion_cpp(
    components: list[Component],
    config: PlacementConfig,
) -> tuple[dict[str, Vector2D], dict[str, float]]:
    """Compute component repulsion forces using C++ backend.

    Args:
        components: List of components.
        config: Placement configuration.

    Returns:
        Tuple of (forces dict, torques dict) keyed by component ref.

    Raises:
        RuntimeError: If C++ backend is not available.
    """
    if not _CPP_AVAILABLE:
        raise RuntimeError("C++ force engine backend not available")

    from kicad_tools.optim.geometry import Vector2D

    n = len(components)
    if n == 0:
        return {}, {}

    positions_x, positions_y, edges_flat, edge_offsets, fixed_mask = _marshal_components(components)

    # Build ForceConfig
    cpp_config = placement_cpp.ForceConfig()
    cpp_config.charge_density = config.charge_density
    cpp_config.min_distance = config.min_distance
    cpp_config.edge_samples = config.edge_samples
    cpp_config.boundary_charge = config.boundary_charge

    result = placement_cpp.compute_all_repulsion(
        positions_x,
        positions_y,
        edges_flat,
        edge_offsets,
        n,
        cpp_config,
        fixed_mask,
    )

    # Convert back to dicts
    forces: dict[str, Vector2D] = {}
    torques: dict[str, float] = {}
    for i, comp in enumerate(components):
        forces[comp.ref] = Vector2D(result.forces_x[i], result.forces_y[i])
        torques[comp.ref] = result.torques[i]

    return forces, torques


def compute_boundary_forces_cpp(
    components: list[Component],
    board_outline: Polygon,
    config: PlacementConfig,
    *,
    effective_boundary_charge: float | None = None,
) -> tuple[dict[str, Vector2D], dict[str, float]]:
    """Compute boundary forces using C++ backend.

    Args:
        components: List of components.
        board_outline: Board outline polygon.
        config: Placement configuration.
        effective_boundary_charge: Pre-computed boundary charge (auto-scaled
            for component density).  When *None* the raw
            ``config.boundary_charge`` is used.

    Returns:
        Tuple of (forces dict, torques dict) keyed by component ref.

    Raises:
        RuntimeError: If C++ backend is not available.
    """
    if not _CPP_AVAILABLE:
        raise RuntimeError("C++ force engine backend not available")

    from kicad_tools.optim.geometry import Vector2D

    n = len(components)
    if n == 0:
        return {}, {}

    positions_x, positions_y, edges_flat, edge_offsets, fixed_mask = _marshal_components(components)
    board_edges, n_board_edges = _marshal_board_edges(board_outline)

    # Compute inside flags
    inside_flags: list[bool] = []
    for comp in components:
        center = comp.position()
        inside_flags.append(board_outline.contains_point(center))

    # Build ForceConfig -- use effective charge when provided
    boundary_charge = (
        effective_boundary_charge
        if effective_boundary_charge is not None
        else config.boundary_charge
    )
    cpp_config = placement_cpp.ForceConfig()
    cpp_config.charge_density = config.charge_density
    cpp_config.min_distance = config.min_distance
    cpp_config.edge_samples = config.edge_samples
    cpp_config.boundary_charge = boundary_charge

    result = placement_cpp.compute_boundary_forces(
        positions_x,
        positions_y,
        edges_flat,
        edge_offsets,
        board_edges,
        n_board_edges,
        n,
        cpp_config,
        fixed_mask,
        inside_flags,
    )

    # Convert back to dicts
    forces: dict[str, Vector2D] = {}
    torques: dict[str, float] = {}
    for i, comp in enumerate(components):
        forces[comp.ref] = Vector2D(result.forces_x[i], result.forces_y[i])
        torques[comp.ref] = result.torques[i]

    return forces, torques
