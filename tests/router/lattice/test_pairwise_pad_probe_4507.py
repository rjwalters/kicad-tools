"""The lattice's #4506 pad waiver must probe where the #4588 gate probes (#4507).

The #4507 T4 attribution sub-task asked, of each residual softstart rev-C
board-level census fail: is it (a) an audit-visibility gap, (b) preserved copper
never subject to this run's search, or (c) a genuine search-time defect?

On the 2026-09-14 re-run every gate-reported trace-vs-pad shortfall turned out
to be **(c)**, with one shared root cause::

    LatticeObstacleModel.pairwise_pad_blocked(...)
        ... and not pairwise.exempt_seg_pt(a, b, (pad.x, pad.y), ...)
                                                 ^^^^^^^^^^^^^^

The search probed the #4506 rated-footprint waiver at the pad's **centre**,
while the #4588 gate probes the closest-gap midpoint between the two copper
*polygons* (``_copper_vs_pad_violation`` -> ``_shapely_gap_and_midpoint``).  The
centre is up to half a pad-width deeper INSIDE the rated footprint -- i.e.
systematically further inside the attach-zone rectangle -- so for any proximity
whose true closest-gap midpoint falls just OUTSIDE the zone while the
centre-biased midpoint falls just inside, the search waived copper the gate then
reported.  That is a silent, one-directional disagreement between a search and
the gate auditing it, exactly the class of bug #4588/#4699/#4867 each closed in
turn, and it is what ``exempt_seg_seg``'s "the search verdict and the #4588 gate
agree by construction" contract already promised for trace-vs-trace.

Measured live on the softstart rev-C fixture (2026-09-14, see
``docs/hv-pairwise-softstart-proof.md``): three net pairs / four instances,
every one of them allowed by the centre probe and blocked by the edge probe,
with every genuinely-rated instance still waived by both.

These tests pin the geometry helper and the search/gate agreement on a
synthetic fixture with the same shape.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.obstacles import (
    LatticeObstacleModel,
    closest_point_on_rect_to_segment,
)
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.quadtree import OctilinearLattice
from kicad_tools.router.primitives import Pad

AGENT_RADIUS = 0.25
HV_NET, LV_NET = 1, 2
REQUIRED_MM = 4.0
OWN_HALF = 0.1

# A 2x2 mm LV pad centred at (10, 10): raw copper spans (9, 9)-(11, 11) and the
# model's ``pad_rects`` entry is that box grown by ``AGENT_RADIUS``.
PAD_X, PAD_Y = 10.0, 10.0

# The moving HV segment runs vertically BELOW the pad, ending 3 mm short of the
# pad's bottom edge -- inside the 4 mm requirement, so the pair is live.
SEG_A, SEG_B = (10.0, 4.0), (10.0, 6.0)

# Probe points the two conventions produce for that geometry:
#   edge probe   -> midway between (10, 6) and the pad EDGE   (10, 9)  = (10, 7.5)
#   centre probe -> midway between (10, 6) and the pad CENTRE (10, 10) = (10, 8.0)
EDGE_PROBE_Y = 7.5
CENTRE_PROBE_Y = 8.0


def _model() -> LatticeObstacleModel:
    pad = Pad(x=PAD_X, y=PAD_Y, width=2.0, height=2.0, net=LV_NET, net_name="/LV_SENSE")
    lattice = OctilinearLattice((0.0, 0.0, 20.0, 20.0), [])
    return LatticeObstacleModel(lattice, [pad], [(0,)], 1, agent_radius=AGENT_RADIUS)


def _pairwise(zone_min_y: float | None) -> LatticePairwise:
    """Projection with an optional #4506 zone whose SOUTH edge is ``zone_min_y``."""
    zones = ()
    if zone_min_y is not None:
        zones = ((8.0, zone_min_y, 12.0, 12.25, frozenset({HV_NET, LV_NET}), None),)
    return LatticePairwise(
        required_by_pair={(HV_NET, LV_NET): REQUIRED_MM},
        zones=zones,
        max_by_net={HV_NET: REQUIRED_MM, LV_NET: REQUIRED_MM},
    )


