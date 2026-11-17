#!/usr/bin/env python3
"""Calibrate pre-route placement signals against routing outcomes (Issues #5948, #6233).

Question: do the placement-only signals we already compute -- RUDY congestion,
HPWL, the ``optimize-placement`` cost, the Phase 0 / FOM pre-route features --
predict which of two placements of the *same* board routes better?  If one of
them (or a fitted combination) does, it can serve as the acceptance test in
``kct optimize-placement``.  Results and the decision live in
``docs/research/placement_score_calibration.md``.

The study has two corpora, each with its own labels, signal set and fit:

Corpus A -- recorded Phase 0 labels (``phase0``)
    No routing.  Rebuilds every placement of the learned-FOM Phase 0 corpus
    (``data/research/fom_phase0/labels.jsonl``) from its stored RNG seed,
    scores it, and ranks it against the recorded outcome (route failed, else
    ``kct check`` error count).  Writes
    ``data/research/placement_score/results.json``.

Corpus B -- fresh routed fleet corpus (``generate`` + ``analyze``)
    ``generate``: for each seed board, write N placement variants (the
    unperturbed placement plus Gaussian position jitter / +-90 degree
    rotations, reusing ``perturb_pcb`` from ``generate_perturbations.py`` --
    the Phase 0 machinery of Issue #3187), compute every pre-route signal on
    the unrouted variant, route it with ``kct route`` under a fixed budget and
    label it with signal-net completion (% of requested nets) and the
    ``kct check`` error count.  Rows are appended to a JSONL file as they
    finish, so an interrupted run resumes by re-running the same command.

    ``analyze``: read the JSONL and report, per signal, same-board pairwise
    ranking accuracy and within-board Spearman correlations, plus a pairwise
    logistic combination evaluated leave-one-board-out.  Writes
    ``data/research/placement_score/analysis*.{json,md}``.

The math helpers of both corpora are pure numpy and are pinned by
``tests/test_placement_score_calibration.py``.

Examples
--------
::

    uv run python scripts/research/placement_score_calibration.py phase0

    uv run python scripts/research/placement_score_calibration.py generate \\
        --out "$SCRATCH/labels.jsonl" --work-dir "$SCRATCH/work" \\
        --samples-per-board 18 --workers 3 --route-timeout 45

    uv run python scripts/research/placement_score_calibration.py analyze \\
        "$SCRATCH/labels.jsonl" --json "$SCRATCH/analysis.json"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]

# Corpus B boards: those with an unrouted ``output/<name>.kicad_pcb``
# (``kct fleet status``).
DEFAULT_BOARDS: tuple[str, ...] = (
    "boards/00-simple-led/output/simple_led.kicad_pcb",
    "boards/01-voltage-divider/output/voltage_divider.kicad_pcb",
    "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb",
    "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
    "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb",
    "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb",
    "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
    "boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb",
)

# Signals evaluated by ``analyze``.  Every one is cost-like: lower is assumed
# to mean "more routable".  That direction is fixed a priori (never fit), so
# the single-signal accuracies carry no selection bias.
SIGNALS: tuple[str, ...] = (
    "hpwl_signal",
    "hpwl_all",
    "steiner_signal_length",
    "rudy_peak",
    "rudy_overflow",
    "rudy_top10_mean",
    "crossing_count",
    "placement_cost_total",
    "placement_wirelength",
    "placement_overlap",
    "placement_drc",
    "placement_boundary",
)

# Signals fed to the fitted pairwise combination.  ``placement_cost_total``
# is excluded because it is itself a fixed linear mix of the placement_*
# terms (and hpwl_all of hpwl_signal + power nets).
FIT_SIGNALS: tuple[str, ...] = (
    "hpwl_signal",
    "steiner_signal_length",
    "rudy_peak",
    "rudy_overflow",
    "rudy_top10_mean",
    "crossing_count",
    "placement_wirelength",
    "placement_overlap",
    "placement_drc",
    "placement_boundary",
)

# Sigma (mm) ladder cycled across a board's perturbed samples, so every board
# spans "barely moved" to "badly scrambled".
SIGMA_LADDER: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)

# Track pitch used for the RUDY capacity model (0.2 mm track + 0.2 mm gap).
RUDY_TRACK_PITCH_MM = 0.4

# Reference row in the signal table (perturbation sigma; see ``analyze``).
REF_SIGMA = "sigma (reference, not a signal)"

# Completion values closer than this (percentage points) are treated as equal.
COMPLETION_EPS = 1e-6


# ===========================================================================
# Corpus B (fresh routed fleet corpus): pure math (unit-tested)
# ===========================================================================


def rankdata(values: Sequence[float]) -> np.ndarray:
    """Average ranks (1-based), ties share the mean rank -- scipy's default."""
    arr = np.asarray(values, dtype=float)
    n = arr.size
    order = np.argsort(arr, kind="mergesort")
    ranks = np.empty(n, dtype=float)
    sorted_vals = arr[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rho with average-rank ties; None when either side is constant."""
    if len(x) != len(y):
        raise ValueError("x and y must have the same length")
    if len(x) < 3:
        return None
    rx = rankdata(x)
    ry = rankdata(y)
    sx = rx.std()
    sy = ry.std()
    if sx == 0 or sy == 0:
        return None
    return float(((rx - rx.mean()) * (ry - ry.mean())).mean() / (sx * sy))


def routability_compare(a: dict[str, Any], b: dict[str, Any]) -> int:
    """+1 if *a* routed better than *b*, -1 if worse, 0 if indistinguishable.

    Better = higher completion; at equal completion, fewer check errors.
    """
    ca, cb = float(a["completion"]), float(b["completion"])
    if ca > cb + COMPLETION_EPS:
        return 1
    if cb > ca + COMPLETION_EPS:
        return -1
    da, db = int(a["drc_errors"]), int(b["drc_errors"])
    if da < db:
        return 1
    if db < da:
        return -1
    return 0


def build_pairs(
    rows: Sequence[dict[str, Any]],
    key: str = "board",
    compare: Callable[[dict[str, Any], dict[str, Any]], int] = routability_compare,
) -> list[tuple[int, int]]:
    """All same-board pairs with a strict outcome order, as (better, worse) indices."""
    by_board: dict[str, list[int]] = {}
    for idx, r in enumerate(rows):
        by_board.setdefault(r[key], []).append(idx)
    pairs: list[tuple[int, int]] = []
    for idxs in by_board.values():
        for i_pos, i in enumerate(idxs):
            for j in idxs[i_pos + 1 :]:
                c = compare(rows[i], rows[j])
                if c > 0:
                    pairs.append((i, j))
                elif c < 0:
                    pairs.append((j, i))
    return pairs


def pairwise_accuracy(scores: Sequence[float], pairs: Sequence[tuple[int, int]]) -> float | None:
    """Fraction of (better, worse) pairs where the better one has the LOWER score.

    Score ties count as half a hit (a coin flip), so a constant signal scores
    exactly 0.5.  None when there are no pairs.
    """
    if not pairs:
        return None
    s = np.asarray(scores, dtype=float)
    hits = 0.0
    for better, worse in pairs:
        if s[better] < s[worse]:
            hits += 1.0
        elif s[better] == s[worse]:
            hits += 0.5
    return hits / len(pairs)


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60.0, 60.0)))


def fit_pairwise_logistic(
    diffs: np.ndarray,
    l2: float = 1.0,
    iters: int = 50,
) -> np.ndarray:
    """Fit w so sigmoid(w . (x_worse - x_better)) ~= 1 (RankNet-style, no intercept).

    *diffs* is ``(n_pairs, n_features)`` with each row ``x_worse - x_better``,
    i.e. every training example is a positive.  The symmetric negative
    (``-diff``, label 0) is implied, which is why no intercept is fit:
    swapping a pair's order must flip the prediction.  Newton/IRLS with an L2
    ridge so collinear or separable training data still converges.

    The resulting linear score ``x . w`` is cost-like: lower = more routable.
    """
    X = np.vstack([diffs, -diffs])
    y = np.concatenate([np.ones(len(diffs)), np.zeros(len(diffs))])
    n, d = X.shape
    w = np.zeros(d)
    if n == 0:
        return w
    for _ in range(iters):
        p = sigmoid(X @ w)
        grad = X.T @ (p - y) / n + l2 * w / n
        hess = (X.T * (p * (1 - p))) @ X / n + (l2 / n) * np.eye(d)
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    return w


def feature_matrix(rows: Sequence[dict[str, Any]], names: Sequence[str]) -> np.ndarray:
    """log1p-transformed signal matrix (signals are all >= 0).

    Differences of log1p values are ~log-ratios, which puts a 30-net and a
    300-net board on the same footing inside one pairwise model.
    """
    return np.array(
        [[math.log1p(max(0.0, float(r["signals"][n]))) for n in names] for r in rows],
        dtype=float,
    )


def lobo_evaluate(
    rows: Sequence[dict[str, Any]],
    names: Sequence[str],
    l2: float = 1.0,
) -> dict[str, Any]:
    """Leave-one-board-out evaluation of the pairwise logistic combination.

    For each board B: standardise and fit on every other board's pairs, then
    score B's pairs.  Also evaluates the *honest* single-signal baseline --
    the signal with the best training accuracy, re-chosen per fold -- so the
    fitted model is compared to a selection that never saw the test board.

    Returns micro (pooled pairs) and macro (mean per board) accuracies, the
    per-board breakdown, the held-out pair probabilities used for threshold
    analysis, and the weights fit on all boards.
    """
    X = feature_matrix(rows, names)
    pairs = build_pairs(rows)
    boards = sorted({r["board"] for r in rows})
    board_of = [r["board"] for r in rows]

    per_board: dict[str, dict[str, Any]] = {}
    heldout_probs: list[tuple[float, int]] = []  # (P(first-listed better), truth)
    # Each row's score from the fold that held its board out (NaN if its
    # board had no pairs), so any within-board pair subset can be re-scored.
    heldout_scores = np.full(len(rows), np.nan)
    tot_fit = tot_best = 0.0
    tot_pairs = 0
    for b in boards:
        test = [(i, j) for (i, j) in pairs if board_of[i] == b]
        train = [(i, j) for (i, j) in pairs if board_of[i] != b]
        if not test or not train:
            continue
        d_train = np.array([X[j] - X[i] for (i, j) in train])
        scale = d_train.std(axis=0)
        scale[scale == 0] = 1.0
        w = fit_pairwise_logistic(d_train / scale, l2=l2)
        score = (X / scale) @ w
        acc_fit = pairwise_accuracy(score, test)
        for idx, bo in enumerate(board_of):
            if bo == b:
                heldout_scores[idx] = score[idx]

        # Honest single-signal baseline: best by TRAINING accuracy.
        train_accs = [pairwise_accuracy(X[:, k], train) or 0.0 for k in range(len(names))]
        k_best = int(np.argmax(train_accs))
        acc_best = pairwise_accuracy(X[:, k_best], test)

        for i, j in test:
            # Present each pair in a deterministic but order-agnostic way:
            # first-listed = lower row index, truth = 1 if it routed better.
            a, c = (i, j) if i < j else (j, i)
            p_a_better = float(sigmoid(np.array([score[c] - score[a]]))[0])
            heldout_probs.append((p_a_better, 1 if a == i else 0))

        per_board[b] = {
            "n_pairs": len(test),
            "acc_fit": acc_fit,
            "acc_best_single": acc_best,
            "best_single_signal": names[k_best],
        }
        tot_fit += (acc_fit or 0.0) * len(test)
        tot_best += (acc_best or 0.0) * len(test)
        tot_pairs += len(test)

    d_all = np.array([X[j] - X[i] for (i, j) in pairs]) if pairs else np.zeros((0, len(names)))
    scale_all = d_all.std(axis=0) if len(d_all) else np.ones(len(names))
    scale_all[scale_all == 0] = 1.0
    w_all = fit_pairwise_logistic(d_all / scale_all, l2=l2) if len(d_all) else np.zeros(len(names))

    macro = [v["acc_fit"] for v in per_board.values()]
    macro_best = [v["acc_best_single"] for v in per_board.values()]
    return {
        "n_pairs": tot_pairs,
        "acc_fit_micro": tot_fit / tot_pairs if tot_pairs else None,
        "acc_best_single_micro": tot_best / tot_pairs if tot_pairs else None,
        "acc_fit_macro": float(np.mean(macro)) if macro else None,
        "acc_best_single_macro": float(np.mean(macro_best)) if macro_best else None,
        "per_board": per_board,
        "heldout_probs": heldout_probs,
        "heldout_scores": heldout_scores,
        "weights_all_boards": {
            n: float(wv / s) for n, wv, s in zip(names, w_all, scale_all, strict=True)
        },
    }


def gate_table(
    heldout_probs: Sequence[tuple[float, int]],
    thresholds: Sequence[float] = (0.5, 0.6, 0.7, 0.8, 0.9),
) -> list[dict[str, Any]]:
    """Acceptance-gate view of held-out pair predictions.

    Every pair is considered in both directions (candidate = either member,
    incumbent = the other).  The gate *accepts* the candidate when
    P(candidate better) >= t.  Reported per threshold: how many candidate
    moves are accepted, the precision (accepted moves that really routed
    better), and recall (truly-better moves that were accepted).
    """
    rows = []
    for t in thresholds:
        accepted = correct = positives = 0
        for p, truth in heldout_probs:
            for prob, is_better in ((p, truth), (1.0 - p, 1 - truth)):
                positives += is_better
                if prob >= t:
                    accepted += 1
                    correct += is_better
        rows.append(
            {
                "threshold": t,
                "accepted": accepted,
                "precision": correct / accepted if accepted else None,
                "recall": correct / positives if positives else None,
            }
        )
    return rows


# ===========================================================================
# Signal extraction (placement-only)
# ===========================================================================


def _load_sibling(name: str):
    """Import a sibling research script by path (scripts/ is not a package)."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_pscal_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _rudy_signals(pcb: Any, feats: Any, power_check: Callable[[str], bool]) -> dict[str, float]:
    """RUDY peak / overflow / top-decile utilisation over the signal nets.

    ``CongestionEstimator`` produces per-tile *demand* (mm of HPWL spread over
    each net's bbox tiles) but has no capacity model.  We add the simplest
    one: capacity = tile area / track pitch * copper layers (mm of track the
    tile could hold).  Utilisation = demand / capacity; overflow = sum of
    utilisation above 1.0.
    """
    from kicad_tools.router.congestion_estimator import CongestionEstimator

    outline = pcb.get_board_outline()
    if outline:
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        ox, oy, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    else:
        x0, y0, x1, y1 = feats.board_bbox
        ox, oy, w, h = x0, y0, x1 - x0, y1 - y0

    nets: dict[int, list[tuple[str, str]]] = {}
    pads: dict[tuple[str, str], Any] = {}
    for net_num, pfs in feats.nets_to_pads.items():
        name = feats.net_names.get(net_num, "") or ""
        if power_check(name):
            continue
        keys = []
        for k, pf in enumerate(pfs):
            key = (f"{pf.reference}", f"{pf.pad_number}#{net_num}#{k}")
            pads[key] = SimpleNamespace(x=pf.x, y=pf.y)
            keys.append(key)
        nets[net_num] = keys

    est = CongestionEstimator.from_nets(nets, pads, ox, oy, w, h, target_tiles=100)
    n_layers = max(1, len(pcb.copper_layers))
    cap = est.grid.tile_w * est.grid.tile_h / RUDY_TRACK_PITCH_MM * n_layers
    util = sorted((d / cap for row in est.demand for d in row), reverse=True)
    top = util[: max(1, len(util) // 10)]
    return {
        "rudy_peak": util[0] if util else 0.0,
        "rudy_overflow": sum(max(0.0, u - 1.0) for u in util),
        "rudy_top10_mean": sum(top) / len(top) if top else 0.0,
    }


def compute_signals(pcb_path: Path, pcb: Any) -> dict[str, float]:
    """All pre-route signals for one (unrouted) placement."""
    from kicad_tools.optim import fom_features as ff
    from kicad_tools.optim.fom_geometry import crossing_count

    feats = ff.extract_features(pcb)
    power = ff._looks_like_power_net

    hpwl_all = hpwl_sig = 0.0
    for net_num, pfs in feats.nets_to_pads.items():
        if len(pfs) < 2:
            continue
        xs = [p.x for p in pfs]
        ys = [p.y for p in pfs]
        hp = (max(xs) - min(xs)) + (max(ys) - min(ys))
        hpwl_all += hp
        if not power(feats.net_names.get(net_num, "") or ""):
            hpwl_sig += hp

    sig: dict[str, float] = {
        "hpwl_all": hpwl_all,
        "hpwl_signal": hpwl_sig,
        "steiner_signal_length": ff._steiner_total_signal_length(feats),
        "crossing_count": crossing_count(feats),
    }
    sig.update(_rudy_signals(pcb, feats, power))
    sig.update(_placement_cost(pcb_path))
    return sig


def _placement_cost(pcb_path: Path) -> dict[str, float]:
    """The optimize-placement objective, via its own ``--dry-run --format json``."""
    cmd = [
        "uv",
        "run",
        "kct",
        "optimize-placement",
        str(pcb_path),
        "--dry-run",
        "--format",
        "json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=REPO_ROOT)
    doc = json.loads(res.stdout[res.stdout.find("{") :])
    cur = doc["scores"]["current"]
    b = cur["breakdown"]
    return {
        "placement_cost_total": float(cur["total"]),
        "placement_feasible": 1.0 if cur["feasible"] else 0.0,
        "placement_wirelength": float(b.get("wirelength", 0.0)),
        "placement_overlap": float(b.get("overlap", 0.0)),
        "placement_drc": float(b.get("drc", 0.0)),
        "placement_boundary": float(b.get("boundary", 0.0)),
        "placement_area": float(b.get("area", 0.0)),
    }


# ===========================================================================
# Routing + labelling
# ===========================================================================


def run_route(src: Path, out: Path, timeout_s: int, router_seed: int) -> dict[str, Any]:
    """``kct route`` under a fixed budget.

    The negotiated router spends most of whatever budget it is given, and on a
    deadline it writes its best-so-far board as a checkpoint (named in
    ``<out>.timeout.json``) instead of ``<out>``.  Both are valid labels for
    "what this placement routes to within the budget", so we keep whichever
    exists and record which one (``label_source``).  Completion is measured on
    that file by ``kct net-status`` (see :func:`run_net_status`), never taken
    from the router's own summary, so final and checkpoint boards are scored
    the same way.
    """
    cmd = [
        "uv",
        "run",
        "kct",
        "route",
        str(src),
        "-o",
        str(out),
        "--format",
        "json",
        "--backend",
        "cpp",
        "--skip-drc",
        "--no-optimize",
        "--no-sync-check",
        "--no-placement-feedback",
        "--no-routing-plan",
        "--no-auto-layers",
        "--oracle-rounds",
        "0",
        "--timeout",
        str(timeout_s),
        "--seed",
        str(router_seed),
    ]
    t0 = time.perf_counter()
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s * 4 + 60, cwd=REPO_ROOT
        )
    except subprocess.TimeoutExpired:
        return {"status": "hard_timeout", "seconds": time.perf_counter() - t0}
    secs = time.perf_counter() - t0
    rec: dict[str, Any] = {"seconds": secs, "exit_code": res.returncode}
    txt = res.stdout or ""
    try:
        summary = json.loads(txt[txt.find("{") :])["summary"]
        rec["router_success_rate"] = summary.get("success_rate")
    except (ValueError, KeyError):
        pass
    timeout_doc = out.with_suffix(".timeout.json")
    if res.returncode == 0 and out.exists():
        rec.update(status="ok", label_source="final", label_pcb=str(out))
    elif timeout_doc.exists():
        doc = json.loads(timeout_doc.read_text())
        ck = doc.get("checkpoint") or doc.get("snapshot")
        rec["interrupted_stage"] = doc.get("interrupted_stage")
        if ck and Path(ck).exists():
            rec.update(status="ok", label_source="checkpoint", label_pcb=ck)
        else:
            rec.update(status="timeout_no_checkpoint")
    elif out.exists():
        rec.update(status="ok", label_source="final_nonzero_exit", label_pcb=str(out))
    else:
        tail = (res.stderr or "").strip().splitlines()[-1:] or [""]
        rec.update(status="crash", note=tail[0][:200])
    return rec


def run_net_status(pcb: Path) -> dict[str, Any]:
    """Routed completion of a board, from ``kct net-status``.

    ``completion`` = % of multi-pad *signal* nets (not plane/pour nets) that
    are fully connected.  ``conn_completion`` = % of their pad-to-pad
    connections that are routed (finer-grained, fewer ties).  Plane nets are
    reported separately because their connectivity depends on the zone fill,
    which may not have finished within the budget.
    """
    cmd = ["uv", "run", "kct", "net-status", str(pcb), "--format", "json"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=REPO_ROOT)
        txt = res.stdout or ""
        doc = json.loads(txt[txt.find("{") :])
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {"ok": False, "note": f"net-status failed: {exc}"[:200]}
    sig = [n for n in doc["nets"] if not n.get("is_plane_net") and n.get("total_pads", 0) >= 2]
    planes = [n for n in doc["nets"] if n.get("is_plane_net")]
    n_sig = len(sig)
    n_done = sum(1 for n in sig if n.get("status") == "complete")
    conns = sum(n.get("total_connections", 0) for n in sig)
    open_c = sum(n.get("open_connections", 0) for n in sig)
    return {
        "ok": True,
        "signal_nets": n_sig,
        "signal_nets_complete": n_done,
        "completion": 100.0 * n_done / n_sig if n_sig else 100.0,
        "conn_completion": 100.0 * (conns - open_c) / conns if conns else 100.0,
        "plane_nets": len(planes),
        "plane_open_connections": sum(n.get("open_connections", 0) for n in planes),
    }


def run_check(pcb: Path, mfr: str = "jlcpcb") -> dict[str, Any]:
    """``kct check`` error count, split into connectivity vs everything else."""
    cmd = ["uv", "run", "kct", "check", str(pcb), "--format", "json", "--mfr", mfr]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=REPO_ROOT)
        txt = res.stdout or ""
        doc = json.loads(txt[txt.find("{") :])
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {"check_ok": False, "note": f"check failed: {exc}"[:200]}
    by_rule: dict[str, int] = {}
    for v in doc.get("violations", []):
        if v.get("severity") == "error" and not v.get("waived"):
            by_rule[v.get("rule_id", "?")] = by_rule.get(v.get("rule_id", "?"), 0) + 1
    conn = sum(n for r, n in by_rule.items() if "connect" in r or "unrouted" in r)
    total = sum(by_rule.values())
    return {
        "check_ok": True,
        "errors_total": total,
        "connectivity_errors": conn,
        "drc_errors": total - conn,
        "errors_by_rule": by_rule,
    }


_write_lock = threading.Lock()


def _append(out: Path, rec: dict[str, Any]) -> None:
    with _write_lock, out.open("a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _done_ids(out: Path) -> set[str]:
    if not out.exists():
        return set()
    ids = set()
    for line in out.read_text().splitlines():
        try:
            ids.add(json.loads(line)["sample_id"])
        except (ValueError, KeyError):
            continue
    return ids


def _make_jobs(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Deterministic job list: per board, the original (x router seeds) + perturbations."""
    master = random.Random(args.seed)
    jobs = []
    for board in args.boards:
        name = Path(board).stem
        for rs in range(args.noise_router_seeds):
            jobs.append(
                {
                    "sample_id": f"{name}__orig__r{rs}",
                    "board": name,
                    "seed_path": board,
                    "kind": "original",
                    "sigma": 0.0,
                    "rotate_prob": 0.0,
                    "rng_seed": None,
                    "router_seed": rs,
                    "route_timeout": args.route_timeout,
                }
            )
        for k in range(args.samples_per_board):
            jobs.append(
                {
                    "sample_id": f"{name}__p{k:03d}",
                    "board": name,
                    "seed_path": board,
                    "kind": "perturbed",
                    "sigma": SIGMA_LADDER[k % len(SIGMA_LADDER)],
                    "rotate_prob": args.rotate_prob,
                    "rng_seed": master.randint(0, 2**31 - 1),
                    "router_seed": 0,
                    "route_timeout": args.route_timeout,
                }
            )
    return jobs


def _run_job(job: dict[str, Any], args: argparse.Namespace, perturb: Callable) -> dict[str, Any]:
    from kicad_tools.schema.pcb import PCB

    work = Path(args.work_dir)
    placed = work / "placed" / f"{job['sample_id']}.kicad_pcb"
    routed = work / "routed" / f"{job['sample_id']}.kicad_pcb"
    placed.parent.mkdir(parents=True, exist_ok=True)
    routed.parent.mkdir(parents=True, exist_ok=True)

    rec = dict(job)
    pcb = PCB.load(REPO_ROOT / job["seed_path"])
    stripped = pcb.strip_traces()  # several fleet "unrouted" PCBs carry copper
    rec["stripped"] = stripped
    if job["kind"] == "perturbed":
        n_moved, n_rot = perturb(
            pcb, random.Random(job["rng_seed"]), job["sigma"], job["rotate_prob"]
        )
        rec["n_moved"], rec["n_rotated"] = n_moved, n_rot
    pcb.save(placed)
    try:
        rec["signals"] = compute_signals(placed, PCB.load(placed))
    except Exception as exc:  # noqa: BLE001 -- record, never abort the corpus
        rec["signals"] = None
        rec["signal_error"] = f"{type(exc).__name__}: {exc}"[:200]
    rec["route"] = run_route(placed, routed, job["route_timeout"], job["router_seed"])
    if rec["route"].get("status") == "ok":
        label_pcb = Path(rec["route"]["label_pcb"])
        rec["net_status"] = run_net_status(label_pcb)
        rec["check"] = run_check(label_pcb, mfr=args.mfr)
    if args.cleanup:
        for p in placed.parent.glob(f"{job['sample_id']}.*"):
            p.unlink(missing_ok=True)
        for p in routed.parent.glob(f"*{job['sample_id']}*"):
            p.unlink(missing_ok=True)
    return rec


def cmd_generate(args: argparse.Namespace) -> int:
    perturb = _load_sibling("generate_perturbations").perturb_pcb
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = _done_ids(out)
    jobs = [j for j in _make_jobs(args) if j["sample_id"] not in done]
    print(f"{len(jobs)} jobs to run ({len(done)} already in {out})", flush=True)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_run_job, j, args, perturb): j for j in jobs}
        for n, fut in enumerate(as_completed(futs), 1):
            job = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001
                rec = dict(
                    job, signals=None, route={"status": "harness_error", "note": str(exc)[:200]}
                )
            _append(out, rec)
            r = rec.get("route", {})
            print(
                f"[{n}/{len(jobs)} {time.perf_counter() - t0:6.0f}s] {rec['sample_id']}: "
                f"{r.get('status')}/{r.get('label_source')} "
                f"completion={rec.get('net_status', {}).get('completion')} "
                f"drc={rec.get('check', {}).get('drc_errors')} t={r.get('seconds', 0):.0f}s",
                flush=True,
            )
    return 0


# ===========================================================================
# Analysis
# ===========================================================================


# Completion assigned to a no-checkpoint timeout under ``--timeouts-as-worst``:
# strictly below any measured completion (0-100), so it ranks last.
TIMEOUT_WORST_COMPLETION = -1.0


def load_labelled(
    paths: Sequence[Path],
    timeouts_as_worst: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(usable rows flattened for analysis, excluded rows with the reason).

    A route that hit the budget before writing any best-so-far checkpoint
    (``timeout_no_checkpoint``) has no measurable completion, so by default
    it is excluded.  ``timeouts_as_worst`` instead keeps it as the worst
    possible outcome (completion -1, 0 DRC) -- a sensitivity check, since a
    loaded machine can cause such timeouts independent of the placement.
    """
    usable, excluded = [], []
    lines = [ln for p in paths for ln in Path(p).read_text().splitlines()]
    for line in lines:
        if not line.strip():
            continue
        r = json.loads(line)
        route = r.get("route") or {}
        chk = r.get("check") or {}
        ns = r.get("net_status") or {}
        reason = None
        if not r.get("signals"):
            reason = "signal extraction failed"
        elif route.get("status") != "ok":
            reason = f"route {route.get('status')}"
        elif not ns.get("ok"):
            reason = "net-status failed"
        elif not chk.get("check_ok"):
            reason = "check failed"
        if (
            reason
            and timeouts_as_worst
            and r.get("signals")
            and route.get("status") == "timeout_no_checkpoint"
        ):
            usable.append(
                {
                    "sample_id": r["sample_id"],
                    "board": r["board"],
                    "kind": r["kind"],
                    "sigma": r["sigma"],
                    "router_seed": r["router_seed"],
                    "signals": r["signals"],
                    "completion": TIMEOUT_WORST_COMPLETION,
                    "conn_completion": TIMEOUT_WORST_COMPLETION,
                    "drc_errors": 0,
                    "label_source": "timeout_as_worst",
                    "route_seconds": route["seconds"],
                    "route_timeout": r.get("route_timeout"),
                }
            )
            continue
        if reason:
            excluded.append({"sample_id": r["sample_id"], "board": r["board"], "reason": reason})
            continue
        usable.append(
            {
                "sample_id": r["sample_id"],
                "board": r["board"],
                "kind": r["kind"],
                "sigma": r["sigma"],
                "router_seed": r["router_seed"],
                "signals": r["signals"],
                "completion": ns["completion"],
                "conn_completion": ns["conn_completion"],
                "drc_errors": chk["drc_errors"],
                "label_source": route["label_source"],
                "route_seconds": route["seconds"],
                "route_timeout": r.get("route_timeout"),
            }
        )
    return usable, excluded


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Analysis set: one row per placement (router seed 0 only); the extra
    # router seeds of the original placement measure label noise instead.
    main = [r for r in rows if r["router_seed"] == 0]
    pairs = build_pairs(main)
    comp_pairs = build_pairs(
        main,
        compare=lambda a, b: (
            0
            if abs(a["completion"] - b["completion"]) <= COMPLETION_EPS
            else (1 if a["completion"] > b["completion"] else -1)
        ),
    )
    boards = sorted({r["board"] for r in main})
    # Pairs of two perturbations drawn at the SAME sigma: controls for "how
    # hard was it shaken", which a gate comparing optimizer candidates never
    # gets to see.
    same_sigma = [
        (i, j)
        for (i, j) in pairs
        if main[i]["kind"] == main[j]["kind"] == "perturbed"
        and main[i]["sigma"] == main[j]["sigma"]
    ]

    def value(r: dict[str, Any], s: str) -> float:
        # ``sigma`` is the perturbation size, not a pre-route signal: a
        # reference row showing how much of the outcome "how hard did we
        # shake it" alone explains (unknowable at optimize-placement time).
        return float(r["sigma"]) if s == REF_SIGMA else float(r["signals"][s])

    per_signal = {}
    for s in (*SIGNALS, REF_SIGMA):
        col = [value(r, s) for r in main]
        rho_c, rho_d = [], []
        for b in boards:
            sub = [r for r in main if r["board"] == b]
            xs = [value(r, s) for r in sub]
            rc = spearman(xs, [r["completion"] for r in sub])
            rd = spearman(xs, [r["drc_errors"] for r in sub])
            if rc is not None:
                rho_c.append(rc)
            if rd is not None:
                rho_d.append(rd)
        per_signal[s] = {
            "acc_all_pairs": pairwise_accuracy(col, pairs),
            "acc_completion_pairs": pairwise_accuracy(col, comp_pairs),
            "acc_same_sigma_pairs": pairwise_accuracy(col, same_sigma),
            "spearman_completion_mean": float(np.mean(rho_c)) if rho_c else None,
            "spearman_completion_n_boards": len(rho_c),
            "spearman_drc_mean": float(np.mean(rho_d)) if rho_d else None,
            "spearman_drc_n_boards": len(rho_d),
        }

    lobo = lobo_evaluate(main, FIT_SIGNALS)
    per_board = {}
    for b in boards:
        sub = [r for r in main if r["board"] == b]
        orig = [r for r in rows if r["board"] == b and r["kind"] == "original"]
        per_board[b] = {
            "n_placements": len(sub),
            "n_pairs": sum(1 for i, _ in pairs if main[i]["board"] == b),
            "completion_min": min(r["completion"] for r in sub),
            "completion_max": max(r["completion"] for r in sub),
            "n_full_completion": sum(1 for r in sub if r["completion"] >= 100.0 - 1e-9),
            "drc_min": min(r["drc_errors"] for r in sub),
            "drc_max": max(r["drc_errors"] for r in sub),
            "orig_router_seed_completions": [r["completion"] for r in orig],
            "orig_router_seed_drc": [r["drc_errors"] for r in orig],
            "route_seconds_median": float(np.median([r["route_seconds"] for r in sub])),
            "route_timeout": sorted({r["route_timeout"] for r in sub}, key=str),
            "n_checkpoint_labels": sum(1 for r in sub if r["label_source"] == "checkpoint"),
            "acc_by_signal": {
                s: pairwise_accuracy(
                    [value(r, s) for r in main], [p for p in pairs if main[p[0]]["board"] == b]
                )
                for s in SIGNALS
            },
        }
    return {
        "n_boards": len(boards),
        "n_placements": len(main),
        "n_pairs": len(pairs),
        "n_completion_pairs": len(comp_pairs),
        "per_signal": per_signal,
        "n_same_sigma_pairs": len(same_sigma),
        "same_sigma_fit_acc": pairwise_accuracy(
            lobo["heldout_scores"],
            [p for p in same_sigma if not np.isnan(lobo["heldout_scores"][p[0]])],
        ),
        "fit": {k: v for k, v in lobo.items() if k not in ("heldout_probs", "heldout_scores")},
        "gate": gate_table(lobo["heldout_probs"]),
        "per_board": per_board,
    }


def _fmt(x: float | None, nd: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_markdown(res: dict[str, Any]) -> str:
    out = [
        f"n_boards={res['n_boards']} n_placements={res['n_placements']} "
        f"n_pairs={res['n_pairs']} (completion-decided: {res['n_completion_pairs']})",
        "",
        "| Signal | Pairwise acc (all pairs) | Pairwise acc (completion-decided) "
        f"| Pairwise acc (same-sigma, n={res['n_same_sigma_pairs']}) "
        "| Spearman vs completion | Spearman vs DRC |",
        "|---|---|---|---|---|---|",
    ]
    for s, v in res["per_signal"].items():
        out.append(
            f"| `{s}` | {_fmt(v['acc_all_pairs'])} | {_fmt(v['acc_completion_pairs'])} "
            f"| {_fmt(v['acc_same_sigma_pairs'])} "
            f"| {_fmt(v['spearman_completion_mean'], 2)} (n={v['spearman_completion_n_boards']}) "
            f"| {_fmt(v['spearman_drc_mean'], 2)} (n={v['spearman_drc_n_boards']}) |"
        )
    f = res["fit"]
    out += [
        "",
        f"Fitted pairwise logistic, leave-one-board-out: micro {_fmt(f['acc_fit_micro'])}, "
        f"macro {_fmt(f['acc_fit_macro'])} over {f['n_pairs']} held-out pairs; "
        f"same-sigma pairs {_fmt(res['same_sigma_fit_acc'])}.",
        f"Best single signal chosen on training boards (same folds): micro "
        f"{_fmt(f['acc_best_single_micro'])}, macro {_fmt(f['acc_best_single_macro'])}.",
        "",
        "| Held-out board | pairs | fitted | best-single (chosen) |",
        "|---|---|---|---|",
    ]
    for b, v in f["per_board"].items():
        out.append(
            f"| {b} | {v['n_pairs']} | {_fmt(v['acc_fit'])} | "
            f"{_fmt(v['acc_best_single'])} (`{v['best_single_signal']}`) |"
        )
    key = ("hpwl_all", "placement_wirelength", "placement_cost_total", "rudy_peak")
    out += [
        "",
        "| Board (per-board pairwise acc) | " + " | ".join(f"`{k}`" for k in key) + " |",
        "|---|" + "---|" * len(key),
    ]
    for b, v in res["per_board"].items():
        out.append(f"| {b} | " + " | ".join(_fmt(v["acc_by_signal"][k]) for k in key) + " |")
    out += [
        "",
        "| Gate threshold P(cand better) >= t | accepted | precision | recall |",
        "|---|---|---|---|",
    ]
    for g in res["gate"]:
        out.append(
            f"| {g['threshold']:.1f} | {g['accepted']} | {_fmt(g['precision'])} | {_fmt(g['recall'])} |"
        )
    out += [
        "",
        "| Board | placements | pairs | completion range | 100% routed | DRC range "
        "| original, router seeds (completion / DRC) | median route s | budget s | checkpoint labels |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for b, v in res["per_board"].items():
        seeds = ", ".join(
            f"{c:.0f}/{d}"
            for c, d in zip(
                v["orig_router_seed_completions"], v["orig_router_seed_drc"], strict=True
            )
        )
        out.append(
            f"| {b} | {v['n_placements']} | {v['n_pairs']} | "
            f"{v['completion_min']:.0f}-{v['completion_max']:.0f}% | {v['n_full_completion']} | "
            f"{v['drc_min']}-{v['drc_max']} | {seeds} | {v['route_seconds_median']:.0f} | "
            f"{'/'.join(str(t) for t in v['route_timeout'])} | {v['n_checkpoint_labels']} |"
        )
    return "\n".join(out)


def cmd_analyze(args: argparse.Namespace) -> int:
    rows, excluded = load_labelled(
        [Path(p) for p in args.labels], timeouts_as_worst=args.timeouts_as_worst
    )
    rows = [r for r in rows if r["board"] not in set(args.exclude_board)]
    res = analyze(rows)
    res["excluded"] = excluded
    print(render_markdown(res))
    if excluded:
        print(f"\nExcluded {len(excluded)} rows:")
        for e in excluded:
            print(f"  {e['sample_id']}: {e['reason']}")
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2, sort_keys=True))
    return 0


# ===========================================================================
# Corpus A: recorded Phase 0 labels (``phase0`` subcommand, no routing)
# ===========================================================================
#
# The learned-FOM Phase 0 corpus (``data/research/fom_phase0/labels.jsonl``,
# ``docs/research/learned_fom_phase0.md``) already stores, per sample, the RNG
# seed / sigma / rotate-prob that generated it plus the routing outcome
# (``route_ok``, ``drc_errors``).  Each placement is regenerated
# deterministically with ``perturb_pcb`` and scored; nothing is routed.
#
# Outcome severity (lower is better)::
#
#     route failed / timed out  -> ROUTE_FAIL_SEVERITY
#     routed                    -> drc_errors (0 == clean)

ROUTE_FAIL_SEVERITY = 1000.0

# Phase 0 signals; all are "higher = predicted harder to route".  These are
# computed by this section's own scorer (not ``compute_signals``) so the
# committed ``results.json`` stays reproducible.
PHASE0_SIGNALS: tuple[str, ...] = (
    "hpwl",
    "rudy_peak",
    "rudy_overflow",
    "overlap",
    "placement_cost",
    "steiner_signal_length",
)
# Signals that feed the Phase 0 fitted combination (steiner is a stored feature).
PHASE0_FIT_SIGNALS: tuple[str, ...] = ("hpwl", "rudy_peak", "rudy_overflow", "overlap")

# RUDY overflow capacity = this multiple of the seed placement's mean tile demand.
OVERFLOW_CAPACITY_FACTOR = 1.5
GRID_MARGIN_MM = 10.0
# Commit that added data/research/fom_phase0/labels.jsonl; seed boards are read
# as of this revision so the recorded perturbations reproduce exactly.
LABELS_REV = "83b0bc82"


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
    """``pairwise_accuracy`` with NaN (not None) for no pairs, for the Phase 0 tables."""
    acc = pairwise_accuracy(values, pairs)
    return float("nan") if acc is None else acc


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
        return np.zeros(len(PHASE0_FIT_SIGNALS))
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
        res["boards"][b] = {s: acc_for(b, np.array([r[s] for r in rows])) for s in PHASE0_SIGNALS}

    feats = {
        b: np.array([[r[s] for s in PHASE0_FIT_SIGNALS] for r in rows_by_board[b]]) for b in usable
    }
    # Leave-one-board-out fitted combination.
    weights = {}
    for b in usable:
        train = [(feats[o], pairs_by_board[o]) for o in usable if o != b]
        w = fit_pairwise(train) if train else np.zeros(len(PHASE0_FIT_SIGNALS))
        weights[b] = dict(zip(PHASE0_FIT_SIGNALS, map(float, w), strict=True))
        res["boards"][b]["fitted_lobo"] = acc_for(b, _zscore(feats[b]) @ w)
    # In-sample fit (upper bound / optimism check).
    w_all = fit_pairwise([(feats[b], pairs_by_board[b]) for b in usable])
    for b in usable:
        res["boards"][b]["fitted_in_sample"] = acc_for(b, _zscore(feats[b]) @ w_all)
    res["weights_lobo"] = weights
    res["weights_in_sample"] = dict(zip(PHASE0_FIT_SIGNALS, map(float, w_all), strict=True))

    # Pair-weighted pooled accuracy across usable boards.
    keys = [*PHASE0_SIGNALS, "fitted_lobo", "fitted_in_sample"]
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
    out = tmp / rev / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        subprocess.run(
            ["git", "show", f"{rev}:{rel_path}"], cwd=REPO_ROOT, check=True, capture_output=True
        ).stdout
    )
    return out


def build_rows(labels_path: Path, seed_rev: str = LABELS_REV) -> dict[str, list[dict]]:
    """Regenerate and score every recorded Phase 0 placement, grouped by seed board."""
    import tempfile

    from kicad_tools.schema.pcb import PCB

    tmp = Path(tempfile.mkdtemp(prefix="placement_score_seeds_"))

    perturb_pcb = _load_sibling("generate_perturbations").perturb_pcb
    records = [json.loads(line) for line in labels_path.read_text().splitlines() if line.strip()]
    by_board: dict[str, list[dict]] = defaultdict(list)
    cache: dict[str, tuple] = {}
    for rec in records:
        seed_path = _seed_from_git(seed_rev, rec["seed_path"], tmp)
        if rec["seed_name"] not in cache:
            seed = PCB.load(seed_path)
            box = grid_box_for(seed)
            demand = _rudy(*_collect(seed)[4:], box)
            cache[rec["seed_name"]] = (box, OVERFLOW_CAPACITY_FACTOR * float(demand.mean()))
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


# ===========================================================================
# CLI
# ===========================================================================


def cmd_phase0(args: argparse.Namespace) -> int:
    rows = build_rows(args.labels, args.seed_rev)
    res = evaluate(rows)
    res["bootstrap_95ci_pooled"] = {
        s: bootstrap_ci(rows, lambda r, s=s: r[s]) for s in PHASE0_SIGNALS
    }
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


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p0 = sub.add_parser("phase0", help="corpus A: score recorded Phase 0 labels (no routing)")
    p0.add_argument(
        "--labels", type=Path, default=REPO_ROOT / "data/research/fom_phase0/labels.jsonl"
    )
    p0.add_argument(
        "--output", type=Path, default=REPO_ROOT / "data/research/placement_score/results.json"
    )
    p0.add_argument("--seed-rev", default=LABELS_REV, help="git rev of the seed boards")
    p0.set_defaults(func=cmd_phase0)

    g = sub.add_parser("generate", help="perturb, measure signals, route, label")
    g.add_argument("--boards", nargs="*", default=list(DEFAULT_BOARDS))
    g.add_argument("--out", required=True, help="labels JSONL (appended; resumable)")
    g.add_argument("--work-dir", required=True)
    g.add_argument("--samples-per-board", type=int, default=16)
    g.add_argument(
        "--noise-router-seeds",
        type=int,
        default=3,
        help="route the unperturbed placement with this many router seeds",
    )
    g.add_argument("--rotate-prob", type=float, default=0.1)
    g.add_argument("--route-timeout", type=int, default=45)
    g.add_argument("--workers", type=int, default=3)
    g.add_argument("--seed", type=int, default=5948)
    g.add_argument("--mfr", default="jlcpcb")
    g.add_argument("--cleanup", action="store_true", help="delete per-sample PCBs when done")
    g.set_defaults(func=cmd_generate)

    a = sub.add_parser("analyze", help="ranking accuracy / correlation / LOBO fit")
    a.add_argument("labels", nargs="+", help="one or more labels JSONL files")
    a.add_argument("--json", default=None)
    a.add_argument(
        "--timeouts-as-worst",
        action="store_true",
        help="keep no-checkpoint timeouts as the worst outcome instead of excluding them",
    )
    a.add_argument(
        "--exclude-board",
        action="append",
        default=[],
        help="drop a board (by PCB stem) from the analysis; repeatable",
    )
    a.set_defaults(func=cmd_analyze)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
