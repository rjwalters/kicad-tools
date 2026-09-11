"""Tests for segment-to-pad clearance with rectangular pads (issue #2781).

The ``ClearanceRule`` in ``kicad_tools.validate.rules.clearance`` checks
trace-to-pad clearance during ``kct check``. Prior to this fix
(commit 6ec0344c had fixed the analogous pad-to-pad case but skipped
segment-to-pad), ``_segment_circle_clearance`` modelled every pad --
including thin rectangular SMD pads (USB-C, QFP, QFN, DDR, DFN) -- as a
disc of radius ``max(width, height) / 2``.  That inflated the obstacle by
``(max - min) / 2`` along the pad's short axis, so a 0.5 x 1.2 mm USB-C
pad became a 1.2 mm-diameter disc with 0.35 mm of phantom copper hanging
off each side.  When PR #2762 / PR #2753 unmasked the
``clearance_pad_segment`` rule on routed PCBs, every routed board with
rectangular pads near the routing channels grew dozens of "marginal"
violations -- 19 false positives on board 03 (USB-C), 2 on board 04
(QFP-32), 67 of the 105 ``clearance_pad_segment`` reports on board 05,
and so on.

The fix uses true axis-aligned-rectangle-to-segment geometry for
rectangular pads (vias and square pads keep the disc model).  The
function correctly:

1.  **Reduces false positives** along the pad's short axis -- a trace
    that clears the rectangle by 0.4 mm is no longer reported as 0.05 mm
    away because the disc would have extended 0.35 mm past the rect's
    short edge.

2.  **Surfaces previously-hidden corner near-misses** along the pad's
    short axis -- the disc bound has rounded corners that under-report
    the obstacle at the rectangle's sharp corner, so traces that
    grazed a corner were reported as having more clearance than the
    sharp rectangle actually allows.  These cases are now correctly
    flagged.

3.  **Preserves negative-clearance reporting** for traces that route
    through pad metal -- board 05's TO-263 GND-tab violations remain
    flagged, with a depth indicator that reflects how deep the trace
    runs inside the rectangle.
"""

from __future__ import annotations

import pytest

from kicad_tools.schema.pcb import Footprint, Pad
from kicad_tools.validate.rules.clearance import (
    CopperElement,
    _rect_segment_centerline_distance,
    _segment_circle_clearance,
)


def _make_pad(cx: float, cy: float, w: float, h: float, *, net: int = 99) -> CopperElement:
    """Build a ``CopperElement`` for a rectangular SMD pad."""
    return CopperElement(
        element_type="pad",
        layer="*",
        net_number=net,
        geometry=(cx, cy, w, h),
        reference="UTEST-1",
        net_name="NET_PAD",
    )


def _make_via(cx: float, cy: float, diameter: float, *, net: int = 99) -> CopperElement:
    """Build a ``CopperElement`` for a circular via."""
    return CopperElement(
        element_type="via",
        layer="*",
        net_number=net,
        geometry=(cx, cy, diameter, diameter),
        reference="VIA-1",
        net_name="NET_VIA",
    )


def _make_seg(
    x1: float, y1: float, x2: float, y2: float, width: float, *, net: int = 42
) -> CopperElement:
    """Build a ``CopperElement`` for a routed trace segment."""
    return CopperElement(
        element_type="segment",
        layer="F.Cu",
        net_number=net,
        geometry=(x1, y1, x2, y2, width),
        reference="Trace-test",
        net_name="NET_TRACE",
    )


# ---------------------------------------------------------------------------
# Direct unit tests on the rect-segment distance primitive
# ---------------------------------------------------------------------------


