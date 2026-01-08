"""Tests for the footprint selector module."""

import pytest

from kicad_tools.schematic.footprint_selector import (
    FootprintSelector,
    parse_component_value,
    select_footprint_for_passive,
)


class TestParseCapacitance:
    """Tests for capacitance value parsing."""

    def test_parse_nanofarads(self):
        """Test parsing nanofarad values."""
        assert parse_component_value("100nF", "capacitor") == pytest.approx(1e-7)
        assert parse_component_value("10nF", "capacitor") == pytest.approx(1e-8)
        assert parse_component_value("1nF", "capacitor") == pytest.approx(1e-9)
        assert parse_component_value("4.7nF", "capacitor") == pytest.approx(4.7e-9)

    def test_parse_microfarads(self):
        """Test parsing microfarad values."""
        assert parse_component_value("10uF", "capacitor") == pytest.approx(1e-5)
        assert parse_component_value("1uF", "capacitor") == pytest.approx(1e-6)
        assert parse_component_value("4.7uF", "capacitor") == pytest.approx(4.7e-6)
        assert parse_component_value("0.1uF", "capacitor") == pytest.approx(1e-7)

    def test_parse_picofarads(self):
        """Test parsing picofarad values."""
        assert parse_component_value("100pF", "capacitor") == pytest.approx(1e-10)
        assert parse_component_value("10pF", "capacitor") == pytest.approx(1e-11)
        assert parse_component_value("1pF", "capacitor") == pytest.approx(1e-12)

    def test_parse_case_insensitive(self):
        """Test that value parsing is case insensitive."""
        assert parse_component_value("100NF", "capacitor") == pytest.approx(1e-7)
        assert parse_component_value("10UF", "capacitor") == pytest.approx(1e-5)
        assert parse_component_value("100Pf", "capacitor") == pytest.approx(1e-10)

    def test_parse_with_spaces(self):
        """Test parsing values with leading/trailing spaces."""
        assert parse_component_value(" 100nF ", "capacitor") == pytest.approx(1e-7)
        assert parse_component_value("  10uF", "capacitor") == pytest.approx(1e-5)

    def test_parse_unicode_mu(self):
        """Test parsing values with unicode micro symbol (µ)."""
        assert parse_component_value("10µF", "capacitor") == pytest.approx(1e-5)
        assert parse_component_value("4.7µF", "capacitor") == pytest.approx(4.7e-6)

    def test_invalid_capacitance_raises(self):
        """Test that invalid values raise ValueError."""
        with pytest.raises(ValueError):
            parse_component_value("invalid", "capacitor")
        with pytest.raises(ValueError):
            parse_component_value("10X", "capacitor")


class TestParseResistance:
    """Tests for resistance value parsing."""

    def test_parse_ohms(self):
        """Test parsing ohm values."""
        assert parse_component_value("100R", "resistor") == pytest.approx(100)
        assert parse_component_value("10R", "resistor") == pytest.approx(10)
        assert parse_component_value("4.7R", "resistor") == pytest.approx(4.7)

    def test_parse_kilohms(self):
        """Test parsing kilohm values."""
        assert parse_component_value("10k", "resistor") == pytest.approx(10000)
        assert parse_component_value("4.7k", "resistor") == pytest.approx(4700)
        assert parse_component_value("100k", "resistor") == pytest.approx(100000)

    def test_parse_megohms(self):
        """Test parsing megohm values."""
        assert parse_component_value("1M", "resistor") == pytest.approx(1e6)
        assert parse_component_value("4.7M", "resistor") == pytest.approx(4.7e6)
        assert parse_component_value("10M", "resistor") == pytest.approx(1e7)

    def test_parse_inline_decimal(self):
        """Test parsing inline decimal notation (e.g., 4R7)."""
        assert parse_component_value("4R7", "resistor") == pytest.approx(4.7)
        assert parse_component_value("4K7", "resistor") == pytest.approx(4700)
        assert parse_component_value("1M5", "resistor") == pytest.approx(1.5e6)

    def test_parse_case_insensitive_resistance(self):
        """Test that resistance parsing is case insensitive."""
        assert parse_component_value("10K", "resistor") == pytest.approx(10000)
        assert parse_component_value("10k", "resistor") == pytest.approx(10000)

    def test_invalid_resistance_raises(self):
        """Test that invalid values raise ValueError."""
        with pytest.raises(ValueError):
            parse_component_value("invalid", "resistor")


