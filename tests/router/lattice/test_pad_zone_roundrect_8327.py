"""The search-time attach-zone waiver must measure roundrect pads (#8327).

#8280 (issue #8277) made ``_pad_zone_waives`` run the commit gate's own
``_attach_zone_verdict`` on true copper.  But it took the pad outline from
``pad_waiver_polygon``, which returns ``None`` for ``roundrect`` -- the shape of
nearly every SMD pad.  Those pads fell back to ``_pad_pair_zone_contains_both``
(one zone must hold the whole segment AND the whole pad), so any lattice edge
that crosses a rated footprint's attach-zone boundary near the foreign pad of
the same footprint was refused, although the gate accepts it: its residual
outside the zone clears.  On an HV board that strands the low side of a
two-terminal tap part (one net lost per run, reproducibly, with no foreign
routed copper nearby).

The fixture is synthetic: a 2-pad tap resistor between an HV net and an LV net
(roundrect pads, 1.6 mm pair requirement, the footprint's attach zone), and an
HV trace entering the zone from outside to reach its own pad.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.geometry.copper import pad_gate_polygon, pad_waiver_polygon
from kicad_tools.router.lattice.obstacles import LatticeObstacleModel, _pad_zone_waives
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.quadtree import OctilinearLattice
from kicad_tools.router.primitives import Pad

HV_NET, LV_NET = 1, 2
REQUIRED_MM = 1.6
AGENT_RADIUS = 0.15
OWN_HALF = 0.1
RRATIO = 0.25

# Tap resistor R_a: HV pad at the origin, LV pad 1.8 mm east (1.0 x 1.4 mm).
HV_PAD = Pad(
    x=0.0,
    y=0.0,
    width=1.0,
    height=1.4,
    net=HV_NET,
    net_name="HV",
    shape="roundrect",
    roundrect_rratio=RRATIO,
)
LV_PAD = Pad(
    x=1.8,
    y=0.0,
    width=1.0,
    height=1.4,
    net=LV_NET,
    net_name="LV",
    shape="roundrect",
    roundrect_rratio=RRATIO,
)
# The footprint's attach zone (both pads plus margin).
ZONE = (-2.0, -2.4, 3.8, 2.4, frozenset({HV_NET, LV_NET}), None)

# An HV edge entering the zone from the south to reach its own pad.  Its end
# inside the zone sits ~0.84 mm from the LV pad (waived: both nearest points
# are in the zone); the part outside the zone (y <= -2.4) clears 1.6 mm.
CROSSING = ((0.0, -3.0), (0.0, -1.0))


def _model(lv_pad: Pad = LV_PAD) -> LatticeObstacleModel:
    lattice = OctilinearLattice((-6.0, -6.0, 8.0, 6.0), [])
    return LatticeObstacleModel(lattice, [HV_PAD, lv_pad], [(0,), (0,)], 1, AGENT_RADIUS)


def _pairwise(zones=(ZONE,)) -> LatticePairwise:
    return LatticePairwise(
        required_by_pair={(HV_NET, LV_NET): REQUIRED_MM},
        zones=tuple(zones),
        max_by_net={HV_NET: REQUIRED_MM, LV_NET: REQUIRED_MM},
    )


def _blocked(a, b, lv_pad: Pad = LV_PAD, zones=(ZONE,)) -> bool:
    return _model(lv_pad).pairwise_pad_blocked(a, b, 0, HV_NET, OWN_HALF, 0.0, _pairwise(zones))


class TestRoundrectPadWaiver:
    def test_edge_crossing_the_zone_boundary_is_waived_like_the_gate(self):
        # Pre-fix: refused (the roundrect had no polygon, so the whole edge had
        # to sit inside the zone).
        assert not _blocked(*CROSSING)
        assert _pad_zone_waives(*CROSSING, LV_PAD, OWN_HALF, HV_NET, 0, REQUIRED_MM, _pairwise())

    def test_unknown_ratio_is_measured_as_the_enclosing_rectangle(self):
        from dataclasses import replace

        assert not _blocked(*CROSSING, lv_pad=replace(LV_PAD, roundrect_rratio=None))

    def test_nearest_point_outside_the_zone_is_still_refused(self):
        # A trace running past the LV pad just outside a tight zone: its
        # nearest point to the pad lies outside the zone, so the gate refuses
        # (both witnesses must be in one zone) -- and so must the search.
        tight = (-2.0, -1.2, 3.8, 1.2, frozenset({HV_NET, LV_NET}), None)
        assert _blocked((0.0, -1.5), (3.0, -1.5), zones=(tight,))
        # The same trace inside a zone that holds it is waived.
        assert not _blocked((0.0, -1.5), (3.0, -1.5))

    def test_no_zone_means_no_waiver(self):
        assert _blocked(*CROSSING, zones=())

    def test_edge_clearing_the_requirement_is_not_blocked(self):
        assert not _blocked((-2.0, -3.0), (-2.0, -1.0), zones=())


class TestPadGatePolygon:
    def test_roundrect_is_the_rounded_outline(self):
        poly = pad_gate_polygon(1.8, 0.0, 1.0, 1.4, "roundrect", 0.0, RRATIO)
        r = RRATIO * 1.0
        expected = 1.0 * 1.4 - (4.0 - math.pi) * r * r
        assert poly.area == pytest.approx(expected, rel=1e-3)
        assert poly.bounds == pytest.approx((1.3, -0.7, 2.3, 0.7), abs=1e-9)

    def test_unknown_ratio_is_the_rectangle(self):
        poly = pad_gate_polygon(1.8, 0.0, 1.0, 1.4, "roundrect", 0.0, None)
        assert poly.area == pytest.approx(1.4)

    def test_max_ratio_on_a_square_pad_is_a_disc(self):
        poly = pad_gate_polygon(0.0, 0.0, 1.0, 1.0, "roundrect", 0.0, 0.5)
        assert poly.area == pytest.approx(math.pi * 0.25, rel=1e-3)

    def test_rotation_follows_pad_waiver_polygon(self):
        rect = pad_waiver_polygon(1.0, 2.0, 1.0, 0.5, "rect", 90.0)
        rounded = pad_gate_polygon(1.0, 2.0, 1.0, 0.5, "roundrect", 90.0, None)
        assert rounded.symmetric_difference(rect).area == pytest.approx(0.0, abs=1e-9)

    def test_other_shapes_defer_to_pad_waiver_polygon(self):
        for shape in ("rect", "circle", "oval"):
            assert pad_gate_polygon(0.0, 0.0, 1.0, 0.6, shape, 0.0, None) is pad_waiver_polygon(
                0.0, 0.0, 1.0, 0.6, shape, 0.0
            )
        assert pad_gate_polygon(0.0, 0.0, 0.0, 0.6, "roundrect", 0.0, 0.25) is None