class TestRectSegmentCenterlineDistance:
    """Exercises ``_rect_segment_centerline_distance`` in isolation.

    These tests pin the sign convention and the four geometric regimes
    (entirely outside, crossing an edge, entirely inside, parallel to
    an edge).
    """

    def test_segment_far_outside_returns_distance(self) -> None:
        # Pad 0.5 x 1.2 at origin; segment 5 mm to the left.
        d = _rect_segment_centerline_distance(0.0, 0.0, 0.5, 1.2, -5.0, 0.0, -5.0, 5.0)
        # Closest point on rect to (-5, 0) is (-0.25, 0); distance 4.75.
        assert d == pytest.approx(4.75, abs=1e-6)

    def test_segment_parallel_to_long_axis_short_side_clearance(self) -> None:
        # USB-style 0.5 x 1.2 pad at origin; a vertical trace running 1 mm to
        # the right.  Distance to the rect's right edge is 0.75 mm.  The
        # old disc bound (radius 0.6) reported 0.4 mm instead -- the bug.
        d = _rect_segment_centerline_distance(0.0, 0.0, 0.5, 1.2, 1.0, -5.0, 1.0, 5.0)
        assert d == pytest.approx(0.75, abs=1e-6)

    def test_segment_crossing_rect_edge_returns_zero(self) -> None:
        # Segment from (-1, 0) to (1, 0) cuts straight through a 1 x 1 pad.
        d = _rect_segment_centerline_distance(0.0, 0.0, 1.0, 1.0, -1.0, 0.0, 1.0, 0.0)
        assert d == pytest.approx(0.0, abs=1e-9)

    def test_segment_entirely_inside_returns_negative_depth(self) -> None:
        # 4 x 4 pad at origin; segment from origin to (0.5, 0.5).
        # The deepest point on the segment is at (0, 0), 2 mm from the
        # nearest edge.  Sign convention: negative when inside.
        d = _rect_segment_centerline_distance(0.0, 0.0, 4.0, 4.0, 0.0, 0.0, 0.5, 0.5)
        assert d == pytest.approx(-2.0, abs=1e-6)

    def test_segment_crossing_long_pad_reports_deepest_penetration(self) -> None:
        # 3 x 8 pad (board 05 TO-263 GND tab geometry).  A horizontal trace
        # cutting straight through the pad on its short axis should be
        # flagged with a depth roughly equal to half the short axis.
        d = _rect_segment_centerline_distance(115.4, 122.0, 3.0, 8.0, 114.0, 122.0, 116.0, 122.0)
        # Deepest point is at the rect's X centre, 1.5 mm from either side.
        # The sampled implementation lands within ~0.05 mm of the analytic
        # optimum on a 2 mm segment with 32 subdivisions; assert the
        # violation magnitude is at least 1.45 mm (clearly negative -- the
        # exact reported depth need only be a reasonable lower bound).
        assert d <= -1.45
        assert d >= -1.5  # don't over-report

    def test_endpoints_straddle_boundary_returns_zero(self) -> None:
        # Segment with one endpoint inside, one outside -- crosses an edge.
        d = _rect_segment_centerline_distance(0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 5.0, 0.0)
        assert d == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Regression: USB-style narrow rect pad must not over-fire
# ---------------------------------------------------------------------------


class TestUsbPadDoesNotOverFire:
    """Pin the issue #2781 over-emit fix on the USB-C geometry that
    triggered 19 false-positive ``clearance_pad_segment`` violations on
    board 03 (commit ``91a774fc``).
    """

    def test_trace_parallel_to_long_axis_clears_at_pad_pitch(self) -> None:
        # USB-C pad pitch is 0.8 mm; pad is 0.5 wide x 1.2 tall.  A trace on
        # the neighbouring pin (0.8 mm away on the pad's short axis,
        # parallel to the long axis) clears the rectangle by ``0.8 - 0.25
        # = 0.55 mm`` along the X axis.  With a 0.2 mm trace, edge-to-edge
        # clearance is ``0.55 - 0.10 = 0.45 mm`` -- well above the JLCPCB
        # 0.127 mm minimum, but the old disc bound reported ``0.8 - 0.6 -
        # 0.1 = 0.10 mm``, which falsely failed DRC.
        pad = _make_pad(0.0, 0.0, 0.5, 1.2)
        seg = _make_seg(0.8, -5.0, 0.8, 5.0, 0.2)

        clearance, loc_x, loc_y = _segment_circle_clearance(seg, pad)

        assert clearance == pytest.approx(0.45, abs=1e-6)
        assert loc_x == pytest.approx(0.0, abs=1e-9)
        assert loc_y == pytest.approx(0.0, abs=1e-9)

    def test_trace_through_pad_centre_still_flagged_negative(self) -> None:
        # The fix must NOT silently pass traces that genuinely route
        # through pad metal.  A horizontal trace along the pad's centre
        # crosses the rectangle and must report negative clearance.
        pad = _make_pad(0.0, 0.0, 0.5, 1.2)
        seg = _make_seg(-5.0, 0.0, 5.0, 0.0, 0.2)

        clearance, _, _ = _segment_circle_clearance(seg, pad)

        # Segment centerline is exactly on the long axis -- it crosses
        # the rect's left and right edges, so centerline distance is 0
        # and the only contribution to "clearance" is the negative half
        # trace width.
        assert clearance == pytest.approx(-0.1, abs=1e-6)


