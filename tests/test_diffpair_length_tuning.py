"""Differential-pair length-match (serpentine) tuner tests.

Issue #2648 / Epic #2556 Phase 3I.

This module verifies the four curator-specified mitigations of
:func:`kicad_tools.router.diffpair_length_tuning.tune_diff_pair_skew`:

1. **Per-insertion DRC self-check with byte-for-byte rollback** -- a
   placed neighbor trace at clearance limit forces the candidate
   serpentine to drop below the intra threshold; the tuner rolls back
   and returns the original Route reference (by ``is`` identity) AND
   its original ``.segments`` list (also by ``is`` identity).

2. **Cascade-safety budget N=3** -- an unreachable target skew is
   capped at three trombone insertions and reports
   ``"exceeded_max_inserts"``.

3. **Outer-normal-only bulges** -- bulging never invades the partner
   trace's half-plane.

4. **Drift-prevention** -- the longer (untouched) half is returned
   identically to its input (``is``-identity on both the Route object
   and its segments list).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import (
    MAX_INSERTS_PER_PAIR,
    DiffPairTuneResult,
    tune_diff_pair_skew,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.serpentine import SerpentineConfig, SerpentineGenerator
from kicad_tools.router.primitives import Route, Segment

# Issue #3436: CI runs the suite with `-n auto --timeout=60`.  These
# tests route real boards (often via subprocess) and comfortably beat
# 60s alone, but under full-suite xdist CPU contention the wall-clock
# reaper killed them spuriously.  The marker overrides the CLI default
# with a contention-tolerant budget; it does NOT slow the happy path.
pytestmark = pytest.mark.timeout(300)


# =============================================================================
# Test helpers
# =============================================================================


def _make_signal(name: str, net_id: int, polarity: str) -> DifferentialSignal:
    return DifferentialSignal(
        net_name=name,
        net_id=net_id,
        base_name=name.rstrip("+-_PN"),
        polarity=polarity,
        notation="plus_minus",
    )


def _make_pair(p_id: int = 1, n_id: int = 2) -> DetectedPair:
    return DetectedPair(
        pair=DifferentialPair(
            name="USB_D",
            positive=_make_signal("USB_D+", p_id, "P"),
            negative=_make_signal("USB_D-", n_id, "N"),
            pair_type=DifferentialPairType.USB2,
        ),
        source=DetectionSource.EXPLICIT,
    )


def _straight_route(net_id: int, name: str, length_mm: float, y: float = 0.0) -> Route:
    """Single horizontal segment along +x at y=``y``."""
    return Route(
        net=net_id,
        net_name=name,
        segments=[
            Segment(
                x1=0.0,
                y1=y,
                x2=length_mm,
                y2=y,
                width=0.2,
                layer=Layer.F_CU,
                net=net_id,
                net_name=name,
            )
        ],
    )


# =============================================================================
# 1. Already-within-tolerance path (no-op)
# =============================================================================


class TestAlreadyMatched:
    def test_skew_below_tolerance_returns_unchanged(self):
        pair = _make_pair()
        p = _straight_route(1, "USB_D+", 10.0, y=0.0)
        n = _straight_route(2, "USB_D-", 10.3, y=0.5)
        routes = {1: p, 2: n}

        p_out, n_out, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
        )

        assert result.success is True
        assert result.reason == "already_within_tolerance"
        # Both halves are returned by reference (no mutation).
        assert p_out is p
        assert n_out is n
        assert p_out.segments is p.segments
        assert n_out.segments is n.segments
        assert result.skew_before_mm == pytest.approx(0.3, abs=1e-9)
        assert result.skew_after_mm == pytest.approx(0.3, abs=1e-9)


# =============================================================================
# 2. Unrouted-half rejection
# =============================================================================


class TestUnroutedHalf:
    def test_missing_p_route_returns_unrouted_reason(self):
        pair = _make_pair()
        n = _straight_route(2, "USB_D-", 10.0, y=0.5)
        routes = {2: n}  # P missing

        _p_out, n_out, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
        )

        assert result.success is False
        assert result.reason == "unrouted"
        # The routed half is still returned by reference.
        assert n_out is n


# =============================================================================
# 3. Outer-normal-only bulges (mitigation #3)
# =============================================================================


class TestOuterNormalOnly:
    """Tuned half's bulges land on the outer side of the pair only."""

    def test_partner_below_means_bulge_goes_up(self):
        # P at y=10 (NORTH), N at y=8 (SOUTH).  Tune P (shorter).
        # The outer side for P is +y (away from N at y=8).
        # After tuning, max(seg.y2) must be > 10 (north excursion)
        # AND no segment may have y2 < 10 (no south excursion toward N).
        pair = _make_pair(p_id=1, n_id=2)
        # Make P shorter: 5mm; N longer: 8mm so skew = 3mm.
        # P at y=10; N at y=8.  Long enough to host trombone.
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 10.0, 8.0, 10.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 8.0, 11.0, 8.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        routes = {1: p, 2: n}

        p_out, n_out, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.5, min_spacing=0.2, gap_factor=2.0),
        )

        assert result.inserts_applied >= 1
        # n must be untouched (drift-prevention preview -- full check below).
        assert n_out is n

        # Every segment of the tuned P must NOT drop below y=10
        # (the partner sits at y=8, which is the "inner" side).
        ys = []
        for seg in p_out.segments:
            ys.append(seg.y1)
            ys.append(seg.y2)
        assert max(ys) > 10.0, "Trombone must bulge above the original y=10"
        # The y2/y1 of every segment must be >= 10 - tolerance for fp noise.
        for y in ys:
            assert y >= 10.0 - 1e-6, (
                f"Trombone segment leaked to y={y} (below original y=10); "
                "outer-normal-only invariant violated."
            )

    def test_partner_above_means_bulge_goes_down(self):
        # Symmetric: P at y=10 (SHORTER, SOUTH), N at y=12 (LONGER, NORTH).
        # Outer side for P is -y (away from N at y=12).
        pair = _make_pair(p_id=1, n_id=2)
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 10.0, 8.0, 10.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 12.0, 11.0, 12.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        routes = {1: p, 2: n}

        p_out, n_out, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.5, min_spacing=0.2, gap_factor=2.0),
        )

        assert result.inserts_applied >= 1
        ys = []
        for seg in p_out.segments:
            ys.append(seg.y1)
            ys.append(seg.y2)
        assert min(ys) < 10.0, "Trombone must bulge below the original y=10"
        for y in ys:
            assert y <= 10.0 + 1e-6, (
                f"Trombone segment leaked to y={y} (above original y=10); "
                "outer-normal-only invariant violated."
            )


