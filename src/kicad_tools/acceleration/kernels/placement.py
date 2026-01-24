"""GPU-accelerated force calculations for placement optimization.

Provides GPU implementations of the O(n^2) pairwise force calculations
used in force-directed component placement. The main computation is
edge-to-edge repulsion between component outlines.

The GPU implementation batches all edge pairs and computes forces in parallel,
providing significant speedup for boards with many components (50+).

This module uses GPU-native scatter-add operations to eliminate redundant
CPU-GPU memory transfers within inner loops. See issue #1052 for details.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from kicad_tools.acceleration.backend import (
    ArrayBackend,
    BackendType,
    get_array_pool,
    get_backend,
    get_best_available_backend,
)
from kicad_tools.acceleration.config import should_use_gpu

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from kicad_tools.optim.components import Component
    from kicad_tools.optim.config import PlacementConfig
    from kicad_tools.optim.geometry import Polygon, Vector2D
    from kicad_tools.performance import PerformanceConfig


@dataclass
class EdgeBatch:
    """Batch of edges for GPU processing.

    Attributes:
        starts: (N, 2) array of edge start points.
        ends: (N, 2) array of edge end points.
        component_indices: (N,) array mapping each edge to its component.
        component_centers: (M, 2) array of component centers for torque calc.
    """

    starts: NDArray[np.float32]
    ends: NDArray[np.float32]
    component_indices: NDArray[np.int32]
    component_centers: NDArray[np.float32]


def extract_edges_batch(
    components: list[Component],
    component_map: dict[str, int],
) -> EdgeBatch:
    """Extract all component edges into batched arrays.

    Args:
        components: List of components to extract edges from.
        component_map: Mapping from component ref to index.

    Returns:
        EdgeBatch containing all edges with component indices.
    """
    all_starts: list[tuple[float, float]] = []
    all_ends: list[tuple[float, float]] = []
    all_indices: list[int] = []
    centers: list[tuple[float, float]] = []

    for comp in components:
        idx = component_map[comp.ref]
        outline = comp.outline()
        center = comp.position()
        centers.append((center.x, center.y))

        for start, end in outline.edges():
            all_starts.append((start.x, start.y))
            all_ends.append((end.x, end.y))
            all_indices.append(idx)

    return EdgeBatch(
        starts=np.array(all_starts, dtype=np.float32),
        ends=np.array(all_ends, dtype=np.float32),
        component_indices=np.array(all_indices, dtype=np.int32),
        component_centers=np.array(centers, dtype=np.float32),
    )


def compute_pairwise_repulsion_gpu(
    edge_batch: EdgeBatch,
    backend: ArrayBackend,
    charge_density: float,
    min_distance: float,
    num_samples: int = 5,
    fixed_mask: NDArray[np.bool_] | None = None,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Compute pairwise repulsion forces between all edges on GPU.

    Uses the edge-to-edge charge model where edges have linear charge density.
    Each edge is discretized into sample points, and forces are computed between
    all pairs of edges from different components.

    This implementation uses GPU-native scatter-add operations to accumulate
    forces per component without redundant CPU-GPU memory transfers. The key
    optimization eliminates the to_numpy/array pattern in the inner loop that
    was causing O(n^2) memory transfers.

    Args:
        edge_batch: Batched edge data from extract_edges_batch.
        backend: Array backend (CPU/GPU).
        charge_density: Charge per mm of edge.
        min_distance: Minimum distance to prevent singularity.
        num_samples: Number of sample points per edge.
        fixed_mask: Boolean mask where True means component is fixed (no forces).

    Returns:
        Tuple of:
            - forces: (num_components, 2) net force on each component
            - torques: (num_components,) net torque on each component
    """
    xp = backend.xp
    n_edges = len(edge_batch.starts)
    n_components = len(edge_batch.component_centers)

    if n_edges == 0:
        return np.zeros((n_components, 2), dtype=np.float32), np.zeros(
            n_components, dtype=np.float32
        )

    # Transfer data to GPU (initial transfer only)
    starts = backend.array(edge_batch.starts)  # (E, 2)
    ends = backend.array(edge_batch.ends)  # (E, 2)
    comp_idx = backend.array(edge_batch.component_indices.astype(np.int32))
    centers = backend.array(edge_batch.component_centers)  # (C, 2)

    # Edge vectors and lengths
    edge_vecs = ends - starts  # (E, 2)
    edge_lens = backend.sqrt(backend.sum(edge_vecs**2, axis=1))  # (E,)
    edge_lens = backend.maximum(edge_lens, 1e-10)

    # Initialize force accumulators on GPU (stays GPU-resident throughout)
    forces_accum = backend.zeros((n_components, 2))
    torques_accum = backend.zeros((n_components,))

    # Convert fixed_mask to GPU array if provided
    fixed_mask_gpu = None
    if fixed_mask is not None:
        fixed_mask_gpu = backend.array(fixed_mask.astype(np.float32))  # 1.0 if fixed, 0.0 if not
        # Invert: we want to multiply by 0 for fixed, 1 for non-fixed
        movable_mask_gpu = 1.0 - fixed_mask_gpu  # (C,)

    # Process in chunks to limit memory usage
    # For E edges, full pairwise matrix is E x E x samples^2
    # Chunk to keep memory under ~1GB
    max_chunk_size = min(1000, n_edges)  # Adjust based on GPU memory

    for chunk_start in range(0, n_edges, max_chunk_size):
        chunk_end = min(chunk_start + max_chunk_size, n_edges)

        # Get chunk of source edges (edges receiving forces)
        src_starts = starts[chunk_start:chunk_end]  # (chunk, 2)
        src_ends = ends[chunk_start:chunk_end]
        src_vecs = edge_vecs[chunk_start:chunk_end]
        src_lens = edge_lens[chunk_start:chunk_end]
        src_comp = comp_idx[chunk_start:chunk_end]
        chunk_size = chunk_end - chunk_start

        # Generate sample points along source edges
        # t values: (num_samples,)
        t_vals = backend.array(
            [(i + 0.5) / num_samples for i in range(num_samples)], dtype=xp.float32
        )

        # Sample points: (chunk, samples, 2)
        # sample = start + t * (end - start)
        t_expanded = t_vals.reshape(1, num_samples, 1)  # (1, S, 1)
        src_starts_exp = src_starts[:, None, :]  # (chunk, 1, 2)
        src_vecs_exp = src_vecs[:, None, :]  # (chunk, 1, 2)
        sample_points = src_starts_exp + t_expanded * src_vecs_exp  # (chunk, S, 2)

        # Charge per sample
        sample_charge = charge_density * src_lens / num_samples  # (chunk,)

        # For each sample point, compute force from all target edges
        # This is the expensive O(n^2) part
        for tgt_start_idx in range(0, n_edges, max_chunk_size):
            tgt_end_idx = min(tgt_start_idx + max_chunk_size, n_edges)

            tgt_starts = starts[tgt_start_idx:tgt_end_idx]  # (T, 2)
            tgt_ends = ends[tgt_start_idx:tgt_end_idx]
            tgt_lens = edge_lens[tgt_start_idx:tgt_end_idx]
            tgt_comp = comp_idx[tgt_start_idx:tgt_end_idx]

            # Skip same-component interactions
            # Create mask: (chunk, T) where True means different components
            src_comp_exp = src_comp[:, None]  # (chunk, 1)
            tgt_comp_exp = tgt_comp[None, :]  # (1, T)
            diff_comp_mask = src_comp_exp != tgt_comp_exp  # (chunk, T)

            # Compute force from each target edge on each sample point
            # Using edge-to-point force formula:
            # F = (charge * edge_charge / distance) * direction

            # For each (sample, target_edge) pair:
            # 1. Find closest point on target edge to sample point
            # 2. Compute distance and force

            # sample_points: (chunk, S, 2)
            # tgt_starts: (T, 2), tgt_ends: (T, 2)

            # Expand for broadcasting
            sp = sample_points[:, :, None, :]  # (chunk, S, 1, 2)
            ts = tgt_starts[None, None, :, :]  # (1, 1, T, 2)
            te = tgt_ends[None, None, :, :]  # (1, 1, T, 2)
            tv = te - ts  # (1, 1, T, 2)
            tl = tgt_lens[None, None, :]  # (1, 1, T)

            # Vector from target start to sample point
            w = sp - ts  # (chunk, S, T, 2)

            # Project onto target edge: t = (w . tv) / |tv|^2
            tv_sq = backend.sum(tv**2, axis=-1, keepdims=True)  # (1, 1, T, 1)
            tv_sq = backend.maximum(tv_sq, 1e-20)
            t_proj = backend.sum(w * tv, axis=-1, keepdims=True) / tv_sq  # (chunk, S, T, 1)
            t_proj = backend.clip(t_proj, 0.0, 1.0)

            # Closest point on target edge
            closest = ts + t_proj * tv  # (chunk, S, T, 2)

            # Displacement from closest point to sample point
            disp = sp - closest  # (chunk, S, T, 2)
            dist = backend.sqrt(backend.sum(disp**2, axis=-1))  # (chunk, S, T)
            dist = backend.maximum(dist, min_distance)

            # Force magnitude: (sample_charge * tgt_charge) / dist
            # tgt_charge = charge_density * tgt_len
            tgt_charge = charge_density * tl  # (1, 1, T)
            src_charge_exp = sample_charge[:, None, None]  # (chunk, 1, 1)

            # Coulomb-like: F = k * q1 * q2 / r^2
            # Using linear charge model: F = (lambda1 * L1) * (lambda2 * L2) / r
            force_mag = src_charge_exp * tgt_charge / (dist**2 + 1e-10)  # (chunk, S, T)

            # Apply component mask (no self-interaction)
            mask_exp = diff_comp_mask[:, None, :].astype(xp.float32)  # (chunk, 1, T)
            force_mag = force_mag * mask_exp  # (chunk, S, T)

            # Force direction: normalized displacement
            dist_exp = dist[:, :, :, None]  # (chunk, S, T, 1)
            force_dir = disp / (dist_exp + 1e-10)  # (chunk, S, T, 2)

            # Force vectors
            force_vecs = force_dir * force_mag[:, :, :, None]  # (chunk, S, T, 2)

            # Sum over samples and target edges to get force per source edge
            edge_forces = backend.sum(force_vecs, axis=(1, 2))  # (chunk, 2)

            # Apply fixed mask to edge forces before accumulation
            if fixed_mask_gpu is not None:
                # Get movable mask for each edge's component
                # src_comp: (chunk,) indices into (C,) mask
                # Need to index movable_mask_gpu by src_comp
                src_comp_np = backend.to_numpy(src_comp).astype(np.int32)
                movable_mask_np = backend.to_numpy(movable_mask_gpu)
                edge_movable = backend.array(movable_mask_np[src_comp_np])  # (chunk,)
                edge_forces = edge_forces * edge_movable[:, None]  # (chunk, 2)

            # GPU-native scatter-add: accumulate forces per component
            # This eliminates the CPU-GPU transfer in the inner loop!
            forces_accum = backend.scatter_add(forces_accum, src_comp, edge_forces)

            # Compute torques (also GPU-resident)
            # Torque = r x F where r is from component center to edge center
            src_centers_exp = src_starts + src_vecs * 0.5  # edge centers (chunk, 2)

            # Get component centers for each edge
            # Need advanced indexing: centers[src_comp]
            src_comp_np = backend.to_numpy(src_comp).astype(np.int32)
            centers_np = backend.to_numpy(centers)
            comp_centers_chunk = backend.array(centers_np[src_comp_np])  # (chunk, 2)
            r_vecs = src_centers_exp - comp_centers_chunk  # (chunk, 2)

            # 2D cross product: r.x * F.y - r.y * F.x
            torque_contrib = r_vecs[:, 0] * edge_forces[:, 1] - r_vecs[:, 1] * edge_forces[:, 0]  # (chunk,)

            # GPU-native scatter-add for torques
            torques_accum = backend.scatter_add(torques_accum, src_comp, torque_contrib)

    # Transfer results to CPU only once at the end
    return backend.to_numpy(forces_accum), backend.to_numpy(torques_accum)


