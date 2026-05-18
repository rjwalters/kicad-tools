"""Tests for the diff-pair intra-pair clearance violation detector
(Issue #3023 Phase A).

Phase A is observability-only.  After ``CoupledPathfinder`` produces a
routed (P, N) pair, ``find_intra_pair_clearance_violations`` walks every
same-layer (p-segment, n-segment) pair and reports any whose
edge-to-edge clearance is below the per-pair
``NetClassRouting.effective_intra_pair_clearance()`` threshold.

These tests pin three properties:

1. A known-violating pair produces a non-empty violation record whose
   ``violation_magnitude_mm`` is positive and whose worst segment pair
   is correctly identified.
2. A clean pair produces ``None`` (no violation record at all).
3. The detector consults the per-pair ``NetClassRouting`` accessor --
   NOT the global ``DifferentialPairRules.spacing`` heuristic -- so an
   intra-pair clearance override is honoured.

Regression boundary: this suite must NOT change the routes
``CoupledPathfinder`` produces; it only inspects them.  PR #3022's
8-case ``test_diffpair_coupled_floor.py`` and PR #3005's 9-case
``test_diffpair_serpentine_clearance.py`` suites stay green.

Phase B (the fine-grid sub-pass that closes the empirical AC of #3023)
ships in a separate PR.
"""

from __future__ import annotations