# =============================================================================
# 4. Drift-prevention -- longer half is `is`-identical (mitigation #4)
# =============================================================================


class TestDriftPrevention:
    """The longer (untouched) half is returned BY REFERENCE."""

    def test_longer_route_is_identity_preserved(self):
        # N is longer (12mm), P is shorter (8mm).  Skew = 4mm.
        pair = _make_pair(p_id=1, n_id=2)
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 0.0, 8.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 2.0, 12.0, 2.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        routes = {1: p, 2: n}
        original_n_segments = n.segments  # capture list reference

        p_out, n_out, _result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.5),
        )

        # SAME Route object.
        assert n_out is n, "Longer half's Route object identity must be preserved"
        # SAME .segments list object.
        assert n_out.segments is original_n_segments, (
            "Longer half's segments list identity must be preserved"
        )

    def test_longer_route_is_identity_when_longer_is_p(self):
        # Symmetric: P is longer this time (10mm); N is shorter (6mm).
        pair = _make_pair(p_id=1, n_id=2)
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 0.0, 10.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 2.0, 6.0, 2.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        routes = {1: p, 2: n}
        original_p_segments = p.segments

        p_out, _n_out, _result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=0.5),
        )

        assert p_out is p
        assert p_out.segments is original_p_segments


# =============================================================================
# 5. Cascade-safety budget N=3 (mitigation #2)
# =============================================================================


