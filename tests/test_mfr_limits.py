"""Tests for manufacturer design rule limits and relaxation tiers."""

import pytest

from kicad_tools.router.mfr_limits import (
    MFR_JLCPCB,
    MFR_LIMITS,
    MFR_OSHPARK,
    MFR_PCBWAY,
    RelaxationTier,
    get_mfr_limits,
    get_relaxation_tiers,
)


class TestMfrLimits:
    """Tests for the MfrLimits dataclass."""

    def test_min_via_diameter_computed_property(self):
        """Test that min_via_diameter is correctly computed from drill and annular ring."""
        # JLCPCB: 0.3mm drill + 2 * 0.15mm annular = 0.6mm
        assert MFR_JLCPCB.min_via_diameter == pytest.approx(0.6)

        # OSHPark: 0.254mm drill + 2 * 0.127mm annular = 0.508mm
        assert MFR_OSHPARK.min_via_diameter == pytest.approx(0.508)

        # PCBWay: 0.2mm drill + 2 * 0.15mm annular = 0.5mm
        assert MFR_PCBWAY.min_via_diameter == pytest.approx(0.5)

    def test_jlcpcb_limits(self):
        """Test JLCPCB manufacturer limits are correct."""
        assert MFR_JLCPCB.name == "jlcpcb"
        assert MFR_JLCPCB.min_trace == 0.127  # 5 mil
        assert MFR_JLCPCB.min_clearance == 0.127  # 5 mil
        assert MFR_JLCPCB.min_via_drill == 0.3
        assert MFR_JLCPCB.min_via_annular == 0.15

    def test_oshpark_limits(self):
        """Test OSHPark manufacturer limits are correct."""
        assert MFR_OSHPARK.name == "oshpark"
        assert MFR_OSHPARK.min_trace == 0.152  # 6 mil
        assert MFR_OSHPARK.min_clearance == 0.152  # 6 mil
        assert MFR_OSHPARK.min_via_drill == 0.254  # 10 mil
        assert MFR_OSHPARK.min_via_annular == 0.127

    def test_pcbway_limits(self):
        """Test PCBWay manufacturer limits are correct."""
        assert MFR_PCBWAY.name == "pcbway"
        assert MFR_PCBWAY.min_trace == 0.127  # 5 mil
        assert MFR_PCBWAY.min_clearance == 0.127  # 5 mil
        assert MFR_PCBWAY.min_via_drill == 0.2  # 8 mil
        assert MFR_PCBWAY.min_via_annular == 0.15

    def test_mfr_limits_is_frozen(self):
        """Test that MfrLimits instances are immutable (frozen dataclass)."""
        with pytest.raises(AttributeError):
            MFR_JLCPCB.min_trace = 0.1

    def test_all_manufacturers_in_limits_dict(self):
        """Test that all manufacturer presets are in the MFR_LIMITS dict."""
        assert "jlcpcb" in MFR_LIMITS
        assert "oshpark" in MFR_LIMITS
        assert "pcbway" in MFR_LIMITS
        assert MFR_LIMITS["jlcpcb"] is MFR_JLCPCB
        assert MFR_LIMITS["oshpark"] is MFR_OSHPARK
        assert MFR_LIMITS["pcbway"] is MFR_PCBWAY


