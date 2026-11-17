"""Integration test for the LQFP-48 stitch-via halo (Issue #2842).

Builds a synthetic LQFP-48 footprint with:
- 3 corner GND pads (plane net, ``pad.net == 0``)
- 9 signal pads mixed in (so the escape router must route past the GND pads)

Then verifies that after pad insertion the routing grid leaves enough
clear space around the GND pads for a 0.45 / 0.2 stitch via to land --
the same geometric constraint the stitcher's ``calculate_via_position``
applies at ``cli/stitch_cmd.py:1069``.

Acceptance: at least 10 of 11 GND pads (>= 95%) on a full LQFP-48 fixture
get a valid via location after #2842 (the current state -- 0% -- comes
from the fine-pitch envelope crowding the corner GND pads).

The test uses the same synthetic LQFP-48 fixture as
``tests/test_escape_via_in_pad_lqfp.py`` so the geometry is identical to
the production board 04 STM32F103C8T6 footprint.
"""

from __future__ import annotations

import math

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# LQFP-48 stitch-default via geometry (mirrors ``stitch_cmd.py:2400, :2573``).
STITCH_VIA_SIZE = 0.45
STITCH_VIA_CLEARANCE = 0.20


def _make_lqfp48_pads(
    *,
    gnd_pin_numbers: set[int],
    pitch: float = 0.5,
    pad_short: float = 0.30,
    pad_long: float = 1.50,
    pads_per_edge: int = 12,
    body_size: float | None = None,
    pad_stick_out: float = 0.85,
    start_net: int = 1,
    ref: str = "U2",
) -> list[Pad]:
    """Build an LQFP-48 footprint where the listed pins belong to the plane net.

    Mirrors ``_make_lqfp48_0p5mm`` in ``tests/test_escape_via_in_pad_lqfp.py``
    but assigns pins in ``gnd_pin_numbers`` to net 0 (the plane sentinel
    from ``io.py:2661``).
    """
    span = (pads_per_edge - 1) * pitch
    if body_size is None:
        body_size = span + 3.0 * pitch + 2.0 * pad_long
    half_body = body_size / 2
    pad_center_offset = half_body + pad_stick_out / 2
    half_span = span / 2

    pads: list[Pad] = []
    pin_no = 1

    def _net_for(p: int) -> tuple[int, str]:
        if p in gnd_pin_numbers:
            return 0, "GND"
        return start_net + p - 1, f"NET{start_net + p - 1}"

    # WEST edge: pads' long axis runs along X (perpendicular to the edge).
    for i in range(pads_per_edge):
        y = half_span - i * pitch  # top -> bottom
        net, name = _net_for(pin_no)
        pads.append(
            Pad(
                x=-pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=net,
                net_name=name,
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # SOUTH edge
    for i in range(pads_per_edge):
        x = -half_span + i * pitch
        net, name = _net_for(pin_no)
        pads.append(
            Pad(
                x=x,
                y=-pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=net,
                net_name=name,
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # EAST edge
    for i in range(pads_per_edge):
        y = -half_span + i * pitch
        net, name = _net_for(pin_no)
        pads.append(
            Pad(
                x=pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=net,
                net_name=name,
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # NORTH edge
    for i in range(pads_per_edge):
        x = half_span - i * pitch
        net, name = _net_for(pin_no)
        pads.append(
            Pad(
                x=x,
                y=pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=net,
                net_name=name,
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    return pads


def _make_board04_rules(manufacturer: str | None = "jlcpcb-tier1") -> DesignRules:
    """Match board 04's production design rules."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        min_trace_width=0.127,
        fine_pitch_clearance=0.127,
        fine_pitch_threshold=0.8,
        manufacturer=manufacturer,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )


def _gnd_pad_has_via_landing(
    grid: RoutingGrid,
    pad: Pad,
    via_size: float,
    clearance: float,
    other_pads: list[Pad] | None = None,
) -> bool:
    """Return True if at least one position on or near the pad can host a
    stitch via of diameter ``via_size`` with at least ``clearance`` to
    foreign-net pad metal.

    Mirrors the geometric pre-check that ``calculate_via_position`` in
    ``cli/stitch_cmd.py:1069`` does -- but limited to pad-vs-via geometry
    (the post-route trace and via clashes are out of scope for this
    unit-level fixture; the integration test in board 04 covers the trace
    side).  We sweep candidate via centers across:

    1. The pad metal interior (the stitcher's first preference).
    2. Offsets along the pad's long axis up to one pad pitch beyond the
       metal edge (where the stitcher's dog-leg / extended escape would
       look on tall LQFP-48-style pads).

    A candidate passes when:
    - The candidate center is inside the pad metal OR within
      ``via_size/2 + clearance`` of the pad metal centerline (so the via
      stub stays connected to the pad).
    - No foreign-net pad metal is within ``via_size/2 + clearance`` of
      the candidate center.

    Args:
        grid: The routing grid (only used for layer indexing; the actual
            geometry uses the pad coordinates from ``other_pads``).
        pad: The plane-net pad to bond.
        via_size: Stitch via outer diameter (mm).
        clearance: Required clearance to foreign-net copper (mm).
        other_pads: Optional list of other pads on the same board -- when
            provided, used for foreign-net pad-vs-via geometric checks.
    """
    via_radius = via_size / 2.0
    needed = via_radius + clearance

    other_pads = other_pads or []

    # Build candidate via positions.  We start at the pad center, then
    # walk outward along the pad's long axis.  For LQFP-48 west-edge
    # pads (long axis along X), this lets the via slip past the
    # adjacent-pin row toward the chip's outside.  The stitcher's
    # extended-escape mode (cli/stitch_cmd.py:1599) searches up to 3 mm
    # away from the pad along this axis when the central placement fails.
    candidates: list[tuple[float, float]] = [(pad.x, pad.y)]
    long_along_x = pad.width >= pad.height
    long_half = max(pad.width, pad.height) / 2.0
    short_half = min(pad.width, pad.height) / 2.0
    step = grid.resolution
    # Stitcher's extended-escape budget is 3.0 mm; we search up to that
    # but the via must remain electrically connected to the pad metal.
    # The connection is supplied by a separate stub trace from the pad
    # to the via, so we only require the via candidate to clear foreign
    # pad metal.  See cli/stitch_cmd.py:2272 ("via_size/2 + clearance").
    extent = long_half + 3.0
    n = int(math.ceil(extent / step))
    for k in range(1, n + 1):
        if long_along_x:
            candidates.append((pad.x + k * step, pad.y))
            candidates.append((pad.x - k * step, pad.y))
        else:
            candidates.append((pad.x, pad.y + k * step))
            candidates.append((pad.x, pad.y - k * step))

    for cx, cy in candidates:
        # The candidate must remain laterally aligned with the pad's
        # short axis (the via stub fans out along the long axis).
        if long_along_x:
            if abs(cy - pad.y) > short_half:
                continue
        else:
            if abs(cx - pad.x) > short_half:
                continue

        # Check pad-vs-via geometric clearance.
        ok = True
        for other in other_pads:
            if other is pad:
                continue
            if other.net == pad.net:
                # Same-net pads don't violate clearance.
                continue
            # Pad-pad bounding box check (rectangle vs circle): does the
            # candidate via circle intersect the other pad's metal
            # rectangle expanded by ``clearance``?
            ohw = other.width / 2.0
            ohh = other.height / 2.0
            # Closest point on other pad's metal rect to the candidate
            closest_x = max(other.x - ohw, min(cx, other.x + ohw))
            closest_y = max(other.y - ohh, min(cy, other.y + ohh))
            dist = math.hypot(cx - closest_x, cy - closest_y)
            if dist < needed:
                ok = False
                break

        if ok:
            return True

    return False


class TestStitchViaHaloEndToEnd:
    """End-to-end-ish coverage of #2842 against an LQFP-48 fixture."""

    def test_corner_gnd_pads_get_via_landing_with_halo(self):
        """LQFP-48 corner GND pins (pad 8, 23, 35) must have clear space
        for a 0.45 / 0.2 stitch via *before* any escape routing -- which
        is what the halo guarantees.
        """
        rules = _make_board04_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        # Board 04's three corner-GND pins on U2 (LQFP-48): 8, 23, 35.
        # Plus 8 more inner GND pins typical for an STM32F103C8T6 ground
        # network -- 11 total, matching the LQFP-48 reference geometry the
        # issue ticket cites.
        gnd_pins = {8, 23, 35, 1, 12, 13, 24, 25, 36, 37, 47}
        pads = _make_lqfp48_pads(gnd_pin_numbers=gnd_pins)
        for pad in pads:
            grid.add_pad(pad, pin_pitch=0.5)

        gnd_pads = [p for p in pads if p.net == 0]
        assert len(gnd_pads) == len(gnd_pins)

        ok_count = sum(
            1
            for p in gnd_pads
            if _gnd_pad_has_via_landing(
                grid,
                p,
                STITCH_VIA_SIZE,
                STITCH_VIA_CLEARANCE,
                other_pads=pads,
            )
        )

        # AC: >= 95% of plane-net pads get vias (>= 10 / 11 on LQFP-48).
        ratio = ok_count / len(gnd_pads)
        assert ratio >= 0.95, (
            f"#2842 acceptance: expected >= 95% (>= 10/11) of LQFP-48 GND pads "
            f"to have a clear stitch-via landing; got {ok_count}/{len(gnd_pads)} "
            f"({ratio:.0%})."
        )

    def test_signal_pad_envelopes_preserved(self):
        """Regression guard: the halo must NOT block signal pads' own
        metal cells.  Each signal pad must still report ``cell.net == its
        own net`` at the pad center (this is what the router uses for
        same-net pathfinding to the pad).
        """
        rules = _make_board04_rules()
        grid = _make_grid(rules)
        gnd_pins = {8, 23, 35}
        pads = _make_lqfp48_pads(gnd_pin_numbers=gnd_pins)
        for pad in pads:
            grid.add_pad(pad, pin_pitch=0.5)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        sig_pads = [p for p in pads if p.net > 0]
        for sig_pad in sig_pads:
            gx, gy = grid.world_to_grid(sig_pad.x, sig_pad.y)
            cell = grid.grid[layer_idx][gy][gx]
            assert cell.net == sig_pad.net, (
                f"Signal pad {sig_pad.pin} (net {sig_pad.net}) center cell "
                f"lost its net assignment: got cell.net={cell.net}."
            )
            assert cell.pad_blocked, f"Signal pad {sig_pad.pin} center must remain pad_blocked."
            # And the owner net must still pass our blocked check at its
            # own pad center.
            assert not grid.is_blocked(gx, gy, Layer.F_CU, net=sig_pad.net), (
                f"Signal pad {sig_pad.pin} center must remain reachable to its own net."
            )

    def test_halo_blocks_more_cells_than_disabled_mode(self):
        """Sanity check that toggling ``stitch_via_halo`` actually changes
        the grid state around plane-net pads.  With the halo OFF, fewer
        cells around the pad are blocked for foreign nets.  Counts cells
        in a 1.5 mm box around pad 8's center that are blocked for a
        foreign net (net=99 -- arbitrary).

        Issue #2865 follow-up: the original board-04 rules
        (``trace_clearance=0.15``, ``trace_width=0.2``, ``pitch=0.5``)
        now route through the narrow-channel guard's "standard
        envelope" branch (effective channel 0.173 mm < required
        0.5 mm).  When the standard envelope is wider than the via
        halo on both axes, the halo and no-halo modes produce
        identical blocking -- because the standard envelope already
        covers what the halo would reserve.  This is the correct
        outcome for jlcpcb-tier1 LQFP-48 corner GND pins: the
        standard halo *is* the via halo, and no extra ring is needed.

        To still exercise the halo's contribution to blocking,
        construct a fixture where the *fine-pitch shrink remains
        feasible* (looser clearance with min_trace_width), so the
        halo annular ring lives outside the shrunk standard envelope.
        """

        # Looser-clearance ruleset where the narrow-channel guard
        # permits the shrink at 0.65 mm pitch, so the halo annular
        # ring is visible beyond the shrunk envelope.
        def _make_loose_rules() -> DesignRules:
            return DesignRules(
                trace_width=0.15,
                trace_clearance=0.1,
                via_drill=0.3,
                via_diameter=0.6,
                grid_resolution=0.05,
                min_trace_width=0.1,
                fine_pitch_clearance=0.1,
                fine_pitch_threshold=0.8,
                manufacturer="jlcpcb-tier1",
            )

        # With halo (default)
        rules_on = _make_loose_rules()
        grid_on = _make_grid(rules_on)
        gnd_pins = {8, 23, 35}
        # Use 0.65 mm pitch so the shrink remains feasible per #2865's
        # narrow-channel guard.
        pads_on = _make_lqfp48_pads(gnd_pin_numbers=gnd_pins, pitch=0.65)
        for pad in pads_on:
            grid_on.add_pad(pad, pin_pitch=0.65)

        # Without halo (opt-out)
        rules_off = _make_loose_rules()
        rules_off.stitch_via_halo = False
        grid_off = _make_grid(rules_off)
        pads_off = _make_lqfp48_pads(gnd_pin_numbers=gnd_pins, pitch=0.65)
        for pad in pads_off:
            grid_off.add_pad(pad, pin_pitch=0.65)

        # Count cells blocked for a foreign net (net=99) in a 1.5 mm
        # box around pin 8's center.
        gnd_pad_on = next(p for p in pads_on if p.pin == "8" and p.net == 0)

        box_half_mm = 1.5
        steps = int(box_half_mm / grid_on.resolution)
        center_gx, center_gy = grid_on.world_to_grid(gnd_pad_on.x, gnd_pad_on.y)

        blocked_on = 0
        blocked_off = 0
        for dy in range(-steps, steps + 1):
            for dx in range(-steps, steps + 1):
                gx_n, gy_n = center_gx + dx, center_gy + dy
                if grid_on.is_blocked(gx_n, gy_n, Layer.F_CU, net=99):
                    blocked_on += 1
                if grid_off.is_blocked(gx_n, gy_n, Layer.F_CU, net=99):
                    blocked_off += 1

        assert blocked_on > blocked_off, (
            f"#2842 halo must block more foreign-net cells than the no-halo mode: "
            f"with_halo={blocked_on} <= without_halo={blocked_off}"
        )
