"""End-to-end integration tests for the fine-pitch escape ladder (P_FP3).

Issue #3371 / Phase 3 (P_FP3) -- verifies the wiring between
:func:`kicad_tools.router.io.load_pcb_for_routing` and
:meth:`RoutingGrid.set_fine_pitch_regions` / the per-pad halo at
:meth:`RoutingGrid._clearance_for_pin_pitch`.  The detector + region
install runs inside ``load_pcb_for_routing`` (before any pad is added
to the grid) so the Python pathfinder halo and the C++ pad-segment
validator both pick up the in-region escape clearance.

What this module tests
======================

1. **Region installation lands on the grid** -- when ``load_pcb_for_routing``
   parses a PCB with a fine-pitch SOIC and the recipe parameters trip
   the Q_FP1 geometry predicate, the resulting Autorouter's grid has
   exactly one :class:`FinePitchRegion` per qualifying component.

2. **Pad halo shrinks for in-region pads** -- ``_clearance_for_pin_pitch``
   returns the region's escape halo (``escape_clearance + trace_width/2``)
   for pads inside a region, and the standard halo for pads outside.

3. **Impedance guard is preserved (PR #3273)** --
   :func:`resolve_clearance_with_escape_region` short-circuits to the
   default :meth:`DesignRules.get_clearance_for_component` lookup when
   the active net class declares any impedance target.

4. **Manufacturer fallback warning** -- when no manufacturer is
   configured, the detector still runs but the region's escape
   clearance falls back to ``rules.trace_clearance`` (no shrink).
   This is the documented Q_FP2 behaviour we want to surface to users.

The heavyweight smoke tests (softstart rev B end-to-end routing reach)
land in ``tests/router/test_softstart_revb_fine_pitch_escape.py`` per
P_FP5.  This file covers the integration *contracts* without invoking
the full pathfinder.

Issue: https://github.com/rjwalters/kicad-tools/issues/3371
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.fine_pitch_escape import (
    FinePitchRegion,
    detect_fine_pitch_regions,
    resolve_clearance_with_escape_region,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.mfr_limits import MFR_JLCPCB_TIER1
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

# ============================================================================
# Helpers -- synthetic UCC27211 SOIC-8 layout for the integration tests
# ============================================================================


def _ucc27211_pads(
    origin_x: float = 50.0,
    origin_y: float = 50.0,
    rotated_90: bool = False,
) -> list[Pad]:
    """Construct synthetic UCC27211 SOIC-8 pads (8 pins, 1.27mm pitch).

    The package is laid out as two rows of 4 pins each, 4mm apart in Y
    (typical SOIC-8 body width).  Pad geometry is 0.30 mm (along pitch)
    by 1.55 mm (across).  When ``rotated_90`` is True the layout is
    reflected so the pitch axis is Y (vertical row).

    All pads carry distinct net IDs so the synthetic board is "fully
    described" by the detector's standpoint; the actual values are
    immaterial for halo / clearance checks.
    """
    pads: list[Pad] = []
    for i in range(4):
        pin_top = str(i + 1)
        pin_bot = str(8 - i)
        if rotated_90:
            # Pitch axis Y -- pad height (along Y) is the narrow dim.
            x_left = origin_x - 2.0
            x_right = origin_x + 2.0
            pads.append(
                Pad(
                    x=x_left,
                    y=origin_y + i * 1.27,
                    width=1.55,
                    height=0.30,
                    net=10 + i,
                    net_name=f"NET{pin_top}",
                    layer=Layer.F_CU,
                    ref="U5",
                    pin=pin_top,
                )
            )
            pads.append(
                Pad(
                    x=x_right,
                    y=origin_y + i * 1.27,
                    width=1.55,
                    height=0.30,
                    net=20 + i,
                    net_name=f"NET{pin_bot}",
                    layer=Layer.F_CU,
                    ref="U5",
                    pin=pin_bot,
                )
            )
        else:
            # Pitch axis X -- pad width (along X) is the narrow dim.
            pads.append(
                Pad(
                    x=origin_x + i * 1.27,
                    y=origin_y - 2.0,
                    width=0.30,
                    height=1.55,
                    net=10 + i,
                    net_name=f"NET{pin_top}",
                    layer=Layer.F_CU,
                    ref="U5",
                    pin=pin_top,
                )
            )
            pads.append(
                Pad(
                    x=origin_x + i * 1.27,
                    y=origin_y + 2.0,
                    width=0.30,
                    height=1.55,
                    net=20 + i,
                    net_name=f"NET{pin_bot}",
                    layer=Layer.F_CU,
                    ref="U5",
                    pin=pin_bot,
                )
            )
    return pads


# ============================================================================
# Tests
# ============================================================================


class TestRegionInstallation:
    """Region installation on the grid lands the regions correctly."""

    def test_set_and_get_round_trip(self) -> None:
        """set_fine_pitch_regions + get_fine_pitch_regions round-trips."""
        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            grid_resolution=0.1,
            manufacturer="jlcpcb-tier1",
        )
        grid = RoutingGrid(
            width=100.0, height=100.0, rules=rules, layer_stack=LayerStack.two_layer()
        )

        pads = _ucc27211_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB_TIER1)
        assert len(regions) == 1, "Expected exactly one region for UCC27211 fixture"

        grid.set_fine_pitch_regions(regions)
        assert grid.get_fine_pitch_regions() == regions

    def test_empty_regions_byte_for_byte_unchanged(self) -> None:
        """Empty regions list means no behaviour change (back-compat)."""
        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(width=50.0, height=50.0, rules=rules, layer_stack=LayerStack.two_layer())

        # No regions installed -- default state.
        assert grid.get_fine_pitch_regions() == []

        # Pad with pin_pitch=0.65 (fine-pitch) -- halo follows the
        # legacy path (no escape-region branch).
        legacy_halo = grid._clearance_for_pin_pitch(pin_pitch=0.65)
        expected = rules.trace_clearance + rules.trace_width / 2
        # No min_trace_width configured -> standard halo.
        assert abs(legacy_halo - expected) < 1e-9


class TestPadHaloShrinkInRegion:
    """``_clearance_for_pin_pitch`` shrinks halo for pads inside a region."""

    def _make_grid_with_region(self) -> tuple[RoutingGrid, list[Pad]]:
        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            grid_resolution=0.1,
            manufacturer="jlcpcb-tier1",
        )
        grid = RoutingGrid(
            width=100.0, height=100.0, rules=rules, layer_stack=LayerStack.two_layer()
        )
        pads = _ucc27211_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB_TIER1)
        grid.set_fine_pitch_regions(regions)
        return grid, pads

    def test_in_region_pad_gets_escape_halo(self) -> None:
        grid, pads = self._make_grid_with_region()
        # Any of the UCC27211 pads -- pad.ref == 'U5' identity match.
        in_region_pad = pads[0]
        halo = grid._clearance_for_pin_pitch(pin_pitch=1.27, pad=in_region_pad)
        # Region's escape_clearance = jlcpcb-tier1 floor (0.127) + 0.013
        # safety margin = 0.140.  Halo = escape_clearance + trace_width/2.
        expected = 0.140 + grid.rules.trace_width / 2
        assert abs(halo - expected) < 1e-6, f"In-region halo {halo:.4f} != expected {expected:.4f}"

    def test_out_of_region_pad_uses_standard_halo(self) -> None:
        grid, _ = self._make_grid_with_region()
        # A foreign pad far from U5 -- not in any region and ref doesn't
        # match.  Should get the standard halo.
        foreign_pad = Pad(
            x=10.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=99,
            net_name="VCC",
            layer=Layer.F_CU,
            ref="R99",
            pin="1",
        )
        halo = grid._clearance_for_pin_pitch(pin_pitch=None, pad=foreign_pad)
        expected = grid.rules.trace_clearance + grid.rules.trace_width / 2
        assert abs(halo - expected) < 1e-6

    def test_pad_is_none_preserves_legacy_behaviour(self) -> None:
        """When pad=None (pre-P_FP3 callers), no region branch fires."""
        grid, _ = self._make_grid_with_region()
        # Even though regions are installed, pad=None means the helper
        # cannot test "is this pad in a region" -- the legacy path runs.
        halo = grid._clearance_for_pin_pitch(pin_pitch=None, pad=None)
        expected = grid.rules.trace_clearance + grid.rules.trace_width / 2
        assert abs(halo - expected) < 1e-6


class TestImpedanceGuardPreserved:
    """Impedance-controlled nets must NOT pick up the escape shrink."""

    def test_diff_impedance_net_bypasses_escape_clearance(self) -> None:
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20, manufacturer="jlcpcb-tier1")
        pads = _ucc27211_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB_TIER1)
        assert len(regions) == 1

        # In-region pad on an impedance-controlled net -> clearance must
        # NOT shrink (PR #3273 trap).
        impedance_class = NetClassRouting(
            name="USB_DP",
            trace_width=0.30,
            clearance=0.20,
            target_diff_impedance=90.0,
            escape_clearance=0.14,  # would shrink if guard fell through
        )
        clearance = resolve_clearance_with_escape_region(
            rules,
            pads[0],
            net_class=impedance_class,
            regions=regions,
        )
        # Must be the standard trace_clearance -- NOT 0.14.
        assert abs(clearance - rules.trace_clearance) < 1e-9, (
            f"Impedance guard failed: in-region impedance-controlled net "
            f"got clearance {clearance:.4f}, expected {rules.trace_clearance:.4f}"
        )

    def test_single_impedance_net_bypasses_escape_clearance(self) -> None:
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20, manufacturer="jlcpcb-tier1")
        pads = _ucc27211_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB_TIER1)
        single_class = NetClassRouting(
            name="ZL_50",
            trace_width=0.30,
            clearance=0.20,
            target_single_impedance=50.0,
            escape_clearance=0.14,
        )
        clearance = resolve_clearance_with_escape_region(
            rules,
            pads[0],
            net_class=single_class,
            regions=regions,
        )
        assert abs(clearance - rules.trace_clearance) < 1e-9


class TestQFNRegionDetection:
    """QFN (non-row) layouts qualify per P_FP2 builder decision #5."""

    def test_qfn_layout_yields_region_at_strict_clearance(self) -> None:
        """A QFN-style symmetric 4-row layout fires the detector when
        the recipe-relative trigger predicate matches."""
        # 16-pin QFN at 0.5mm pitch, 0.25mm pads, ~3mm body.  4 pads per
        # side.  At 0.20mm + 0.30mm recipe the corridor is infeasible by
        # a wide margin so the detector should fire.
        pitch = 0.5
        pad_w = 0.25  # along pitch axis
        pad_h = 0.30  # across pitch axis
        body = 3.0  # mm
        cx, cy = 50.0, 50.0
        pads: list[Pad] = []

        # South row: pins 1-4, pitch axis X
        for i in range(4):
            pads.append(
                Pad(
                    x=cx - 1.5 * pitch + i * pitch,
                    y=cy - body / 2,
                    width=pad_w,
                    height=pad_h,
                    net=i + 1,
                    net_name=f"P{i + 1}",
                    layer=Layer.F_CU,
                    ref="U99",
                    pin=str(i + 1),
                )
            )
        # East row: pins 5-8, pitch axis Y -- here pitch dim is height
        for i in range(4):
            pads.append(
                Pad(
                    x=cx + body / 2,
                    y=cy - 1.5 * pitch + i * pitch,
                    width=pad_h,
                    height=pad_w,
                    net=i + 5,
                    net_name=f"P{i + 5}",
                    layer=Layer.F_CU,
                    ref="U99",
                    pin=str(i + 5),
                )
            )
        # North row
        for i in range(4):
            pads.append(
                Pad(
                    x=cx + 1.5 * pitch - i * pitch,
                    y=cy + body / 2,
                    width=pad_w,
                    height=pad_h,
                    net=i + 9,
                    net_name=f"P{i + 9}",
                    layer=Layer.F_CU,
                    ref="U99",
                    pin=str(i + 9),
                )
            )
        # West row
        for i in range(4):
            pads.append(
                Pad(
                    x=cx - body / 2,
                    y=cy + 1.5 * pitch - i * pitch,
                    width=pad_h,
                    height=pad_w,
                    net=i + 13,
                    net_name=f"P{i + 13}",
                    layer=Layer.F_CU,
                    ref="U99",
                    pin=str(i + 13),
                )
            )

        rules = DesignRules(trace_width=0.30, trace_clearance=0.20, manufacturer="jlcpcb-tier1")
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB_TIER1)
        assert len(regions) == 1, "QFN fixture should yield exactly one region"
        assert regions[0].package_ref == "U99"

    def test_qfn_region_applies_to_qfn_pads(self) -> None:
        """The QFN region's ``applies_to_pad`` matches the QFN's own pads."""
        # Reuse the QFN layout above
        pitch = 0.5
        cx, cy = 50.0, 50.0
        sample_pad = Pad(
            x=cx - 1.5 * pitch,
            y=cy - 1.5,
            width=0.25,
            height=0.30,
            net=1,
            net_name="P1",
            layer=Layer.F_CU,
            ref="U99",
            pin="1",
        )

        # Region centred at (cx, cy) with 5mm radius -- the pad sits
        # within ~1.5mm of the centre so applies_to_pad returns True.
        region = FinePitchRegion(
            package_ref="U99",
            package_origin=(cx, cy),
            radius_mm=5.0,
            pin_pitch=0.5,
            pad_size_along_pitch=0.25,
            escape_clearance=0.14,
            pad_refs=frozenset([("U99", "1")]),
        )
        assert region.applies_to_pad(sample_pad)


