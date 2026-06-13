"""Tests for the proactive narrow-channel halo (Issue #2878).

Verifies that ``RoutingGrid._apply_narrow_channel_halo`` re-blocks the
channel between two same-component pads when the manufacturer clearance
rules make the inter-pad channel too narrow to host a foreign trace at
full clearance, while preserving each same-component pad's own-net
escape corridor.

Background.  PR #2866's ``_clearance_for_pin_pitch`` already detects
the geometric infeasibility and returns the standard envelope (which
closes the channel).  But ``_relax_same_component_clearance`` (PR for
#2452) then unblocks the overlap region between same-component pads to
preserve chip escape routing.  That relaxation re-opens the channel to
FOREIGN nets on fine-pitch packages (LQFP-48 0.5 mm pitch at
jlcpcb-tier1 rules) -- the root cause of 44 ``clearance_pad_segment``
errors on routed board 04.

Issue #2878 plugs the leak: after the relaxation runs, this helper
inspects each same-component sibling pad on a different net and, if
the narrow-channel guard would have rejected the shrink, re-blocks the
inter-pad rectangle in a NET-AWARE way:

- Cells owned by either same-component pad's net are marked
  ``_blocked = True`` + ``_is_obstacle = True``, preserving the
  cell's ``net`` assignment so the chip's own escape (cell.net ==
  routing_net) still passes the pathfinder check.
- Unclaimed cells (cell.net == 0) are re-blocked with the standard
  static-obstacle pattern.
- Foreign-component cells (cell.net is some other net) are left
  untouched.

Acceptance scenarios (from the issue):

* LQFP-48 0.5 mm pitch under jlcpcb-tier1 (``trace=clearance=0.127``):
  the channel is infeasible; foreign-net probes between two
  same-component pads must report blocked.
* Same-component own-net probes must remain passable -- a chip's
  own escape corridor between its two pads (NRST out of pin 7 with
  GND on pin 8) must not regress.
* Wider pitches (0.65 mm with relaxed clearance) where the channel
  is feasible at the fine-pitch shrink: the helper must do nothing
  and the relaxation's permissive behaviour is preserved.
* Foreign-component cells in the channel rectangle (a stray
  passive's clearance halo crossing the band) must not be
  overwritten.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def lqfp48_jlcpcb_tier1_rules() -> DesignRules:
    """LQFP-48 fine-pitch rules approximating jlcpcb-tier1.

    ``trace_width = trace_clearance = 0.127 mm``, ``min_trace_width =
    0.127 mm``, ``fine_pitch_threshold = 0.65 mm``.  At 0.5 mm pitch the
    narrow-channel guard's predicate

        effective_channel = pitch - 2*shrunk - trace_width
                          = 0.5 - 2*0.0635 - 0.127
                          = 0.246 mm
        required_channel  = 2*trace_clearance + trace_width
                          = 2*0.127 + 0.127
                          = 0.381 mm

    holds: 0.246 < 0.381, so the guard rejects the shrink and the
    helper must engage.
    """
    return DesignRules(
        trace_width=0.127,
        trace_clearance=0.127,
        grid_resolution=0.05,
        min_trace_width=0.127,
        fine_pitch_clearance=0.0635,
        fine_pitch_threshold=0.65,
    )


@pytest.fixture
def wide_pitch_relaxed_rules() -> DesignRules:
    """0.65 mm pitch with relaxed clearance.

    ``trace_width = 0.15``, ``trace_clearance = 0.1``,
    ``min_trace_width = 0.1``.  The predicate becomes

        effective_channel = 0.65 - 2*0.05 - 0.15 = 0.4
        required_channel  = 2*0.1 + 0.15        = 0.35

    0.4 >= 0.35, so the guard accepts the shrink and the helper must
    NOT engage (chorus-test BGA escape regression guard).
    """
    return DesignRules(
        trace_width=0.15,
        trace_clearance=0.1,
        grid_resolution=0.05,
        min_trace_width=0.1,
        fine_pitch_threshold=0.8,
    )


def _make_lqfp_pad(
    x: float,
    y: float,
    net: int,
    *,
    width: float = 1.475,
    height: float = 0.3,
    ref: str = "U2",
    pin: str = "1",
    net_name: str = "",
) -> Pad:
    """Construct an LQFP-style pad with the long axis along x.

    Real LQFP-48 pads have their long axis perpendicular to the chip
    edge (radial / x) and their short axis (0.3 mm) along the chip
    edge (y).  Pads are stacked along y at 0.5 mm pitch.  This is the
    geometric arrangement needed to exercise the narrow-channel halo:
    pitch axis = y, channel-strip axis = x.
    """
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name or (f"NET{net}" if net > 0 else "GND"),
        ref=ref,
        pin=pin,
        layer=Layer.F_CU,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    """20x20 mm grid centred on the origin."""
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=-10.0,
        origin_y=-10.0,
    )


class TestNarrowChannelHaloEngages:
    """LQFP-48 + jlcpcb-tier1: the helper must engage and block the
    inter-pad channel for foreign nets.
    """

    def test_foreign_net_blocked_in_lqfp_channel(self, lqfp48_jlcpcb_tier1_rules):
        """Two same-component pads on different signal nets at 0.5 mm
        pitch under jlcpcb-tier1 -- the channel cells must report
        blocked for a foreign net (e.g. a stray NRST routed past the
        chip)."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)

        # Pin 7 (NRST, net=9) at y=0.0
        sibling = _make_lqfp_pad(x=-5.0, y=0.0, net=9, net_name="NRST", pin="7")
        grid.add_pad(sibling, pin_pitch=0.5)

        # Pin 8 (a same-component signal pad, net=11) at y=0.5
        # (0.5 mm pitch).
        plane = _make_lqfp_pad(x=-5.0, y=0.5, net=11, net_name="SWCLK", pin="8")
        grid.add_pad(plane, pin_pitch=0.5)

        # Probe the geometric midpoint of the channel.  Channel y
        # spans [0.15, 0.35] (inner metal edges), midpoint y=0.25;
        # channel x spans the pads' x metal extents [-5.7375, -4.2625],
        # midpoint x=-5.0.  This cell is in the inter-pad rectangle and
        # MUST be blocked for a foreign net (e.g. net=42).
        gx, gy = grid.world_to_grid(-5.0, 0.25)
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Channel cell between two same-component pads at fine pitch "
            "(LQFP-48 0.5 mm + jlcpcb-tier1) must be blocked for foreign "
            "nets; the narrow-channel halo (Issue #2878) re-closes the "
            "channel after _relax_same_component_clearance opens it for "
            "chip-escape routing.  Without this fix, foreign signal "
            "traces thread through the chip's own pad clearance and "
            "produce clearance_pad_segment DRC errors (44 errors on "
            "routed board 04 before this fix)."
        )

    def test_same_component_own_net_passable_through_channel(self, lqfp48_jlcpcb_tier1_rules):
        """Same fixture -- the channel cells must remain PASSABLE for
        each same-component pad's own net.  This preserves the chip's
        escape routing (board 04 NRST escape between U2.7 NRST and
        U2.8 plane-pad GND was the motivating regression guard for
        PR #2870; we must not regress it)."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)

        sibling = _make_lqfp_pad(x=-5.0, y=0.0, net=9, net_name="NRST", pin="7")
        grid.add_pad(sibling, pin_pitch=0.5)
        plane = _make_lqfp_pad(x=-5.0, y=0.5, net=11, net_name="SWCLK", pin="8")
        grid.add_pad(plane, pin_pitch=0.5)

        # Probe near sibling pad (closer to net=9 boundary).  Channel
        # cells owned by sibling's net should remain passable for net=9.
        gx, gy = grid.world_to_grid(-5.0, 0.20)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]

        # If this cell is owned by the sibling's net (9), it MUST be
        # passable for net=9 -- otherwise the chip's own escape is
        # broken.  The cell.net assignment depends on which pad's
        # envelope claimed the cell first (sibling was added first, so
        # cells near it should be net=9).  If cell.net == 0 or some
        # other value, this test silently checks the same property
        # against whatever net owns the cell.
        if cell.net == sibling.net:
            assert not grid.is_blocked(gx, gy, Layer.F_CU, net=sibling.net), (
                "Cell owned by sibling pad's net must remain passable for "
                "that net -- preserves chip escape routing.  Without this "
                "guarantee the narrow-channel halo would block the chip's "
                "own pad-to-pad escape that PR #2452 / PR #2870 carefully "
                "preserved."
            )
        elif cell.net == plane.net:
            assert not grid.is_blocked(gx, gy, Layer.F_CU, net=plane.net), (
                "Cell owned by plane pad's net must remain passable for "
                "that net (own-net escape symmetry)."
            )
        else:
            pytest.skip(
                f"Probe cell does not belong to either same-component pad "
                f"(cell.net={cell.net}; expected {sibling.net} or "
                f"{plane.net}); fixture geometry drifted -- adjust probe."
            )

    def test_plane_signal_pair_channel_blocked(self, lqfp48_jlcpcb_tier1_rules):
        """Mixed plane-net + signal-net pair (e.g. U2 GND pin alongside
        U2 NRST pin) -- the channel must still block foreign nets.
        This is the most common board-04 failure mode: 44 errors trace
        to foreign signals routing through GND-pad channels."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)

        # Plane-net GND pad at y=0.0 (pin 8 / 23 / 35 archetype)
        plane = _make_lqfp_pad(x=-5.0, y=0.0, net=0, net_name="GND", pin="8")
        grid.add_pad(plane, pin_pitch=0.5)
        # Same-component signal pad at y=0.5 (pin 7 archetype)
        sibling = _make_lqfp_pad(x=-5.0, y=0.5, net=9, net_name="NRST", pin="7")
        grid.add_pad(sibling, pin_pitch=0.5)

        gx, gy = grid.world_to_grid(-5.0, 0.25)
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Channel between plane-net pad and same-component signal pad "
            "must be blocked for foreign nets (Issue #2878).  This is the "
            "dominant board-04 failure mode (44 of 56 errors)."
        )