# ---------------------------------------------------------------------------
# Regression: PR #2767 dormant signal must remain caught
# ---------------------------------------------------------------------------


class TestPr2767BugStillCaught:
    """PR #2767 closed a dormant ``clearance_pad_segment`` signal: the
    in-router DRC was silently skipping skipped-pour-net pads (``pad.net
    == 0`` with a non-empty ``net_name``), so traces that chamfered
    diagonally through GND BGA pads went undetected.  The fix in #2767
    is in ``router/io.py`` (``validate_routes``), but the equivalent
    dormant signal must also hold in ``kct check`` -- a trace that
    runs through cross-net pad metal MUST be flagged regardless of how
    the obstacle is geometrically modelled.

    These tests run the geometric primitive directly so they hold under
    any future refactor of ``validate_routes`` (the post-route DRC).
    """

    def test_chamfer_through_gnd_bga_pad_reports_negative_clearance(self) -> None:
        # Synthetic 1 mm x 1 mm BGA pad on net GND; diagonal trace
        # chamfering straight through the pad centre on a signal net.
        # Whether the pad is modelled as a disc or a rectangle, a trace
        # that cuts through the centre must be flagged.
        pad = _make_pad(0.0, 0.0, 1.0, 1.0, net=1)  # square -- treated as disc
        seg = _make_seg(-2.0, -2.0, 2.0, 2.0, 0.2, net=2)

        clearance, _, _ = _segment_circle_clearance(seg, pad)

        # Square pads are still modelled as discs (``is_circular`` path),
        # so radius = 0.5 and the segment passes through the centre:
        # clearance = 0 (center_dist) - 0.5 (radius) - 0.1 (half_width) = -0.6.
        assert clearance == pytest.approx(-0.6, abs=1e-6)

    def test_rect_gnd_pad_with_trace_inside_reports_negative_depth(self) -> None:
        # Board 05's TO-263 GND tab is a 3 x 8 mm SMD rect.  Traces
        # routed through the tab metal must remain visible to DRC after
        # the fix -- this is the "real bugs the original PR was trying
        # to surface" half of the regression coverage requested in
        # issue #2781.
        pad = _make_pad(0.0, 0.0, 3.0, 8.0, net=1)
        seg = _make_seg(-1.0, 0.0, 1.0, 0.0, 0.2, net=2)

        clearance, _, _ = _segment_circle_clearance(seg, pad)

        # Trace crosses the rectangle along its short axis through the
        # centre.  The deepest point sits at the rect centre at depth
        # ``-min(w, h) / 2 = -1.5 mm``; subtract half trace width =>
        # roughly -1.6 mm.  Allow a small tolerance for the sampled
        # interior-depth search.
        assert clearance <= -1.4
        assert clearance >= -1.6


# ---------------------------------------------------------------------------
# Via and square pad paths must keep the disc behaviour
# ---------------------------------------------------------------------------


