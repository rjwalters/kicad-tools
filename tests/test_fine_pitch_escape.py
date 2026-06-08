"""Tests for the fine-pitch escape foundation (Issue #3371 / P_FP1).

Phase 1 (data + predicate + threshold-cap fix) of the fine-pitch escape
capability ladder.  Validates:

- :func:`kicad_tools.router.fine_pitch_escape.geometry_needs_fine_pitch_escape`
  Q_FP1 recipe-relative trigger predicate.
- :func:`kicad_tools.router.fine_pitch_escape.get_default_escape_clearance`
  Q_FP2 manufacturer-aware safe-default helper.
- :attr:`kicad_tools.router.rules.NetClassRouting.escape_clearance` field
  (backward-compat preserved when unset; round-trip serialization).
- :attr:`kicad_tools.router.rules.DesignRules.fine_pitch_threshold` raised
  from 0.8mm to 1.5mm so 1.27mm-pitch SOIC qualifies.
- :data:`kicad_tools.router.escape.FINE_PITCH_THRESHOLD_MM` constant + the
  matching :func:`is_fine_pitch_ssop` default-threshold change.
- :meth:`kicad_tools.router.rules.DesignRules.get_clearance_for_component`
  extension to accept an optional ``net_class`` argument.

No router behaviour is exercised here -- P_FP2 wires the predicate into a
detector and P_FP3 applies the clearance at the pathfinder boundary.
"""

import pytest

