"""Tests for the plane-net stitch-via halo (Issue #2842).

Verifies that ``RoutingGrid._add_pad_unsafe`` reserves a foreign-net
clearance halo around plane-net pads (``pad.net == 0``) so the subsequent
``kct stitch`` step has room to drop a via.  Covers both the standard-pitch
path and the fine-pitch path (where ``_clearance_for_pin_pitch`` shrinks
the trace envelope to ``min_trace_width/2``).

These are pure unit tests against the grid -- no router invocation.  The
companion integration test ``tests/test_stitch_via_halo.py`` exercises the
LQFP-48 fixture end-to-end.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _make_pad(
    x: float,
    y: float,
    net: int,
    *,
    width: float = 0.3,
    height: float = 1.475,
    ref: str = "U2",
    pin: str = "1",
    net_name: str = "",
) -> Pad:
    """Construct a single LQFP-48-style pad (default 0.3 x 1.475 mm)."""
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


@pytest.fixture
def fine_pitch_rules() -> DesignRules:
    """LQFP-48-style fine-pitch rules (0.5mm pitch board)."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        grid_resolution=0.05,
        min_trace_width=0.127,
        fine_pitch_clearance=0.127,
        fine_pitch_threshold=0.8,
    )


@pytest.fixture
def standard_pitch_rules() -> DesignRules:
    """Standard-pitch passive-style rules (no fine-pitch overrides)."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        grid_resolution=0.05,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    """Build a small 20x20 mm grid centred on the origin."""
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=-10.0,
        origin_y=-10.0,
    )


class TestStitchViaHaloRadius:
    """Verify ``DesignRules.stitch_via_halo_radius()`` derives the right size."""

    def test_default_radius_uses_stitcher_default_via(self):
        """No manufacturer -> 0.45 mm via / 2 + 0.2 mm clearance = 0.425 mm."""
        rules = DesignRules(trace_clearance=0.2)
        assert rules.stitch_via_halo_radius() == pytest.approx(0.425)

    def test_jlcpcb_tier1_radius_uses_mfr_min_via(self):
        """jlcpcb-tier1 has min_via_diameter=0.6 mm -> halo = 0.5 mm (Issue #2848)."""
        rules = DesignRules(trace_clearance=0.2, manufacturer="jlcpcb-tier1")
        assert rules.stitch_via_halo_radius() == pytest.approx(0.5)

    def test_unknown_manufacturer_falls_back_to_default(self):
        """Unknown mfr -> conservative 0.45 mm via default rather than crashing."""
        rules = DesignRules(trace_clearance=0.2, manufacturer="not-a-real-mfr")
        # Unknown mfr falls back; result must still be >= the standard envelope.
        radius = rules.stitch_via_halo_radius()
        assert radius >= rules.trace_clearance + rules.trace_width / 2.0
        # And no smaller than the unmanufactured default (0.425 mm).
        assert radius == pytest.approx(0.425)

    def test_radius_never_shrinks_standard_envelope(self):
        """If the standard envelope is wider than the via halo, use the wider one."""
        # Pathological case: very wide trace + tight clearance.
        rules = DesignRules(trace_width=2.0, trace_clearance=0.05)
        standard = rules.trace_clearance + rules.trace_width / 2.0
        assert standard == pytest.approx(1.05)
        assert rules.stitch_via_halo_radius() >= standard


class TestPlaneNetHaloReservation:
    """Direct unit tests on ``_add_pad_unsafe`` -> ``_apply_stitch_via_halo``."""

    def test_fine_pitch_plane_pad_reserves_via_halo(self, fine_pitch_rules):
        """Fine-pitch plane-net pad must reserve a halo extending the
        foreign-net keep-out to ``via_radius + clearance`` from the pad
        center -- much larger than the fine-pitch trace envelope
        (~0.0635 mm) but bounded at the via halo radius (0.425 mm).
        """
        grid = _make_grid(fine_pitch_rules)
        plane_pad = _make_pad(x=0.0, y=0.0, net=0, net_name="GND")
        grid.add_pad(plane_pad, pin_pitch=0.5)

        # Halo extends ``stitch_via_halo_radius() = 0.425 mm`` from the
        # pad center.  Pad east edge at x=0.15; halo east edge at
        # x=0.425.  A cell at x=0.4 sits inside the halo (0.4 < 0.425)
        # but well outside the fine-pitch trace envelope (0.15 + 0.0635
        # = 0.2135).  Without #2842 it would be unblocked.
        assert grid.is_blocked(*grid.world_to_grid(0.4, 0.0), Layer.F_CU, net=9), (
            "Cell at 0.4 mm east of fine-pitch plane pad must be blocked for foreign "
            "nets after #2842 (halo extends to 0.425 mm from pad center)."
        )

    def test_fine_pitch_plane_pad_halo_does_not_extend_beyond_radius(self, fine_pitch_rules):
        """A cell well outside the halo must remain unblocked."""
        grid = _make_grid(fine_pitch_rules)
        plane_pad = _make_pad(x=0.0, y=0.0, net=0, net_name="GND")
        grid.add_pad(plane_pad, pin_pitch=0.5)

        # Halo extends 0.425 mm from pad center.  Cell at x=0.6 is
        # 0.175 mm outside the halo -- must remain passable.
        assert not grid.is_blocked(*grid.world_to_grid(0.6, 0.0), Layer.F_CU, net=9), (
            "Cell well outside the halo radius must remain passable."
        )

    def test_standard_pitch_plane_pad_envelope_unchanged(self, standard_pitch_rules):
        """Standard-pitch passives use ``trace_clearance + trace_width/2 =
        0.3 mm`` for their normal envelope.  That envelope is already at
        least as wide as the via halo extension from the pad center
        (0.5 mm half-width >= 0.425 mm halo-from-center), so no additional
        ring is reserved -- a regression guard for inter-component routing
        channels (board 04 passive arrays).
        """
        grid = _make_grid(standard_pitch_rules)
        plane_pad = _make_pad(x=0.0, y=0.0, net=0, net_name="GND", width=1.0, height=1.0)
        grid.add_pad(plane_pad)  # No pin_pitch -> standard envelope

        # Standard envelope: 0.5 (pad half-width) + 0.3 = 0.8 mm from
        # center.  Halo from center = 0.425 mm -- well inside the pad
        # metal, so no extra ring extends beyond the standard envelope.
        # A cell at x=0.85 (inside the standard envelope) is blocked by
        # the standard halo, not by the via halo.  A cell at x=0.95
        # (just past the standard envelope) must NOT be blocked by the
        # via halo (because the halo would not extend that far).
        assert not grid.is_blocked(*grid.world_to_grid(0.95, 0.0), Layer.F_CU, net=9), (
            "Standard-pitch plane pad must keep its pre-#2842 envelope; "
            "the via halo is satisfied inside the pad metal itself."
        )

    def test_signal_pad_unaffected_by_halo_machinery(self, fine_pitch_rules):
        """Signal pads (``pad.net > 0``) must not receive the *via* halo
        regardless of pin pitch; the via halo is *only* for plane-net pads.

        Issue #2865 narrow-channel guard: with this fixture's
        ``trace_clearance=trace_width=0.2`` and ``pitch=0.5``, the
        fine-pitch shrink is geometrically infeasible (channel cannot
        fit ``2*clearance + trace_width = 0.6 mm`` -- only 0.246 mm is
        available).  ``_clearance_for_pin_pitch`` therefore returns the
        standard envelope (0.3 mm) so any cell within 0.45 mm of the pad
        center *is* blocked, but that block comes from the standard
        clearance envelope -- NOT from the via halo (which never applies
        to signal pads).  Probe a point well beyond the standard
        envelope to assert "no via halo" cleanly.
        """
        grid = _make_grid(fine_pitch_rules)
        sig_pad = _make_pad(x=0.0, y=0.0, net=9, net_name="NRST")
        grid.add_pad(sig_pad, pin_pitch=0.5)

        # Standard envelope edge: pad_half_width (0.15) + clearance +
        # trace_width/2 (0.3) = 0.45 mm.  Via halo would have extended
        # to 0.425 mm from center.  A cell at x=0.5 is past both, so
        # the only way it could be blocked is if the via halo (a
        # plane-net-only feature) were applied to this signal pad.
        assert not grid.is_blocked(*grid.world_to_grid(0.5, 0.0), Layer.F_CU, net=10), (
            "Signal pad envelope must not get the via halo (only plane-net pads do)."
        )

    def test_halo_does_not_overwrite_signal_pad_metal(self, fine_pitch_rules):
        """When a plane-net pad's halo overlaps an adjacent same-component
        signal pad's metal area, the signal pad's net assignment must be
        preserved (``cell.net`` stays at the signal net; ``pad_blocked``
        stays True).  This is the regression guard requested by the AC:
        "no previously-routing signal nets ... fail because of the new
        clearance reservation".
        """
        grid = _make_grid(fine_pitch_rules)

        # LQFP-48 layout: pad 7 (signal, NRST) at y=0.25, pad 8 (plane, GND)
        # at y=0.75.  Pads are 0.3 wide and 1.475 tall at 0.5 mm pitch.
        sig_pad = _make_pad(x=0.0, y=0.25, net=9, net_name="NRST", pin="7")
        plane_pad = _make_pad(x=0.0, y=0.75, net=0, net_name="GND", pin="8")
        grid.add_pad(sig_pad, pin_pitch=0.5)
        grid.add_pad(plane_pad, pin_pitch=0.5)

        # Cell at (0, 0.25) is the signal pad's center -- must still
        # belong to net 9 even after the plane halo covers it.
        gx, gy = grid.world_to_grid(0.0, 0.25)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]
        assert cell.pad_blocked, "Signal pad metal must remain pad-blocked."
        assert cell.net == 9, (
            f"Signal pad center cell must keep its net assignment; got {cell.net} not 9."
        )
        # And the signal owner must still be able to route to its own pad.
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=9), (
            "Signal pad's own net (9) must still reach the pad metal."
        )

    def test_halo_blocks_foreign_net_but_pad_net_marker_unchanged(self, fine_pitch_rules):
        """The halo cells must remain ``cell.net == 0`` (the plane-net
        sentinel) so the existing "static no-net obstacle" path in
        ``Grid.is_blocked`` and the pathfinder fires correctly for foreign
        nets.
        """
        grid = _make_grid(fine_pitch_rules)
        plane_pad = _make_pad(x=0.0, y=0.0, net=0, net_name="GND")
        grid.add_pad(plane_pad, pin_pitch=0.5)

        # Cell deep inside the halo ring (0.42 < 0.425 mm from center)
        # but well outside the standard fine-pitch envelope (0.15 + 0.0635
        # = 0.2135 mm).
        gx, gy = grid.world_to_grid(0.4, 0.0)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]
        assert cell.blocked, "Halo cell must be blocked."
        assert cell.net == 0, (
            f"Halo cell must keep cell.net==0 (plane-net sentinel); got {cell.net}."
        )

    def test_halo_opt_out_via_rules_flag(self, fine_pitch_rules):
        """Setting ``rules.stitch_via_halo = False`` must restore pre-#2842
        behaviour (no halo reservation).  This is the routing-intent
        opt-out the AC requires.

        Issue #2865 narrow-channel guard: with the fixture's tight
        clearance (0.2 mm) the fine-pitch shrink is geometrically
        infeasible at 0.5 mm pitch, so the envelope falls back to the
        standard one regardless of the via halo flag.  Probe a cell
        past the standard envelope edge (0.45 mm) so the test isolates
        the via halo's contribution.
        """
        rules_no_halo = DesignRules(
            trace_width=fine_pitch_rules.trace_width,
            trace_clearance=fine_pitch_rules.trace_clearance,
            grid_resolution=fine_pitch_rules.grid_resolution,
            min_trace_width=fine_pitch_rules.min_trace_width,
            fine_pitch_clearance=fine_pitch_rules.fine_pitch_clearance,
            fine_pitch_threshold=fine_pitch_rules.fine_pitch_threshold,
            stitch_via_halo=False,
        )
        grid = _make_grid(rules_no_halo)
        plane_pad = _make_pad(x=0.0, y=0.0, net=0, net_name="GND")
        grid.add_pad(plane_pad, pin_pitch=0.5)

        # With the opt-out, the via halo (which would extend to 0.425 mm
        # from center) is not applied.  A cell at x=0.5 (past the standard
        # envelope edge at 0.45 mm, past the would-be halo at 0.425 mm)
        # must remain unblocked.
        gx, gy = grid.world_to_grid(0.5, 0.0)
        assert not grid.is_blocked(gx, gy, Layer.F_CU, net=9), (
            "With stitch_via_halo=False the halo must NOT be applied."
        )

    def test_pth_plane_pad_envelope_covers_all_layers(self, standard_pitch_rules):
        """Through-hole plane-net pads (e.g. PTH connector ground pins)
        block routing on every layer.  Because they are standard-pitch
        (pad half-width 0.85 mm > 0.425 mm halo-from-center), the via
        halo lives entirely inside the pad metal and the existing
        standard envelope already covers everything.  This test pins
        the existing PTH behaviour so the via-halo opt-in does not
        regress it.
        """
        grid = _make_grid(standard_pitch_rules)
        pth_pad = Pad(
            x=0.0,
            y=0.0,
            width=1.7,
            height=1.7,
            net=0,
            net_name="GND",
            ref="J1",
            pin="2",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
        )
        grid.add_pad(pth_pad)

        # PTH pad metal extends to x=0.85; standard envelope to ~1.15.
        # A cell at x=1.0 (inside standard envelope) is blocked on both
        # layers by the standard PTH pad-add path (NOT by the via halo).
        for layer in (Layer.F_CU, Layer.B_CU):
            gx, gy = grid.world_to_grid(1.0, 0.0)
            assert grid.is_blocked(gx, gy, layer, net=9), (
                f"PTH plane-pad standard envelope must extend to {layer.value} (not just F.Cu)."
            )
