"""SkewLedger: per-source attribution of a finished pair's P-N skew (#6447)."""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice.coupled import offset_polyline
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairplan.bend_balance import joint_skew
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.skew_ledger import KIND_JOINT, KIND_TIE, KIND_VIA, build_skew_ledger

PITCH = 0.36


def _route(points, net, layer=Layer.F_CU):
    segs = [
        Segment(a[0], a[1], b[0], b[1], 0.1, layer, net=net, net_name=f"N{net}")
        for a, b in zip(points, points[1:], strict=False)
    ]
    return Route(net=net, net_name=f"N{net}", segments=segs)


def _pair(center, p_left=True):
    s = 1.0 if p_left else -1.0
    p = offset_polyline(center, s * PITCH / 2)
    n = offset_polyline(center, -s * PITCH / 2)
    assert p is not None and n is not None
    return _route(p, 1), _route(n, 2)


def _heading(deg, length):
    r = math.radians(deg)
    return length * math.cos(r), length * math.sin(r)


def _polyline(start, turns_deg, length=5.0):
    pts = [start]
    heading = 0.0
    x, y = start
    for t in (0.0, *turns_deg):
        heading += t
        dx, dy = _heading(heading, length)
        x, y = x + dx, y + dy
        pts.append((x, y))
    return pts


@pytest.mark.parametrize("turn", [45.0, 90.0, 135.0, -90.0, -135.0])
@pytest.mark.parametrize("p_left", [True, False])
def test_single_joint_matches_formula(turn: float, p_left: bool) -> None:
    center = _polyline((0.0, 0.0), [turn])
    p, n = _pair(center, p_left)
    ledger = build_skew_ledger(p, n)

    assert len(ledger.sources) == 1
    src = ledger.sources[0]
    assert src.kind == KIND_JOINT
    expected = joint_skew(round(abs(turn) / 45), PITCH)
    assert abs(src.skew_mm) == pytest.approx(expected, abs=1e-6)
    # P is inner on a turn toward its own side.
    assert (src.skew_mm < 0) == (p_left == (turn > 0))
    assert ledger.measured_skew_mm == pytest.approx(src.skew_mm, abs=1e-6)
    assert abs(ledger.residual_mm) < 1e-6
    assert ledger.pitch_mm == pytest.approx(PITCH)


def test_two_joints_sum_to_measured_skew() -> None:
    center = _polyline((0.0, 0.0), [90.0, 135.0])
    p, n = _pair(center)
    ledger = build_skew_ledger(p, n)

    assert [s.kind for s in ledger.sources] == [KIND_JOINT, KIND_JOINT]
    assert abs(ledger.attributed_mm - ledger.measured_skew_mm) < 0.01
    assert abs(ledger.residual_mm) < 0.01
    assert ledger.total_by_kind()[KIND_JOINT] == pytest.approx(ledger.measured_skew_mm)


def test_dominant_is_largest_same_sign_source() -> None:
    center = _polyline((0.0, 0.0), [90.0, -45.0, 45.0])
    p, n = _pair(center)
    ledger = build_skew_ledger(p, n)
    dom = ledger.dominant()
    assert dom is not None
    assert dom.skew_mm * ledger.measured_skew_mm > 0
    # The 90-degree joint is the biggest term.
    assert abs(dom.skew_mm) == pytest.approx(joint_skew(2, PITCH), abs=1e-6)
    assert len(ledger.anchors()) == len(ledger.sources)


def test_n_routed_from_far_end_is_oriented() -> None:
    center = _polyline((0.0, 0.0), [90.0])
    p, n = _pair(center)
    n_rev = _route(
        list(
            reversed([(s.x1, s.y1) for s in n.segments] + [(n.segments[-1].x2, n.segments[-1].y2)])
        ),
        2,
    )
    a = build_skew_ledger(p, n)
    b = build_skew_ledger(p, n_rev)
    assert b.measured_skew_mm == pytest.approx(a.measured_skew_mm)
    assert b.sources[0].skew_mm == pytest.approx(a.sources[0].skew_mm)


def test_straight_pair_with_one_longer_leg_is_a_tie() -> None:
    p = _route([(0.0, 0.18), (10.0, 0.18)], 1)
    n = _route([(0.0, -0.18), (8.0, -0.18)], 2)
    ledger = build_skew_ledger(p, n)
    assert ledger.measured_skew_mm == pytest.approx(2.0)
    assert [s.kind for s in ledger.sources] == [KIND_TIE]
    assert ledger.sources[0].skew_mm == pytest.approx(2.0)
    assert ledger.residual_mm == pytest.approx(0.0)


def test_via_on_one_leg_only_is_attributed_to_via() -> None:
    p = _route([(0.0, 0.18), (5.0, 0.18)], 1)
    p.segments.append(Segment(5.0, 0.18, 9.0, 0.18, 0.1, Layer.B_CU, net=1, net_name="N1"))
    p.vias.append(Via(5.0, 0.18, 0.2, 0.4, (Layer.F_CU, Layer.B_CU), net=1))
    n = _route([(0.0, -0.18), (5.0, -0.18)], 2)
    n.segments.append(Segment(5.0, -0.18, 8.5, -0.18, 0.1, Layer.B_CU, net=2, net_name="N2"))
    n.vias.append(Via(5.0, -0.18, 0.2, 0.4, (Layer.F_CU, Layer.B_CU), net=2))
    ledger = build_skew_ledger(p, n)
    assert ledger.measured_skew_mm == pytest.approx(0.5)
    assert [s.kind for s in ledger.sources] == [KIND_VIA]
    assert ledger.sources[0].run == 1
    assert ledger.anchors(run=0) == []
    assert abs(ledger.residual_mm) < 1e-9


def test_unpairable_runs_leave_the_skew_as_residual() -> None:
    p = _route([(0.0, 0.18), (5.0, 0.18)], 1)
    p.segments.append(Segment(5.0, 0.18, 9.0, 0.18, 0.1, Layer.B_CU, net=1, net_name="N1"))
    p.vias.append(Via(5.0, 0.18, 0.2, 0.4, (Layer.F_CU, Layer.B_CU), net=1))
    n = _route([(0.0, -0.18), (8.0, -0.18)], 2)
    ledger = build_skew_ledger(p, n)
    assert ledger.sources == ()
    assert ledger.residual_mm == pytest.approx(ledger.measured_skew_mm)
