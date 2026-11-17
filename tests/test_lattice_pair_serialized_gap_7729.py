"""Lattice diff-pair legs keep ``pair_gap`` after 4-decimal serialization (Issue #7729).

06rf seed 42 routed 26/26 yet ``kct check`` reported 12 ``diffpair_clearance_intra``
findings: legs offset exactly ``pitch/2`` apart lose up to ~0.14 um when the board
file stores 4 decimals, and the checker allows only 1e-4 mm under the gap.
"""

from __future__ import annotations

import math
import random

from kicad_tools.router.lattice.coupled import offset_polyline
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.pathfinder import _PAIR_SERIALIZATION_MARGIN_MM

TRACE_W = 0.275
GAP = 0.1016  # 4 mil: the floor ``DesignRules.pair_gap`` hands the checker
DRC_TOLERANCE = 1e-4  # validate/rules/base.py


def _octilinear_centerline(rng: random.Random) -> list[tuple[float, float]]:
    pts = [(rng.uniform(5, 6), rng.uniform(5, 6))]
    for _ in range(8):
        ang = rng.choice([0, 45, -45])  # x-monotone: legs never self-cross
        step = rng.uniform(0.6, 1.5)
        x, y = pts[-1]
        pts.append((x + step * math.cos(math.radians(ang)), y + step * math.sin(math.radians(ang))))
    return pts


def _min_leg_gap(plus, minus) -> float:
    q = lambda leg: [(round(x, 4), round(y, 4)) for x, y in leg]  # noqa: E731
    plus, minus = q(plus), q(minus)
    best = math.inf
    for a, b in zip(plus, plus[1:], strict=False):
        for c, d in zip(minus, minus[1:], strict=False):
            if a != b and c != d:
                best = min(best, seg_seg_dist(a, b, c, d) - TRACE_W)
    return best


def test_serialized_legs_meet_pair_gap_checker_threshold() -> None:
    rng = random.Random(7729)
    half = (TRACE_W + GAP) / 2.0 + _PAIR_SERIALIZATION_MARGIN_MM / 2.0
    checked = 0
    for _ in range(300):
        center = _octilinear_centerline(rng)
        plus, minus = offset_polyline(center, half), offset_polyline(center, -half)
        if plus is None or minus is None:
            continue
        checked += 1
        assert _min_leg_gap(plus, minus) + DRC_TOLERANCE >= GAP
    assert checked > 100


def test_margin_is_needed_without_it() -> None:
    """Pins the failure mode: exact ``pitch/2`` offsets can dip under the gap."""
    rng = random.Random(7729)
    half = (TRACE_W + GAP) / 2.0
    worst = math.inf
    for _ in range(300):
        center = _octilinear_centerline(rng)
        plus, minus = offset_polyline(center, half), offset_polyline(center, -half)
        if plus is not None and minus is not None:
            worst = min(worst, _min_leg_gap(plus, minus))
    assert worst < GAP
