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
