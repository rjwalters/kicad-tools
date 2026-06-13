"""Tests for the fine-pitch escape detector (Issue #3371 / P_FP2).

Phase 2 (region detector + per-net-class threading at the pathfinder /
cpp_backend clearance boundary) of the fine-pitch escape capability
ladder.  Validates:

- :class:`kicad_tools.router.fine_pitch_escape.FinePitchRegion` --
  frozen dataclass + ``contains_point`` / ``applies_to_pad`` helpers.
- :func:`kicad_tools.router.fine_pitch_escape.detect_fine_pitch_regions`
  -- per-component grouping, pitch-ceiling filter, Q_FP1 recipe-relative
  geometry trigger, manufacturer-aware default escape clearance.
- :func:`kicad_tools.router.fine_pitch_escape.resolve_clearance_with_escape_region`
  -- the single-threading-point helper.  In particular the
  *impedance-controlled-net guard* (PR #3273 carve-out) -- a fine-pitch
  package on an impedance-controlled net must NOT trigger the escape
  shrink.
- :data:`kicad_tools.router.fine_pitch_escape.DEFAULT_ESCAPE_REGION_RADIUS_MM`
  -- the 5mm halo radius (Q_FP2 architect recommendation).
- Pad-axis extraction (Q_FP2 builder decision #2) -- horizontal SOIC row
  vs vertical row vs non-row layout.

No router behaviour is exercised here -- P_FP3 applies the regions at
the per-net A* boundary; P_FP4 composes the ladder; P_FP5 adds the
softstart consumer test.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.fine_pitch_escape import (
    DEFAULT_ESCAPE_REGION_RADIUS_MM,
    FinePitchRegion,
    detect_fine_pitch_regions,
    get_default_escape_clearance,
    resolve_clearance_with_escape_region,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.mfr_limits import (
    MFR_JLCPCB,
    MFR_JLCPCB_TIER1,
    MFR_OSHPARK,
    MFR_PCBWAY,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

# ============================================================================
# Helpers
# ============================================================================


def _make_soic8_pads(
    ref: str = "U5",
    cx: float = 10.0,
    cy: float = 10.0,
    pitch: float = 1.27,
    pad_w: float = 0.30,
    pad_h: float = 1.55,
    row_gap: float = 4.0,
    net_id: int = 1,
) -> list[Pad]:
    """Synthesize an SOIC-8 footprint at JLCPCB tier-1 geometry.

    Mirrors the UCC27211 SOIC-8 fixture from Issue #3371:
    1.27mm pitch, 0.30mm pad width, 1.55mm pad height,
    4mm row-to-row gap (centre-to-centre).

    Returns a list of 8 router-level :class:`Pad` objects with pad
    centres laid out as a horizontal dual-row package.
    """
    pads: list[Pad] = []
    # Bottom row (pins 1-4), top row (pins 8-5, KiCad numbering)
    half_pitch_span = (pitch * (4 - 1)) / 2.0
    for i in range(4):
        x = cx - half_pitch_span + i * pitch
        # Bottom row pads
        pads.append(
            Pad(
                x=x,
                y=cy - row_gap / 2.0,
                width=pad_w,
                height=pad_h,
                net=net_id,
                net_name=f"N{net_id}",
                layer=Layer.F_CU,
                ref=ref,
                pin=str(i + 1),
            )
        )
        # Top row pads (pin numbering counts down)
        pads.append(
            Pad(
                x=x,
                y=cy + row_gap / 2.0,
                width=pad_w,
                height=pad_h,
                net=net_id,
                net_name=f"N{net_id}",
                layer=Layer.F_CU,
                ref=ref,
                pin=str(8 - i),
            )
        )
    return pads


def _make_2p54_dip_pads(
    ref: str = "J1", cx: float = 50.0, cy: float = 50.0, pitch: float = 2.54
) -> list[Pad]:
    """Synthesize a 2.54mm-pitch DIP-style header (2x4 = 8 pads).

    DIP pitch is too coarse to ever need the fine-pitch escape, so the
    detector should NEVER return a region for this fixture regardless
    of recipe.
    """
    pads: list[Pad] = []
    for i in range(4):
        for row in (0, 1):
            pads.append(
                Pad(
                    x=cx + i * pitch,
                    y=cy + row * 7.62,
                    width=1.6,
                    height=1.6,
                    net=row * 4 + i + 1,
                    net_name=f"DIP{row * 4 + i + 1}",
                    layer=Layer.F_CU,
                    ref=ref,
                    pin=str(row * 4 + i + 1),
                )
            )
    return pads


# ============================================================================
# FinePitchRegion dataclass
# ============================================================================


class TestFinePitchRegion:
    """Tests for the :class:`FinePitchRegion` dataclass + helpers."""

    def test_contains_point_inside_halo(self):
        """A point near the centre is inside the 5mm halo."""
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(10.0, 10.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        assert region.contains_point(10.0, 10.0)
        assert region.contains_point(12.0, 13.0)  # dist=3.6 < 5
        assert region.contains_point(13.5, 13.5)  # dist~4.95 < 5

    def test_contains_point_outside_halo(self):
        """A point past the radius is outside the halo."""
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(10.0, 10.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        assert not region.contains_point(15.001, 10.0)  # dist=5.001 > 5
        assert not region.contains_point(20.0, 20.0)

    def test_contains_point_on_boundary_is_outside(self):
        """A point exactly on the radius boundary is considered outside.

        The helper uses strict less-than (``<``) so the boundary cell
        falls back to standard clearance rather than the escape value.
        This matters at integer grid resolutions where the boundary
        is at a known grid line.
        """
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(0.0, 0.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        # Point at distance exactly 5.0
        assert not region.contains_point(5.0, 0.0)
        assert not region.contains_point(3.0, 4.0)  # 3-4-5 triangle

    def test_applies_to_pad_identity_match(self):
        """A pad with (ref, pin) in pad_refs is always considered to apply.

        Identity match wins even when the pad position happens to be
        outside the circular halo (defensive: a 14-pin SOIC's outermost
        pad can sit just past 5mm from the centroid).
        """
        # Pad sits at (100, 100) -- well outside the 5mm halo at (0, 0).
        pad = Pad(
            x=100.0,
            y=100.0,
            width=0.30,
            height=1.55,
            net=1,
            net_name="N1",
            ref="U5",
            pin="1",
        )
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(0.0, 0.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
            pad_refs=frozenset({("U5", "1"), ("U5", "2")}),
        )
        assert region.applies_to_pad(pad)

    def test_applies_to_pad_geometric_containment(self):
        """A foreign pad inside the halo also applies to the region."""
        # Foreign pad (different ref) sitting near U5's centroid.
        pad = Pad(
            x=11.0,
            y=11.0,
            width=0.30,
            height=0.30,
            net=2,
            net_name="N2",
            ref="C1",  # Different component
            pin="1",
        )
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(10.0, 10.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
            pad_refs=frozenset({("U5", "1")}),
        )
        assert region.applies_to_pad(pad)

    def test_applies_to_pad_outside(self):
        """A foreign pad outside the halo does NOT apply."""
        pad = Pad(
            x=50.0,
            y=50.0,
            width=0.30,
            height=0.30,
            net=2,
            net_name="N2",
            ref="C1",
            pin="1",
        )
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(10.0, 10.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        assert not region.applies_to_pad(pad)

    def test_region_is_frozen(self):
        """The dataclass is frozen -- fields cannot be reassigned."""
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(10.0, 10.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        with pytest.raises((AttributeError, Exception)):
            region.radius_mm = 10.0  # type: ignore[misc]


# ============================================================================
# detect_fine_pitch_regions -- the detector
# ============================================================================


class TestDetectFinePitchRegions:
    """Tests for the per-board fine-pitch escape region detector."""

    def test_ucc27211_soic8_at_strict_recipe_fires(self):
        """UCC27211 SOIC-8 at 0.30mm trace + 0.20mm clearance qualifies."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert len(regions) == 1
        assert regions[0].package_ref == "U5"
        # SOIC-8 layout: pitch is 1.27mm along X axis.
        assert regions[0].pin_pitch == pytest.approx(1.27, abs=0.01)
        # Pad size along pitch = pad width (X dimension).
        assert regions[0].pad_size_along_pitch == pytest.approx(0.30, abs=0.01)
        # Default halo radius is 5mm.
        assert regions[0].radius_mm == DEFAULT_ESCAPE_REGION_RADIUS_MM
        # Escape clearance is the manufacturer-aware default
        # (0.127 + 0.013 = 0.140mm for jlcpcb).
        assert regions[0].escape_clearance == pytest.approx(0.140, abs=1e-6)

    def test_ucc27211_at_relaxed_recipe_does_not_fire(self):
        """SOIC-8 at 0.15mm clearance -- corridor fits, no region."""
        # 0.30mm trace + 0.15mm clearance = 0.90mm corridor.
        # SOIC gap = 1.27 - 0.30 = 0.97mm.  Corridor fits -- no region.
        rules = DesignRules(trace_width=0.30, trace_clearance=0.15)
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert regions == []

    def test_2p54mm_dip_does_not_fire(self):
        """2.54mm DIP header is too coarse -- never fires."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = _make_2p54_dip_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert regions == []

    def test_2p54mm_dip_does_not_fire_at_extreme_recipe(self):
        """Even at an absurd recipe (1mm trace + 1mm clearance) DIP doesn't fire.

        2.54mm pitch with 1.6mm pad gives gap = 0.94mm.  But pitch is
        2.54 > FINE_PITCH_THRESHOLD_MM (1.5mm), so the pitch ceiling
        excludes it BEFORE the recipe geometry check.
        """
        rules = DesignRules(trace_width=1.0, trace_clearance=1.0)
        pads = _make_2p54_dip_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert regions == []

    def test_empty_pad_list_returns_empty(self):
        """No pads -> no regions."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        regions = detect_fine_pitch_regions([], rules, mfr_limits=MFR_JLCPCB)
        assert regions == []

    def test_pads_without_ref_are_skipped(self):
        """Pads with empty ``ref`` (board fiducials, edge cuts) are skipped."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = [
            Pad(x=0.0, y=0.0, width=0.30, height=1.55, net=0, net_name="", ref="", pin=""),
            Pad(x=1.27, y=0.0, width=0.30, height=1.55, net=0, net_name="", ref="", pin=""),
        ]
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert regions == []

    def test_single_pad_clusters_are_skipped(self):
        """Single-pad clusters cannot be fine-pitch packages."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = [
            Pad(x=0.0, y=0.0, width=0.30, height=1.55, net=1, net_name="N1", ref="U1", pin="1"),
        ]
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert regions == []

    def test_multiple_packages_yield_multiple_regions(self):
        """Two fine-pitch packages on the same board -> two regions."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = _make_soic8_pads(ref="U5", cx=10.0, cy=10.0)
        pads += _make_soic8_pads(ref="U6", cx=30.0, cy=30.0)
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert len(regions) == 2
        refs = {r.package_ref for r in regions}
        assert refs == {"U5", "U6"}

    def test_origin_is_pad_cluster_centroid(self):
        """The region origin is the cluster's bounding-box centre."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        # U5 centred at (20, 30); the SOIC-8 fixture centres pads at cx, cy.
        pads = _make_soic8_pads(ref="U5", cx=20.0, cy=30.0)
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert len(regions) == 1
        # Allow small tolerance for the row-gap centring.
        assert regions[0].package_origin[0] == pytest.approx(20.0, abs=0.5)
        assert regions[0].package_origin[1] == pytest.approx(30.0, abs=0.5)

    def test_radius_is_configurable(self):
        """Custom radius is honoured."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB, radius_mm=3.0)
        assert len(regions) == 1
        assert regions[0].radius_mm == 3.0

    def test_pad_refs_are_populated(self):
        """The region's pad_refs set covers all of the package's pads."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = _make_soic8_pads(ref="U5")
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert len(regions) == 1
        # SOIC-8 has 8 pads with pins "1".."8".
        expected_refs = frozenset({("U5", str(i)) for i in range(1, 9)})
        assert regions[0].pad_refs == expected_refs

    def test_default_escape_clearance_uses_mfr_floor(self):
        """The region's escape clearance is mfr.min_clearance + 0.013."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = _make_soic8_pads()

        for mfr in (MFR_JLCPCB, MFR_JLCPCB_TIER1, MFR_PCBWAY, MFR_OSHPARK):
            regions = detect_fine_pitch_regions(pads, rules, mfr_limits=mfr)
            assert len(regions) == 1
            expected = get_default_escape_clearance(mfr)
            assert regions[0].escape_clearance == pytest.approx(expected, abs=1e-9)

    def test_manufacturer_resolved_from_rules(self):
        """When no explicit mfr_limits, rules.manufacturer is used as a fallback."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20, manufacturer="jlcpcb")
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules)
        assert len(regions) == 1
        assert regions[0].escape_clearance == pytest.approx(0.140, abs=1e-6)

    def test_unknown_manufacturer_falls_back_to_trace_clearance(self):
        """When rules has an unknown manufacturer, escape_clearance defaults to trace_clearance.

        This is a *no-shrink* fallback so a board without a recognised
        manufacturer profile does not accidentally generate a region
        with an unsafe clearance value.
        """
        rules = DesignRules(
            trace_width=0.30, trace_clearance=0.20, manufacturer="this-mfr-does-not-exist"
        )
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules)
        assert len(regions) == 1
        assert regions[0].escape_clearance == pytest.approx(0.20, abs=1e-6)

    def test_no_manufacturer_falls_back_to_trace_clearance(self):
        """When neither argument nor rules supplies a manufacturer, escape_clearance falls back."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules)
        assert len(regions) == 1
        assert regions[0].escape_clearance == pytest.approx(0.20, abs=1e-6)

    def test_explicit_mfr_limits_overrides_rules_manufacturer(self):
        """An explicit ``mfr_limits`` argument wins over ``rules.manufacturer``."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20, manufacturer="jlcpcb")
        pads = _make_soic8_pads()
        # Force the OSHPark default (0.152 + 0.013 = 0.165) instead of jlcpcb (0.140).
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_OSHPARK)
        assert len(regions) == 1
        assert regions[0].escape_clearance == pytest.approx(0.165, abs=1e-6)