class TestParseInductance:
    """Tests for inductance value parsing."""

    def test_parse_microhenries(self):
        """Test parsing microhenry values."""
        assert parse_component_value("10uH", "inductor") == pytest.approx(1e-5)
        assert parse_component_value("1uH", "inductor") == pytest.approx(1e-6)
        assert parse_component_value("4.7uH", "inductor") == pytest.approx(4.7e-6)

    def test_parse_nanohenries(self):
        """Test parsing nanohenry values."""
        assert parse_component_value("100nH", "inductor") == pytest.approx(1e-7)
        assert parse_component_value("10nH", "inductor") == pytest.approx(1e-8)

    def test_parse_millihenries(self):
        """Test parsing millihenry values."""
        assert parse_component_value("1mH", "inductor") == pytest.approx(1e-3)
        assert parse_component_value("10mH", "inductor") == pytest.approx(1e-2)


class TestFootprintSelectorCapacitors:
    """Tests for capacitor footprint selection."""

    def test_default_profile_capacitor_selection(self):
        """Test capacitor footprint selection with default profile."""
        selector = FootprintSelector(profile="default")

        # ≤100nF -> 0402
        assert selector.select_capacitor_footprint("100nF") == "Capacitor_SMD:C_0402_1005Metric"
        assert selector.select_capacitor_footprint("10nF") == "Capacitor_SMD:C_0402_1005Metric"

        # ≤1µF -> 0603
        assert selector.select_capacitor_footprint("1uF") == "Capacitor_SMD:C_0603_1608Metric"
        assert selector.select_capacitor_footprint("0.5uF") == "Capacitor_SMD:C_0603_1608Metric"

        # ≤10µF -> 0805
        assert selector.select_capacitor_footprint("10uF") == "Capacitor_SMD:C_0805_2012Metric"
        assert selector.select_capacitor_footprint("4.7uF") == "Capacitor_SMD:C_0805_2012Metric"

        # >10µF -> 1206
        assert selector.select_capacitor_footprint("22uF") == "Capacitor_SMD:C_1206_3216Metric"
        assert selector.select_capacitor_footprint("100uF") == "Capacitor_SMD:C_1206_3216Metric"

    def test_machine_profile_capacitor_selection(self):
        """Test capacitor footprint selection with machine profile."""
        selector = FootprintSelector(profile="machine")

        # Machine profile allows larger values in smaller packages
        assert selector.select_capacitor_footprint("1uF") == "Capacitor_SMD:C_0402_1005Metric"
        assert selector.select_capacitor_footprint("10uF") == "Capacitor_SMD:C_0603_1608Metric"

    def test_hand_solder_profile_capacitor_selection(self):
        """Test capacitor footprint selection with hand_solder profile."""
        selector = FootprintSelector(profile="hand_solder")

        # Hand solder uses larger packages
        assert selector.select_capacitor_footprint("100nF") == "Capacitor_SMD:C_0603_1608Metric"
        assert selector.select_capacitor_footprint("1uF") == "Capacitor_SMD:C_0805_2012Metric"

    def test_compact_profile_capacitor_selection(self):
        """Test capacitor footprint selection with compact profile."""
        selector = FootprintSelector(profile="compact")

        # Compact uses smallest viable packages
        assert selector.select_capacitor_footprint("10uF") == "Capacitor_SMD:C_0402_1005Metric"
        assert selector.select_capacitor_footprint("47uF") == "Capacitor_SMD:C_0603_1608Metric"


class TestFootprintSelectorResistors:
    """Tests for resistor footprint selection."""

    def test_default_profile_resistor_selection(self):
        """Test resistor footprint selection with default profile."""
        selector = FootprintSelector(profile="default")

        # ≤10k -> 0402
        assert selector.select_resistor_footprint("10k") == "Resistor_SMD:R_0402_1005Metric"
        assert selector.select_resistor_footprint("1k") == "Resistor_SMD:R_0402_1005Metric"

        # >10k -> 0603
        assert selector.select_resistor_footprint("100k") == "Resistor_SMD:R_0603_1608Metric"
        assert selector.select_resistor_footprint("1M") == "Resistor_SMD:R_0603_1608Metric"

    def test_machine_profile_resistor_selection(self):
        """Test resistor footprint selection with machine profile."""
        selector = FootprintSelector(profile="machine")

        # Machine profile uses 0402 for all values
        assert selector.select_resistor_footprint("10k") == "Resistor_SMD:R_0402_1005Metric"
        assert selector.select_resistor_footprint("100k") == "Resistor_SMD:R_0402_1005Metric"
        assert selector.select_resistor_footprint("1M") == "Resistor_SMD:R_0402_1005Metric"


class TestFootprintSelectorInductors:
    """Tests for inductor footprint selection."""

    def test_default_profile_inductor_selection(self):
        """Test inductor footprint selection with default profile."""
        selector = FootprintSelector(profile="default")

        # ≤1µH -> 0603
        assert selector.select_inductor_footprint("1uH") == "Inductor_SMD:L_0603_1608Metric"

        # ≤10µH -> 0805
        assert selector.select_inductor_footprint("10uH") == "Inductor_SMD:L_0805_2012Metric"

        # >10µH -> 1206
        assert selector.select_inductor_footprint("100uH") == "Inductor_SMD:L_1206_3216Metric"


