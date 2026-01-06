"""
Unit tests for SPI interface specification.

Tests cover:
- SPIInterfaceSpec implements InterfaceSpec protocol
- All SPI variants defined and registered
- Constraint derivation for max length, length matching
- Intent-aware validation messages for common SPI violations
"""

import pytest

from kicad_tools.intent import (
    REGISTRY,
    ConstraintSeverity,
    InterfaceCategory,
    create_intent_declaration,
    derive_constraints,
    validate_intent,
)
from kicad_tools.intent.interfaces.spi import (
    SPI_VARIANTS,
    SPIInterfaceSpec,
    SPIVariant,
)


class TestSPIVariants:
    """Tests for SPI variant definitions."""

    def test_all_variants_defined(self):
        """Test that all SPI variants are defined."""
        expected_variants = ["spi_standard", "spi_fast", "spi_high_speed"]
        for variant in expected_variants:
            assert variant in SPI_VARIANTS, f"Missing variant: {variant}"

    def test_variant_dataclass_fields(self):
        """Test SPIVariant has all required fields."""
        variant = SPI_VARIANTS["spi_standard"]
        assert isinstance(variant, SPIVariant)
        assert hasattr(variant, "max_freq")
        assert hasattr(variant, "max_trace_length_mm")
        assert hasattr(variant, "length_tolerance_mm")
        assert hasattr(variant, "termination_recommended")

    def test_spi_standard_params(self):
        """Test SPI standard parameters."""
        variant = SPI_VARIANTS["spi_standard"]
        assert variant.max_freq == 10e6
        assert variant.max_trace_length_mm == 200.0
        assert variant.length_tolerance_mm is None
        assert variant.termination_recommended is False

    def test_spi_fast_params(self):
        """Test SPI fast parameters."""
        variant = SPI_VARIANTS["spi_fast"]
        assert variant.max_freq == 50e6
        assert variant.max_trace_length_mm == 100.0
        assert variant.length_tolerance_mm == 5.0
        assert variant.termination_recommended is False

    def test_spi_high_speed_params(self):
        """Test SPI high-speed parameters."""
        variant = SPI_VARIANTS["spi_high_speed"]
        assert variant.max_freq == 100e6
        assert variant.max_trace_length_mm == 50.0
        assert variant.length_tolerance_mm == 2.0
        assert variant.termination_recommended is True


class TestSPIInterfaceSpec:
    """Tests for SPIInterfaceSpec class."""

    def test_spec_creation_default_variant(self):
        """Test creating spec with default variant."""
        spec = SPIInterfaceSpec()
        assert spec.name == "spi_standard"

    def test_spec_creation_with_variant(self):
        """Test creating spec with specific variant."""
        spec = SPIInterfaceSpec("spi_high_speed")
        assert spec.name == "spi_high_speed"

    def test_spec_creation_invalid_variant(self):
        """Test that invalid variant raises ValueError."""
        with pytest.raises(ValueError, match="Unknown SPI variant"):
            SPIInterfaceSpec("spi_invalid")

    def test_category_is_bus(self):
        """Test that all SPI specs have BUS category."""
        for variant_name in SPI_VARIANTS:
            spec = SPIInterfaceSpec(variant_name)
            assert spec.category == InterfaceCategory.BUS

    def test_implements_interface_spec_protocol(self):
        """Test that SPIInterfaceSpec implements InterfaceSpec protocol."""
        spec = SPIInterfaceSpec()
        assert hasattr(spec, "name")
        assert hasattr(spec, "category")
        assert hasattr(spec, "validate_nets")
        assert hasattr(spec, "derive_constraints")
        assert hasattr(spec, "get_validation_message")


class TestSPINetValidation:
    """Tests for SPI net validation."""

    def test_validate_nets_correct_count(self):
        """Test validation passes with 3+ nets."""
        spec = SPIInterfaceSpec()
        errors = spec.validate_nets(["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS"])
        assert errors == []

    def test_validate_nets_minimum_count(self):
        """Test validation passes with minimum 3 nets."""
        spec = SPIInterfaceSpec()
        errors = spec.validate_nets(["SPI_CLK", "SPI_MOSI", "SPI_CS"])
        assert errors == []

    def test_validate_nets_too_few(self):
        """Test validation fails with 2 nets."""
        spec = SPIInterfaceSpec()
        errors = spec.validate_nets(["SPI_CLK", "SPI_MOSI"])
        assert len(errors) == 1
        assert "at least 3 nets" in errors[0]

    def test_validate_nets_empty(self):
        """Test validation fails with no nets."""
        spec = SPIInterfaceSpec()
        errors = spec.validate_nets([])
        assert len(errors) == 1


