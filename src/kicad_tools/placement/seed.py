"""Initial placement heuristics for generating optimizer seed placements.

Provides two strategies for generating an initial placement that serves as a
starting point for the CMA-ES optimizer:

- **Force-directed**: iterative simulation where components attract connected
  neighbours (via shared nets) and repel all other components (to avoid
  overlap). A boundary force keeps components inside the board outline.
  Typically produces much better seeds than random placement.

- **Random**: uniform random placement inside the board, followed by an
  iterative push-apart pass that resolves overlaps. Used as a fallback
  when no net connectivity information is available.

Usage::

    from kicad_tools.placement.seed import force_directed_placement, random_placement
    from kicad_tools.placement.cost import BoardOutline, Net
    from kicad_tools.placement.vector import ComponentDef

    seed = force_directed_placement(components, nets, board)
    # or
    seed = random_placement(components, board)
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from numpy.typing import NDArray

from .cost import BoardOutline, Net
from .vector import (
    FIELDS_PER_COMPONENT,
    ComponentDef,
    PlacementVector,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Force-directed defaults
_FD_MAX_ITERATIONS = 500
_FD_DT = 0.05  # time-step per iteration
_FD_DAMPING = 0.95  # velocity damping
_FD_ATTRACTIVE_STRENGTH = 1.0  # base attractive force per shared net
_FD_REPULSIVE_STRENGTH = 5.0  # repulsive force constant
_FD_BOUNDARY_STRENGTH = 10.0  # boundary restoring force constant
_FD_MIN_SEPARATION = 0.1  # minimum distance to avoid division by zero (mm)
_FD_EQUILIBRIUM_THRESHOLD = 1e-4  # max displacement to declare equilibrium

# Overlap resolution defaults
_OR_MAX_ITERATIONS = 200
_OR_PUSH_FACTOR = 1.1  # fraction of overlap depth to push apart


# ---------------------------------------------------------------------------
# Force-directed placement
# ---------------------------------------------------------------------------


def _build_net_adjacency(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
) -> NDArray[np.float64]:
    """Build an NxN matrix of shared-net counts between component pairs.

    ``adj[i][j]`` equals the number of nets that connect component *i* and
    component *j*. This is used to scale attractive forces proportionally
    to connectivity density.
    """
    n = len(components)
    ref_to_idx: dict[str, int] = {c.reference: i for i, c in enumerate(components)}
    adj = np.zeros((n, n), dtype=np.float64)

    for net in nets:
        # Collect unique component indices touched by this net
        indices: set[int] = set()
        for ref, _ in net.pins:
            if ref in ref_to_idx:
                indices.add(ref_to_idx[ref])
        idx_list = sorted(indices)
        # Add edge weight for every pair
        for a_pos in range(len(idx_list)):
            for b_pos in range(a_pos + 1, len(idx_list)):
                adj[idx_list[a_pos], idx_list[b_pos]] += 1.0
                adj[idx_list[b_pos], idx_list[a_pos]] += 1.0

    return adj


def force_directed_placement(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
    board: BoardOutline,
    *,
    max_iterations: int = _FD_MAX_ITERATIONS,
    dt: float = _FD_DT,
    damping: float = _FD_DAMPING,
    attractive_strength: float = _FD_ATTRACTIVE_STRENGTH,
    repulsive_strength: float = _FD_REPULSIVE_STRENGTH,
    boundary_strength: float = _FD_BOUNDARY_STRENGTH,
) -> PlacementVector:
    """Generate an initial placement using a force-directed algorithm.

    Components sharing nets attract each other (proportional to net count).
    All component pairs repel to prevent overlap. Components are pushed back
    inside the board outline by a boundary restoring force.

    The simulation iterates until forces reach equilibrium (max displacement
    below threshold) or *max_iterations* is exhausted.

    All components are placed on the front side (side=0) with rotation=0.
    The optimizer will explore rotations and side assignments from this seed.

    Args:
        components: Component definitions to place.
        nets: Net connectivity information.
        board: Board outline defining placement boundaries.
        max_iterations: Maximum simulation steps.
        dt: Time-step per iteration.
        damping: Velocity damping factor (0-1).
        attractive_strength: Scaling factor for attractive (net) forces.
        repulsive_strength: Scaling factor for repulsive (overlap) forces.
        boundary_strength: Scaling factor for boundary restoring forces.

    Returns:
        A :class:`PlacementVector` with the computed placement.
    """
    n = len(components)
    if n == 0:
        return PlacementVector(data=np.empty(0, dtype=np.float64))

    # Board centre and half-dimensions
    cx = (board.min_x + board.max_x) / 2.0
    cy = (board.min_y + board.max_y) / 2.0
    hw = board.width / 2.0
    hh = board.height / 2.0

    # Place all components initially near the centre with slight jitter
    rng = np.random.default_rng(42)
    positions = np.empty((n, 2), dtype=np.float64)
    for i in range(n):
        positions[i, 0] = cx + rng.uniform(-hw * 0.3, hw * 0.3)
        positions[i, 1] = cy + rng.uniform(-hh * 0.3, hh * 0.3)

    # Pre-compute component half-sizes for repulsion sizing
    half_sizes = np.array([(c.width / 2.0, c.height / 2.0) for c in components], dtype=np.float64)

    # Build connectivity matrix
    adj = _build_net_adjacency(components, nets)

    # Velocity array for momentum-based simulation
    velocities = np.zeros((n, 2), dtype=np.float64)

    # Per-component safe bounds (component centre must stay within these)
    x_lo = np.array([board.min_x + hs[0] for hs in half_sizes], dtype=np.float64)
    x_hi = np.array([board.max_x - hs[0] for hs in half_sizes], dtype=np.float64)
    y_lo = np.array([board.min_y + hs[1] for hs in half_sizes], dtype=np.float64)
    y_hi = np.array([board.max_y - hs[1] for hs in half_sizes], dtype=np.float64)

    for _iteration in range(max_iterations):
        forces = np.zeros((n, 2), dtype=np.float64)

        # --- Pairwise forces ---
        for i in range(n):
            for j in range(i + 1, n):
                dx = positions[j, 0] - positions[i, 0]
                dy = positions[j, 1] - positions[i, 1]
                dist = math.sqrt(dx * dx + dy * dy)
                if dist < _FD_MIN_SEPARATION:
                    dist = _FD_MIN_SEPARATION
                    # Use a deterministic direction when components overlap
                    dx = _FD_MIN_SEPARATION
                    dy = 0.0

                ux = dx / dist
                uy = dy / dist

                # Repulsive force: inversely proportional to distance squared
                # Scaled by sum of component sizes so larger components repel harder
                size_scale = (
                    half_sizes[i, 0] + half_sizes[i, 1] + half_sizes[j, 0] + half_sizes[j, 1]
                )
                f_repel = repulsive_strength * size_scale / (dist * dist)
                forces[i, 0] -= f_repel * ux
                forces[i, 1] -= f_repel * uy
                forces[j, 0] += f_repel * ux
                forces[j, 1] += f_repel * uy

                # Attractive force: proportional to distance and shared-net count
                net_count = adj[i, j]
                if net_count > 0:
                    f_attract = attractive_strength * net_count * dist
                    forces[i, 0] += f_attract * ux
                    forces[i, 1] += f_attract * uy
                    forces[j, 0] -= f_attract * ux
                    forces[j, 1] -= f_attract * uy

        # --- Boundary restoring force ---
        for i in range(n):
            px, py = positions[i]
            # Push inward if component bounding box extends past board edge
            if px < x_lo[i]:
                forces[i, 0] += boundary_strength * (x_lo[i] - px)
            elif px > x_hi[i]:
                forces[i, 0] += boundary_strength * (x_hi[i] - px)

            if py < y_lo[i]:
                forces[i, 1] += boundary_strength * (y_lo[i] - py)
            elif py > y_hi[i]:
                forces[i, 1] += boundary_strength * (y_hi[i] - py)

        # --- Update velocities and positions ---
        velocities = damping * velocities + dt * forces
        positions += dt * velocities

        # --- Clamp positions to board bounds ---
        for i in range(n):
            positions[i, 0] = max(x_lo[i], min(x_hi[i], positions[i, 0]))
            positions[i, 1] = max(y_lo[i], min(y_hi[i], positions[i, 1]))

        # --- Check equilibrium ---
        max_displacement = np.max(np.abs(dt * velocities))
        if max_displacement < _FD_EQUILIBRIUM_THRESHOLD:
            break

    # --- Encode result as PlacementVector ---
    data = np.zeros(n * FIELDS_PER_COMPONENT, dtype=np.float64)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        data[base] = positions[i, 0]  # x
        data[base + 1] = positions[i, 1]  # y
        data[base + 2] = 0.0  # rotation index (0 = 0 degrees)
        data[base + 3] = 0.0  # side (0 = front)

    return PlacementVector(data=data)


# ---------------------------------------------------------------------------
# Random placement with overlap resolution
# ---------------------------------------------------------------------------


def random_placement(
    components: Sequence[ComponentDef],
    board: BoardOutline,
    *,
    seed: int | None = None,
    max_overlap_iterations: int = _OR_MAX_ITERATIONS,
    push_factor: float = _OR_PUSH_FACTOR,
) -> PlacementVector:
    """Generate a random placement with iterative overlap resolution.

    Places components uniformly at random within the board bounds (accounting
    for component size), then iteratively pushes overlapping pairs apart
    until no overlaps remain or *max_overlap_iterations* is exhausted.

    All components are placed on the front side (side=0) with rotation=0.

    Args:
        components: Component definitions to place.
        board: Board outline defining placement boundaries.
        seed: Random seed for reproducibility. ``None`` for non-deterministic.
        max_overlap_iterations: Maximum push-apart iterations.
        push_factor: Fraction of overlap depth to apply as push displacement.

    Returns:
        A :class:`PlacementVector` with the computed placement.
    """
    n = len(components)
    if n == 0:
        return PlacementVector(data=np.empty(0, dtype=np.float64))

    rng = np.random.default_rng(seed)

    # Half-sizes
    half_sizes = np.array([(c.width / 2.0, c.height / 2.0) for c in components], dtype=np.float64)

    # Generate random positions within safe bounds
    positions = np.empty((n, 2), dtype=np.float64)
    for i in range(n):
        lo_x = board.min_x + half_sizes[i, 0]
        hi_x = board.max_x - half_sizes[i, 0]
        lo_y = board.min_y + half_sizes[i, 1]
        hi_y = board.max_y - half_sizes[i, 1]
        # Handle case where component is wider/taller than board
        if lo_x > hi_x:
            positions[i, 0] = (board.min_x + board.max_x) / 2.0
        else:
            positions[i, 0] = rng.uniform(lo_x, hi_x)
        if lo_y > hi_y:
            positions[i, 1] = (board.min_y + board.max_y) / 2.0
        else:
            positions[i, 1] = rng.uniform(lo_y, hi_y)

    # Iterative overlap resolution
    for _iteration in range(max_overlap_iterations):
        any_overlap = False
        for i in range(n):
            for j in range(i + 1, n):
                # Check axis-aligned bounding box overlap
                dx = positions[j, 0] - positions[i, 0]
                dy = positions[j, 1] - positions[i, 1]

                combined_hw = half_sizes[i, 0] + half_sizes[j, 0]
                combined_hh = half_sizes[i, 1] + half_sizes[j, 1]

                overlap_x = combined_hw - abs(dx)
                overlap_y = combined_hh - abs(dy)

                if overlap_x > 0 and overlap_y > 0:
                    any_overlap = True
                    # Push apart along the axis of minimum overlap
                    if overlap_x < overlap_y:
                        push = push_factor * overlap_x / 2.0
                        if dx >= 0:
                            positions[i, 0] -= push
                            positions[j, 0] += push
                        else:
                            positions[i, 0] += push
                            positions[j, 0] -= push
                    else:
                        push = push_factor * overlap_y / 2.0
                        if dy >= 0:
                            positions[i, 1] -= push
                            positions[j, 1] += push
                        else:
                            positions[i, 1] += push
                            positions[j, 1] -= push

        # Clamp positions to board bounds
        for i in range(n):
            lo_x = board.min_x + half_sizes[i, 0]
            hi_x = board.max_x - half_sizes[i, 0]
            lo_y = board.min_y + half_sizes[i, 1]
            hi_y = board.max_y - half_sizes[i, 1]
            if lo_x <= hi_x:
                positions[i, 0] = max(lo_x, min(hi_x, positions[i, 0]))
            else:
                positions[i, 0] = (board.min_x + board.max_x) / 2.0
            if lo_y <= hi_y:
                positions[i, 1] = max(lo_y, min(hi_y, positions[i, 1]))
            else:
                positions[i, 1] = (board.min_y + board.max_y) / 2.0

        if not any_overlap:
            break

    # Encode result
    data = np.zeros(n * FIELDS_PER_COMPONENT, dtype=np.float64)
    for i in range(n):
        base = i * FIELDS_PER_COMPONENT
        data[base] = positions[i, 0]
        data[base + 1] = positions[i, 1]
        data[base + 2] = 0.0  # rotation index
        data[base + 3] = 0.0  # side

    return PlacementVector(data=data)
