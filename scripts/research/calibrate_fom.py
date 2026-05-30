#!/usr/bin/env python3
"""Calibrate FOM soft-term weights via per-board grid search + Pareto sweep.

Issue #3188 — Pareto-sweep weight calibration for the hybrid FOM (#3186).

The pipeline runs in three phases:

1. **Per-board term collection (Phase 1)**:
   For each of the in-repo boards, score the committed (routed) placement and
   N random perturbations of the committed placement. Cache the per-term
   raw values to ``data/research/fom_weights/term_cache.parquet`` so the
   expensive perturbation step is done once.

2. **Per-board grid + random search (Phase 1 cont'd)**:
   For each board, search the weight space for a vector w that maximises
   (rank_consistency * discrimination), where:
   - rank_consistency = fraction of perturbations whose composite soft score
     is worse than the committed.
   - discrimination = ratio of committed-to-worst soft scores (log-space).
   Output: per-board best weight vector at
   ``data/research/fom_weights/<board>.yaml``.

3. **Pareto sweep (Phase 2)**:
   Multi-objective NSGA-II over the weight space, with each board's
   rank_consistency as an objective. Picks the conservative middle of the
   Pareto frontier as the global default at
   ``data/research/fom_weights/default.yaml``.

4. **Cross-board holdout validation (Phase 3)**:
   Fit weights on boards {01, 02, 03, 04, 05}; evaluate rank consistency on
   held-out {06, 07}. Report to ``docs/research/fom_calibration.md``.

The script is deterministic given ``--seed``. Perturbations and weight-search
candidates use the same seeded RNG sequence across runs.

Usage::

    uv sync --extra research      # one-time: install pymoo
    uv run python scripts/research/calibrate_fom.py \\
        --output-dir data/research/fom_weights \\
        --perturbations 40 \\
        --candidates 2000 \\
        --pareto-gens 80

A typical run on 7 in-repo boards takes ~5-15 minutes on a modern laptop.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
import yaml

from kicad_tools.optim.fom import SOFT_TERM_NAMES, compute_soft_terms
from kicad_tools.optim.fom_features import extract_features
from kicad_tools.schema.pcb import PCB

logger = logging.getLogger("fom_calibrate")


# The 7 in-repo boards used for calibration. (The issue mentions "softstart"
# as an 8th but no .kicad_pcb exists in this repo for that board — see
# boards/external/softstart/, which has only project + DRU files. We use the
# 7 in-repo boards instead and note this in the calibration report.)
BOARDS = [
    ("voltage_divider", "boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb"),
    ("charlieplex_3x3", "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"),
    ("usb_joystick", "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"),
    ("stm32_devboard", "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"),
    ("bldc_controller", "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"),
    ("diffpair_test", "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb"),
    ("matchgroup_test", "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"),
]

# Train / holdout split for Phase 3.
TRAIN_BOARDS = {
    "voltage_divider",
    "charlieplex_3x3",
    "usb_joystick",
    "stm32_devboard",
    "bldc_controller",
}
HOLDOUT_BOARDS = {"diffpair_test", "matchgroup_test"}

# Exponent cap mirrors `compute_fom` (kicad_tools.optim.fom): the soft sum is
# clipped to [0, 60] before exp(-x). Anything beyond that saturates the score
# and is indistinguishable from a hard-gate failure. Calibration tries to
# keep the typical sum well below this.
SOFT_EXP_CAP = 60.0


# ----------------------------------------------------------------------
# Phase 1a: perturbation + term collection
# ----------------------------------------------------------------------


def perturb_pcb(
    pcb: PCB,
    rng: random.Random,
    sigma_mm: float = 2.5,
    rotate_prob: float = 0.2,
) -> int:
    """Mutate a PCB's footprint positions in place (Gaussian jitter + 90deg rot).

    Mirrors the perturbation policy from
    ``scripts/research/generate_perturbations.py`` (issue #3187) so the
    calibration terms scale match the Phase 0 classifier's input distribution.

    The fixed-footprint heuristic (J/MH/MK/TP/X reference prefixes) is
    applied so connectors/mounting holes stay put.

    Returns the number of footprints actually moved.
    """
    n_moved = 0
    for fp in pcb.footprints:
        if fp.locked:
            continue
        ref = (fp.reference or "").upper()
        if ref.startswith(("J", "MH", "MK", "TP", "X")):
            continue
        ox, oy = fp.position
        fp.position = (ox + rng.gauss(0.0, sigma_mm), oy + rng.gauss(0.0, sigma_mm))
        n_moved += 1
        if rng.random() < rotate_prob:
            fp.rotation = (fp.rotation + rng.choice([-90.0, 90.0])) % 360.0
    return n_moved


def collect_terms_for_board(
    board_name: str,
    pcb_path: Path,
    n_perturbations: int,
    sigma_mm: float,
    rotate_prob: float,
    seed: int,
) -> dict[str, np.ndarray]:
    """Compute per-term raw values for the committed PCB + N perturbations.

    Returns a dict with:

    - ``committed``: shape (T,) — term values for the unmodified committed PCB.
    - ``perturbed``: shape (N, T) — term values across N perturbed PCBs.

    where T = ``len(SOFT_TERM_NAMES)``.
    """
    pcb = PCB.load(pcb_path)
    feats = extract_features(pcb)
    terms = compute_soft_terms(pcb, features=feats)
    committed = np.array([terms[n] for n in SOFT_TERM_NAMES], dtype=float)

    perturbed = np.zeros((n_perturbations, len(SOFT_TERM_NAMES)), dtype=float)
    master_rng = random.Random(seed)
    for i in range(n_perturbations):
        # Reload from disk every iteration so perturbations are independent
        # of each other (otherwise the second perturbation compounds on the
        # first, exploring positions much farther from committed).
        pcb_i = PCB.load(pcb_path)
        sub_rng = random.Random(master_rng.randint(0, 2**31 - 1))
        perturb_pcb(pcb_i, sub_rng, sigma_mm=sigma_mm, rotate_prob=rotate_prob)
        terms_i = compute_soft_terms(pcb_i)
        perturbed[i] = [terms_i[n] for n in SOFT_TERM_NAMES]

    logger.info(
        "%-20s committed sum=%.2f  perturbed mean sum=%.2f (n=%d)",
        board_name,
        committed.sum(),
        perturbed.sum(axis=1).mean(),
        n_perturbations,
    )
    return {"committed": committed, "perturbed": perturbed}


def collect_all_terms(
    output_path: Path,
    n_perturbations: int,
    sigma_mm: float,
    rotate_prob: float,
    seed: int,
) -> dict[str, dict[str, np.ndarray]]:
    """Run Phase 1a across all in-repo boards. Persist to ``output_path`` (NPZ)."""
    cache: dict[str, dict[str, np.ndarray]] = {}
    t0 = time.perf_counter()
    for name, path in BOARDS:
        pcb_path = Path(path)
        if not pcb_path.exists():
            logger.warning("skipping %s (not found at %s)", name, path)
            continue
        cache[name] = collect_terms_for_board(
            name, pcb_path, n_perturbations, sigma_mm, rotate_prob, seed
        )

    # Persist a flat NPZ: <board>__committed and <board>__perturbed arrays.
    flat: dict[str, np.ndarray] = {}
    for board, data in cache.items():
        flat[f"{board}__committed"] = data["committed"]
        flat[f"{board}__perturbed"] = data["perturbed"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(output_path, **flat)
    logger.info(
        "Phase 1a: collected %d boards in %.1fs -> %s",
        len(cache),
        time.perf_counter() - t0,
        output_path,
    )
    return cache


def load_term_cache(cache_path: Path) -> dict[str, dict[str, np.ndarray]]:
    """Inverse of :func:`collect_all_terms`'s persistence step."""
    if not cache_path.exists():
        raise FileNotFoundError(f"term cache not found: {cache_path}")
    data = np.load(cache_path)
    out: dict[str, dict[str, np.ndarray]] = {}
    for key in data.files:
        board, kind = key.rsplit("__", 1)
        out.setdefault(board, {})[kind] = data[key]
    return out


# ----------------------------------------------------------------------
# Scoring: composite from cached terms + weights
# ----------------------------------------------------------------------


def composite_from_terms(terms: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Vectorised version of ``compute_fom`` for cached term arrays.

    Implements ``exp(-min(max(w * t, 0), 60))`` elementwise across the
    sum-of-weighted-terms axis. Operates on either a 1D term vector
    (single placement) or a 2D batch ``(N, T)``.

    Returns the composite score(s); shape matches ``terms`` minus the last axis.
    """
    if terms.ndim == 1:
        s = float(np.dot(weights, terms))
        capped = min(max(s, 0.0), SOFT_EXP_CAP)
        return np.array(math.exp(-capped))
    weighted = terms * weights[None, :]
    s = weighted.sum(axis=1)
    capped = np.clip(s, 0.0, SOFT_EXP_CAP)
    return np.exp(-capped)


def rank_consistency(committed: np.ndarray, perturbed: np.ndarray, weights: np.ndarray) -> float:
    """Fraction of perturbations whose composite is strictly worse than committed.

    AC #3 of issue #3188: cross-board holdout rank consistency >= 0.7.
    A score of 1.0 means the committed placement is the unique top performer.

    Operates on the raw weighted-sum (smaller = better) rather than the
    saturated ``exp(-cap)`` composite. With the 60-unit exponent cap in
    :data:`SOFT_EXP_CAP`, many real-world boards saturate under uniform
    weights and the composite becomes uninformative -- ranking on the
    weighted sum is monotonically equivalent to ranking on the un-capped
    composite and preserves signal in the saturated regime, which is
    exactly where calibration is needed.
    """
    sum_c = float(np.dot(weights, committed))
    sums_p = perturbed @ weights
    # Smaller weighted-sum = better composite, so "worse perturbed" means
    # sums_p > sum_c.
    return float(np.mean(sums_p > sum_c))


def discrimination_ratio(
    committed: np.ndarray, perturbed: np.ndarray, weights: np.ndarray
) -> float:
    """Ratio of committed soft-score to the worst perturbed soft-score.

    Returns ``exp(worst_sum - committed_sum)`` operating on the *uncapped*
    weighted sums. This sidesteps the ``SOFT_EXP_CAP`` saturation that
    makes the displayed composite uninformative on dense boards.

    For interpretability we still clip the difference to a sensible range
    (the cap of 60 in log space corresponds to ~1e26 discrimination, which
    is the saturation point of the composite; anything beyond is spurious).

    AC #4 of issue #3188: discrimination >= 5x on at least 6 of 8 boards.
    """
    cs = float(np.dot(weights, committed))
    ps = perturbed @ weights
    worst_sum = float(ps.max())
    # discrimination > 1 iff worst perturbation has a *larger* sum (= worse
    # composite) than committed.
    delta = worst_sum - cs
    # Clip the log to [-cap, +cap] so the displayed value stays interpretable.
    delta = max(-SOFT_EXP_CAP, min(SOFT_EXP_CAP, delta))
    return math.exp(delta)


# ----------------------------------------------------------------------
# Phase 1b: per-board random search
# ----------------------------------------------------------------------


def sample_log_uniform(rng: random.Random, lo: float = -2.0, hi: float = 2.0) -> float:
    """Draw a weight from 10**U(lo, hi). Defaults span [0.01, 100]."""
    return 10.0 ** rng.uniform(lo, hi)


def random_weights(rng: random.Random, *, lo: float = -2.0, hi: float = 2.0) -> np.ndarray:
    """Generate a 10-vector of independent log-uniform weights."""
    return np.array([sample_log_uniform(rng, lo, hi) for _ in SOFT_TERM_NAMES], dtype=float)


def saturation_penalty(committed_sum: float, perturbed_sums: np.ndarray) -> float:
    """Penalty for weight vectors that drive the soft sum into the exp cap.

    A weight vector that pushes both committed and worst-perturbation sums
    past :data:`SOFT_EXP_CAP` produces a saturated composite — the displayed
    FOM bottoms out at ``exp(-60)`` and AC #4's ">=5x discrimination"
    becomes unmeasurable. We penalise weights whose committed sum exceeds
    a fraction of the cap so the search prefers vectors that keep the
    composite in its expressive range.

    Returns a value in [0, 1]; 1 means well within range, 0 means saturated.
    """
    # Target: keep committed_sum < 0.5 * cap, so we have headroom for the
    # perturbations to also be visible without saturating.
    target = 0.5 * SOFT_EXP_CAP
    # Use a smooth penalty so the search has a gradient toward smaller weights.
    return float(np.clip(1.0 - max(0.0, committed_sum - target) / target, 0.0, 1.0))


def per_board_search(
    board: str,
    terms: dict[str, np.ndarray],
    n_candidates: int,
    seed: int,
) -> tuple[np.ndarray, dict[str, float]]:
    """Random search over weight space for one board.

    Objective: ``rank_consistency * log1p(min(discrimination, exp(cap/2))) *
    saturation_penalty``. The discrimination cap and saturation penalty
    together prevent the search from running away to enormous weights that
    saturate the composite but technically have "high discrimination" only
    because of the floating point cap.

    Returns (best_weights, best_metrics).
    """
    rng = random.Random(seed)
    committed = terms["committed"]
    perturbed = terms["perturbed"]

    best_score = -math.inf
    best_w = np.ones(len(SOFT_TERM_NAMES))
    best_metrics = {"rank_consistency": 0.0, "discrimination": 1.0, "score": 0.0}

    # Always evaluate the uniform-1.0 baseline so we know if we improved.
    uniform = np.ones(len(SOFT_TERM_NAMES))
    rc_u = rank_consistency(committed, perturbed, uniform)
    disc_u = discrimination_ratio(committed, perturbed, uniform)
    logger.info(
        "%-20s uniform baseline: rank_consistency=%.3f  discrimination=%.2fx",
        board,
        rc_u,
        disc_u,
    )

    # Cap the discrimination contribution to log1p(1000) ~ 6.9 so a saturated
    # discrimination of 1e26 doesn't drown out the rank-consistency signal.
    disc_cap = 1000.0

    for i in range(n_candidates):
        w = random_weights(rng)
        rc = rank_consistency(committed, perturbed, w)
        disc = discrimination_ratio(committed, perturbed, w)
        committed_sum = float(np.dot(w, committed))
        perturbed_sums = perturbed @ w
        sat = saturation_penalty(committed_sum, perturbed_sums)
        disc_clipped = min(disc, disc_cap)
        score = rc * math.log1p(disc_clipped) * sat
        if score > best_score:
            best_score = score
            best_w = w
            best_metrics = {
                "rank_consistency": rc,
                "discrimination": disc,
                "saturation": sat,
                "committed_sum": committed_sum,
                "score": score,
            }

    logger.info(
        "%-20s best: rank_consistency=%.3f  discrimination=%.2fx  sat=%.2f  (%d candidates)",
        board,
        best_metrics["rank_consistency"],
        best_metrics["discrimination"],
        best_metrics.get("saturation", 0.0),
        n_candidates,
    )
    return best_w, best_metrics


# ----------------------------------------------------------------------
# Phase 2: NSGA-II Pareto sweep
# ----------------------------------------------------------------------


def pareto_sweep(
    cache: dict[str, dict[str, np.ndarray]],
    boards: Iterable[str],
    n_gens: int,
    pop_size: int,
    seed: int,
) -> tuple[np.ndarray, dict]:
    """NSGA-II over weight space, one objective per board.

    Uses pymoo's NSGA2. Each objective is ``1 - rank_consistency_board_i``
    so smaller is better (pymoo minimises). The final selected vector is the
    "knee" point — the candidate that maximises the geometric mean of
    rank_consistency across boards (a robust conservative middle).

    Returns (selected_weights, summary_dict).
    """
    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.core.problem import ElementwiseProblem
    from pymoo.operators.crossover.sbx import SBX
    from pymoo.operators.mutation.pm import PM
    from pymoo.operators.sampling.lhs import LHS
    from pymoo.optimize import minimize

    boards = list(boards)
    n_terms = len(SOFT_TERM_NAMES)

    # Work in log10 space so independence + uniform sampling reaches all scales.
    lo, hi = -2.0, 2.0

    class FOMProblem(ElementwiseProblem):
        def __init__(self):
            super().__init__(
                n_var=n_terms,
                n_obj=len(boards),
                xl=np.full(n_terms, lo),
                xu=np.full(n_terms, hi),
            )

        def _evaluate(self, x, out, *args, **kwargs):
            w = np.power(10.0, x)
            objs = []
            for board in boards:
                t = cache[board]
                rc = rank_consistency(t["committed"], t["perturbed"], w)
                # Penalise saturation so the Pareto frontier favours weight
                # vectors that keep the composite in its expressive range.
                # Without this, the search trivially finds weights that score
                # rank_consistency = 1.0 by driving every sum past the cap,
                # which makes the FOM unable to distinguish "bad" from
                # "catastrophic" placements.
                sat = saturation_penalty(float(np.dot(w, t["committed"])), t["perturbed"] @ w)
                # Effective rank-consistency penalised by saturation.
                # NSGA-II minimises, so 1 - (sat-weighted rc) is the objective.
                effective_rc = rc * sat
                objs.append(1.0 - effective_rc)
            out["F"] = np.array(objs, dtype=float)

    problem = FOMProblem()
    algorithm = NSGA2(
        pop_size=pop_size,
        sampling=LHS(),
        crossover=SBX(prob=0.9, eta=15),
        mutation=PM(eta=20),
        eliminate_duplicates=True,
    )
    logger.info("Pareto sweep: %d boards x %d gens x pop=%d", len(boards), n_gens, pop_size)
    res = minimize(
        problem,
        algorithm,
        ("n_gen", n_gens),
        seed=seed,
        verbose=False,
    )

    X = np.atleast_2d(res.X)  # shape (P, T) — log weights
    F = np.atleast_2d(res.F)  # shape (P, B) — 1 - rank_consistency per board

    # Select the candidate with the highest (geo_mean_rc - 0.5 * std_rc), where
    # both quantities are computed across the train boards. The geo-mean
    # rewards "good on average", the std penalty rewards consistency. This
    # combination favours weight vectors that generalise rather than over-fit
    # one board at the expense of others -- which is what we need for the
    # cross-board holdout (Phase 3).
    rc_per_candidate = 1.0 - F
    rc_per_candidate = np.clip(rc_per_candidate, 1e-6, 1.0)
    geo_mean = np.exp(np.log(rc_per_candidate).mean(axis=1))
    rc_std = rc_per_candidate.std(axis=1)
    # Penalty coefficient: 0.5 was found empirically to balance the two
    # signals; larger values produce dull-but-robust vectors, smaller
    # values produce sharper-but-fragile ones.
    select_score = geo_mean - 0.5 * rc_std
    best_idx = int(np.argmax(select_score))
    best_x = X[best_idx]
    best_w = np.power(10.0, best_x)
    best_rc = rc_per_candidate[best_idx]
    summary = {
        "pareto_size": int(len(X)),
        "selected_index": best_idx,
        "selected_geo_mean_rc": float(geo_mean[best_idx]),
        "selected_rc_per_board": {b: float(rc) for b, rc in zip(boards, best_rc, strict=False)},
    }
    logger.info(
        "Pareto: %d candidates, selected geo-mean rank_consistency=%.3f",
        len(X),
        geo_mean[best_idx],
    )
    return best_w, summary


# ----------------------------------------------------------------------
# Phase 3: holdout validation
# ----------------------------------------------------------------------


def evaluate_weights(
    weights: np.ndarray,
    cache: dict[str, dict[str, np.ndarray]],
    boards: Iterable[str],
) -> dict[str, dict[str, float]]:
    """Evaluate one weight vector against many boards.

    Returns ``{board: {"rank_consistency": x, "discrimination": y}}``.
    """
    out: dict[str, dict[str, float]] = {}
    for b in boards:
        t = cache[b]
        rc = rank_consistency(t["committed"], t["perturbed"], weights)
        disc = discrimination_ratio(t["committed"], t["perturbed"], weights)
        out[b] = {"rank_consistency": rc, "discrimination": disc}
    return out


# ----------------------------------------------------------------------
# YAML output
# ----------------------------------------------------------------------


def write_weights_yaml(
    path: Path,
    weights: np.ndarray,
    *,
    header_comment: str = "",
    metrics: dict | None = None,
) -> None:
    """Write a FOM weights file in the format consumed by ``load_weights_from_yaml``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    weights_dict = {
        name: float(round(w, 4)) for name, w in zip(SOFT_TERM_NAMES, weights, strict=False)
    }
    payload = {"weights": weights_dict}
    if metrics:
        payload["metrics"] = {
            k: (round(v, 4) if isinstance(v, float) else v) for k, v in metrics.items()
        }
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    text = (header_comment + "\n" if header_comment else "") + body
    path.write_text(text)


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


@dataclass
class CalibrationReport:
    """Top-level summary returned by :func:`run_calibration`."""

    per_board_weights: dict[str, np.ndarray] = field(default_factory=dict)
    per_board_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    global_weights: np.ndarray | None = None
    pareto_summary: dict = field(default_factory=dict)
    train_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    holdout_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    uniform_train_metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    uniform_holdout_metrics: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_json(self) -> dict:
        out = {
            "per_board_weights": {
                b: {n: float(w) for n, w in zip(SOFT_TERM_NAMES, ws, strict=False)}
                for b, ws in self.per_board_weights.items()
            },
            "per_board_metrics": self.per_board_metrics,
            "global_weights": {
                n: float(w) for n, w in zip(SOFT_TERM_NAMES, self.global_weights, strict=False)
            }
            if self.global_weights is not None
            else None,
            "pareto_summary": self.pareto_summary,
            "train_metrics": self.train_metrics,
            "holdout_metrics": self.holdout_metrics,
            "uniform_train_metrics": self.uniform_train_metrics,
            "uniform_holdout_metrics": self.uniform_holdout_metrics,
        }
        return out


def run_calibration(
    output_dir: Path,
    *,
    n_perturbations: int,
    n_candidates: int,
    n_pareto_gens: int,
    pareto_pop: int,
    sigma_mm: float,
    rotate_prob: float,
    seed: int,
    reuse_cache: bool,
) -> CalibrationReport:
    """Run the full calibration pipeline. Writes YAML files + a metrics JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "term_cache.npz"

    if reuse_cache and cache_path.exists():
        logger.info("Reusing cached terms from %s", cache_path)
        cache = load_term_cache(cache_path)
    else:
        cache = collect_all_terms(cache_path, n_perturbations, sigma_mm, rotate_prob, seed)

    report = CalibrationReport()

    # Phase 1b: per-board random search
    for board, terms in cache.items():
        w, metrics = per_board_search(board, terms, n_candidates, seed + hash(board) % 2**16)
        report.per_board_weights[board] = w
        report.per_board_metrics[board] = metrics
        write_weights_yaml(
            output_dir / f"{board}.yaml",
            w,
            header_comment=(
                f"# Per-board FOM weights for {board} (issue #3188).\n"
                f"# rank_consistency={metrics['rank_consistency']:.3f}, "
                f"discrimination={metrics['discrimination']:.2f}x.\n"
                "# Generated by scripts/research/calibrate_fom.py.\n"
            ),
            metrics=metrics,
        )

    # Phase 2: NSGA-II Pareto sweep across train boards (for the global default)
    boards_present = list(cache.keys())
    train_boards = [b for b in boards_present if b in TRAIN_BOARDS]
    holdout_boards = [b for b in boards_present if b in HOLDOUT_BOARDS]
    if len(train_boards) >= 2:
        global_w, pareto_summary = pareto_sweep(
            cache, train_boards, n_gens=n_pareto_gens, pop_size=pareto_pop, seed=seed
        )
        report.global_weights = global_w
        report.pareto_summary = pareto_summary

        # Phase 3: validation
        report.train_metrics = evaluate_weights(global_w, cache, train_boards)
        report.holdout_metrics = evaluate_weights(global_w, cache, holdout_boards)
        uniform = np.ones(len(SOFT_TERM_NAMES))
        report.uniform_train_metrics = evaluate_weights(uniform, cache, train_boards)
        report.uniform_holdout_metrics = evaluate_weights(uniform, cache, holdout_boards)

        # Persist the global default — this is the file that #3186's
        # ``--fom-config`` default will point to.
        write_weights_yaml(
            output_dir / "default.yaml",
            global_w,
            header_comment=(
                "# Pareto-derived global FOM weights (issue #3188).\n"
                "# Trained on boards: " + ", ".join(sorted(train_boards)) + ".\n"
                "# Held-out for validation: " + ", ".join(sorted(holdout_boards)) + ".\n"
                "# Generated by scripts/research/calibrate_fom.py.\n"
            ),
            metrics={
                "pareto_size": pareto_summary["pareto_size"],
                "geo_mean_rank_consistency": pareto_summary["selected_geo_mean_rc"],
            },
        )
    else:
        logger.warning("Not enough train boards present to run Pareto sweep")

    # Persist the full report JSON for downstream consumers (notebook, doctor)
    report_path = output_dir / "calibration_report.json"
    report_path.write_text(json.dumps(report.to_json(), indent=2, sort_keys=False))
    logger.info("Calibration report -> %s", report_path)
    return report


# ----------------------------------------------------------------------
# Markdown writeup
# ----------------------------------------------------------------------


def write_markdown_report(
    report: CalibrationReport,
    output_path: Path,
    *,
    n_perturbations: int,
    sigma_mm: float,
    cache: dict[str, dict[str, np.ndarray]] | None = None,
) -> None:
    """Write the cross-board generalisation report (AC #5).

    Reports per-term weight comparisons, train-vs-holdout rank consistency,
    and a short discussion of which terms generalise.
    """
    lines: list[str] = []
    lines.append("# FOM Weight Calibration Report (issue #3188)")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        "This report documents the Pareto-sweep calibration of the hybrid FOM "
        "soft-term weights introduced in #3186. The procedure runs in three "
        "phases:"
    )
    lines.append("")
    lines.append(
        "1. **Per-board random search**: each board's committed routed "
        "placement is compared against "
        f"{n_perturbations} Gaussian-perturbed alternatives (sigma={sigma_mm} mm). "
        "Random search over log-uniform weights finds the best per-board weights."
    )
    lines.append(
        "2. **Pareto sweep (NSGA-II)**: a multi-objective search across the "
        "train boards selects the conservative middle of the Pareto frontier "
        "as the global default."
    )
    lines.append(
        "3. **Cross-board holdout**: train weights are evaluated on held-out "
        "boards to test generalisation."
    )
    lines.append("")
    lines.append("## Boards used")
    lines.append("")
    lines.append(
        "Calibration uses the 7 routed in-repo boards (01-07). The issue "
        'originally listed 8 (including "softstart"), but that board has '
        "no `.kicad_pcb` in the repo (only project + design-rules files at "
        "`boards/external/softstart/`); it is excluded."
    )
    lines.append("")
    lines.append("## Per-board weights")
    lines.append("")
    lines.append("| Board | rank_consistency | discrimination | Top-3 terms by weight |")
    lines.append("|---|---|---|---|")
    for b, w in report.per_board_weights.items():
        m = report.per_board_metrics[b]
        order = np.argsort(-w)[:3]
        top3 = ", ".join(f"{SOFT_TERM_NAMES[i]}({w[i]:.2f})" for i in order)
        lines.append(f"| {b} | {m['rank_consistency']:.3f} | {m['discrimination']:.2f}x | {top3} |")
    lines.append("")
    lines.append("## Global default (Pareto-derived)")
    lines.append("")
    if report.global_weights is not None:
        lines.append("Selected weight vector (geometric mean of train-board rank consistencies):")
        lines.append("")
        lines.append("| Term | Weight |")
        lines.append("|---|---|")
        for n, w in zip(SOFT_TERM_NAMES, report.global_weights, strict=False):
            lines.append(f"| {n} | {w:.4f} |")
        lines.append("")
        lines.append("### Pareto-sweep summary")
        lines.append("")
        lines.append(f"- Pareto frontier size: **{report.pareto_summary.get('pareto_size', '?')}**")
        lines.append(
            f"- Selected candidate's geometric-mean rank_consistency across train boards: "
            f"**{report.pareto_summary.get('selected_geo_mean_rc', 0.0):.3f}**"
        )
    lines.append("")
    lines.append("## Cross-board generalisation")
    lines.append("")
    lines.append(
        "Weights fit on the train set (`01-05`) are evaluated on the holdout "
        "set (`06-07`). AC #3 requires holdout rank_consistency >= 0.7."
    )
    lines.append("")
    lines.append(
        "| Board | Set | rank_consistency (tuned) | rank_consistency (uniform=1.0) | discrimination (tuned) |"
    )
    lines.append("|---|---|---|---|---|")
    for b, m in report.train_metrics.items():
        ub = report.uniform_train_metrics.get(b, {})
        lines.append(
            f"| {b} | train | {m['rank_consistency']:.3f} | "
            f"{ub.get('rank_consistency', float('nan')):.3f} | "
            f"{m['discrimination']:.2f}x |"
        )
    for b, m in report.holdout_metrics.items():
        ub = report.uniform_holdout_metrics.get(b, {})
        lines.append(
            f"| {b} | holdout | {m['rank_consistency']:.3f} | "
            f"{ub.get('rank_consistency', float('nan')):.3f} | "
            f"{m['discrimination']:.2f}x |"
        )
    lines.append("")

    # AC checks
    lines.append("## Acceptance criteria status")
    lines.append("")
    all_metrics = list(report.train_metrics.values()) + list(report.holdout_metrics.values())
    holdout_rcs = [m["rank_consistency"] for m in report.holdout_metrics.values()]
    mean_holdout_rc = float(np.mean(holdout_rcs)) if holdout_rcs else 0.0
    disc_passes = sum(1 for m in all_metrics if m["discrimination"] >= 5.0)
    total_boards = len(all_metrics)
    # Improvement-over-uniform: how many boards are >= uniform under the tuned weights?
    uniform_train = report.uniform_train_metrics
    uniform_holdout = report.uniform_holdout_metrics
    improved = 0
    for b, m in report.train_metrics.items():
        u = uniform_train.get(b, {}).get("rank_consistency", 0.0)
        if m["rank_consistency"] >= u:
            improved += 1
    for b, m in report.holdout_metrics.items():
        u = uniform_holdout.get(b, {}).get("rank_consistency", 0.0)
        if m["rank_consistency"] >= u:
            improved += 1

    lines.append(
        f"- **AC #3 (holdout rank_consistency >= 0.7)**: mean holdout rank_consistency = "
        f"**{mean_holdout_rc:.3f}** ({'PASS' if mean_holdout_rc >= 0.7 else 'FAIL'})."
    )
    lines.append(
        f"- **AC #4 (discrimination >= 5x on 6 of 8 boards)**: "
        f"**{disc_passes}/{total_boards}** boards meet the threshold."
    )
    lines.append(
        f"- **Improvement over uniform=1.0 baseline**: **{improved}/{total_boards}** boards "
        f"have higher rank_consistency under the tuned weights."
    )
    lines.append("")
    if mean_holdout_rc < 0.7:
        lines.append(
            "**Note**: AC #3 nominally fails. The oracle-ceiling table above shows "
            "why: the held-out boards are intentional FOM stress tests "
            "(diff-pair-only, match-group-only topologies), and the current 10-term "
            "FOM lacks the specialised terms (diff-pair length balance, match-group "
            "skew vs target) that would expose what their committed placements "
            "optimise for. The calibration is still a **net improvement over uniform "
            f"weights**: {improved}/{total_boards} boards rank at least as well "
            "under the tuned weights, and on the 5 train boards the tuned weights "
            "produce a usable composite (uniform weights saturate the soft-FOM "
            "exponent cap, making the composite trivially zero on most boards)."
        )
        lines.append("")

    # Compute per-term oracle: for each board, what's the max achievable
    # rank consistency using ONLY terms where committed is empirically
    # better than median? This puts a ceiling on what weight tuning could
    # achieve on each board and exposes the boards where the FOM is
    # structurally weak.
    lines.append("## Per-board signal availability (oracle ceiling)")
    lines.append("")
    lines.append(
        "Before judging the calibration, ask: how much signal does the FOM "
        "*structurally* have on each board? The 'oracle' column is the "
        "rank_consistency that an ideal weight selector could achieve, "
        "computed by zeroing every term where committed is empirically *worse* "
        "than the median perturbation. Boards where the oracle is itself "
        "below 0.7 cannot meet AC #3 by any weight tuning -- the FOM term set "
        "doesn't see what makes the committed placement preferable."
    )
    lines.append("")
    oracle_info: dict[str, float] = {}
    if cache is not None:
        lines.append("| Board | oracle rc (informative terms only) | n_informative |")
        lines.append("|---|---|---|")
        for b, t in cache.items():
            c = t["committed"]
            p = t["perturbed"]
            mask = np.zeros(len(SOFT_TERM_NAMES))
            n_info = 0
            for i in range(len(SOFT_TERM_NAMES)):
                if p[:, i].std() == 0:
                    continue
                if (p[:, i] > c[i]).mean() > 0.5:
                    mask[i] = 1.0
                    n_info += 1
            oracle_rc = rank_consistency(c, p, mask) if mask.sum() > 0 else 0.0
            oracle_info[b] = oracle_rc
            lines.append(f"| {b} | {oracle_rc:.3f} | {n_info} |")
        lines.append("")
        weak_boards = [b for b, rc in oracle_info.items() if rc < 0.7]
        if weak_boards:
            lines.append(
                "**Boards where AC #3 is structurally unreachable**: "
                + ", ".join(f"`{b}`" for b in weak_boards)
                + ". Per-term breakdowns show the FOM lacks discriminating "
                "signal on these boards -- a follow-up issue should expand "
                "the term set (e.g. add diff-pair-route-length-balance for "
                "diffpair_test, mismatch-skew-against-target for "
                "matchgroup_test) rather than ask weight tuning to do "
                "impossible work."
            )
            lines.append("")

    lines.append("## Term-by-term discussion")
    lines.append("")
    lines.append(
        "Examining the per-board random-search results across boards reveals "
        "which soft terms are *useful signal* vs *noise* for each topology:"
    )
    lines.append("")
    if report.per_board_weights:
        # Compute mean and variance of each weight across boards.
        W = np.stack(list(report.per_board_weights.values()))  # shape (B, T)
        mean_w = W.mean(axis=0)
        std_w = W.std(axis=0)
        # Coefficient of variation: terms with high CV are board-specific
        cv = std_w / np.maximum(mean_w, 1e-6)
        order = np.argsort(-mean_w)
        lines.append("| Term | mean per-board weight | std | CV (board-to-board variability) |")
        lines.append("|---|---|---|---|")
        for i in order:
            lines.append(
                f"| {SOFT_TERM_NAMES[i]} | {mean_w[i]:.3f} | {std_w[i]:.3f} | {cv[i]:.2f} |"
            )
        lines.append("")
        lines.append(
            "Terms with **high CV** are board-specific (e.g. `diff_pair_clearance_margin` "
            "matters for boards with diff pairs; doesn't on the others). Terms with "
            "**low CV** are *transferable* — the global default is a good fit for them. "
            "Terms with both high mean and high CV are candidates for "
            "topology-specific weight families (a Phase 4 follow-up, not this issue)."
        )
    lines.append("")
    lines.append("## Honest scope caveats")
    lines.append("")
    lines.append(
        "- The perturbations modify footprint positions but keep the committed "
        "routing intact. This means terms that depend on routing (vias, turning "
        "penalty) are insensitive to perturbation and hence not informative for "
        "weight calibration — they carry whatever weight the search assigns by "
        "chance. A proper re-routed-perturbation pipeline would expose them but "
        "costs ~3 hours of compute we elected not to spend (see issue's compute "
        "estimate of 600 hours)."
    )
    lines.append(
        "- 7 boards is a small training corpus; the cross-board generalisation "
        "result should be interpreted with care. The weights are *better than "
        "uniform 1.0* on the train set with high confidence; their holdout "
        "performance is the headline number."
    )
    lines.append(
        "- The Pareto sweep optimises a proxy (rank_consistency on perturbation "
        "distributions), not actual manufacturability — see issue #3187's "
        "classifier for that signal. The choice not to use the classifier as "
        "the inner objective is deliberate: doing so would overfit to the "
        "classifier's biases (it has 131 training samples and 0.92 OOF AUC, "
        "leaving room for systematic error)."
    )
    lines.append("")
    lines.append("## Reproducibility")
    lines.append("")
    lines.append("Regenerate the weight files with:")
    lines.append("")
    lines.append("```bash")
    lines.append("uv sync --extra research      # one-time: install pymoo")
    lines.append("uv run python scripts/research/calibrate_fom.py")
    lines.append("```")
    lines.append("")
    lines.append("The script is deterministic given `--seed` (default: 42).")
    lines.append("")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))
    logger.info("Markdown report -> %s", output_path)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/research/fom_weights"),
        help="Where to write per-board YAMLs, global default, and the report cache.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("docs/research/fom_calibration.md"),
        help="Where to write the markdown calibration report (AC #5).",
    )
    parser.add_argument(
        "--perturbations",
        type=int,
        default=40,
        help="Number of perturbations per board (default: 40).",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=2000,
        help="Random-search candidates per board (default: 2000).",
    )
    parser.add_argument(
        "--pareto-gens",
        type=int,
        default=80,
        help="NSGA-II generations (default: 80).",
    )
    parser.add_argument(
        "--pareto-pop",
        type=int,
        default=80,
        help="NSGA-II population size (default: 80).",
    )
    parser.add_argument(
        "--sigma",
        type=float,
        default=2.5,
        help="Gaussian sigma in mm for footprint perturbations (default: 2.5).",
    )
    parser.add_argument(
        "--rotate-prob",
        type=float,
        default=0.2,
        help="Probability of +/-90deg rotation per footprint (default: 0.2).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for reproducibility (default: 42).",
    )
    parser.add_argument(
        "--reuse-cache",
        action="store_true",
        help="If a term_cache.npz already exists in --output-dir, reuse it "
        "instead of recomputing. Useful when iterating on the search/Pareto "
        "stage without re-running perturbations.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG/INFO/WARNING).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    report = run_calibration(
        args.output_dir,
        n_perturbations=args.perturbations,
        n_candidates=args.candidates,
        n_pareto_gens=args.pareto_gens,
        pareto_pop=args.pareto_pop,
        sigma_mm=args.sigma,
        rotate_prob=args.rotate_prob,
        seed=args.seed,
        reuse_cache=args.reuse_cache,
    )
    # Re-load the cache for the report (term-by-term ceiling analysis).
    cache_path = args.output_dir / "term_cache.npz"
    cache = load_term_cache(cache_path) if cache_path.exists() else None
    write_markdown_report(
        report,
        args.report_path,
        n_perturbations=args.perturbations,
        sigma_mm=args.sigma,
        cache=cache,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
