#!/usr/bin/env python3
"""Calibrate pre-route placement signals against recorded routing outcomes.

Issue #5948.  Question: how often does a cheap, pre-route placement signal
(RUDY peak / overflow, HPWL, bbox overlap, the optimizer's placement cost)
pick the *more routable* of two placements of the same board?

No routing is run here.  The labelled perturbation corpus produced for the
learned-FOM Phase 0 study (``data/research/fom_phase0/labels.jsonl``, see
``docs/research/learned_fom_phase0.md``) already holds, per sample, the RNG
seed / sigma / rotate-prob that generated it plus the routing outcome
(``route_ok``, ``drc_errors``).  We regenerate each perturbed placement
deterministically with ``generate_perturbations.perturb_pcb`` and score it.

Outcome severity (lower is better)::

    route failed / timed out  -> ROUTE_FAIL_SEVERITY
    routed                    -> drc_errors (0 == clean)

A *pair* is two samples of the same seed board with different severity.  A
signal is correct on a pair when it is lower for the lower-severity sample;
exact ties score 0.5.  Pairs never cross boards (signals are not comparable
across boards).

Usage::

    uv run python scripts/research/placement_score_calibration.py \\
        --labels data/research/fom_phase0/labels.jsonl \\
        --output data/research/placement_score/results.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]

ROUTE_FAIL_SEVERITY = 1000.0

# Signals under test; all are "higher = predicted harder to route".
SIGNALS = (
    "hpwl",
    "rudy_peak",
    "rudy_overflow",
    "overlap",
    "placement_cost",
    "steiner_signal_length",
)
# Signals that feed the fitted combination (steiner is a stored Phase 0 feature).
FIT_SIGNALS = ("hpwl", "rudy_peak", "rudy_overflow", "overlap")

# RUDY overflow capacity = this multiple of the seed placement's mean tile demand.
OVERFLOW_CAPACITY_FACTOR = 1.5
GRID_MARGIN_MM = 10.0
# Commit that added data/research/fom_phase0/labels.jsonl; seed boards are read
# as of this revision so the recorded perturbations reproduce exactly.
LABELS_REV = "83b0bc82"


def _load_perturber():
    path = REPO / "scripts" / "research" / "generate_perturbations.py"
    spec = importlib.util.spec_from_file_location("_gen_perturb", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_gen_perturb"] = mod
    spec.loader.exec_module(mod)
    return mod.perturb_pcb


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def severity(record: dict) -> float:
    """Routing-outcome severity of a labels.jsonl record (lower is better)."""
    if not record.get("route_ok") or record.get("drc_errors", -1) < 0:
        return ROUTE_FAIL_SEVERITY
    return float(record["drc_errors"])


def _fp_size(fp) -> tuple[float, float]:
    if not fp.pads:
        return (2.0, 2.0)
    xs: list[float] = []
    ys: list[float] = []
    for p in fp.pads:
        xs += [p.position[0] - p.size[0] / 2, p.position[0] + p.size[0] / 2]
        ys += [p.position[1] - p.size[1] / 2, p.position[1] + p.size[1] / 2]
    return (max(max(xs) - min(xs), 1.0), max(max(ys) - min(ys), 1.0))


def _collect(pcb):
    """Return (placements, sizes, nets, pad_xy, router_nets, router_pads)."""
    from kicad_tools.placement.cost import ComponentPlacement, Net
    from kicad_tools.router.primitives import Pad

    placements, sizes = [], {}
    pad_xy: dict[tuple[str, str], tuple[float, float]] = {}
    net_pins: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for fp in pcb.footprints:
        ref = fp.reference
        if not ref:
            continue
        placements.append(
            ComponentPlacement(ref, fp.position[0], fp.position[1], fp.rotation or 0.0)
        )
        sizes[ref] = _fp_size(fp)
        for pad in fp.pads:
            pos = pcb.get_pad_position(ref, pad.number)
            if pos is None:
                continue
            pad_xy[(ref, pad.number)] = pos
            if pad.net_name and pad.net_name != "unconnected":
                net_pins[pad.net_name].append((ref, pad.number))
    nets = [Net(name=n, pins=p) for n, p in net_pins.items() if len(p) >= 2]
    net_ids = {n.name: i + 1 for i, n in enumerate(nets)}
    router_nets = {net_ids[n.name]: list(n.pins) for n in nets}
    router_pads = {
        k: Pad(x=v[0], y=v[1], width=0.0, height=0.0, net=0, net_name="", ref=k[0], pin=k[1])
        for k, v in pad_xy.items()
    }
    return placements, sizes, nets, pad_xy, router_nets, router_pads


def _rudy(router_nets, router_pads, grid_box):
    from kicad_tools.router.congestion_estimator import CongestionEstimator

    x0, y0, x1, y1 = grid_box
    est = CongestionEstimator.from_nets(
        router_nets, router_pads, x0, y0, x1 - x0, y1 - y0, target_tiles=100
    )
    return np.array(est.demand, dtype=float)


def grid_box_for(pcb) -> tuple[float, float, float, float]:
    """Fixed per-board RUDY grid: seed pad extent plus a margin."""
    xs, ys = [], []
    for fp in pcb.footprints:
        for pad in fp.pads:
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos:
                xs.append(pos[0])
                ys.append(pos[1])
    m = GRID_MARGIN_MM
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def score_placement(pcb, grid_box, capacity: float) -> dict[str, float]:
    """Compute every pre-route signal for one (possibly perturbed) PCB."""
    from kicad_tools.placement.cost import (
        BoardOutline,
        DesignRuleSet,
        compute_overlap,
        compute_wirelength,
        evaluate_placement,
    )

    placements, sizes, nets, pad_xy, router_nets, router_pads = _collect(pcb)
    demand = _rudy(router_nets, router_pads, grid_box)
    outline = BoardOutline(*grid_box)
    cost = evaluate_placement(
        placements, nets, DesignRuleSet(), outline, footprint_sizes=sizes, pad_positions=pad_xy
    )
    return {
        "hpwl": compute_wirelength(placements, nets, pad_xy),
        "rudy_peak": float(demand.max()),
        "rudy_overflow": float(np.maximum(demand - capacity, 0.0).sum()),
        "overlap": compute_overlap(placements, sizes),
        "placement_cost": float(cost.total),
    }


# ---------------------------------------------------------------------------
# Pairwise ranking
# ---------------------------------------------------------------------------


def make_pairs(rows: list[dict]) -> list[tuple[int, int]]:
    """Index pairs (better, worse) within one board where severities differ."""
    out = []
    for i in range(len(rows)):
        for j in range(len(rows)):
            if rows[i]["severity"] < rows[j]["severity"]:
                out.append((i, j))
    return out


def pair_accuracy(values: np.ndarray, pairs: list[tuple[int, int]]) -> float:
    """Fraction of pairs where value[better] < value[worse]; ties = 0.5."""
    if not pairs:
        return float("nan")
    b = np.array([values[i] for i, _ in pairs])
    w = np.array([values[j] for _, j in pairs])
    return float(((b < w).sum() + 0.5 * (b == w).sum()) / len(pairs))


def _zscore(mat: np.ndarray) -> np.ndarray:
    sd = mat.std(axis=0)
    sd[sd == 0] = 1.0
    return (mat - mat.mean(axis=0)) / sd


def fit_pairwise(board_data: list[tuple[np.ndarray, list[tuple[int, int]]]]) -> np.ndarray:
    """Fit linear weights by logistic loss on (worse - better) feature deltas.

    Features are z-scored within each board.  Plain numpy gradient descent with
    L2 so there is no extra dependency; deterministic.
    """
    deltas = []
    for x, pairs in board_data:
        z = _zscore(x)
        deltas += [z[j] - z[i] for i, j in pairs]
    if not deltas:
        return np.zeros(len(FIT_SIGNALS))
    d = np.array(deltas)
    d = np.vstack([d, -d])  # symmetrise
    y = np.concatenate([np.ones(len(d) // 2), np.zeros(len(d) // 2)])
    w = np.zeros(d.shape[1])
    for _ in range(2000):
        p = 1.0 / (1.0 + np.exp(-d @ w))
        w -= 0.1 * (d.T @ (p - y) / len(y) + 0.01 * w)
    return w


def evaluate(rows_by_board: dict[str, list[dict]]) -> dict:
    """Per-signal and fitted-combination pairwise accuracy, with LOBO fit."""
    pairs_by_board = {b: make_pairs(r) for b, r in rows_by_board.items()}
    usable = [b for b, p in pairs_by_board.items() if p]
    res: dict = {"boards": {}, "pooled": {}, "n_pairs": {}}
    for b in rows_by_board:
        res["n_pairs"][b] = len(pairs_by_board[b])

    def acc_for(b, vals):
        return pair_accuracy(vals, pairs_by_board[b])

    for b in usable:
        rows = rows_by_board[b]
        res["boards"][b] = {s: acc_for(b, np.array([r[s] for r in rows])) for s in SIGNALS}

    feats = {b: np.array([[r[s] for s in FIT_SIGNALS] for r in rows_by_board[b]]) for b in usable}
    # Leave-one-board-out fitted combination.
    weights = {}
    for b in usable:
        train = [(feats[o], pairs_by_board[o]) for o in usable if o != b]
        w = fit_pairwise(train) if train else np.zeros(len(FIT_SIGNALS))
        weights[b] = dict(zip(FIT_SIGNALS, map(float, w), strict=True))
        res["boards"][b]["fitted_lobo"] = acc_for(b, _zscore(feats[b]) @ w)
    # In-sample fit (upper bound / optimism check).
    w_all = fit_pairwise([(feats[b], pairs_by_board[b]) for b in usable])
    for b in usable:
        res["boards"][b]["fitted_in_sample"] = acc_for(b, _zscore(feats[b]) @ w_all)
    res["weights_lobo"] = weights
    res["weights_in_sample"] = dict(zip(FIT_SIGNALS, map(float, w_all), strict=True))

    # Pair-weighted pooled accuracy across usable boards.
    keys = [*SIGNALS, "fitted_lobo", "fitted_in_sample"]
    tot = sum(len(pairs_by_board[b]) for b in usable)
    for k in keys:
        res["pooled"][k] = (
            sum(res["boards"][b][k] * len(pairs_by_board[b]) for b in usable) / tot
            if tot
            else float("nan")
        )
    res["pooled_n_pairs"] = tot
    return res


def bootstrap_ci(rows_by_board, key_fn, n=300, seed=0) -> tuple[float, float]:
    """95% sample-level bootstrap CI of pooled accuracy for a plain signal."""
    rng = random.Random(seed)
    stats = []
    for _ in range(n):
        num = den = 0.0
        for b, rows in rows_by_board.items():
            res = [rows[rng.randrange(len(rows))] for _ in rows]
            pairs = make_pairs(res)
            if not pairs:
                continue
            num += pair_accuracy(np.array([key_fn(r) for r in res]), pairs) * len(pairs)
            den += len(pairs)
        if den:
            stats.append(num / den)
    stats.sort()
    return (stats[int(0.025 * len(stats))], stats[int(0.975 * len(stats)) - 1])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _seed_from_git(rev: str, rel_path: str, tmp: Path) -> Path:
    """Materialise ``rel_path`` as of git revision ``rev`` under ``tmp``.

    The committed seed boards have been regenerated since the Phase 0 corpus
    was routed (e.g. charlieplex_3x3 grew from 14 to 19 footprints), so the
    perturbations only reproduce against the boards as they were when the
    labels were committed.
    """
    import subprocess

    out = tmp / rev / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        subprocess.run(
            ["git", "show", f"{rev}:{rel_path}"], cwd=REPO, check=True, capture_output=True
        ).stdout
    )
    return out


def build_rows(labels_path: Path, seed_rev: str = LABELS_REV) -> dict[str, list[dict]]:
    import tempfile

    from kicad_tools.schema.pcb import PCB

    tmp = Path(tempfile.mkdtemp(prefix="placement_score_seeds_"))

    perturb_pcb = _load_perturber()
    records = [json.loads(line) for line in labels_path.read_text().splitlines() if line.strip()]
    by_board: dict[str, list[dict]] = defaultdict(list)
    cache: dict[str, tuple] = {}
    for rec in records:
        seed_path = _seed_from_git(seed_rev, rec["seed_path"], tmp)
        if rec["seed_name"] not in cache:
            seed = PCB.load(seed_path)
            box = grid_box_for(seed)
            base = score_placement(seed, box, capacity=float("inf"))  # peak only
            demand = _rudy(*_collect(seed)[4:], box)
            cache[rec["seed_name"]] = (box, OVERFLOW_CAPACITY_FACTOR * float(demand.mean()))
            del base
        box, cap = cache[rec["seed_name"]]
        pcb = PCB.load(seed_path)
        n_moved, n_rot = perturb_pcb(
            pcb, random.Random(rec["rng_seed"]), rec["sigma_mm"], rec["rotate_prob"]
        )
        if (n_moved, n_rot) != (rec["n_moved"], rec["n_rotated"]):
            raise RuntimeError(f"perturbation not reproducible for {rec['sample_id']}")
        if len(list(pcb.footprints)) != rec["n_footprints"]:
            raise RuntimeError(f"seed board drifted for {rec['sample_id']}")
        row = score_placement(pcb, box, cap)
        row["steiner_signal_length"] = rec["features"]["steiner_signal_length"]
        row["severity"] = severity(rec)
        row["sample_id"] = rec["sample_id"]
        by_board[rec["seed_name"]].append(row)
    return by_board


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--labels", type=Path, default=REPO / "data/research/fom_phase0/labels.jsonl")
    ap.add_argument(
        "--output", type=Path, default=REPO / "data/research/placement_score/results.json"
    )
    ap.add_argument("--seed-rev", default=LABELS_REV, help="git rev of the seed boards")
    args = ap.parse_args()

    rows = build_rows(args.labels, args.seed_rev)
    res = evaluate(rows)
    res["bootstrap_95ci_pooled"] = {s: bootstrap_ci(rows, lambda r, s=s: r[s]) for s in SIGNALS}
    res["n_samples"] = {b: len(r) for b, r in rows.items()}
    res["severity_counts"] = {
        b: {
            "clean": sum(r["severity"] == 0 for r in rs),
            "routed_with_drc": sum(0 < r["severity"] < ROUTE_FAIL_SEVERITY for r in rs),
            "route_failed": sum(r["severity"] == ROUTE_FAIL_SEVERITY for r in rs),
        }
        for b, rs in rows.items()
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(res, indent=2, sort_keys=True))
    print(json.dumps({k: res[k] for k in ("pooled", "pooled_n_pairs", "n_pairs")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