class TestSPIConstraintDerivation:
    """Tests for SPI constraint derivation."""

    def test_spi_standard_constraints(self):
        """Test SPI standard generates correct constraints."""
        spec = SPIInterfaceSpec("spi_standard")
        constraints = spec.derive_constraints(["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS"], {})

        constraint_types = {c.type for c in constraints}
        assert "max_length" in constraint_types
        # Standard doesn't have length matching
        assert "length_match" not in constraint_types

    def test_spi_high_speed_constraints(self):
        """Test SPI high-speed generates length matching and termination."""
        spec = SPIInterfaceSpec("spi_high_speed")
        constraints = spec.derive_constraints(["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS"], {})

        constraint_types = {c.type for c in constraints}
        assert "max_length" in constraint_types
        assert "length_match" in constraint_types
        assert "termination" in constraint_types

    def test_max_length_constraint_params(self):
        """Test max length constraint has correct parameters."""
        spec = SPIInterfaceSpec("spi_standard")
        constraints = spec.derive_constraints(["CLK", "MOSI", "MISO", "CS"], {})

        # Find max_length constraint for all nets
        max_length = next(c for c in constraints if c.type == "max_length" and "nets" in c.params)
        assert max_length.params["max_mm"] == 200.0
        assert max_length.source == "spi:spi_standard"
        assert max_length.severity == ConstraintSeverity.WARNING

    def test_length_match_constraint_params(self):
        """Test length match constraint has correct parameters."""
        spec = SPIInterfaceSpec("spi_high_speed")
        constraints = spec.derive_constraints(["CLK", "MOSI", "MISO", "CS"], {})

        length_constraint = next(c for c in constraints if c.type == "length_match")
        assert length_constraint.params["tolerance_mm"] == 2.0
        assert length_constraint.severity == ConstraintSeverity.WARNING

    def test_clock_net_detection(self):
        """Test clock net is detected from various naming patterns."""
        spec = SPIInterfaceSpec()

        # Test SCK pattern
        constraints = spec.derive_constraints(["SCK", "MOSI", "MISO", "CS"], {})
        clk_constraints = [c for c in constraints if c.params.get("net") == "SCK"]
        assert len(clk_constraints) > 0

        # Test SCLK pattern
        constraints = spec.derive_constraints(["SCLK", "MOSI", "MISO", "CS"], {})
        clk_constraints = [c for c in constraints if c.params.get("net") == "SCLK"]
        assert len(clk_constraints) > 0

    def test_variant_override_via_params(self):
        """Test variant can be overridden via params."""
        spec = SPIInterfaceSpec("spi_standard")
        constraints = spec.derive_constraints(
            ["CLK", "MOSI", "MISO", "CS"], {"variant": "spi_high_speed"}
        )

        # Should use high speed constraints even though spec is standard
        constraint_types = {c.type for c in constraints}
        assert "length_match" in constraint_types

    def test_constraint_source_format(self):
        """Test constraint source uses spi:variant format."""
        spec = SPIInterfaceSpec("spi_fast")
        constraints = spec.derive_constraints(["CLK", "MOSI", "MISO", "CS"], {})

        for constraint in constraints:
            assert constraint.source == "spi:spi_fast"


class TestSPIValidationMessages:
    """Tests for SPI validation message formatting."""

    def test_length_mismatch_message(self):
        """Test length mismatch validation message."""
        spec = SPIInterfaceSpec("spi_high_speed")
        msg = spec.get_validation_message({"type": "length_mismatch", "delta": 3.5})

        assert "SPI" in msg
        assert "3.5mm" in msg
        assert "signal integrity" in msg.lower()

    def test_max_length_message(self):
        """Test max length violation message."""
        spec = SPIInterfaceSpec("spi_standard")
        msg = spec.get_validation_message({"type": "max_length", "actual": 250})

        assert "250mm" in msg
        assert "200" in msg  # Max for standard
        assert "signal integrity" in msg.lower()

    def test_termination_message(self):
        """Test termination recommendation message."""
        spec = SPIInterfaceSpec("spi_high_speed")
        msg = spec.get_validation_message({"type": "termination"})

        assert "termination" in msg.lower()
        assert "22-33" in msg  # Typical resistor range

    def test_unknown_violation_fallback(self):
        """Test fallback for unknown violation types."""
        spec = SPIInterfaceSpec()
        msg = spec.get_validation_message({"type": "unknown", "message": "Custom message"})
        assert msg == "Custom message"


