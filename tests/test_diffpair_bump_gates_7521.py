"""AC3 geometry gates for diff-pair skew bumps (issue #7521).

Excursion at most 2x the pair gap, jogs at least 3x the trace width, and the
bump within 15 mm of a mismatch source in the same via-delimited run.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_bump_gates import (
    MAX_SOURCE_DISTANCE_MM,
    bump_gate_violation,
    jog_spaced,
    replaced_index,
)
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import tune_diff_pair_skew
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.serpentine import SerpentineConfig
from kicad_tools.router.primitives import Route, Segment, Via

W = 0.2
GAP = 0.15


def _seg(x1, y1, x2, y2, net=1, name="D+", layer=Layer.F_CU):
    return Segment(x1, y1, x2, y2, W, layer, net=net, net_name=name)


def _bump(x0: float, amplitude: float, jog: float) -> list[Segment]:
    """Lead-in, one square loop, lead-out along y=0 from ``x0``."""
    x1, x2 = x0 + 1.0, x0 + 1.0 + jog
    return [
        _seg(x0 - 1.0, 0, x1, 0),
        _seg(x1, 0, x1, amplitude),
        _seg(x1, amplitude, x2, amplitude),
        _seg(x2, amplitude, x2, 0),
        _seg(x2, 0, x2 + 1.0, 0),
    ]


def _host(length: float, vias: list[Via] | None = None) -> Route:
    return Route(net=1, net_name="D+", segments=[_seg(0, 0, length, 0)], vias=vias or [])


def _partner(vias: list[Via] | None = None) -> Route:
    return Route(
        net=2, net_name="D-", segments=[_seg(0, -0.35, 60, -0.35, 2, "D-")], vias=vias or []
    )


def _violation(bump, host=None, partner=None):
    host = host or _host(60.0)
    return bump_gate_violation(bump, host, 0, partner or _partner(), nominal_gap_mm=GAP)


class TestExcursion:
    def test_within_twice_the_gap_passes(self):
        assert _violation(_bump(2.0, 2 * GAP, 0.6)) is None

    def test_beyond_twice_the_gap_is_rejected(self):
        assert "excursion" in _violation(_bump(2.0, 2 * GAP + 0.05, 0.6))


class TestJog:
    def test_three_widths_passes(self):
        assert _violation(_bump(2.0, 0.2, 3 * W)) is None

    def test_shorter_flat_top_is_rejected(self):
        assert "jog" in _violation(_bump(2.0, 0.2, 3 * W - 0.05))

    def test_between_loop_connector_is_a_jog_too(self):
        bump = _bump(2.0, 0.2, 0.6)
        # Splice a second loop after a 0.3 mm connector on the host line.
        x = bump[3].x2
        bump[4:] = [
            _seg(x, 0, x + 0.3, 0),
            _seg(x + 0.3, 0, x + 0.3, 0.2),
            _seg(x + 0.3, 0.2, x + 0.9, 0.2),
            _seg(x + 0.9, 0.2, x + 0.9, 0),
            _seg(x + 0.9, 0, x + 1.9, 0),
        ]
        assert "jog" in _violation(bump)


class TestProximity:
    def test_bump_near_the_run_end_passes(self):
        assert _violation(_bump(3.0, 0.2, 0.6)) is None

    def test_bump_far_from_every_source_is_rejected(self):
        message = _violation(_bump(30.0, 0.2, 0.6))
        assert message is not None and "mismatch source" in message

    def test_lead_in_and_lead_out_do_not_count_as_the_bump(self):
        """Leads that span the host put no copper near the run ends (review #7591)."""
        bump = [
            _seg(0, 0, 40, 0),
            _seg(40, 0, 40, 0.2),
            _seg(40, 0.2, 40.6, 0.2),
            _seg(40.6, 0.2, 40.6, 0),
            _seg(40.6, 0, 60, 0),
        ]
        message = _violation(bump)
        assert message is not None and "mismatch source" in message

    def test_partner_via_on_the_run_is_a_source(self):
        partner = _partner(
            [
                Via(
                    x=30.0,
                    y=-0.35,
                    drill=0.3,
                    diameter=0.6,
                    layers=(Layer.F_CU, Layer.B_CU),
                    net=2,
                    net_name="D-",
                )
            ]
        )
        assert _violation(_bump(30.0, 0.2, 0.6), partner=partner) is None

    def test_a_via_splits_the_run(self):
        """Host segments past a via belong to the next run, with their own ends."""
        host = Route(
            net=1,
            net_name="D+",
            segments=[
                _seg(0, 0, 40, 0),
                _seg(40, 0, 100, 0, layer=Layer.B_CU),
            ],
            vias=[
                Via(
                    x=40.0,
                    y=0.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(Layer.F_CU, Layer.B_CU),
                    net=1,
                    net_name="D+",
                )
            ],
        )
        near_via = [
            _seg(x + 40, y, x2 + 40, y2, layer=Layer.B_CU)
            for x, y, x2, y2 in (
                (2, 0, 3, 0),
                (3, 0, 3, 0.2),
                (3, 0.2, 3.6, 0.2),
                (3.6, 0.2, 3.6, 0),
                (3.6, 0, 4.6, 0),
            )
        ]
        assert bump_gate_violation(near_via, host, 1, _partner(), nominal_gap_mm=GAP) is None
        far = [_seg(s.x1 + 30, s.y1, s.x2 + 30, s.y2, layer=Layer.B_CU) for s in near_via]
        assert "mismatch source" in bump_gate_violation(
            far, host, 1, _partner(), nominal_gap_mm=GAP
        )

    def test_limit_is_fifteen_mm(self):
        assert MAX_SOURCE_DISTANCE_MM == 15.0


class TestHelpers:
    def test_jog_spaced_widens_loop_spacing_only_when_needed(self):
        cfg = SerpentineConfig(min_spacing=0.1, gap_factor=2.0)
        spaced = jog_spaced(cfg, 0.2)
        assert spaced.min_spacing * spaced.gap_factor >= 3 * 0.2
        wide = SerpentineConfig(min_spacing=0.5, gap_factor=2.0)
        assert jog_spaced(wide, 0.2) is wide

    def test_replaced_index_counts_shared_leading_segments(self):
        a, b, c = _seg(0, 0, 1, 0), _seg(1, 0, 2, 0), _seg(2, 0, 3, 0)
        host = Route(net=1, net_name="D+", segments=[a, b, c])
        cand = Route(net=1, net_name="D+", segments=[a, _seg(1, 0, 1.5, 0), c])
        assert replaced_index(host, cand) == 1


def _pair() -> DetectedPair:
    def sig(name, net_id, pol):
        return DifferentialSignal(
            net_name=name, net_id=net_id, base_name="D", polarity=pol, notation="plus_minus"
        )

    return DetectedPair(
        pair=DifferentialPair(
            name="D",
            positive=sig("D+", 1, "P"),
            negative=sig("D-", 2, "N"),
            pair_type=DifferentialPairType.USB2,
        ),
        source=DetectionSource.EXPLICIT,
    )


@pytest.mark.parametrize("chamfer", [0.0, 0.55])
def test_tuned_bumps_obey_the_gates(chamfer):
    """Whatever the tuner commits stays inside the AC3 envelope."""
    p = _host(30.0)
    n = Route(net=2, net_name="D-", segments=[_seg(0, -0.35, 31.5, -0.35, 2, "D-")])
    p_out, _n, result = tune_diff_pair_skew(
        _pair(),
        {1: p, 2: n},
        tolerance_mm=0.15,
        intra_pair_clearance_mm=GAP,
        corner_chamfer_widths=chamfer,
    )
    assert result.inserts_applied >= 1
    assert result.skew_after_mm < 1.5  # the bump closed some of the 1.5 mm skew
    offsets = [abs(v) for s in p_out.segments for v in (s.y1, s.y2)]
    assert max(offsets) <= 2 * GAP + 1e-6
    for s in p_out.segments:
        if math.isclose(s.y1, s.y2, abs_tol=1e-9) and s.y1 != 0.0:
            assert math.hypot(s.x2 - s.x1, s.y2 - s.y1) >= 3 * s.width - 1e-6
    anchors = (p_out.segments[0].x1, p_out.segments[-1].x2)
    assert all(
        min(abs(s.x1 - a) for a in anchors) <= MAX_SOURCE_DISTANCE_MM + 1e-6
        for s in p_out.segments
        if s.y1 != 0.0
    )


def test_bump_on_an_end_segment_stays_near_a_run_end():
    """Hosts at the run ends take the bump, and the bump stays near the ends."""
    p = Route(
        net=1,
        net_name="D+",
        segments=[_seg(0, 0, 18, 0), _seg(18, 0, 42, 0), _seg(42, 0, 60, 0)],
    )
    n = Route(net=2, net_name="D-", segments=[_seg(0, -0.35, 60.5, -0.35, 2, "D-")])
    p_out, _n, result = tune_diff_pair_skew(
        _pair(),
        {1: p, 2: n},
        tolerance_mm=0.15,
        intra_pair_clearance_mm=GAP,
    )
    # The ends of the run are the sources: whatever was bumped is near them.
    for s in p_out.segments:
        if s.y1 != 0.0 or s.y2 != 0.0:
            assert min(abs(s.x1 - 0), abs(s.x1 - 60)) <= MAX_SOURCE_DISTANCE_MM + 1e-6
    assert result.skew_after_mm <= result.skew_before_mm


def test_mid_run_longest_host_with_far_bump_is_not_tuned():
    """Longest host [20, 70] would put the loops at x~20.6, 20.6 mm from any end.

    Its lead-out ends 5 mm from the run end at 75, which used to let the gate
    pass.  The loops, not the leads, must be within 15 mm of a source.
    """
    p = Route(
        net=1,
        net_name="D+",
        segments=[_seg(0, 0, 20, 0), _seg(20, 0, 70, 0), _seg(70, 0, 75, 0)],
    )
    n = Route(net=2, net_name="D-", segments=[_seg(0, -0.35, 76, -0.35, 2, "D-")])
    p_out, _n, _result = tune_diff_pair_skew(
        _pair(),
        {1: p, 2: n},
        tolerance_mm=0.15,
        intra_pair_clearance_mm=GAP,
    )
    for s in p_out.segments:
        for x, y in ((s.x1, s.y1), (s.x2, s.y2)):
            if abs(y) > 1e-9:
                assert min(x, 75 - x) <= MAX_SOURCE_DISTANCE_MM + 1e-6
