"""The bump gate measures to SkewLedger sources, joints included (#6447)."""

from __future__ import annotations

import pytest

from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_bump_gates import MAX_SOURCE_DISTANCE_MM, bump_gate_violation
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import tune_diff_pair_skew
from kicad_tools.router.lattice.coupled import offset_polyline
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.skew_ledger import build_skew_ledger

W = 0.1
PITCH = 0.36
GAP = 0.26
ARM = 40.0  # each straight arm is far longer than MAX_SOURCE_DISTANCE_MM


def _route(points, net, layer=Layer.F_CU, vias=None):
    segs = [
        Segment(a[0], a[1], b[0], b[1], W, layer, net=net, net_name=f"N{net}")
        for a, b in zip(points, points[1:], strict=False)
    ]
    return Route(net=net, net_name=f"N{net}", segments=segs, vias=vias or [])


def _l_pair(p_left: bool = True):
    """Two arms of ARM mm with one 90-degree joint at (ARM, 0)."""
    centre = [(0.0, 0.0), (ARM, 0.0), (ARM, ARM)]
    s = 1.0 if p_left else -1.0
    p = offset_polyline(centre, s * PITCH / 2)
    n = offset_polyline(centre, -s * PITCH / 2)
    assert p is not None and n is not None
    return _route(p, 1), _route(n, 2)


def _bump_at(x: float, route: Route, amplitude: float = 0.3) -> list[Segment]:
    """Lead-in, one square loop, lead-out replacing segment 0 of ``route``."""
    seg = route.segments[0]
    y = seg.y1
    a = amplitude
    pts = [
        (seg.x1, y),
        (x, y),
        (x, y + a),
        (x + 1.0, y + a),
        (x + 1.0, y),
        (seg.x2, y),
    ]
    return [
        Segment(p[0], p[1], q[0], q[1], W, seg.layer, net=seg.net, net_name=seg.net_name)
        for p, q in zip(pts, pts[1:], strict=False)
    ]


def _violation(host: Route, partner: Route, bump, ledger, *, host_is_p: bool):
    run = ledger.host_run(host, 0, host_is_p=host_is_p)
    assert run is not None
    return bump_gate_violation(
        bump,
        host,
        0,
        partner,
        nominal_gap_mm=GAP,
        source_anchors=ledger.anchors(run),
    )


@pytest.mark.parametrize("p_left", [True, False])
@pytest.mark.parametrize("host_is_p", [True, False])
def test_bump_near_a_joint_is_accepted_and_far_one_rejected(p_left, host_is_p):
    p, n = _l_pair(p_left)
    ledger = build_skew_ledger(p, n)
    assert any(s.kind == "joint" for s in ledger.sources)
    host, partner = (p, n) if host_is_p else (n, p)

    near = _bump_at(ARM - 3.0, host)
    far = _bump_at(ARM - 3.0 - (MAX_SOURCE_DISTANCE_MM + 5.0), host)

    # Run ends are ARM mm away, so the old re-scan cannot see the joint.
    assert bump_gate_violation(near, host, 0, partner, nominal_gap_mm=GAP) is not None
    assert _violation(host, partner, near, ledger, host_is_p=host_is_p) is None
    assert "mismatch source" in _violation(host, partner, far, ledger, host_is_p=host_is_p)


def test_no_ledger_source_on_the_run_falls_back_to_the_rescan():
    """A straight pair (no joint, no leftover) leaves the gate to its own scan."""
    p = _route([(0.0, 0.0), (30.0, 0.0)], 1)
    n = _route([(0.0, -PITCH), (30.0, -PITCH)], 2)
    ledger = build_skew_ledger(p, n)
    assert ledger.anchors(0) == []
    bump = _bump_at(2.0, p)
    assert bump_gate_violation(bump, p, 0, n, nominal_gap_mm=GAP, source_anchors=[]) is None


def test_host_run_follows_the_via_delimited_runs_and_n_orientation():
    via = Via(x=10.0, y=0.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
    p = _route([(0.0, 0.0), (10.0, 0.0)], 1)
    p.segments += _route([(10.0, 0.0), (20.0, 0.0)], 1, layer=Layer.B_CU).segments
    p.vias = [via]
    # N is routed from the far end: its first run is the ledger's last.
    n = _route([(20.0, -PITCH), (10.0, -PITCH)], 2, layer=Layer.B_CU)
    n.segments += _route([(10.0, -PITCH), (0.0, -PITCH)], 2).segments
    n.vias = [
        Via(x=10.0, y=-PITCH, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
    ]
    ledger = build_skew_ledger(p, n)
    assert ledger.run_count == 2 and ledger.n_reversed
    assert ledger.host_run(p, 0, host_is_p=True) == 0
    assert ledger.host_run(p, 1, host_is_p=True) == 1
    assert ledger.host_run(n, 0, host_is_p=False) == 1
    assert ledger.host_run(n, 1, host_is_p=False) == 0
    assert ledger.host_run(p, 7, host_is_p=True) is None


def test_ledger_sum_matches_measured_skew_with_90_and_135_joints():
    centre = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (10.0, 30.0)]
    p_pts = offset_polyline(centre, PITCH / 2)
    n_pts = offset_polyline(centre, -PITCH / 2)
    assert p_pts is not None and n_pts is not None
    ledger = build_skew_ledger(_route(p_pts, 1), _route(n_pts, 2))
    assert sum(1 for s in ledger.sources if s.kind == "joint") == 2
    assert ledger.attributed_mm == pytest.approx(ledger.measured_skew_mm, abs=0.01)


def _detected() -> DetectedPair:
    def sig(name, net_id, pol):
        return DifferentialSignal(
            net_name=name, net_id=net_id, base_name="D", polarity=pol, notation="plus_minus"
        )

    return DetectedPair(
        pair=DifferentialPair(
            name="D",
            positive=sig("N1", 1, "P"),
            negative=sig("N2", 2, "N"),
            pair_type=DifferentialPairType.USB2,
        ),
        source=DetectionSource.EXPLICIT,
    )


def test_tuner_with_ledger_keeps_the_untouched_span_and_the_longer_leg():
    """Retention proxy: only the host segment is replaced; the rest is by identity."""
    p, n = _l_pair(p_left=True)
    ledger = build_skew_ledger(p, n)
    shorter_before = list(p.segments if ledger.measured_skew_mm < 0 else n.segments)
    p_out, n_out, result = tune_diff_pair_skew(
        _detected(),
        {1: p, 2: n},
        tolerance_mm=0.05,
        intra_pair_clearance_mm=GAP,
        ledger=ledger,
    )
    assert result.reason != "unrouted"
    tuned, orig = (
        (p_out, shorter_before) if ledger.measured_skew_mm < 0 else (n_out, shorter_before)
    )
    if result.inserts_applied:
        kept = [s for s in tuned.segments if any(s is o for o in orig)]
        assert len(kept) >= len(orig) - 1


def test_tuner_does_not_raise_when_legs_have_different_vertex_counts():
    centre = [(0.0, 0.0), (ARM, 0.0), (ARM, ARM)]
    p_pts = offset_polyline(centre, PITCH / 2)
    assert p_pts is not None
    p = _route(p_pts, 1)
    n = _route([(0.0, -PITCH / 2), (ARM + PITCH / 2 + 1.0, -PITCH / 2)], 2)
    ledger = build_skew_ledger(p, n)
    tune_diff_pair_skew(
        _detected(),
        {1: p, 2: n},
        tolerance_mm=0.05,
        intra_pair_clearance_mm=GAP,
        ledger=ledger,
    )
