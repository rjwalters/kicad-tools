#!/usr/bin/env python3
"""Score negative controls against the trained Phase 0 classifier.

Confounder check (issue #3187 risk #2): the model should label
artificially-broken placements (``negatives.jsonl``) with *lower*
probability than random samples from the positive corpus.  If it
doesn't, the model is learning the perturbation procedure rather
than a true manufacturability signal.

Usage::

    python scripts/research/check_negatives.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import joblib
import pandas as pd

logger = logging.getLogger("fom_phase0.negatives")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--negatives",
        type=Path,
        default=Path("data/research/fom_phase0/negatives.jsonl"),
    )
    parser.add_argument(
        "--classifier",
        type=Path,
        default=Path("data/research/fom_phase0/classifier.joblib"),
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("data/research/fom_phase0/labels.jsonl"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/research/fom_phase0/negative_control_report.json"),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.classifier.exists():
        logger.error(
            "Classifier not found at %s. Run train_phase0_classifier.py first "
            "(and ensure AUC > 0.7).",
            args.classifier,
        )
        return 1

    bundle = joblib.load(args.classifier)
    estimator = bundle["estimator"]
    feat_names = bundle["feature_names"]

    # Score negatives.
    neg_rows = []
    with args.negatives.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            feats = d.get("features", {})
            neg_rows.append(
                {
                    "sample_id": d["sample_id"],
                    "flavour": d["flavour"],
                    "seed_path": d["seed_path"],
                    **{f"feat__{n}": float(feats.get(n, 0.0)) for n in feat_names},
                }
            )
    neg_df = pd.DataFrame(neg_rows)
    X_neg = neg_df[[f"feat__{n}" for n in feat_names]].to_numpy(float)
    neg_df["pred"] = estimator.predict_proba(X_neg)[:, 1]

    # Score positive corpus for reference distribution.
    pos_rows = []
    with args.labels.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            feats = d.get("features", {})
            pos_rows.append(
                {
                    "sample_id": d["sample_id"],
                    "label": d["label"],
                    **{f"feat__{n}": float(feats.get(n, 0.0)) for n in feat_names},
                }
            )
    pos_df = pd.DataFrame(pos_rows)
    X_pos = pos_df[[f"feat__{n}" for n in feat_names]].to_numpy(float)
    pos_df["pred"] = estimator.predict_proba(X_pos)[:, 1]

    report = {
        "n_negatives": int(len(neg_df)),
        "n_corpus": int(len(pos_df)),
        "negatives_mean_pred": float(neg_df["pred"].mean()),
        "negatives_median_pred": float(neg_df["pred"].median()),
        "negatives_below_0p5_pct": float((neg_df["pred"] < 0.5).mean() * 100),
        "negatives_below_0p3_pct": float((neg_df["pred"] < 0.3).mean() * 100),
        "corpus_positive_mean_pred": float(pos_df.loc[pos_df.label == 1, "pred"].mean())
        if (pos_df.label == 1).any()
        else float("nan"),
        "corpus_negative_mean_pred": float(pos_df.loc[pos_df.label == 0, "pred"].mean())
        if (pos_df.label == 0).any()
        else float("nan"),
        "by_flavour": {
            flavour: {
                "n": int((neg_df["flavour"] == flavour).sum()),
                "mean_pred": float(neg_df.loc[neg_df["flavour"] == flavour, "pred"].mean()),
            }
            for flavour in neg_df["flavour"].unique()
        },
    }

    # Heuristic verdict
    if report["negatives_mean_pred"] < 0.4 and report["negatives_below_0p5_pct"] > 60.0:
        verdict = "PASS"
    elif report["negatives_mean_pred"] < 0.6:
        verdict = "MIXED"
    else:
        verdict = "FAIL"
    report["verdict"] = verdict
    report["verdict_explanation"] = {
        "PASS": "Negatives score systematically lower than positive corpus -- the model is detecting badness, not the perturbation procedure.",
        "MIXED": "Negatives partially separated; signal exists but with bleed-through.",
        "FAIL": "Negatives score similarly or higher than positives -- model may be learning the perturbation noise distribution rather than manufacturability.",
    }[verdict]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