class PlacementGPUAccelerator:
    """GPU accelerator for placement force calculations.

    Manages GPU backend and provides high-level interface for computing
    forces with automatic CPU fallback.

    This class uses the ArrayBackend abstraction with GPU-native scatter-add
    operations to eliminate redundant CPU-GPU memory transfers during force
    accumulation. See backend.py for implementation details.
    """

    def __init__(
        self,
        perf_config: PerformanceConfig | None = None,
        backend: ArrayBackend | None = None,
    ):
        """Initialize the accelerator.

        Args:
            perf_config: Performance configuration with GPU settings.
            backend: Explicit backend to use (overrides perf_config detection).
        """
        self._perf_config = perf_config
        self._backend = backend
        self._edge_batch: EdgeBatch | None = None
        self._component_map: dict[str, int] = {}

    def _get_backend(self, n_components: int) -> ArrayBackend:
        """Get appropriate backend based on problem size."""
        if self._backend is not None:
            return self._backend

        if self._perf_config is not None:
            if should_use_gpu(self._perf_config, n_components, "placement"):
                return get_best_available_backend()
            else:
                return get_backend(BackendType.CPU)

        # Default: use GPU for 50+ components
        if n_components >= 50:
            return get_best_available_backend()
        return get_backend(BackendType.CPU)

    def prepare_batch(
        self,
        components: list[Component],
    ) -> None:
        """Prepare edge batch for force calculations.

        Call this once when components change (not every iteration).

        Args:
            components: List of components to process.
        """
        self._component_map = {comp.ref: i for i, comp in enumerate(components)}
        self._edge_batch = extract_edges_batch(components, self._component_map)

    def compute_repulsion_forces(
        self,
        components: list[Component],
        config: PlacementConfig,
        fixed_refs: set[str] | None = None,
    ) -> tuple[dict[str, Vector2D], dict[str, float]]:
        """Compute repulsion forces and torques on all components.

        Args:
            components: List of components (must match prepare_batch).
            config: Placement configuration.
            fixed_refs: Set of component refs that are fixed (no forces applied).

        Returns:
            Tuple of (forces dict, torques dict) keyed by component ref.
        """
        from kicad_tools.optim.geometry import Vector2D

        n_components = len(components)
        if n_components == 0:
            return {}, {}

        # Re-extract edges if batch not prepared or component count changed
        if (
            self._edge_batch is None
            or len(self._edge_batch.component_centers) != n_components
        ):
            self.prepare_batch(components)

        backend = self._get_backend(n_components)

        # Create fixed mask
        fixed_mask = None
        if fixed_refs:
            fixed_mask = np.array(
                [comp.ref in fixed_refs for comp in components], dtype=np.bool_
            )

        # Compute forces on GPU
        forces_array, torques_array = compute_pairwise_repulsion_gpu(
            edge_batch=self._edge_batch,  # type: ignore
            backend=backend,
            charge_density=config.charge_density,
            min_distance=config.min_distance,
            num_samples=config.edge_samples,
            fixed_mask=fixed_mask,
        )

        # Convert to dict format
        forces = {
            comp.ref: Vector2D(float(forces_array[i, 0]), float(forces_array[i, 1]))
            for i, comp in enumerate(components)
        }
        torques = {comp.ref: float(torques_array[i]) for i, comp in enumerate(components)}

        return forces, torques

    def update_edge_positions(self, components: list[Component]) -> None:
        """Update edge positions after components move.

        More efficient than full prepare_batch when only positions change.

        Args:
            components: Components with updated positions.
        """
        # For now, just re-extract. Could optimize to only update positions.
        self.prepare_batch(components)

    @property
    def backend_type(self) -> BackendType:
        """Return the active backend type."""
        if self._backend is not None:
            return self._backend.backend_type
        return BackendType.CPU
