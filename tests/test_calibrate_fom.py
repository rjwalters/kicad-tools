"""Tests for the FOM weight calibration pipeline (issue #3188).

These tests cover the building blocks of ``scripts/research/calibrate_fom.py``
without running the full Pareto sweep (which takes several minutes). The
end-to-end pipeline is exercised by running the script manually; here we
verify that the numeric primitives (rank_consistency, discrimination,
saturation_penalty) behave as expected on synthetic data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# The calibration script lives under scripts/, not in the package. Add it
# to the path so we can import its primitives directly for unit testing.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from calibrate_fom import (  # noqa: E402  (sys.path manipulation above)
    SOFT_EXP_CAP,
    composite_from_terms,
    discrimination_ratio,
    rank_consistency,
    saturation_penalty,
)


def _two_term_setup():
    """Return (committed, perturbed) for a simple 2-term-equivalent problem.

    The 10-D vectors only use the first two slots so the test exercises
    rank/discrimination logic without needing realistic PCB data.
    """
    n_terms = 10
    committed = np.zeros(n_terms)
    committed[0] = 1.0  # small term 0 = good
    committed[1] = 0.5

    perturbed = np.zeros((4, n_terms))
    # 3 of 4 perturbations are strictly worse on term 0; 2 are worse on term 1.
    perturbed[:, 0] = [2.0, 3.0, 0.5, 4.0]
    perturbed[:, 1] = [0.6, 0.4, 0.3, 1.0]
    return committed, perturbed


def test_composite_from_terms_scalar():
    """Single-vector input returns exp(-w.t)."""
    weights = np.array([1.0, 1.0] + [0.0] * 8)
    terms = np.array([0.5, 0.5] + [0.0] * 8)
    out = composite_from_terms(terms, weights)
    np.testing.assert_allclose(float(out), np.exp(-1.0))


def test_composite_from_terms_batch():
    """Batch input returns one composite per row."""
    weights = np.ones(10)
    terms = np.zeros((3, 10))
    terms[0, 0] = 1.0  # sum = 1
    terms[1, 0] = 2.0  # sum = 2
    terms[2, 0] = 3.0  # sum = 3
    out = composite_from_terms(terms, weights)
    np.testing.assert_allclose(out, np.exp([-1.0, -2.0, -3.0]))


def test_composite_from_terms_cap_clipping():
    """Large weighted sums saturate at exp(-SOFT_EXP_CAP), no NaN/Inf."""
    weights = np.ones(10)
    terms = np.array([1000.0] + [0.0] * 9)  # weighted sum = 1000
    out = composite_from_terms(terms, weights)
    np.testing.assert_allclose(float(out), np.exp(-SOFT_EXP_CAP))


def test_composite_from_terms_negative_clip():
    """Negative weighted sums clip to 0 (composite never exceeds 1.0)."""
    weights = np.array([1.0] + [0.0] * 9)
    terms = np.array([-5.0] + [0.0] * 9)
    out = composite_from_terms(terms, weights)
    # Clipped to 0 -> exp(0) == 1.0
    np.testing.assert_allclose(float(out), 1.0)


def test_rank_consistency_perfect():
    """Committed strictly smaller than all perturbations -> rc = 1.0."""
    committed, perturbed = _two_term_setup()
    weights = np.array([1.0] + [0.0] * 9)  # only term 0 matters
    rc = rank_consistency(committed, perturbed, weights)
    # 3 of 4 perturbations have term 0 > 1.0; 1 has term 0 = 0.5 < 1.0
    assert rc == 0.75


def test_rank_consistency_zero():
    """Committed worse than all perturbations -> rc = 0.0."""
    committed = np.array([10.0] + [0.0] * 9)
    perturbed = np.zeros((5, 10))
    perturbed[:, 0] = [1.0, 2.0, 3.0, 4.0, 5.0]
    weights = np.array([1.0] + [0.0] * 9)
    rc = rank_consistency(committed, perturbed, weights)
    assert rc == 0.0


def test_rank_consistency_ignores_term_dimensions_with_zero_weight():
    """A zero-weight term doesn't contribute to the ranking."""
    n = 10
    committed = np.zeros(n)
    committed[5] = 100.0  # huge but should be ignored if weight = 0
    perturbed = np.zeros((3, n))
    weights = np.zeros(n)
    weights[0] = 1.0
    # All sums are 0 -> rank_consistency is 0 (no strict greater).
    rc = rank_consistency(committed, perturbed, weights)
    assert rc == 0.0


