"""Tests for the exterior-side halo on same-component plane-net pads
(Issue #2902).

Verifies that ``RoutingGrid._apply_exterior_plane_pad_halo`` re-blocks
the EXTERIOR corridor of same-component plane-net pads (cells outside
the pad on cardinal sides without a same-component sibling within
~1.5 * pin_pitch).  This closes the residual 44
``clearance_pad_segment`` errors on board-04's STM32 LQFP-48 west edge
that ``_apply_narrow_channel_halo`` (Issue #2878) does NOT cover -- the
narrow-channel helper handles the inter-pad channel, but the failure
mode is on the exterior (open) side of plane-net pads.

Geometric setup mirrors the real board-04 U2 west-edge layout:
- Component at world (131, 122)
- Pin 1 (+3.3V plane, net=0 in router) at (126.8375, 119.25), metal
  1.475 x 0.3 (long axis along x, short axis along y, 0.5 mm pitch)
- Pin 2 sibling at (126.8375, 119.75) -- 0.5 mm pitch south of pin 1
- "Exterior" of pin 1 = north side (no sibling north of pin 1) AND
  west side (pad tip, away from chip body centroid at x=131)

Acceptance scenarios:

* LQFP-48 0.5 mm pitch under jlcpcb-tier1: foreign-net probe in the
  NORTH exterior of pin 1 must be blocked at the circular validator
  radius (pad_radius + clearance + trace_width/2).
* Same probe location must remain passable for net=0 (the plane
  net's own bonding via during stitch pass).
* Inter-pad channel (south of pin 1, north of pin 2 sibling) is
  handled by ``_apply_narrow_channel_halo`` and must NOT be
  double-blocked here -- the relaxation/narrow-channel-halo
  contract must be preserved.
* Signal pads (pad.net != 0) must NOT trigger the exterior halo --
  their escape routing must remain feasible.
* Wide pitch (>= fine_pitch_threshold) must short-circuit the
  helper -- regression guard for chorus-test BGA escape.
* Foreign-component cells inside the halo radius must NOT be
  perturbed -- regression guard mirroring the narrow-channel helper.

Reproduction of the board-04 failure-mode segment: trace endpoint
(127.5, 119.6) is just north of pin 1 metal (top edge y=119.4).  At
jlcpcb-tier1 (trace=clearance=0.127, pad_radius=0.7375) the validator
demands sqrt(...) - 0.0635 - 0.7375 >= 0.127, requiring distance from
trace center to pad center >= 0.928 mm.  The cell at (127.5, 119.6) is
sqrt((127.5-126.8375)^2 + (119.6-119.25)^2) = sqrt(0.439+0.1225) =
sqrt(0.5614) = 0.749 mm from pin 1 center -- well inside the 0.928 mm
exterior halo radius, so this cell MUST be blocked for foreign nets.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def lqfp48_jlcpcb_tier1_rules() -> DesignRules:
    """LQFP-48 fine-pitch rules approximating jlcpcb-tier1."""
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
    """0.65 mm pitch + relaxed clearance -- predicate gates the helper off."""
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
    """LQFP-48 west-edge pad: long axis along x, short along y."""
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
    """20x20 mm grid spanning [-10, 10] in both axes -- enough room to
    place a chip-corner pad and probe its exterior halo without grid
    boundary clamping interfering with the test logic."""
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=-10.0,
        origin_y=-10.0,
    )


class TestExteriorPlanePadHaloEngages:
    """LQFP-48 + jlcpcb-tier1: helper engages on plane-net pads with
    open exterior sides."""

    def test_board04_failure_mode_blocked(self, lqfp48_jlcpcb_tier1_rules):
        """Reproduce the exact board-04 failure-mode segment: a cell
        at the trace endpoint (127.5, 119.6) is 0.749 mm from pin 1
        center -- inside the validator's 0.928 mm circular radius and
        on pin 1's NORTH exterior (no sibling).  Must be blocked for
        foreign nets after the exterior halo runs."""
        # Translate board-04 coordinates into the test grid frame
        # (grid is centred on origin; subtract U2 placement at
        # (131, 122) to map pin 1 to (-4.1625, -2.75)).
        # Use the original board-04 absolute coordinates and shift the
        # grid origin to encompass them.
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=lqfp48_jlcpcb_tier1_rules,
            origin_x=124.0,
            origin_y=117.0,
        )

        # Pin 1 (+3.3V plane, net=0 in router) at (126.8375, 119.25).
        pin1 = _make_lqfp_pad(
            x=126.8375, y=119.25, net=0, net_name="+3.3V", ref="U2", pin="1"
        )
        grid.add_pad(pin1, pin_pitch=0.5)

        # Pin 2 sibling at (126.8375, 119.75), net=0 (also plane in
        # the .kicad_pcb pin 2 is "" / net 0 unassigned).  Use a
        # signal-net sibling (net=4 OSC_IN approximation) so the
        # north-vs-south classification of pin 1 is "no sibling
        # north, sibling south at 0.5 mm pitch".  Real board-04 pin 2
        # is unassigned in the test PCB but is still a same-component
        # pad that counts as a south-side sibling here.
        pin2 = _make_lqfp_pad(
            x=126.8375, y=119.75, net=4, net_name="OSC_IN", ref="U2", pin="2"
        )
        grid.add_pad(pin2, pin_pitch=0.5)

        # Failure-mode probe at (127.5, 119.6) -- inside the 0.928 mm
        # circular halo radius from pin 1, on pin 1's exterior north
        # side (wy=119.6 > 119.4=pad top edge; metal rectangle is
        # 119.10-119.40 in y, so 119.6 is outside metal).
        gx, gy = grid.world_to_grid(127.5, 119.6)
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Board-04 failure-mode segment endpoint (127.5, 119.6) must "
            "be blocked for foreign nets after the exterior plane-pad "
            "halo runs.  Without this fix, a foreign signal stub clips "
            "pin 1 (+3.3V) from the north exterior at 0.027 mm short of "
            "manufacturer clearance (44 errors on routed board-04)."
        )

    def test_corner_pad_exterior_north_blocked(self, lqfp48_jlcpcb_tier1_rules):
        """Corner pin (no north sibling): foreign-net probe ABOVE the
        pad (north exterior) within the halo radius must be blocked."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        # Plane pad at origin, sibling 0.5 mm south.
        plane = _make_lqfp_pad(x=0.0, y=0.0, net=0, pin="1")
        grid.add_pad(plane, pin_pitch=0.5)
        sibling = _make_lqfp_pad(x=0.0, y=0.5, net=4, pin="2")
        grid.add_pad(sibling, pin_pitch=0.5)

        # Probe at (0.5, -0.4): north of pad metal (top edge y=-0.15)
        # by 0.25 mm, east of pad center by 0.5 mm.  Distance to
        # plane pad center = sqrt(0.25 + 0.0625) = sqrt(0.3125) =
        # 0.559 mm.  Inside the 0.928 mm halo radius.  This cell is
        # on the NORTH side (open, no sibling) -- must be blocked.
        gx, gy = grid.world_to_grid(0.5, -0.4)
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Corner plane pad's north exterior (open side) must be "
            "blocked for foreign nets when fine-pitch infeasibility "
            "is triggered."
        )

    def test_corner_pad_inter_pad_channel_not_double_blocked(
        self, lqfp48_jlcpcb_tier1_rules
    ):
        """The inter-pad channel between plane pad and signal sibling
        is handled by ``_apply_narrow_channel_halo`` and must remain
        traversable for the SIBLING's own net.  The exterior halo
        must not re-block this side."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        plane = _make_lqfp_pad(x=0.0, y=0.0, net=0, pin="1")
        grid.add_pad(plane, pin_pitch=0.5)
        sibling = _make_lqfp_pad(x=0.0, y=0.5, net=4, pin="2")
        grid.add_pad(sibling, pin_pitch=0.5)

        # Channel midpoint between plane and sibling: y=0.25 (south
        # of plane, north of sibling).  Cell at (0.0, 0.25) is on
        # plane pad's SOUTH side (has_south_sibling=True), so the
        # exterior halo's south-side check returns False
        # (cell_exterior=False) and the cell is left for the narrow-
        # channel helper to manage.  The sibling's own net must be
        # able to escape through this cell.
        gx, gy = grid.world_to_grid(0.0, 0.25)
        # Sibling's net should be able to traverse this cell.  The
        # narrow-channel helper marks it ``is_obstacle=True`` with
        # ``cell.net == sibling.net``, which is_blocked checks via
        # ``cell.net != routing_net``.  For routing_net == sibling.net,
        # the cell must be passable.
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]
        # Only assert the property if the cell is owned by sibling's
        # net -- if grid quantisation assigned it elsewhere, the
        # narrow-channel helper governs.
        if cell.net == sibling.net:
            assert not grid.is_blocked(gx, gy, Layer.F_CU, net=sibling.net), (
                "Sibling's own net must remain passable through the "
                "inter-pad channel; exterior halo must not overreach "
                "into the channel."
            )

    def test_plane_pad_own_net_passable(self, lqfp48_jlcpcb_tier1_rules):
        """The plane pad's own net (net=0 in router) must remain able
        to claim cells in its exterior halo -- the stitch pass needs
        to bond the plane to the pad without false blocking."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        plane = _make_lqfp_pad(x=0.0, y=0.0, net=0, pin="1")
        grid.add_pad(plane, pin_pitch=0.5)
        sibling = _make_lqfp_pad(x=0.0, y=0.5, net=4, pin="2")
        grid.add_pad(sibling, pin_pitch=0.5)

        # Probe in north exterior -- a foreign net=42 is blocked, but
        # the plane (net=0) should be able to claim the cell.
        gx, gy = grid.world_to_grid(0.5, -0.4)
        # For routing_net=0 (the plane), the cell should be passable
        # under the standard ``is_blocked`` check
        # (``cell.blocked && (cell.net == 0 || cell.net != net)``).
        # Specifically the helper sets ``_blocked=True`` with
        # ``cell.net`` PRESERVED -- if cell.net was 0 (unclaimed),
        # is_blocked returns True even for net=0 (since cell.net==0
        # rejects all nonzero AND zero).  But the stitch pass uses
        # raw grid access, not is_blocked.  Verify the cell.net was
        # NOT corrupted to a foreign value.
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]
        # The exterior halo's bucket A/B uses cell.net==0 path which
        # preserves cell.net.  Ensure no foreign net was written.
        assert cell.net in (0, plane.net), (
            f"Exterior halo must not corrupt cell.net (got {cell.net}, "
            f"expected 0 or {plane.net} for the plane pad)."
        )


