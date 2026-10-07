#!/usr/bin/env python3
"""A/B re-check of the FOM ``decoupling_proximity`` weight across a classifier change.

Issue #5984.  PR #5971 (issue #5939) switched
``optim.fom_electrical._looks_like_power_net`` from a substring match against
a hint list to the shared whole-token classifier
``router.net_class.is_power_rail_name``.  The ``decoupling_proximity`` weight
in ``src/kicad_tools/optim/weights/default.yaml`` was fitted against the old
classifier, so this script asks: *does the fitted weight move?*

It reuses ``calibrate_fom.py``'s exact perturbation policy (seed, sigma,
rotation probability, N) and scores every placement **once**, computing the
``decoupling_proximity`` column under both classifiers.  The two term caches
therefore differ *only* in that column -- a clean A/B with no
perturbation-sampling noise between arms.  On them it reports:

1. the raw term per board (committed vs perturbed median);
2. a 1-D scan of the decoupling weight with the other nine weights held at
   ``default.yaml``, scored with ``calibrate_fom``'s own selection objective;
3. the full NSGA-II Pareto re-fit (``calibrate_fom.pareto_sweep``) over
   several seeds, because the fitted weight is a pick from a rank-consistency
   plateau and a single seed is not evidence.

Speed: ``trace_length_excess`` calls the router's iterated 1-Steiner RSMT,
whose pure-Python Prim is O(n^3) per trial MST; the 44-pad GND net on board
03 takes ~2 min per placement, so 7 boards x 41 placements runs for hours.
By default this script installs a numpy Prim batched across all Hanan
candidates (same candidate order, same strict ``gain > best_gain`` rule).
It is *not* bit-identical in general: Prim's float summation order differs,
so on near-tied gains it can pick a different Steiner point (about 2% of
random small point sets in review, final tree length usually equal, at most
one 0.635 mm grid step apart).  On the calibration boards it matched the
production solver exactly: all 41x10 board-04 term values, board 03's GND
net (243.5965 mm both ways, 111 s vs 0.6 s), and spot-checks of boards 01,
02 and 06.  Only ``trace_length_excess`` uses it, a column shared by both
A/B arms.  ``--exact-steiner`` disables it.

Usage::

    uv sync --extra research
    uv run python scripts/research/fom_decoupling_ab.py --out /tmp/fom_ab
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate_fom as cf  # noqa: E402

from kicad_tools.optim import fom_electrical as fe  # noqa: E402
from kicad_tools.optim.fom import SOFT_TERM_NAMES, compute_soft_terms  # noqa: E402
from kicad_tools.optim.fom_features import extract_features  # noqa: E402
from kicad_tools.router.algorithms import steiner as st  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

DI = SOFT_TERM_NAMES.index("decoupling_proximity")
DEFAULT_YAML = Path("src/kicad_tools/optim/weights/default.yaml")

# The pre-#5971 substring heuristic, verbatim.
_OLD_POWER_RAIL_HINTS = (
    "VCC", "VDD", "+3V3", "+3V", "+5V", "+12V", "VBAT", "VBUS",
    "VAA", "AVDD", "DVDD", "VCCIO", "VDDIO", "PWR",
)  # fmt: skip


def old_looks_like_power_net(net_name: str) -> bool:
    name = (net_name or "").upper()
    return any(hint in name for hint in _OLD_POWER_RAIL_HINTS)


# ----------------------------------------------------------------------
# Measurement-only batched 1-Steiner (exact on the calibration boards; see module docstring)
# ----------------------------------------------------------------------

_exact_one_steiner = st._iterative_one_steiner


def _batched_mst_cost(points, candidates) -> np.ndarray:
    """Manhattan MST cost of ``points + [c]`` for every candidate ``c``."""
    p = np.asarray(points, float)
    cand = np.asarray(candidates, float)
    c_n, m = len(cand), len(p)
    x = np.concatenate([np.broadcast_to(p, (c_n, m, 2)), cand[:, None, :]], axis=1)
    rows = np.arange(c_n)
    connected = np.zeros((c_n, m + 1), bool)
    connected[:, 0] = True
    best = np.abs(x[:, :, 0] - x[:, :1, 0]) + np.abs(x[:, :, 1] - x[:, :1, 1])
    best[connected] = np.inf
    total = np.zeros(c_n)
    for _ in range(m):
        j = np.argmin(best, axis=1)
        total += best[rows, j]
        connected[rows, j] = True
        xj = x[rows, j]
        d = np.abs(x[:, :, 0] - xj[:, None, 0]) + np.abs(x[:, :, 1] - xj[:, None, 1])
        np.minimum(best, d, out=best)
        best[connected] = np.inf
    return total


def _batched_one_steiner(terminals, cost_fn=None, max_iterations=50):
    if cost_fn is not None:
        return _exact_one_steiner(terminals, cost_fn, max_iterations)
    all_points = list(terminals)
    current_cost = st._mst_cost(all_points, None)
    for _ in range(max_iterations):
        candidates = st._hanan_grid(all_points)
        if not candidates:
            break
        best_gain, best_candidate = 0.0, None
        for cand, trial in zip(
            candidates, _batched_mst_cost(all_points, candidates).tolist(), strict=True
        ):
            gain = current_cost - trial
            if gain > best_gain:
                best_gain, best_candidate = gain, cand
        if best_candidate is None or best_gain <= 0:
            break
        all_points.append(best_candidate)
        current_cost -= best_gain
    return all_points, st._build_mst_edges(all_points, None)


# ----------------------------------------------------------------------
# Collection
# ----------------------------------------------------------------------


def _terms_both(pcb: PCB) -> tuple[np.ndarray, np.ndarray]:
    """Score once; return (old-classifier terms, new-classifier terms)."""
    feats = extract_features(pcb)
    terms = compute_soft_terms(pcb, features=feats)
    new = np.array([terms[n] for n in SOFT_TERM_NAMES], dtype=float)
    current = fe._looks_like_power_net
    fe._looks_like_power_net = old_looks_like_power_net
    try:
        old_dec = fe.decoupling_proximity(feats)
    finally:
        fe._looks_like_power_net = current
    old = new.copy()
    old[DI] = old_dec
    return old, new


def collect(board: str, path: str, n: int, sigma: float, rot: float, seed: int, out: Path) -> None:
    dest = out / f"{board}.npz"
    if dest.exists():
        return
    old_c, new_c = _terms_both(PCB.load(path))
    old_p = np.zeros((n, len(SOFT_TERM_NAMES)))
    new_p = np.zeros_like(old_p)
    master = random.Random(seed)  # identical sequence to calibrate_fom
    for i in range(n):
        pcb = PCB.load(path)
        cf.perturb_pcb(
            pcb, random.Random(master.randint(0, 2**31 - 1)), sigma_mm=sigma, rotate_prob=rot
        )
        old_p[i], new_p[i] = _terms_both(pcb)
    np.savez(
        dest, old_committed=old_c, old_perturbed=old_p, new_committed=new_c, new_perturbed=new_p
    )
    print(f"collected {board}", flush=True)


# ----------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------


def _select_score(weights, cache, boards) -> float:
    """``calibrate_fom.pareto_sweep``'s selection score for one vector."""
    rc = np.array(
        [
            cf.rank_consistency(cache[b]["committed"], cache[b]["perturbed"], weights)
            * cf.saturation_penalty(
                float(weights @ cache[b]["committed"]), cache[b]["perturbed"] @ weights
            )
            for b in boards
        ]
    )
    rc = np.clip(rc, 1e-6, 1.0)
    return float(np.exp(np.log(rc).mean()) - 0.5 * rc.std())