def test_discrimination_ratio_above_one_when_committed_best():
    committed = np.array([1.0] + [0.0] * 9)
    perturbed = np.zeros((3, 10))
    perturbed[:, 0] = [2.0, 3.0, 4.0]
    weights = np.array([1.0] + [0.0] * 9)
    disc = discrimination_ratio(committed, perturbed, weights)
    # worst perturbed sum = 4.0; committed = 1.0; disc = exp(3) ~ 20.09
    np.testing.assert_allclose(disc, np.exp(3.0))


def test_discrimination_ratio_below_one_when_committed_worst():
    committed = np.array([5.0] + [0.0] * 9)
    perturbed = np.zeros((3, 10))
    perturbed[:, 0] = [1.0, 2.0, 3.0]
    weights = np.array([1.0] + [0.0] * 9)
    disc = discrimination_ratio(committed, perturbed, weights)
    # worst perturbed sum = 3.0; committed = 5.0; disc = exp(-2) ~ 0.135
    np.testing.assert_allclose(disc, np.exp(-2.0))


def test_discrimination_ratio_caps_at_exp_60():
    committed = np.array([1.0] + [0.0] * 9)
    perturbed = np.zeros((1, 10))
    perturbed[:, 0] = [1000.0]  # delta = 999 -> would overflow without cap
    weights = np.array([1.0] + [0.0] * 9)
    disc = discrimination_ratio(committed, perturbed, weights)
    # Cap is SOFT_EXP_CAP = 60.
    np.testing.assert_allclose(disc, np.exp(SOFT_EXP_CAP))


def test_saturation_penalty_full_when_below_half_cap():
    """Sums well below 0.5 * cap get the full 1.0 weight."""
    p = saturation_penalty(committed_sum=10.0, perturbed_sums=np.array([5.0, 8.0]))
    assert p == 1.0


def test_saturation_penalty_zero_when_above_cap():
    """Sums equal to the cap get zero penalty (driven to floor)."""
    p = saturation_penalty(committed_sum=60.0, perturbed_sums=np.array([60.0]))
    assert p == 0.0


def test_saturation_penalty_smooth_in_middle():
    """Sums between 0.5*cap and cap get a fractional weight."""
    target = 0.5 * SOFT_EXP_CAP  # 30
    # committed_sum = target + 15 -> halfway through the penalty range
    p = saturation_penalty(committed_sum=target + 15.0, perturbed_sums=np.array([0.0]))
    np.testing.assert_allclose(p, 0.5)


# --------------------------------------------------------------------
# Optional: integration smoke test that runs a tiny calibration end-to-end.
# Skipped by default because it takes ~30 seconds; opt in with
# KICAD_FOM_INTEGRATION=1.
# --------------------------------------------------------------------


