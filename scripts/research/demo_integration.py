#!/usr/bin/env python3
"""Demonstrate that the Phase 0 classifier plugs into the FOM's predictor hook.

Issue #3187 AC #7: if AUC > 0.7 the trained model is saved to
``classifier.joblib`` AND ``compute_fom(..., predictor=load_classifier(),
beta=0.1)`` is demonstrated.

This script is the demonstration.  It loads the classifier, wraps it in
a ``predictor(pcb) -> float`` callable matching #3186's hook signature,
and runs :func:`kicad_tools.optim.fom.compute_fom` on the unrouted board
01 PCB to show the predictor multiplies into the soft-FOM as expected.

Run::

    uv run python scripts/research/demo_integration.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import joblib
import numpy as np

from kicad_tools.optim.fom import compute_fom
from kicad_tools.optim.fom_features import extract_phase0_features_from_pcb
from kicad_tools.schema.pcb import PCB

logger = logging.getLogger("fom_phase0.demo")


def load_predictor(classifier_path: Path):
    """Return a ``predictor(pcb) -> float`` callable backed by the classifier."""
    bundle = joblib.load(classifier_path)
    estimator = bundle["estimator"]
    feat_names = bundle["feature_names"]

    def predictor(pcb: PCB) -> float:
        feats = extract_phase0_features_from_pcb(pcb)
        X = np.array([[feats[n] for n in feat_names]], dtype=float)
        return float(estimator.predict_proba(X)[0, 1])

    return predictor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--classifier",
        type=Path,
        default=Path("data/research/fom_phase0/classifier.joblib"),
    )
    parser.add_argument(
        "--pcb",
        type=Path,
        default=Path("boards/01-voltage-divider/output/voltage_divider.kicad_pcb"),
    )
    parser.add_argument("--beta", type=float, default=0.1)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.classifier.exists():
        logger.error(
            "Classifier not found at %s. Train + save it first with train_phase0_classifier.py.",
            args.classifier,
        )
        return 1

    predictor = load_predictor(args.classifier)
    pcb = PCB.load(args.pcb)

    # Score directly to show the value.
    p = predictor(pcb)
    logger.info("predictor P(manufacturable) on %s = %.4f", args.pcb.name, p)

    # Now plug it into compute_fom with beta = args.beta.
    # We do not supply DRC/ERC reports because we want to see the soft +
    # predictor contribution.  (The hard gate would short-circuit otherwise.)
    res_no_pred = compute_fom(pcb)
    res_with_pred = compute_fom(pcb, predictor=predictor, beta=args.beta)

    logger.info("FOM without predictor: score=%.4f", res_no_pred.score)
    logger.info(
        "FOM with predictor   : score=%.4f  (predictor=%.4f, beta=%.2f)",
        res_with_pred.score,
        res_with_pred.predictor_value,
        res_with_pred.beta,
    )

    # Expected: score_with == score_without * p^beta
    expected = res_no_pred.score * (p**args.beta) if p > 0 else 0.0
    logger.info("Sanity check: soft_score * p^beta = %.4f", expected)

    if abs(expected - res_with_pred.score) > 1e-6:
        logger.error("Mismatch: predictor not applied as documented!")
        return 1

    logger.info(
        "Integration confirmed -- the Phase 0 classifier plugs into "
        "compute_fom() without changes to call sites."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