class TestCascadeBudget:
    """An unreachable skew is capped at MAX_INSERTS_PER_PAIR attempts."""

    def test_max_inserts_per_pair_is_three(self):
        assert MAX_INSERTS_PER_PAIR == 3

    def test_unreachable_skew_caps_at_three_attempts(self):
        # Skew = 20mm, amplitude per loop = 0.1 -> each insertion adds
        # at most ~0.4mm (4 loops * 2 * amplitude).  20mm is unreachable
        # in 3 insertions; the budget should fire.
        pair = _make_pair(p_id=1, n_id=2)
        # Long P (50mm) so the segment is large enough for trombones,
        # but skew vs the partner (70mm) is the unreachable 20mm.
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 0.0, 50.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 5.0, 70.0, 5.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        routes = {1: p, 2: n}

        # Tight amplitude / few loops per insertion so 3 insertions cannot
        # reach 20mm.  amplitude=0.1, gap_factor=2 -> per-loop add ~= 0.2mm;
        # max_iterations=4 -> per-insertion add ~= 0.8mm -> 3 inserts ~= 2.4mm,
        # still way short of 20mm.
        cfg = SerpentineConfig(
            amplitude=0.1,
            min_spacing=0.2,
            gap_factor=2.0,
            max_iterations=4,
        )

        # Spy on add_serpentine to confirm exactly 3 calls.
        orig_add = SerpentineGenerator.add_serpentine
        call_count = {"n": 0}

        def spy(self, route, target_length, grid=None):
            call_count["n"] += 1
            return orig_add(self, route, target_length, grid)

        with patch.object(SerpentineGenerator, "add_serpentine", spy):
            _p_out, _n_out, result = tune_diff_pair_skew(
                pair,
                routes,
                tolerance_mm=0.1,
                intra_pair_clearance_mm=0.1,
                config=cfg,
            )

        assert call_count["n"] == MAX_INSERTS_PER_PAIR, (
            f"Expected exactly {MAX_INSERTS_PER_PAIR} add_serpentine calls, got {call_count['n']}"
        )
        assert result.attempts == MAX_INSERTS_PER_PAIR
        assert result.reason == "exceeded_max_inserts"
        assert result.success is False


# =============================================================================
# 6. Per-insertion DRC self-check + byte-for-byte rollback (mitigation #1)
# =============================================================================


class TestPostInsertionRollback:
    """A neighbor at the clearance limit forces a candidate to be rolled back."""

    def test_neighbor_at_clearance_limit_triggers_rollback(self):
        # P is the shorter (10mm); N is the longer (15mm), so skew=5mm.
        # We need a NEIGHBOR (not P, not N) placed in the outer half-plane
        # right where the trombone would land.  P at y=0; N at y=5 (south
        # invalid -- partner). Outer for P is -y. Place neighbor at y=-0.5
        # (close to where the amplitude-0.5 trombone would bulge) so the
        # neighbor sits at clearance limit and the trombone collides.
        pair = _make_pair(p_id=1, n_id=2)
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 0.0, 10.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 5.0, 15.0, 5.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        # Neighbor net 3, a wide trace right where the trombone would bulge.
        # P's outer normal is -y (away from N).  amplitude=0.5 means the
        # trombone goes to y=-0.5.  Put the neighbor at y=-0.5 so the
        # edge-to-edge clearance to the trombone equals zero (overlap).
        neighbor = Route(
            net=3,
            net_name="VCC",
            segments=[Segment(0.0, -0.5, 10.0, -0.5, 0.2, Layer.F_CU, net=3, net_name="VCC")],
        )
        routes = {1: p, 2: n, 3: neighbor}
        original_p = p
        original_p_segments = p.segments

        cfg = SerpentineConfig(amplitude=0.5, min_spacing=0.2, gap_factor=2.0)
        p_out, n_out, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.1,
            intra_pair_clearance_mm=0.2,  # neighbor at 0mm -> below threshold
            config=cfg,
        )

        assert result.success is False
        assert result.reason == "post_insertion_drc_violation"
        # Byte-for-byte rollback: P is returned by reference.
        assert p_out is original_p, "Original Route reference must be preserved on rollback"
        assert p_out.segments is original_p_segments, (
            "Original segments list reference must be preserved on rollback"
        )
        # N also untouched.
        assert n_out is n


# =============================================================================
# 7. Successful tuning path (smoke -- pair actually reaches tolerance)
# =============================================================================


class TestSuccessfulTuning:
    def test_small_skew_can_be_tuned_in_one_insert(self):
        # Skew = 4mm; amplitude=1.0 -> 2 loops add 4mm -> exact match in 1 insert.
        # The shorter side has to be long enough that 2 loops fit (gap_factor*2*amplitude*2 ~ 1.6mm forward).
        pair = _make_pair(p_id=1, n_id=2)
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 0.0, 12.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 5.0, 16.0, 5.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        routes = {1: p, 2: n}

        _p_out, _n_out, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
            config=SerpentineConfig(amplitude=1.0, min_spacing=0.2, gap_factor=2.0),
        )

        assert result.success is True, (
            f"expected success but got reason={result.reason!r} "
            f"skew_before={result.skew_before_mm} skew_after={result.skew_after_mm}"
        )
        assert result.reason == "tuned"
        assert result.inserts_applied >= 1
        assert result.skew_after_mm <= 0.5 + 1e-6
        assert result.skew_before_mm == pytest.approx(4.0, abs=1e-9)