class TestGetMfrLimits:
    """Tests for the get_mfr_limits() function."""

    def test_case_insensitive_lookup(self):
        """Test that manufacturer lookup is case-insensitive."""
        assert get_mfr_limits("jlcpcb") is MFR_JLCPCB
        assert get_mfr_limits("JLCPCB") is MFR_JLCPCB
        assert get_mfr_limits("JlCpCb") is MFR_JLCPCB
        assert get_mfr_limits("OSHPark") is MFR_OSHPARK
        assert get_mfr_limits("PCBWAY") is MFR_PCBWAY

    def test_unknown_manufacturer_raises_error(self):
        """Test that unknown manufacturer raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            get_mfr_limits("unknown_mfr")
        assert "Unknown manufacturer" in str(exc_info.value)
        assert "unknown_mfr" in str(exc_info.value)

    def test_error_message_lists_valid_options(self):
        """Test that error message includes list of valid manufacturers."""
        with pytest.raises(ValueError) as exc_info:
            get_mfr_limits("invalid")
        error_msg = str(exc_info.value)
        assert "jlcpcb" in error_msg
        assert "oshpark" in error_msg
        assert "pcbway" in error_msg


class TestRelaxationTier:
    """Tests for the RelaxationTier dataclass."""

    def test_tier_str_format(self):
        """Test that __str__ produces expected format."""
        tier = RelaxationTier(
            tier=0,
            trace_width=0.2,
            clearance=0.3,
            via_drill=0.3,
            via_diameter=0.6,
            description="User-specified",
        )
        result = str(tier)
        assert "Tier 0" in result
        assert "trace=0.200mm" in result
        assert "clearance=0.300mm" in result
        assert "User-specified" in result

    def test_tier_attributes(self):
        """Test that tier attributes are accessible."""
        tier = RelaxationTier(
            tier=2,
            trace_width=0.15,
            clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            description="Aggressive relaxation",
        )
        assert tier.tier == 2
        assert tier.trace_width == 0.15
        assert tier.clearance == 0.2
        assert tier.via_drill == 0.3
        assert tier.via_diameter == 0.6
        assert tier.description == "Aggressive relaxation"


class TestGetRelaxationTiers:
    """Tests for the get_relaxation_tiers() function."""

    def test_basic_tier_generation(self):
        """Test basic tier generation with default 4 tiers."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
        )
        assert len(tiers) == 4
        # First tier is user-specified
        assert tiers[0].tier == 0
        assert tiers[0].trace_width == 0.2
        assert tiers[0].clearance == 0.4
        assert tiers[0].description == "User-specified"
        # Last tier is manufacturer minimum
        assert tiers[3].tier == 3
        assert tiers[3].trace_width == pytest.approx(0.127)
        assert tiers[3].clearance == pytest.approx(0.127)
        assert "JLCPCB" in tiers[3].description

    def test_single_tier_when_at_minimum(self):
        """Test that only one tier is returned when user values are already at minimum."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.127,
            initial_clearance=0.127,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
        )
        assert len(tiers) == 1
        assert tiers[0].tier == 0
        assert tiers[0].trace_width == 0.127
        assert tiers[0].clearance == 0.127
        assert "at minimum" in tiers[0].description

    def test_single_tier_when_below_minimum(self):
        """Test that only one tier is returned when user values are below minimum."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.1,  # Below JLCPCB min of 0.127
            initial_clearance=0.1,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
        )
        assert len(tiers) == 1
        # User values are preserved even if below minimum
        assert tiers[0].trace_width == 0.1
        assert tiers[0].clearance == 0.1

    def test_floor_overrides_trace(self):
        """Test that min_trace_floor overrides manufacturer minimum."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
            min_trace_floor=0.15,  # Higher than JLCPCB's 0.127
        )
        # Last tier should respect the floor
        assert tiers[-1].trace_width == pytest.approx(0.15)
        # Clearance should still go to mfr minimum
        assert tiers[-1].clearance == pytest.approx(0.127)

    def test_floor_overrides_clearance(self):
        """Test that min_clearance_floor overrides manufacturer minimum."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
            min_clearance_floor=0.2,  # Higher than JLCPCB's 0.127
        )
        # Last tier should respect the floor
        assert tiers[-1].clearance == pytest.approx(0.2)
        # Trace should still go to mfr minimum
        assert tiers[-1].trace_width == pytest.approx(0.127)

    def test_both_floors_override(self):
        """Test that both floor values override manufacturer minimums."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.3,
            initial_clearance=0.5,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
            min_trace_floor=0.18,
            min_clearance_floor=0.25,
        )
        assert tiers[-1].trace_width == pytest.approx(0.18)
        assert tiers[-1].clearance == pytest.approx(0.25)

    def test_linear_interpolation(self):
        """Test that intermediate tiers are linearly interpolated."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
            num_tiers=4,
        )
        # Check monotonic decrease
        for i in range(len(tiers) - 1):
            assert tiers[i].trace_width >= tiers[i + 1].trace_width
            assert tiers[i].clearance >= tiers[i + 1].clearance

        # Check intermediate values are between start and end
        for tier in tiers[1:-1]:
            assert tiers[0].trace_width >= tier.trace_width >= tiers[-1].trace_width
            assert tiers[0].clearance >= tier.clearance >= tiers[-1].clearance

    def test_tier_descriptions(self):
        """Test that tier descriptions are assigned correctly."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="jlcpcb",
            num_tiers=4,
        )
        assert tiers[0].description == "User-specified"
        assert tiers[1].description == "Moderate relaxation"
        assert tiers[2].description == "Aggressive relaxation"
        assert "JLCPCB" in tiers[3].description
        assert "minimum" in tiers[3].description

    def test_different_num_tiers(self):
        """Test tier generation with different num_tiers values."""
        # 2 tiers
        tiers_2 = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            num_tiers=2,
        )
        assert len(tiers_2) == 2
        assert tiers_2[0].trace_width == 0.2
        assert tiers_2[1].trace_width == pytest.approx(0.127)

        # 6 tiers
        tiers_6 = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            num_tiers=6,
        )
        assert len(tiers_6) == 6
        assert tiers_6[0].trace_width == 0.2
        assert tiers_6[5].trace_width == pytest.approx(0.127)

    def test_single_tier_num(self):
        """Test tier generation with num_tiers=1."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            num_tiers=1,
        )
        # Should return just the initial values
        assert len(tiers) == 1
        assert tiers[0].trace_width == 0.2
        assert tiers[0].clearance == 0.4

    def test_different_manufacturer(self):
        """Test tier generation with different manufacturers."""
        # OSHPark has higher minimums
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            manufacturer="oshpark",
        )
        # Last tier should reflect OSHPark minimums
        assert tiers[-1].trace_width == pytest.approx(0.152)
        assert tiers[-1].clearance == pytest.approx(0.152)
        assert "OSHPARK" in tiers[-1].description

    def test_via_values_interpolated(self):
        """Test that via drill and diameter are also interpolated."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.5,  # Larger than minimum
            initial_via_diameter=1.0,
            manufacturer="jlcpcb",
        )
        # First tier has user values
        assert tiers[0].via_drill == 0.5
        assert tiers[0].via_diameter == 1.0
        # Last tier should be at/near minimum
        assert tiers[-1].via_drill == pytest.approx(0.3)
        assert tiers[-1].via_diameter == pytest.approx(0.6)

    def test_tier_numbers_sequential(self):
        """Test that tier numbers are sequential starting from 0."""
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.4,
            initial_via_drill=0.3,
            initial_via_diameter=0.6,
            num_tiers=5,
        )
        for i, tier in enumerate(tiers):
            assert tier.tier == i


class TestIntegration:
    """Integration tests for the mfr_limits module."""

    def test_module_exports_from_router(self):
        """Test that all public symbols are exported from kicad_tools.router."""
        from kicad_tools.router import (
            MFR_JLCPCB,
            MFR_LIMITS,
            MFR_OSHPARK,
            MFR_PCBWAY,
            MfrLimits,
            RelaxationTier,
            get_mfr_limits,
            get_relaxation_tiers,
        )

        # Verify they're the same objects
        assert MfrLimits is not None
        assert RelaxationTier is not None
        assert get_mfr_limits is not None
        assert get_relaxation_tiers is not None
        assert MFR_JLCPCB is not None
        assert MFR_OSHPARK is not None
        assert MFR_PCBWAY is not None
        assert MFR_LIMITS is not None

    def test_roundtrip_lookup(self):
        """Test that looking up a manufacturer and using its limits works correctly."""
        mfr = get_mfr_limits("jlcpcb")
        tiers = get_relaxation_tiers(
            initial_trace_width=0.2,
            initial_clearance=0.3,
            initial_via_drill=0.4,
            initial_via_diameter=0.8,
            manufacturer=mfr.name,
        )
        # Final tier should match the looked-up manufacturer's limits
        assert tiers[-1].trace_width == pytest.approx(mfr.min_trace)
        assert tiers[-1].clearance == pytest.approx(mfr.min_clearance)
