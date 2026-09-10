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


@lru_cache(maxsize=256)
def _vacuum_impedance(w: float, h1: float, h2: float, t: float) -> float:
    """Solve a graded panel discretization of the conductor boundary."""
    height = h1 + h2 + t
    w, h1, t = w / height, h1 / height, t / height
    # Cosine grading resolves the integrable edge/corner charge singularities.
    panel_count = 48 if t > 0 else 96
    fractions = (1 - np.cos(np.linspace(0, math.pi, panel_count + 1))) / 2
    edges = [((-w / 2, h1), (w / 2, h1))]
    if t > 0:
        edges += [
            ((w / 2, h1), (w / 2, h1 + t)),
            ((w / 2, h1 + t), (-w / 2, h1 + t)),
            ((-w / 2, h1 + t), (-w / 2, h1)),
        ]
    starts, ends = [], []
    for first, last in edges:
        first, last = np.asarray(first), np.asarray(last)
        points = first + fractions[:, None] * (last - first)
        starts.extend(points[:-1])
        ends.extend(points[1:])
    a, b = np.asarray(starts), np.asarray(ends)
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
    density = np.linalg.solve(matrix, np.ones(len(mid)))
    capacitance_over_epsilon = float(density @ lengths)
    if not math.isfinite(capacitance_over_epsilon) or capacitance_over_epsilon <= 0:
        raise ValueError("Stripline capacitance solve did not produce a positive finite result")
    return 1 / (SPEED_OF_LIGHT * VACUUM_PERMITTIVITY * capacitance_over_epsilon)