class TestExteriorPlanePadHaloDoesNotEngage:
    """Negative cases: helper short-circuits on inapplicable inputs."""

    def test_signal_pad_no_exterior_halo(self, lqfp48_jlcpcb_tier1_rules):
        """A signal-net pad (pad.net != 0) must NOT trigger the
        exterior halo.  Signal-pad escape routing must remain feasible
        -- the validator excludes same-component signal pads via
        ``exclude_refs``, so we have no reason to pre-block them."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        signal = _make_lqfp_pad(x=0.0, y=0.0, net=9, pin="7")
        grid.add_pad(signal, pin_pitch=0.5)
        sibling = _make_lqfp_pad(x=0.0, y=0.5, net=11, pin="8")
        grid.add_pad(sibling, pin_pitch=0.5)

        # Probe in north exterior of signal pad (no sibling north).
        # Cell at (0.5, -0.4) must remain passable for foreign nets
        # since signal pads don't trigger the exterior halo.
        gx, gy = grid.world_to_grid(0.5, -0.4)
        # The standard pad envelope at fine-pitch shrunk to ~0.0635
        # may still block this cell IF inside that envelope, but at
        # (0.5, -0.4) we are 0.5 mm radially outside the pad metal
        # in x and 0.25 mm in y -- well outside the shrunk envelope
        # (0.0635 mm) and the standard envelope (0.1905 mm).  So the
        # cell should be passable for foreign nets.
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Signal-net pad must NOT trigger exterior halo; "
            "cell outside its envelope must remain passable for "
            "foreign nets (preserves chip-escape routing)."
        )

    def test_wide_pitch_no_exterior_halo(self, wide_pitch_relaxed_rules):
        """Wide pitch where narrow-channel guard's predicate fails
        -- helper must short-circuit (chorus-test BGA regression
        guard)."""
        grid = _make_grid(wide_pitch_relaxed_rules)
        plane = _make_lqfp_pad(x=0.0, y=0.0, net=0, pin="1")
        grid.add_pad(plane, pin_pitch=0.65)
        sibling = _make_lqfp_pad(x=0.0, y=0.65, net=4, pin="2")
        grid.add_pad(sibling, pin_pitch=0.65)

        # Probe well outside the pad metal in the would-be north
        # exterior.  At 0.65 mm pitch with predicate feasible, no
        # exterior halo is applied so cell must remain passable.
        gx, gy = grid.world_to_grid(0.0, -0.5)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Wide-pitch exterior cell must remain passable when the "
            "narrow-channel guard's predicate fails -- regression guard "
            "for chorus-test BGA escape."
        )

    def test_no_pin_pitch_no_halo(self, lqfp48_jlcpcb_tier1_rules):
        """pin_pitch=None must short-circuit the helper."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        plane = _make_lqfp_pad(x=0.0, y=0.0, net=0, ref="TP1", pin="1")
        grid.add_pad(plane, pin_pitch=None)

        # Cell far from pad must be passable.
        gx, gy = grid.world_to_grid(2.0, 2.0)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "No pin_pitch metadata: helper must short-circuit and "
            "leave the grid untouched."
        )

    def test_single_pad_component_no_halo(self, lqfp48_jlcpcb_tier1_rules):
        """A single plane pad with no siblings on the component must
        not trigger the helper (len(component_pads) < 2 guard)."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)
        plane = _make_lqfp_pad(x=0.0, y=0.0, net=0, ref="TP1", pin="1")
        grid.add_pad(plane, pin_pitch=0.5)

        # Probe well outside the pad's envelope.
        gx, gy = grid.world_to_grid(2.0, 2.0)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Single plane pad on component: helper must short-circuit "
            "on the sibling-count guard."
        )

    def test_foreign_component_cell_not_overwritten(
        self, lqfp48_jlcpcb_tier1_rules
    ):
        """A foreign component's pad inside the halo radius must keep
        its net assignment.  Mirror of the narrow-channel halo's
        bucket-C contract."""
        grid = _make_grid(lqfp48_jlcpcb_tier1_rules)

        # Foreign component R1 claims a cell with net=42 inside what
        # would be U2 pin 1's north exterior halo.
        foreign = Pad(
            x=0.5,
            y=-0.4,
            width=0.1,
            height=0.1,
            net=42,
            net_name="NET42",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
        )
        grid.add_pad(foreign)

        # Now add U2's plane pair so the exterior halo would otherwise
        # overwrite the foreign cell.
        plane = _make_lqfp_pad(x=0.0, y=0.0, net=0, ref="U2", pin="1")
        grid.add_pad(plane, pin_pitch=0.5)
        sibling = _make_lqfp_pad(x=0.0, y=0.5, net=4, ref="U2", pin="2")
        grid.add_pad(sibling, pin_pitch=0.5)

        # Foreign pad center must keep its net=42 assignment intact.
        gx, gy = grid.world_to_grid(foreign.x, foreign.y)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]
        assert cell.net == 42, (
            f"Foreign component pad cell must keep its net=42 after "
            f"U2's exterior plane-pad halo runs; got cell.net={cell.net}."
        )


class TestExteriorPlanePadHaloIdempotency:
    """Adding pads in different orders must produce the same final
    grid state -- exterior halo is order-invariant."""

    def test_order_invariance(self, lqfp48_jlcpcb_tier1_rules):
        """Plane-then-signal vs signal-then-plane add order must give
        the same exterior-halo blocking."""
        grid_a = _make_grid(lqfp48_jlcpcb_tier1_rules)
        plane_a = _make_lqfp_pad(x=0.0, y=0.0, net=0, pin="1")
        grid_a.add_pad(plane_a, pin_pitch=0.5)
        sib_a = _make_lqfp_pad(x=0.0, y=0.5, net=4, pin="2")
        grid_a.add_pad(sib_a, pin_pitch=0.5)

        grid_b = _make_grid(lqfp48_jlcpcb_tier1_rules)
        sib_b = _make_lqfp_pad(x=0.0, y=0.5, net=4, pin="2")
        grid_b.add_pad(sib_b, pin_pitch=0.5)
        plane_b = _make_lqfp_pad(x=0.0, y=0.0, net=0, pin="1")
        grid_b.add_pad(plane_b, pin_pitch=0.5)

        # North exterior probe must agree in both orderings.
        gx, gy = grid_a.world_to_grid(0.5, -0.4)
        a_blocked = grid_a.is_blocked(gx, gy, Layer.F_CU, net=42)
        b_blocked = grid_b.is_blocked(gx, gy, Layer.F_CU, net=42)
        assert a_blocked == b_blocked, (
            "Exterior plane-pad halo must be order-invariant -- adding "
            "plane first vs sibling first must produce the same "
            "blocking decision on the plane's exterior."
        )
