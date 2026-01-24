"""GPU kernels for signal integrity calculations.

Provides batched crosstalk and impedance calculations using the
ArrayBackend abstraction for CPU/GPU acceleration.

Example::

    from kicad_tools.acceleration.kernels.signal_integrity import (
        SignalIntegrityGPUAccelerator,
        calculate_crosstalk_matrix,
    )
    from kicad_tools.acceleration import get_backend
    import numpy as np

    backend = get_backend("cuda")  # or "metal" or "cpu"

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

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from kicad_tools.acceleration.backend import ArrayBackend

# Constants for crosstalk calculation
CROSSTALK_CONSTANT = 0.01  # Empirical coupling constant (mm units)
MIN_SPACING_MM = 0.05  # Minimum spacing to avoid division by zero
SPEED_OF_LIGHT = 299792458.0  # m/s


def calculate_crosstalk_matrix(
    separations: NDArray[np.floating],
    parallel_lengths: NDArray[np.floating],
    backend: ArrayBackend,
    crosstalk_constant: float = CROSSTALK_CONSTANT,
) -> NDArray[np.floating]:
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
    coupling = crosstalk_constant * lengths / (sep_safe * sep_safe)

    # Clamp to [0, 1] range and transfer back to numpy
    coupling = backend.clip(coupling, 0.0, 1.0)

    # Zero out diagonal (no self-coupling)
    coupling = backend.fill_diagonal(coupling, 0.0)

    return backend.to_numpy(coupling)


def calculate_impedance_batch(
    widths: NDArray[np.floating],
    dielectric_heights: NDArray[np.floating],
    dielectric_constants: NDArray[np.floating],
    backend: ArrayBackend,
    copper_thickness: float = 0.035,
) -> NDArray[np.floating]:
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

    # Avoid division by zero with minimum width
    w_safe = backend.maximum(w, backend.array([0.001]))

    # Calculate impedance using Hammerstad-Jensen
    denominator = 0.8 * w_safe + t
    ratio = 5.98 * h / denominator

    # Ensure log argument is valid (> 1)
    ratio_safe = backend.maximum(ratio, backend.array([1.001]))

    # Z0 = (87 / sqrt(er + 1.41)) * ln(ratio)
    xp = backend.xp
    log_ratio = xp.log(ratio_safe)
    sqrt_term = backend.sqrt(er + 1.41)
    z0 = 87.0 / sqrt_term * log_ratio

    # Clamp to reasonable impedance range [10, 200] ohms
    z0 = backend.clip(z0, 10.0, 200.0)

    return backend.to_numpy(z0)


def calculate_next_fext_batch(
    coupling_coefficients: NDArray[np.floating],
    parallel_lengths: NDArray[np.floating],
    rise_times_ns: NDArray[np.floating],
    effective_dielectric: NDArray[np.floating],
    backend: ArrayBackend,
) -> tuple[NDArray[np.floating], NDArray[np.floating]]:
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
    # Transfer to backend
    k = backend.array(coupling_coefficients)
    lengths = backend.array(parallel_lengths)
    rise_times = backend.array(rise_times_ns)
    eps_eff = backend.array(effective_dielectric)

    # Phase velocity: v_p = c / sqrt(eps_eff) in m/s
    sqrt_eps = backend.sqrt(eps_eff)
    v_p = SPEED_OF_LIGHT / sqrt_eps

    # Rise distance in mm: rise_time (ns) * v_p (m/s) * 1e-6 = mm
    rise_distance = rise_times * v_p * 1e-6

    # Saturation length (mm)
    lsat = rise_distance / 2

    # NEXT coefficient (saturates at k/2)
    kb_max = k / 2

    # Calculate NEXT with saturation using numpy operations for conditional
    k_np = backend.to_numpy(k)
    lengths_np = backend.to_numpy(lengths)
    lsat_np = backend.to_numpy(lsat)
    kb_max_np = backend.to_numpy(kb_max)

    # NEXT: saturated or linear region
    next_coeff = np.where(
        lengths_np >= lsat_np,
        kb_max_np,  # Saturated
        kb_max_np * (lengths_np / (lsat_np + 1e-9)),  # Linear
    )
    next_coeff = np.clip(next_coeff, 0.0, 1.0)

    # FEXT coefficient: Kf = 2 * k * L / rise_distance
    rise_distance_np = backend.to_numpy(rise_distance)
    fext_coeff = 2 * k_np * lengths_np / (rise_distance_np + 1e-9)
    fext_coeff = np.clip(fext_coeff, 0.0, 1.0)

    # Convert to percentages
    next_percent = next_coeff * 100.0
    fext_percent = fext_coeff * 100.0

    return next_percent, fext_percent


def calculate_pairwise_distances(
    positions: NDArray[np.floating],
    backend: ArrayBackend,
) -> NDArray[np.floating]:
    """Calculate pairwise Euclidean distances between positions.

    Args:
        positions: (N, 2) array of (x, y) positions in mm.
        backend: Array backend for computation.

    Returns:
        (N, N) symmetric distance matrix in mm.
    """
    # Transfer to backend
    pos = backend.array(positions)

    # Broadcasting: dist[i,j] = sqrt((xi-xj)^2 + (yi-yj)^2)
    pos_i = backend.expand_dims(pos, 1)  # (N, 1, 2)
    pos_j = backend.expand_dims(pos, 0)  # (1, N, 2)

    diff = pos_i - pos_j  # (N, N, 2)
    dist_sq = backend.sum(diff * diff, axis=2)  # (N, N)
    distances = backend.sqrt(dist_sq)

    return backend.to_numpy(distances)


def estimate_parallel_lengths(
    trace_endpoints: NDArray[np.floating],
    backend: ArrayBackend,
) -> NDArray[np.floating]:
    """Estimate parallel routing lengths between trace pairs.

    Uses bounding box overlap as a proxy for parallel routing length.
    This is a heuristic - actual parallel length depends on routing.

    Args:
        trace_endpoints: (N, 4) array of [x1, y1, x2, y2] endpoints in mm.
        backend: Array backend for computation.

    Returns:
        (N, N) matrix of estimated parallel lengths in mm.
    """
    xp = backend.xp

    # Transfer to backend
    endpoints = backend.array(trace_endpoints)

    # Extract coordinates
    x1 = endpoints[:, 0]
    y1 = endpoints[:, 1]
    x2 = endpoints[:, 2]
    y2 = endpoints[:, 3]

    # Bounding boxes (min/max for each trace)
    x_min = xp.minimum(x1, x2)
    x_max = xp.maximum(x1, x2)
    y_min = xp.minimum(y1, y2)
    y_max = xp.maximum(y1, y2)

    # Expand for pairwise comparison
    x_min_i = backend.expand_dims(x_min, 1)
    x_max_i = backend.expand_dims(x_max, 1)
    y_min_i = backend.expand_dims(y_min, 1)
    y_max_i = backend.expand_dims(y_max, 1)

    x_min_j = backend.expand_dims(x_min, 0)
    x_max_j = backend.expand_dims(x_max, 0)
    y_min_j = backend.expand_dims(y_min, 0)
    y_max_j = backend.expand_dims(y_max, 0)

    # Calculate overlaps
    overlap_x = xp.maximum(
        backend.array([0.0]),
        xp.minimum(x_max_i, x_max_j) - xp.maximum(x_min_i, x_min_j),
    )
    overlap_y = xp.maximum(
        backend.array([0.0]),
        xp.minimum(y_max_i, y_max_j) - xp.maximum(y_min_i, y_min_j),
    )

    # Parallel length is the larger overlap dimension
    parallel_lengths = xp.maximum(overlap_x, overlap_y)

    # Zero diagonal (no self-coupling)
    parallel_lengths = backend.fill_diagonal(parallel_lengths, 0.0)

    return backend.to_numpy(parallel_lengths)


def classify_crosstalk_risk(
    next_percent: NDArray[np.floating],
    fext_percent: NDArray[np.floating],
) -> NDArray[np.int_]:
    """Classify crosstalk risk level for trace pairs.

    Risk levels (based on IPC-2141A recommendations):
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

    # Classify based on thresholds
    risk = np.zeros_like(max_xt, dtype=np.int_)
    risk[max_xt >= 3.0] = 1  # Marginal
    risk[max_xt >= 10.0] = 2  # Excessive

    return risk


