"""Source-side diff-pair skew avoidance (issue #7649).

Two pieces, both default behaviour on the lattice engine:

* **Balanced bends** (:mod:`kicad_tools.router.pairplan.bend_balance`): a
  mitred joint turning ``theta`` adds ``2 p tan(theta/2)`` of P-N skew, so a
  sharp 90- or 135-degree joint makes far more skew than the same turn taken
  in 45-degree steps.  The pair finish gate now also verifies the searched
  centerline with its skew-adding sharp joints stepped, and ships the
  variant with the least skew.
* **Distinct gateways** (:func:`~kicad_tools.router.pairplan.gateway.enumerate_gateways`):
  a ``centred`` site that coincides with the ``parallel`` one is the same
  gateway, and used to spend the combination budget on copies.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from kicad_tools.router.lattice import pathfinder as pathfinder_mod
from kicad_tools.router.lattice.coupled import CoupledConnection, offset_polyline
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pairplan.bend_balance import (
    balance_bends,
    centerline_skew,
    joint_skew,
)
from kicad_tools.router.pairplan.gateway import enumerate_gateways

from .router.lattice.test_coupled_pairs import _HS, _PITCH, _RULES, _pad

P = 0.36


def _length(poly: list[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(poly, poly[1:], strict=False))


def _leg_skew(center: list[tuple[float, float]], pitch: float) -> float:
    """Measured P-N difference of the two offset legs of ``center``."""
    plus = offset_polyline(center, pitch / 2.0)
    minus = offset_polyline(center, -pitch / 2.0)
    assert plus is not None and minus is not None
    return _length(plus) - _length(minus)


# ---------------------------------------------------------------------------
# Geometry (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("units", "factor"),
    [(1, 2 * math.tan(math.pi / 8)), (2, 2.0), (3, 2 * math.tan(3 * math.pi / 8))],
)
def test_joint_skew_matches_the_offset_legs(units: int, factor: float) -> None:
    assert joint_skew(units, P) == pytest.approx(factor * P)
    assert joint_skew(-units, P) == pytest.approx(-factor * P)
    heading = units * math.pi / 4
    center = [(0.0, 0.0), (5.0, 0.0), (5.0 + 5 * math.cos(heading), 5 * math.sin(heading))]
    # The left (+) leg is the inner one on a counter-clockwise turn.
    assert -_leg_skew(center, P) == pytest.approx(joint_skew(units, P), abs=1e-9)


@pytest.mark.parametrize(
    ("center", "before", "after"),
    [
        # 90 degrees: one sharp joint (2 p) -> two 45-degree joints (1.66 p).
        ([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)], 2.0, 4 * math.tan(math.pi / 8)),
        # 135 degrees: 4.83 p -> three 45-degree joints, 2.49 p.
        (
            [(0.0, 0.0), (5.0, 0.0), (1.0, 4.0)],
            2 * math.tan(3 * math.pi / 8),
            6 * math.tan(math.pi / 8),
        ),
        # A U made of two sharp 90s: 4 p -> 3.31 p.
        ([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)], 4.0, 8 * math.tan(math.pi / 8)),
    ],
    ids=["90", "135", "U"],
)
def test_balanced_bends_cut_the_offset_skew(center, before, after) -> None:
    balanced = balance_bends(center, P, P)
    assert balanced is not None
    assert abs(_leg_skew(center, P)) == pytest.approx(before * P)
    assert abs(_leg_skew(balanced, P)) == pytest.approx(after * P)
    assert abs(centerline_skew(balanced, P)) == pytest.approx(after * P)
    # Same ends, same end directions, every step octilinear, never longer.
    assert balanced[0] == center[0] and balanced[-1] == center[-1]
    for got, want in ((balanced[:2], center[:2]), (balanced[-2:], center[-2:])):
        g = math.atan2(got[1][1] - got[0][1], got[1][0] - got[0][0])
        w = math.atan2(want[1][1] - want[0][1], want[1][0] - want[0][0])
        assert g == pytest.approx(w)
    for a, b in zip(balanced, balanced[1:], strict=False):
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45.0
        assert min(ang, 45.0 - ang) < 1e-6
    assert _length(balanced) < _length(center)


def test_an_opposing_sharp_joint_is_left_sharp() -> None:
    # Net +90 made of a sharp -90 then four +45s: stepping the -90 would
    # ADD skew (-2 p + 3.31 p -> -1.66 p + 3.31 p).
    center = [
        (0.0, 0.0),
        (4.0, 0.0),
        (4.0, -3.0),
        (6.0, -5.0),
        (9.0, -5.0),
        (11.0, -3.0),
        (11.0, 2.0),
    ]
    before = centerline_skew(center, P)
    assert before is not None and before > 0
    assert balance_bends(center, P, P) is None


def test_no_sharp_joint_or_no_room_changes_nothing() -> None:
    assert balance_bends([(0.0, 0.0), (5.0, 0.0), (8.0, 3.0)], P, P) is None
    assert balance_bends([(0.0, 0.0), (5.0, 0.0)], P, P) is None
    # The 90 sits 0.1 mm from the start: its 45-degree step needs 0.25 mm.
    assert balance_bends([(0.0, 0.0), (0.1, 0.0), (0.1, 5.0)], P, P) is None
    # Off-octilinear input is left alone.
    assert balance_bends([(0.0, 0.0), (5.0, 0.0), (6.0, 3.0)], P, P) is None


# ---------------------------------------------------------------------------
# Lattice pair: pre-bump skew on synthetic asymmetric-pad pairs
# ---------------------------------------------------------------------------


def _turning_pair(b_x: float, b_y: float) -> tuple[LatticePathfinder, CoupledConnection]:
    """End A's pads stack vertically, end B's sit side by side: a 90-degree net turn."""
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]
    p_a = _pad(3.0, 5.0, 1, "D+", ref="J1")
    n_a = _pad(3.0, 6.0, 2, "D-", ref="J1")
    p_b = _pad(b_x, b_y, 1, "D+", ref="U1")
    n_b = _pad(b_x + 1.0, b_y, 2, "D-", ref="U1")
    pf = LatticePathfinder(outline, [p_a, n_a, p_b, n_b], _RULES, LayerStack.two_layer())
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=p_a,
        pad_n_a=n_a,
        pad_p_b=p_b,
        pad_n_b=n_b,
        # Issue #7970: end-stub balancing only moves copper for a skew the
        # pair's bound would flag, so give the pair a bound below the turn skew.
        net_class=dataclasses.replace(_HS, skew_tolerance_mm=0.05),
        pitch=_PITCH,
    )
    return pf, pc


def _routed_skew(monkeypatch: pytest.MonkeyPatch, b_x: float, b_y: float, *, balance: bool):
    with monkeypatch.context() as m:
        if not balance:
            m.setattr(pathfinder_mod, "_balanced_centerlines", lambda _result, _pitch: [])
        pf, pc = _turning_pair(b_x, b_y)
        routes, _stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_outcomes

    def length(route) -> float:
        return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)

    return abs(length(routes[(pc.key, "P")]) - length(routes[(pc.key, "N")]))


@pytest.mark.parametrize(("b_x", "b_y"), [(20.0, 15.0), (20.0, 2.0)], ids=["down", "up"])
def test_balanced_bends_reduce_pre_bump_skew(monkeypatch, b_x: float, b_y: float) -> None:
    searched = _routed_skew(monkeypatch, b_x, b_y, balance=False)
    balanced = _routed_skew(monkeypatch, b_x, b_y, balance=True)
    assert balanced < searched - 0.05, (searched, balanced)


# ---------------------------------------------------------------------------
# Gateways: no duplicate candidates
# ---------------------------------------------------------------------------


def test_centred_site_equal_to_parallel_is_one_gateway() -> None:
    # Pads exactly one via gap apart: the centred family's sites are the
    # parallel family's sites, rung for rung.
    via_gap = 0.7
    pad_p, pad_n = (0.0, 0.0), (0.0, via_gap)

    def check(site):
        return [pad_p, site.via_p], [pad_n, site.via_n]

    found, legal = enumerate_gateways(
        pad_p, pad_n, (10.0, 0.35), 1, check, pitch=0.36, via_gap=via_gap
    )
    keys = [
        (g.site.heading, tuple(round(c, 6) for c in g.site.via_p + g.site.via_n)) for g in found
    ]
    assert len(keys) == len(set(keys)) == legal
    # Heading 0 / 180 run perpendicular to the pad axis: there every centred
    # site is a parallel copy.  Diagonal centred sites stay (they differ).
    across = {g.site.family for g in found if g.site.heading in (0, 4)}
    assert "parallel" in across and "centred" not in across
    assert any(g.site.family == "centred" for g in found)
