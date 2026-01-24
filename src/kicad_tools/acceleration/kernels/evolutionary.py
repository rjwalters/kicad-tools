"""GPU-accelerated kernels for evolutionary fitness evaluation.

Provides vectorized fitness computation for entire populations at once,
enabling significant speedup over per-individual CPU evaluation.

Example::

    from kicad_tools.acceleration.backend import ArrayBackend
    from kicad_tools.acceleration.kernels.evolutionary import evaluate_population_gpu

    backend = ArrayBackend.auto()

    # positions: (population_size, n_components, 2)
    # Returns: (population_size,) fitness scores
    fitness = evaluate_population_gpu(
        positions, component_sizes, springs, board_vertices, weights, backend
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from kicad_tools.acceleration.backend import ArrayBackend


def evaluate_population_gpu(
    positions: NDArray[np.float32],
    component_sizes: NDArray[np.float32],
    springs: NDArray[np.int32],
    spring_pin_offsets: NDArray[np.float32],
    board_vertices: NDArray[np.float32],
    weights: dict[str, float],
    backend: ArrayBackend,
) -> NDArray[np.float32]:
    """Evaluate fitness for entire population on GPU.

    Vectorizes fitness computation across all individuals in the population,
    enabling parallel evaluation of all fitness components.

    Args:
        positions: Component positions with shape (pop_size, n_components, 2).
            The XY coordinates for each component in each individual.
        component_sizes: Component dimensions with shape (n_components, 2).
            Width and height for each component.
        springs: Spring connectivity with shape (n_springs, 2).
            Each row is (comp1_idx, comp2_idx) for connected components.
        spring_pin_offsets: Pin offsets with shape (n_springs, 2, 2).
            For each spring: [[pin1_offset_x, pin1_offset_y],
                              [pin2_offset_x, pin2_offset_y]]
        board_vertices: Board outline with shape (n_vertices, 2).
            XY coordinates of board boundary polygon.
        weights: Fitness weights dictionary with keys:
            - baseline: Starting fitness value (default 1000)
            - wire_length_weight: Weight for wire length penalty
            - conflict_weight: Weight for overlap penalty
            - boundary_violation_weight: Weight for out-of-bounds penalty
            - routability_weight: Weight for spacing bonus
            - pin_alignment_weight: Weight for alignment bonus
            - pin_alignment_tolerance: Tolerance for alignment detection
        backend: ArrayBackend to use for computation.

    Returns:
        Fitness values with shape (pop_size,). Higher is better.
    """
    pop_size = positions.shape[0]

    # Transfer data to GPU
    pos_gpu = backend.array(positions)
    sizes_gpu = backend.array(component_sizes)
    springs_gpu = backend.array(springs, dtype=backend.int32)
    pin_offsets_gpu = backend.array(spring_pin_offsets)
    board_gpu = backend.array(board_vertices)

    # Compute each fitness component (vectorized across population)
    wire_lengths = _compute_wire_lengths_batch(
        pos_gpu, springs_gpu, pin_offsets_gpu, backend
    )

    overlaps = _compute_overlaps_batch(pos_gpu, sizes_gpu, backend)

    boundary_violations = _compute_boundary_violations_batch(
        pos_gpu, board_gpu, backend
    )

    routability = _compute_routability_batch(pos_gpu, backend)

    alignment = _compute_pin_alignment_batch(
        pos_gpu, springs_gpu, pin_offsets_gpu, weights["pin_alignment_tolerance"], backend
    )

    # Compute combined fitness (vectorized)
    baseline = weights.get("baseline", 1000.0)
    fitness = (
        baseline
        - wire_lengths * weights["wire_length_weight"]
        - overlaps * weights["conflict_weight"]
        - boundary_violations * weights["boundary_violation_weight"]
        + routability * weights["routability_weight"]
        + alignment * weights["pin_alignment_weight"]
    )

    return backend.to_numpy(fitness).astype(np.float64)


def _compute_wire_lengths_batch(
    positions: Any,  # (pop_size, n_components, 2)
    springs: Any,  # (n_springs, 2)
    pin_offsets: Any,  # (n_springs, 2, 2)
    backend: ArrayBackend,
) -> Any:
    """Compute total wire length for each individual in population.

    Wire length is the sum of Euclidean distances between connected pins.

    Args:
        positions: Component positions (pop_size, n_components, 2)
        springs: Spring connectivity (n_springs, 2)
        pin_offsets: Pin offset positions (n_springs, 2, 2)
        backend: Array backend

    Returns:
        Wire lengths (pop_size,)
    """
    xp = backend.xp
    pop_size = positions.shape[0]
    n_springs = springs.shape[0]

    if n_springs == 0:
        return backend.zeros((pop_size,))

    # Get component indices for each spring
    comp1_idx = springs[:, 0]  # (n_springs,)
    comp2_idx = springs[:, 1]  # (n_springs,)

    # Get component positions for each spring
    # positions[all_individuals, comp_indices, :] -> (pop_size, n_springs, 2)
    comp1_pos = positions[:, comp1_idx, :]  # (pop_size, n_springs, 2)
    comp2_pos = positions[:, comp2_idx, :]  # (pop_size, n_springs, 2)

    # Get pin offsets for each spring
    pin1_offset = pin_offsets[:, 0, :]  # (n_springs, 2)
    pin2_offset = pin_offsets[:, 1, :]  # (n_springs, 2)

    # Broadcast pin offsets to population dimension
    # pin1_offset: (n_springs, 2) -> (1, n_springs, 2) -> broadcast to (pop_size, n_springs, 2)
    pin1_offset = backend.expand_dims(pin1_offset, axis=0)  # (1, n_springs, 2)
    pin2_offset = backend.expand_dims(pin2_offset, axis=0)  # (1, n_springs, 2)

    # Compute pin positions (component position + pin offset)
    # For simplicity, we assume rotation is 0. A full implementation would
    # include rotation transforms, but the current CPU implementation also
    # pre-computes pin positions based on rotation in the Individual.
    pin1_pos = comp1_pos + pin1_offset  # (pop_size, n_springs, 2)
    pin2_pos = comp2_pos + pin2_offset  # (pop_size, n_springs, 2)

    # Compute distances
    diff = pin2_pos - pin1_pos  # (pop_size, n_springs, 2)
    distances = backend.sqrt(backend.sum(diff ** 2, axis=2))  # (pop_size, n_springs)

    # Sum distances per individual
    wire_lengths = backend.sum(distances, axis=1)  # (pop_size,)

    return wire_lengths


def _compute_overlaps_batch(
    positions: Any,  # (pop_size, n_components, 2)
    sizes: Any,  # (n_components, 2)
    backend: ArrayBackend,
) -> Any:
    """Count component overlaps for each individual using AABB collision.

    Uses axis-aligned bounding box (AABB) overlap detection between
    all pairs of components.

    Args:
        positions: Component positions (pop_size, n_components, 2)
        sizes: Component sizes (n_components, 2) as (width, height)
        backend: Array backend

    Returns:
        Overlap counts (pop_size,)
    """
    xp = backend.xp
    pop_size = positions.shape[0]
    n_comp = positions.shape[1]

    if n_comp < 2:
        return backend.zeros((pop_size,))

    # Half-sizes for AABB checks
    half_sizes = sizes / 2.0  # (n_components, 2)

    # For each pair (i, j) where i < j, check overlap
    # Create indices for upper triangle (excluding diagonal)
    idx_i = []
    idx_j = []
    for i in range(n_comp):
        for j in range(i + 1, n_comp):
            idx_i.append(i)
            idx_j.append(j)

    if not idx_i:
        return backend.zeros((pop_size,))

    idx_i = backend.array(idx_i, dtype=backend.int32)
    idx_j = backend.array(idx_j, dtype=backend.int32)
    n_pairs = len(idx_i) if hasattr(idx_i, "__len__") else idx_i.shape[0]

    # Get positions for all pairs
    # positions[:, idx_i, :] gives (pop_size, n_pairs, 2)
    pos_i = positions[:, idx_i, :]  # (pop_size, n_pairs, 2)
    pos_j = positions[:, idx_j, :]  # (pop_size, n_pairs, 2)

    # Get half-sizes for all pairs
    half_i = half_sizes[idx_i, :]  # (n_pairs, 2)
    half_j = half_sizes[idx_j, :]  # (n_pairs, 2)

    # Broadcast to population
    half_i = backend.expand_dims(half_i, axis=0)  # (1, n_pairs, 2)
    half_j = backend.expand_dims(half_j, axis=0)  # (1, n_pairs, 2)

    # Compute separation and combined half-sizes
    separation = backend.abs(pos_i - pos_j)  # (pop_size, n_pairs, 2)
    combined_half = half_i + half_j  # (1, n_pairs, 2) broadcasts to (pop_size, n_pairs, 2)

    # Overlap occurs when separation < combined_half on BOTH axes
    overlap_x = separation[:, :, 0] < combined_half[:, :, 0]  # (pop_size, n_pairs)
    overlap_y = separation[:, :, 1] < combined_half[:, :, 1]  # (pop_size, n_pairs)
    overlaps = backend.logical_and(overlap_x, overlap_y)  # (pop_size, n_pairs)

    # Count overlaps per individual
    # Cast bool to float for sum
    overlap_counts = backend.sum(overlaps.astype(backend.float32), axis=1)  # (pop_size,)

    return overlap_counts


def _compute_boundary_violations_batch(
    positions: Any,  # (pop_size, n_components, 2)
    board_vertices: Any,  # (n_vertices, 2)
    backend: ArrayBackend,
) -> Any:
    """Count components outside board boundary for each individual.

    Uses ray-casting algorithm for point-in-polygon test.

    Args:
        positions: Component positions (pop_size, n_components, 2)
        board_vertices: Board boundary vertices (n_vertices, 2)
        backend: Array backend

    Returns:
        Violation counts (pop_size,)
    """
    xp = backend.xp
    pop_size = positions.shape[0]
    n_comp = positions.shape[1]
    n_verts = board_vertices.shape[0]

    if n_verts < 3:
        # No valid polygon, no violations
        return backend.zeros((pop_size,))

    # Flatten positions for vectorized point-in-polygon
    # (pop_size, n_components, 2) -> (pop_size * n_components, 2)
    points = backend.reshape(positions, (pop_size * n_comp, 2))

    # Ray-casting algorithm: count edge crossings
    # For each edge (v[i], v[j]), check if horizontal ray from point crosses it
    inside = backend.zeros((pop_size * n_comp,), dtype=backend.int32)

    for i in range(n_verts):
        j = (i + 1) % n_verts
        vi = board_vertices[i]  # (2,)
        vj = board_vertices[j]  # (2,)

        # Edge from (xi, yi) to (xj, yj)
        xi, yi = vi[0], vi[1]
        xj, yj = vj[0], vj[1]

        # Point coordinates
        px = points[:, 0]  # (pop_size * n_comp,)
        py = points[:, 1]  # (pop_size * n_comp,)

        # Check if ray crosses this edge
        # Condition 1: point y is between edge endpoints' y
        cond1 = (yi > py) != (yj > py)

        # Condition 2: point x is left of intersection
        # x_intersect = (xj - xi) * (py - yi) / (yj - yi) + xi
        # Avoid division by zero (when yj == yi, cond1 is False anyway)
        dy = yj - yi
        # Use safe division with small epsilon
        eps = 1e-10
        safe_dy = backend.where(backend.abs(dy) < eps, eps, dy)
        x_intersect = (xj - xi) * (py - yi) / safe_dy + xi
        cond2 = px < x_intersect

        # Toggle inside for points where both conditions are met
        crosses = backend.logical_and(cond1, cond2)
        # XOR with current inside status
        inside = backend.where(crosses, 1 - inside, inside)

    # Count violations (points outside = inside == 0)
    outside = 1 - inside  # (pop_size * n_comp,)
    outside = backend.reshape(outside, (pop_size, n_comp))
    violation_counts = backend.sum(outside.astype(backend.float32), axis=1)

    return violation_counts


def _compute_routability_batch(
    positions: Any,  # (pop_size, n_components, 2)
    backend: ArrayBackend,
) -> Any:
    """Estimate routability based on average component spacing.

    Higher spacing generally means better routability.

    Args:
        positions: Component positions (pop_size, n_components, 2)
        backend: Array backend

    Returns:
        Routability scores (pop_size,) in range [0, 100]
    """
    xp = backend.xp
    pop_size = positions.shape[0]
    n_comp = positions.shape[1]

    if n_comp < 2:
        return backend.ones((pop_size,)) * 100.0

    # Compute pairwise distances between all components
    # Using broadcasting: (pop, n, 1, 2) - (pop, 1, n, 2) -> (pop, n, n, 2)
    pos_i = backend.expand_dims(positions, axis=2)  # (pop, n, 1, 2)
    pos_j = backend.expand_dims(positions, axis=1)  # (pop, 1, n, 2)
    diff = pos_i - pos_j  # (pop, n, n, 2)
    distances = backend.sqrt(backend.sum(diff ** 2, axis=3))  # (pop, n, n)

    # Create upper triangle mask (excluding diagonal)
    # We want to sum only distances where i < j
    mask = np.triu(np.ones((n_comp, n_comp), dtype=np.float32), k=1)
    mask_gpu = backend.array(mask)  # (n, n)

    # Apply mask and compute average
    masked_distances = distances * mask_gpu  # (pop, n, n)
    n_pairs = n_comp * (n_comp - 1) // 2
    avg_spacing = backend.sum(masked_distances, axis=(1, 2)) / n_pairs  # (pop,)

    # Score: more spacing = better, capped at 100
    routability = backend.minimum(avg_spacing * 5.0, 100.0)

    return routability


def _compute_pin_alignment_batch(
    positions: Any,  # (pop_size, n_components, 2)
    springs: Any,  # (n_springs, 2)
    pin_offsets: Any,  # (n_springs, 2, 2)
    tolerance: float,
    backend: ArrayBackend,
) -> Any:
    """Compute pin alignment score for each individual.

    Aligned pins (horizontal or vertical) are easier to route.
    Score is percentage of pin pairs within alignment tolerance.

    Args:
        positions: Component positions (pop_size, n_components, 2)
        springs: Spring connectivity (n_springs, 2)
        pin_offsets: Pin offset positions (n_springs, 2, 2)
        tolerance: Tolerance in mm for alignment detection
        backend: Array backend

    Returns:
        Alignment scores (pop_size,) in range [0, 100]
    """
    xp = backend.xp
    pop_size = positions.shape[0]
    n_springs = springs.shape[0]

    if n_springs == 0:
        return backend.zeros((pop_size,))

    # Get component indices for each spring
    comp1_idx = springs[:, 0]  # (n_springs,)
    comp2_idx = springs[:, 1]  # (n_springs,)

    # Get component positions
    comp1_pos = positions[:, comp1_idx, :]  # (pop_size, n_springs, 2)
    comp2_pos = positions[:, comp2_idx, :]  # (pop_size, n_springs, 2)

    # Get pin offsets
    pin1_offset = backend.expand_dims(pin_offsets[:, 0, :], axis=0)  # (1, n_springs, 2)
    pin2_offset = backend.expand_dims(pin_offsets[:, 1, :], axis=0)  # (1, n_springs, 2)

    # Compute pin positions
    pin1_pos = comp1_pos + pin1_offset  # (pop_size, n_springs, 2)
    pin2_pos = comp2_pos + pin2_offset  # (pop_size, n_springs, 2)

    # Compute alignment
    diff = backend.abs(pin2_pos - pin1_pos)  # (pop_size, n_springs, 2)
    dx = diff[:, :, 0]  # (pop_size, n_springs)
    dy = diff[:, :, 1]  # (pop_size, n_springs)

    # Aligned if within tolerance on either axis
    aligned_x = dx < tolerance  # (pop_size, n_springs)
    aligned_y = dy < tolerance  # (pop_size, n_springs)
    aligned = backend.logical_or(aligned_x, aligned_y)  # (pop_size, n_springs)

    # Compute percentage
    aligned_count = backend.sum(aligned.astype(backend.float32), axis=1)  # (pop_size,)
    alignment_score = (aligned_count / n_springs) * 100.0  # (pop_size,)

    return alignment_score


def prepare_evaluation_data(
    components: dict[str, tuple[float, float, float, float, float, list[tuple[float, float, str]]]],
    springs: list[tuple[str, str, str, str]],
    board_vertices: list[tuple[float, float]],
    ref_to_idx: dict[str, int],
) -> tuple[
    NDArray[np.float32],  # component_sizes
    NDArray[np.int32],  # spring indices
    NDArray[np.float32],  # spring pin offsets
    NDArray[np.float32],  # board vertices
]:
    """Prepare evaluation data arrays from component and spring dictionaries.

    Converts the dictionary-based representation used by the evolutionary
    optimizer into the array format needed for GPU evaluation.

    Args:
        components: Component data dict: ref -> (x, y, rot, width, height, pins)
        springs: Spring list: (comp1_ref, pin1_num, comp2_ref, pin2_num)
        board_vertices: Board outline vertices as (x, y) tuples
        ref_to_idx: Mapping from component ref to array index

    Returns:
        Tuple of (component_sizes, spring_indices, spring_pin_offsets, board_vertices_arr)
    """
    n_components = len(components)
    n_springs = len(springs)
    n_verts = len(board_vertices)

    # Build component sizes array
    component_sizes = np.zeros((n_components, 2), dtype=np.float32)
    for ref, (_, _, _, width, height, _) in components.items():
        idx = ref_to_idx[ref]
        component_sizes[idx] = [width, height]

    # Build spring arrays
    spring_indices = np.zeros((n_springs, 2), dtype=np.int32)
    spring_pin_offsets = np.zeros((n_springs, 2, 2), dtype=np.float32)

    for i, (comp1_ref, pin1_num, comp2_ref, pin2_num) in enumerate(springs):
        # Get component indices
        if comp1_ref not in ref_to_idx or comp2_ref not in ref_to_idx:
            continue
        comp1_idx = ref_to_idx[comp1_ref]
        comp2_idx = ref_to_idx[comp2_ref]
        spring_indices[i] = [comp1_idx, comp2_idx]

        # Get pin offsets from component data
        _, _, _, _, _, pins1 = components[comp1_ref]
        _, _, _, _, _, pins2 = components[comp2_ref]

        # Find matching pins
        pin1_offset = next(
            ((ox, oy) for ox, oy, pn in pins1 if pn == pin1_num),
            (0.0, 0.0),
        )
        pin2_offset = next(
            ((ox, oy) for ox, oy, pn in pins2 if pn == pin2_num),
            (0.0, 0.0),
        )

        spring_pin_offsets[i, 0] = pin1_offset
        spring_pin_offsets[i, 1] = pin2_offset

    # Build board vertices array
    board_vertices_arr = np.array(board_vertices, dtype=np.float32)

    return component_sizes, spring_indices, spring_pin_offsets, board_vertices_arr


def population_to_batch(
    population: list[Any],  # list of Individual
    ref_order: list[str],
) -> NDArray[np.float32]:
    """Convert population of Individuals to batch array.

    Args:
        population: List of Individual objects with positions dict
        ref_order: Ordered list of component refs for consistent indexing

    Returns:
        Positions array (pop_size, n_components, 2)
    """
    pop_size = len(population)
    n_comp = len(ref_order)

    positions = np.zeros((pop_size, n_comp, 2), dtype=np.float32)

    for i, ind in enumerate(population):
        for j, ref in enumerate(ref_order):
            if ref in ind.positions:
                x, y = ind.positions[ref]
                positions[i, j] = [x, y]

    return positions