@dataclass
class SignalIntegrityGPUAccelerator:
    """GPU-accelerated signal integrity analysis.

    Provides batched calculation of crosstalk and impedance metrics
    using GPU acceleration when available.

    Attributes:
        backend: Array backend for GPU/CPU computation.

    Example::

        from kicad_tools.acceleration import get_best_available_backend
        from kicad_tools.acceleration.kernels.signal_integrity import (
            SignalIntegrityGPUAccelerator,
        )
        import numpy as np

        backend = get_best_available_backend()
        accelerator = SignalIntegrityGPUAccelerator(backend)

        # Analyze crosstalk for 100 trace pairs
        separations = np.random.rand(100, 100) * 2.0
        lengths = np.random.rand(100, 100) * 20.0

        result = accelerator.analyze_crosstalk(separations, lengths)
    """

    backend: ArrayBackend

    def analyze_crosstalk(
        self,
        separations: NDArray[np.floating],
        parallel_lengths: NDArray[np.floating],
    ) -> dict[str, NDArray[np.floating]]:
        """Analyze crosstalk for all trace pairs.

        Args:
            separations: (N, N) array of edge-to-edge spacings in mm.
            parallel_lengths: (N, N) array of parallel run lengths in mm.

        Returns:
            Dictionary with:
                - coupling: (N, N) coupling coefficient matrix
                - max_coupling: (N,) maximum coupling for each trace
                - risk: (N,) risk level for each trace (0, 1, or 2)
        """
        coupling = calculate_crosstalk_matrix(
            separations,
            parallel_lengths,
            self.backend,
        )

        # Maximum coupling for each trace (across all other traces)
        max_coupling = np.max(coupling, axis=1)

        # Convert to percentage for risk classification
        max_coupling_pct = max_coupling * 100

        # Risk classification (using max coupling as proxy for crosstalk)
        risk = classify_crosstalk_risk(max_coupling_pct, max_coupling_pct)

        return {
            "coupling": coupling,
            "max_coupling": max_coupling,
            "risk": risk,
        }

    def calculate_impedances(
        self,
        widths: NDArray[np.floating],
        heights: NDArray[np.floating],
        dielectric_constants: NDArray[np.floating],
    ) -> NDArray[np.floating]:
        """Calculate characteristic impedance for multiple traces.

        Args:
            widths: (N,) array of trace widths in mm.
            heights: (N,) array of dielectric heights in mm.
            dielectric_constants: (N,) array of relative permittivity.

        Returns:
            (N,) array of characteristic impedances in ohms.
        """
        return calculate_impedance_batch(
            widths,
            heights,
            dielectric_constants,
            self.backend,
        )

    def analyze_net_pair(
        self,
        coupling_coefficient: float,
        parallel_length_mm: float,
        rise_time_ns: float = 1.0,
        effective_dielectric: float = 3.5,
    ) -> dict[str, float]:
        """Analyze crosstalk for a single net pair.

        Convenience method for analyzing individual net pairs.

        Args:
            coupling_coefficient: Coupling coefficient (0-1).
            parallel_length_mm: Parallel run length in mm.
            rise_time_ns: Signal rise time in nanoseconds.
            effective_dielectric: Effective dielectric constant.

        Returns:
            Dictionary with NEXT%, FEXT%, and risk level.
        """
        # Create single-element arrays
        k = np.array([coupling_coefficient])
        length = np.array([parallel_length_mm])
        rise = np.array([rise_time_ns])
        eps = np.array([effective_dielectric])

        next_pct, fext_pct = calculate_next_fext_batch(
            k, length, rise, eps, self.backend
        )
        risk = classify_crosstalk_risk(next_pct, fext_pct)

        return {
            "next_percent": float(next_pct[0]),
            "fext_percent": float(fext_pct[0]),
            "risk": int(risk[0]),
            "risk_label": ["acceptable", "marginal", "excessive"][risk[0]],
        }
