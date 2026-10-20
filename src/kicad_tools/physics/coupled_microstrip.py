"""Quasi-static even/odd-mode solve for edge-coupled microstrip (#7060).

Two identical rectangular strips of width ``w``, thickness ``t`` and edge gap
``s`` sit on a dielectric slab (relative permittivity ``er``, height ``h``)
over an infinite ground plane, with air above. The problem is solved by the
boundary-element method in units where lengths are normalised by ``h``:

* The ground plane (y = 0) is handled with image charges, so the free-space
  kernel is ``G = -(ln r - ln r_image) / (2 pi)``.
* Free charge ``q`` sits on every strip face (for ``t = 0``, on the strip
  sheet). Polarisation charge ``p`` sits on the whole dielectric-air interface
  (y = h), truncated at a distance well past the strips, including under the
  strips.
* Interface condition: with ``E'`` the normal field of all *other* charge,
  ``p = 2 (er - 1) / (er + 1) * (E' - q_coincident / 2)``. For a thick strip
  the bottom face and the interface share the same panels, so the
  ``q_coincident / 2`` term is exact; ``q_coincident`` is the strip's own free
  charge at the same location.
* Panel integrals use the exact closed forms for the potential and field of a
  uniformly charged straight segment, so nearly touching panels and the
  collinear interface need no quadrature.
* Unit potentials (+1, +1) give the even mode and (+1, -1) the odd mode.
  ``C_m`` is the free charge on one strip per unit potential. With ``C_m,air``
  from the same geometry with ``er = 1``, ``eps_eff = C_m / C_m,air`` and
  ``Z0m = 1 / (c * sqrt(C_m * C_m,air))``.

Panels are cosine-graded toward the strip edges, where the charge density is
integrably singular.

Method references (formulation only; no third-party code was consulted):
    R. F. Harrington, "Field Computation by Moment Methods", Macmillan,
    1968, ch. 2 (charge integral equations); the polarisation-charge
    interface condition is the standard bound-charge formulation, e.g.
    Jackson, "Classical Electrodynamics", sec. 4.4.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from .constants import SPEED_OF_LIGHT, VACUUM_PERMITTIVITY

_EPS = 1e-12
# Panels per strip face, per strip wall, in the whole gap (split evenly over
# the two strip edges) and in each outer interface tail. Sized so the
# single-ended limit (s >> h) is within ~0.5 % of independent finite-difference
# anchors for 1 oz copper (#7164); the coarser 40/24/40/50 set read 0.6-2.5 %
# high because the gap panels did not resolve the interface charge next to the
# strip edges.
_FACE_PANELS = 50
_WALL_PANELS = 28
_GAP_PANELS = 48
_TAIL_PANELS = 70
# Interface tail length beyond the strips, in units of h. The polarisation
# charge decays like 1/x^2 against the ground plane, so the truncated tail
# contributes well under 0.1 % to the capacitances.
_TAIL_LENGTH = 25.0


def _graded(first: float, last: float, count: int) -> np.ndarray:
    """Nodes from ``first`` to ``last`` clustered toward both ends."""
    fractions = (1 - np.cos(np.linspace(0, math.pi, count + 1))) / 2
    return first + (last - first) * fractions


def _edge_graded(length: float, first: float, count: int) -> np.ndarray:
    """Nodes from 0 to ``length`` in ``count`` panels growing geometrically.

    The first panel has size ``first`` and each next one is ``r`` times larger,
    with ``r`` chosen so the panels span ``length`` exactly. Falls back to a
    uniform split when ``first`` is already no smaller than the uniform size.
    """
    if first * count >= length:
        return np.linspace(0, length, count + 1)
    lo, hi = 1.0 + 1e-9, 50.0
    for _ in range(100):
        ratio = (lo + hi) / 2
        if first * (ratio**count - 1) / (ratio - 1) < length:
            lo = ratio
        else:
            hi = ratio
    nodes = first * np.cumsum(np.concatenate([[0.0], ratio ** np.arange(count)]))
    scaled: np.ndarray = nodes * (length / nodes[-1])
    return scaled


def _segment_terms(
    points: np.ndarray, a: np.ndarray, b: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Potential and field of unit-density segments a->b at ``points``.

    Returns ``(phi, field)`` with shapes ``(n, m)`` and ``(n, m, 2)``, where
    ``phi = -(1/2pi) * integral(ln r)`` and ``field = -grad(phi)``. Points on
    the segment's own line get the principal-value normal field, i.e. zero.
    """
    d = b - a
    length = np.linalg.norm(d, axis=1)
    tangent = d / length[:, None]
    normal = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)
    rel = points[:, None, :] - a[None, :, :]
    u = np.einsum("nmk,mk->nm", rel, tangent)
    v = np.einsum("nmk,mk->nm", rel, normal)
    s0, s1 = -u, length[None, :] - u
    off_line = np.abs(v) > _EPS
    safe_v = np.where(off_line, v, 1.0)

    def antiderivative(s: np.ndarray) -> np.ndarray:
        r_sq = s * s + v * v
        log_term = np.where(r_sq > 0, 0.5 * s * np.log(np.where(r_sq > 0, r_sq, 1.0)), 0.0)
        result: np.ndarray = log_term - s + np.where(off_line, v * np.arctan(s / safe_v), 0.0)
        return result

    phi = -(antiderivative(s1) - antiderivative(s0)) / (2 * math.pi)
    r0_sq, r1_sq = s0 * s0 + v * v, s1 * s1 + v * v
    along = 0.5 * (
        np.log(np.where(r0_sq > 0, r0_sq, 1.0)) - np.log(np.where(r1_sq > 0, r1_sq, 1.0))
    )
    across = np.where(off_line, np.arctan(s1 / safe_v) - np.arctan(s0 / safe_v), 0.0)
    field = (along[..., None] * tangent[None] + across[..., None] * normal[None]) / (2 * math.pi)
    return phi, field