from kicad_tools.core.types import CopperLayer as Layer
from kicad_tools.router.diffpair_routing import (
    IntraPairClearanceViolation,
    find_intra_pair_clearance_violations,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_route(
    net_id: int,
    net_name: str,
    segments: list[tuple[float, float, float, float, Layer]],
    width: float = 0.15,
) -> Route:
    """Build a Route from a list of ``(x1, y1, x2, y2, layer)`` tuples.

    Mirrors the ad-hoc constructors used in
    ``test_diffpair_coupled_floor.py`` so the two suites share a shape.
    """
    return Route(
        net=net_id,
        net_name=net_name,
        segments=[
            Segment(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                width=width,
                layer=layer,
                net=net_id,
                net_name=net_name,
            )
            for (x1, y1, x2, y2, layer) in segments
        ],
    )


# ---------------------------------------------------------------------------
# Test 1: known-violating pair → non-empty record
# ---------------------------------------------------------------------------


def test_detects_violation_on_too_tight_pair():
    """A pair routed with centerlines below ``threshold + trace_width``
    triggers a violation record carrying the worst-case magnitude."""
    # Two parallel traces 0.15 mm wide, centerlines 0.20 mm apart.
    # Edge-to-edge clearance = 0.20 - 0.15 = 0.05 mm.
    # Threshold = 0.10 mm.  Magnitude = 0.10 - 0.05 = 0.05 mm.
    p_route = _make_route(
        net_id=1,
        net_name="DQS0_P",
        segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
        width=0.15,
    )
    n_route = _make_route(
        net_id=2,
        net_name="DQS0_N",
        segments=[(0.0, 1.2, 10.0, 1.2, Layer.F_CU)],
        width=0.15,
    )

    violation = find_intra_pair_clearance_violations(
        p_route, n_route, threshold_mm=0.10, pair_name="DQS0"
    )

    assert violation is not None
    assert isinstance(violation, IntraPairClearanceViolation)
    assert violation.pair_name == "DQS0"
    assert violation.positive_net_name == "DQS0_P"
    assert violation.negative_net_name == "DQS0_N"
    assert violation.expected_clearance_mm == 0.10
    # The actual clearance is 0.05 mm (within floating-point tolerance).
    assert abs(violation.actual_clearance_mm - 0.05) < 1e-6
    # Magnitude is the deficit, which must be positive.
    assert violation.violation_magnitude_mm > 0
    assert abs(violation.violation_magnitude_mm - 0.05) < 1e-6
    # At least one offending segment-pair was recorded.
    assert len(violation.segment_violations) >= 1
    # The worst-case layer is reported as a KiCad name string.
    assert violation.layer == "F.Cu"


# ---------------------------------------------------------------------------
# Test 2: clean pair → no violation record
# ---------------------------------------------------------------------------


def test_no_violation_on_well_spaced_pair():
    """A pair whose edge-to-edge clearance comfortably exceeds the
    threshold returns ``None`` -- no violation record is emitted."""
    # Two parallel traces 0.15 mm wide, centerlines 0.40 mm apart.
    # Edge-to-edge clearance = 0.40 - 0.15 = 0.25 mm >> 0.10 mm threshold.
    p_route = _make_route(
        net_id=1,
        net_name="USB_DP",
        segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
        width=0.15,
    )
    n_route = _make_route(
        net_id=2,
        net_name="USB_DN",
        segments=[(0.0, 1.4, 10.0, 1.4, Layer.F_CU)],
        width=0.15,
    )

    violation = find_intra_pair_clearance_violations(
        p_route, n_route, threshold_mm=0.10, pair_name="USB_D"
    )

    assert violation is None


# ---------------------------------------------------------------------------
# Test 3: per-pair NetClassRouting.effective_intra_pair_clearance() is
# what the detector consults -- NOT the global pair.rules.spacing.
# ---------------------------------------------------------------------------


def test_detector_honours_per_pair_intra_pair_clearance_override():
    """The detector compares against the threshold the caller passes
    in.  The caller's responsibility -- documented on the helper's
    docstring -- is to source that threshold from
    ``NetClassRouting.effective_intra_pair_clearance()``, NOT the
    legacy ``DifferentialPairRules.spacing`` default.

    This test sets up a routed pair that:
      * PASSES against ``DifferentialPairRules.spacing`` (0.05 mm), AND
      * FAILS against the per-pair ``intra_pair_clearance`` override
        (0.20 mm).

    It then runs the detector twice: once with each threshold.  The
    detector must report a violation in the second case (the override)
    and stay silent in the first.  This pins the contract that the
    per-pair override -- not the global rule -- governs detection.
    """
    # Two parallel traces 0.15 mm wide, centerlines 0.25 mm apart.
    # Edge-to-edge clearance = 0.25 - 0.15 = 0.10 mm.
    # Below 0.20 mm threshold but above 0.05 mm fallback.
    p_route = _make_route(
        net_id=1,
        net_name="HDMI_D0_P",
        segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
        width=0.15,
    )
    n_route = _make_route(
        net_id=2,
        net_name="HDMI_D0_N",
        segments=[(0.0, 1.25, 10.0, 1.25, Layer.F_CU)],
        width=0.15,
    )

    # Build a NetClassRouting matching the per-pair override.  The
    # effective accessor (rules.py:892) returns intra_pair_clearance
    # when set, otherwise falls back to .clearance.
    net_class = NetClassRouting(
        name="DiffPair_HDMI",
        trace_width=0.15,
        clearance=0.05,
        intra_pair_clearance=0.20,
    )

    # Sanity-check the accessor returns the override (not the fallback).
    assert net_class.effective_intra_pair_clearance() == 0.20

    # First case: feed the legacy/global value (0.05 mm).  No violation.
    legacy = find_intra_pair_clearance_violations(
        p_route, n_route, threshold_mm=0.05, pair_name="HDMI_D0"
    )
    assert legacy is None, (
        "Detector flagged a clearance that passes the legacy threshold; "
        "this means the floor is being applied to a stricter target than "
        "the caller asked for."
    )

    # Second case: feed the per-pair override (0.20 mm).  Violation must
    # surface because edge-to-edge clearance (0.10 mm) is below the
    # per-pair threshold even though it passes the global fallback.
    override = find_intra_pair_clearance_violations(
        p_route,
        n_route,
        threshold_mm=net_class.effective_intra_pair_clearance(),
        pair_name="HDMI_D0",
    )
    assert override is not None, (
        "Detector missed a clearance below the per-pair "
        "intra_pair_clearance override -- it must read the per-pair "
        "value, not the global pair.rules.spacing default."
    )
    assert override.expected_clearance_mm == 0.20
    assert abs(override.actual_clearance_mm - 0.10) < 1e-6
    assert override.violation_magnitude_mm > 0


# ---------------------------------------------------------------------------
# Edge cases (small additional coverage; stays well under the LOC budget).
# ---------------------------------------------------------------------------


def test_detector_ignores_different_layer_segments():
    """A P-segment on F.Cu and an N-segment on B.Cu never trigger a
    violation regardless of their X/Y proximity -- the clearance rule
    applies per-layer only."""
    p_route = _make_route(
        net_id=1,
        net_name="X_P",
        segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
        width=0.15,
    )
    n_route = _make_route(
        net_id=2,
        net_name="X_N",
        segments=[(0.0, 1.0, 10.0, 1.0, Layer.B_CU)],  # same X/Y, OTHER layer
        width=0.15,
    )

    # If the detector ignored layers, this would report -0.15 mm
    # clearance (centerlines overlap).  Layer-awareness must filter it.
    violation = find_intra_pair_clearance_violations(
        p_route, n_route, threshold_mm=0.10, pair_name="X"
    )
    assert violation is None


def test_detector_returns_none_on_empty_routes():
    """Empty or no-segment routes never trigger a violation."""
    empty = Route(net=1, net_name="EMPTY", segments=[])
    populated = _make_route(
        net_id=2,
        net_name="POP",
        segments=[(0.0, 1.0, 10.0, 1.0, Layer.F_CU)],
        width=0.15,
    )

    # Either side empty → no violation, no exception.
    assert find_intra_pair_clearance_violations(empty, populated, threshold_mm=1.0) is None
    assert find_intra_pair_clearance_violations(populated, empty, threshold_mm=1.0) is None
    assert find_intra_pair_clearance_violations(empty, empty, threshold_mm=1.0) is None


def test_detector_records_every_offending_segment_pair():
    """When multiple same-layer segment pairs are below threshold, all
    of them appear in ``segment_violations`` -- Phase B (the corridor
    sub-pass) consumes the full list to scope the rip-and-replace
    region."""
    # Two-segment P route running parallel to a two-segment N route,
    # both on F.Cu, with both segment-pairs below threshold.
    p_route = _make_route(
        net_id=1,
        net_name="MULTI_P",
        segments=[
            (0.0, 1.0, 5.0, 1.0, Layer.F_CU),
            (5.0, 1.0, 10.0, 1.0, Layer.F_CU),
        ],
        width=0.15,
    )
    n_route = _make_route(
        net_id=2,
        net_name="MULTI_N",
        segments=[
            (0.0, 1.2, 5.0, 1.2, Layer.F_CU),
            (5.0, 1.2, 10.0, 1.2, Layer.F_CU),
        ],
        width=0.15,
    )

    violation = find_intra_pair_clearance_violations(
        p_route, n_route, threshold_mm=0.10, pair_name="MULTI"
    )

    assert violation is not None
    # With two P-segs and two N-segs and ALL clearances at ~0.05 mm,
    # every (p, n) combination on the same layer is offending: 4 total.
    assert len(violation.segment_violations) == 4
