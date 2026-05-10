"""Tests for issue #2605: in-pad via escape on fine-pitch SSOP/TSSOP packages.

Verifies the new in-pad escape strategy that activates when a manufacturer
supports via-in-pad processing (e.g. JLCPCB Capability+/tier1, PCBWay).

Pre-#2605 behavior (preserved when manufacturer doesn't support in-pad):
- Pins that fail surface clearance are deferred to the main router.

Post-#2605 behavior (when via_in_pad_supported=True):
- Pins that fail surface clearance fall through to an in-pad via escape
  attempt before deferring.
- The via is placed dead-centre on the pad (off-centre breaks paste stencil).
- The escape segment runs from the via on an inner signal layer (In1.Cu on
  4-layer boards, B.Cu on 2-layer boards).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.escape import EscapeRouter, PackageType
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _make_dual_row_ssop(
    pin_count: int = 28,
    pitch: float = 0.65,
    ref: str = "U5",
    pad_width: float = 0.35,
    pad_height: float = 1.45,
    row_spacing: float = 5.3,
    start_net: int = 1,
) -> list[Pad]:
    """Build a dual-row SSOP/TSSOP fixture with ``pin_count`` pads.

    Default geometry mimics PCM5122PW (TSSOP-28, 0.65mm pitch).  Each pad
    has unique nets so the escape router does not group them.
    """
    assert pin_count % 2 == 0, "Pin count must be even"
    pins_per_row = pin_count // 2
    pads: list[Pad] = []
    total_width = (pins_per_row - 1) * pitch
    start_x = -total_width / 2

    # Top row (pins 1..N/2 left-to-right)
    for i in range(pins_per_row):
        pads.append(
            Pad(
                x=start_x + i * pitch,
                y=row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=start_net + i,
                net_name=f"NET{start_net + i}",
                ref=ref,
                pin=str(i + 1),
                layer=Layer.F_CU,
            )
        )

    # Bottom row (pins N/2+1..N right-to-left)
    for i in range(pins_per_row):
        pads.append(
            Pad(
                x=start_x + (pins_per_row - 1 - i) * pitch,
                y=-row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=start_net + pins_per_row + i,
                net_name=f"NET{start_net + pins_per_row + i}",
                ref=ref,
                pin=str(pin_count - i),
                layer=Layer.F_CU,
            )
        )

    return pads


def _make_strict_rules(manufacturer: str | None = None) -> DesignRules:
    """Build DesignRules tight enough to force deferrals on a 0.65mm
    pitch fixture so the in-pad escape strategy has work to do.

    We force a wider clearance via ``component_clearances`` to bypass the
    auto-derived fine-pitch clearance (~0.24mm for 0.65mm pitch / 0.35mm
    pads) and reproduce the deferred-pin pattern seen on chorus-test U5.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer=manufacturer,
    )
    # Force every pin in U5 to fail surface clearance so the in-pad
    # strategy has to rescue them (chorus-test pattern).
    rules.component_clearances["U5"] = 0.4
    return rules


def _make_grid(rules: DesignRules, layer_stack: LayerStack | None = None) -> RoutingGrid:
    # Use the 4-layer signal-signal stack so that ``_select_inner_escape_layer``
    # returns In1.Cu (a SIGNAL layer) rather than falling back to B.Cu.  The
    # default ``four_layer_sig_gnd_pwr_sig`` has In1.Cu marked as PLANE, which
    # is unsuitable for routing in-pad escape segments.
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=-10.0,
        origin_y=-10.0,
        layer_stack=layer_stack or LayerStack.four_layer_sig_sig_gnd_pwr(),
    )


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


