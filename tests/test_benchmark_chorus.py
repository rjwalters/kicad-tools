"""Unit tests for the chorus-test-revA routing benchmark wiring (Issue #2611).

These tests cover the metrics-plumbing and regression-detection layers
without invoking the actual router on the (heavyweight, optional)
chorus-test PCB.  The integration-level "run the full route" test lives
in the nightly CI workflow because it takes 30+ minutes.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.benchmark import (
    ABSOLUTE_THRESHOLDS,
    REGRESSION_THRESHOLDS,
    BenchmarkResult,
    check_regression,
    load_baseline,
)
from kicad_tools.benchmark.cases import get_case_by_name
from kicad_tools.benchmark.runner import _classify_nets_by_connectivity

# ---------------------------------------------------------------------------
# Case registration / path resolution
# ---------------------------------------------------------------------------


def test_chorus_test_revA_is_registered() -> None:
    """The case must appear in BENCHMARK_CASES with HARD difficulty."""
    case = get_case_by_name("chorus_test_revA")
    assert case is not None, "chorus_test_revA must be in the registry"
    assert case.difficulty.value == "hard"


def test_chorus_test_revA_points_at_v21_stripped() -> None:
    """Issue #3474 Phase 0: the stale v18 path was bumped to v21_stripped.

    v18/v19 predate the chorus repo's 2026-05-11 netlist repair and the
    2026-06-10 U1/U2 regulator-connectivity restoration; routing them to
    100% would be fake-manufacturable.  (Issue #2611 had previously
    bumped the even-staler v9_grid50 to v18.)
    """
    case = get_case_by_name("chorus_test_revA")
    assert case is not None
    assert case.pcb_path is not None
    assert "v21_stripped" in case.pcb_path, (
        f"Expected v21_stripped PCB, got: {case.pcb_path}.  See Issue "
        "#3474 Phase 0 — v18/v19 lack the 2026-05-11 schematic repairs "
        "and the U1/U2 regulator connectivity restoration."
    )


def test_chorus_test_revA_skips_power_nets() -> None:
    """Power/ground nets are intentionally excluded from routing."""
    case = get_case_by_name("chorus_test_revA")
    assert case is not None
    # The five power/ground nets that should be poured, not routed.
    expected = {"+3.3V", "+3.3VA", "+5V", "GNDA", "GNDD"}
    assert set(case.skip_nets) == expected


# ---------------------------------------------------------------------------
# Net-connectivity classification helper
# ---------------------------------------------------------------------------


def test_classify_fully_routed_only() -> None:
    """All multi-pad nets fully connected -> all counted as fully_routed."""
    connectivity = {
        1: {"total_pads": 2, "connected_pads": 2, "connected": True},
        2: {"total_pads": 3, "connected_pads": 3, "connected": True},
    }
    fully, partial, unrouted, ids = _classify_nets_by_connectivity(connectivity)
    assert fully == 2
    assert partial == 0
    assert unrouted == 0
    assert ids == []


def test_classify_mixed() -> None:
    """A representative mix: 1 fully, 1 partial, 1 unrouted."""
    connectivity = {
        1: {"total_pads": 2, "connected_pads": 2, "connected": True},
        2: {"total_pads": 4, "connected_pads": 3, "connected": False},
        3: {"total_pads": 2, "connected_pads": 0, "connected": False},
    }
    fully, partial, unrouted, ids = _classify_nets_by_connectivity(connectivity)
    assert fully == 1
    assert partial == 1
    assert unrouted == 1
    assert ids == [3]


def test_classify_skips_single_pad_nets() -> None:
    """Single-pad nets are not part of the multi-pad signal-net population."""
    connectivity = {
        1: {"total_pads": 1, "connected_pads": 1, "connected": True},
        2: {"total_pads": 2, "connected_pads": 0, "connected": False},
    }
    fully, partial, unrouted, ids = _classify_nets_by_connectivity(connectivity)
    assert fully == 0
    assert partial == 0
    assert unrouted == 1
    assert ids == [2]


def test_classify_respects_nets_to_route_ids() -> None:
    """Filtering restricts to the targeted multi-pad signal nets."""
    connectivity = {
        1: {"total_pads": 2, "connected_pads": 2, "connected": True},
        2: {"total_pads": 2, "connected_pads": 0, "connected": False},
        3: {"total_pads": 2, "connected_pads": 2, "connected": True},
    }
    # Only count nets 1 and 2; net 3 is e.g. a skipped power net.
    fully, partial, unrouted, ids = _classify_nets_by_connectivity(
        connectivity, nets_to_route_ids={1, 2}
    )
    assert fully == 1
    assert partial == 0
    assert unrouted == 1
    assert ids == [2]


def test_classify_none_connectivity_returns_zeros() -> None:
    """No pad data -> zero counts (caller falls back to legacy nets_routed)."""
    fully, partial, unrouted, ids = _classify_nets_by_connectivity(None)
    assert (fully, partial, unrouted, ids) == (0, 0, 0, [])


# ---------------------------------------------------------------------------
# Regression detection — structural floor (absolute threshold)
# ---------------------------------------------------------------------------


def _make_result(
    nets_fully_routed: int = 26,
    nets_unrouted: int = 8,
    routing_time_sec: float = 1900.0,
    drc_violations: int = 3,
) -> BenchmarkResult:
    """Build a minimal BenchmarkResult for regression-test scenarios."""
    return BenchmarkResult(
        case_name="chorus_test_revA",
        strategy="negotiated",
        nets_total=46,
        nets_routed=nets_fully_routed,
        completion_rate=nets_fully_routed / 46,
        nets_fully_routed=nets_fully_routed,
        nets_partial=46 - nets_fully_routed - nets_unrouted,
        nets_unrouted=nets_unrouted,
        unrouteable_nets=[f"NET_{i}" for i in range(nets_unrouted)],
        routing_time_sec=routing_time_sec,
        drc_violations=drc_violations,
    )


def test_unrouted_increase_triggers_error() -> None:
    """Issue #2611: any increase in nets_unrouted above baseline = error."""
    baseline = [_make_result(nets_unrouted=8)]
    current = [_make_result(nets_unrouted=9)]
    regressions = check_regression(current, baseline)
    floor = [r for r in regressions if r.metric == "nets_unrouted"]
    assert len(floor) == 1
    assert floor[0].severity == "error"
    assert floor[0].baseline_value == 8.0
    assert floor[0].current_value == 9.0