class TestViaAndSquarePadStillUseDisc:
    """Vias are intrinsically circular, and square pads are
    well-modelled as discs.  These elements MUST take the disc branch
    so the fix doesn't perturb the existing well-tuned via and BGA
    clearance budgets.
    """

    def test_via_uses_disc_geometry(self) -> None:
        # 0.6 mm via; trace 0.5 mm to the right, 0.2 mm wide.
        via = _make_via(0.0, 0.0, 0.6)
        seg = _make_seg(0.5, -5.0, 0.5, 5.0, 0.2)

        clearance, _, _ = _segment_circle_clearance(seg, via)

        # Disc radius = 0.3; clearance = 0.5 - 0.3 - 0.1 = 0.1.
        assert clearance == pytest.approx(0.1, abs=1e-9)

    def test_square_pad_uses_disc_geometry(self) -> None:
        # 1 mm square pad: w == h within the 0.001 tolerance => disc.
        pad = _make_pad(0.0, 0.0, 1.0, 1.0)
        seg = _make_seg(0.7, -5.0, 0.7, 5.0, 0.2)

        clearance, _, _ = _segment_circle_clearance(seg, pad)

        # Disc radius = 0.5; clearance = 0.7 - 0.5 - 0.1 = 0.1.
        assert clearance == pytest.approx(0.1, abs=1e-9)


# ---------------------------------------------------------------------------
# Symmetry: pad-first and segment-first argument orders must agree
# ---------------------------------------------------------------------------


class TestArgumentOrderSymmetry:
    """The clearance dispatcher in ``ClearanceRule._check_layer`` may call
    ``_segment_circle_clearance(pad, seg)`` or ``_segment_circle_clearance(
    seg, pad)`` depending on which element appears first in the layer
    iteration.  The reported clearance must be invariant under swap.
    """

    def test_rect_pad_segment_swap_invariant(self) -> None:
        pad = _make_pad(2.0, 1.0, 0.5, 1.2)
        seg = _make_seg(2.8, 0.0, 2.8, 2.0, 0.2)

        from kicad_tools.validate.rules.clearance import _calculate_clearance

        c1, x1, y1 = _calculate_clearance(seg, pad)
        c2, x2, y2 = _calculate_clearance(pad, seg)

        assert c1 == pytest.approx(c2, abs=1e-9)
        assert (x1, y1) == pytest.approx((x2, y2), abs=1e-9)


# ---------------------------------------------------------------------------
# Regression: rotated roundrect pads must use the true polygon, not the AABB
# (issue #4985 -- the segment-vs-pad analogue of #3826's pad-vs-pad fix)
# ---------------------------------------------------------------------------


def _c19_pad_and_footprint() -> tuple[Pad, Footprint]:
    """Reproduce the exact C19 fixture from issue #4985 (chorus-test-revA).

    Footprint at (147.086449, 97.046534), rotation -90 deg.  Pad 2 is a
    1.0 x 1.45 mm ``roundrect`` (``roundrect_rratio`` 0.25) with local
    position (0.95, 0) and absolute pad angle 270 deg (KiCad stores
    ``pad.rotation`` in the absolute board frame -- it already folds in
    the footprint's rotation, issue #3902).
    """
    footprint = Footprint(
        name="C19",
        reference="C19",
        value="",
        position=(147.086449, 97.046534),
        rotation=-90.0,
        layer="F.Cu",
    )
    pad = Pad(
        number="2",
        type="smd",
        shape="roundrect",
        position=(0.95, 0.0),
        size=(1.0, 1.45),
        layers=["F.Cu"],
        net_number=5,
        net_name="GNDA",
        rotation=270.0,
        roundrect_rratio=0.25,
    )
    return pad, footprint


