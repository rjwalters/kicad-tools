"""GPU kernels for signal integrity calculations.

Provides batched crosstalk and impedance calculations using the
configured GPU backend (CUDA, Metal, or CPU fallback).

Example::

    from kicad_tools.acceleration.kernels.signal_integrity import (
        calculate_crosstalk_matrix,
        calculate_impedance_batch,
    )
    from kicad_tools.acceleration import get_backend
    from kicad_tools.performance import PerformanceConfig
    import numpy as np

    config = PerformanceConfig.load_calibrated()
    backend = get_backend(config)

    # Calculate pairwise crosstalk for 100 traces
    separations = np.random.rand(100, 100) * 2.0  # mm
    parallel_lengths = np.random.rand(100, 100) * 20.0  # mm

    coupling = calculate_crosstalk_matrix(
        separations=separations,
        parallel_lengths=parallel_lengths,
        backend=backend,
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from kicad_tools.acceleration.config import ArrayBackend

# Constants for crosstalk calculation
CROSSTALK_CONSTANT = 0.01  # Empirical coupling constant (mm units)
MIN_SPACING_MM = 0.05  # Minimum spacing to avoid division by zero


def calculate_crosstalk_matrix(
    separations: NDArray[np.floating[Any]],
    parallel_lengths: NDArray[np.floating[Any]],
    backend: ArrayBackend,
    crosstalk_constant: float = CROSSTALK_CONSTANT,
) -> NDArray[np.floating[Any]]:
    """Calculate pairwise crosstalk coupling matrix.

    Uses the simplified coupling model:
        k = crosstalk_constant * parallel_length / (separation^2 + epsilon)

    This is based on the "3W rule" heuristic where coupling decreases
    with the square of the separation.

    Args:
        separations: (N, N) array of edge-to-edge spacings in mm.
        parallel_lengths: (N, N) array of parallel run lengths in mm.
        backend: Array backend for computation (GPU or CPU).
        crosstalk_constant: Coupling constant (default from empirical data).

    Returns:
        (N, N) coupling coefficient matrix. Values are in range [0, 1].
        Diagonal is always 0 (no self-coupling).

    Note:
        For accurate physics-based crosstalk, use the CrosstalkAnalyzer
        from kicad_tools.physics.crosstalk which considers dielectric
        properties and frequency-dependent effects.
    """
    # Transfer to backend
    sep = backend.array(separations)
    lengths = backend.array(parallel_lengths)

    # Add small epsilon to avoid division by zero
    sep_safe = sep + MIN_SPACING_MM

    # Calculate coupling coefficient: k proportional to length / separation^2
    # This models the field coupling decay with distance
    coupling = crosstalk_constant * lengths / (sep_safe * sep_safe)

    # Clamp to [0, 1] range
    # For GPU-friendly clamping, we transfer to numpy, clamp, and transfer back
    coupling_np = backend.to_numpy(coupling)
    coupling_np = np.clip(coupling_np, 0.0, 1.0)
    coupling = backend.array(coupling_np)

    # Zero out diagonal (no self-coupling)
    coupling = backend.fill_diagonal(coupling, 0.0)

    return backend.to_numpy(coupling)


def calculate_impedance_batch(
    widths: NDArray[np.floating[Any]],
    dielectric_heights: NDArray[np.floating[Any]],
    dielectric_constants: NDArray[np.floating[Any]],
    backend: ArrayBackend,
    copper_thickness: float = 0.035,
) -> NDArray[np.floating[Any]]:
    """Calculate characteristic impedance for multiple traces in batch.

    Uses the Hammerstad-Jensen microstrip approximation:
        Z0 = (87 / sqrt(er + 1.41)) * ln(5.98 * h / (0.8 * w + t))

    Args:
        widths: (N,) array of trace widths in mm.
        dielectric_heights: (N,) array of substrate heights in mm.
        dielectric_constants: (N,) array of relative permittivity values.
        backend: Array backend for computation (GPU or CPU).
        copper_thickness: Copper thickness in mm (default: 1oz = 0.035mm).

    Returns:
        (N,) array of characteristic impedance values in ohms.

    Note:
        This is a simplified formula suitable for quick estimation.
        For accurate impedance calculations, use TransmissionLine
        from kicad_tools.physics.transmission_line.
    """
    # Transfer to backend
    w = backend.array(widths)
    h = backend.array(dielectric_heights)
    er = backend.array(dielectric_constants)
    t = copper_thickness

    # Hammerstad-Jensen microstrip formula (simplified)
    # Z0 = (87 / sqrt(er + 1.41)) * ln(5.98 * h / (0.8 * w + t))
    #
    # Avoid division by zero with minimum width
    w_safe = w + 0.001  # Add 1 micron minimum

    # Calculate impedance
    denominator = 0.8 * w_safe + t
    ratio = 5.98 * h / denominator

    # Clamp ratio to avoid log of values <= 0
    ratio_np = backend.to_numpy(ratio)
    ratio_np = np.maximum(ratio_np, 1.001)  # Ensure log argument > 1
    ratio = backend.array(ratio_np)

    log_ratio = backend.log(ratio)
    sqrt_term = backend.sqrt(er + 1.41)

    z0 = 87.0 / sqrt_term * log_ratio

    # Clamp to reasonable impedance range [10, 200] ohms
    z0_np = backend.to_numpy(z0)
    z0_np = np.clip(z0_np, 10.0, 200.0)

    return z0_np


def calculate_next_fext_batch(
    coupling_coefficients: NDArray[np.floating[Any]],
    parallel_lengths: NDArray[np.floating[Any]],
    rise_times_ns: NDArray[np.floating[Any]],
    effective_dielectric: NDArray[np.floating[Any]],
    backend: ArrayBackend,
) -> tuple[NDArray[np.floating[Any]], NDArray[np.floating[Any]]]:
    """Calculate NEXT and FEXT for multiple trace pairs in batch.

    NEXT (Near-End Crosstalk):
        - Saturates at k/2 for long coupling lengths
        - Kb = k/2 when L > Lsat

    FEXT (Far-End Crosstalk):
        - Proportional to coupling length
        - Kf = 2 * k * L / rise_distance

    Args:
        coupling_coefficients: (N,) array of coupling coefficients (0-1).
        parallel_lengths: (N,) array of parallel run lengths in mm.
        rise_times_ns: (N,) array of signal rise times in nanoseconds.
        effective_dielectric: (N,) array of effective dielectric constants.
        backend: Array backend for computation.

    Returns:
        Tuple of (next_percent, fext_percent) arrays, each (N,).
    """
    # Speed of light in vacuum (m/s)
    c = 299792458.0

    # Transfer to backend
    k = backend.array(coupling_coefficients)
    lengths = backend.array(parallel_lengths)
    rise_times = backend.array(rise_times_ns)
    eps_eff = backend.array(effective_dielectric)

    # Phase velocity (m/s)
    # v_p = c / sqrt(eps_eff)
    sqrt_eps = backend.sqrt(eps_eff)
    v_p = c / sqrt_eps  # m/s

    # Rise distance (mm)
    # rise_distance = rise_time (ns) * v_p (m/s) * 1e-6 = mm
    rise_distance = rise_times * v_p * 1e-6

    # Saturation length (mm)
    lsat = rise_distance / 2

    # NEXT coefficient (saturates at k/2)
    # Kb = k/2 when L >= Lsat
    # Kb = k/2 * (L/Lsat) when L < Lsat
    kb_max = k / 2

    # Calculate NEXT - need to handle saturation
    kb_max_np = backend.to_numpy(kb_max)
    lengths_np = backend.to_numpy(lengths)
    lsat_np = backend.to_numpy(lsat)

    # NEXT calculation with saturation
    next_coeff = np.where(
        lengths_np >= lsat_np,
        kb_max_np,  # Saturated
        kb_max_np * (lengths_np / (lsat_np + 1e-9)),  # Linear region
    )
    next_coeff = np.clip(next_coeff, 0.0, 1.0)

    # FEXT coefficient
    # Kf = 2 * k * L / rise_distance
    k_np = backend.to_numpy(k)
    rise_distance_np = backend.to_numpy(rise_distance)

    fext_coeff = 2 * k_np * lengths_np / (rise_distance_np + 1e-9)
    fext_coeff = np.clip(fext_coeff, 0.0, 1.0)

    # Convert to percentages
    next_percent = next_coeff * 100.0
    fext_percent = fext_coeff * 100.0

    return next_percent, fext_percent


def calculate_pairwise_distances(
    positions: NDArray[np.floating[Any]],
    backend: ArrayBackend,
) -> NDArray[np.floating[Any]]:
    """Calculate pairwise Euclidean distances between positions.

    Args:
        positions: (N, 2) array of (x, y) positions in mm.
        backend: Array backend for computation.

    Returns:
        (N, N) symmetric distance matrix in mm.
    """
    # Transfer to backend
    pos = backend.array(positions)

    # Use broadcasting: dist[i,j] = sqrt((xi-xj)^2 + (yi-yj)^2)
    # This is memory-intensive but parallelizes well on GPU

    pos_np = backend.to_numpy(pos)

    # Compute squared differences
    # diff[i,j,k] = positions[i,k] - positions[j,k]
    diff = pos_np[:, np.newaxis, :] - pos_np[np.newaxis, :, :]

    # Euclidean distance
    # dist[i,j] = sqrt(sum_k(diff[i,j,k]^2))
    dist_sq = np.sum(diff * diff, axis=2)
    distances = np.sqrt(dist_sq)

    return distances


def estimate_parallel_lengths(
    trace_endpoints: NDArray[np.floating[Any]],
    backend: ArrayBackend,
) -> NDArray[np.floating[Any]]:
    """Estimate parallel routing lengths between trace pairs.

    Uses bounding box overlap as a proxy for parallel routing length.
    This is a heuristic - actual parallel length depends on routing.

    Args:
        trace_endpoints: (N, 4) array of [x1, y1, x2, y2] trace endpoints in mm.
        backend: Array backend for computation.

    Returns:
        (N, N) matrix of estimated parallel lengths in mm.
    """
    # Work with numpy directly for bounding box calculation
    endpoints_np = trace_endpoints

    # Extract coordinates
    x1 = endpoints_np[:, 0]
    y1 = endpoints_np[:, 1]
    x2 = endpoints_np[:, 2]
    y2 = endpoints_np[:, 3]

    # Bounding boxes
    x_min = np.minimum(x1, x2)
    x_max = np.maximum(x1, x2)
    y_min = np.minimum(y1, y2)
    y_max = np.maximum(y1, y2)

    # Pairwise bounding box overlap
    # overlap_x[i,j] = max(0, min(x_max[i], x_max[j]) - max(x_min[i], x_min[j]))
    # overlap_y[i,j] = max(0, min(y_max[i], y_max[j]) - max(y_min[i], y_min[j]))

    # Expand dimensions for broadcasting
    x_min_i = x_min[:, np.newaxis]
    x_max_i = x_max[:, np.newaxis]
    y_min_i = y_min[:, np.newaxis]
    y_max_i = y_max[:, np.newaxis]

    x_min_j = x_min[np.newaxis, :]
    x_max_j = x_max[np.newaxis, :]
    y_min_j = y_min[np.newaxis, :]
    y_max_j = y_max[np.newaxis, :]

    # Calculate overlaps
    overlap_x = np.maximum(0, np.minimum(x_max_i, x_max_j) - np.maximum(x_min_i, x_min_j))
    overlap_y = np.maximum(0, np.minimum(y_max_i, y_max_j) - np.maximum(y_min_i, y_min_j))

    # Parallel length is the larger overlap dimension
    parallel_lengths = np.maximum(overlap_x, overlap_y)

    # Zero diagonal (no self-coupling)
    np.fill_diagonal(parallel_lengths, 0.0)

    return parallel_lengths


def classify_crosstalk_risk(
    next_percent: NDArray[np.floating[Any]],
    fext_percent: NDArray[np.floating[Any]],
) -> NDArray[np.int_]:
    """Classify crosstalk risk level for trace pairs.

    Risk levels:
        0 = acceptable (< 3%)
        1 = marginal (3-10%)
        2 = excessive (> 10%)

    Args:
        next_percent: (N,) array of NEXT percentages.
        fext_percent: (N,) array of FEXT percentages.

    Returns:
        (N,) array of risk level codes (0, 1, or 2).
    """
    # Maximum of NEXT and FEXT
    max_xt = np.maximum(next_percent, fext_percent)

    # Classify
    risk = np.zeros_like(max_xt, dtype=np.int_)
    risk[max_xt >= 3.0] = 1  # Marginal
    risk[max_xt >= 10.0] = 2  # Excessive

    return risk