def test_unrouted_decrease_no_regression() -> None:
    """A decrease in structural floor is a celebration, not a regression."""
    baseline = [_make_result(nets_unrouted=8)]
    current = [_make_result(nets_unrouted=7)]
    regressions = check_regression(current, baseline)
    floor = [r for r in regressions if r.metric == "nets_unrouted"]
    assert len(floor) == 0


def test_unrouted_unchanged_no_regression() -> None:
    """Same floor value = no flag."""
    baseline = [_make_result(nets_unrouted=8)]
    current = [_make_result(nets_unrouted=8)]
    regressions = check_regression(current, baseline)
    floor = [r for r in regressions if r.metric == "nets_unrouted"]
    assert len(floor) == 0


# ---------------------------------------------------------------------------
# Regression detection — fully-routed (relative threshold)
# ---------------------------------------------------------------------------


def test_fully_routed_drop_more_than_10pct_errors() -> None:
    """Issue #2611 acceptance criterion: 10% drop in fully-routed = error."""
    baseline = [_make_result(nets_fully_routed=26)]
    # 23/26 = 88.5% -> 11.5% drop, above the 10% error threshold.
    current = [_make_result(nets_fully_routed=23)]
    regressions = check_regression(current, baseline)
    full = [r for r in regressions if r.metric == "nets_fully_routed"]
    assert len(full) == 1
    assert full[0].severity == "error"


def test_fully_routed_drop_at_5pct_warns() -> None:
    """5% drop in fully-routed sits between warning and error thresholds."""
    baseline = [_make_result(nets_fully_routed=26)]
    # 24/26 = 92.3% -> 7.7% drop, above warn (5%) below error (10%).
    current = [_make_result(nets_fully_routed=24)]
    regressions = check_regression(current, baseline)
    full = [r for r in regressions if r.metric == "nets_fully_routed"]
    assert len(full) == 1
    assert full[0].severity == "warning"


def test_fully_routed_increase_no_regression() -> None:
    """More fully-routed nets = good, no flag."""
    baseline = [_make_result(nets_fully_routed=26)]
    current = [_make_result(nets_fully_routed=30)]
    regressions = check_regression(current, baseline)
    full = [r for r in regressions if r.metric == "nets_fully_routed"]
    assert len(full) == 0


# ---------------------------------------------------------------------------
# Threshold registration
# ---------------------------------------------------------------------------


def test_threshold_registry_includes_new_metrics() -> None:
    """The new metrics are wired into the regression checker."""
    assert "nets_fully_routed" in REGRESSION_THRESHOLDS
    assert "drc_violations" in REGRESSION_THRESHOLDS
    assert "nets_unrouted" in ABSOLUTE_THRESHOLDS


# ---------------------------------------------------------------------------
# Baseline file integrity
# ---------------------------------------------------------------------------


def test_baseline_file_loads() -> None:
    """The committed baseline must round-trip through load_baseline."""
    repo_root = Path(__file__).parent.parent
    baseline_path = repo_root / "tests" / "baselines" / "chorus_test_revA.json"
    assert baseline_path.exists(), (
        f"Baseline file missing: {baseline_path}.  Issue #2611 requires a "
        "stored baseline so the nightly benchmark has something to compare "
        "against."
    )
    results = load_baseline(baseline_path)
    assert len(results) >= 1
    chorus = next(r for r in results if r.case_name == "chorus_test_revA")
    # Sanity-check the Issue #3474 Phase 0 v21 re-baseline (2026-06-10,
    # cpp seed 42, pinned recipe -- see tests/test_chorus_reach_floor_3237.py
    # CHORUS_V21_* constants for the full measurement record).
    assert chorus.nets_total == 51, (
        "nets_total must match the v21_stripped multi-pad signal-net "
        "count (51 after the five power/ground nets are skipped)."
    )
    assert chorus.nets_unrouted == 19, (
        "The 19-net unrouted floor is a load-bearing constant; changing it "
        "requires justification in the PR description per docs/benchmark.md."
    )
    assert chorus.nets_fully_routed == 2, (
        "The honest (low) v21 strict-reach baseline; expected to RISE as "
        "#3474 phases R1/R2/P1 land -- bump with justification."
    )


# ---------------------------------------------------------------------------
# Skip-when-fixture-missing behaviour
# ---------------------------------------------------------------------------


def test_missing_pcb_skips_gracefully(tmp_path: Path) -> None:
    """A fresh runner without the chorus PCB must not raise — just skip."""
    from kicad_tools.benchmark import BenchmarkRunner
    from kicad_tools.benchmark.cases import BenchmarkCase, Difficulty

    # Synthetic case pointing at a path that doesn't exist.
    case = BenchmarkCase(
        name="missing_fixture_demo",
        pcb_path="nonexistent/path/to.kicad_pcb",
        difficulty=Difficulty.HARD,
    )
    runner = BenchmarkRunner(base_dir=tmp_path, verbose=False)
    # Should return empty list, not raise.
    results = runner.run_case(case, strategies=["negotiated"])
    assert results == []
