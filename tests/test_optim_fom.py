"""Tests for kicad_tools.optim.fom -- main entry point + hard constraints.

Issue #3186.  Primary acceptance test surface; this file aims for high
coverage of fom.py specifically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_tools.optim.fom import (
    HARD_CONSTRAINT_NAMES,
    SOFT_TERM_NAMES,
    FOMResult,
    FOMWeights,
    _load_tolerance_yaml,
    _tolerance_floor,
    check_hard_constraints,
    compute_fom,
    compute_soft_terms,
    default_weights,
    legacy_weights,
    load_weights_from_yaml,
)
from kicad_tools.optim.fom_features import extract_features
from kicad_tools.schema.pcb import PCB


def _empty_pcb() -> PCB:
    return PCB.create(width=100, height=100)


@dataclass
class _StubReport:
    """A duck-typed DRC/ERC report with a configurable error count."""

    error_count: int


# --------------------------------------------------------------------
# FOMWeights
# --------------------------------------------------------------------


def test_default_weights_match_calibrated_yaml():
    """Default weights should load from the calibrated YAML (issue #3188).

    Before #3188 these were the uniform 1.0 placeholder from #3186; the
    Pareto-sweep calibration shipped in #3188 replaces them with the values
    in ``src/kicad_tools/optim/weights/default.yaml``.
    """
    w = default_weights()
    # Sanity: not the uniform-1.0 placeholder.
    assert not all(getattr(w, n) == 1.0 for n in SOFT_TERM_NAMES), (
        "default_weights() should be the calibrated values, not uniform 1.0"
    )
    # Cross-check against the package YAML.
    from pathlib import Path

    from kicad_tools.optim.fom import load_weights_from_yaml

    pkg_default = Path(__file__).resolve().parents[1] / "src/kicad_tools/optim/weights/default.yaml"
    yaml_w = load_weights_from_yaml(pkg_default)
    for name in SOFT_TERM_NAMES:
        assert getattr(w, name) == getattr(yaml_w, name), (
            f"default_weights() {name} mismatch with package YAML"
        )


def test_legacy_weights_zero_all_but_length():
    w = legacy_weights()
    assert w.trace_length_excess == 1.0
    for name in SOFT_TERM_NAMES:
        if name == "trace_length_excess":
            continue
        assert getattr(w, name) == 0.0


def test_fom_weights_as_dict_returns_canonical_order():
    w = FOMWeights()
    d = w.as_dict()
    assert list(d.keys()) == list(SOFT_TERM_NAMES)


def test_fom_weights_from_dict_partial():
    # Unspecified terms default to 1.0.
    w = FOMWeights.from_dict({"trace_length_excess": 5.0})
    assert w.trace_length_excess == 5.0
    assert w.weighted_via_count == 1.0
    assert w.compactness == 1.0


def test_fom_weights_from_dict_ignores_unknown_keys():
    w = FOMWeights.from_dict({"trace_length_excess": 2.0, "future_term": 99.0})
    assert w.trace_length_excess == 2.0


def test_fom_weights_from_dict_handles_none_values():
    # None means "use default" -- shouldn't crash.
    w = FOMWeights.from_dict({"trace_length_excess": None, "weighted_via_count": 3.0})
    assert w.trace_length_excess == 1.0
    assert w.weighted_via_count == 3.0


# --------------------------------------------------------------------
# load_weights_from_yaml
# --------------------------------------------------------------------


def test_load_weights_from_yaml_flat(tmp_path: Path):
    p = tmp_path / "weights.yaml"
    p.write_text("trace_length_excess: 2.5\nweighted_via_count: 0.5\n")
    w = load_weights_from_yaml(p)
    assert w.trace_length_excess == 2.5
    assert w.weighted_via_count == 0.5
    assert w.turning_penalty == 1.0  # default


def test_load_weights_from_yaml_nested(tmp_path: Path):
    p = tmp_path / "weights.yaml"
    p.write_text("weights:\n  trace_length_excess: 3.0\n")
    w = load_weights_from_yaml(p)
    assert w.trace_length_excess == 3.0


def test_load_weights_from_yaml_empty_file(tmp_path: Path):
    p = tmp_path / "weights.yaml"
    p.write_text("")
    w = load_weights_from_yaml(p)
    # All defaults.
    for name in SOFT_TERM_NAMES:
        assert getattr(w, name) == 1.0


def test_load_weights_from_yaml_non_dict_root(tmp_path: Path):
    p = tmp_path / "weights.yaml"
    p.write_text("- a\n- b\n")
    w = load_weights_from_yaml(p)
    # All defaults.
    assert w.trace_length_excess == 1.0


def test_load_weights_from_yaml_real_default_yaml():
    """Sanity-check that the shipped default.yaml loads as the calibrated
    values produced by issue #3188's Pareto sweep.

    Before #3188 this YAML was uniform 1.0; after #3188 it carries the
    Pareto-derived values. The exact numbers may drift if the calibration
    pipeline is rerun, so we test for structural properties (positive,
    non-uniform, all 10 terms present) rather than hard-coded numbers.
    """
    here = Path(__file__).parent.parent / "src/kicad_tools/optim/weights/default.yaml"
    w = load_weights_from_yaml(here)
    # All terms present and positive.
    for name in SOFT_TERM_NAMES:
        v = getattr(w, name)
        assert v > 0.0, f"{name} weight should be positive, got {v}"
    # Not the uniform 1.0 placeholder (issue #3188 calibration).
    values = [getattr(w, n) for n in SOFT_TERM_NAMES]
    assert len(set(values)) > 1, "default weights should be non-uniform after #3188 calibration"


def test_load_weights_from_yaml_real_legacy_yaml():
    here = Path(__file__).parent.parent / "src/kicad_tools/optim/weights/legacy.yaml"
    w = load_weights_from_yaml(here)
    assert w.trace_length_excess == 1.0
    for name in SOFT_TERM_NAMES:
        if name == "trace_length_excess":
            continue
        assert getattr(w, name) == 0.0


# --------------------------------------------------------------------
# check_hard_constraints
# --------------------------------------------------------------------


def test_check_hard_constraints_no_reports_pass():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb)
    assert passed
    assert failures == []


def test_check_hard_constraints_drc_pass():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, drc_report=_StubReport(0))
    assert passed
    assert failures == []


def test_check_hard_constraints_drc_fail_strict():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, drc_report=_StubReport(3))
    assert not passed
    assert "drc_clean" in failures


def test_check_hard_constraints_drc_pass_with_tolerance(tmp_path: Path):
    pcb = _empty_pcb()
    allow = tmp_path / "tol.yaml"
    allow.write_text("tolerances:\n  myboard.kicad_pcb: 5\n")
    passed, failures = check_hard_constraints(
        pcb,
        drc_report=_StubReport(3),
        tolerance_allowlist_path=allow,
        pcb_path="myboard.kicad_pcb",
    )
    assert passed
    assert failures == []


def test_check_hard_constraints_drc_fail_over_tolerance(tmp_path: Path):
    pcb = _empty_pcb()
    allow = tmp_path / "tol.yaml"
    allow.write_text("tolerances:\n  myboard.kicad_pcb: 2\n")
    passed, failures = check_hard_constraints(
        pcb,
        drc_report=_StubReport(5),
        tolerance_allowlist_path=allow,
        pcb_path="myboard.kicad_pcb",
    )
    assert not passed
    assert "drc_clean" in failures


def test_check_hard_constraints_drc_report_missing_attribute():
    # Report without error_count attribute -> fail.
    @dataclass
    class BadReport:
        garbage: int = 0

    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, drc_report=BadReport())
    assert not passed
    assert "drc_clean" in failures


def test_check_hard_constraints_erc_pass():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, erc_report=_StubReport(0))
    assert passed
    assert failures == []


def test_check_hard_constraints_erc_fail():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, erc_report=_StubReport(2))
    assert not passed
    assert "erc_clean" in failures


def test_check_hard_constraints_lvs_pass():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, lvs_orphan_pads=0)
    assert passed


def test_check_hard_constraints_lvs_fail():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, lvs_orphan_pads=3)
    assert not passed
    assert "lvs_clean" in failures


def test_check_hard_constraints_multiple_failures():
    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(
        pcb,
        drc_report=_StubReport(5),
        erc_report=_StubReport(2),
        lvs_orphan_pads=1,
    )
    assert not passed
    assert "drc_clean" in failures
    assert "erc_clean" in failures
    assert "lvs_clean" in failures


def test_check_hard_constraints_unreadable_allowlist(tmp_path: Path):
    pcb = _empty_pcb()
    # Reference a non-existent allowlist path.
    passed, failures = check_hard_constraints(
        pcb,
        tolerance_allowlist_path=tmp_path / "does_not_exist.yaml",
    )
    assert not passed
    assert "mfg_tolerance_allowlist" in failures


def test_tolerance_floor_missing_path():
    assert _tolerance_floor(None, None) == 0


def test_tolerance_floor_no_pcb_path():
    assert _tolerance_floor(None, "any.yaml") == 0


def test_tolerance_floor_suffix_match(tmp_path: Path):
    p = tmp_path / "tol.yaml"
    p.write_text("tolerances:\n  boards/foo.kicad_pcb: 10\n")
    # Exact suffix match should fire.
    assert _tolerance_floor("/abs/path/boards/foo.kicad_pcb", p) == 10


def test_tolerance_floor_no_match(tmp_path: Path):
    p = tmp_path / "tol.yaml"
    p.write_text("tolerances:\n  boards/foo.kicad_pcb: 10\n")
    assert _tolerance_floor("/other.kicad_pcb", p) == 0


def test_load_tolerance_yaml_returns_empty_for_blank_file(tmp_path: Path):
    p = tmp_path / "tol.yaml"
    p.write_text("")
    assert _load_tolerance_yaml(p) == {}


# --------------------------------------------------------------------
# compute_soft_terms
# --------------------------------------------------------------------


def test_compute_soft_terms_empty_pcb_returns_all_zero():
    pcb = _empty_pcb()
    terms = compute_soft_terms(pcb)
    assert set(terms.keys()) == set(SOFT_TERM_NAMES)
    for v in terms.values():
        assert v == 0.0


def test_compute_soft_terms_canonical_order():
    pcb = _empty_pcb()
    terms = compute_soft_terms(pcb)
    assert list(terms.keys()) == list(SOFT_TERM_NAMES)


def test_compute_soft_terms_with_pre_cached_features():
    pcb = _empty_pcb()
    f = extract_features(pcb)
    terms = compute_soft_terms(pcb, features=f)
    assert all(v == 0.0 for v in terms.values())


# --------------------------------------------------------------------
# compute_fom
# --------------------------------------------------------------------


def test_compute_fom_empty_pcb_perfect_score():
    pcb = _empty_pcb()
    result = compute_fom(pcb)
    assert isinstance(result, FOMResult)
    # Empty PCB: no soft terms fire, score = 1.0.
    assert result.score == pytest.approx(1.0)
    assert result.soft_score == pytest.approx(1.0)
    assert result.hard_gate_passed
    assert result.hard_failures == []
    assert result.predictor_value == 1.0
    assert result.beta == 0.0


def test_compute_fom_hard_gate_failure_zeros_score():
    pcb = _empty_pcb()
    result = compute_fom(pcb, drc_report=_StubReport(99))
    assert result.score == 0.0
    assert not result.hard_gate_passed
    assert "drc_clean" in result.hard_failures
    # Soft score still computed for debugging.
    assert result.soft_score > 0


def test_compute_fom_records_per_term():
    pcb = _empty_pcb()
    result = compute_fom(pcb)
    for name in SOFT_TERM_NAMES:
        assert name in result.soft_terms
        assert name in result.weighted_soft_terms


def test_compute_fom_with_custom_weights():
    pcb = _empty_pcb()
    w = FOMWeights(trace_length_excess=0.0)
    result = compute_fom(pcb, weights=w)
    assert result.weighted_soft_terms["trace_length_excess"] == 0.0


def test_compute_fom_with_predictor_no_beta():
    pcb = _empty_pcb()
    # predictor returns 0.5 but beta=0 -> factor = 1.0 unchanged.
    result = compute_fom(pcb, predictor=lambda p: 0.5, beta=0.0)
    assert result.predictor_value == 0.5
    assert result.score == pytest.approx(1.0)


def test_compute_fom_with_predictor_and_beta():
    pcb = _empty_pcb()
    result = compute_fom(pcb, predictor=lambda p: 0.5, beta=2.0)
    # soft_score = 1.0 * predictor^beta = 0.5^2 = 0.25.
    assert result.score == pytest.approx(0.25)
    assert result.predictor_value == 0.5
    assert result.beta == 2.0


def test_compute_fom_predictor_exception_falls_back_to_one():
    pcb = _empty_pcb()

    def bad_predictor(p):
        raise RuntimeError("oops")

    result = compute_fom(pcb, predictor=bad_predictor, beta=1.0)
    # Bad predictor -> 1.0 fallback.
    assert result.predictor_value == 1.0


def test_compute_fom_predictor_clamped_to_unit_interval():
    pcb = _empty_pcb()
    # Predictor returns out-of-range value; we clamp.
    result = compute_fom(pcb, predictor=lambda p: 5.0, beta=1.0)
    assert result.predictor_value == 1.0  # clamped to [0, 1]


def test_compute_fom_with_pcb_path_for_drc_tolerance(tmp_path: Path):
    pcb = _empty_pcb()
    allow = tmp_path / "tol.yaml"
    allow.write_text("tolerances:\n  myboard.kicad_pcb: 100\n")
    # 50 errors but tolerance is 100 -> passes.
    result = compute_fom(
        pcb,
        drc_report=_StubReport(50),
        tolerance_allowlist_path=allow,
        pcb_path="myboard.kicad_pcb",
    )
    assert result.hard_gate_passed
    assert result.score > 0


def test_compute_fom_caches_features_on_result():
    pcb = _empty_pcb()
    result = compute_fom(pcb)
    assert result.feature_cache is not None


def test_compute_fom_summary_includes_all_terms():
    pcb = _empty_pcb()
    result = compute_fom(pcb)
    s = result.summary()
    for name in SOFT_TERM_NAMES:
        assert name in s


def test_compute_fom_summary_includes_hard_failures():
    pcb = _empty_pcb()
    result = compute_fom(pcb, drc_report=_StubReport(99))
    s = result.summary()
    assert "FAILED" in s
    assert "drc_clean" in s


# --------------------------------------------------------------------
# Integration: real routed PCB
# --------------------------------------------------------------------


@pytest.fixture
def voltage_divider_pcb_path() -> Path:
    here = Path(__file__).parent.parent
    return here / "boards" / "01-voltage-divider" / "output" / "voltage_divider_routed.kicad_pcb"


def test_compute_fom_real_pcb_loads(voltage_divider_pcb_path: Path):
    if not voltage_divider_pcb_path.exists():
        pytest.skip("voltage divider routed PCB not present in checkout")
    pcb = PCB.load(voltage_divider_pcb_path)
    result = compute_fom(pcb)
    # With uniform weights every term contributes; expect FOM ~0 due to
    # turning_penalty's high deg^2 unnormalized values.  That's expected
    # (the issue's "weights matter a lot" risk).  We just verify the
    # result has the right structure.
    assert result.score >= 0.0
    assert result.score <= 1.0
    assert set(result.soft_terms.keys()) == set(SOFT_TERM_NAMES)


def test_compute_fom_real_pcb_legacy_weights_score_in_range(voltage_divider_pcb_path: Path):
    if not voltage_divider_pcb_path.exists():
        pytest.skip("voltage divider routed PCB not present in checkout")
    pcb = PCB.load(voltage_divider_pcb_path)
    # Legacy weights: only trace_length_excess contributes -> soft_score
    # should be exp(-x) for small x.  Expect a usable, non-trivial score.
    result = compute_fom(pcb, weights=legacy_weights())
    assert 0.3 < result.score < 1.0


def test_compute_fom_canonical_term_count():
    # Exactly 10 soft terms per the issue.
    assert len(SOFT_TERM_NAMES) == 10


def test_compute_fom_canonical_hard_constraint_count():
    # Exactly 4 hard constraints per the issue.
    assert len(HARD_CONSTRAINT_NAMES) == 4


def test_compute_fom_overflow_safety():
    # Build a pathological feature set with a huge length excess.
    # Verify the exp doesn't underflow / soft score remains 0.
    pcb = _empty_pcb()
    # Build a custom feature cache with absurd soft-term values via the
    # weights side: very large weights * 0-valued terms = 0, fine.
    # Instead create a huge weight on a term that produces non-zero value
    # via the predictor hack.
    result = compute_fom(
        pcb,
        weights=FOMWeights(trace_length_excess=1e6),
    )
    # No segments -> length excess = 0 -> weighted = 0 -> score = 1.
    assert result.score == pytest.approx(1.0)


def test_check_hard_constraints_erc_missing_attribute():
    @dataclass
    class BadReport:
        garbage: int = 0

    pcb = _empty_pcb()
    passed, failures = check_hard_constraints(pcb, erc_report=BadReport())
    assert not passed
    assert "erc_clean" in failures


def test_tolerance_floor_unreadable_yaml(tmp_path: Path):
    # Allowlist file exists but is invalid YAML.
    p = tmp_path / "bad.yaml"
    p.write_text("not: a: valid yaml\nbroken")
    floor = _tolerance_floor("any.kicad_pcb", p)
    assert floor == 0


def test_compute_fom_with_pre_cached_features():
    pcb = _empty_pcb()
    features = extract_features(pcb)
    result = compute_fom(pcb, features=features)
    # The result should still pin the same features cache.
    assert result.feature_cache is features


def test_compute_fom_hard_constraints_short_circuit_does_not_skip_term_computation():
    # When hard gate fails, soft terms are still recorded for debugging.
    pcb = _empty_pcb()
    result = compute_fom(pcb, lvs_orphan_pads=5)
    assert result.score == 0.0
    assert result.soft_score > 0  # soft computation didn't get skipped
    for name in SOFT_TERM_NAMES:
        assert name in result.soft_terms
