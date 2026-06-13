"""Tests for manufacturer design rule limits and relaxation tiers."""

import pytest

from kicad_tools.router.mfr_limits import (
    MFR_JLCPCB,
    MFR_JLCPCB_SIZE_TIERS,
    MFR_LIMITS,
    MFR_OSHPARK,
    MFR_PCBWAY,
    MFR_SIZE_TIER_LADDERS,
    ManufacturerSizeTier,
    RelaxationTier,
    find_smallest_admitting_tier,
    get_mfr_limits,
    get_mfr_size_tier_ladder,
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
        assert MFR_JLCPCB.min_edge_clearance == 0.3

    def test_oshpark_limits(self):
        """Test OSHPark manufacturer limits are correct."""
        assert MFR_OSHPARK.name == "oshpark"
        assert MFR_OSHPARK.min_trace == 0.152  # 6 mil
        assert MFR_OSHPARK.min_clearance == 0.152  # 6 mil
        assert MFR_OSHPARK.min_via_drill == 0.254  # 10 mil
        assert MFR_OSHPARK.min_via_annular == 0.127
        assert MFR_OSHPARK.min_edge_clearance == 0.381

    def test_pcbway_limits(self):
        """Test PCBWay manufacturer limits are correct."""
        assert MFR_PCBWAY.name == "pcbway"
        assert MFR_PCBWAY.min_trace == 0.127  # 5 mil
        assert MFR_PCBWAY.min_clearance == 0.127  # 5 mil
        assert MFR_PCBWAY.min_via_drill == 0.2  # 8 mil
        assert MFR_PCBWAY.min_via_annular == 0.15
        assert MFR_PCBWAY.min_edge_clearance == 0.25

    def test_min_edge_clearance_default_is_zero(self):
        """Test that min_edge_clearance defaults to 0 for custom MfrLimits."""
        from kicad_tools.router.mfr_limits import MfrLimits

        custom = MfrLimits(
            name="custom",
            min_trace=0.1,
            min_clearance=0.1,
            min_via_drill=0.2,
            min_via_annular=0.1,
        )
        assert custom.min_edge_clearance == 0.0

    def test_mfr_limits_is_frozen(self):
        """Test that MfrLimits instances are immutable (frozen dataclass)."""
        with pytest.raises(AttributeError):
            MFR_JLCPCB.min_trace = 0.1

    def test_all_manufacturers_in_limits_dict(self):
        """Test that all manufacturer presets are in the MFR_LIMITS dict."""
        assert "jlcpcb" in MFR_LIMITS
        assert "seeed" in MFR_LIMITS
        assert "seeed-fusion" in MFR_LIMITS
        assert "oshpark" in MFR_LIMITS
        assert "pcbway" in MFR_LIMITS
        assert MFR_LIMITS["jlcpcb"] is MFR_JLCPCB
        assert MFR_LIMITS["seeed"] is MFR_JLCPCB
        assert MFR_LIMITS["seeed-fusion"] is MFR_JLCPCB
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

    def test_seeed_alias_resolves_to_jlcpcb(self):
        """Test that 'seeed' resolves to JLCPCB limits."""
        result = get_mfr_limits("seeed")
        assert result is MFR_JLCPCB

    def test_seeed_fusion_alias_resolves_to_jlcpcb(self):
        """Test that 'seeed-fusion' resolves to JLCPCB limits."""
        assert get_mfr_limits("seeed-fusion") is MFR_JLCPCB
        # Underscore variant via alias
        assert get_mfr_limits("seeed_fusion") is MFR_JLCPCB
        # No separator variant via alias
        assert get_mfr_limits("seeedfusion") is MFR_JLCPCB

    def test_seeedstudio_alias_resolves_to_jlcpcb(self):
        """Test that 'seeedstudio' alias resolves to JLCPCB limits."""
        assert get_mfr_limits("seeedstudio") is MFR_JLCPCB

    def test_seeed_case_insensitive(self):
        """Test that seeed aliases are case-insensitive."""
        assert get_mfr_limits("Seeed") is MFR_JLCPCB
        assert get_mfr_limits("SEEED") is MFR_JLCPCB
        assert get_mfr_limits("Seeed-Fusion") is MFR_JLCPCB
        assert get_mfr_limits("SEEED-FUSION") is MFR_JLCPCB
        assert get_mfr_limits("SeeedStudio") is MFR_JLCPCB

    def test_error_suggests_closest_match(self):
        """Test that unknown manufacturer error includes 'Did you mean?' suggestion."""
        with pytest.raises(ValueError) as exc_info:
            get_mfr_limits("seeeeed")  # typo with extra 'e'
        error_msg = str(exc_info.value)
        assert "Did you mean" in error_msg
        assert "seeed" in error_msg

    def test_error_suggests_closest_match_for_jlcpcb_typo(self):
        """Test that a JLCPCB typo gets a suggestion."""
        with pytest.raises(ValueError) as exc_info:
            get_mfr_limits("jlcpb")  # missing 'c'
        error_msg = str(exc_info.value)
        assert "Did you mean" in error_msg
        assert "jlcpcb" in error_msg

    def test_no_suggestion_for_completely_unrelated(self):
        """Test that completely unrelated input does not produce suggestions."""
        with pytest.raises(ValueError) as exc_info:
            get_mfr_limits("xyz123")
        error_msg = str(exc_info.value)
        assert "Did you mean" not in error_msg


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


class TestManufacturerSizeTier:
    """Tests for the ManufacturerSizeTier dataclass (Issue #3352)."""

    def test_dataclass_construction(self):
        """ManufacturerSizeTier is a dataclass with the documented fields."""
        tier = ManufacturerSizeTier(
            max_width_mm=100.0,
            max_height_mm=150.0,
            price_2l_usd=5.0,
            price_4l_usd=15.0,
        )
        assert tier.max_width_mm == 100.0
        assert tier.max_height_mm == 150.0
        assert tier.price_2l_usd == 5.0
        assert tier.price_4l_usd == 15.0
        assert tier.note == ""  # default

    def test_area_cm2_property(self):
        """area_cm2 converts mm^2 envelope to cm^2 (divides by 100)."""
        tier = ManufacturerSizeTier(
            max_width_mm=100.0,
            max_height_mm=100.0,
            price_2l_usd=2.0,
            price_4l_usd=5.0,
        )
        # 100mm * 100mm = 10000 mm^2 = 100 cm^2
        assert tier.area_cm2 == pytest.approx(100.0)

    def test_frozen_dataclass(self):
        """ManufacturerSizeTier is immutable (frozen)."""
        tier = ManufacturerSizeTier(
            max_width_mm=100.0,
            max_height_mm=100.0,
            price_2l_usd=2.0,
            price_4l_usd=5.0,
        )
        with pytest.raises((AttributeError, Exception)):
            tier.max_width_mm = 200.0  # type: ignore[misc]


class TestJlcpcbSizeTiers:
    """Tests for the JLCPCB size-tier ladder."""

    def test_ladder_nonempty(self):
        """JLCPCB has at least the 6 documented tiers."""
        assert len(MFR_JLCPCB_SIZE_TIERS) >= 6

    def test_ladder_ascending_area(self):
        """Tiers are ordered by ascending envelope area."""
        areas = [t.area_cm2 for t in MFR_JLCPCB_SIZE_TIERS]
        assert areas == sorted(areas), "MFR_JLCPCB_SIZE_TIERS must be ordered by ascending area"

    def test_ladder_ascending_2l_price(self):
        """Prices are monotonically non-decreasing along the ladder."""
        prices_2l = [t.price_2l_usd for t in MFR_JLCPCB_SIZE_TIERS]
        assert prices_2l == sorted(prices_2l), (
            "JLCPCB 2L prices should be monotonically non-decreasing"
        )

    def test_base_tier_is_100x100(self):
        """Base tier matches JLCPCB's $2 100x100 bracket."""
        base = MFR_JLCPCB_SIZE_TIERS[0]
        assert base.max_width_mm == 100.0
        assert base.max_height_mm == 100.0

    def test_4l_more_expensive_than_2l(self):
        """4-layer pricing is always more expensive than 2-layer at the same tier."""
        for tier in MFR_JLCPCB_SIZE_TIERS:
            assert tier.price_4l_usd > tier.price_2l_usd, (
                f"Tier {tier.max_width_mm}x{tier.max_height_mm}: "
                f"4L (${tier.price_4l_usd}) must exceed 2L (${tier.price_2l_usd})"
            )


class TestGetMfrSizeTierLadder:
    """Tests for get_mfr_size_tier_ladder()."""

    def test_jlcpcb_lookup(self):
        """JLCPCB returns the documented size tiers."""
        ladder = get_mfr_size_tier_ladder("jlcpcb")
        assert ladder == MFR_JLCPCB_SIZE_TIERS

    def test_case_insensitive(self):
        """Lookup is case-insensitive."""
        assert get_mfr_size_tier_ladder("JLCPCB") == MFR_JLCPCB_SIZE_TIERS
        assert get_mfr_size_tier_ladder("JlcPcb") == MFR_JLCPCB_SIZE_TIERS

    def test_alias_resolution(self):
        """Aliases (e.g. seeed_fusion) resolve to the canonical ladder."""
        ladder = get_mfr_size_tier_ladder("seeed_fusion")
        assert ladder == MFR_JLCPCB_SIZE_TIERS

    def test_returns_copy(self):
        """Returned list is a copy -- mutating it does not affect the registry."""
        ladder = get_mfr_size_tier_ladder("jlcpcb")
        ladder.clear()
        # Registry should still have the original entries
        assert len(MFR_JLCPCB_SIZE_TIERS) >= 6
        assert len(MFR_SIZE_TIER_LADDERS["jlcpcb"]) >= 6

    def test_unknown_manufacturer_raises(self):
        """Unknown manufacturer raises ValueError with suggestions."""
        with pytest.raises(ValueError, match="Unknown manufacturer"):
            get_mfr_size_tier_ladder("acme-corp")


class TestFindSmallestAdmittingTier:
    """Tests for find_smallest_admitting_tier()."""

    def test_small_board_picks_base_tier(self):
        """An 80x80 board fits in the 100x100 base tier."""
        tier = find_smallest_admitting_tier(80, 80)
        assert tier is not None
        assert tier.max_width_mm == 100.0
        assert tier.max_height_mm == 100.0

    def test_exact_tier_match(self):
        """A 100x100 board exactly fits the 100x100 base tier."""
        tier = find_smallest_admitting_tier(100, 100)
        assert tier is not None
        assert tier.max_width_mm == 100.0
        assert tier.max_height_mm == 100.0

    def test_one_axis_stretch(self):
        """A 120x80 board fits the 100x150 tier when rotated."""
        tier = find_smallest_admitting_tier(120, 80)
        assert tier is not None
        # 120x80 fits in 100x150 (after 90 deg rotation: 80x120 fits)
        assert tier.max_width_mm == 100.0
        assert tier.max_height_mm == 150.0

    def test_softstart_envelope(self):
        """A 150x100 board (softstart rev B) fits the 100x150 tier (rotated)."""
        tier = find_smallest_admitting_tier(150, 100)
        assert tier is not None
        # 150x100 rotates to 100x150 which fits the 100x150 tier exactly
        assert tier.max_width_mm == 100.0
        assert tier.max_height_mm == 150.0

    def test_oversize_returns_none(self):
        """A board exceeding the largest tier returns None."""
        tier = find_smallest_admitting_tier(500, 500)
        assert tier is None

    def test_manufacturer_argument(self):
        """The manufacturer arg routes through the alias resolution."""
        tier_canonical = find_smallest_admitting_tier(100, 100, "jlcpcb")
        tier_alias = find_smallest_admitting_tier(100, 100, "JLCPCB")
        assert tier_canonical == tier_alias
