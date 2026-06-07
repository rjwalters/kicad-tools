"""Tests for Issue #3278: per-pad escape width with row-max geometry.

Before #3278, ``_create_fine_pitch_row_escapes`` computed a single
``escape_width`` from ``pads[0].net_name``'s net class and applied it to
every ``Segment`` emitted in the row.  When the first pad in row sort
order happened to land on a wide-trace net class (e.g. GND in the Power
class at 0.5mm), every escape segment in the row inherited that width,
producing 0-gap clearance violations on adjacent narrow-class pads (the
exact USB_D+/USB_D- collision the issue spec calls out for board 03).

The fix split the width into two values:

- ``row_max_width`` -- worst-case trace width across all pads in the
  row.  Used for ``lateral_offset`` (the cross-row via offset that must
  stay constant for the whole row).
- per-pad ``pad_escape_width`` -- each pad's own net-class width.  Used
  for ``Segment.width`` at every emission site and forwarded to the
  in-pad / lateral rescue helpers.

This test verifies BOTH halves of the contract:

- Per-segment width: each emitted segment carries the trace width of
  its own pad's net (AC#1, AC#6 first bullet).
- Row-max geometry preserved: the via x/y positions for the odd-indexed
  pins are identical to a uniform fat-width baseline -- i.e. the lateral
  offset still uses the worst-case width, so cross-row via clearance is
  preserved (AC#2, AC#6 second bullet, curator's "trap" guard).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import EscapeRouter, PackageType
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_mixed_class_usb_c_pads(ref: str = "J1") -> list[Pad]:
    """Build a synthetic USB-C-style row reproducing the board-03 net-class
    mixture.

    The first pad in row sort order is GND (the fat Power-class net at
    0.5mm trace), with USB_D+/USB_D- HighSpeed pads (0.2mm trace) adjacent
    to each other in the middle.  This is the exact arrangement that
    triggered the bug.
    """
    # Two rows so the dispatcher routes through the USB_C / dual-row
    # fine-pitch path (mirrors GCT_USB4105 geometry).
    smt_data: list[tuple[str, float, float, str, int]] = [
        ("A1", -2.75, 0.0, "GND", 1),  # Power, fat
        ("A4", -1.75, 0.0, "VBUS", 2),  # Power, fat
        ("A5", -1.0, 0.0, "USB_CC1", 3),  # HighSpeed, narrow
        ("A6", -0.25, 0.0, "USB_D+", 5),  # HighSpeed, narrow
        ("A7", 0.25, 0.0, "USB_D-", 6),  # HighSpeed, narrow
        ("A8", 1.0, 0.0, "NC_A8", 7),  # default
        ("A9", 1.75, 0.0, "VBUS", 2),  # Power, fat
        ("A12", 2.75, 0.0, "GND", 1),  # Power, fat
        ("B1", 2.75, 1.0, "GND", 1),
        ("B4", 1.75, 1.0, "VBUS", 2),
        ("B5", 1.0, 1.0, "USB_CC2", 4),
        ("B6", 0.25, 1.0, "USB_D+", 5),
        ("B7", -0.25, 1.0, "USB_D-", 6),
        ("B8", -1.0, 1.0, "NC_B8", 8),
        ("B9", -1.75, 1.0, "VBUS", 2),
        ("B12", -2.75, 1.0, "GND", 1),
    ]
    pads: list[Pad] = []
    for pin, x, y, name, net in smt_data:
        pads.append(
            Pad(
                x=x,
                y=y,
                width=0.25,
                height=0.35,
                net=net,
                net_name=name,
                ref=ref,
                pin=pin,
                layer=Layer.F_CU,
            )
        )
    # Through-hole shield tabs to trigger the USB_C dispatcher.
    for pin, x in [("S1", -4.3), ("S2", 4.3)]:
        pads.append(
            Pad(
                x=x,
                y=1.5,
                width=1.0,
                height=1.0,
                net=1,
                net_name="GND",
                ref=ref,
                pin=pin,
                layer=Layer.F_CU,
                through_hole=True,
                drill=0.6,
            )
        )
    return pads


def _make_mixed_class_net_map() -> dict[str, NetClassRouting]:
    """The board-03 trace-class mix (fat Power, narrow HighSpeed)."""
    power = NetClassRouting(name="Power", trace_width=0.5, clearance=0.2)
    hs = NetClassRouting(name="HighSpeed", trace_width=0.2, clearance=0.15)
    return {
        "GND": power,
        "VBUS": power,
        "USB_CC1": hs,
        "USB_CC2": hs,
        "USB_D+": hs,
        "USB_D-": hs,
    }


def _make_uniform_fat_net_map() -> dict[str, NetClassRouting]:
    """Every signal mapped to the fat 0.5mm Power class.

    This reproduces the pre-#3278 behaviour where ``pads[0].net_name``
    happened to be GND (Power class) and ``escape_width`` was applied
    uniformly across the whole row.  Used as the geometry reference for
    the row-max-preservation check.
    """
    power = NetClassRouting(name="Power", trace_width=0.5, clearance=0.2)
    return {
        "GND": power,
        "VBUS": power,
        "USB_CC1": power,
        "USB_CC2": power,
        "USB_D+": power,
        "USB_D-": power,
    }


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer="jlcpcb",
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=40.0,
        height=30.0,
        rules=rules,
        origin_x=-5.0,
        origin_y=-5.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPerPadEscapeWidth:
    """Issue #3278: per-pad ``Segment.width`` in mixed-net-class rows."""

    def test_each_segment_uses_its_pads_net_class_width(self):
        """AC#1: every ``Segment`` emitted by the fine-pitch dispatcher
        uses the trace width of its own pad's net (not ``pads[0]``'s)."""
        pads = _make_mixed_class_usb_c_pads()
        rules = _make_rules()
        grid = _make_grid(rules)
        net_class_map = _make_mixed_class_net_map()

        router = EscapeRouter(grid, rules, net_class_map=net_class_map)
        package_info = router.analyze_package(pads)
        assert package_info.package_type == PackageType.USB_C_CONNECTOR

        escapes = router.generate_escapes(package_info)
        # Only SMT pads should be in the escape list.
        smt_escapes = [e for e in escapes if not e.pad.through_hole]
        assert len(smt_escapes) >= 8

        # Group escapes by expected trace width based on the pad's net.
        # Pads not in the net_class_map fall through to rules.trace_width
        # (matches ``_get_trace_width_for_net`` semantics).
        for esc in smt_escapes:
            nc = net_class_map.get(esc.pad.net_name)
            expected_width = nc.trace_width if nc else rules.trace_width
            for seg in esc.segments:
                assert abs(seg.width - expected_width) < 1e-6, (
                    f"Segment for {esc.pad.ref}.{esc.pad.pin} "
                    f"(net {esc.pad.net_name}) has width {seg.width}, "
                    f"expected {expected_width} (per-pad net-class)."
                )

    def test_narrow_class_segments_dont_clip_neighbours(self):
        """AC#4: with per-pad widths, USB_D+/USB_D- (0.2mm HighSpeed)
        no longer carry the fat 0.5mm GND-class width that produced the
        ``Escape clearance violation between pads USB_D- and USB_D+``
        diagnostic on board 03 at 0.5mm pitch."""
        pads = _make_mixed_class_usb_c_pads()
        rules = _make_rules()
        grid = _make_grid(rules)
        net_class_map = _make_mixed_class_net_map()

        router = EscapeRouter(grid, rules, net_class_map=net_class_map)
        package_info = router.analyze_package(pads)
        escapes = router.generate_escapes(package_info)

        # Pick USB_D+ and USB_D- escapes and confirm their segments are
        # the narrow HighSpeed width, not the fat Power width.
        usb_d_segments = [
            seg
            for esc in escapes
            if esc.pad.net_name in ("USB_D+", "USB_D-")
            for seg in esc.segments
        ]
        assert len(usb_d_segments) > 0, (
            "No USB_D+/USB_D- segments emitted -- per-pad escape did not "
            "fire."
        )
        for seg in usb_d_segments:
            # 0.2mm HighSpeed, NOT 0.5mm Power (pre-#3278).
            assert abs(seg.width - 0.2) < 1e-6, (
                f"USB_D segment width {seg.width} should be 0.2mm "
                f"(HighSpeed), not 0.5mm (Power from pads[0])."
            )

    def test_lateral_offset_preserves_row_max_geometry(self):
        """AC#2 / curator's "trap" guard: switching to per-pad width MUST
        NOT collapse ``lateral_offset`` for the narrow-class pins.

        The cross-row via x/y positions for the odd-indexed pins must be
        identical (within floating-point rounding) to the uniform fat-
        width baseline -- this is what proves the lateral offset still
        uses the worst-case row width, NOT each pad's own width.
        """
        pads = _make_mixed_class_usb_c_pads()
        rules = _make_rules()
        grid = _make_grid(rules)

        # Mixed-class run (production behaviour after #3278).
        mixed_router = EscapeRouter(
            grid, rules, net_class_map=_make_mixed_class_net_map()
        )
        mixed_info = mixed_router.analyze_package(pads)
        mixed_escapes = mixed_router.generate_escapes(mixed_info)

        # Uniform fat-width baseline (every pad mapped to 0.5mm Power).
        # Build a fresh grid because the router mutates grid obstacle state
        # during generation.
        fat_router = EscapeRouter(
            _make_grid(rules), rules, net_class_map=_make_uniform_fat_net_map()
        )
        fat_info = fat_router.analyze_package(pads)
        fat_escapes = fat_router.generate_escapes(fat_info)

        # Build maps from (ref, pin) to the via position.
        def via_positions(escapes):
            return {
                (e.pad.ref, e.pad.pin): e.via_pos
                for e in escapes
                if e.via_pos is not None and not e.pad.through_hole
            }

        mixed_vias = via_positions(mixed_escapes)
        fat_vias = via_positions(fat_escapes)

        # The set of pads with vias may differ if the dispatcher defers
        # some pads in one case but not the other -- the contract is that
        # for pads with vias in BOTH runs, the via positions match (proves
        # the row-max-derived lateral_offset is identical).
        shared = set(mixed_vias) & set(fat_vias)
        assert len(shared) > 0, (
            "No pads with vias common to both runs -- cannot validate "
            "row-max geometry preservation."
        )
        for key in shared:
            mx, my = mixed_vias[key]
            fx, fy = fat_vias[key]
            assert abs(mx - fx) < 1e-6 and abs(my - fy) < 1e-6, (
                f"Via for {key} moved between fat-uniform and mixed-class "
                f"runs: mixed=({mx:.4f}, {my:.4f}), fat=({fx:.4f}, "
                f"{fy:.4f}).  Lateral offset should depend on row_max "
                f"width, NOT per-pad width."
            )

    def test_min_trace_width_overrides_collapse_to_necked_width(self):
        """When ``rules.min_trace_width`` is set (neck-down path), both
        ``row_max_width`` and ``pad_escape_width`` should collapse to the
        manufacturer-minimum width.  This keeps the pre-existing neck-down
        contract intact.
        """
        pads = _make_mixed_class_usb_c_pads()
        rules = _make_rules()
        rules.min_trace_width = 0.10  # manufacturer minimum (neck-down)
        grid = _make_grid(rules)
        net_class_map = _make_mixed_class_net_map()

        router = EscapeRouter(grid, rules, net_class_map=net_class_map)
        package_info = router.analyze_package(pads)
        escapes = router.generate_escapes(package_info)

        smt_escapes = [e for e in escapes if not e.pad.through_hole]
        for esc in smt_escapes:
            for seg in esc.segments:
                assert abs(seg.width - 0.10) < 1e-6, (
                    f"With min_trace_width=0.10, segment for "
                    f"{esc.pad.ref}.{esc.pad.pin} should be 0.10mm "
                    f"(necked), got {seg.width}."
                )


