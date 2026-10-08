"""Unit tests for scripts/research/placement_score_calibration.py (issue #5948)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

_PATH = Path(__file__).resolve().parents[1] / "scripts/research/placement_score_calibration.py"
_spec = importlib.util.spec_from_file_location("placement_score_calibration", _PATH)
psc = importlib.util.module_from_spec(_spec)
sys.modules["placement_score_calibration"] = psc
_spec.loader.exec_module(psc)


def test_severity_orders_route_failure_worst():
    assert psc.severity({"route_ok": True, "drc_errors": 0}) == 0
    assert psc.severity({"route_ok": True, "drc_errors": 3}) == 3
    assert psc.severity({"route_ok": False, "drc_errors": -1}) == psc.ROUTE_FAIL_SEVERITY


def test_pairs_skip_equal_severity_and_accuracy_counts_ties_half():
    rows = [{"severity": 0}, {"severity": 0}, {"severity": 2}]
    pairs = psc.make_pairs(rows)
    assert sorted(pairs) == [(0, 2), (1, 2)]
    assert psc.pair_accuracy(np.array([1.0, 3.0, 2.0]), pairs) == 0.5
    assert psc.pair_accuracy(np.array([1.0, 1.0, 1.0]), pairs) == 0.5
    assert psc.pair_accuracy(np.array([1.0, 1.0, 2.0]), pairs) == 1.0


def test_evaluate_perfect_signal_and_unusable_board():
    def row(v, sev):
        r = dict.fromkeys(psc.SIGNALS, v)
        r["severity"] = sev
        return r

    rows = {
        "a": [row(1.0, 0), row(2.0, 1), row(3.0, 2)],
        "b": [row(1.0, 0), row(2.0, 1), row(4.0, 3)],
        "dead": [row(1.0, 1000), row(2.0, 1000)],
    }
    res = psc.evaluate(rows)
    assert res["n_pairs"]["dead"] == 0
    assert res["pooled"]["hpwl"] == 1.0
    assert res["pooled"]["fitted_lobo"] == 1.0