class TestManufacturerFallback:
    """When no manufacturer is configured the detector still runs but
    the region's escape clearance defaults to ``rules.trace_clearance``
    (no shrink)."""

    def test_no_manufacturer_yields_no_shrink_region(self) -> None:
        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            grid_resolution=0.1,
            # No manufacturer set
        )
        pads = _ucc27211_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=None)
        # Detector still fires because the Q_FP1 geometry trigger is
        # recipe-relative and does NOT depend on manufacturer info.
        assert len(regions) == 1
        # But the escape clearance falls back to trace_clearance --
        # i.e. the region is a NO-OP.  This is what the route_cmd
        # WARNING surfaces.
        assert abs(regions[0].escape_clearance - rules.trace_clearance) < 1e-9


class TestLoadPcbIntegration:
    """End-to-end: ``load_pcb_for_routing`` installs regions for fine-pitch
    packages in the parsed components."""

    def test_softstart_revb_installs_regions(self, tmp_path: Path) -> None:
        """The softstart rev B PCB has UCC27211 SOIC-8 packages -- when
        loaded with the jlcpcb-tier1 recipe, the autorouter's grid has
        the expected fine-pitch escape regions installed."""
        # Regenerate softstart rev B on demand.  This is gated by a
        # missing recipe surface -- when the recipe fails to import the
        # test is skipped (mirrors the slow-test pattern from
        # ``test_softstart_revb_auto_pcb_size.py``).
        import os
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        board_dir = repo_root / "boards" / "external" / "softstart"
        if not (board_dir / "generate_design.py").exists():
            pytest.skip("softstart recipe not present")

        # Allow opt-out via env var to keep this test off the fast CI
        # path (PCB generation takes ~10-30s).
        if os.environ.get("KICAD_SKIP_SOFTSTART_GENERATION", "0") == "1":
            pytest.skip("KICAD_SKIP_SOFTSTART_GENERATION=1")

        sys.path.insert(0, str(board_dir))
        try:
            import generate_design  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)

        output_dir = tmp_path / "softstart_out"
        generate_design.create_project(output_dir, "softstart")
        generate_design.create_softstart_schematic(output_dir)
        pcb_path = generate_design.create_softstart_pcb(output_dir)

        from kicad_tools.router.io import load_pcb_for_routing

        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            grid_resolution=0.1,
            manufacturer="jlcpcb-tier1",
        )
        router, _ = load_pcb_for_routing(
            str(pcb_path),
            skip_nets=[
                "AC_LINE",
                "AC_NEUTRAL",
                "FUSED_LINE",
                "GND",
                "+3.3V",
                "VRECT",
                "SCAP_POS+",
                "SCAP_POS_GND",
                "SCAP_NEG+",
                "SCAP_NEG_GND",
                "ISENSE_POS",
            ],
            rules=rules,
        )
        regions = router.grid.get_fine_pitch_regions()
        # softstart rev B has UCC27211 (U5, U6) + MCP6001 (U7, U8) +
        # XC6206 LDO + STM32 LQFP-32 (U1) -- detector should pick up
        # all qualifying packages.  Asserting >= 2 is robust to
        # placement evolution while still proving the wiring works.
        assert len(regions) >= 2, (
            f"Expected >= 2 fine-pitch regions on softstart rev B; got {len(regions)}"
        )
        refs = {r.package_ref for r in regions}
        assert "U5" in refs or "U6" in refs, (
            f"Expected UCC27211 (U5 or U6) among detected regions; got {refs}"
        )
        # All regions should have a manufacturer-aware escape clearance
        # strictly below the recipe's trace_clearance (no fallback path).
        for r in regions:
            assert r.escape_clearance < rules.trace_clearance, (
                f"Region {r.package_ref} escape_clearance {r.escape_clearance} "
                f">= trace_clearance {rules.trace_clearance} (manufacturer "
                f"fallback path triggered when it should not have)"
            )


