"""Tests for the fine-pitch narrow-channel guard (Issue #2865).

Verifies that ``RoutingGrid._clearance_for_pin_pitch`` declines to apply
the fine-pitch shrink when the resulting inter-pad channel cannot host a
trace at full manufacturer clearance.  This is the fix for the 44
``clearance_pad_segment`` errors on board 04 (STM32 LQFP-48 west edge)
caused by the pathfinder threading traces through geometrically
infeasible channels.

Acceptance scenarios (from the issue):

* LQFP-48 0.5 mm pitch under jlcpcb-tier1 (``trace=clearance=0.127 mm``):
  the channel is too narrow; the guard returns the standard envelope and
  the pathfinder is forced to escape around the package.
* Wider SSOP/QFP pitches (0.65 mm) at relaxed clearance: shrink still
  applies; chorus-test BGA pad-access routing does not regress.
* Mirror in ``find_pad_ref_at`` happens for free because that method
  already routes through ``_clearance_for_pin_pitch`` (per the #2604
  symmetry fix); a dedicated test pins the contract.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _make_grid(rules: DesignRules) -> RoutingGrid:
    """Build a small 20x20 mm grid centred on the origin."""
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=-10.0,
        origin_y=-10.0,
    )


def _make_pad(
    x: float,
    y: float,
    net: int,
    *,
    width: float = 0.3,
    height: float = 1.475,
    ref: str = "U2",
    pin: str = "1",
) -> Pad:
    """LQFP-48-style pad (default 0.3 x 1.475 mm)."""
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=f"NET{net}" if net > 0 else "GND",
        ref=ref,
        pin=pin,
        layer=Layer.F_CU,
    )


class TestClearanceForPinPitchNarrowChannel:
    """Direct unit tests for the ``_clearance_for_pin_pitch`` helper."""

    def test_lqfp48_jlcpcb_tier1_refuses_shrink(self):
        """LQFP-48 0.5 mm pitch under jlcpcb-tier1 (trace=clr=0.127):
        channel cannot fit a trace at full clearance, so the guard
        rejects the shrink and returns the standard envelope.
        """
        rules = DesignRules(
            trace_width=0.127,
            trace_clearance=0.127,
            grid_resolution=0.05,
            min_trace_width=0.127,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)

        standard = rules.trace_clearance + rules.trace_width / 2.0  # 0.1905
        clearance = grid._clearance_for_pin_pitch(0.5)
        assert clearance == pytest.approx(standard), (
            "LQFP-48 at jlcpcb-tier1 is geometrically infeasible at 0.5 mm "
            "pitch: required channel = 0.381 mm but only 0.246 mm available. "
            "The guard must return the standard envelope, not the shrunk "
            "min_trace_width/2 (0.0635 mm)."
        )

    def test_wider_pitch_still_shrinks(self):
        """Wider SSOP-style 0.65 mm pitch under looser clearance: the
        guard permits the shrink because a trace fits with full clearance.
        This is the chorus-test BGA case that #1778/#2604 originally
        added the shrink for; we must not regress it.
        """
        rules = DesignRules(
            trace_width=0.15,
            trace_clearance=0.1,
            grid_resolution=0.05,
            min_trace_width=0.1,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)

        # effective_channel = 0.65 - 2*0.05 - 0.15 = 0.4
        # required_channel  = 2*0.1 + 0.15 = 0.35
        # 0.4 >= 0.35 -> shrink applies
        expected = rules.min_trace_width / 2.0  # 0.05
        clearance = grid._clearance_for_pin_pitch(0.65)
        assert clearance == pytest.approx(expected), (
            "0.65 mm pitch at relaxed clearance must still get the "
            "fine-pitch shrink (chorus-test BGA / SSOP escape case)."
        )

    def test_above_threshold_returns_standard(self):
        """Pitch >= fine_pitch_threshold always uses the standard envelope
        regardless of geometry.  Pre-#2865 behavior; smoke-test that the
        narrow-channel guard did not move this branch.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            min_trace_width=0.127,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)

        standard = rules.trace_clearance + rules.trace_width / 2.0
        clearance = grid._clearance_for_pin_pitch(1.0)  # standard pitch
        assert clearance == pytest.approx(standard)

    def test_no_pitch_returns_standard(self):
        """``pin_pitch=None`` (e.g. resistor pads added without pitch info)
        uses the standard envelope.  Pre-#2865 behavior preserved.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            min_trace_width=0.127,
        )
        grid = _make_grid(rules)

        standard = rules.trace_clearance + rules.trace_width / 2.0
        clearance = grid._clearance_for_pin_pitch(None)
        assert clearance == pytest.approx(standard)

    def test_min_trace_width_unconfigured_returns_standard(self):
        """When ``rules.min_trace_width`` is ``None`` (no neck-down
        configured), fine-pitch shrink is not available at all, so the
        guard never fires -- standard envelope is always returned.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            min_trace_width=None,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)

        standard = rules.trace_clearance + rules.trace_width / 2.0
        clearance = grid._clearance_for_pin_pitch(0.5)
        assert clearance == pytest.approx(standard)

    def test_borderline_feasible_channel_shrinks(self):
        """Exactly-borderline channel (effective == required) must permit
        the shrink: the geometry is just sufficient.  This pins the
        ``>=`` comparison in the guard against an accidental ``>`` slip
        that would over-reject otherwise-feasible cases.
        """
        # Choose parameters so effective_channel == required_channel.
        #   effective = pitch - 2*shrunk - tw     = P - mtw - tw
        #   required  = 2*tc + tw
        #   Equate:  P - mtw - tw = 2*tc + tw  ->  P = mtw + 2*tw + 2*tc
        rules = DesignRules(
            trace_width=0.1,
            trace_clearance=0.05,
            min_trace_width=0.1,
            fine_pitch_threshold=0.8,
        )
        # P = 0.1 + 0.2 + 0.1 = 0.4
        grid = _make_grid(rules)
        expected = rules.min_trace_width / 2.0  # 0.05
        clearance = grid._clearance_for_pin_pitch(0.4)
        assert clearance == pytest.approx(expected), (
            "Borderline feasible (effective == required) channel must still permit the shrink."
        )

    def test_just_below_borderline_refuses_shrink(self):
        """One micron narrower than borderline must refuse: the guard's
        ``>=`` is strict at the equality, so a hair below must fail.
        """
        rules = DesignRules(
            trace_width=0.1,
            trace_clearance=0.05,
            min_trace_width=0.1,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)
        standard = rules.trace_clearance + rules.trace_width / 2.0
        # P = 0.4 (borderline); shrink at 0.4 - 0.001 must refuse
        clearance = grid._clearance_for_pin_pitch(0.399)
        assert clearance == pytest.approx(standard), (
            "Channel a hair below borderline must refuse the shrink "
            "(narrow-channel guard threshold is geometric, not heuristic)."
        )


