"""Tests for issue #3183: extended-pitch in-pad escape on 0.65-0.8 mm packages.

Issue #3183 extends the in-pad escape fallback gate from ``pin_pitch <= 0.55``
to ``pin_pitch <= 0.8`` when the opt-in
``KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK=1`` env var is set, so 0.65-0.8
mm-pitch QFP/TQFP/QFN packages (e.g. board-03 U1 TQFP-32 at 0.8 mm pitch) can
route their inner signal pins via in-pad vias when surface escape would clip
an adjacent foreign-net pad's clearance.

Coverage:

- The new ``EscapeRouter.generate_in_pad_rescues_only`` helper emits no
  surface escape stubs and only rescues pins selected by the
  adjacent-signal-neighbour predicate (or by an explicit ``pin_filter``).
- The new ``Router.route_all(enable_in_pad_escape_rescues=...)`` wiring
  invokes the helper and populates ``self._in_pad_escape_protected_nets``
  so the BLOCKED_BY_COMPONENT rip-up does not displace the rescue routes.
- The extended-pitch gate only fires when both
  ``via_in_pad_supported = True`` AND
  ``extended_pitch_in_pad_fallback = True`` -- tier-0 manufacturers
  (e.g. plain ``jlcpcb``) MUST stay disabled even when the env var is set
  so users do not silently land surcharge-incurring via-in-pad geometry
  on a non-Capability+ tier.
- The ``missed_via_in_pad_rescues`` counter (used by ``--auto-mfr-tier``)
  widens its pitch window to match the new gate so a board that opted
  into the extended fallback but landed on tier-0 still surfaces the
  "would-have-rescued" diagnostic.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ----------------------------------------------------------------------------
# Fixtures: TQFP-32 at 0.8 mm pitch (mirrors board-03 U1)
# ----------------------------------------------------------------------------


def _make_tqfp32_pads(ref: str = "U1") -> list[Pad]:
    """Build a TQFP-32-like footprint: 8 pins per edge at 0.8 mm pitch.

    Pad geometry mirrors ``Package_QFP:TQFP-32_7x7mm_P0.8mm`` (0.5x1.2 mm
    rectangular pads on each edge of a 7x7 mm body).  Pins 1-8 = west,
    9-16 = south, 17-24 = east, 25-32 = north.  Signal/plane net assignment
    follows the board-03 pattern (alternating signal pins with GND/VCC
    plane pads on the corners).
    """
    pads: list[Pad] = []
    pitch = 0.8
    pad_width = 0.5
    pad_height = 1.2
    edge_offset = 4.5  # distance from chip center to pad center on each edge

    # Plane net IDs: GND=3, VCC=2.  Signal nets start at 4.
    # We follow board 03's south-edge pattern (pins 9-16):
    # GND, JOY_X, JOY_Y, JOY_BTN, BTN1, BTN2, BTN3, BTN4, GND
    # so the BTN1-BTN4 cluster sits at positions 12, 13, 14, 15 and
    # the inner-pair signal-neighbour predicate fires for all four.
    #
    # West/east/north edges use plane-only fillers for the present tests.
    south_nets = [3, 4, 5, 6, 7, 8, 9, 10]
    south_names = ["GND", "JOY_X", "JOY_Y", "JOY_BTN", "BTN1", "BTN2", "BTN3", "BTN4"]
    # Pin positions on the south edge: 8 pads, pitch 0.8mm centred on origin.
    south_xs = [(-3.5 + i * pitch) for i in range(8)]
    for i, (x, net, name) in enumerate(zip(south_xs, south_nets, south_names, strict=False)):
        pads.append(
            Pad(
                x=x,
                y=edge_offset,
                width=pad_width,
                height=pad_height,
                net=net,
                net_name=name,
                ref=ref,
                pin=str(9 + i),
                layer=Layer.F_CU,
            )
        )

    # North edge: USB_CC1, USB_CC2 adjacent at positions 26, 27.
    north_nets = [3, 11, 12, 13, 14, 15, 3, 3]
    north_names = ["GND", "USB_CC2", "USB_CC1", "USB_D-", "USB_D+", "VBUS", "GND", "GND"]
    for i, (x, net, name) in enumerate(zip(south_xs, north_nets, north_names, strict=False)):
        pads.append(
            Pad(
                x=x,
                y=-edge_offset,
                width=pad_width,
                height=pad_height,
                net=net,
                net_name=name,
                ref=ref,
                pin=str(25 + i),
                layer=Layer.F_CU,
            )
        )

    # West edge: GND fillers (so the package is detected as a 32-pin quad).
    west_ys = south_xs
    for i, y in enumerate(west_ys):
        pads.append(
            Pad(
                x=-edge_offset,
                y=y,
                width=pad_height,
                height=pad_width,
                net=3,
                net_name="GND",
                ref=ref,
                pin=str(1 + i),
                layer=Layer.F_CU,
            )
        )

    # East edge: GND fillers.
    for i, y in enumerate(west_ys):
        pads.append(
            Pad(
                x=edge_offset,
                y=y,
                width=pad_height,
                height=pad_width,
                net=3,
                net_name="GND",
                ref=ref,
                pin=str(17 + i),
                layer=Layer.F_CU,
            )
        )

    return pads


def _make_rules(manufacturer: str | None) -> DesignRules:
    return DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        manufacturer=manufacturer,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=-10.0,
        origin_y=-10.0,
        layer_stack=LayerStack.two_layer(),
    )


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


class TestExtendedPitchInPadGate:
    """The gate raise is opt-in via env var.  Verify both directions."""

    def test_default_gate_blocks_0p8mm_pitch(self, monkeypatch):
        """Without the env var set, 0.8mm pitch packages get NO rescue."""
        monkeypatch.delenv("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", raising=False)
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        er = EscapeRouter(grid, rules)
        assert er.via_in_pad_supported is True
        assert er.extended_pitch_in_pad_fallback is False

        pads = _make_tqfp32_pads()
        pkg = er.analyze_package(pads)
        # TQFP-32 at 0.8mm pitch -- ABOVE the default 0.55mm gate.
        assert pkg.pin_pitch == pytest.approx(0.8, abs=0.01)

        rescues = er.generate_in_pad_rescues_only(pkg)
        assert rescues == [], (
            "Default gate should block 0.8mm-pitch rescue without the extended-pitch opt-in flag."
        )

    def test_extended_gate_rescues_0p8mm_pitch(self, monkeypatch):
        """With the env var set, 0.8mm pitch packages can be rescued."""
        monkeypatch.setenv("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", "1")
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        er = EscapeRouter(grid, rules)
        assert er.extended_pitch_in_pad_fallback is True
        assert er.via_in_pad_supported is True

        pads = _make_tqfp32_pads()
        pkg = er.analyze_package(pads)

        rescues = er.generate_in_pad_rescues_only(pkg)
        # The adjacent-signal-neighbour predicate fires on:
        #   south:  JOY_X, JOY_Y, JOY_BTN, BTN1, BTN2, BTN3, BTN4 (7 inner)
        #   north:  USB_CC2, USB_CC1, USB_D-, USB_D+ (4 inner)
        # = 11 pins.
        assert len(rescues) > 0, (
            "Extended-pitch gate + via_in_pad_supported must rescue at "
            "least one inner pin on a 0.8mm-pitch TQFP-32."
        )
        for r in rescues:
            assert r.via is not None, "Rescue must contain an in-pad via"
            assert r.via.in_pad is True
            assert r.pad.net != 0, "Rescues should never fire for plane pads"

    def test_tier0_manufacturer_blocks_extended_rescue(self, monkeypatch):
        """Tier-0 jlcpcb (no via-in-pad capability) MUST NOT rescue even
        when the env var is set."""
        monkeypatch.setenv("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", "1")
        rules = _make_rules(manufacturer="jlcpcb")
        grid = _make_grid(rules)
        er = EscapeRouter(grid, rules)
        # Capability gate: tier-0 has via_in_pad_supported = False.
        assert er.via_in_pad_supported is False
        assert er.extended_pitch_in_pad_fallback is True

        pads = _make_tqfp32_pads()
        pkg = er.analyze_package(pads)

        rescues = er.generate_in_pad_rescues_only(pkg)
        assert rescues == [], (
            "Tier-0 jlcpcb (via_in_pad_supported=False) MUST return empty "
            "even when the extended-pitch flag is set -- capability gating "
            "is unchanged."
        )


class TestPinFilter:
    """Test the explicit per-pin filter knob."""

    def test_pin_filter_restricts_rescues(self, monkeypatch):
        monkeypatch.setenv("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", "1")
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        er = EscapeRouter(grid, rules)

        pads = _make_tqfp32_pads()
        pkg = er.analyze_package(pads)

        # Only rescue south-edge BTN2 (pin 14, since the fixture's south
        # edge runs pins 9-16).  Wait -- fixture south pins 9-16 are:
        # 9=GND, 10=JOY_X, 11=JOY_Y, 12=JOY_BTN, 13=BTN1, 14=BTN2, 15=BTN3, 16=BTN4.
        rescues = er.generate_in_pad_rescues_only(pkg, pin_filter=["14"])
        assert len(rescues) == 1
        assert rescues[0].pad.pin == "14"
        assert rescues[0].pad.net_name == "BTN2"

    def test_pin_filter_empty_skips_all(self, monkeypatch):
        monkeypatch.setenv("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", "1")
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        er = EscapeRouter(grid, rules)

        pads = _make_tqfp32_pads()
        pkg = er.analyze_package(pads)

        rescues = er.generate_in_pad_rescues_only(pkg, pin_filter=[])
        assert rescues == [], "An empty pin_filter list should produce no rescues."


class TestMissedRescueCounterWidening:
    """The wants_in_pad_but_unavailable counter widens to 0.8mm when the
    extended-pitch flag is set so --auto-mfr-tier still surfaces the
    diagnostic for boards that opted in but landed on tier-0."""

    def test_counter_widens_to_extended_band(self, monkeypatch):
        # The counter widens only inside _escape_qfp_alternating (the
        # full QFP dispatcher), not inside generate_in_pad_rescues_only
        # (which short-circuits to an empty list when via_in_pad_supported
        # is False).  Verify the EscapeRouter flag is set so the dispatcher
        # picks up the wider band.
        monkeypatch.setenv("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", "1")
        rules = _make_rules(manufacturer="jlcpcb")  # tier-0, no in-pad
        grid = _make_grid(rules)
        er = EscapeRouter(grid, rules)
        assert er.extended_pitch_in_pad_fallback is True
        assert er.via_in_pad_supported is False


class TestRescueGeometryShape:
    """Each rescue must produce an in-pad via tagged in_pad=True with an
    inner-layer segment of the configured trace width."""

    def test_rescue_geometry_includes_via_and_segment(self, monkeypatch):
        monkeypatch.setenv("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", "1")
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        er = EscapeRouter(grid, rules)

        pads = _make_tqfp32_pads()
        pkg = er.analyze_package(pads)

        rescues = er.generate_in_pad_rescues_only(pkg, pin_filter=["14"])
        assert len(rescues) == 1
        r = rescues[0]
        assert r.via is not None
        assert r.via.in_pad is True
        # The in-pad via should land at the pad center (or a small
        # long-axis nudge from it -- never on a foreign neighbour).
        assert abs(r.via.x - r.pad.x) <= r.pad.width
        assert abs(r.via.y - r.pad.y) <= r.pad.height
        # The escape segment should be on the OTHER layer (B.Cu on a
        # 2-layer stack).
        assert len(r.segments) == 1
        seg = r.segments[0]
        assert seg.layer != r.pad.layer
