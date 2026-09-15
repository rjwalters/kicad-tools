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
    pad_waiver_probe_point,
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

# Probe points the three conventions produce for that geometry:
#   copper probe (correct) -> midway between the TRACE COPPER EDGE (10, 6.1)
#                             and the PAD COPPER EDGE (10, 9)        = (10, 7.55)
#   centreline probe       -> midway between (10, 6) and (10, 9)     = (10, 7.50)
#   pad-centre probe (the original defect)
#                          -> midway between (10, 6) and (10, 10)    = (10, 8.00)
COPPER_PROBE_Y = 7.55
CENTRELINE_PROBE_Y = 7.5
PAD_CENTRE_PROBE_Y = 8.0


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


class TestPadWaiverProbePoint:
    def test_is_the_copper_to_copper_midpoint_not_a_centreline_one(self):
        model = _model()
        probe = pad_waiver_probe_point(SEG_A, SEG_B, model.pads[0], OWN_HALF)

        assert probe == pytest.approx((10.0, COPPER_PROBE_Y))
        # Neither of the two wrong conventions this helper exists to replace.
        assert probe[1] != pytest.approx(CENTRELINE_PROBE_Y)
        assert probe[1] != pytest.approx(PAD_CENTRE_PROBE_Y)

    def test_matches_the_gates_own_shapely_midpoint(self):
        """Search and gate must agree by construction, not by coincidence.

        This is the check the previous revision lacked: it compares against the
        production gate helper on real polygons rather than against another
        analytic formula derived the same (wrong) way.
        """
        from shapely.geometry import box

        from kicad_tools.router.layers import Layer
        from kicad_tools.router.pairwise_clearance import (
            _segment_copper_polygon,
            _shapely_gap_and_midpoint,
        )
        from kicad_tools.router.primitives import Segment

        seg = Segment(
            SEG_A[0], SEG_A[1], SEG_B[0], SEG_B[1], OWN_HALF * 2.0, Layer.F_CU, net=HV_NET
        )
        _, gate_x, gate_y = _shapely_gap_and_midpoint(
            _segment_copper_polygon(seg), box(9.0, 9.0, 11.0, 11.0)
        )
        probe = pad_waiver_probe_point(SEG_A, SEG_B, _model().pads[0], OWN_HALF)

        assert probe == pytest.approx((gate_x, gate_y), abs=1e-9)

    def test_degenerate_point_probe_stays_sane(self):
        """``a == b`` is the lattice-node probe form, as elsewhere in this file."""
        model = _model()
        probe = pad_waiver_probe_point((0.0, 10.0), (0.0, 10.0), model.pads[0], OWN_HALF)

        # Midway between the node's own copper edge (0.1, 10) and the pad (9, 10).
        assert probe == pytest.approx((4.55, 10.0))

    def test_copper_overlapping_the_pad_probes_the_pad_boundary(self):
        model = _model()
        probe = pad_waiver_probe_point((10.0, 4.0), (10.0, 16.0), model.pads[0], OWN_HALF)

        # The gate measures a zero gap for overlapping copper and reports a
        # point on the shared boundary; so does this.
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
        zone_min_y = (COPPER_PROBE_Y + PAD_CENTRE_PROBE_Y) / 2.0
        pairwise = _pairwise(zone_min_y)
        # Precondition: the two conventions genuinely straddle the zone edge.
        assert pairwise.exempt(10.0, PAD_CENTRE_PROBE_Y, HV_NET, LV_NET, 0)
        assert not pairwise.exempt(10.0, COPPER_PROBE_Y, HV_NET, LV_NET, 0)

        blocked = _model().pairwise_pad_blocked(SEG_A, SEG_B, 0, HV_NET, OWN_HALF, 0.0, pairwise)

        assert blocked, "search waived proximity the #4588 gate reports"

    def test_a_zone_covering_the_true_closest_gap_point_still_waives(self):
        """#4506 must keep working -- a rated footprint stays routable."""
        pairwise = _pairwise(zone_min_y=7.0)
        assert pairwise.exempt(10.0, COPPER_PROBE_Y, HV_NET, LV_NET, 0)

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


class TestReviewCounterexampleSearchVersusGate:
    """PR #5392 review, counterexample 2 — reproduced as a regression test.

    The first revision of this fix moved the probe onto the pad's copper
    boundary but still projected onto the moving trace's **centreline**,
    omitting its half-width.  On a 0.2 mm trace and a rectangular pad that is a
    0.05 mm bias -- enough to flip the verdict for a zone edge that falls
    between the two, which the review demonstrated with the zone below.

    This test drives BOTH production consumers on the same geometry: the search
    predicate and the actual copper gate (``find_pairwise_violations``), rather
    than comparing the helper against another analytic point derived the same
    way.
    """

    ZONE = (8.0, 7.0, 12.0, 7.52)

    def _pairwise_with_review_zone(self):
        from kicad_tools.router.lattice.pairwise import LatticePairwise

        return LatticePairwise(
            required_by_pair={(HV_NET, LV_NET): 3.2},
            zones=((*self.ZONE, frozenset({HV_NET, LV_NET}), None),),
            max_by_net={HV_NET: 3.2, LV_NET: 3.2},
        )

    def _gate_findings(self):
        from shapely.geometry import box

        from kicad_tools.router.layers import Layer
        from kicad_tools.router.pairwise_clearance import (
            AttachZone,
            PadGeometry,
            build_pairwise_clearance_table,
            find_pairwise_violations,
        )
        from kicad_tools.router.primitives import Route, Segment

        seg = Segment(10, 4, 10, 6, 0.2, Layer.F_CU, net=1, net_name="/HV")
        table = build_pairwise_clearance_table({"/HV": 300.0, "/LV": 0.0}, dru=0.2)
        return find_pairwise_violations(
            [Route(net=1, net_name="/HV", segments=[seg])],
            table,
            dru=0.2,
            attach_zones=(AttachZone(*self.ZONE, frozenset({"HV", "LV"})),),
            foreign_pads=(PadGeometry("/LV", frozenset({"F.Cu"}), box(9.0, 9.0, 11.0, 11.0)),),
        )

    def test_gate_reports_this_geometry(self):
        """Precondition: the copper gate genuinely finds 2.90 mm against 3.20 mm."""
        findings = self._gate_findings()

        assert len(findings) == 1, findings
        assert findings[0].actual_mm == pytest.approx(2.90, abs=1e-6)
        assert findings[0].required_mm == pytest.approx(3.20)

    def test_search_declines_what_the_gate_reports(self):
        """The regression: search and gate must reach the same verdict here."""
        blocked = _model().pairwise_pad_blocked(
            (10.0, 4.0), (10.0, 6.0), 0, HV_NET, OWN_HALF, 0.0, self._pairwise_with_review_zone()
        )

        assert blocked, "search waived proximity the copper gate reports"
        assert self._gate_findings(), "fixture no longer reproduces the gate finding"