class TestNarrowChannelBlocksThroughChannel:
    """Integration: a pair of LQFP-48 pads at jlcpcb-tier1 must produce a
    grid with NO unblocked path between them, so the pathfinder is
    forced to seek an escape rather than threading through the channel.
    """

    @pytest.fixture
    def jlcpcb_lqfp48_rules(self) -> DesignRules:
        """jlcpcb-tier1-equivalent clearance rules."""
        return DesignRules(
            trace_width=0.127,
            trace_clearance=0.127,
            grid_resolution=0.0125,  # fine enough to see the channel
            min_trace_width=0.127,
            fine_pitch_threshold=0.8,
        )

    def test_lqfp48_west_edge_channel_fully_blocked(self, jlcpcb_lqfp48_rules):
        """Two adjacent LQFP-48 pads at 0.5 mm pitch under jlcpcb-tier1
        leave NO unblocked cell band between them after the narrow-channel
        guard kicks in.  Pre-#2865 the shrunk halo left ~0.246 mm of
        unblocked band; post-#2865 the standard envelope (0.1905 mm
        radius) exceeds the pad half-width (0.15) by 0.0405 mm and the
        adjacent halo overlaps the same midpoint, so the channel is
        fully obstructed for *foreign-component* nets (the
        same-component clearance relaxation in #2452 would otherwise
        unblock this corridor).

        We use a foreign-net probe from a different component here so
        the result is independent of #2452 same-component handling --
        the failure mode reported in #2865 is foreign signal nets
        threading the LQFP corridor, not the chip's own pin escapes.
        """
        grid = _make_grid(jlcpcb_lqfp48_rules)
        # Two adjacent pads centred 0.5 mm apart along x (the LQFP-48
        # west-edge pitch).  Use DISTINCT component refs so the
        # same-component clearance relaxation (#2452) does not unblock
        # the corridor -- the LQFP pin-to-pin failure in #2865 is about
        # foreign signal nets threading between U2's pads, not about
        # U2's own pin escapes (which the same-component path handles
        # separately via _relax_same_component_clearance).
        p1 = _make_pad(x=-0.25, y=0.0, net=1, ref="U2", pin="1")
        p2 = _make_pad(x=0.25, y=0.0, net=2, ref="U99", pin="1")
        grid.add_pad(p1, pin_pitch=0.5)
        grid.add_pad(p2, pin_pitch=0.5)

        # Probe the channel midpoint (x=0): with the guard, both pads'
        # standard envelopes overlap here and the cell is blocked for
        # any foreign net.
        gx, gy = grid.world_to_grid(0.0, 0.0)
        # Net 3 is "foreign" to both p1 (net=1) and p2 (net=2).
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=3), (
            "LQFP-48 channel midpoint must be blocked for foreign nets "
            "after the narrow-channel guard; pre-#2865 it was unblocked "
            "and the pathfinder routed through it, producing DRC errors."
        )

    def test_wider_pitch_channel_remains_passable(self):
        """SSOP-style 0.65 mm pitch at relaxed clearance: the guard
        permits the shrink, so the inter-pad channel remains passable
        and the pathfinder can still escape between pads.  Regression
        guard for chorus-test BGA pad-access routing.
        """
        rules = DesignRules(
            trace_width=0.15,
            trace_clearance=0.1,
            grid_resolution=0.025,
            min_trace_width=0.1,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)
        # Two adjacent pads at 0.65 mm pitch, narrower pad to leave a
        # genuine routing channel between them.
        p1 = _make_pad(x=-0.325, y=0.0, net=1, pin="1", width=0.3, height=0.4)
        p2 = _make_pad(x=0.325, y=0.0, net=2, pin="2", width=0.3, height=0.4)
        grid.add_pad(p1, pin_pitch=0.65)
        grid.add_pad(p2, pin_pitch=0.65)

        # Midpoint x=0: with shrunk halo 0.05 mm, both halos end at
        # x = +/-0.225 and the midpoint is well outside either halo.
        gx, gy = grid.world_to_grid(0.0, 0.0)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=3), (
            "Wider-pitch channel midpoint must remain passable for "
            "foreign nets so chorus-test-style escape routing works."
        )


