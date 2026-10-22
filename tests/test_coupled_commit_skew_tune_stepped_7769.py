"""Issue #7769: a grid-stepped joint-search pair can host a skew bump.

On board 07 (``--differential-pairs``, seed 42) the polarity-swapped DQS pair
couples via the joint search only (``kept=joint``; the pose trunk has no
crossover).  The joint search commits one segment per grid step, so DQS_N's
19.9 mm B.Cu trunk reached the in-router skew tune as 189 segments of
0.127 mm.  No segment met the tuner's ``min_segment_length``, so the pair was
declined with ``no_suitable_segment`` at 1.954 mm of skew against a 0.050 mm
``kct check`` limit.

The fix fuses the pair's straight same-width chains of 5 mm or more before the
tune (copper unchanged, as board 06's #7548 recipe step does).  The fixture is
DQS's shape: a B.Cu trunk in 0.127 mm steps, N shorter than P.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPair, DifferentialSignal
from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

W = 0.225  # board 07 DQS trace width
GAP = 0.156  # 3-cell pitch (0.381 mm) minus one width
STEP = 0.127  # board 07's grid pitch: one segment per step
PITCH = 3 * STEP
LIMIT = 0.05  # DDR_DQS skew_tolerance_mm


def _stepped(x0: float, x1: float, y: float, net: int, name: str) -> list[Segment]:
    """``x0 -> x1`` along ``y`` on B.Cu, one segment per grid step."""
    n = round((x1 - x0) / STEP)
    xs = [x0 + i * (x1 - x0) / n for i in range(n + 1)]
    return [
        Segment(a, y, b, y, W, Layer.B_CU, net=net, net_name=name)
        for a, b in zip(xs, xs[1:], strict=False)
    ]


def _length(route: Route) -> float:
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)


def _setup(skew: float = 1.016):
    nc = NetClassRouting(
        name="DDR_DQS",
        trace_width=W,
        clearance=0.15,
        intra_pair_clearance=GAP,
        coupled_routing=True,
        skew_tolerance_mm=LIMIT,
        length_critical=True,
    )
    ar = Autorouter(
        width=40.0,
        height=20.0,
        rules=DesignRules(trace_width=W, trace_clearance=0.15, grid_resolution=STEP),
    )
    ar.net_names = {1: "DQS_P", 2: "DQS_N"}
    ar.net_class_map = {"DQS_P": nc, "DQS_N": nc}
    y = 10.0
    p = Route(net=1, net_name="DQS_P", segments=_stepped(5.0, 25.32, y, 1, "DQS_P"))
    n = Route(net=2, net_name="DQS_N", segments=_stepped(5.0 + skew, 25.32, y + PITCH, 2, "DQS_N"))
    for route in (p, n):
        ar._mark_route(route)
        ar.routes.append(route)
    pair = DifferentialPair(
        name="DQS",
        positive=DifferentialSignal("DQS_P", 1, "DQS", "P", "pn_suffix"),
        negative=DifferentialSignal("DQS_N", 2, "DQS", "N", "pn_suffix"),
    )
    return ar, pair, p, n


def test_stepped_joint_pair_is_bump_tuned_to_the_checker_limit():
    ar, pair, p, n = _setup()
    assert max(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in n.segments) < 0.13
    p_length = _length(p)
    assert p_length - _length(n) == pytest.approx(1.016, abs=1e-6)

    # Before the fix: ``no_suitable_segment`` (no 0.127 mm step can host a bump).
    assert DiffPairRouter(ar)._tune_committed_pair_skew(pair)

    assert abs(_length(p) - _length(n)) <= LIMIT
    # The committed Route objects are edited in place.
    assert ar.routes[0] is p and ar.routes[1] is n
    # The longer half's copper is unchanged (fused, never bumped).
    assert _length(p) == pytest.approx(p_length, abs=1e-9)
    assert all(s.y1 == s.y2 == 10.0 for s in p.segments)
    # The shorter half is still one connected chain.
    for a, b in zip(n.segments, n.segments[1:], strict=False):
        assert (a.x2, a.y2) == pytest.approx((b.x1, b.y1))


def test_short_stepped_chains_are_not_fused():
    """Chains under 5 mm (breakout scale) keep their grid steps (#7548)."""
    from kicad_tools.router.optimizer.merge import merge_collinear_same_width

    ar = Autorouter(width=20.0, height=10.0, rules=DesignRules(grid_resolution=STEP))
    short = Route(net=1, net_name="DQS_P", segments=_stepped(1.0, 3.54, 5.0, 1, "DQS_P"))
    ar.routes.append(short)
    before = list(short.segments)
    assert merge_collinear_same_width(ar, {"DQS_P"}, min_chain_mm=5.0) == 0
    assert short.segments == before