def _influence(points: np.ndarray, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Potential and field at ``points`` of segments plus ground-plane images."""
    mirror = np.array([1.0, -1.0])
    phi, field = _segment_terms(points, a, b)
    phi_img, field_img = _segment_terms(points, a * mirror, b * mirror)
    return phi - phi_img, field - field_img


def _strip_panels(x0: float, x1: float, t: float, bottom_nodes: np.ndarray):
    """Free-charge panels of one strip: bottom face, then (t>0) wall/top/wall."""
    nodes = [bottom_nodes[:, None] * np.array([1.0, 0.0]) + np.array([0.0, 1.0])]
    if t > 0:
        wall = np.linspace(0, 1, _WALL_PANELS + 1)
        wall = (1 - np.cos(math.pi * wall)) / 2
        top = _graded(x1, x0, _FACE_PANELS)
        nodes += [
            np.stack([np.full_like(wall, x1), 1 + t * wall], axis=1),
            np.stack([top, np.full_like(top, 1 + t)], axis=1),
            np.stack([np.full_like(wall, x0), 1 + t * (1 - wall)], axis=1),
        ]
    starts = np.vstack([n[:-1] for n in nodes])
    ends = np.vstack([n[1:] for n in nodes])
    return starts, ends


def _geometry(u: float, g: float, t: float):
    """Return free-charge panels, interface panels and bookkeeping indices."""
    xa, xb = g / 2, g / 2 + u
    left_bottom = _graded(-xb, -xa, _FACE_PANELS)
    right_bottom = _graded(xa, xb, _FACE_PANELS)
    fraction = np.linspace(0, 1, _TAIL_PANELS + 1)
    left_tail = -xb - _TAIL_LENGTH * (1 - fraction) ** 3
    right_tail = xb + _TAIL_LENGTH * fraction**3
    # The gap carries interface charge that peaks at both strip edges: start
    # from the size of the strip's own edge panels and grow toward the middle,
    # so the resolution holds as the gap widens (single-ended limit).
    half_gap = _edge_graded(xa, u * (math.pi / _FACE_PANELS) ** 2 / 4, _GAP_PANELS // 2)
    gap = np.concatenate([-xa + half_gap[:-1], (xa - half_gap)[::-1]])
    # Interface nodes, ascending in x; the strip bottoms are shared exactly.
    x = np.concatenate([left_tail[:-1], left_bottom[:-1], gap[:-1], right_bottom[:-1], right_tail])
    interface_a = np.stack([x[:-1], np.ones(len(x) - 1)], axis=1)
    interface_b = np.stack([x[1:], np.ones(len(x) - 1)], axis=1)
    bottoms = []
    offset = len(left_tail) - 1
    bottoms.append(offset)
    offset += len(left_bottom) - 1 + len(gap) - 1
    bottoms.append(offset)

    strips = [_strip_panels(-xb, -xa, t, left_bottom), _strip_panels(xa, xb, t, right_bottom)]
    # Right strip's bottom panels ascend from xa; left strip's from -xb.
    free_a = np.vstack([strips[0][0], strips[1][0]])
    free_b = np.vstack([strips[0][1], strips[1][1]])
    per_strip = len(strips[0][0])
    # coincident[j] = interface panel index sharing free panel j (bottom faces).
    coincident = np.full(len(free_a), -1)
    for strip, start in enumerate(bottoms):
        coincident[strip * per_strip : strip * per_strip + _FACE_PANELS] = start + np.arange(
            _FACE_PANELS
        )
    return free_a, free_b, interface_a, interface_b, coincident, per_strip


def _solve(u: float, g: float, t: float, er: float) -> tuple[float, float]:
    """Return (even, odd) free charge on one strip per unit potential (/eps0)."""
    free_a, free_b, int_a, int_b, coincident, per_strip = _geometry(u, g, t)
    n_free = len(free_a)
    free_mid = (free_a + free_b) / 2
    free_len = np.linalg.norm(free_b - free_a, axis=1)
    potentials = np.zeros((n_free, 2))
    potentials[:per_strip] = 1.0
    potentials[per_strip:, 0] = 1.0
    potentials[per_strip:, 1] = -1.0

    phi_ff, _ = _influence(free_mid, free_a, free_b)
    if er == 1.0:
        q = np.linalg.solve(phi_ff, potentials)
    else:
        n_int = len(int_a)
        int_mid = (int_a + int_b) / 2
        phi_fp, _ = _influence(free_mid, int_a, int_b)
        _, field_pf = _influence(int_mid, free_a, free_b)
        _, field_pp = _influence(int_mid, int_a, int_b)
        kappa = 2 * (er - 1) / (er + 1)
        e_pf, e_pp = field_pf[..., 1], field_pp[..., 1]
        half_self = np.zeros((n_int, n_free))
        shared = np.nonzero(coincident >= 0)[0]
        half_self[coincident[shared], shared] = 0.5
        matrix = np.block(
            [
                [phi_ff, phi_fp],
                [-kappa * (e_pf - half_self), np.eye(n_int) - kappa * e_pp],
            ]
        )
        rhs = np.vstack([potentials, np.zeros((n_int, 2))])
        q = np.linalg.solve(matrix, rhs)[:n_free]
    charge = free_len[:per_strip] @ q[:per_strip]
    if not np.all(np.isfinite(charge)) or np.any(charge <= 0):
        raise ValueError("Coupled microstrip solve did not produce positive finite capacitance")
    return float(charge[0]), float(charge[1])


@lru_cache(maxsize=512)
def _cached_solve(u: float, g: float, t: float, er: float) -> tuple[float, float]:
    return _solve(u, g, t, er)


def coupled_microstrip_modes(
    w: float, s: float, h: float, er: float, t: float
) -> tuple[float, float, float, float]:
    """Return ``(z0_even, z0_odd, eps_eff_even, eps_eff_odd)`` for a coupled pair.

    ``w``, ``s``, ``h`` and ``t`` share one length unit: strip width, edge gap,
    dielectric height to the ground plane and copper thickness.
    """
    if any(not math.isfinite(v) or v <= 0 for v in (w, s, h, er)):
        raise ValueError("Coupled microstrip width, gap, height and permittivity must be positive")
    if not math.isfinite(t) or t < 0:
        raise ValueError("Coupled microstrip copper thickness must be finite and nonnegative")
    # Rounding keeps the router's bisection probes cache-friendly; the
    # impedance varies far less than 1e-6 over this quantisation.
    u, g, tn = (round(v / h, 6) for v in (w, s, t))
    er = round(er, 6)
    with_dielectric = _cached_solve(u, g, tn, er)
    in_air = _cached_solve(u, g, tn, 1.0)
    scale = SPEED_OF_LIGHT * VACUUM_PERMITTIVITY
    z0 = [
        1 / (scale * math.sqrt(c * c_air)) for c, c_air in zip(with_dielectric, in_air, strict=True)
    ]
    eps_eff = [c / c_air for c, c_air in zip(with_dielectric, in_air, strict=True)]
    return z0[0], z0[1], eps_eff[0], eps_eff[1]