from kicad_tools.router.escape import FINE_PITCH_THRESHOLD_MM, is_fine_pitch_ssop
from kicad_tools.router.fine_pitch_escape import (
    ESCAPE_CLEARANCE_SAFETY_MARGIN_MM,
    geometry_needs_fine_pitch_escape,
    get_default_escape_clearance,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.mfr_limits import (
    MFR_JLCPCB,
    MFR_JLCPCB_TIER1,
    MFR_OSHPARK,
    MFR_PCBWAY,
    get_mfr_limits,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting


# ============================================================================
# Q_FP1 trigger predicate
# ============================================================================


class TestGeometryNeedsFinePitchEscape:
    """Tests for the Q_FP1 recipe-relative trigger predicate."""

    def test_ucc27211_soic8_at_strict_recipe_fires(self):
        """UCC27211 SOIC-8 at JLCPCB tier-1 recipe NEEDS escape.

        Empirical fixture from Issue #3371: 1.27mm pitch, 0.30mm pad,
        0.30mm trace, 0.20mm clearance.

            gap       = 1.27 - 0.30 = 0.97 mm
            corridor  = 2 * (0.30 + 0.20) = 1.00 mm
            corridor > gap  ->  True
        """
        assert geometry_needs_fine_pitch_escape(
            trace_width=0.30, clearance=0.20, pin_pitch=1.27, pad_size=0.30
        )

    def test_ucc27211_soic8_at_relaxed_clearance_does_not_fire(self):
        """Same SOIC-8 footprint at 0.15mm clearance does NOT need escape.

        Demonstrates the *recipe-relative* property: the same footprint
        flips depending on routing parameters.

            gap      = 1.27 - 0.30 = 0.97 mm
            corridor = 2 * (0.30 + 0.15) = 0.90 mm
            corridor <= gap  ->  False
        """
        assert not geometry_needs_fine_pitch_escape(
            trace_width=0.30, clearance=0.15, pin_pitch=1.27, pad_size=0.30
        )

    def test_2p54mm_dip_pitch_does_not_fire(self):
        """2.54mm DIP-style pitch is never corridor-constrained at typical recipes."""
        assert not geometry_needs_fine_pitch_escape(
            trace_width=0.30, clearance=0.20, pin_pitch=2.54, pad_size=0.50
        )

    def test_tssop_0p5mm_fires_at_strict_recipe(self):
        """TSSOP at 0.5mm pitch needs escape under standard recipe."""
        assert geometry_needs_fine_pitch_escape(
            trace_width=0.15, clearance=0.10, pin_pitch=0.5, pad_size=0.30
        )

    def test_ssop_0p65mm_fires_under_jlcpcb_tier1(self):
        """SSOP at 0.65mm pitch fires for typical jlcpcb-tier1 strict recipe."""
        assert geometry_needs_fine_pitch_escape(
            trace_width=0.15, clearance=0.127, pin_pitch=0.65, pad_size=0.30
        )

    def test_boundary_exact_fit_does_not_fire(self):
        """Boundary case: corridor exactly equals gap -> False (fits)."""
        # gap = 1.0; corridor = 2 * (0.30 + 0.20) = 1.0; equal -> fits
        assert not geometry_needs_fine_pitch_escape(
            trace_width=0.30, clearance=0.20, pin_pitch=1.30, pad_size=0.30
        )

    def test_boundary_corridor_just_too_wide_fires(self):
        """Boundary case: corridor > gap by a hair -> True (does not fit)."""
        # gap = 0.999; corridor = 1.0 -> fires
        assert geometry_needs_fine_pitch_escape(
            trace_width=0.30, clearance=0.20, pin_pitch=1.299, pad_size=0.30
        )


# ============================================================================
# Q_FP2 manufacturer-aware default
# ============================================================================


class TestGetDefaultEscapeClearance:
    """Tests for the Q_FP2 manufacturer-aware safe-default helper."""

    def test_jlcpcb_tier1_default(self):
        """jlcpcb-tier1: 0.127mm + 0.013mm safety = 0.140mm."""
        result = get_default_escape_clearance(MFR_JLCPCB_TIER1)
        assert result == pytest.approx(0.140, abs=1e-9)

    def test_jlcpcb_base_default(self):
        """plain jlcpcb shares the 0.127mm floor."""
        result = get_default_escape_clearance(MFR_JLCPCB)
        assert result == pytest.approx(0.140, abs=1e-9)

    def test_pcbway_default(self):
        """pcbway also at 0.127mm floor -> 0.140mm default."""
        result = get_default_escape_clearance(MFR_PCBWAY)
        assert result == pytest.approx(0.140, abs=1e-9)

    def test_oshpark_default(self):
        """oshpark at 0.152mm floor -> 0.165mm default."""
        result = get_default_escape_clearance(MFR_OSHPARK)
        assert result == pytest.approx(0.165, abs=1e-9)

    def test_safety_margin_constant_documented(self):
        """The Q_FP2 safety margin is 0.013mm as documented in the decisions table."""
        assert ESCAPE_CLEARANCE_SAFETY_MARGIN_MM == 0.013

    def test_default_is_strictly_above_floor(self):
        """For every supported manufacturer, default > floor."""
        for mfr_name in ("jlcpcb", "jlcpcb-tier1", "pcbway", "oshpark"):
            mfr = get_mfr_limits(mfr_name)
            assert get_default_escape_clearance(mfr) > mfr.min_clearance


# ============================================================================
# NetClassRouting.escape_clearance field
# ============================================================================


class TestNetClassRoutingEscapeClearance:
    """Tests for the new ``escape_clearance`` field on NetClassRouting."""

    def test_default_is_none(self):
        """Backward-compat: default is None (preserves pre-#3371 behavior)."""
        nc = NetClassRouting(name="Test")
        assert nc.escape_clearance is None

    def test_explicit_set_is_preserved(self):
        """An explicit value is stored verbatim."""
        nc = NetClassRouting(name="Test", escape_clearance=0.14)
        assert nc.escape_clearance == 0.14

    def test_round_trip_with_value(self):
        """to_dict / from_dict preserves the explicit value."""
        original = NetClassRouting(name="Test", escape_clearance=0.14)
        rt = NetClassRouting.from_dict(original.to_dict())
        assert rt.escape_clearance == 0.14
        assert rt == original

    def test_round_trip_with_none(self):
        """to_dict / from_dict preserves None."""
        original = NetClassRouting(name="Test")
        rt = NetClassRouting.from_dict(original.to_dict())
        assert rt.escape_clearance is None
        assert rt == original

    def test_to_dict_contains_field(self):
        """The new field appears in to_dict output (drift prevention)."""
        d = NetClassRouting(name="Test").to_dict()
        assert "escape_clearance" in d


# ============================================================================
# DesignRules.fine_pitch_threshold default raise
# ============================================================================


class TestFinePitchThresholdRaised:
    """Verify the raised default threshold lets 1.27mm SOIC qualify."""

    def test_default_threshold_is_1_5(self):
        """Default is now 1.5mm per Issue #3371."""
        assert DesignRules().fine_pitch_threshold == 1.5

    def test_soic_1p27mm_now_below_default_threshold(self):
        """1.27mm SOIC pitch IS now below the default fine_pitch_threshold."""
        rules = DesignRules()
        assert 1.27 < rules.fine_pitch_threshold

    def test_old_threshold_still_settable(self):
        """Callers can still pin the pre-#3371 value of 0.8mm explicitly."""
        rules = DesignRules(fine_pitch_threshold=0.8)
        assert rules.fine_pitch_threshold == 0.8


class TestFinePitchThresholdConstantSyncedWithEscape:
    """The escape.py FINE_PITCH_THRESHOLD_MM constant should equal the rules default."""

    def test_constants_match(self):
        """FINE_PITCH_THRESHOLD_MM equals DesignRules.fine_pitch_threshold default."""
        assert FINE_PITCH_THRESHOLD_MM == DesignRules().fine_pitch_threshold


# ============================================================================
# is_fine_pitch_ssop now includes 1.27mm SOIC
# ============================================================================


def _make_dual_row_pads(
    pin_count: int,
    pitch: float,
    pad_width: float = 0.4,
    pad_height: float = 1.2,
    row_spacing: float = 5.3,
) -> list[Pad]:
    """Build dual-row pads suitable for the is_fine_pitch_ssop predicate."""
    assert pin_count % 2 == 0
    per_row = pin_count // 2
    total_width = (per_row - 1) * pitch
    start_x = -total_width / 2
    pads: list[Pad] = []
    for i in range(per_row):
        x = start_x + i * pitch
        pads.append(
            Pad(
                x=x,
                y=row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=i + 1,
                net_name=f"NET{i + 1}",
                ref="U1",
                pin=str(i + 1),
                layer=Layer.F_CU,
            )
        )
    for i in range(per_row):
        x = start_x + i * pitch
        pads.append(
            Pad(
                x=x,
                y=-row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=per_row + i + 1,
                net_name=f"NET{per_row + i + 1}",
                ref="U1",
                pin=str(per_row + i + 1),
                layer=Layer.F_CU,
            )
        )
    return pads


class TestIsFinePitchSsopIncludesSoic:
    """Verify the raised threshold now includes 1.27mm-pitch SOIC."""

    def test_soic_1p27mm_default_threshold_matches(self):
        """SOIC-8 at 1.27mm pitch IS fine-pitch under the new default."""
        pads = _make_dual_row_pads(pin_count=8, pitch=1.27)
        assert is_fine_pitch_ssop(pads)

    def test_soic_1p27mm_explicit_old_threshold_excluded(self):
        """The pre-#3371 0.75mm explicit threshold still excludes SOIC."""
        pads = _make_dual_row_pads(pin_count=8, pitch=1.27)
        assert not is_fine_pitch_ssop(pads, pitch_threshold=0.75)


# ============================================================================
# get_clearance_for_component(net_class=...) extension
# ============================================================================


class TestGetClearanceForComponentNetClassExtension:
    """Tests for the new optional ``net_class`` argument."""

    def test_net_class_escape_clearance_overrides_fine_pitch(self):
        """When net_class.escape_clearance is set, it wins over the global fine-pitch shrink."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            fine_pitch_clearance=0.15,
        )
        nc = NetClassRouting(name="EscapeClass", escape_clearance=0.14)
        # 1.27mm SOIC is below the new default threshold (1.5mm), so
        # without net_class we'd get fine_pitch_clearance (0.15) if
        # geometry feasible.  With net_class.escape_clearance set, the
        # 0.14 override wins.
        result = rules.get_clearance_for_component("U1", pin_pitch=1.27, net_class=nc)
        assert result == 0.14

    def test_net_class_without_escape_clearance_falls_through(self):
        """A net_class with escape_clearance=None falls through to default logic."""
        rules = DesignRules(
            trace_width=0.1,
            trace_clearance=0.075,
            fine_pitch_clearance=0.08,
            fine_pitch_threshold=0.8,
        )
        nc = NetClassRouting(name="Standard")  # escape_clearance is None by default
        # Should match the no-net_class behaviour (fine-pitch shrink to 0.08 at 0.65mm pitch).
        result = rules.get_clearance_for_component("U1", pin_pitch=0.65, net_class=nc)
        assert result == 0.08

    def test_net_class_default_argument_preserves_legacy_callers(self):
        """Legacy callers that don't pass net_class get unchanged behavior."""
        rules = DesignRules(trace_clearance=0.20)
        # Plain 2-arg call -- same as pre-#3371.
        assert rules.get_clearance_for_component("R1") == 0.20
        assert rules.get_clearance_for_component("R1", pin_pitch=1.27) == 0.20

    def test_explicit_component_override_still_wins(self):
        """Per-component override beats net_class.escape_clearance."""
        rules = DesignRules(
            trace_clearance=0.20,
            component_clearances={"U1": 0.08},
        )
        nc = NetClassRouting(name="EscapeClass", escape_clearance=0.14)
        # Explicit per-component override (0.08) wins over the per-class
        # override (0.14): the component-level override is the strongest
        # signal because it is asserted at the recipe level.
        result = rules.get_clearance_for_component("U1", pin_pitch=1.27, net_class=nc)
        assert result == 0.08

    def test_net_class_escape_clearance_does_not_require_pin_pitch(self):
        """When net_class.escape_clearance is set, no pin_pitch is needed.

        This matches the explicit-per-component-override semantics: the
        caller is asserting the override is appropriate, so the global
        fine-pitch-threshold gate is bypassed.
        """
        rules = DesignRules(trace_clearance=0.20)
        nc = NetClassRouting(name="EscapeClass", escape_clearance=0.14)
        result = rules.get_clearance_for_component("U1", net_class=nc)
        assert result == 0.14