def analyse(out: Path, seeds: list[int]) -> dict:
    caches: dict[str, dict] = {"old": {}, "new": {}}
    for board, _ in cf.BOARDS:
        d = np.load(out / f"{board}.npz")
        for arm in caches:
            caches[arm][board] = {
                "committed": d[f"{arm}_committed"],
                "perturbed": d[f"{arm}_perturbed"],
            }
    train = [b for b, _ in cf.BOARDS if b in cf.TRAIN_BOARDS]
    hold = [b for b, _ in cf.BOARDS if b in cf.HOLDOUT_BOARDS]
    cur = yaml.safe_load(DEFAULT_YAML.read_text())["weights"]
    w0 = np.array([cur[n] for n in SOFT_TERM_NAMES])
    report: dict = {
        "current_weight": cur["decoupling_proximity"],
        "raw": {},
        "rc_at_current": {},
        "scan_1d": {},
        "pareto": {},
    }

    print("raw decoupling_proximity: committed / perturbed median  (old -> new)")
    for b, _ in cf.BOARDS:
        row = {
            arm: (float(c[b]["committed"][DI]), float(np.median(c[b]["perturbed"][:, DI])))
            for arm, c in caches.items()
        }
        report["raw"][b] = row
        print(
            f"  {b:16s} {row['old'][0]:7.2f} / {row['old'][1]:7.2f}  ->  {row['new'][0]:7.2f} / {row['new'][1]:7.2f}"
        )

    print(
        f"rank consistency at default.yaml (decoupling={cur['decoupling_proximity']})  (old -> new)"
    )
    for b, _ in cf.BOARDS:
        rcs = {
            arm: cf.rank_consistency(c[b]["committed"], c[b]["perturbed"], w0)
            for arm, c in caches.items()
        }
        report["rc_at_current"][b] = rcs
        print(f"  {b:16s} {rcs['old']:.3f} -> {rcs['new']:.3f}")

    grid = np.power(10.0, np.linspace(-3, 1, 161))
    print(
        "1-D re-fit (other weights = default.yaml), calibrate_fom selection score on train boards"
    )
    for arm, c in caches.items():
        scores = []
        for dw in grid:
            w = w0.copy()
            w[DI] = dw
            scores.append(_select_score(w, c, train))
        scores_a = np.array(scores)
        best = float(scores_a.max())
        near = grid[scores_a >= best - 0.01]
        at_cur = _select_score(w0, c, train)
        report["scan_1d"][arm] = {
            "argmax": float(grid[int(scores_a.argmax())]),
            "best_score": best,
            "near_best_band": [float(near.min()), float(near.max())],
            "score_at_current": at_cur,
        }
        print(
            f"  {arm}: argmax={grid[int(scores_a.argmax())]:.4f} (score {best:.4f}); "
            f"within 0.01 of best: [{near.min():.4f}, {near.max():.4f}]; at current: {at_cur:.4f}"
        )

    print("NSGA-II Pareto re-fit (calibrate_fom.pareto_sweep, 80 gens x 80 pop)")
    for arm, c in caches.items():
        fits = []
        for s in seeds:
            gw, summary = cf.pareto_sweep(c, train, n_gens=80, pop_size=80, seed=s)
            h = cf.evaluate_weights(gw, c, hold)
            fits.append(
                {
                    "seed": s,
                    "decoupling_proximity": float(gw[DI]),
                    "train_geo_rc": summary["selected_geo_mean_rc"],
                    "holdout_mean_rc": float(np.mean([h[b]["rank_consistency"] for b in hold])),
                }
            )
        ws = [f["decoupling_proximity"] for f in fits]
        report["pareto"][arm] = {
            "fits": fits,
            "median": float(np.median(ws)),
            "geo_mean": float(np.exp(np.mean(np.log(ws)))),
            "range": [min(ws), max(ws)],
        }
        print(
            f"  {arm}: decoupling weight median={np.median(ws):.4f} "
            f"geo-mean={np.exp(np.mean(np.log(ws))):.4f} range=[{min(ws):.4f}, {max(ws):.4f}] over {len(seeds)} seeds"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--out", type=Path, required=True, help="Directory for per-board term caches + report JSON"
    )
    ap.add_argument("--perturbations", type=int, default=40)
    ap.add_argument("--sigma", type=float, default=2.5)
    ap.add_argument("--rotate-prob", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--pareto-seeds", default="42,1,2,3,4,5,6,7")
    ap.add_argument(
        "--board", action="append", help="Collect only this board (repeatable); skips analysis"
    )
    ap.add_argument(
        "--exact-steiner", action="store_true", help="Use the production (slow) 1-Steiner solver"
    )
    args = ap.parse_args(argv)

    if not args.exact_steiner:
        st._iterative_one_steiner = _batched_one_steiner
    args.out.mkdir(parents=True, exist_ok=True)
    for board, path in cf.BOARDS:
        if args.board and board not in args.board:
            continue
        collect(board, path, args.perturbations, args.sigma, args.rotate_prob, args.seed, args.out)
    if args.board:
        return 0
    report = analyse(args.out, [int(s) for s in args.pareto_seeds.split(",")])
    (args.out / "decoupling_ab_report.json").write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
