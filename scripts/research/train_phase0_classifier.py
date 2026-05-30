#!/usr/bin/env python3
"""Train + evaluate the Phase 0 binary classifier for issue #3187.

This is the reproducible driver behind ``notebooks/fom_phase0.ipynb``.  The
notebook calls into the functions defined here so the same code runs from
both Jupyter and a plain ``python ... train_phase0_classifier.py`` command.

Reads ``data/research/fom_phase0/labels.jsonl`` (the JSONL output of
``scripts/research/generate_perturbations.py``), trains a
``HistGradientBoostingClassifier`` with 5-fold leave-one-seed-out cross
validation, and emits:

- ``data/research/fom_phase0/metrics.json``       -- AUC + calibration summary
- ``data/research/fom_phase0/feature_importances.csv``
- ``data/research/fom_phase0/per_seed_performance.csv``
- ``data/research/fom_phase0/classifier.joblib``   (only if AUC > 0.7)
- ``data/research/fom_phase0/calibration_plot.png``
- ``data/research/fom_phase0/feature_importance_plot.png``

If AUC > 0.7 the classifier is saved with a ``predict_proba`` callable, ready
to be plugged into ``compute_fom(..., predictor=load_classifier(), beta=0.1)``
from issue #3186.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import joblib
import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.calibration import calibration_curve  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.inspection import permutation_importance  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    brier_score_loss,
    roc_auc_score,
)
from sklearn.model_selection import GroupKFold  # noqa: E402

from kicad_tools.optim.fom_features import PHASE0_FEATURE_NAMES  # noqa: E402

logger = logging.getLogger("fom_phase0.train")

OUT_DIR_DEFAULT = Path("data/research/fom_phase0")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_labels_jsonl(path: Path) -> pd.DataFrame:
    """Load the JSONL labels file into a flat DataFrame.

    Features are unpacked into per-column floats.  Returns one row per sample.
    """
    rows: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            feats = d.pop("features", {}) or {}
            for name in PHASE0_FEATURE_NAMES:
                d[f"feat__{name}"] = float(feats.get(name, 0.0))
            rows.append(d)
    df = pd.DataFrame(rows)
    return df


def feature_columns() -> list[str]:
    return [f"feat__{n}" for n in PHASE0_FEATURE_NAMES]


# ---------------------------------------------------------------------------
# Modeling
# ---------------------------------------------------------------------------


@dataclass
class FoldResult:
    seed_holdout: str
    n_train: int
    n_test: int
    n_pos_test: int
    auc: float
    ap: float
    brier: float
    pred: np.ndarray
    y_true: np.ndarray


def make_classifier() -> HistGradientBoostingClassifier:
    """The Phase 0 model.

    HistGradientBoostingClassifier is sklearn-native (no extra deps), fast
    on small tabular data, and supports ``class_weight`` for the imbalance
    the corpus carries.  Hyperparameters are conservative; we're testing
    signal existence, not chasing the last 2% of AUC.
    """
    return HistGradientBoostingClassifier(
        loss="log_loss",
        max_iter=300,
        learning_rate=0.05,
        max_depth=6,
        l2_regularization=1.0,
        random_state=0,
        class_weight="balanced",
    )


def cross_validate_across_seeds(
    df: pd.DataFrame,
    feature_cols: Sequence[str],
    label_col: str = "label",
    group_col: str = "seed_name",
) -> tuple[list[FoldResult], np.ndarray, np.ndarray]:
    """Leave-one-seed-out cross validation.

    Splits the corpus on the ``group_col`` so the holdout fold contains *only*
    samples from seeds not seen during training -- this is the spec's
    no-data-leakage requirement.

    Returns:
        (folds, all_y_true, all_pred): the per-fold result list plus the
        concatenated OOF predictions (for the global AUC / calibration).
    """
    X = df[list(feature_cols)].to_numpy(dtype=float)
    y = df[label_col].to_numpy(dtype=int)
    groups = df[group_col].to_numpy()

    n_groups = len(set(groups))
    n_splits = min(5, n_groups)
    if n_splits < 2:
        raise ValueError(
            f"Cross-validation needs at least 2 seeds; got {n_groups}. "
            "Generate samples from more boards before training."
        )

    gkf = GroupKFold(n_splits=n_splits)
    folds: list[FoldResult] = []
    all_y_true: list[np.ndarray] = []
    all_pred: list[np.ndarray] = []

    for fold_idx, (train_idx, test_idx) in enumerate(gkf.split(X, y, groups)):
        clf = make_classifier()
        if len(set(y[train_idx])) < 2:
            logger.warning("Fold %d: train set has only one class; skipping.", fold_idx)
            continue
        clf.fit(X[train_idx], y[train_idx])
        proba = clf.predict_proba(X[test_idx])[:, 1]

        y_test = y[test_idx]
        if len(set(y_test)) < 2:
            auc = float("nan")
            ap = float("nan")
        else:
            auc = float(roc_auc_score(y_test, proba))
            ap = float(average_precision_score(y_test, proba))
        brier = float(brier_score_loss(y_test, proba)) if len(y_test) else float("nan")

        holdout_groups = sorted(set(groups[test_idx]))
        folds.append(
            FoldResult(
                seed_holdout=",".join(holdout_groups),
                n_train=int(len(train_idx)),
                n_test=int(len(test_idx)),
                n_pos_test=int(y_test.sum()),
                auc=auc,
                ap=ap,
                brier=brier,
                pred=proba,
                y_true=y_test,
            )
        )
        all_y_true.append(y_test)
        all_pred.append(proba)

    return folds, np.concatenate(all_y_true), np.concatenate(all_pred)


def compute_metrics(folds: Sequence[FoldResult], y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """Aggregate per-fold + global metrics into a dict."""
    valid_auc = [f.auc for f in folds if not np.isnan(f.auc)]
    valid_ap = [f.ap for f in folds if not np.isnan(f.ap)]
    if len(set(y_true)) >= 2:
        global_auc = float(roc_auc_score(y_true, y_pred))
        global_ap = float(average_precision_score(y_true, y_pred))
    else:
        global_auc = float("nan")
        global_ap = float("nan")

    # Calibration: at predicted P > 0.8, what fraction are positive?
    if len(y_true) and (y_pred > 0.8).any():
        mask = y_pred > 0.8
        prec_at_0p8 = float(y_true[mask].mean())
        n_at_0p8 = int(mask.sum())
    else:
        prec_at_0p8 = float("nan")
        n_at_0p8 = 0

    return {
        "n_samples": int(len(y_true)),
        "n_positive": int(int(y_true.sum())),
        "n_folds": len(folds),
        "auc_mean": float(np.mean(valid_auc)) if valid_auc else float("nan"),
        "auc_std": float(np.std(valid_auc)) if valid_auc else float("nan"),
        "ap_mean": float(np.mean(valid_ap)) if valid_ap else float("nan"),
        "auc_global_oof": global_auc,
        "ap_global_oof": global_ap,
        "brier_mean": float(np.mean([f.brier for f in folds])) if folds else float("nan"),
        "precision_at_pred_gt_0p8": prec_at_0p8,
        "n_at_pred_gt_0p8": n_at_0p8,
    }


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------


def plot_calibration(y_true: np.ndarray, y_pred: np.ndarray, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(5, 5))
    if len(set(y_true)) >= 2:
        try:
            prob_true, prob_pred = calibration_curve(y_true, y_pred, n_bins=10, strategy="quantile")
            ax.plot(prob_pred, prob_true, "o-", label="model")
        except ValueError:
            ax.text(
                0.5,
                0.5,
                "calibration_curve failed (degenerate bins)",
                ha="center",
                va="center",
                transform=ax.transAxes,
            )
    else:
        ax.text(
            0.5, 0.5, "Only one class in y_true", ha="center", va="center", transform=ax.transAxes
        )
    ax.plot([0, 1], [0, 1], "--", color="grey", label="ideal")
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Fraction of positives")
    ax.set_title("Phase 0 calibration (leave-one-seed-out OOF)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_feature_importance(importance_df: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 6))
    df_sorted = importance_df.sort_values("importance_mean", ascending=True)
    ax.barh(df_sorted["feature"], df_sorted["importance_mean"], xerr=df_sorted["importance_std"])
    ax.set_xlabel("Permutation importance (mean +/- std)")
    ax.set_title("Phase 0 feature importance")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_score_distribution(y_true: np.ndarray, y_pred: np.ndarray, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist(y_pred[y_true == 1], bins=20, alpha=0.6, label="label=1 (routes clean)")
    ax.hist(y_pred[y_true == 0], bins=20, alpha=0.6, label="label=0 (fails)")
    ax.set_xlabel("Predicted P(manufacturable)")
    ax.set_ylabel("Count")
    ax.set_title("Phase 0 predicted-probability distribution")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Feature importance via permutation on a full-corpus refit
# ---------------------------------------------------------------------------


def feature_importance_df(
    df: pd.DataFrame,
    feature_cols: Sequence[str],
    label_col: str = "label",
    n_repeats: int = 5,
    random_state: int = 0,
) -> tuple[pd.DataFrame, HistGradientBoostingClassifier]:
    """Refit on the full corpus, then report permutation importance."""
    X = df[list(feature_cols)].to_numpy(dtype=float)
    y = df[label_col].to_numpy(dtype=int)
    clf = make_classifier()
    if len(set(y)) < 2:
        # Degenerate corpus; return zeros and a trivial classifier.
        clf.fit(X, y)
        return (
            pd.DataFrame(
                {
                    "feature": [c.replace("feat__", "") for c in feature_cols],
                    "importance_mean": [0.0] * len(feature_cols),
                    "importance_std": [0.0] * len(feature_cols),
                }
            ),
            clf,
        )
    clf.fit(X, y)
    result = permutation_importance(
        clf, X, y, n_repeats=n_repeats, random_state=random_state, n_jobs=1
    )
    imp = pd.DataFrame(
        {
            "feature": [c.replace("feat__", "") for c in feature_cols],
            "importance_mean": result.importances_mean,
            "importance_std": result.importances_std,
        }
    )
    return imp.sort_values("importance_mean", ascending=False).reset_index(drop=True), clf


# ---------------------------------------------------------------------------
# Per-seed performance
# ---------------------------------------------------------------------------


def per_seed_performance(folds: Sequence[FoldResult]) -> pd.DataFrame:
    rows = []
    for f in folds:
        rows.append(
            {
                "holdout_seed": f.seed_holdout,
                "n_train": f.n_train,
                "n_test": f.n_test,
                "n_pos_test": f.n_pos_test,
                "auc": f.auc,
                "ap": f.ap,
                "brier": f.brier,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def go_iterate_abandon(metrics: dict) -> tuple[str, str]:
    """Apply the issue's go/iterate/abandon rule on the global OOF AUC."""
    auc = metrics["auc_global_oof"]
    if np.isnan(auc):
        return ("inconclusive", "AUC could not be computed (degenerate folds).")
    if auc > 0.70:
        return (
            "escalate",
            f"Global OOF AUC = {auc:.3f} > 0.70 -- signal exists. "
            "Phase 1: integrate as residual into compute_fom(..., predictor=load_classifier(), beta=0.1).",
        )
    if auc > 0.55:
        return (
            "iterate",
            f"Global OOF AUC = {auc:.3f} in (0.55, 0.70]. "
            "Some signal but not enough to justify integration. "
            "Iterate features (esp. those with low permutation importance) or expand corpus.",
        )
    return (
        "abandon",
        f"Global OOF AUC = {auc:.3f} <= 0.55 -- no learnable signal at this corpus size. "
        "Pivot to CNN-style features or larger seed corpus, or close the workstream.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--labels", type=Path, default=Path("data/research/fom_phase0/labels.jsonl")
    )
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR_DEFAULT)
    parser.add_argument(
        "--min-samples",
        type=int,
        default=30,
        help="Refuse to train if fewer than this many samples.",
    )
    parser.add_argument(
        "--save-threshold-auc",
        type=float,
        default=0.70,
        help="Save classifier.joblib only if global OOF AUC > this.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if not args.labels.exists():
        logger.error("Labels file not found: %s", args.labels)
        return 1

    df = load_labels_jsonl(args.labels)
    logger.info(
        "Loaded %d samples; positive rate: %.1f%%",
        len(df),
        100.0 * df["label"].mean() if len(df) else 0.0,
    )
    logger.info("Seeds present: %s", sorted(df["seed_name"].unique()))

    if len(df) < args.min_samples:
        logger.error(
            "Need at least %d samples; got %d. Run the corpus generator longer.",
            args.min_samples,
            len(df),
        )
        return 1

    # Snapshot the labelled corpus to parquet for downstream tooling and the
    # AC #2 deliverable.
    try:
        df.to_parquet(args.out_dir / "labels.parquet")
        logger.info("Wrote labels.parquet (%d rows).", len(df))
    except Exception as exc:  # pragma: no cover -- pyarrow may not be installed
        logger.warning("Could not write labels.parquet: %s", exc)

    fcols = feature_columns()

    folds, oof_y, oof_pred = cross_validate_across_seeds(df, fcols)
    metrics = compute_metrics(folds, oof_y, oof_pred)
    metrics["seeds_in_corpus"] = sorted(df["seed_name"].unique())

    decision, rationale = go_iterate_abandon(metrics)
    metrics["decision"] = decision
    metrics["decision_rationale"] = rationale

    # Per-seed table
    per_seed_df = per_seed_performance(folds)
    per_seed_df.to_csv(args.out_dir / "per_seed_performance.csv", index=False)

    # Feature importance
    importance_df, full_clf = feature_importance_df(df, fcols)
    importance_df.to_csv(args.out_dir / "feature_importances.csv", index=False)

    # Plots
    plot_calibration(oof_y, oof_pred, args.out_dir / "calibration_plot.png")
    plot_feature_importance(importance_df, args.out_dir / "feature_importance_plot.png")
    plot_score_distribution(oof_y, oof_pred, args.out_dir / "score_distribution.png")

    # Metrics summary
    with (args.out_dir / "metrics.json").open("w") as fh:
        json.dump(metrics, fh, indent=2, sort_keys=True)

    # Save classifier only if AUC is good enough
    if (metrics["auc_global_oof"] or 0.0) > args.save_threshold_auc:
        joblib.dump(
            {
                "estimator": full_clf,
                "feature_names": list(PHASE0_FEATURE_NAMES),
                "metrics": metrics,
            },
            args.out_dir / "classifier.joblib",
        )
        logger.info(
            "Classifier saved (AUC=%.3f > %.2f).",
            metrics["auc_global_oof"],
            args.save_threshold_auc,
        )
    else:
        logger.info(
            "Classifier NOT saved (AUC=%.3f <= threshold %.2f).",
            metrics["auc_global_oof"],
            args.save_threshold_auc,
        )

    logger.info("Decision: %s", decision)
    logger.info("%s", rationale)
    logger.info("Outputs in %s", args.out_dir)
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
