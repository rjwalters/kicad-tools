"""Tests for the part suggestion module."""

from __future__ import annotations

import pytest

from kicad_tools.cost.suggest import (
    ComponentType,
    extract_package_from_footprint,
    parse_component_value,
)


class TestExtractPackage:
    """Tests for extract_package_from_footprint function."""

    def test_capacitor_0402(self):
        """Test extracting 0402 from capacitor footprint."""
        assert extract_package_from_footprint("Capacitor_SMD:C_0402_1005Metric") == "0402"

    def test_resistor_0805(self):
        """Test extracting 0805 from resistor footprint."""
        assert extract_package_from_footprint("Resistor_SMD:R_0805_2012Metric") == "0805"

    def test_soic_8(self):
        """Test extracting SOIC-8 from package footprint."""
        assert extract_package_from_footprint("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm") == "SOIC-8"

    def test_tssop_20(self):
        """Test extracting TSSOP-20 from package footprint."""
        assert extract_package_from_footprint("Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm") == "TSSOP-20"

    def test_qfn_32(self):
        """Test extracting QFN-32 from package footprint."""
        assert extract_package_from_footprint("Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm") == "QFN-32"

    def test_sot_23(self):
        """Test extracting SOT-23 from package footprint."""
        assert extract_package_from_footprint("Package_TO_SOT_SMD:SOT-23") == "SOT-23"

    def test_empty_footprint(self):
        """Test empty footprint returns empty string."""
        assert extract_package_from_footprint("") == ""

    def test_unknown_footprint(self):
        """Test unknown footprint returns empty string."""
        assert extract_package_from_footprint("Custom:Unknown_Package") == ""


class TestParseComponentValue:
    """Tests for parse_component_value function."""

    def test_resistor_10k(self):
        """Test parsing 10k resistor value."""
        result = parse_component_value("10k", "R1")
        assert result.component_type == ComponentType.RESISTOR
        assert result.numeric_value == 10000
        assert result.unit == "Ω"
        assert "10k" in result.search_terms

    def test_resistor_4k7(self):
        """Test parsing 4.7k resistor value."""
        result = parse_component_value("4.7k", "R2")
        assert result.component_type == ComponentType.RESISTOR
        assert result.numeric_value == 4700
        assert "4.7k" in result.search_terms

    def test_resistor_100(self):
        """Test parsing 100 ohm resistor value."""
        result = parse_component_value("100", "R3")
        assert result.component_type == ComponentType.RESISTOR
        assert result.numeric_value == 100
        assert "100" in result.search_terms

    def test_resistor_1M(self):
        """Test parsing 1M resistor value."""
        result = parse_component_value("1M", "R4")
        assert result.component_type == ComponentType.RESISTOR
        assert result.numeric_value == 1_000_000
        assert "1M" in result.search_terms

    def test_capacitor_100nF(self):
        """Test parsing 100nF capacitor value."""
        result = parse_component_value("100nF", "C1")
        assert result.component_type == ComponentType.CAPACITOR
        assert result.numeric_value == pytest.approx(100e-9, rel=1e-6)
        assert result.unit == "F"
        assert "100nF" in result.search_terms

    def test_capacitor_10uF(self):
        """Test parsing 10uF capacitor value."""
        result = parse_component_value("10uF", "C2")
        assert result.component_type == ComponentType.CAPACITOR
        assert result.numeric_value == pytest.approx(10e-6, rel=1e-6)
        assert "10uF" in result.search_terms

    def test_capacitor_22pF(self):
        """Test parsing 22pF capacitor value."""
        result = parse_component_value("22pF", "C3")
        assert result.component_type == ComponentType.CAPACITOR
        assert result.numeric_value == pytest.approx(22e-12, rel=1e-6)
        assert "22pF" in result.search_terms

    def test_inductor_10uH(self):
        """Test parsing 10uH inductor value."""
        result = parse_component_value("10uH", "L1")
        assert result.component_type == ComponentType.INDUCTOR
        assert result.numeric_value == pytest.approx(10e-6, rel=1e-6)
        assert result.unit == "H"
        assert "10uH" in result.search_terms

    def test_ic_part_number(self):
        """Test parsing IC part number."""
        result = parse_component_value("STM32C011F4P6", "U1")
        assert result.component_type == ComponentType.IC
        assert "STM32C011F4P6" in result.search_terms

    def test_diode_reference(self):
        """Test diode reference detection."""
        result = parse_component_value("1N4148", "D1")
        assert result.component_type == ComponentType.DIODE

    def test_led_reference(self):
        """Test LED reference detection."""
        result = parse_component_value("LED_RED", "D2")
        assert result.component_type == ComponentType.LED

    def test_transistor_reference(self):
        """Test transistor reference detection."""
        result = parse_component_value("2N2222", "Q1")
        assert result.component_type == ComponentType.TRANSISTOR

    def test_connector_reference(self):
        """Test connector reference detection."""
        result = parse_component_value("USB_C", "J1")
        assert result.component_type == ComponentType.CONNECTOR
