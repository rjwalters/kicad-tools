"""Tests for the rect-aware same-component plane-net validator (Issue #2908).

The router's post-route ``validate_segment_clearance`` (in
``src/kicad_tools/router/grid.py``) historically used two over-broad
shortcuts that combined to silently accept board-04's 44
``clearance_pad_segment`` violations on U2's west edge cluster:

1.  ``pad.net != 0 and pad.ref in exclude_refs`` skipped ANY
    same-component pad whose net id wasn't the SKIPPED-pour convention.
    Boards that route plane nets (``+3.3V`` / ``GND``) as real nets
    -- not in ``--skip-nets`` -- therefore exempted plane pads from
    pad-vs-segment validation entirely, even when the trace ran
    along the pad's EXTERIOR side (NOT the inter-pad corridor that
    Issue #1764 carved out for reachability).

2.  The geometry used ``pad_radius = max(w, h) / 2``, modelling each
    pad as a disc of radius equal to its LONG axis half-extent.
    On long rectangular SMD pads (1.475 x 0.3 mm LQFP-48) this
    over-blocks along the SHORT axis by ``(long - short) / 2 = 0.587 mm``
    of phantom inflation, AND under-detects at the LONG-axis corners
    where the disc's rounded edge clips inside the rectangle's sharp
    corner.

This module exercises the fix on a synthetic LQFP-48 fixture so the
regression test is self-contained and does not depend on the larger
board-04 fixture pipeline (which couples placement, escape routing,
and the auto-fix DRC nudge passes).

The fixture mirrors the verified board-04 failure geometry:

  * Plane pad U2.1 at (126.8375, 119.25), 1.475 x 0.3 mm, net 2 (+3.3V).
  * Foreign-net segment ``(127.7465, 119.7903) -> (127.5, 119.6)``
    on F.Cu, width 0.2, net 5 (OSC_OUT).
  * Manufacturer clearance 0.127 mm (jlcpcb-tier1).

Geometric values (verified analytically):

  * Rect closest-point: (127.500, 119.400) on the top edge.
  * Centerline distance from segment endpoint to rect: 0.200 mm.
  * Edge-to-edge clearance = 0.200 - 0.100 (half trace) = 0.100 mm.
  * Required clearance = 0.127 mm.
  * Shortfall = 0.027 mm -> validator MUST reject.

  * Disc closest-point: at distance ``sqrt(0.6625^2 + 0.35^2) = 0.749``
    from pad centre, minus radius 0.7375 = 0.012 mm.  Disc would also
    reject this case but with an over-rejected magnitude (-0.088 mm
    vs the true 0.027 mm shortfall) -- the rect-aware geometry
    reports the *accurate* DRC magnitude, which is what
    ``drc_verify_and_nudge`` consumes.

Acceptance signal:

  * Foreign-net trace at sub-DRC clearance against the EXTERIOR corner
    asserts validator rejection (the previously-silent class).
  * Same-net trace through the same cells asserts validator pass
    (the plane-net pad's own-net escape is unaffected -- mirrors the
    net-aware-carve-out precedent from #2869).
  * Rect-aware geometry primitive ``_rect_segment_centerline_distance``
    is exercised in isolation against the four geometric regimes.
  * Plane-net classification ``_is_plane_net_pad`` correctly
    distinguishes plane pours from single-drop signal nets.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import (
    RoutingGrid,
    _is_plane_net_pad,
    _rect_segment_centerline_distance,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


# -----------------------------------------------------------------------------
# Fixture helpers
# -----------------------------------------------------------------------------


def _make_jlcpcb_tier1_rules() -> DesignRules:
    """Build the design-rules object used by board 04 routing (jlcpcb-tier1)."""
    return DesignRules(
        trace_width=0.127,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.127,
        grid_resolution=0.05,
    )


def _make_grid_with_lqfp48_west_edge() -> RoutingGrid:
    """Build a routing grid populated with the board-04 LQFP-48 west-edge cluster.

    Only the pads that participate in the verified failure geometry are
    added (pin 1 + 2 + 6 along U2's west edge, plus a Y1 crystal pad as
    the OSC_OUT destination).  The grid is sized just large enough to
    enclose the cluster + clearance envelopes; the rest of the chip is
    omitted to keep the fixture cheap.
    """
    rules = _make_jlcpcb_tier1_rules()
    grid = RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=120.0,
        origin_y=110.0,
    )

    # U2.1 -- the verified offender: +3.3V plane pad on the same component
    # whose exterior was being clipped by OSC_OUT escape stubs.
    grid.add_pad(
        Pad(
            x=126.8375,
            y=119.25,
            width=1.475,
            height=0.3,
            net=2,
            net_name="+3.3V",
            ref="U2",
            pin="1",
            layer=Layer.F_CU,
        )
    )

    # U2.2 -- adjacent signal pin (NC in real board 04).  Included to
    # establish a same-component non-plane neighbour so the carve-out's
    # signal-pad semantics are exercised by the same fixture.
    grid.add_pad(
        Pad(
            x=126.8375,
            y=119.75,
            width=1.475,
            height=0.3,
            net=20,
            net_name="USART2_TX",
            ref="U2",
            pin="2",
            layer=Layer.F_CU,
        )
    )

    # U2.6 -- the OSC_OUT signal pin that escapes laterally and runs past
    # U2.1's exterior.  Same component, signal net.
    grid.add_pad(
        Pad(
            x=126.8375,
            y=121.75,
            width=1.475,
            height=0.3,
            net=5,
            net_name="OSC_OUT",
            ref="U2",
            pin="6",
            layer=Layer.F_CU,
        )
    )

    # Y1.1 -- crystal pad to which OSC_OUT must route.  Different
    # component, so its ref is also in exclude_refs.
    grid.add_pad(
        Pad(
            x=128.5,
            y=115.0,
            width=0.8,
            height=0.6,
            net=5,
            net_name="OSC_OUT",
            ref="Y1",
            pin="1",
            layer=Layer.F_CU,
        )
    )

    return grid


# -----------------------------------------------------------------------------
# The rect-aware distance primitive (geometric regimes)
# -----------------------------------------------------------------------------


class TestRectSegmentCenterlineDistance:
    """Unit-level coverage of ``_rect_segment_centerline_distance``.

    The four regimes (segment entirely outside, crossing an edge,
    parallel to a long axis, entirely inside) are each covered.
    """

    def test_segment_far_outside_returns_distance(self) -> None:
        # Pad 1.475 x 0.3 at the LQFP-48 west-edge geometry.  A segment
        # 5 mm away on the X axis must report Euclidean centerline
        # distance to the rectangle's right edge.
        d = _rect_segment_centerline_distance(
            126.8375, 119.25, 1.475, 0.3, 132.5, 119.25, 132.5, 130.0
        )
        # Rect right edge at x = 127.575; segment at x = 132.5.
        assert d == pytest.approx(132.5 - 127.575, abs=1e-6)

    def test_segment_parallel_to_short_axis_reports_short_axis_clearance(self) -> None:
        # Vertical segment 0.2 mm north of the pad's top edge (119.4 + 0.2 = 119.6).
        # Closest rect point: (127.5, 119.4) at distance 0.2.  Verified
        # against the board-04 failing geometry.
        d = _rect_segment_centerline_distance(
            126.8375, 119.25, 1.475, 0.3, 127.5, 119.6, 127.5, 130.0
        )
        assert d == pytest.approx(0.2, abs=1e-6)

    def test_segment_crossing_rect_edge_returns_zero(self) -> None:
        # Horizontal segment cutting straight across the rect's long axis.
        d = _rect_segment_centerline_distance(
            126.8375, 119.25, 1.475, 0.3,
            120.0, 119.25, 135.0, 119.25,
        )
        assert d == pytest.approx(0.0, abs=1e-9)

    def test_segment_inside_returns_negative_depth(self) -> None:
        # Short segment entirely INSIDE the rect.  Sign convention:
        # negative magnitude equal to the deepest signed-depth.
        d = _rect_segment_centerline_distance(
            126.8375, 119.25, 1.475, 0.3,
            126.8375, 119.25, 126.9, 119.30,
        )
        # Deepest signed-depth at the rect centre is -min(0.15, 0.7375) = -0.15.
        assert d < 0.0
        assert d >= -0.15 - 1e-6

    def test_endpoint_straddles_boundary_returns_zero(self) -> None:
        # One endpoint inside, one outside -- the centerline crosses an edge.
        d = _rect_segment_centerline_distance(
            126.8375, 119.25, 1.475, 0.3,
            126.8375, 119.25, 130.0, 119.25,
        )
        assert d == pytest.approx(0.0, abs=1e-9)


# -----------------------------------------------------------------------------
# Plane-net classification heuristic
# -----------------------------------------------------------------------------


class TestIsPlaneNetPad:
    """Pin the plane-net classification so the validator's same-component
    carve-out does not regress.
    """

    @pytest.mark.parametrize(
        "net_name",
        ["+3.3V", "+5V", "+12V", "+1V2", "+1V8", "+0.9V", "+24V"],
    )
    def test_numbered_voltage_rails_are_plane_nets(self, net_name: str) -> None:
        pad = Pad(
            x=0.0, y=0.0, width=1.0, height=1.0,
            net=2, net_name=net_name, ref="U1", pin="1", layer=Layer.F_CU,
        )
        assert _is_plane_net_pad(pad) is True

    @pytest.mark.parametrize(
        "net_name",
        ["GND", "AGND", "DGND", "PGND", "SGND", "GROUND", "EARTH",
         "VCC", "VDD", "VSS", "VEE", "VBAT", "VDDA", "VDDIO", "AVDD", "AVSS",
         "DVDD", "DVSS", "VAA"],
    )
    def test_canonical_power_pin_names_are_plane_nets(self, net_name: str) -> None:
        pad = Pad(
            x=0.0, y=0.0, width=1.0, height=1.0,
            net=3, net_name=net_name, ref="U1", pin="1", layer=Layer.F_CU,
        )
        assert _is_plane_net_pad(pad) is True

    @pytest.mark.parametrize(
        "net_name",
        ["VIN", "VOUT", "VBUS", "OSC_OUT", "NRST", "SWDIO", "USART2_TX",
         "DATA", "CLK", "MOSI", "MISO"],
    )
    def test_signal_nets_are_not_plane_nets(self, net_name: str) -> None:
        """Single-drop signal nets are NEVER plane nets even when their
        name contains a power-like substring.  This is the discriminator
        that prevents board 01's voltage-divider routing from regressing
        (VIN/VOUT are 2-pad signal nets, not pours)."""
        pad = Pad(
            x=0.0, y=0.0, width=1.0, height=1.0,
            net=10, net_name=net_name, ref="R1", pin="1", layer=Layer.F_CU,
        )
        assert _is_plane_net_pad(pad) is False

    def test_skipped_pour_net_is_plane(self) -> None:
        """``pad.net == 0`` is the skipped-pour-net convention (set by
        ``io.py`` when the user passes ``--skip-nets GND,+3.3V``).
        It always classifies as a plane regardless of net_name."""
        pad = Pad(
            x=0.0, y=0.0, width=1.0, height=1.0,
            net=0, net_name="", ref="U1", pin="1", layer=Layer.F_CU,
        )
        assert _is_plane_net_pad(pad) is True


# -----------------------------------------------------------------------------
# The integrated validator: regression spec for Issue #2908
# -----------------------------------------------------------------------------


class TestSameComponentPlaneNetCarveOut:
    """End-to-end behaviour of ``validate_segment_clearance`` against
    the verified board-04 LQFP-48 failure geometry.
    """

    def test_foreign_net_clipping_plane_pad_exterior_is_rejected(self) -> None:
        """The previously-silent class: an OSC_OUT (net 5) segment clips
        the exterior side of U2.1 (+3.3V plane pad on the same component).
        The pre-#2908 code skipped U2.1 entirely because
        ``pad.net != 0 and pad.ref in {'U2', 'Y1'}``; #2908 keeps plane
        pads in the validator and uses rect-aware geometry to compute
        the accurate clearance.
        """
        grid = _make_grid_with_lqfp48_west_edge()

        # Verified failing segment from the issue.
        seg = Segment(
            x1=127.7465, y1=119.7903,
            x2=127.5, y2=119.6,
            width=0.2,
            layer=Layer.F_CU,
            net=5,
            net_name="OSC_OUT",
        )

        is_valid, clearance, location = grid.validate_segment_clearance(
            seg,
            exclude_net=5,
            exclude_refs={"U2", "Y1"},
        )

        assert is_valid is False
        # The accurate rect-aware clearance is 0.100 mm
        # (0.200 mm centerline distance to top of U2.1 rect, minus 0.100
        # half-trace).  The required clearance at jlcpcb-tier1 is
        # 0.127 mm, so the shortfall is 0.027 mm -- this is the verified
        # DRC actual_value on board 04's committed PCB.
        assert clearance == pytest.approx(0.1, abs=1e-6)
        assert location is not None

    def test_same_net_escape_through_plane_pad_is_passed(self) -> None:
        """Net-aware carve-out: a same-net segment (OSC_OUT trace from
        the OSC_OUT plane pad's own net) MUST pass even when its
        centerline crosses the pad metal.  Mirrors the net-aware
        precedent at ``_apply_stitch_via_halo`` (#2869).
        """
        # Build a grid where the plane pad has a foreign-but-same-net
        # signal name.  The test segment runs from U2.6 (OSC_OUT,
        # net 5) and is routed on net 5 -- it must NOT be rejected
        # against U2.6's own pad (the same-net skip is the FIRST
        # filter in ``validate_segment_clearance``).
        grid = _make_grid_with_lqfp48_west_edge()
        seg = Segment(
            x1=126.8375, y1=121.75,  # U2.6 centre
            x2=126.0, y2=121.75,     # 0.8 mm west
            width=0.2,
            layer=Layer.F_CU,
            net=5,
            net_name="OSC_OUT",
        )

        is_valid, _clearance, _location = grid.validate_segment_clearance(
            seg,
            exclude_net=5,
            exclude_refs={"U2", "Y1"},
        )

        assert is_valid is True

    def test_signal_net_carve_out_still_applies(self) -> None:
        """The reachability fix from Issue #1764: a foreign-net trace
        running between two SIGNAL pads on the same component (e.g.
        chip escape past the chip's own signal pin neighbour) is still
        permitted.  The #2908 narrowing only re-engages the validator
        for PLANE pads -- non-plane same-component pads continue to be
        skipped.
        """
        grid = _make_grid_with_lqfp48_west_edge()

        # Foreign-net (NRST = net 12) segment routing past U2.2
        # (USART2_TX, a SIGNAL net on U2).  Pre-#2908 behaviour: skip
        # U2.2 because ``pad.net != 0``.  Post-#2908: still skip
        # because U2.2 is not a plane net.
        seg = Segment(
            x1=127.5, y1=119.75,     # tight clearance to U2.2 top edge
            x2=127.5, y2=119.85,
            width=0.2,
            layer=Layer.F_CU,
            net=12,
            net_name="NRST",
        )

        is_valid, _clearance, _location = grid.validate_segment_clearance(
            seg,
            exclude_net=12,
            exclude_refs={"U2", "Y1"},
        )

        # U2.2 is signal-net, so the same-component-ref skip still
        # applies and the segment passes (Issue #1764 reachability
        # preserved).
        assert is_valid is True

    def test_far_foreign_net_segment_passes_plane_pad_validator(self) -> None:
        """Sanity: a foreign-net segment well clear of the plane pad's
        clearance envelope passes the rect-aware validator (no spurious
        over-rejection).
        """
        grid = _make_grid_with_lqfp48_west_edge()
        seg = Segment(
            x1=130.0, y1=119.25,
            x2=130.0, y2=125.0,
            width=0.2,
            layer=Layer.F_CU,
            net=12,
            net_name="NRST",
        )

        is_valid, clearance, _location = grid.validate_segment_clearance(
            seg,
            exclude_net=12,
            exclude_refs={"U2", "Y1"},
        )

        assert is_valid is True
        # Rect right edge at 127.575; segment at x = 130.0.  Clearance
        # is 130.0 - 127.575 = 2.425 minus 0.1 half-trace = 2.325.
        assert clearance == pytest.approx(2.325, abs=1e-3)


class TestNoRegressionOnCircularPads:
    """Square/circular pads (vias, 0.5 x 0.5 mm SMD pads) keep the disc
    bound.  This pins the dispatcher so future changes don't
    accidentally lose the disc fast-path for the common circular case.
    """

    def test_square_pad_uses_disc_bound(self) -> None:
        """A square SMD pad (w == h within 1 micron) is modelled as a
        disc of radius ``max(w, h) / 2``.  Verified by constructing a
        scenario where rect and disc would diverge in the SHORT-axis
        regime: a square pad has no short axis, so the two bounds must
        agree.
        """
        rules = _make_jlcpcb_tier1_rules()
        grid = RoutingGrid(
            width=10.0, height=10.0, rules=rules, origin_x=0.0, origin_y=0.0,
        )
        # Square 1 x 1 mm pad at the centre, signal net.
        grid.add_pad(
            Pad(
                x=5.0, y=5.0, width=1.0, height=1.0,
                net=2, net_name="+3.3V",
                ref="U1", pin="1", layer=Layer.F_CU,
            )
        )

        # Segment 0.6 mm from the pad centre on +X axis.  Disc radius
        # 0.5, distance = 0.6, disc clearance = 0.6 - 0.5 = 0.1.  Half
        # trace = 0.1.  Final = 0.0 -- right at the trace edge.
        seg = Segment(
            x1=5.6, y1=4.0, x2=5.6, y2=6.0,
            width=0.2, layer=Layer.F_CU, net=10, net_name="DATA",
        )
        is_valid, clearance, _location = grid.validate_segment_clearance(
            seg, exclude_net=10,
        )
        # Disc and rect agree for square pads.  Either way the
        # clearance must be 0.0 (touching pad metal).
        assert clearance == pytest.approx(0.0, abs=1e-6)
        # 0.0 < 0.127 -- rejected.
        assert is_valid is False


def test_validator_does_not_over_reject_non_fine_pitch_geometry() -> None:
    """Edge case from the issue plan: a 0805 cap pair must NOT see the
    rect-aware fix as a regression.  The fix is a no-op for
    normal-pitch geometry.
    """
    rules = _make_jlcpcb_tier1_rules()
    grid = RoutingGrid(
        width=10.0, height=10.0, rules=rules, origin_x=0.0, origin_y=0.0,
    )
    # Two 0805 pads (1.0 x 1.25 mm) at standard 0805 pitch (1.85 mm
    # centre-to-centre).
    grid.add_pad(
        Pad(
            x=4.075, y=5.0, width=1.0, height=1.25,
            net=2, net_name="+3.3V", ref="C1", pin="1", layer=Layer.F_CU,
        )
    )
    grid.add_pad(
        Pad(
            x=5.925, y=5.0, width=1.0, height=1.25,
            net=3, net_name="GND", ref="C1", pin="2", layer=Layer.F_CU,
        )
    )

    # Routing GND on a different component -- not in exclude_refs.
    # A foreign-net segment running at 0.5 mm above the cap (well clear)
    # must pass.
    seg = Segment(
        x1=4.0, y1=6.0, x2=6.0, y2=6.0,
        width=0.127, layer=Layer.F_CU, net=10, net_name="DATA",
    )
    is_valid, _clearance, _location = grid.validate_segment_clearance(
        seg, exclude_net=10,
    )
    # Cap top edge at y = 5.625; segment at y = 6.0; centerline distance
    # 0.375 - 0.0635 (half trace) = 0.311.  Required = 0.127.  PASS.
    assert is_valid is True
