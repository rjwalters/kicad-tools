"""Tests for kicad_tools.manufacturers module."""

import pytest
from kicad_tools.manufacturers import (
    get_profile,
    list_manufacturers,
    get_manufacturer_ids,
    find_compatible_manufacturers,
    compare_design_rules,
    DesignRules,
    ManufacturerProfile,
)


class TestManufacturerProfiles:
    """Tests for manufacturer profile functions."""

    def test_list_manufacturers(self):
        """Test listing all manufacturers."""
        manufacturers = list_manufacturers()
        assert len(manufacturers) == 4

        names = {m.name for m in manufacturers}
        assert "JLCPCB" in names
        assert "Seeed Fusion" in names
        assert "PCBWay" in names
        assert "OSHPark" in names

    def test_get_manufacturer_ids(self):
        """Test getting manufacturer IDs."""
        ids = get_manufacturer_ids()
        assert "jlcpcb" in ids
        assert "seeed" in ids
        assert "pcbway" in ids
        assert "oshpark" in ids

    def test_get_profile_by_id(self):
        """Test getting a profile by ID."""
        profile = get_profile("jlcpcb")
        assert profile.id == "jlcpcb"
        assert profile.name == "JLCPCB"
        assert "jlcpcb.com" in profile.website

    def test_get_profile_with_alias(self):
        """Test getting a profile using an alias."""
        profile = get_profile("jlc")
        assert profile.id == "jlcpcb"

        profile = get_profile("osh")
        assert profile.id == "oshpark"

    def test_get_profile_invalid(self):
        """Test getting a profile with invalid ID."""
        with pytest.raises(ValueError, match="Unknown manufacturer"):
            get_profile("invalid_manufacturer")


class TestDesignRules:
    """Tests for design rules."""

    def test_jlcpcb_2layer_rules(self):
        """Test JLCPCB 2-layer design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.127)  # 5 mil
        assert rules.min_clearance_mm == pytest.approx(0.127)
        assert rules.min_via_drill_mm == pytest.approx(0.3)

    def test_jlcpcb_4layer_rules(self):
        """Test JLCPCB 4-layer design rules."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=4, copper_oz=1.0)

        assert rules.min_trace_width_mm == pytest.approx(0.1016)  # 4 mil
        assert rules.min_via_drill_mm == pytest.approx(0.2)

    def test_rules_to_dict(self):
        """Test converting rules to dictionary."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(layers=2)
        d = rules.to_dict()

        assert "min_trace_width_mm" in d
        assert "min_clearance_mm" in d
        assert "min_via_drill_mm" in d

    def test_compare_design_rules(self):
        """Test comparing design rules across manufacturers."""
        comparison = compare_design_rules(layers=4, copper_oz=1.0)

        assert "jlcpcb" in comparison
        assert "seeed" in comparison
        assert isinstance(comparison["jlcpcb"], DesignRules)


class TestAssembly:
    """Tests for assembly capabilities."""

    def test_jlcpcb_supports_assembly(self):
        """Test that JLCPCB supports assembly."""
        profile = get_profile("jlcpcb")
        assert profile.supports_assembly()
        assert profile.assembly is not None

    def test_oshpark_no_assembly(self):
        """Test that OSHPark doesn't support assembly."""
        profile = get_profile("oshpark")
        assert not profile.supports_assembly()
        assert profile.assembly is None


class TestPartsLibrary:
    """Tests for parts library."""

    def test_jlcpcb_lcsc_library(self):
        """Test JLCPCB LCSC parts library."""
        profile = get_profile("jlcpcb")
        assert profile.parts_library is not None
        assert profile.parts_library.name == "LCSC"

        url = profile.get_part_search_url("C123456")
        assert "lcsc.com" in url
        assert "C123456" in url

    def test_oshpark_no_library(self):
        """Test that OSHPark has no parts library."""
        profile = get_profile("oshpark")
        assert profile.parts_library is None
        assert profile.get_part_search_url("any") is None


class TestCompatibleManufacturers:
    """Tests for finding compatible manufacturers."""

    def test_find_compatible_conservative_design(self):
        """Test finding compatible manufacturers for conservative design."""
        # Very conservative design rules (6mil/6mil, 0.3mm via)
        compatible = find_compatible_manufacturers(
            trace_width_mm=0.1524,  # 6 mil
            clearance_mm=0.1524,
            via_drill_mm=0.3,
            layers=2,
            needs_assembly=False,
        )

        # All manufacturers should support this
        assert len(compatible) >= 3

    def test_find_compatible_aggressive_design(self):
        """Test finding compatible manufacturers for aggressive design."""
        # Aggressive but achievable design rules (4mil/4mil)
        compatible = find_compatible_manufacturers(
            trace_width_mm=0.1016,  # 4 mil
            clearance_mm=0.1016,
            via_drill_mm=0.2,
            layers=6,
            needs_assembly=False,
        )

        # JLCPCB and PCBWay support this
        assert len(compatible) >= 1

    def test_find_compatible_with_assembly(self):
        """Test finding manufacturers that support assembly."""
        compatible = find_compatible_manufacturers(
            trace_width_mm=0.2,
            clearance_mm=0.2,
            via_drill_mm=0.3,
            layers=2,
            needs_assembly=True,
        )

        # OSHPark should not be in the list (no assembly)
        ids = {m.id for m in compatible}
        assert "oshpark" not in ids
