"""SkewLedger on a gateway pair route: copper order, joints, pitch (#7864).

The lattice gateway emits a pair leg as ``[fan A, fan B, coupled body]``.
Board 06rf ``USB3_RX1`` (seed 42) showed three ledger faults on that shape:

* the runs were read in list order, so "run 1" was the far-end fan and the
  body's skew landed at the far end (the bump gate then refused a body bump
  as 37.7 mm from any source, and the pair stayed at 2.87 mm even at 2x);
* the body legs have different vertex counts (a convergence jog into the
  via pair), so no joint was placed and the winding became one entry;
* the pitch was the mean gap of the run starts (pads, fan vias), not the
  coupled spacing.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice.coupled import offset_polyline
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.skew_ledger import KIND_JOINT, KIND_VIA, build_skew_ledger

PITCH = 0.376
W = 0.2


def _segs(points, net, layer):
    return [
        Segment(a[0], a[1], b[0], b[1], W, layer, net=net, net_name=f"N{net}")
        for a, b in zip(points, points[1:], strict=False)
    ]


def _via(pt, net):
    return Via(pt[0], pt[1], 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=net)


def _gateway_pair():
    """An L-shaped coupled body on B.Cu with F.Cu fans at both ends.

    The body turns 90 degrees left, so P (left of travel) is the inner leg.
    End A's fans are one-sided: N's is 0.7 mm longer.  N's body has one more
    vertex than P's (a collinear jog vertex near its start).
    """
    centre = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0)]
    p_body = offset_polyline(centre, PITCH / 2)
    n_body = offset_polyline(centre, -PITCH / 2)
    assert p_body is not None and n_body is not None
    n_body = [n_body[0], (n_body[0][0] + 0.5, n_body[0][1]), *n_body[1:]]
    p_fan_a = [(p_body[0][0] - 1.0, p_body[0][1]), p_body[0]]
    n_fan_a = [(n_body[0][0] - 1.7, n_body[0][1]), n_body[0]]
    p_fan_b = [p_body[-1], (p_body[-1][0], p_body[-1][1] + 0.8)]
    n_fan_b = [n_body[-1], (n_body[-1][0], n_body[-1][1] + 0.8)]

    def leg(net, fan_a, fan_b, body):
        # Gateway emission order: fan A, fan B, then the body.
        return Route(
            net=net,
            net_name=f"N{net}",
            segments=[
                *_segs(fan_a, net, Layer.F_CU),
                *_segs(fan_b, net, Layer.F_CU),
                *_segs(body, net, Layer.B_CU),
            ],
            vias=[_via(body[0], net), _via(body[-1], net)],
        )

    return leg(1, p_fan_a, p_fan_b, p_body), leg(2, n_fan_a, n_fan_b, n_body)


def test_runs_follow_copper_order_not_list_order() -> None:
    p, n = _gateway_pair()
    ledger = build_skew_ledger(p, n)
    assert ledger.run_count == 3
    # Fan A is run 0 and carries the one-sided fan; fan B (run 2) is even.
    fan_a = [s for s in ledger.sources if s.run == 0]
    assert [s.kind for s in fan_a] == [KIND_VIA]
    assert fan_a[0].skew_mm == pytest.approx(-0.7, abs=1e-6)
    assert not [s for s in ledger.sources if s.run == 2]
    # The P body's first segment (list index 2) is on run 1, the body.
    assert ledger.host_run(p, 2, host_is_p=True) == 1


def test_body_joints_are_placed_despite_differing_vertex_counts() -> None:
    p, n = _gateway_pair()
    ledger = build_skew_ledger(p, n)
    joints = [s for s in ledger.sources if s.kind == KIND_JOINT]
    assert len(joints) == 1
    j = joints[0]
    assert j.run == 1
    # P is inner on the left turn: the joint shortens P by 2 p tan(45 deg).
    assert j.skew_mm == pytest.approx(-2.0 * PITCH * math.tan(math.pi / 4.0), abs=1e-6)
    assert math.hypot(j.x - 20.0, j.y - 0.0) < 0.5
    assert ledger.pitch_mm == pytest.approx(PITCH, abs=1e-3)
    assert ledger.attributed_mm + ledger.residual_mm == pytest.approx(ledger.measured_skew_mm)
    assert abs(ledger.residual_mm) < 1e-9


def test_body_bump_anchors_reach_the_joint() -> None:
    """The bump gate's anchors on the body run include the joint, so a bump
    near the turn is within reach (main put the only body source at a run
    end)."""
    p, n = _gateway_pair()
    ledger = build_skew_ledger(p, n)
    anchors = ledger.anchors(ledger.host_run(p, 2, host_is_p=True))
    assert any(math.hypot(x - 20.0, y) < 0.5 for x, y in anchors)


def test_ordered_route_is_unchanged() -> None:
    """A leg already in copper order reads exactly as before."""
    centre = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    p = offset_polyline(centre, PITCH / 2)
    n = offset_polyline(centre, -PITCH / 2)
    assert p is not None and n is not None
    rp = Route(net=1, net_name="N1", segments=_segs(p, 1, Layer.F_CU))
    rn = Route(net=2, net_name="N2", segments=_segs(n, 2, Layer.F_CU))
    ledger = build_skew_ledger(rp, rn)
    assert [s.kind for s in ledger.sources] == [KIND_JOINT]
    assert ledger.sources[0].skew_mm == pytest.approx(ledger.measured_skew_mm)