class TestFootprintSelectorAutoDetection:
    """Tests for automatic component type detection."""

    def test_select_footprint_capacitor(self):
        """Test auto-detection of capacitor from lib_id."""
        selector = FootprintSelector(profile="default")

        fp = selector.select_footprint("Device:C", "100nF")
        assert fp == "Capacitor_SMD:C_0402_1005Metric"

        fp = selector.select_footprint("Device:C_Polarized", "10uF")
        assert fp == "Capacitor_SMD:C_0805_2012Metric"

    def test_select_footprint_resistor(self):
        """Test auto-detection of resistor from lib_id."""
        selector = FootprintSelector(profile="default")

        fp = selector.select_footprint("Device:R", "10k")
        assert fp == "Resistor_SMD:R_0402_1005Metric"

        fp = selector.select_footprint("Device:R_Small", "100k")
        assert fp == "Resistor_SMD:R_0603_1608Metric"

    def test_select_footprint_inductor(self):
        """Test auto-detection of inductor from lib_id."""
        selector = FootprintSelector(profile="default")

        fp = selector.select_footprint("Device:L", "10uH")
        assert fp == "Inductor_SMD:L_0805_2012Metric"

    def test_select_footprint_unknown_returns_none(self):
        """Test that unknown components return None."""
        selector = FootprintSelector(profile="default")

        fp = selector.select_footprint("Device:D", "1N4148")
        assert fp is None

        fp = selector.select_footprint("Connector:USB_C", "USB-C")
        assert fp is None


class TestSelectFootprintForPassive:
    """Tests for the convenience function."""

    def test_select_footprint_for_passive_default(self):
        """Test convenience function with default profile."""
        fp = select_footprint_for_passive("Device:C", "100nF")
        assert fp == "Capacitor_SMD:C_0402_1005Metric"

    def test_select_footprint_for_passive_custom_profile(self):
        """Test convenience function with custom profile."""
        fp = select_footprint_for_passive("Device:C", "100nF", profile="hand_solder")
        assert fp == "Capacitor_SMD:C_0603_1608Metric"


class TestFootprintSelectorFallback:
    """Tests for fallback behavior with invalid inputs."""

    def test_invalid_capacitor_value_fallback(self):
        """Test fallback for invalid capacitor values."""
        selector = FootprintSelector()
        fp = selector.select_capacitor_footprint("invalid")
        assert fp == "Capacitor_SMD:C_0603_1608Metric"  # Default fallback

    def test_invalid_resistor_value_fallback(self):
        """Test fallback for invalid resistor values."""
        selector = FootprintSelector()
        fp = selector.select_resistor_footprint("invalid")
        assert fp == "Resistor_SMD:R_0603_1608Metric"  # Default fallback

    def test_invalid_inductor_value_fallback(self):
        """Test fallback for invalid inductor values."""
        selector = FootprintSelector()
        fp = selector.select_inductor_footprint("invalid")
        assert fp == "Inductor_SMD:L_0805_2012Metric"  # Default fallback


class TestFootprintSelectorCustomRules:
    """Tests for custom rule configuration."""

    def test_custom_capacitor_rules(self):
        """Test custom capacitor rules override defaults."""
        custom_rules = {
            "capacitor": {
                "0-1uF": "Capacitor_SMD:C_0201_0603Metric",
                "1uF+": "Capacitor_SMD:C_0402_1005Metric",
            }
        }
        selector = FootprintSelector(profile="default", custom_rules=custom_rules)

        assert selector.select_capacitor_footprint("100nF") == "Capacitor_SMD:C_0201_0603Metric"
        assert selector.select_capacitor_footprint("10uF") == "Capacitor_SMD:C_0402_1005Metric"


class TestFootprintSelectorBoundaryValues:
    """Tests for boundary value conditions."""

    def test_exactly_at_boundary_capacitor(self):
        """Test values exactly at boundary thresholds."""
        selector = FootprintSelector(profile="default")

        # Exactly 100nF should be ≤100nF
        assert selector.select_capacitor_footprint("100nF") == "Capacitor_SMD:C_0402_1005Metric"

        # Exactly 1uF should be ≤1uF
        assert selector.select_capacitor_footprint("1uF") == "Capacitor_SMD:C_0603_1608Metric"

        # Exactly 10uF should be ≤10uF
        assert selector.select_capacitor_footprint("10uF") == "Capacitor_SMD:C_0805_2012Metric"

    def test_exactly_at_boundary_resistor(self):
        """Test values exactly at boundary thresholds."""
        selector = FootprintSelector(profile="default")

        # Exactly 10k should be ≤10k
        assert selector.select_resistor_footprint("10k") == "Resistor_SMD:R_0402_1005Metric"