class TestRoundrectPadSegmentClearance:
    """The C19 roundrect fixture from issue #4985.

    Prior to the fix, ``_segment_circle_clearance`` routed every
    non-circular pad through ``_rect_segment_centerline_distance``'s
    AABB rectangle -- including ``roundrect`` pads, whose true copper
    outline cuts back the corners.  For this fixture the AABB
    over-approximation reported ~0.0812 mm of clearance (a false
    ``clearance_pad_segment`` violation against the board's 0.1016 mm
    floor); the true rounded-corner polygon (matching
    ``pcbnew.PAD.GetEffectivePolygon``) clears by ~0.1846 mm.
    """

    def test_c19_fixture_does_not_over_fire(self) -> None:
        pad, footprint = _c19_pad_and_footprint()
        pad_elem = CopperElement.from_pad(pad, footprint)

        # Foreign F.Cu trace on Net-(D3-A), width 0.11 mm, from the issue.
        seg = CopperElement(
            element_type="segment",
            layer="F.Cu",
            net_number=7,
            geometry=(147.9, 98.6, 154.4, 98.6, 0.11),
            reference="Trace-test",
            net_name="Net-(D3-A)",
        )

        clearance, loc_x, loc_y = _segment_circle_clearance(seg, pad_elem)

        # True rounded geometry clears comfortably above the 0.1016 mm
        # board floor -- close to the verified GetEffectivePolygon value
        # of ~0.1846 mm, not the AABB's false ~0.0812 mm.
        assert clearance == pytest.approx(0.1846, abs=5e-3)
        assert clearance > 0.1016
        # Location is still reported at the pad center.
        assert (loc_x, loc_y) == pytest.approx((147.086449, 97.996534), abs=1e-6)

    def test_aabb_path_would_have_reported_the_false_positive(self) -> None:
        """Pin the pre-fix AABB value so a future refactor can't silently
        regress back to it without this test noticing."""
        pad, footprint = _c19_pad_and_footprint()
        pad_elem = CopperElement.from_pad(pad, footprint)
        cx, cy, w, h = pad_elem.geometry  # AABB dimensions (swapped for 270 deg)

        center_dist = _rect_segment_centerline_distance(cx, cy, w, h, 147.9, 98.6, 154.4, 98.6)
        aabb_clearance = center_dist - 0.11 / 2

        assert aabb_clearance == pytest.approx(0.0812, abs=5e-3)
        assert aabb_clearance < 0.1016  # the false violation this issue fixes

    def test_c19_fixture_moved_inward_still_fires(self) -> None:
        """A trace moved inward creates a genuine <0.1016 mm violation
        against the TRUE rounded-corner geometry -- the fix must not mask
        real violations."""
        pad, footprint = _c19_pad_and_footprint()
        pad_elem = CopperElement.from_pad(pad, footprint)

        seg = CopperElement(
            element_type="segment",
            layer="F.Cu",
            net_number=7,
            geometry=(147.9, 98.45, 154.4, 98.45, 0.11),
            reference="Trace-test",
            net_name="Net-(D3-A)",
        )

        clearance, _, _ = _segment_circle_clearance(seg, pad_elem)

        assert clearance < 0.1016
        assert clearance == pytest.approx(0.0904, abs=5e-3)

    def test_rect_pad_shape_keeps_aabb_path_unchanged(self) -> None:
        """A plain ``rect`` pad (no rounding) must not be routed through
        the polygon path -- same numeric result as the AABB formula."""
        footprint = Footprint(
            name="U1", reference="U1", value="", position=(0.0, 0.0), rotation=0.0, layer="F.Cu"
        )
        pad = Pad(
            number="1",
            type="smd",
            shape="rect",
            position=(0.0, 0.0),
            size=(0.5, 1.2),
            layers=["F.Cu"],
            net_number=1,
            net_name="SIG",
        )
        pad_elem = CopperElement.from_pad(pad, footprint)
        assert pad_elem.polygon is not None  # true-geometry polygon still built

        seg = CopperElement(
            element_type="segment",
            layer="F.Cu",
            net_number=2,
            geometry=(0.8, -5.0, 0.8, 5.0, 0.2),
            reference="Trace",
            net_name="SIG2",
        )

        clearance, _, _ = _segment_circle_clearance(seg, pad_elem)
        expected = _rect_segment_centerline_distance(0.0, 0.0, 0.5, 1.2, 0.8, -5.0, 0.8, 5.0) - 0.1

        assert clearance == pytest.approx(expected, abs=1e-9)

    def test_zero_rratio_roundrect_matches_plain_rect(self) -> None:
        """``roundrect_rratio=0`` degenerates ``_pad_polygon`` to an exact
        rectangle, so the polygon path must agree with the AABB formula."""
        footprint = Footprint(
            name="U3", reference="U3", value="", position=(0.0, 0.0), rotation=0.0, layer="F.Cu"
        )
        pad = Pad(
            number="3",
            type="smd",
            shape="roundrect",
            position=(0.0, 0.0),
            size=(1.0, 2.0),
            layers=["F.Cu"],
            net_number=1,
            net_name="SIG",
            roundrect_rratio=0.0,
        )
        pad_elem = CopperElement.from_pad(pad, footprint)

        seg = CopperElement(
            element_type="segment",
            layer="F.Cu",
            net_number=2,
            geometry=(0.6, -5.0, 0.6, 5.0, 0.2),
            reference="Trace",
            net_name="SIG2",
        )

        clearance, _, _ = _segment_circle_clearance(seg, pad_elem)
        expected = _rect_segment_centerline_distance(0.0, 0.0, 1.0, 2.0, 0.6, -5.0, 0.6, 5.0) - 0.1

        assert clearance == pytest.approx(expected, abs=1e-6)

    def test_oval_pad_corner_false_positive_fixed(self) -> None:
        """Non-square ``oval``/``obround`` pads have the same AABB
        over-approximation at the stadium's flat-cap corners; the fix
        must apply to them too, not just ``roundrect``."""
        footprint = Footprint(
            name="U1", reference="U1", value="", position=(0.0, 0.0), rotation=0.0, layer="F.Cu"
        )
        pad = Pad(
            number="1",
            type="smd",
            shape="oval",
            position=(0.0, 0.0),
            size=(2.0, 0.6),
            layers=["F.Cu"],
            net_number=1,
            net_name="SIG",
        )
        pad_elem = CopperElement.from_pad(pad, footprint)

        # Trace grazing the stadium's rounded end-cap corner, where the
        # AABB rectangle extends past the pad's actual copper.
        seg = CopperElement(
            element_type="segment",
            layer="F.Cu",
            net_number=2,
            geometry=(1.05, 0.28, 1.2, 0.28, 0.2),
            reference="Trace",
            net_name="SIG2",
        )

        clearance, _, _ = _segment_circle_clearance(seg, pad_elem)
        aabb_clearance = (
            _rect_segment_centerline_distance(0.0, 0.0, 2.0, 0.6, 1.05, 0.28, 1.2, 0.28) - 0.1
        )

        # AABB reports a phantom overlap (negative); the true stadium
        # geometry clears.
        assert aabb_clearance < 0.0
        assert clearance > 0.0
        assert clearance == pytest.approx(0.0484, abs=5e-3)

    def test_arbitrary_rotation_uses_true_polygon_not_just_cardinal(self) -> None:
        """The polygon path must engage for non-cardinal rotations too --
        not only the 90/270 degree case exercised by the C19 fixture."""
        footprint = Footprint(
            name="U9",
            reference="U9",
            value="",
            position=(10.0, 5.0),
            rotation=30.0,
            layer="F.Cu",
        )
        pad = Pad(
            number="1",
            type="smd",
            shape="roundrect",
            position=(0.0, 0.0),
            size=(2.0, 1.0),
            layers=["F.Cu"],
            net_number=1,
            net_name="SIG",
            rotation=30.0,
            roundrect_rratio=0.3,
        )
        pad_elem = CopperElement.from_pad(pad, footprint)
        assert pad_elem.polygon is not None

        seg = CopperElement(
            element_type="segment",
            layer="F.Cu",
            net_number=2,
            geometry=(11.5, 4.0, 11.5, 6.0, 0.2),
            reference="Trace",
            net_name="SIG2",
        )

        clearance, _, _ = _segment_circle_clearance(seg, pad_elem)

        from shapely.geometry import LineString

        expected = LineString([(11.5, 4.0), (11.5, 6.0)]).buffer(0.1).distance(pad_elem.polygon)
        assert clearance == pytest.approx(expected, abs=1e-9)

        # The AABB path would report a meaningfully different (tighter)
        # clearance for this non-cardinal rotation -- confirms the polygon
        # path is actually engaged, not silently falling back to the AABB.
        cx, cy, w, h = pad_elem.geometry
        aabb_clearance = _rect_segment_centerline_distance(cx, cy, w, h, 11.5, 4.0, 11.5, 6.0) - 0.1
        assert abs(clearance - aabb_clearance) > 0.05