@pytest.mark.parametrize(
    "shape,rotation,size",
    [("rect", 45, (2, 1)), ("circle", 0, (2, 2)), ("oval", 30, (2, 1)), ("rect", 0, (2, 1))],
)
def test_shaped_pad_waiver_matches_actual_gate_at_boundary(shape, rotation, size):
    from types import SimpleNamespace

    from kicad_tools.router.layers import Layer
    from kicad_tools.router.pairwise_clearance import (
        AttachZone,
        PadGeometry,
        _segment_copper_polygon,
        _shapely_gap_and_midpoint,
        build_pairwise_clearance_table,
        find_pairwise_violations,
    )
    from kicad_tools.router.primitives import Route, Segment
    from kicad_tools.validate.rules.clearance import _pad_polygon

    pad = Pad(10, 10, *size, 2, "/LV", shape=shape, rotation=rotation)
    model = LatticeObstacleModel(
        OctilinearLattice((0, 0, 20, 20), []), [pad], [(0,)], 1, AGENT_RADIUS
    )
    # Schema geometry is the audit oracle, independent of the search builder.
    polygon = _pad_polygon(
        SimpleNamespace(position=(10, 10), size=size, shape=shape, rotation=rotation),
        SimpleNamespace(position=(0, 0), rotation=0),
    )
    a, b = (7, 5), (8.5, 7)
    segment = Segment(*a, *b, 0.2, Layer.F_CU, net=1, net_name="/HV")
    gap, x, y = _shapely_gap_and_midpoint(_segment_copper_polygon(segment), polygon)
    assert gap < 3.2
    table = build_pairwise_clearance_table({"/HV": 300, "/LV": 0}, dru=0.2)
    for shift, expected_block in [(0, False), (0.04, True)]:
        zone = (x - 0.01, y - 0.01 + shift, x + 0.01, y + 0.01 + shift)
        pairwise = LatticePairwise(
            required_by_pair={(1, 2): 3.2},
            zones=((*zone, frozenset({1, 2}), None),),
            max_by_net={1: 3.2, 2: 3.2},
        )
        blocked = model.pairwise_pad_blocked(a, b, 0, 1, 0.1, 0, pairwise)
        findings = find_pairwise_violations(
            [Route(net=1, net_name="/HV", segments=[segment])],
            table,
            dru=0.2,
            attach_zones=(AttachZone(*zone, frozenset({"HV", "LV"})),),
            foreign_pads=(PadGeometry("/LV", frozenset({"F.Cu"}), polygon),),
        )
        assert blocked == expected_block == bool(findings)


@pytest.mark.parametrize("zone,expected_block", [((0, 0, 20, 20), False), ((8, 7, 12, 7.6), True)])
def test_missing_roundrect_radius_requires_full_midpoint_envelope(zone, expected_block):
    pad = Pad(10, 10, 2, 2, 2, "/LV", shape="roundrect")
    model = LatticeObstacleModel(
        OctilinearLattice((0, 0, 20, 20), []), [pad], [(0,)], 1, AGENT_RADIUS
    )
    pw = LatticePairwise(
        required_by_pair={(1, 2): 3.2},
        zones=((*zone, frozenset({1, 2}), None),),
        max_by_net={1: 3.2, 2: 3.2},
    )
    assert model.pairwise_pad_blocked(SEG_A, SEG_B, 0, 1, 0.1, 0, pw) == expected_block


def test_separate_zones_cannot_collectively_waive_uncertain_midpoint_envelope():
    pad = Pad(10, 10, 2, 2, 2, "/LV", shape="roundrect")
    model = LatticeObstacleModel(
        OctilinearLattice((0, 0, 20, 20), []), [pad], [(0,)], 1, AGENT_RADIUS
    )
    zones = ((0, 0, 10, 20, frozenset({1, 2}), None), (10.01, 0, 20, 20, frozenset({1, 2}), None))
    pw = LatticePairwise(required_by_pair={(1, 2): 3.2}, zones=zones, max_by_net={1: 3.2, 2: 3.2})
    assert model.pairwise_pad_blocked(SEG_A, SEG_B, 0, 1, 0.1, 0, pw)