def test_shipped_calibration_yamls_load():
    """Every YAML in ``data/research/fom_weights/`` should be loadable as
    valid :class:`FOMWeights`. This is the contract for AC #1 (per-board
    YAMLs) and AC #2 (global default).
    """
    from kicad_tools.optim.fom import SOFT_TERM_NAMES, load_weights_from_yaml

    data_dir = ROOT / "data" / "research" / "fom_weights"
    if not data_dir.is_dir():
        pytest.skip("calibration output dir not present (run calibrate_fom.py first)")
    yamls = sorted(data_dir.glob("*.yaml"))
    # AC #1: per-board YAMLs for the 7 in-repo boards + default.
    expected_boards = {
        "voltage_divider",
        "charlieplex_3x3",
        "usb_joystick",
        "stm32_devboard",
        "bldc_controller",
        "diffpair_test",
        "matchgroup_test",
    }
    found_boards = {p.stem for p in yamls} - {"default"}
    missing = expected_boards - found_boards
    assert not missing, f"Missing per-board YAMLs: {missing}"
    # Every YAML loads cleanly and produces positive weights for every term.
    for path in yamls:
        w = load_weights_from_yaml(path)
        for name in SOFT_TERM_NAMES:
            v = getattr(w, name)
            assert v >= 0.0, f"{path.name}: {name} is negative ({v})"


@pytest.mark.skipif(
    "KICAD_FOM_INTEGRATION" not in __import__("os").environ,
    reason="Integration smoke test; set KICAD_FOM_INTEGRATION=1 to run.",
)
def test_run_calibration_smoke(tmp_path: Path):
    """End-to-end smoke test of run_calibration on synthetic-tiny config."""
    from calibrate_fom import run_calibration

    report = run_calibration(
        tmp_path,
        n_perturbations=3,
        n_candidates=10,
        n_pareto_gens=3,
        pareto_pop=8,
        sigma_mm=2.5,
        rotate_prob=0.2,
        seed=7,
        reuse_cache=False,
    )
    # Every YAML the script promises should be on disk.
    assert (tmp_path / "default.yaml").exists()
    # At least one per-board YAML.
    assert any(p.name.endswith(".yaml") and p.name != "default.yaml" for p in tmp_path.iterdir())
    assert report.global_weights is not None


def test_markdown_report_carries_addenda_forward(tmp_path: Path):
    """Regenerating the report must keep hand-written ``## Addendum`` sections.

    Issue #6021: re-running ``calibrate_fom.py`` used to overwrite
    ``docs/research/fom_calibration.md`` and drop the #5984 addendum.
    """
    from calibrate_fom import CalibrationReport, write_markdown_report

    report_path = tmp_path / "fom_calibration.md"
    addenda = (
        "## Addendum: first re-check (issue #1)\n\nKeep 0.0181.\n\n"
        "## Addendum: second re-check (issue #2)\n\nMoved three weights.\n"
    )
    report_path.write_text("# stale generated body\n\nold numbers\n\n" + addenda)

    write_markdown_report(CalibrationReport(), report_path, n_perturbations=3, sigma_mm=2.5)
    text = report_path.read_text()
    assert "old numbers" not in text
    assert text.startswith("# FOM Weight Calibration Report")
    assert text.endswith(addenda)
    assert text.count("## Addendum") == 2

    # Idempotent: a second regeneration does not duplicate the addenda.
    write_markdown_report(CalibrationReport(), report_path, n_perturbations=3, sigma_mm=2.5)
    assert report_path.read_text().count("## Addendum") == 2


def test_markdown_report_without_existing_file(tmp_path: Path):
    from calibrate_fom import CalibrationReport, write_markdown_report

    report_path = tmp_path / "new.md"
    write_markdown_report(CalibrationReport(), report_path, n_perturbations=3, sigma_mm=2.5)
    assert "## Addendum" not in report_path.read_text()


def test_board_seed_independent_of_pythonhashseed():
    """Per-board seeds must not depend on PYTHONHASHSEED (issue #6111)."""
    import os
    import subprocess

    code = (
        "import sys; sys.path.insert(0, %r); "
        "from calibrate_fom import board_seed; "
        "print([board_seed(42, b) for b in ('01-foo', '02-bar', 'x')])"
    ) % str(ROOT / "scripts" / "research")
    outs = []
    for hs in ("1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hs}
        r = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        )
        outs.append(r.stdout)
    assert outs[0] == outs[1]