# =============================================================================
# 8. Engagement gate -- length_critical=False is skipped
# =============================================================================


class TestEngagementGate:
    """Pairs whose net class is not length-critical are not tuned."""

    def test_length_critical_false_skips_tuning(self):
        # Skew = 10mm (unambiguous violation if tuned); length_critical=False
        # -> tuner must NOT touch either half.
        pair = _make_pair(p_id=1, n_id=2)
        p = Route(
            net=1,
            net_name="USB_D+",
            segments=[Segment(0.0, 0.0, 10.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
        )
        n = Route(
            net=2,
            net_name="USB_D-",
            segments=[Segment(0.0, 5.0, 20.0, 5.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
        )
        routes = {1: p, 2: n}
        original_p_segments = p.segments
        original_n_segments = n.segments

        p_out, n_out, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
            length_critical=False,
        )

        # No change.
        assert result.reason == "not_length_critical"
        assert result.success is True  # gate is a successful no-op, not a failure
        assert p_out is p
        assert n_out is n
        assert p_out.segments is original_p_segments
        assert n_out.segments is original_n_segments


# =============================================================================
# 9. Triple-gate -- Autorouter.apply_diffpair_length_tuning invokes the tuner
# =============================================================================


class TestTripleGate:
    """The full call chain: Autorouter -> tune_diff_pair_skew -> serpentine insert.

    The triple-gate test is the #2587/#2639 lesson: it asserts EVERY link in
    the chain actually fires.  A spy on :func:`tune_diff_pair_skew` confirms
    the pipeline invokes the tuner for each detected pair.
    """

    @pytest.mark.xfail(
        reason="net-class length_critical state polluted by sibling xdist-worker tests -- see issue #3524",
        strict=False,
    )
    def test_autorouter_invokes_tuner_for_each_pair(self):
        from kicad_tools.router import diffpair_length_tuning as dlt_module
        from kicad_tools.router.core import Autorouter

        ar = Autorouter(width=40.0, height=20.0)
        # Tell the autorouter that nets 1+2 are a USB_D pair via the
        # net-name registry.  detect_diff_pairs uses suffix inference so
        # the names alone are enough.
        ar.net_names = {1: "USB_D+", 2: "USB_D-"}
        # Place two unequal routes -- the shorter one needs a trombone.
        ar.routes = [
            Route(
                net=1,
                net_name="USB_D+",
                segments=[Segment(0.0, 0.0, 12.0, 0.0, 0.2, Layer.F_CU, net=1, net_name="USB_D+")],
            ),
            Route(
                net=2,
                net_name="USB_D-",
                segments=[Segment(0.0, 5.0, 16.0, 5.0, 0.2, Layer.F_CU, net=2, net_name="USB_D-")],
            ),
        ]

        # Detect the pair the same way the CLI does.
        from kicad_tools.router.diffpair_detection import detect_diff_pairs

        detected_pairs = detect_diff_pairs(net_names=ar.net_names)
        assert len(detected_pairs) == 1, "Expected exactly one detected USB_D pair"

        call_count = {"n": 0}
        # Wrap the real function to count calls without disabling its logic.
        real_tune = dlt_module.tune_diff_pair_skew

        def spy(*args, **kwargs):
            call_count["n"] += 1
            return real_tune(*args, **kwargs)

        with patch.object(dlt_module, "tune_diff_pair_skew", spy):
            # Patch the imported name inside core.py's apply_diffpair_length_tuning;
            # the import is local-to-method, so we patch through the package.
            results = ar.apply_diffpair_length_tuning(
                detected_pairs=detected_pairs,
                verbose=False,
            )

        assert call_count["n"] == 1, (
            f"Expected exactly 1 tune_diff_pair_skew call (one detected pair), "
            f"got {call_count['n']}"
        )
        assert ("USB_D+", "USB_D-") in results
        # A 4mm skew at default amplitude=1.0 / 2 loops should tune cleanly.
        assert results[("USB_D+", "USB_D-")].reason in ("tuned", "already_within_tolerance")


# =============================================================================
# 10. Module-level invariants
# =============================================================================


class TestModuleInvariants:
    def test_diff_pair_tune_result_dataclass_defaults(self):
        r = DiffPairTuneResult()
        assert r.success is False
        assert r.reason == ""
        assert r.attempts == 0
        assert r.inserts_applied == 0
        assert r.serpentine_results == []
