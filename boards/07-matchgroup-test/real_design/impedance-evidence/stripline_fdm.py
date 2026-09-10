"""Independent quasi-static 2D capacitance cross-check, all geometry in mm."""

import json
import sys
import time

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve

EPS0 = 8.8541878128e-12
C = 299792458
breaks = np.array([0, 0.13, 0.1452, 0.2616, 0.9616, 1.078, 1.0932, 1.2232])
eps = np.array([4.6, 4.16, 4.16, 4.6, 4.16, 4.16, 4.6])


def solve(width, step, span=2.5):
    x = np.unique(
        np.round(np.r_[np.arange(-span, span + step / 2, step), -width / 2, width / 2], 10)
    )
    y = np.concatenate(
        [
            np.linspace(a, b, int(np.ceil((b - a) / step)) + 1)[:-1]
            for a, b in zip(breaks[:-1], breaks[1:], strict=True)
        ]
        + [breaks[-1:]]
    )
    nx, ny = len(x), len(y)
    dx = np.diff(x)
    dy = np.diff(y)
    wx = np.r_[dx[0] / 2, (dx[:-1] + dx[1:]) / 2, dx[-1] / 2]
    indices = np.arange(nx * ny).reshape(ny, nx)
    trace = (
        (np.abs(x)[None, :] <= width / 2 + 1e-9)
        & (y[:, None] >= 0.13 - 1e-9)
        & (y[:, None] <= 0.1452 + 1e-9)
    )
    fixed = trace.copy()
    fixed[[0, -1], :] = True
    fixed[:, [0, -1]] = True
    free = np.flatnonzero(~fixed.ravel())
    tr = np.flatnonzero(trace.ravel())
    mids = (y[:-1] + y[1:]) / 2
    ers = eps[np.clip(np.searchsorted(breaks, mids, side="right") - 1, 0, len(eps) - 1)]
    caps = []
    for er_edge in [np.ones(len(dy)), ers]:
        wy = np.r_[
            er_edge[0] * dy[0] / 2,
            (er_edge[:-1] * dy[:-1] + er_edge[1:] * dy[1:]) / 2,
            er_edge[-1] * dy[-1] / 2,
        ]
        gh = wy[:, None] / dx[None, :]
        gv = er_edge[:, None] * wx[None, :] / dy[:, None]
        i = np.r_[indices[:, :-1].ravel(), indices[:-1, :].ravel()]
        j = np.r_[indices[:, 1:].ravel(), indices[1:, :].ravel()]
        g = np.r_[gh.ravel(), gv.ravel()]
        L = coo_matrix(
            (np.r_[g, g, -g, -g], (np.r_[i, j, i, j], np.r_[i, j, j, i])), shape=(nx * ny, nx * ny)
        ).tocsr()
        potential = np.zeros(nx * ny)
        potential[tr] = 1
        rhs = -(L @ potential)[free]
        potential[free] = spsolve(L[free][:, free], rhs)
        caps.append(EPS0 * np.sum((L @ potential)[tr]))
    c0, ce = caps
    return {
        "width_mm": width,
        "grid_mm": step,
        "half_domain_mm": span,
        "nodes": nx * ny,
        "vacuum_capacitance_pF_m": c0 * 1e12,
        "capacitance_pF_m": ce * 1e12,
        "effective_er": ce / c0,
        "impedance_ohm": 1 / (C * np.sqrt(c0 * ce)),
    }


if __name__ == "__main__":
    cases = [(0.15, 0.01), (0.16, 0.01), (0.18, 0.01), (0.19, 0.01), (0.16, 0.005)]
    if len(sys.argv) > 1:
        cases = [(float(sys.argv[1]), float(sys.argv[2]))]
    for w, h in cases:
        t = time.time()
        r = solve(w, h)
        r["seconds"] = time.time() - t
        print(json.dumps(r), flush=True)
