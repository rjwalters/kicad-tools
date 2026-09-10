"""Independent finite-volume vacuum capacitance, uniform-Er scaling; no BEM code."""

import json
import math
import time

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve

EPS0 = 8.8541878128e-12
C = 299792458


def axis(intervals):
    return np.concatenate(
        [np.linspace(a, b, int(np.ceil((b - a) / s)) + 1)[:-1] for a, b, s in intervals]
        + [[intervals[-1][1]]]
    )


def solve(w, t, h1, h2, step, span=2.5, er=4.5):
    H = h1 + t + h2
    edge = 0.35
    x = axis(
        [
            (-span, -edge, 4 * step),
            (-edge, -w / 2, step),
            (-w / 2, w / 2, step),
            (w / 2, edge, step),
            (edge, span, 4 * step),
        ]
    )
    ranges = [(0, h1, step), (h1, h1 + t, step)]
    if h2 > 0.35:
        ranges.extend([(h1 + t, h1 + t + 0.35, step), (h1 + t + 0.35, H, 4 * step)])
    else:
        ranges.append((h1 + t, H, step))
    y = axis(ranges)
    nx = len(x)
    ny = len(y)
    dx = np.diff(x)
    dy = np.diff(y)
    wx = np.r_[dx[0] / 2, (dx[:-1] + dx[1:]) / 2, dx[-1] / 2]
    wy = np.r_[dy[0] / 2, (dy[:-1] + dy[1:]) / 2, dy[-1] / 2]
    indices = np.arange(nx * ny).reshape(ny, nx)
    trace = (
        (np.abs(x)[None, :] <= w / 2 + 1e-10)
        & (y[:, None] >= h1 - 1e-10)
        & (y[:, None] <= h1 + t + 1e-10)
    )
    fixed = trace.copy()
    fixed[[0, -1], :] = True
    fixed[:, [0, -1]] = True
    free = np.flatnonzero(~fixed.ravel())
    tr = np.flatnonzero(trace.ravel())
    gh = wy[:, None] / dx[None, :]
    gv = wx[None, :] / dy[:, None]
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
    cap = EPS0 * np.sum((L @ potential)[tr])
    return {
        "w": w,
        "t": t,
        "h1": h1,
        "h2": h2,
        "er": er,
        "step": step,
        "span": span,
        "nodes": nx * ny,
        "c0_pF_m": cap * 1e12,
        "z": 1 / (C * cap * math.sqrt(er)),
    }


if __name__ == "__main__":
    for args in [(0.16, 0.0152, 0.13, 1.078), (0.16, 0.0152, 0.13, 0.13), (0.2, 0.035, 0.2, 0.2)]:
        for step in [0.005, 0.0025, 0.00125]:
            start = time.time()
            r = solve(*args, step)
            r["seconds"] = time.time() - start
            print(json.dumps(r), flush=True)
    # Outer boundary convergence check independent of mesh refinement.
    print(json.dumps(solve(0.16, 0.0152, 0.13, 1.078, 0.0025, span=4.0)), flush=True)