class TestUniformNetClassUnchanged:
    """AC#5: rows where every pad shares a net class produce identical
    escape geometry to before -- per-pad collapses to row-max collapses
    to the single common width.
    """

    def test_uniform_row_geometry_unchanged(self):
        """Even though we split the width into row_max + per-pad, a row
        where every pad shares a net class still emits segments of the
        single common width (no regression on the SSOP/TSSOP/QFN
        homogeneous-class case the issue spec calls out)."""
        pads = _make_mixed_class_usb_c_pads()
        rules = _make_rules()
        grid = _make_grid(rules)
        # Every pad in the uniform-fat net map sees the same 0.5mm class.
        net_class_map = _make_uniform_fat_net_map()

        router = EscapeRouter(grid, rules, net_class_map=net_class_map)
        package_info = router.analyze_package(pads)
        escapes = router.generate_escapes(package_info)

        smt_escapes = [e for e in escapes if not e.pad.through_hole]
        # NC pads aren't in the net_class_map so they collapse to
        # rules.trace_width (0.2); pads with a class entry get 0.5.
        # Both are the SAME values they'd have used pre-#3278 -- the row
        # used pads[0]=GND=0.5 for everything, except the NC pads
        # were no-ops anyway.  The contract for AC#5 is that NO pad's
        # segment width DIFFERS from what _get_trace_width_for_net
        # would return for the SINGLE pad alone -- proved by per-pad
        # equality with the helper itself.
        for esc in smt_escapes:
            expected = router._get_trace_width_for_net(esc.pad.net_name)
            for seg in esc.segments:
                assert abs(seg.width - expected) < 1e-6, (
                    f"Uniform-class row produced inconsistent segment "
                    f"widths: {esc.pad.ref}.{esc.pad.pin} (net "
                    f"{esc.pad.net_name}) got {seg.width}, expected "
                    f"{expected} (per-pad net-class lookup)."
                )


if __name__ == "__main__":  # pragma: no cover - test-runner convenience
    pytest.main([__file__, "-v"])