class TestClosestPointOnRectToSegment:
    def test_lands_on_the_pads_own_copper_edge_not_its_centre(self):
        model = _model()
        probe = closest_point_on_rect_to_segment(SEG_A, SEG_B, model.pad_rects[0], AGENT_RADIUS)

        # The pad's raw copper edge, with the agent-radius inflation removed --
        # the same boundary the gate's shapely ``shortest_line`` terminates on.
        assert probe == pytest.approx((10.0, 9.0))
        assert probe != pytest.approx((PAD_X, PAD_Y))

    def test_degenerate_point_probe_is_a_plain_clamp(self):
        """``a == b`` is the lattice-node probe form, as elsewhere in this file."""
        model = _model()
        probe = closest_point_on_rect_to_segment(
            (0.0, 10.0), (0.0, 10.0), model.pad_rects[0], AGENT_RADIUS
        )

        assert probe == pytest.approx((9.0, 10.0))

    def test_a_segment_crossing_the_pad_clamps_inside(self):
        model = _model()
        probe = closest_point_on_rect_to_segment(
            (10.0, 4.0), (10.0, 16.0), model.pad_rects[0], AGENT_RADIUS
        )

        # Inside the raw rect: the gate measures a zero gap here too.
        assert 9.0 <= probe[0] <= 11.0
        assert 9.0 <= probe[1] <= 11.0


class TestSearchAndGateProbeTheSamePoint:
    """The case (c) defect, and that fixing it does not over-tighten #4506."""

    def test_zone_that_only_covers_the_centre_probe_no_longer_waives(self):
        """The regression: gate reports it, so the search must decline it.

        The zone's south edge sits BETWEEN the two probe conventions, so the
        centre-biased probe falls inside it and the true closest-gap probe does
        not.  Pre-#4507 the search waived this and the gate reported it.
        """
        zone_min_y = (EDGE_PROBE_Y + CENTRE_PROBE_Y) / 2.0
        pairwise = _pairwise(zone_min_y)
        # Precondition: the two conventions genuinely straddle the zone edge.
        assert pairwise.exempt(10.0, CENTRE_PROBE_Y, HV_NET, LV_NET, 0)
        assert not pairwise.exempt(10.0, EDGE_PROBE_Y, HV_NET, LV_NET, 0)

        blocked = _model().pairwise_pad_blocked(SEG_A, SEG_B, 0, HV_NET, OWN_HALF, 0.0, pairwise)

        assert blocked, "search waived proximity the #4588 gate reports"

    def test_a_zone_covering_the_true_closest_gap_point_still_waives(self):
        """#4506 must keep working -- a rated footprint stays routable."""
        pairwise = _pairwise(zone_min_y=7.0)
        assert pairwise.exempt(10.0, EDGE_PROBE_Y, HV_NET, LV_NET, 0)

        blocked = _model().pairwise_pad_blocked(SEG_A, SEG_B, 0, HV_NET, OWN_HALF, 0.0, pairwise)

        assert not blocked, "the #4506 exemption stopped waiving genuinely rated copper"

    def test_no_zone_at_all_still_blocks(self):
        blocked = _model().pairwise_pad_blocked(
            SEG_A, SEG_B, 0, HV_NET, OWN_HALF, 0.0, _pairwise(zone_min_y=None)
        )

        assert blocked

    def test_far_copper_outside_the_requirement_is_untouched(self):
        """Beyond the requirement the predicate stays silent (no over-blocking)."""
        far_a, far_b = (10.0, -10.0), (10.0, -8.0)

        blocked = _model().pairwise_pad_blocked(
            far_a, far_b, 0, HV_NET, OWN_HALF, 0.0, _pairwise(zone_min_y=None)
        )

        assert not blocked

    def test_same_net_pad_is_never_an_obstacle(self):
        blocked = _model().pairwise_pad_blocked(
            SEG_A, SEG_B, 0, LV_NET, OWN_HALF, 0.0, _pairwise(zone_min_y=None)
        )

        assert not blocked