# ============================================================================
# Pad-axis extraction (Q_FP2 builder decision #2)
# ============================================================================


class TestPadAxisExtraction:
    """Tests for the pad-size-along-pitch inference.

    Q_FP2 builder decision #2: the detector infers the pitch axis from
    cluster geometry rather than requiring callers to annotate each pad.
    Horizontal SOIC row -> pad width.  Vertical row -> pad height.  Non-
    row layout -> conservative ``min(width, height)`` fallback.
    """

    def test_horizontal_soic_uses_pad_width(self):
        """A dual-row package laid out horizontally uses pad WIDTH as size."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        # SOIC with 0.30mm width, 1.55mm height -- the pitch is along X.
        pads = _make_soic8_pads(pad_w=0.30, pad_h=1.55)
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert len(regions) == 1
        assert regions[0].pad_size_along_pitch == pytest.approx(0.30, abs=0.01)

    def test_vertical_soic_uses_pad_height(self):
        """A vertical dual-row package uses pad HEIGHT as the pitch-axis dimension.

        Rotate the SOIC 90 degrees: pads cluster around two distinct X
        values with the pitch running along Y.  In that orientation
        the leaded-pad's *short* axis is the pad HEIGHT (the rotated
        equivalent of the horizontal-row width).
        """
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        # Rotated SOIC: swap pad x/y AND swap width/height.
        pads: list[Pad] = []
        for i in range(4):
            for col_offset, pin_offset in ((0, 0), (4, 0)):
                y = 0.0 + i * 1.27
                x = 0.0 if col_offset == 0 else 4.0
                pads.append(
                    Pad(
                        x=x,
                        y=y,
                        width=1.55,  # rotated: width along X (row-perpendicular)
                        height=0.30,  # rotated: height along Y (pitch axis)
                        net=i + 1 + col_offset,
                        net_name=f"N{i + 1 + col_offset}",
                        ref="U7",
                        pin=str(i + 1 + col_offset),
                    )
                )
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert len(regions) == 1
        # Vertical row -> pitch axis is Y -> pad size is HEIGHT (0.30mm).
        assert regions[0].pad_size_along_pitch == pytest.approx(0.30, abs=0.01)

    def test_non_row_layout_uses_min_dimension_fallback(self):
        """A QFN-style non-row layout falls back to ``min(width, height)``.

        QFN packages are not the primary P_FP2 target (BGA escape is a
        separate ladder per the issue's "Out of scope" list), but the
        detector should not silently skip them.  The conservative
        fallback makes the Q_FP1 predicate fire more readily, biasing
        toward applying the escape rule on QFN-style packages.
        """
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        # Synthesize a 0.5mm-pitch quad QFN-16 with square pads.
        pads: list[Pad] = []
        for i in range(4):
            # Bottom row (y=0)
            pads.append(
                Pad(
                    x=i * 0.5,
                    y=0.0,
                    width=0.25,
                    height=0.25,
                    net=i + 1,
                    net_name=f"N{i + 1}",
                    ref="U9",
                    pin=str(i + 1),
                )
            )
            # Top row (y=2)
            pads.append(
                Pad(
                    x=i * 0.5,
                    y=2.0,
                    width=0.25,
                    height=0.25,
                    net=i + 5,
                    net_name=f"N{i + 5}",
                    ref="U9",
                    pin=str(i + 5),
                )
            )
            # Left col (x=0)
            pads.append(
                Pad(
                    x=0.0,
                    y=0.5 + i * 0.4,
                    width=0.25,
                    height=0.25,
                    net=i + 9,
                    net_name=f"N{i + 9}",
                    ref="U9",
                    pin=str(i + 9),
                )
            )
            # Right col (x=2)
            pads.append(
                Pad(
                    x=2.0,
                    y=0.5 + i * 0.4,
                    width=0.25,
                    height=0.25,
                    net=i + 13,
                    net_name=f"N{i + 13}",
                    ref="U9",
                    pin=str(i + 13),
                )
            )
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        # Should fire (0.5mm pitch < 1.5mm threshold AND corridor infeasible).
        assert len(regions) == 1
        # Non-row layout falls back to min dimension (0.25mm).
        assert regions[0].pad_size_along_pitch == pytest.approx(0.25, abs=0.01)


# ============================================================================
# resolve_clearance_with_escape_region -- the impedance guard
# ============================================================================


class TestImpedanceGuard:
    """Tests for the PR #3273 impedance-trap carve-out.

    P_FP1 builder decision #5: when the active net class declares any
    impedance target, the escape rule is bypassed entirely so the
    impedance budget is preserved.  This is the load-bearing guard --
    re-opening it would be a silent regression on every
    impedance-controlled board.
    """

    def test_no_regions_no_net_class_falls_through_to_default(self):
        """Empty regions + no net class -> rules.trace_clearance."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pad = Pad(x=10.0, y=10.0, width=0.30, height=1.55, net=1, net_name="N1", ref="U5", pin="1")
        clearance = resolve_clearance_with_escape_region(rules, pad, net_class=None, regions=None)
        assert clearance == pytest.approx(0.20, abs=1e-6)

    def test_regions_none_falls_through(self):
        """``regions=None`` behaves identically to empty list -- backward compat."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        nc = NetClassRouting(name="SIGNAL", clearance=0.20, escape_clearance=0.14)
        pad = Pad(x=10.0, y=10.0, width=0.30, height=1.55, net=1, net_name="N1", ref="U5", pin="1")
        # Without regions, the per-class escape_clearance still wins (via
        # the standard get_clearance_for_component net_class path).
        clearance = resolve_clearance_with_escape_region(rules, pad, net_class=nc, regions=None)
        # Falls through to get_clearance_for_component which respects
        # nc.escape_clearance as the per-net-class override (P_FP1).
        assert clearance == pytest.approx(0.14, abs=1e-6)

    def test_in_region_uses_net_class_escape_clearance(self):
        """A pad inside a region on a non-impedance net uses nc.escape_clearance."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        nc = NetClassRouting(name="SIGNAL", clearance=0.20, escape_clearance=0.14)
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        assert len(regions) == 1
        # Pick one of U5's pads -- inside the region by identity.
        u5_pad = pads[0]
        clearance = resolve_clearance_with_escape_region(
            rules, u5_pad, net_class=nc, regions=regions
        )
        assert clearance == pytest.approx(0.14, abs=1e-6)

    def test_in_region_without_net_class_override_uses_region_default(self):
        """In-region pad with no per-class override uses region.escape_clearance."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        # No escape_clearance on the class -- should fall back to region default.
        nc = NetClassRouting(name="SIGNAL", clearance=0.20)
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        u5_pad = pads[0]
        clearance = resolve_clearance_with_escape_region(
            rules, u5_pad, net_class=nc, regions=regions
        )
        # Region default at JLCPCB = 0.127 + 0.013 = 0.140.
        assert clearance == pytest.approx(0.140, abs=1e-6)

    def test_impedance_controlled_diff_net_bypasses_escape(self):
        """A diff-impedance net does NOT shrink even inside the region.

        Load-bearing guard: PR #3273 impedance trap.  A USB 3.0 pair at
        90 ohms target_diff_impedance must keep the standard clearance
        even when escaping a fine-pitch package, because the impedance
        budget assumes the full clearance.
        """
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        nc = NetClassRouting(
            name="USB3",
            clearance=0.20,
            escape_clearance=0.14,  # would normally apply
            target_diff_impedance=90.0,  # but this guards
        )
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        u5_pad = pads[0]
        clearance = resolve_clearance_with_escape_region(
            rules, u5_pad, net_class=nc, regions=regions
        )
        # MUST be standard trace_clearance, not the escape value.
        assert clearance == pytest.approx(0.20, abs=1e-6)

    def test_impedance_controlled_single_ended_net_bypasses_escape(self):
        """A 50-ohm single-ended net also bypasses the escape rule."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        nc = NetClassRouting(
            name="CLOCK",
            clearance=0.20,
            escape_clearance=0.14,
            target_single_impedance=50.0,  # guards
        )
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        u5_pad = pads[0]
        clearance = resolve_clearance_with_escape_region(
            rules, u5_pad, net_class=nc, regions=regions
        )
        assert clearance == pytest.approx(0.20, abs=1e-6)

    def test_explicit_component_override_wins_unconditionally(self):
        """Explicit component_clearances override wins -- Issue #1016 contract."""
        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            component_clearances={"U5": 0.10},
        )
        nc = NetClassRouting(name="SIGNAL", clearance=0.20, escape_clearance=0.14)
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        u5_pad = pads[0]
        clearance = resolve_clearance_with_escape_region(
            rules, u5_pad, net_class=nc, regions=regions
        )
        # Component override wins over both escape clearance and trace clearance.
        assert clearance == pytest.approx(0.10, abs=1e-6)

    def test_explicit_component_override_wins_over_impedance_guard(self):
        """Component override is the highest-precedence layer.

        The component override happens BEFORE the impedance guard.  If a
        designer pins a per-component clearance, they're asserting they
        know the geometry and impedance trade-off for that part.  This
        is consistent with the Issue #1016 + #2867 precedence.
        """
        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            component_clearances={"U5": 0.10},
        )
        nc = NetClassRouting(
            name="USB3",
            clearance=0.20,
            escape_clearance=0.14,
            target_diff_impedance=90.0,
        )
        pads = _make_soic8_pads()
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        u5_pad = pads[0]
        clearance = resolve_clearance_with_escape_region(
            rules, u5_pad, net_class=nc, regions=regions
        )
        assert clearance == pytest.approx(0.10, abs=1e-6)

    def test_pad_outside_region_uses_default(self):
        """A pad outside any region falls through to standard clearance."""
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        nc = NetClassRouting(name="SIGNAL", clearance=0.20, escape_clearance=0.14)
        pads = _make_soic8_pads(cx=10.0, cy=10.0)
        regions = detect_fine_pitch_regions(pads, rules, mfr_limits=MFR_JLCPCB)
        # Pad sitting far from the U5 region.
        far_pad = Pad(
            x=100.0,
            y=100.0,
            width=0.30,
            height=0.30,
            net=2,
            net_name="N2",
            ref="R1",
            pin="1",
        )
        clearance = resolve_clearance_with_escape_region(
            rules, far_pad, net_class=nc, regions=regions
        )
        # No region applies -- falls through to the per-class escape_clearance
        # via get_clearance_for_component (P_FP1 path).
        assert clearance == pytest.approx(0.14, abs=1e-6)