@pytest.mark.parametrize(
    "shape,size,rotation,radius",
    [
        ("roundrect", (2.0, 1.0), 45, 0.25),
        ("roundrect", (1.0, 1.0), 0, 0.25),
        ("oval", (2.0, 1.0), 45, 0.5),
        ("circle", (1.0, 1.0), 0, 0.5),
    ],
)
@pytest.mark.parametrize("gap", [0.2, 0.05])
def test_equal_aabb_rounded_pad_clearance(shape, size, rotation, radius, gap):
    """Analytic rounded-corner clearance, independent of polygon buffering."""
    import math

    # A point beyond the upper-right core corner, along its 45-degree normal.
    # Distance to the circular corner is gap + trace radius, by construction.
    trace_radius = 0.05
    distance = radius + gap + trace_radius
    x = size[0] / 2 - radius + distance / math.sqrt(2)
    y = size[1] / 2 - radius + distance / math.sqrt(2)
    angle = math.radians(-rotation)
    x, y = x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)
    fp = Footprint(name="U1", reference="U1", value="", position=(0, 0), rotation=0, layer="F.Cu")
    pad = Pad(
        number="1",
        type="smd",
        shape=shape,
        position=(0, 0),
        size=size,
        rotation=rotation,
        layers=["F.Cu"],
        net_number=1,
        roundrect_rratio=0.25,
    )
    elem = CopperElement.from_pad(pad, fp)
    seg = CopperElement(
        element_type="segment",
        layer="F.Cu",
        net_number=2,
        geometry=(x, y, x, y, trace_radius * 2),
        reference="trace",
        net_name="other",
    )
    clearance, _, _ = _segment_circle_clearance(seg, elem)
    assert clearance == pytest.approx(gap, abs=0.001)
    assert (clearance < 0.1016) == (gap < 0.1016)