class TestFindPadRefAtMirrorsClearance:
    """``find_pad_ref_at`` calls ``_clearance_for_pin_pitch`` directly
    (lines 2506-2507), so the narrow-channel guard mirrors automatically.
    This test pins the symmetry contract from PR #2604 review.
    """

    def test_lookup_uses_standard_envelope_when_guard_refuses_shrink(self):
        """Under jlcpcb-tier1 LQFP-48 conditions, the guard refuses the
        shrink for the pad's halo on the *write* side.  The *read* side
        (``find_pad_ref_at``) must agree -- both must consider the cell
        to belong to the standard envelope.
        """
        rules = DesignRules(
            trace_width=0.127,
            trace_clearance=0.127,
            grid_resolution=0.025,
            min_trace_width=0.127,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)
        pad = _make_pad(x=0.0, y=0.0, net=1, ref="U2", pin="7")
        grid.add_pad(pad, pin_pitch=0.5)

        # Standard envelope edge: 0.15 (pad half-width) + 0.1905
        # (clearance + trace_width/2) = 0.3405 mm.  A point at
        # x = 0.3 mm is INSIDE the standard envelope -- so the lookup
        # must attribute it to U2 (the pad's owner).  Pre-#2604 / pre-#2865
        # with shrunk halo, the lookup would miss it because the shrunk
        # envelope ended at 0.2135 mm.
        ref = grid.find_pad_ref_at(0.3, 0.0)
        assert ref == "U2", (
            "find_pad_ref_at must mirror _clearance_for_pin_pitch's "
            "geometry-aware decision; with the guard refusing the shrink, "
            "the lookup must attribute the cell to U2."
        )

    def test_lookup_uses_shrunk_envelope_when_guard_permits(self):
        """At a pitch wide enough for the shrink to apply, the lookup
        must agree on the smaller envelope -- chorus-test BGA case.
        """
        rules = DesignRules(
            trace_width=0.15,
            trace_clearance=0.1,
            grid_resolution=0.025,
            min_trace_width=0.1,
            fine_pitch_threshold=0.8,
        )
        grid = _make_grid(rules)
        pad = _make_pad(x=0.0, y=0.0, net=1, ref="U5", pin="1", width=0.3, height=0.4)
        grid.add_pad(pad, pin_pitch=0.65)

        # Shrunk envelope edge: 0.15 + 0.05 = 0.20 mm.  A point at
        # x = 0.25 mm is OUTSIDE the shrunk envelope, so the lookup
        # must return None (no pad's halo covers it).
        ref = grid.find_pad_ref_at(0.25, 0.0)
        assert ref is None, (
            "When the guard permits the shrink, find_pad_ref_at must "
            "use the shrunk envelope (mirror #2604 symmetry)."
        )