# ============================================================================
# Threading at the cpp_backend boundary
# ============================================================================


class TestNetClassThreading:
    """Tests that the RoutingGrid carries fine-pitch regions through to the
    cpp_backend pad-clearance lookup at line 619.

    These tests do NOT exercise the C++ backend (which would require the
    extension build).  They verify the *threading* by directly invoking
    :func:`resolve_clearance_with_escape_region` with the same arguments
    the cpp_backend uses, and by checking the
    :meth:`RoutingGrid.set_fine_pitch_regions` /
    :meth:`RoutingGrid.get_fine_pitch_regions` accessors.
    """

    def test_routing_grid_default_regions_empty(self):
        """A freshly constructed grid has no regions installed."""
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(width=50.0, height=50.0, rules=DesignRules())
        assert grid.get_fine_pitch_regions() == []

    def test_routing_grid_set_get_round_trip(self):
        """``set_fine_pitch_regions`` and ``get_fine_pitch_regions`` round-trip."""
        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid(width=50.0, height=50.0, rules=DesignRules())
        region = FinePitchRegion(
            package_ref="U5",
            package_origin=(10.0, 10.0),
            radius_mm=5.0,
            pin_pitch=1.27,
            pad_size_along_pitch=0.30,
            escape_clearance=0.14,
        )
        grid.set_fine_pitch_regions([region])
        regions = grid.get_fine_pitch_regions()
        assert len(regions) == 1
        assert regions[0].package_ref == "U5"

    def test_routing_grid_empty_regions_preserves_clearance_for_component(self):
        """With no regions installed, the cpp_backend clearance lookup is unchanged.

        This is the P_FP2 "no behaviour change" promise: the threading
        seam at cpp_backend.py:619 must produce byte-for-byte identical
        clearances when no regions are installed.
        """
        rules = DesignRules(trace_width=0.30, trace_clearance=0.20)
        pad = Pad(x=10.0, y=10.0, width=0.30, height=1.55, net=1, net_name="N1", ref="U5", pin="1")
        # Pre-#3371 call shape:
        old = rules.get_clearance_for_component("U5", pin_pitch=1.27)
        # Post-#3371 call shape with empty regions:
        new = resolve_clearance_with_escape_region(
            rules, pad, net_class=None, regions=[], pin_pitch=1.27
        )
        assert old == new