def test_45_degree_roundrect_short_segment_clears():
    """Judge's nondegenerate short trace clears despite an equal-width AABB."""
    fp = Footprint(name="U1", reference="U1", value="", position=(0, 0), rotation=45, layer="F.Cu")
    pad = Pad(
        number="1",
        type="smd",
        shape="roundrect",
        position=(0, 0),
        size=(2, 1),
        rotation=45,
        layers=["F.Cu"],
        net_number=1,
        roundrect_rratio=0.25,
    )
    seg = CopperElement(
        element_type="segment",
        layer="F.Cu",
        net_number=2,
        geometry=(0, -1.2, 0.05, -1.2, 0.1),
        reference="trace",
        net_name="other",
    )
    clearance, _, _ = _segment_circle_clearance(seg, CopperElement.from_pad(pad, fp))
    assert clearance == pytest.approx(0.2791, abs=0.001)
    assert clearance > 0.1016


@pytest.mark.parametrize("shape", ["roundrect", "oval"])
@pytest.mark.parametrize("angle", [-45, -30, 30, 45])
def test_segment_on_physical_pad_and_mirrored_gap(shape, angle):
    """Keep the native-confirmed clockwise physical witness in this integration."""
    import math

    from kicad_tools.schema.pcb import Segment
    from kicad_tools.sexp import parse_string
    from kicad_tools.validate.rules.clearance import _calculate_clearance

    pad = Pad.from_sexp(
        parse_string(
            f'(pad "1" smd {shape} (at 0 0 {angle}) (size 4 1) (layers "F.Cu") (roundrect_rratio 0.25) (net 1 "A"))'
        )
    )
    fp = Footprint(
        name="Test",
        layer="F.Cu",
        position=(10, 20),
        rotation=0,
        reference="U1",
        value="Test",
        pads=[pad],
    )
    elem = CopperElement.from_pad(pad, fp)
    x = 10 + 1.5 * math.cos(math.radians(angle))
    for y, overlap in [
        (20 - 1.5 * math.sin(math.radians(angle)), True),
        (20 + 1.5 * math.sin(math.radians(angle)), False),
    ]:
        seg = CopperElement.from_segment(
            Segment(
                start=(x - 0.05, y),
                end=(x + 0.05, y),
                width=0.2,
                layer="F.Cu",
                net_number=2,
                net_name="B",
            )
        )
        distance = _calculate_clearance(elem, seg)[0]
        assert distance < 0 if overlap else distance > 0.15
