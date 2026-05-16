"""Issue #2944: world-coord clearance predicate on EscapeRouter via placement.

Verifies that ``EscapeRouter._can_place_via`` (and the
``_via_clears_other_pads`` helper used by ``_try_in_pad_escape``)
rejects via candidates that sit within
``via_radius + pad_radius + clearance`` of a foreign-net pad and
within ``via_radius + seg.width/2 + clearance`` of a foreign-net trace.

The defect this guards against is the board-04 OSC_OUT in-pad rescue:
a 0.6mm via placed dead-centre on an LQFP 0.5mm-pitch pin sat 0.05mm
from the adjacent foreign-net pin pads (OSC_IN / NRST) and produced
DRC errors at the jlcpcb-tier1 0.127mm clearance rule.  See
``router/via_clearance.py`` and ``router/escape.py:_can_place_via``
for the fix.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_clearance import point_clear_of_copper


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.two_layer(),
    )


class TestPointClearOfCopper:
    """The shared world-coord predicate, tested in isolation."""

    def test_passes_when_nothing_nearby(self):
        assert point_clear_of_copper(
            x=10.0,
            y=10.0,
            via_size=0.6,
            clearance=0.15,
        )

    def test_rejects_foreign_pad_within_clearance(self):
        # via center 0.4mm from pad center; pad radius 0.1mm; via radius 0.3mm
        # required = 0.3 + 0.1 + 0.15 = 0.55; actual = 0.4 -> reject
        assert not point_clear_of_copper(
            x=10.0,
            y=10.0,
            via_size=0.6,
            clearance=0.15,
            other_net_pads=[(10.4, 10.0, 0.1, 99)],
        )

    def test_admits_foreign_pad_outside_clearance(self):
        # 1.0mm away vs 0.55mm required
        assert point_clear_of_copper(
            x=10.0,
            y=10.0,
            via_size=0.6,
            clearance=0.15,
            other_net_pads=[(11.0, 10.0, 0.1, 99)],
        )

    def test_rejects_foreign_segment_within_clearance(self):
        # Horizontal segment at y=10.4 (width 0.2 -> half = 0.1); via
        # radius 0.3, clearance 0.15.
        # required = 0.3 + 0.1 + 0.15 = 0.55.  Distance from via center
        # to segment centerline = 0.4 < 0.55 -> reject.
        seg = Segment(x1=9.0, y1=10.4, x2=11.0, y2=10.4, width=0.2, layer=Layer.F_CU)

        class _Adapter:
            def __init__(self, s: Segment):
                self.start_x = s.x1
                self.start_y = s.y1
                self.end_x = s.x2
                self.end_y = s.y2
                self.width = s.width

        assert not point_clear_of_copper(
            x=10.0,
            y=10.0,
            via_size=0.6,
            clearance=0.15,
            other_net_tracks=[_Adapter(seg)],
        )

    def test_rejects_same_net_via_at_stack_distance(self):
        # via_size=0.6, clearance=0.15.  Two same-net vias touching each
        # other (distance 0.5mm) -> reject (< via_size + clearance = 0.75).
        assert not point_clear_of_copper(
            x=10.0,
            y=10.0,
            via_size=0.6,
            clearance=0.15,
            same_net_vias=[(10.5, 10.0)],
        )


class TestCanPlaceViaWorldCoord:
    """``EscapeRouter._can_place_via`` exercising the Issue #2944 path."""

    def test_grid_only_passes_at_clear_position(self):
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        # Mid-grid with no obstacle markings: must pass.
        assert router._can_place_via(10.0, 10.0)

    def test_rejects_via_within_foreign_pad_clearance(self):
        """Pre-#2944 this returned True because the grid cells around
        the via center weren't marked as pad obstacles.  With the new
        world-coord branch active, the check correctly rejects a via
        whose envelope overlaps a foreign-net pad's clearance zone.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        # Place a foreign-net pad at (10.4, 10.0) with radius 0.1mm.
        # Via at (10.0, 10.0) with diameter 0.6 -> radius 0.3.
        # Required = 0.3 + 0.1 + 0.15 = 0.55; distance = 0.4 -> reject.
        foreign = Pad(
            x=10.4, y=10.0, width=0.2, height=0.2,
            net=99, net_name="OTHER", layer=Layer.F_CU,
        )
        own_net = 5
        assert not router._can_place_via(
            x=10.0,
            y=10.0,
            net=own_net,
            foreign_pads=[foreign],
            clearance=0.15,
            via_diameter=0.6,
        )

    def test_admits_via_when_only_same_net_pad_nearby(self):
        """Same-net pads must NOT trigger the world-coord rejection;
        in-pad vias on the parent pad are valid.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        same_net_pad = Pad(
            x=10.0, y=10.0, width=1.5, height=0.3,
            net=5, net_name="OSC_OUT", layer=Layer.F_CU,
        )
        assert router._can_place_via(
            x=10.0,
            y=10.0,
            net=5,
            foreign_pads=[same_net_pad],
            clearance=0.15,
            via_diameter=0.6,
        )

    def test_rejects_via_within_foreign_segment_clearance(self):
        """Via candidate within trace-clearance distance of a
        foreign-net trace must be rejected (the BOOT0-overlapping case
        in the curator analysis was exactly this).
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        # Horizontal foreign-net trace 0.4mm above the via.  Required
        # = 0.3 + 0.1 + 0.15 = 0.55mm.
        foreign_seg = Segment(
            x1=9.0, y1=10.4, x2=11.0, y2=10.4,
            width=0.2, layer=Layer.F_CU, net=99, net_name="OTHER",
        )
        assert not router._can_place_via(
            x=10.0,
            y=10.0,
            net=5,
            foreign_tracks=[foreign_seg],
            clearance=0.15,
            via_diameter=0.6,
        )


class TestInPadEscapeClearance:
    """``_via_clears_other_pads`` exercises the in-pad rescue path."""

    def test_in_pad_via_rejects_neighbor_pad_overlap(self):
        """Reproduces the board-04 LQFP-48 0.5mm-pitch OSC_OUT case:
        in-pad via on pad 6 collides with pads 5 and 7 along Y.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        # LQFP-48 west edge: pads at x=0, y stepping by 0.5mm.
        pad5 = Pad(
            x=0.0, y=-0.5, width=1.5, height=0.3,
            net=4, net_name="OSC_IN", layer=Layer.F_CU,
        )
        pad6 = Pad(
            x=0.0, y=0.0, width=1.5, height=0.3,
            net=5, net_name="OSC_OUT", layer=Layer.F_CU,
        )
        pad7 = Pad(
            x=0.0, y=0.5, width=1.5, height=0.3,
            net=9, net_name="NRST", layer=Layer.F_CU,
        )

        # Via dead-centre on pad6 (OSC_OUT).  via_diameter=0.6 ->
        # radius=0.3.  Distance from via center to pad5/pad7 edge in Y
        # = 0.5 - 0.15 = 0.35.  Required = 0.3 + 0.15 = 0.45.  Reject.
        assert not router._via_clears_other_pads(
            x=pad6.x,
            y=pad6.y,
            via_diameter=0.6,
            clearance=0.15,
            other_pads=[pad5, pad7],
            same_net=pad6.net,
        )

    def test_in_pad_via_admits_safe_geometry(self):
        """When the neighbor pad is far enough away the in-pad via
        candidate is admitted.  Larger pitch (1.0mm) -> via center is
        0.85mm from neighbor edge, required = 0.45mm, so admit.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        pad_a = Pad(
            x=0.0, y=-1.0, width=1.5, height=0.3,
            net=4, net_name="A", layer=Layer.F_CU,
        )
        pad_b = Pad(
            x=0.0, y=0.0, width=1.5, height=0.3,
            net=5, net_name="B", layer=Layer.F_CU,
        )
        pad_c = Pad(
            x=0.0, y=1.0, width=1.5, height=0.3,
            net=9, net_name="C", layer=Layer.F_CU,
        )

        assert router._via_clears_other_pads(
            x=pad_b.x,
            y=pad_b.y,
            via_diameter=0.6,
            clearance=0.15,
            other_pads=[pad_a, pad_c],
            same_net=pad_b.net,
        )

    def test_in_pad_via_skips_same_net_pad(self):
        """Same-net adjacent pads must not trigger rejection -- they
        could legitimately be the pad we are placing the via on.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        same_net_pad = Pad(
            x=0.0, y=0.3, width=1.5, height=0.3,
            net=5, net_name="OSC_OUT", layer=Layer.F_CU,
        )
        assert router._via_clears_other_pads(
            x=0.0,
            y=0.0,
            via_diameter=0.6,
            clearance=0.15,
            other_pads=[same_net_pad],
            same_net=5,
        )

    def test_in_pad_via_rejects_via_center_inside_foreign_pad(self):
        """A via center inside a foreign-net pad rectangle is an
        immediate clearance violation.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        foreign_pad = Pad(
            x=0.0, y=0.0, width=1.5, height=0.3,
            net=99, net_name="OTHER", layer=Layer.F_CU,
        )
        assert not router._via_clears_other_pads(
            x=0.0,
            y=0.0,
            via_diameter=0.6,
            clearance=0.15,
            other_pads=[foreign_pad],
            same_net=5,
        )


class TestRectAwareForeignPadClearance:
    """Issue #2951: ``point_clear_of_copper`` and
    ``EscapeRouter._can_place_via`` must use rect-distance for oblong
    fine-pitch foreign pads (not the disc-bound ``max(w,h)/2``).

    Modelled on the board-04 LQFP-48 OSC_OUT cluster: 0.3 x 1.4mm pads
    at 0.5mm pitch.  The disc-bound treats a neighbor as a 1.4mm disc,
    making the required centre-to-centre distance
    ``0.3 (via_r) + 0.7 (pad_r) + 0.15 (clear) = 1.15mm`` -- but the
    pitch is only 0.5mm so every nudged via candidate is rejected.
    With rect-distance the same geometry only requires
    ``0.3 (via_r) + 0.15 (clear) = 0.45mm`` clearance to the pad EDGE,
    which the long-axis nudge can satisfy.
    """

    def test_disc_rejects_dead_centre_via(self):
        """Sanity: dead-centre via on the parent pad with the disc-bound
        4-tuple is rejected (this is the production state pre-#2951).
        """
        # Neighbor: 0.3 x 1.4mm pad at (0.0, 0.5), pitch 0.5mm along Y.
        # Disc bound: radius = 0.7mm.
        # Via at (0, 0), diameter 0.6.
        # Required = 0.3 + 0.7 + 0.15 = 1.15mm; actual = 0.5 -> reject.
        assert not point_clear_of_copper(
            x=0.0,
            y=0.0,
            via_size=0.6,
            clearance=0.15,
            other_net_pads=[(0.0, 0.5, 0.7, 99)],
        )

    def test_rect_admits_via_disc_would_reject(self):
        """Core Issue #2951 diagnostic: a via positioned off the SHORT
        axis of an oblong foreign pad clears rect-distance but the
        disc-bound rejects.

        Geometry:
        * Foreign pad 0.3 (X) x 1.4 (Y) centered at (0, 0) -- long axis
          is Y, short axis is X.  Pad extends x in [-0.15, +0.15],
          y in [-0.7, +0.7].
        * Via diameter 0.6, clearance 0.15.
        * Via center at (0.6, 0.5).

        Rect-distance: outside_x = 0.6 - 0.15 = 0.45; outside_y = 0.5 -
        0.7 = -0.2 -> 0.  rect_dist = 0.45.  Required = via_radius +
        clearance = 0.45.  Admits at the boundary.

        Disc bound (radius = max(0.3, 1.4)/2 = 0.7): centre-to-centre =
        sqrt(0.36 + 0.25) = 0.781.  Required = 0.3 + 0.7 + 0.15 = 1.15.
        Rejects.

        This is the LQFP fine-pitch escape pattern: PR #2950's in-pad
        nudge produces candidate via offsets adjacent to the parent pad
        (off the short axis) -- the disc-bound rejects every such
        candidate, but rect-distance correctly admits clearly-clear
        positions.
        """
        # Rect-aware 5-tuple: passes.
        assert point_clear_of_copper(
            x=0.6,
            y=0.5,
            via_size=0.6,
            clearance=0.15,
            other_net_pads=[(0.0, 0.0, 0.3, 1.4, 99)],
        )

        # Same geometry with the legacy disc-bound 4-tuple: rejects.
        # max(0.3, 1.4)/2 = 0.7
        assert not point_clear_of_copper(
            x=0.6,
            y=0.5,
            via_size=0.6,
            clearance=0.15,
            other_net_pads=[(0.0, 0.0, 0.7, 99)],
        )

    def test_can_place_via_uses_rect_aware_for_foreign_pads(self):
        """``EscapeRouter._can_place_via`` end-to-end with a 0.3 x 1.4mm
        oblong foreign pad: the via center off the short axis must be
        admitted (rect-aware) where the legacy disc-bound rejected it.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        # Same dichotomy geometry as above, but driven through the
        # ``_can_place_via`` entry point so the Pad -> 5-tuple
        # conversion in escape.py is exercised.
        foreign = Pad(
            x=10.0, y=10.0, width=0.3, height=1.4,
            net=99, net_name="FOREIGN", layer=Layer.F_CU,
        )
        # Via at (10.6, 10.5) -- 0.45mm rect-distance to pad short edge,
        # exactly at threshold (0.3 + 0.15 = 0.45).  Rect admits.  Disc
        # would have rejected (cent-to-cent 0.78 < 1.15 threshold).
        assert router._can_place_via(
            x=10.6,
            y=10.5,
            net=5,
            foreign_pads=[foreign],
            clearance=0.15,
            via_diameter=0.6,
        )

    def test_can_place_via_still_rejects_close_foreign_pad(self):
        """Rect-aware does NOT make the predicate over-permissive: a
        via clearly inside a foreign pad's clearance envelope is still
        rejected.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        # Same 0.3 x 1.4mm foreign pad; via this time at (10.0, 10.5)
        # i.e. centred on the pad short axis, 0.5mm from pad short
        # edge along Y.  Rect: outside_x=0, outside_y=-0.2 (inside).
        # Via center is inside the pad rectangle -> immediate rejection.
        foreign = Pad(
            x=10.0, y=10.0, width=0.3, height=1.4,
            net=99, net_name="FOREIGN", layer=Layer.F_CU,
        )
        assert not router._can_place_via(
            x=10.0,
            y=10.5,
            net=5,
            foreign_pads=[foreign],
            clearance=0.15,
            via_diameter=0.6,
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