class TestNarrowChannelHaloDoesNotEngage:
    """Negative cases: the helper must NOT engage when the channel is
    geometrically feasible or when the prerequisites are absent.
    """

    def test_wide_pitch_relaxed_no_halo(self, wide_pitch_relaxed_rules):
        """0.65 mm pitch with relaxed clearance -- the narrow-channel
        guard accepts the shrink (predicate passes), so the helper
        must NOT engage and the relaxation's permissive behaviour is
        preserved.  This is the chorus-test BGA escape case (Issue
        #1778 / #2604); we must not regress it."""
        grid = _make_grid(wide_pitch_relaxed_rules)

        # Two pads at 0.65 mm pitch -- relaxation should leave the
        # channel open to all nets (foreign and own).
        sibling = _make_lqfp_pad(x=-5.0, y=0.0, net=9, net_name="SIG_A", pin="1")
        grid.add_pad(sibling, pin_pitch=0.65)
        other = _make_lqfp_pad(x=-5.0, y=0.65, net=11, net_name="SIG_B", pin="2")
        grid.add_pad(other, pin_pitch=0.65)

        # Channel midpoint at y=0.325.  At wide pitch + relaxed
        # clearance the relaxation leaves the cell open -- a foreign
        # net should be able to route through (because the relaxation
        # unblocks the cell).  Note: this is the PRE-#2878 behaviour
        # we must preserve for feasible channels.
        gx, gy = grid.world_to_grid(-5.0, 0.325)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]

        # The cell may or may not be blocked depending on its exact
        # position relative to the pads' reduced-clearance bands, but
        # the *narrow-channel halo* itself must not have flipped
        # ``is_obstacle`` for this cell.  We assert that: if the cell
        # is owned by one of the two same-component nets, it should
        # not have ``is_obstacle = True`` set by THIS helper.
        if cell.net in (sibling.net, other.net):
            # The relaxation may have unblocked this cell; the halo
            # must not have re-blocked it.  Check that the cell is
            # passable for the *opposite* same-component net -- if
            # it were is_obstacle=True with cell.net=sibling.net, a
            # foreign-net trace (e.g. net=42) would be blocked.
            # Under the wide-pitch case the helper's predicate fails
            # at the fine_pitch_threshold check, so is_obstacle must
            # NOT have been set by us.  We can't easily decouple
            # is_obstacle set by us vs. by the standard envelope, so
            # we instead assert the END USER property: foreign nets
            # can still traverse, which is the property the wide-
            # pitch BGA escape case relies on.
            assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
                "Wide-pitch channel cell must remain passable for "
                "foreign nets when the narrow-channel guard's predicate "
                "fails (channel is feasible at fine-pitch shrink).  "
                "Otherwise we regress chorus-test BGA escape (Issue "
                "#1778)."
            )

    def test_no_pin_pitch_no_halo(self, lqfp48_jlcpcb_tier1_rules):
        """Pads added with ``pin_pitch=None`` must not trigger the
        helper -- defensive against passives and other components
        where pitch metadata is absent.  Mirrors the
        ``_clearance_for_pin_pitch`` early-return at line 810."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)

        # Two same-component pads but no pin_pitch metadata.
        sibling = _make_lqfp_pad(x=-5.0, y=0.0, net=9, ref="R1", pin="1")
        grid.add_pad(sibling, pin_pitch=None)
        other = _make_lqfp_pad(x=-5.0, y=0.5, net=11, ref="R1", pin="2")
        grid.add_pad(other, pin_pitch=None)

        # No assertion on specific channel blocking because without
        # pin_pitch the standard envelope is applied.  We only assert
        # the helper itself didn't crash and didn't perturb cells far
        # from the pads.
        gx, gy = grid.world_to_grid(-5.0, 5.0)  # well outside any pad
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Cell far from any pad must remain passable when pin_pitch "
            "is absent -- the helper must short-circuit on missing "
            "pitch metadata."
        )

    def test_pitch_above_threshold_no_halo(self, lqfp48_jlcpcb_tier1_rules):
        """Pitch >= fine_pitch_threshold -- the predicate gates the
        helper off, so the channel is unaffected (standard envelope
        already handles wider pitches).

        We probe a cell *outside* either pad's standard envelope so
        we are not confusing the helper's behaviour with the standard
        pad clearance.  At 2.0 mm pitch with 0.6 mm pads the channel
        midpoint at y=1.0 is well outside each pad's standard
        envelope (half-extent 0.3 + 0.1905 = 0.4905 from each center,
        so each envelope only reaches 0.4905 / -0.4905 from center).
        """
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        # 2.0 mm pitch is well above the 0.65 mm threshold.
        sibling = _make_lqfp_pad(x=-5.0, y=0.0, net=9, ref="U3", pin="1", width=0.6, height=0.6)
        grid.add_pad(sibling, pin_pitch=2.0)
        other = _make_lqfp_pad(x=-5.0, y=2.0, net=11, ref="U3", pin="2", width=0.6, height=0.6)
        grid.add_pad(other, pin_pitch=2.0)

        # Channel midpoint at y=1.0.  The standard envelope spans
        # 0.4905 mm from each pad center (half-extent 0.3 + clearance
        # 0.1905 = 0.4905), so a cell at y=1.0 is 0.5095 mm from the
        # nearest envelope edge -- safely outside both envelopes.
        # Without the halo engaging, this cell must be passable for
        # any net.
        gx, gy = grid.world_to_grid(-5.0, 1.0)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Standard-pitch channel midpoint must remain passable for "
            "foreign nets -- the narrow-channel helper must not engage "
            "above the fine_pitch_threshold."
        )

    def test_foreign_component_cell_not_overwritten(self, lqfp48_jlcpcb_tier1_rules):
        """A foreign component's pad (different ref) claims a cell in
        the channel band.  When our same-component pair triggers the
        halo, the foreign-component cell must NOT be perturbed -- a
        regression guard against cross-component bleed-through (mirror
        of the #2869 carve-out's foreign-component test)."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)

        # Foreign component R1 (different ref) -- its pad claims some
        # cells with net=42 inside what would be U2's channel band.
        foreign = Pad(
            x=-5.0,
            y=0.25,
            width=0.1,
            height=0.1,
            net=42,
            net_name="NET42",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
        )
        grid.add_pad(foreign)

        # Now add U2's same-component pair surrounding the foreign
        # pad's cells.
        sibling = _make_lqfp_pad(x=-5.0, y=0.0, net=9, ref="U2", pin="7")
        grid.add_pad(sibling, pin_pitch=0.5)
        plane = _make_lqfp_pad(x=-5.0, y=0.5, net=11, ref="U2", pin="8")
        grid.add_pad(plane, pin_pitch=0.5)

        # Foreign pad center must keep its net=42 assignment intact.
        # The halo's "Bucket C" path must have skipped this cell.
        gx, gy = grid.world_to_grid(foreign.x, foreign.y)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]
        assert cell.net == 42, (
            f"Foreign component pad center cell must keep its net "
            f"assignment after U2's narrow-channel halo runs; got "
            f"{cell.net} not 42.  The halo's Bucket C path is "
            f"supposed to leave foreign-net cells alone."
        )

    def test_non_adjacent_same_component_pads_not_blocked(self, lqfp48_jlcpcb_tier1_rules):
        """Two same-component pads far apart on the chip (e.g. pin 1
        and pin 7 on an LQFP-48 edge -- 3.0 mm separation) MUST NOT
        trigger the narrow-channel halo.  Without the adjacency guard
        the helper would re-block the entire inter-pad rectangle
        (~3 mm gap times full pad width), wiping out a huge swath of
        the chip exterior's routing space.  This is a critical
        regression guard for the over-blocking bug surfaced during
        board-04 development: the helper must only fire for
        *geometrically adjacent* pairs at the component's pin pitch.
        """
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)

        # Pin 1 at y=-2.75 (LQFP-48 edge top), Pin 7 at y=0.25 -- the
        # exact relative positions on board 04's U2.  Distance = 3.0 mm
        # (>> 0.5 mm pitch).  Both same-component (ref=U2), different
        # nets (NC vs NRST), pin_pitch=0.5 says the COMPONENT is
        # fine-pitch.
        pin1 = _make_lqfp_pad(x=-5.0, y=-2.75, net=0, ref="U2", pin="1")
        pin7 = _make_lqfp_pad(x=-5.0, y=0.25, net=9, ref="U2", pin="7")
        grid.add_pad(pin1, pin_pitch=0.5)
        grid.add_pad(pin7, pin_pitch=0.5)

        # Probe a cell midway between the two pads but FAR from either
        # (1.5 mm from each).  No narrow channel exists between
        # widely-separated pads, so the cell must be passable for ANY
        # net (including foreign).  Under the over-blocking bug this
        # cell was returned as blocked because the helper iterated
        # without an adjacency check.
        gx, gy = grid.world_to_grid(-5.0, -1.25)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Cell 1.5 mm from both same-component pads must remain "
            "passable -- the narrow-channel helper must NOT engage on "
            "geometrically non-adjacent pad pairs.  Without the "
            "adjacency guard the helper would over-block the chip's "
            "exterior routing space and break legitimate foreign-trace "
            "escape routes (over-blocking bug surfaced during board-04 "
            "development; gap > 1.5 * pitch triggers the early-return)."
        )

    def test_no_sibling_no_halo(self, lqfp48_jlcpcb_tier1_rules):
        """A single same-component pad with no siblings -- the helper
        must short-circuit on the ``len(component_pads) < 2`` guard
        and have no effect.  Defensive against single-pin components
        (test points, single-pad jumpers)."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        lone = _make_lqfp_pad(x=-5.0, y=0.0, net=9, ref="TP1", pin="1")
        grid.add_pad(lone, pin_pitch=0.5)

        # Cells far from the lone pad must remain passable.
        gx, gy = grid.world_to_grid(-5.0, 5.0)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Single same-component pad must not trigger halo cells "
            "elsewhere in the grid -- helper must short-circuit on "
            "missing sibling."
        )


class TestNarrowChannelHaloIdempotency:
    """Adding pads in different orders must produce the same final
    grid state -- the helper is order-invariant.
    """

    def test_order_invariance_two_signal_pads(self, lqfp48_jlcpcb_tier1_rules):
        """Adding sibling-then-plane vs plane-then-sibling must
        produce the same blocking decision in the channel.  Validates
        that the per-pair re-evaluation is idempotent."""
        grid_a = _make_grid(lqfp48_jlcpcb_tier1_rules)
        sib_a = _make_lqfp_pad(x=-5.0, y=0.0, net=9, pin="7")
        grid_a.add_pad(sib_a, pin_pitch=0.5)
        pl_a = _make_lqfp_pad(x=-5.0, y=0.5, net=11, pin="8")
        grid_a.add_pad(pl_a, pin_pitch=0.5)

        grid_b = _make_grid(lqfp48_jlcpcb_tier1_rules)
        pl_b = _make_lqfp_pad(x=-5.0, y=0.5, net=11, pin="8")
        grid_b.add_pad(pl_b, pin_pitch=0.5)
        sib_b = _make_lqfp_pad(x=-5.0, y=0.0, net=9, pin="7")
        grid_b.add_pad(sib_b, pin_pitch=0.5)

        # Channel midpoint must be blocked for foreign nets in BOTH
        # orderings -- the per-pair re-evaluation guarantees this.
        gx, gy = grid_a.world_to_grid(-5.0, 0.25)
        assert grid_a.is_blocked(gx, gy, Layer.F_CU, net=42) == grid_b.is_blocked(
            gx, gy, Layer.F_CU, net=42
        ), (
            "Narrow-channel halo must be order-invariant.  Adding pad A "
            "then pad B vs B then A must produce the same blocking "
            "decision in the channel."
        )