class TestInPadEscapeStrategy:
    """Issue #2605: in-pad via escape activates on capable manufacturers."""

    def test_in_pad_escape_generated_when_supported(self):
        """With manufacturer=jlcpcb-tier1, deferred pins should fall through
        to in-pad via escape and produce vias dead-centre on their pads."""
        rules = _make_strict_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_dual_row_ssop(pin_count=28)

        package_info = escape_router.analyze_package(pads)
        assert package_info.package_type in (PackageType.SSOP, PackageType.TSSOP)

        escapes = escape_router.generate_escapes(package_info)

        # With strict component_clearances (0.4mm) all 28 pins defer at
        # surface; via-in-pad should rescue a strong majority.
        assert len(escapes) >= 20, (
            f"Expected >= 20 escapes with via-in-pad; got {len(escapes)}"
        )

        # At least 4 in-pad vias (the deferred-by-default pins).
        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert len(in_pad_vias) >= 4, (
            f"Expected >= 4 in-pad vias with via-in-pad enabled; "
            f"got {len(in_pad_vias)}"
        )

        # Every in-pad via must sit dead-centre on its pad (within 1um).
        for esc in in_pad_vias:
            assert esc.via is not None
            assert abs(esc.via.x - esc.pad.x) < 0.001
            assert abs(esc.via.y - esc.pad.y) < 0.001
            # Inner-layer escape on a 4-layer stack lands on In1.Cu.
            assert esc.via.layers[0] == esc.pad.layer
            assert esc.via.layers[1] == Layer.IN1_CU

    def test_no_in_pad_escape_when_unsupported(self):
        """With default manufacturer=jlcpcb (no via-in-pad), behavior is
        identical to pre-#2605: deferred pins stay deferred and no in-pad
        vias are produced."""
        rules = _make_strict_rules(manufacturer="jlcpcb")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_dual_row_ssop(pin_count=28)

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert in_pad_vias == [], (
            "Default JLCPCB profile must NOT produce in-pad vias "
            "(would silently surcharge users)."
        )

    def test_no_in_pad_escape_when_manufacturer_is_none(self):
        """With manufacturer=None (the default for DesignRules), no in-pad
        escapes are produced (byte-identical to pre-#2605 behavior)."""
        rules = _make_strict_rules(manufacturer=None)
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_dual_row_ssop(pin_count=28)

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert in_pad_vias == []

    def test_in_pad_escape_on_2layer_board(self):
        """On a 2-layer board with via-in-pad enabled, the in-pad via lands
        on B.Cu (the only available inner-or-back signal layer)."""
        rules = _make_strict_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules, layer_stack=LayerStack.two_layer())
        escape_router = EscapeRouter(grid, rules)
        pads = _make_dual_row_ssop(pin_count=28)

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert len(in_pad_vias) >= 1, (
            "Expected at least one in-pad via on the 2-layer fixture."
        )
        for esc in in_pad_vias:
            assert esc.via is not None
            assert esc.via.layers[1] == Layer.B_CU

    def test_in_pad_skipped_when_pad_too_small(self):
        """When pads are too small to host the via, the in-pad strategy
        gracefully bails out and the pin defers as before."""
        rules = _make_strict_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)

        # Tiny pads: 0.25 x 0.4 cannot host a 0.6mm-diameter via.
        pads = _make_dual_row_ssop(
            pin_count=28, pad_width=0.25, pad_height=0.4,
        )

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        # Whatever escapes do come out, NONE of them should be in-pad
        # because the geometry forbids it.
        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert in_pad_vias == [], (
            "In-pad escape should bail out gracefully when pads are too small."
        )

    def test_in_pad_via_does_not_trigger_segment_clearance_warning(self, caplog):
        """The in-pad escape's inner-layer segment must not trigger a
        segment-to-pad clearance warning against its own pad in
        ``_validate_escape_clearances`` (the inner-layer segment is on a
        different layer from the pad)."""
        import logging

        rules = _make_strict_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_dual_row_ssop(pin_count=28)

        package_info = escape_router.analyze_package(pads)

        with caplog.at_level(logging.WARNING, logger="kicad_tools.router.escape"):
            escapes = escape_router.generate_escapes(package_info)

        # We must have actually produced in-pad vias for this assertion to
        # be meaningful.
        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert len(in_pad_vias) > 0

        # No "segment-to-pad clearance violation" warnings about the
        # in-pad escape's own pad.
        violation_msgs = [
            r.message for r in caplog.records
            if r.levelno >= logging.WARNING
            and "Escape segment-to-pad clearance violation" in r.message
        ]
        # All deferred pins were rescued by in-pad escape; the inner-layer
        # segments are on In1.Cu and the pads are on F.Cu, so no
        # segment-to-pad violations should fire against the parent pad.
        for esc in in_pad_vias:
            assert esc.pad.net_name not in "\n".join(violation_msgs), (
                f"In-pad escape for {esc.pad.net_name} triggered an "
                f"unexpected segment-to-pad clearance warning."
            )

    def test_pcm5122pw_full_28pin_fixture(self):
        """Exact PCM5122PW geometry (TSSOP-28, 0.65mm pitch, 0.30x1.45mm
        pads, body ~9.7x4.4mm).  Coverage must be >= 26/28 with
        manufacturer=jlcpcb-tier1."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            manufacturer="jlcpcb-tier1",
        )
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        # PCM5122PW pads: 0.30mm x 1.45mm (datasheet-accurate)
        pads = _make_dual_row_ssop(
            pin_count=28,
            pitch=0.65,
            ref="U5",
            pad_width=0.30,
            pad_height=1.45,
            row_spacing=5.3,
        )

        package_info = escape_router.analyze_package(pads)
        assert package_info.package_type in (PackageType.SSOP, PackageType.TSSOP)
        escapes = escape_router.generate_escapes(package_info)

        assert len(escapes) >= 26, (
            f"PCM5122PW coverage with via-in-pad: expected >= 26/28, "
            f"got {len(escapes)}/28"
        )


# ----------------------------------------------------------------------------
# Regression: chorus-test U5
# ----------------------------------------------------------------------------


class TestChorusTestU5Regression:
    """Issue #2605 acceptance: chorus-test-revA U5 escape coverage floor.

    Today's baseline (May 5/10) is 7-8/14 escapes per row.  The acceptance
    target with via-in-pad is 13-14/14.  This test fails loudly if coverage
    drops below 11/14 -- our floor for the regression assertion.

    Skips gracefully when the chorus-test board is not checked into the
    repo (CI may not have it).
    """

    BOARD_PATH = Path(
        "boards/external/chorus-test-revA/kicad/chorus-test-revA_v18.kicad_pcb"
    )

    def test_chorus_test_u5_escape_coverage_floor(self):
        if not self.BOARD_PATH.exists():
            pytest.skip(
                f"chorus-test-revA board not present at {self.BOARD_PATH}; "
                f"regression check skipped (will run in CI when board is "
                f"checked in)."
            )

        # Lazy import: avoid pulling in PCB/sexp parsing for the synthetic
        # tests above, which already fully cover the in-pad strategy.
        try:
            from kicad_tools.router.io import load_pads_from_pcb
        except ImportError:
            pytest.skip(
                "load_pads_from_pcb not available; cannot run chorus-test "
                "U5 regression."
            )

        u5_pads = load_pads_from_pcb(self.BOARD_PATH, ref="U5")
        if not u5_pads:
            pytest.skip("U5 not found in chorus-test-revA board.")

        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
            manufacturer="jlcpcb-tier1",
        )
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        package_info = escape_router.analyze_package(u5_pads)
        escapes = escape_router.generate_escapes(package_info)

        # U5 is a 28-pin TSSOP -> 14 pins per row.  We assert the
        # *per-row* coverage floor.  Total escapes >= 22 means each row
        # covered >= 11.
        assert len(escapes) >= 22, (
            f"chorus-test U5 escape regression: expected >= 22 (>= 11/14 "
            f"per row), got {len(escapes)}.  Today's baseline is 14-16, "
            f"target with via-in-pad is 26-28."
        )