class TestSPIFrequencyFormatting:
    """Tests for frequency formatting helper."""

    def test_format_mhz(self):
        """Test formatting frequencies in MHz."""
        assert SPIInterfaceSpec._format_freq(10e6) == "10MHz"
        assert SPIInterfaceSpec._format_freq(50e6) == "50MHz"
        assert SPIInterfaceSpec._format_freq(100e6) == "100MHz"

    def test_format_ghz(self):
        """Test formatting frequencies in GHz."""
        assert SPIInterfaceSpec._format_freq(1e9) == "1GHz"


class TestSPIRegistryIntegration:
    """Tests for SPI interface registration in global registry."""

    def test_all_variants_registered(self):
        """Test all SPI variants are registered in global REGISTRY."""
        for variant_name in SPI_VARIANTS:
            assert variant_name in REGISTRY, f"Variant not registered: {variant_name}"

    def test_registered_spec_is_spi_interface_spec(self):
        """Test registered specs are SPIInterfaceSpec instances."""
        spec = REGISTRY.get("spi_standard")
        assert spec is not None
        assert isinstance(spec, SPIInterfaceSpec)

    def test_derive_constraints_via_registry(self):
        """Test deriving constraints through the registry."""
        constraints = derive_constraints(
            interface_type="spi_standard",
            nets=["CLK", "MOSI", "MISO", "CS"],
        )
        assert len(constraints) > 0
        assert any(c.type == "max_length" for c in constraints)

    def test_validate_intent_via_registry(self):
        """Test validating intent through the registry."""
        errors = validate_intent(
            interface_type="spi_standard",
            nets=["CLK", "MOSI", "MISO", "CS"],
        )
        assert errors == []

    def test_create_intent_declaration_via_registry(self):
        """Test creating intent declaration through the registry."""
        declaration = create_intent_declaration(
            interface_type="spi_fast",
            nets=["CLK", "MOSI", "MISO", "CS"],
            metadata={"device": "U1"},
        )
        assert declaration.interface_type == "spi_fast"
        assert len(declaration.constraints) > 0

    def test_spi_specs_in_bus_category(self):
        """Test SPI specs appear in BUS category listing."""
        bus_interfaces = REGISTRY.list_by_category(InterfaceCategory.BUS)
        for variant_name in SPI_VARIANTS:
            assert variant_name in bus_interfaces


class TestSPIAcceptanceCriteria:
    """Tests verifying issue acceptance criteria."""

    def test_implements_interface_spec_protocol(self):
        """AC: SPIInterfaceSpec implements InterfaceSpec protocol."""
        spec = SPIInterfaceSpec()
        assert isinstance(spec.name, str)
        assert isinstance(spec.category, InterfaceCategory)
        assert callable(spec.validate_nets)
        assert callable(spec.derive_constraints)
        assert callable(spec.get_validation_message)

    def test_three_speed_variants(self):
        """AC: SPIInterfaceSpec with 3 speed variants."""
        assert "spi_standard" in SPI_VARIANTS
        assert "spi_fast" in SPI_VARIANTS
        assert "spi_high_speed" in SPI_VARIANTS
        assert len(SPI_VARIANTS) == 3

    def test_intent_aware_messages(self):
        """AC: Intent-aware validation messages for SPI violations."""
        spec = SPIInterfaceSpec("spi_high_speed")

        msg = spec.get_validation_message({"type": "length_mismatch", "delta": 5.0})
        assert "SPI" in msg

        msg = spec.get_validation_message({"type": "max_length", "actual": 100})
        assert "SPI" in msg or "trace" in msg.lower()

    def test_registered_in_global_registry(self):
        """AC: Registered in global REGISTRY on module import."""
        for variant in SPI_VARIANTS:
            assert REGISTRY.get(variant) is not None