class TestInfeasibleRegionGuard:
    """Issue #3421 -- regions whose package corridor stays infeasible at the
    escape clearance must be inert, and the guard's decline must fall through
    to the legacy ``min_trace_width`` shrink (pre-P_FP3 behaviour).

    Regression context: board 06 (diffpair-test) routes at trace_width=0.15 /
    trace_clearance=0.15 / min_trace_width=0.10 with five 0.8mm-pitch
    packages (BGA-49, QFN-32 etc.).  At the jlcpcb escape clearance (0.14)
    those corridors remain infeasible (0.8 - 2*0.14 - 0.15 = 0.37 < 0.43
    required), so the regions cannot serve their purpose.  Pre-fix, the
    P_FP3 region branch (a) hard-returned the STANDARD halo on guard
    decline, clobbering the 0.05mm legacy halo the packages' own pads had
    pre-P_FP3 (a 4.5x halo inflation), and (b) evaluated the guard against
    the foreign pad's own pitch, so in-halo neighbours picked up the 0.14
    escape clearance the region could never use.  Net effect: the board 06
    seed-42 re-route regressed from 13 to 32 DRC errors (CI gate failure,
    Issue #3421).
    """

    def _board06_like_setup(self) -> tuple[RoutingGrid, FinePitchRegion, DesignRules]:
        rules = DesignRules(
            grid_resolution=0.05,
            trace_width=0.15,
            trace_clearance=0.15,
            manufacturer="jlcpcb",
            min_trace_width=0.10,
        )
        grid = RoutingGrid(width=50.0, height=50.0, rules=rules, layer_stack=LayerStack.two_layer())
        # QFN/BGA-style region: 0.8mm pitch, escape clearance 0.14.
        # Corridor at escape clearance: 0.8 - 0.28 - 0.15 = 0.37 < 0.43
        # required -> guard must decline for EVERY pad this region covers.
        region = FinePitchRegion(
            package_ref="U3",
            package_origin=(25.0, 25.0),
            radius_mm=7.07,
            pin_pitch=0.8,
            pad_size_along_pitch=0.3,
            escape_clearance=0.14,
            pad_refs=frozenset([("U3", "1")]),
        )
        grid.set_fine_pitch_regions([region])
        return grid, region, rules

    def test_own_pad_decline_falls_through_to_legacy_shrink(self) -> None:
        """Guard decline must yield the legacy min_trace_width halo, not the
        standard halo (the pre-P_FP3 value for board 06's 0.8mm pads)."""
        grid, _, rules = self._board06_like_setup()
        own_pad = Pad(
            x=25.0,
            y=25.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="SIG",
            layer=Layer.F_CU,
            ref="U3",
            pin="1",
        )
        halo = grid._clearance_for_pin_pitch(pin_pitch=0.8, pad=own_pad)
        # Legacy shrink: min_trace_width / 2 = 0.05 (its own #2865 guard
        # passes: 0.8 - 0.10 - 0.15 = 0.55 >= 0.45 required).
        assert halo == pytest.approx(0.05), (
            f"Guard decline must fall through to the legacy shrink (0.05), "
            f"got {halo} (standard would be 0.225 -- the Issue #3421 bug)"
        )

    def test_foreign_pad_guard_uses_region_pitch(self) -> None:
        """A foreign pad in the halo whose OWN pitch passes the guard must
        still be declined when the region's package pitch is infeasible."""
        grid, _, rules = self._board06_like_setup()
        # 0402-style passive 2mm from the package origin; its own pitch
        # (0.96) would pass the guard (0.96 - 0.28 - 0.15 = 0.53 >= 0.43)
        # but the region's package pitch (0.8) must dominate.
        foreign_pad = Pad(
            x=27.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=2,
            net_name="SIG2",
            layer=Layer.F_CU,
            ref="C7",
            pin="1",
        )
        halo = grid._clearance_for_pin_pitch(pin_pitch=0.96, pad=foreign_pad)
        # Pre-P_FP3 value for this pad: legacy shrink (0.96 pitch passes
        # the legacy #2865 guard) = 0.05.  The escape halo would be 0.215.
        assert halo == pytest.approx(0.05), (
            f"Foreign in-halo pad must not pick up the escape halo when the "
            f"region's package corridor is infeasible; got {halo}"
        )

    def test_resolver_declines_for_all_in_region_pads(self) -> None:
        """The C++ validator clearance source must return the standard
        clearance for every pad covered by an infeasible region."""
        grid, region, rules = self._board06_like_setup()
        own_pad = Pad(
            x=25.0,
            y=25.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="SIG",
            layer=Layer.F_CU,
            ref="U3",
            pin="1",
        )
        foreign_pad = Pad(
            x=27.0,
            y=25.0,
            width=0.5,
            height=0.5,
            net=2,
            net_name="SIG2",
            layer=Layer.F_CU,
            ref="C7",
            pin="1",
        )
        for pad, pitch in ((own_pad, 0.8), (foreign_pad, 0.96), (foreign_pad, 2.54)):
            clearance = resolve_clearance_with_escape_region(
                rules, pad, net_class=None, regions=[region], pin_pitch=pitch
            )
            assert clearance == pytest.approx(rules.trace_clearance), (
                f"Resolver must decline the 0.14 escape clearance for "
                f"{pad.ref} (pitch={pitch}); got {clearance}"
            )

    def test_feasible_region_still_shrinks(self) -> None:
        """Control: a SOIC-style region whose corridor IS feasible at the
        escape clearance keeps the escape shrink (softstart behaviour)."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20, manufacturer="jlcpcb-tier1")
        grid = RoutingGrid(width=50.0, height=50.0, rules=rules, layer_stack=LayerStack.two_layer())
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(25.0, 25.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.3,
            escape_clearance=0.14,
            pad_refs=frozenset([("U5", "1")]),
        )
        grid.set_fine_pitch_regions([region])
        own_pad = Pad(
            x=25.0,
            y=25.0,
            width=0.3,
            height=1.55,
            net=1,
            net_name="N1",
            layer=Layer.F_CU,
            ref="U5",
            pin="1",
        )
        # 1.27 - 0.28 - 0.30 = 0.69 >= 0.58 required -> shrink applies.
        halo = grid._clearance_for_pin_pitch(pin_pitch=1.27, pad=own_pad)
        assert halo == pytest.approx(0.14 + rules.trace_width / 2)
        clearance = resolve_clearance_with_escape_region(
            rules, own_pad, net_class=None, regions=[region], pin_pitch=1.27
        )
        assert clearance == pytest.approx(0.14)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--no-cov"])
