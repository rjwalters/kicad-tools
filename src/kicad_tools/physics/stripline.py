"""Quasi-static capacitance of a rectangular strip between reference planes.

The homogeneous dielectric model uses a two-dimensional boundary-element
solution, rather than extending a centered-stripline formula to offset traces.
Planes are infinite, the conductor has rectangular cross-section, and nearby
signal conductors are absent. Dielectric layering and conductor loss are not
solved here.

For planes y=0,H, the dimensionless Dirichlet Green function is

    G = log((cosh(pi*dx/H)-cos(pi*(y+ys)/H)) /
            (cosh(pi*dx/H)-cos(pi*(y-ys)/H))) / (4*pi).

It satisfies -Laplacian(G)=delta, vanishes at both planes, and has the local
singularity -log(r)/(2*pi). Constant-charge panels enforce unit conductor
potential; their total charge gives C/epsilon. This also preserves the finite
single-plane limit as the other plane recedes (#5016).

Kernel reference: Khélifa and Chorfi (2019), section 2.3,
https://doi.org/10.1186/s13661-019-1245-6 .
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from .constants import SPEED_OF_LIGHT, VACUUM_PERMITTIVITY


def stripline_impedance(w: float, h1: float, h2: float, t: float, er: float) -> float:
    """Return impedance in ohms; dimensions share any consistent length unit.

    ``h1`` and ``h2`` are clear dielectric gaps from the conductor faces to
    their reference planes. ``t=0`` is supported as a single thin strip.
    The geometry solution is cached and permittivity scales it by 1/sqrt(er).
    """
    if any(not math.isfinite(v) or v <= 0 for v in (w, h1, h2, er)):
        raise ValueError("Stripline width, gaps and permittivity must be finite and positive")
    if not math.isfinite(t) or t < 0:
        raise ValueError("Stripline copper thickness must be finite and nonnegative")
    # Reflection is physically identical and should share cached calculations.
    near, far = sorted((h1, h2))
    return _vacuum_impedance(w, near, far, t) / math.sqrt(er)


def _strip_panels(x_center: float, w: float, h1: float, t: float) -> tuple[np.ndarray, np.ndarray]:
    """Cosine-graded panel end points around one strip, in normalized units."""
    # Cosine grading resolves the integrable edge/corner charge singularities.
    panel_count = 48 if t > 0 else 96
    fractions = (1 - np.cos(np.linspace(0, math.pi, panel_count + 1))) / 2
    left, right = x_center - w / 2, x_center + w / 2
    edges = [((left, h1), (right, h1))]
    if t > 0:
        edges += [
            ((right, h1), (right, h1 + t)),
            ((right, h1 + t), (left, h1 + t)),
            ((left, h1 + t), (left, h1)),
        ]
    starts, ends = [], []
    for first, last in edges:
        first_point, last_point = np.asarray(first), np.asarray(last)
        points = first_point + fractions[:, None] * (last_point - first_point)
        starts.extend(points[:-1])
        ends.extend(points[1:])
    return np.asarray(starts), np.asarray(ends)


def _panel_matrix(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return the Green-function matrix and panel lengths for panels a->b."""
    mid = (a + b) / 2
    lengths = np.linalg.norm(b - a, axis=1)
    nodes, weights = np.polynomial.legendre.leggauss(24)
    sources = mid[:, None, :] + (b - a)[:, None, :] * nodes[None, :, None] / 2
    dx = mid[:, None, None, 0] - sources[None, :, :, 0]
    y, ys = mid[:, None, None, 1], sources[None, :, :, 1]
    # Half-angle form avoids subtracting nearly equal cosh/cos values.
    # Logarithmic evaluation also avoids overflow for very wide conductors.
    distance = np.abs(math.pi * dx / 2)
    with np.errstate(divide="ignore"):
        log_sinh_sq = 2 * (distance + np.log(-np.expm1(-2 * distance)) - math.log(2))
        plus = 2 * np.log(np.abs(np.sin(math.pi * (y + ys) / 2)))
        minus = 2 * np.log(np.abs(np.sin(math.pi * (y - ys) / 2)))
    green = (np.logaddexp(log_sinh_sq, plus) - np.logaddexp(log_sinh_sq, minus)) / (4 * math.pi)
    matrix = np.sum(green * weights[None, None, :], axis=2) * lengths[None, :] / 2
    # Replace the quadrature of the logarithmic self singularity with its
    # analytic integral. The smooth image contribution retains quadrature.
    sampled_self = (
        -np.sum(np.log(np.abs(nodes)[None, :] * lengths[:, None] / 2) * weights, axis=1)
        * lengths
        / (4 * math.pi)
    )
    exact_self = lengths * (1 - np.log(lengths / 2)) / (2 * math.pi)
    matrix[np.diag_indices_from(matrix)] += exact_self - sampled_self
    return matrix, lengths


