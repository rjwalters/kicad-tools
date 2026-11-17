"""Pins for ``scripts/research/placement_score_calibration.py`` (Issues #5948, #6233).

The ranking-accuracy tables in ``docs/research/placement_score_calibration.md``
are only as trustworthy as the arithmetic behind them and the data they quote,
so these tests pin, without routing anything:

Corpus B (fresh routed fleet corpus):

* ``rankdata`` / ``spearman`` agree with scipy's average-rank definition;
* ``routability_compare`` orders by completion first, then DRC errors;
* ``build_pairs`` never pairs placements across boards and drops ties;
* ``pairwise_accuracy`` treats lower-is-better and scores ties as 0.5;
* ``fit_pairwise_logistic`` recovers the informative feature and is
  antisymmetric (no intercept);
* ``lobo_evaluate`` holds each board out and never fits on its pairs;
* ``gate_table`` counts both directions of every pair;
* ``analyze`` on the committed labels reproduces the committed analyses.

Corpus A (recorded Phase 0 labels):

* ``severity`` ranks a failed route worst;
* ``make_pairs`` / ``pair_accuracy`` skip equal severity and score ties 0.5;
* ``evaluate`` scores a perfect signal 1.0 and skips boards with no pairs.

Both: the doc's headline table rows match the committed JSON.

The script lives under ``scripts/`` (not ``src/``), so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "placement_score_calibration.py"
DATA = REPO_ROOT / "data" / "research" / "placement_score"
DOC = REPO_ROOT / "docs" / "research" / "placement_score_calibration.md"


def _load():
    spec = importlib.util.spec_from_file_location("placement_score_calibration_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()


def _row(board, completion, drc, **signals):
    return {"board": board, "completion": completion, "drc_errors": drc, "signals": signals}


def test_rankdata_average_ties():
    assert psc.rankdata([10, 20, 20, 5]).tolist() == [2.0, 3.5, 3.5, 1.0]


def test_spearman_perfect_and_inverse_and_constant():
    assert psc.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert psc.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert psc.spearman([1, 2, 3, 4], [7, 7, 7, 7]) is None
    assert psc.spearman([1, 2], [1, 2]) is None


def test_spearman_with_ties_matches_pearson_of_ranks():
    x = [1, 2, 2, 3, 5]
    y = [2, 1, 4, 4, 9]
    rx, ry = psc.rankdata(x), psc.rankdata(y)
    expected = float(np.corrcoef(rx, ry)[0, 1])
    assert psc.spearman(x, y) == pytest.approx(expected)


def test_routability_compare_completion_then_drc():
    a = _row("b", 100.0, 5)
    b = _row("b", 90.0, 0)
    c = _row("b", 100.0, 2)
    assert psc.routability_compare(a, b) == 1  # completion dominates DRC
    assert psc.routability_compare(b, a) == -1
    assert psc.routability_compare(c, a) == 1  # equal completion: fewer DRC wins
    assert psc.routability_compare(a, dict(a)) == 0


def test_build_pairs_same_board_only_and_drops_ties():
    rows = [
        _row("A", 100.0, 0),
        _row("A", 50.0, 0),
        _row("A", 50.0, 0),  # ties with row 1 -> no pair
        _row("B", 10.0, 0),  # different board -> never paired with A
    ]
    pairs = psc.build_pairs(rows)
    assert sorted(pairs) == [(0, 1), (0, 2)]


def test_pairwise_accuracy_lower_is_better_and_ties_half():
    pairs = [(0, 1), (2, 3)]
    assert psc.pairwise_accuracy([1.0, 2.0, 1.0, 2.0], pairs) == 1.0
    assert psc.pairwise_accuracy([2.0, 1.0, 2.0, 1.0], pairs) == 0.0
    assert psc.pairwise_accuracy([5.0, 5.0, 1.0, 2.0], pairs) == 0.75
    assert psc.pairwise_accuracy([3.0] * 4, pairs) == 0.5
    assert psc.pairwise_accuracy([1.0], []) is None


def test_fit_pairwise_logistic_recovers_informative_feature():
    rng = np.random.default_rng(0)
    # Feature 0 decides the outcome; feature 1 is noise.
    diffs = np.column_stack([np.abs(rng.normal(1.0, 0.3, 200)), rng.normal(0.0, 1.0, 200)])
    w = psc.fit_pairwise_logistic(diffs, l2=1.0)
    assert w[0] > 0
    assert abs(w[0]) > 5 * abs(w[1])
    # No intercept: a zero difference is exactly a coin flip.
    assert float(psc.sigmoid(np.zeros(2) @ w)) == pytest.approx(0.5)


def test_fit_pairwise_logistic_empty_is_zero():
    assert psc.fit_pairwise_logistic(np.zeros((0, 3))).tolist() == [0.0, 0.0, 0.0]


def _synthetic_corpus(n_boards=4, per_board=8, seed=1):
    rnd = random.Random(seed)
    rows = []
    for b in range(n_boards):
        scale = 10.0**b  # boards differ wildly in absolute magnitude
        for _ in range(per_board):
            good = rnd.random()
            rows.append(
                _row(
                    f"board{b}",
                    completion=round(100 * good, 1),
                    drc=0,
                    hpwl=scale * (2.0 - good),  # informative (lower = better)
                    noise=scale * rnd.random(),
                )
            )
    return rows


def test_lobo_evaluate_learns_signal_across_board_scales():
    rows = _synthetic_corpus()
    res = psc.lobo_evaluate(rows, ["hpwl", "noise"])
    assert set(res["per_board"]) == {f"board{b}" for b in range(4)}
    assert res["n_pairs"] == len(psc.build_pairs(rows))
    assert res["acc_fit_micro"] == pytest.approx(1.0)
    assert all(v["best_single_signal"] == "hpwl" for v in res["per_board"].values())
    assert res["weights_all_boards"]["hpwl"] > 0


def test_lobo_never_trains_on_held_out_board(monkeypatch):
    rows = _synthetic_corpus(n_boards=3)
    seen = []
    real_fit = psc.fit_pairwise_logistic

    def spy(diffs, l2=1.0, iters=50):
        seen.append(len(diffs))
        return real_fit(diffs, l2=l2, iters=iters)

    monkeypatch.setattr(psc, "fit_pairwise_logistic", spy)
    res = psc.lobo_evaluate(rows, ["hpwl", "noise"])
    pairs = psc.build_pairs(rows)
    # One fit per held-out board (on the other boards' pairs) + one on all.
    expected = [len(pairs) - v["n_pairs"] for v in res["per_board"].values()] + [len(pairs)]
    assert seen == expected


def test_gate_table_counts_both_directions():
    # Two pairs: one confidently right (0.9, truth=1), one confidently wrong.
    probs = [(0.9, 1), (0.9, 0)]
    (g,) = psc.gate_table(probs, thresholds=[0.8])
    assert g["accepted"] == 2  # the 0.9 side of each pair
    assert g["precision"] == pytest.approx(0.5)
    assert g["recall"] == pytest.approx(0.5)
    (g5,) = psc.gate_table([(0.5, 1)], thresholds=[0.5])
    assert g5["accepted"] == 2  # both directions sit exactly on the threshold


def test_load_labelled_excludes_or_ranks_timeouts_last(tmp_path):
    sig = {"hpwl_signal": 1.0}
    ok = {
        "sample_id": "b__p000",
        "board": "b",
        "kind": "perturbed",
        "sigma": 1.0,
        "router_seed": 0,
        "signals": sig,
        "route": {"status": "ok", "label_source": "checkpoint", "seconds": 5.0},
        "net_status": {"ok": True, "completion": 0.0, "conn_completion": 10.0},
        "check": {"check_ok": True, "drc_errors": 3},
    }
    timed_out = {
        "sample_id": "b__p001",
        "board": "b",
        "kind": "perturbed",
        "sigma": 4.0,
        "router_seed": 0,
        "signals": sig,
        "route": {"status": "timeout_no_checkpoint", "seconds": 125.0},
    }
    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (ok, timed_out)) + "\n")

    rows, excluded = psc.load_labelled([path])
    assert [r["sample_id"] for r in rows] == ["b__p000"]
    assert excluded == [
        {"sample_id": "b__p001", "board": "b", "reason": "route timeout_no_checkpoint"}
    ]

    rows, excluded = psc.load_labelled([path], timeouts_as_worst=True)
    assert excluded == []
    # Even a 0%-complete routed board outranks a run that never checkpointed.
    assert psc.build_pairs(rows) == [(0, 1)]


# ---------------------------------------------------------------------------
# Corpus A: recorded Phase 0 labels
# ---------------------------------------------------------------------------


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
    assert np.isnan(psc.pair_accuracy(np.array([1.0]), []))


def test_evaluate_perfect_signal_and_unusable_board():
    def row(v, sev):
        r = dict.fromkeys(psc.PHASE0_SIGNALS, v)
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


# ---------------------------------------------------------------------------
# Committed data <-> doc consistency (both corpora)
# ---------------------------------------------------------------------------


def _assert_close(got, want, path=""):
    if isinstance(want, dict):
        assert set(got) == set(want), path
        for k in want:
            _assert_close(got[k], want[k], f"{path}/{k}")
    elif isinstance(want, list):
        assert len(got) == len(want), path
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            _assert_close(g, w, f"{path}[{i}]")
    elif isinstance(want, float):
        assert got == pytest.approx(want, rel=1e-9, abs=1e-12), path
    else:
        assert got == want, path


@pytest.mark.parametrize(
    ("extra", "stem"),
    [([], "analysis"), (["--timeouts-as-worst"], "analysis_timeouts_as_worst")],
)
def test_committed_corpus_b_analysis_reproduces(tmp_path, capsys, extra, stem):
    """``analyze`` on the committed labels rewrites the committed analyses."""
    out = tmp_path / "a.json"
    labels = [str(DATA / "labels_budget45.jsonl"), str(DATA / "labels_budget120.jsonl")]
    assert psc.main(["analyze", *labels, *extra, "--json", str(out)]) == 0
    assert capsys.readouterr().out == (DATA / f"{stem}.md").read_text()
    _assert_close(json.loads(out.read_text()), json.loads((DATA / f"{stem}.json").read_text()))


def _doc_text() -> str:
    return DOC.read_text().replace("**", "")


def test_doc_corpus_a_table_matches_results_json():
    res = json.loads((DATA / "results.json").read_text())
    doc = _doc_text()
    for s in psc.PHASE0_SIGNALS:
        lo, hi = res["bootstrap_95ci_pooled"][s]
        row = (
            f"| `{s}` | {res['pooled'][s]:.3f} | {lo:.2f}-{hi:.2f} "
            f"| {res['boards']['voltage_divider'][s]:.3f} "
            f"| {res['boards']['charlieplex_3x3'][s]:.3f} |"
        )
        assert row in doc, row
    for key, label in (
        ("fitted_lobo", "leave-one-board-out"),
        ("fitted_in_sample", "in-sample (optimistic)"),
    ):
        row = (
            f"| fitted combo, {label} | {res['pooled'][key]:.3f} | -- "
            f"| {res['boards']['voltage_divider'][key]:.3f} "
            f"| {res['boards']['charlieplex_3x3'][key]:.3f} |"
        )
        assert row in doc, row
    assert res["pooled_n_pairs"] == 915


def test_doc_corpus_b_table_matches_analysis_json():
    res = json.loads((DATA / "analysis.json").read_text())
    doc = _doc_text()
    for s in psc.SIGNALS:
        v = res["per_signal"][s]
        row = (
            f"| `{s}` | {v['acc_all_pairs']:.3f} | {v['acc_completion_pairs']:.3f} "
            f"| {v['acc_same_sigma_pairs']:.3f} |"
        )
        assert row in doc, row
    f = res["fit"]
    assert (
        f"| Fitted combination (LOBO) | {f['acc_fit_micro']:.3f} "
        f"(macro {f['acc_fit_macro']:.3f}) | | {res['same_sigma_fit_acc']:.3f} |"
    ) in doc
    for g in res["gate"]:
        row = (
            f"| {g['threshold']:.1f} | {g['accepted']} | {g['precision']:.3f} | {g['recall']:.3f} |"
        )
        assert row in doc, row
    assert (res["n_placements"], res["n_pairs"]) == (85, 370)