@lru_cache(maxsize=256)
def _vacuum_impedance(w: float, h1: float, h2: float, t: float) -> float:
    """Solve a graded panel discretization of the conductor boundary."""
    height = h1 + h2 + t
    w, h1, t = w / height, h1 / height, t / height
    a, b = _strip_panels(0.0, w, h1, t)
    matrix, lengths = _panel_matrix(a, b)
    density = np.linalg.solve(matrix, np.ones(len(lengths)))
    capacitance_over_epsilon = float(density @ lengths)
    if not math.isfinite(capacitance_over_epsilon) or capacitance_over_epsilon <= 0:
        raise ValueError("Stripline capacitance solve did not produce a positive finite result")
    return 1 / (SPEED_OF_LIGHT * VACUUM_PERMITTIVITY * capacitance_over_epsilon)


def coupled_stripline_impedances(
    w: float, s: float, h1: float, h2: float, t: float, er: float
) -> tuple[float, float]:
    """Return (Z0 even, Z0 odd) in ohms of two identical edge-coupled strips.

    ``s`` is the edge-to-edge gap; other arguments are as for
    :func:`stripline_impedance`. The strips are solved together by the same
    boundary-element method: unit potentials (+1, +1) give the even mode and
    (+1, -1) the odd mode, and ``Z0m = 1 / (c * C_m)`` with ``C_m`` the charge
    on one strip per unit potential in the homogeneous dielectric (#7060).
    """
    if any(not math.isfinite(v) or v <= 0 for v in (w, s, h1, h2, er)):
        raise ValueError("Coupled stripline width, gap, heights and permittivity must be positive")
    if not math.isfinite(t) or t < 0:
        raise ValueError("Coupled stripline copper thickness must be finite and nonnegative")
    near, far = sorted((h1, h2))
    even, odd = _vacuum_coupled_impedances(w, s, near, far, t)
    root_er = math.sqrt(er)
    return even / root_er, odd / root_er


@lru_cache(maxsize=256)
def _vacuum_coupled_impedances(
    w: float, s: float, h1: float, h2: float, t: float
) -> tuple[float, float]:
    height = h1 + h2 + t
    w, s, h1, t = w / height, s / height, h1 / height, t / height
    pitch = (w + s) / 2
    first = _strip_panels(-pitch, w, h1, t)
    second = _strip_panels(pitch, w, h1, t)
    a = np.vstack([first[0], second[0]])
    b = np.vstack([first[1], second[1]])
    matrix, lengths = _panel_matrix(a, b)
    count = len(first[0])
    potentials = np.array([[1.0, 1.0], [1.0, -1.0]]).repeat([count, count], axis=0)
    density = np.linalg.solve(matrix, potentials)
    charge = lengths[:count] @ density[:count]  # (even, odd) charge on strip one
    if not np.all(np.isfinite(charge)) or np.any(charge <= 0):
        raise ValueError("Coupled stripline solve did not produce positive finite capacitance")
    scale = SPEED_OF_LIGHT * VACUUM_PERMITTIVITY
    return float(1 / (scale * charge[0])), float(1 / (scale * charge[1]))
